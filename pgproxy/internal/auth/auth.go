// Package auth автентифікує студента на боці проксі.
//
// Ключова ідея: студент знає ТОКЕН, а не пароль ролі PostgreSQL. Проксі
// перевіряє токен сам, а до СУБД йде з паролем, який виводить із master_secret
// і ніде не зберігає. Наслідок: навіть якщо порт 5432 колись випадково
// опублікують, студент не зможе підключитися напряму й обійти журнал.
package auth

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/alexedwards/argon2id"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var (
	ErrUnknownRole = errors.New("невідома роль")
	ErrBadToken    = errors.New("токен не підходить")
	ErrNoToken     = errors.New("токен ще не випущено")
	ErrExpired     = errors.New("термін дії токена вичерпано")
	ErrDisabled    = errors.New("обліковий запис вимкнено")
	ErrWrongDB     = errors.New("це не ваша база")
	ErrNoConsent   = errors.New("згоду на ведення журналу не надано")
	ErrThrottled   = errors.New("забагато невдалих спроб")
)

// ServiceDB — службова база кластера. Єдина чужа база, до якої студенту
// дозволено підключитися: без неї не зробити DROP DATABASE своєї.
const ServiceDB = "postgres"

type Student struct {
	ID       string
	RoleName string
	DBName   string
	Slot     int
}

// Quota — стан мʼякої квоти. Значення кешоване фоновим job'ом застосунку
// (оновлюється раз на хвилину), тому може відставати. Це прийнятно: жорстку
// межу тримає квота файлової системи, а тут ми лише попереджаємо студента
// раніше, ніж лабораторна зламається посеред роботи.
type Quota struct {
	Used  int64
	Limit int64
}

func (q Quota) Exceeded() bool { return q.Limit > 0 && q.Used >= q.Limit }

// LoadQuota читає кешований розмір слота студента.
func LoadQuota(ctx context.Context, pool *pgxpool.Pool, studentID string) (Quota, error) {
	var q Quota
	err := pool.QueryRow(ctx, `
		SELECT coalesce(s.used_bytes, 0), s.quota_bytes
		  FROM obs.ts_slots s
		 WHERE s.student_id = $1::uuid`, studentID).Scan(&q.Used, &q.Limit)
	return q, err
}

// RolePassword — той самий вивід, що й у front/app/credentials.py.
// Обидві реалізації мусять збігатися байт у байт, інакше проксі не увійде.
func RolePassword(master []byte, studentID string) string {
	mac := hmac.New(sha256.New, master)
	mac.Write([]byte(studentID))
	return strings.TrimRight(base64.URLEncoding.EncodeToString(mac.Sum(nil)), "=")
}

// verifySlots обмежує кількість ОДНОЧАСНИХ перевірок argon2.
//
// Кожна перевірка виділяє стільки памʼяті, скільки записано в самому хеші
// (параметр m). Для типових налаштувань argon2-cffi це 64 МБ. Пара студентів
// цього не помітить, а от початок заняття — коли 30 людей підключаються за
// кілька секунд — множить 64 МБ на 30 і вбиває проксі. Саме так і сталося:
// під навантаженням у 10 сесій контейнер гинув і перезапускався.
//
// Тому перевірки шикуються в чергу. Вхід стає трохи повільнішим, зате
// споживання памʼяті обмежене згори незалежно від параметрів хеша.
var verifySlots chan struct{}

func SetVerifyParallelism(n int) {
	if n < 1 {
		n = 1
	}
	verifySlots = make(chan struct{}, n)
}

func init() { SetVerifyParallelism(3) }

// Authenticate звіряє (роль, токен) і перевіряє, що студент іде у СВОЮ базу.
func Authenticate(ctx context.Context, pool *pgxpool.Pool, role, token, database string) (*Student, error) {
	var (
		s           Student
		tokenHash   *string
		expiresAt   *string
		disabledAt  *string
		consentedAt *string
		dbName      *string
		slot        *int
	)
	err := pool.QueryRow(ctx, `
		SELECT id::text, role_name, db_name, slot,
		       db_token_hash,
		       db_token_expires_at::text,
		       disabled_at::text,
		       consented_at::text
		  FROM obs.students
		 WHERE role_name = $1`, role).
		Scan(&s.ID, &s.RoleName, &dbName, &slot,
			&tokenHash, &expiresAt, &disabledAt, &consentedAt)
	if err != nil {
		// Розрізняємо «такої ролі немає» і «запит не вдався». Раніше обидва
		// давали ErrUnknownRole, і збій читання виглядав як невірний логін —
		// тобто справжня причина не потрапляла нікуди.
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, ErrUnknownRole
		}
		return nil, fmt.Errorf("не вдалося прочитати студента %q: %w", role, err)
	}
	if disabledAt != nil {
		return nil, ErrDisabled
	}
	// Згода — підстава вести журнал, а не формальність на вебі. Якщо
	// перевіряти її лише в кабінеті, позначка «без згоди» в адмінці стає
	// косметичною: токен, виданий в обхід, працює далі. Тому відмова тут.
	if consentedAt == nil {
		return nil, ErrNoConsent
	}
	if tokenHash == nil {
		return nil, ErrNoToken
	}
	if expiresAt == nil {
		return nil, ErrExpired
	}
	// Обмежувач стоїть саме тут: після дешевого читання з каталогу (щоб знати,
	// чия це роль) і ПЕРЕД argon2 — бо захищає він не токен, а слоти.
	if !LoginThrottle.Allow(role) {
		return nil, ErrThrottled
	}
	verifySlots <- struct{}{}
	ok, err := argon2id.ComparePasswordAndHash(token, *tokenHash)
	<-verifySlots
	if err != nil || !ok {
		if LoginThrottle.Fail(role) {
			noteThrottled(pool, s.ID, role)
		}
		return nil, ErrBadToken
	}
	LoginThrottle.Reset(role)

	// Службова база postgres дозволена: студент має змогу зробити
	// DROP DATABASE своєї бази з psql. Усе інше — тільки власна база.
	if dbName != nil {
		s.DBName = *dbName
	}
	if database != ServiceDB && database != s.DBName {
		return nil, ErrWrongDB
	}
	if slot != nil {
		s.Slot = *slot
	}
	return &s, nil
}

// ExpiredCheck винесено окремо, бо порівняння часу робить сама СУБД:
// так не залежимо від годинника контейнера проксі.
func IsTokenLive(ctx context.Context, pool *pgxpool.Pool, studentID string) (bool, error) {
	var live bool
	err := pool.QueryRow(ctx,
		`SELECT db_token_expires_at > now() FROM obs.students WHERE id = $1::uuid`,
		studentID).Scan(&live)
	return live, err
}

// noteThrottled лишає слід у даних, а не лише в логах контейнера. Мовчазна
// відмова на вході гірша за відсутність обмежувача: студент бачить «не пускає»,
// викладач не бачить нічого. Та сама логіка, що з obs.journal_gaps.
func noteThrottled(pool *pgxpool.Pool, studentID, role string) {
	// Свій контекст: клієнт на цьому місці вже отримує відмову і зазвичай
	// одразу рве зʼєднання, а подія має дожити до бази.
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_, err := pool.Exec(ctx, `
		INSERT INTO obs.db_events (student_id, event, actor, details)
		VALUES ($1::uuid, 'rate_limited', 'system',
		        jsonb_build_object('where', 'proxy_login', 'role', $2::text))`,
		studentID, role)
	if err != nil {
		slog.Warn("подію rate_limited не записано", "err", err)
	}
}
