// Package proxy — власне проксі протоколу PostgreSQL.
//
// Насос повнодуплексний: дві горутини незалежно перекладають повідомлення в
// обидва боки. Синхронна схема («переслав запит — читаю відповідь до
// ReadyForQuery») виглядає простішою, але ламається одразу на двох речах:
// у розширеному протоколі після Parse відповіді немає до Sync, а в режимі COPY
// клієнт має надсилати дані, поки ми нібито чекаємо на бекенд.
package proxy

import (
	"context"
	"crypto/rand"
	"crypto/tls"
	"encoding/binary"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgproto3"
	"github.com/jackc/pgx/v5/pgxpool"

	"wireward/pgproxy/internal/auth"
	"wireward/pgproxy/internal/config"
	"wireward/pgproxy/internal/journal"
)

type Server struct {
	cfg     *config.Config
	pool    *pgxpool.Pool
	jw      *journal.Writer
	tlsCfg  *tls.Config

	sem chan struct{} // стеля бекенд-зʼєднань: понад неї черзуємо, а не відкриваємо

	// Стеля прийнятих, але ще НЕ автентифікованих зʼєднань. s.sem рахує лише
	// тих, хто автентифікацію вже пройшов, а сокет живе з моменту Accept:
	// рукостискання тримає дескриптор, якого не бачить жоден лічильник.
	// Кілька тисяч мовчазних TCP-зʼєднань (пароль не потрібен, порти
	// опубліковані) вичерпували ulimits.nofile контейнера — тут вони
	// відкидаються одразу після Accept.
	//
	// Слот звільняється рівно тоді, коли закінчується передавторизаційне
	// вікно (успішний Authenticate або будь-який ранній вихід), а НЕ по
	// завершенні сесії: інакше це була б глобальна стеля сесій, і легітимний
	// студент у черзі на s.sem зʼїдав би слот усю сесію, а наступний клієнт
	// отримував би голий close замість чесного FATAL 53300.
	preauth chan struct{}

	dropLog *logThrottle // тротлінг логу відмов передавторизаційної стелі

	cancelMu sync.Mutex
	cancels  map[cancelKey]cancelTarget
}

type cancelKey struct{ pid, secret uint32 }
type cancelTarget struct{ pid, secret uint32 }

func New(cfg *config.Config, pool *pgxpool.Pool, jw *journal.Writer, tlsCfg *tls.Config) *Server {
	return &Server{
		cfg: cfg, pool: pool, jw: jw, tlsCfg: tlsCfg,
		sem:     make(chan struct{}, cfg.MaxBackends),
		preauth: make(chan struct{}, preauthLimit(cfg.MaxBackends)),
		dropLog: newLogThrottle(time.Second, slog.LevelWarn,
			"відкинуто до автентифікації: стеля рукостискань"),
		cancels: map[cancelKey]cancelTarget{},
	}
}

// preauthLimit — стеля сокетів у рукостисканні: 2× від стелі бекендів, щоб
// нормальна хвиля перепідключень на початку пари проходила без відмов, але з
// нижньою межею для крихітних конфігів у тестах.
func preauthLimit(maxBackends int) int {
	n := maxBackends * 2
	if n < 64 {
		n = 64
	}
	return n
}

func (s *Server) Listen(ctx context.Context, addr string) error {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return err
	}
	port := portOf(ln.Addr())
	slog.Info("слухаємо", "addr", addr, "port", port)
	go func() {
		<-ctx.Done()
		_ = ln.Close()
	}()
	defer s.dropLog.stop() // залишок вікна відмов — у лог перед зупинкою
	return AcceptLoop(ctx, ln, func(c net.Conn) {
		select {
		case s.preauth <- struct{}{}:
			release := releaseOnce(s.preauth)
			go func() {
				defer release() // страховка на всі ранні виходи з handle
				s.handle(ctx, c, port, release)
			}()
		default:
			// Стеля рукостискань вичерпана: тримати сокет десятки секунд
			// сенсу немає — саме це утримання і зʼїдає дескриптори.
			// Закриваємо одразу, ErrorResponse слати нема куди: клієнт ще не
			// надіслав startup.
			_ = c.Close()
			s.dropLog.note("limit", cap(s.preauth))
		}
	})
}

// releaseOnce — звільнення слота семафора, безпечне при подвійному виклику:
// handle звільняє його явно наприкінці передавторизаційного вікна, а defer у
// викликачі підстраховує ранні виходи.
func releaseOnce(sem chan struct{}) func() {
	var once sync.Once
	return func() { once.Do(func() { <-sem }) }
}

// handshakeDeadline — скільки даємо на передавторизаційне вікно: SSLRequest,
// TLS, StartupMessage, пароль. Живому клієнтові (psql, pgjdbc, psycopg3) це
// займає десятки мілісекунд навіть на поганому каналі, тож 5с — вже дуже
// щедро, а не тонка межа. Тримати тут колишні 30с сенсу немає: рівно стільки
// мовчазний сокет утримував би слот стелі рукостискань, дескриптор і — під
// флудом — чужий CancelRequest, який приходить окремим зʼєднанням.
//
// Раніше тут була умова «5с під тиском, інакше 30с». Вона рахувалась один раз
// на вході в handle, тож у сплеску на всю стелю коротший дедлайн діставався
// лише останній чверті зʼєднань — саме тим, які й так вже відкидались.
// Постійні 5с простіші й діють рівномірно.
const handshakeDeadline = 5 * time.Second

func portOf(a net.Addr) int {
	if t, ok := a.(*net.TCPAddr); ok {
		return t.Port
	}
	return 0
}

// ---------------------------------------------------------------- зʼєднання

// handle обслуговує одне клієнтське зʼєднання. releasePreauth звільняє слот
// стелі рукостискань; викликається тут же, щойно передавторизаційне вікно
// закінчилось (успіх Authenticate), і повторно — з defer у викликача на всіх
// ранніх виходах. Виклик ідемпотентний.
func (s *Server) handle(ctx context.Context, raw net.Conn, listenPort int, releasePreauth func()) {
	defer raw.Close()
	_ = raw.SetDeadline(time.Now().Add(handshakeDeadline)) // на час рукостискання

	be := pgproto3.NewBackend(raw, raw)
	startup, err := be.ReceiveStartupMessage()
	if err != nil {
		return
	}

	var conn net.Conn = raw
	tlsVersion := ""

	switch m := startup.(type) {
	case *pgproto3.SSLRequest:
		if _, err := raw.Write([]byte{'S'}); err != nil {
			return
		}
		tc := tls.Server(raw, s.tlsCfg)
		if err := tc.HandshakeContext(ctx); err != nil {
			slog.Debug("TLS", "err", err)
			return
		}
		tlsVersion = tlsName(tc.ConnectionState().Version)
		conn = tc
		be = pgproto3.NewBackend(tc, tc)
		if startup, err = be.ReceiveStartupMessage(); err != nil {
			return
		}
	case *pgproto3.GSSEncRequest:
		_, _ = raw.Write([]byte{'N'})
		return
	case *pgproto3.CancelRequest:
		// Клієнтський сокет тут уже не потрібен, а форвард скасування йде
		// власним зʼєднанням до бекенда — слот стелі звільняємо наперед.
		releasePreauth()
		s.forwardCancel(m)
		return
	}

	sm, ok := startup.(*pgproto3.StartupMessage)
	if !ok {
		return
	}
	// TLS обовʼязковий: далі ми просимо пароль відкритим текстом (це токен),
	// і без шифрування він пішов би по мережі як є.
	if tlsVersion == "" {
		fatal(be, "28000", "зʼєднання без TLS відхилено: додайте sslmode=require")
		return
	}

	user := sm.Parameters["user"]
	database := sm.Parameters["database"]
	if database == "" {
		database = user
	}
	appName := sm.Parameters["application_name"]

	// --- автентифікація токеном --------------------------------------------
	be.Send(&pgproto3.AuthenticationCleartextPassword{})
	if err := be.Flush(); err != nil {
		return
	}
	pmsg, err := be.Receive()
	if err != nil {
		return
	}
	pw, ok := pmsg.(*pgproto3.PasswordMessage)
	if !ok {
		return
	}

	// Пароль отримано — чекати на клієнта більше нема чого, тож короткий
	// дедлайн передавторизаційного вікна знімаємо вже тут, до Authenticate.
	// Далі все повільне — наше: argon2 (сьогодні 25-40мс, але параметри хешу
	// живуть у front/app/credentials.py і можуть поважчати), черга на s.sem
	// (до 10с), підключення до бекенда. З дедлайном 5с студент отримав би
	// тихий i/o timeout замість сесії — або, що гірше, замість тексту
	// «токен протермінований, перевидайте в кабінеті»: fatal() писав би
	// 28P01/53300 у сокет із простроченим дедлайном.
	// Не знімаємо повністю: до AuthenticationOk сокет не має висіти вічно.
	_ = raw.SetDeadline(time.Now().Add(60 * time.Second))

	student, err := auth.Authenticate(ctx, s.pool, user, pw.Password, database)
	if err != nil {
		// Відмова через несправність — не те саме, що невірний токен:
		// перша означає, що зламані МИ, і має бути видно як помилка.
		if errors.Is(err, auth.ErrUnknownRole) || errors.Is(err, auth.ErrBadToken) ||
			errors.Is(err, auth.ErrNoToken) || errors.Is(err, auth.ErrExpired) ||
			errors.Is(err, auth.ErrWrongDB) || errors.Is(err, auth.ErrDisabled) ||
			errors.Is(err, auth.ErrNoConsent) || errors.Is(err, auth.ErrThrottled) {
			slog.Info("відмова", "user", user, "db", database, "err", err)
		} else {
			slog.Error("АВТЕНТИФІКАЦІЯ ЗЛАМАНА", "user", user, "db", database, "err", err)
		}
		fatal(be, "28P01", authMessage(err))
		return
	}

	// Передавторизаційне вікно закінчилось: далі це вже облікований студент,
	// його черга й сесія рахуються через s.sem. Тримати слот стелі
	// рукостискань довше означало б перетворити її на стелю сесій.
	releasePreauth()

	// --- стеля бекендів ------------------------------------------------------
	select {
	case s.sem <- struct{}{}:
		defer func() { <-s.sem }()
	case <-time.After(10 * time.Second):
		fatal(be, "53300", "сервер перевантажений, спробуйте за хвилину")
		return
	}

	// --- бекенд-зʼєднання ----------------------------------------------------
	// pgconn робить startup і SCRAM за нас, після чого ми забираємо сокет.
	// Писати SCRAM руками заради цього не варто.
	dsn := fmt.Sprintf("host=%s port=%d user=%s password=%s dbname=%s sslmode=disable application_name=%s",
		s.cfg.BackendHost, s.cfg.BackendPort, student.RoleName,
		auth.RolePassword(s.cfg.MasterSecret, student.ID), database, quoteApp(appName))
	pgc, err := pgconn.Connect(ctx, dsn)
	if err != nil {
		slog.Warn("бекенд", "err", err)
		fatal(be, "08006", "не вдалося підключитися до бази: "+err.Error())
		return
	}
	hj, err := pgc.Hijack()
	if err != nil {
		_ = pgc.Close(ctx)
		fatal(be, "08006", "внутрішня помилка проксі")
		return
	}
	defer hj.Conn.Close()
	fe := pgproto3.NewFrontend(hj.Conn, hj.Conn)

	// Клієнту видаємо ВЛАСНИЙ ключ скасування й тримаємо мапу на справжній,
	// інакше Ctrl-C у psql перестане працювати.
	ourPID, ourSecret := randKey(), randKey()
	s.cancelMu.Lock()
	s.cancels[cancelKey{ourPID, ourSecret}] = cancelTarget{hj.PID, hj.SecretKey}
	s.cancelMu.Unlock()
	defer func() {
		s.cancelMu.Lock()
		delete(s.cancels, cancelKey{ourPID, ourSecret})
		s.cancelMu.Unlock()
	}()

	// Ми вже спожили стартову послідовність бекенда, тож відтворюємо її клієнту.
	be.Send(&pgproto3.AuthenticationOk{})
	for k, v := range hj.ParameterStatuses {
		be.Send(&pgproto3.ParameterStatus{Name: k, Value: v})
	}
	be.Send(&pgproto3.BackendKeyData{ProcessID: ourPID, SecretKey: ourSecret})
	// Підключення до службової бази дозволене навмисно (DROP DATABASE своєї),
	// але мовчки воно виглядає як «моя база зламалася»: перший же CREATE TABLE
	// дає 42501 permission denied for schema public. Тому попереджаємо одразу.
	if n := serviceDBNotice(database, student.DBName); n != nil {
		be.Send(n)
	}
	be.Send(&pgproto3.ReadyForQuery{TxStatus: 'I'})
	if err := be.Flush(); err != nil {
		return
	}
	_ = conn.SetDeadline(time.Time{}) // рукостискання позаду

	sess := s.newSession(ctx, student, conn, sm.Parameters, listenPort, tlsVersion, hj)
	defer sess.finish(context.WithoutCancel(ctx))

	sess.pump(be, fe, hj)
}

func authMessage(err error) string {
	switch {
	case errors.Is(err, auth.ErrNoToken):
		return "пароль ще не випущено — відкрийте кабінет Wireward і натисніть «Випустити пароль»"
	case errors.Is(err, auth.ErrExpired):
		return "термін дії пароля вичерпано — випустіть новий у кабінеті Wireward"
	case errors.Is(err, auth.ErrWrongDB):
		return "ця база вам не належить"
	case errors.Is(err, auth.ErrDisabled):
		return "обліковий запис вимкнено"
	case errors.Is(err, auth.ErrThrottled):
		return "забагато невдалих спроб входу — зачекайте хвилину і спробуйте ще раз"
	case errors.Is(err, auth.ErrNoConsent):
		return "зайдіть у кабінет Wireward і прочитайте, який журнал ведеться: " +
			"без вашої згоди підключення не відкривається"
	default:
		return "невірний користувач або пароль"
	}
}

// pump — повнодуплексний насос. Дві горутини незалежно перекладають
// повідомлення в обидва боки. Синхронна схема («переслав запит — читаю до
// ReadyForQuery») ламається двічі: у розширеному протоколі після Parse
// відповіді немає до Sync, а в режимі COPY клієнт має надсилати дані, поки ми
// нібито чекаємо на бекенд.
func (sess *session) pump(be *pgproto3.Backend, fe *pgproto3.Frontend, hj *pgconn.HijackedConn) {
	sess.be = be
	done := make(chan struct{})

	go func() { // бекенд -> клієнт
		defer close(done)
		for {
			msg, err := fe.Receive()
			if err != nil {
				return
			}
			sess.fromBackend(msg)
			sess.writeMu.Lock()
			be.Send(msg)
			err = be.Flush()
			sess.writeMu.Unlock()
			if err != nil {
				return
			}
		}
	}()

	for { // клієнт -> бекенд
		msg, err := be.Receive()
		if err != nil {
			break
		}
		if !sess.fromClient(msg) {
			continue // відхилено політикою, до бекенда не йде
		}
		fe.Send(msg)
		if err := fe.Flush(); err != nil {
			break
		}
	}
	_ = hj.Conn.Close()
	<-done
}

// ---------------------------------------------------------------- скасування

func (s *Server) cancelBackend(pid, secret uint32) {
	c, err := net.DialTimeout("tcp",
		fmt.Sprintf("%s:%d", s.cfg.BackendHost, s.cfg.BackendPort), 5*time.Second)
	if err != nil {
		return
	}
	defer c.Close()
	buf := make([]byte, 16)
	binary.BigEndian.PutUint32(buf[0:], 16)
	binary.BigEndian.PutUint32(buf[4:], 80877102) // код CancelRequest
	binary.BigEndian.PutUint32(buf[8:], pid)
	binary.BigEndian.PutUint32(buf[12:], secret)
	_, _ = c.Write(buf)
}

func (s *Server) forwardCancel(m *pgproto3.CancelRequest) {
	s.cancelMu.Lock()
	t, ok := s.cancels[cancelKey{m.ProcessID, m.SecretKey}]
	s.cancelMu.Unlock()
	if !ok {
		return
	}
	s.cancelBackend(t.pid, t.secret)
}

// ---------------------------------------------------------------- дрібниці

func fatal(be *pgproto3.Backend, code, msg string) {
	be.Send(&pgproto3.ErrorResponse{Severity: "FATAL", Code: code, Message: msg})
	_ = be.Flush()
}

// serviceDBNotice — попередження «ви не у своїй базі» для стартової
// послідовності. Повертає nil, коли попереджати нема про що: студент зайшов
// у власну базу або ми не знаємо її імені (тоді порада була б порожньою).
//
// Це NoticeResponse, а не ErrorResponse: зʼєднання лишається робочим, і всі
// клієнти (psql, pgjdbc через SQLWarning, psycopg3 через notices) показують
// його, не рвучи сесію. WARNING/01000, бо NOTICE у pgjdbc легше загубити.
func serviceDBNotice(database, ownDB string) *pgproto3.NoticeResponse {
	if database != auth.ServiceDB || ownDB == "" || ownDB == auth.ServiceDB {
		return nil
	}
	return &pgproto3.NoticeResponse{
		Severity:            "WARNING",
		SeverityUnlocalized: "WARNING",
		Code:                "01000",
		Message: "ви підключені до службової бази postgres, а не до своєї — " +
			"ваша база називається " + ownDB,
		Detail: "у службовій базі у вас немає прав на схему public, тому " +
			"CREATE TABLE, INSERT і SELECT тут відповідатимуть " +
			"«permission denied for schema public» — це не поломка вашої бази",
		Hint: "перепідключіться до бази " + ownDB + "; у postgres має сенс " +
			"лишатися хіба щоб виконати DROP DATABASE " + ownDB,
	}
}

func randKey() uint32 {
	b := make([]byte, 4)
	_, _ = rand.Read(b)
	return binary.BigEndian.Uint32(b)
}

func tlsName(v uint16) string {
	switch v {
	case tls.VersionTLS13:
		return "TLSv1.3"
	case tls.VersionTLS12:
		return "TLSv1.2"
	default:
		return fmt.Sprintf("0x%04x", v)
	}
}

func nullIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func quoteApp(s string) string {
	if s == "" {
		return "observer"
	}
	return strings.Map(func(r rune) rune {
		if r < 32 || r == '\'' || r == '\\' || r == ' ' {
			return '_'
		}
		return r
	}, s)
}

