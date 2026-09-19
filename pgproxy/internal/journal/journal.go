// Package journal — шлях запису журналу.
//
// Записи НЕ йдуть у базу напряму. Проксі дописує їх у локальний append-only
// файл із fsync пачками, а окрема горутина-shipper вантажить пачки в
// obs.queries. Причина: незалежного дубля журналу немає (ми свідомо відмовились
// від pgAudit, ADR-001), тож падіння або перезапуск бази observer не має
// коштувати жодного запису.
package journal

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Query struct {
	SessionID    string    `json:"session_id"`
	StudentID    string    `json:"student_id"`
	Seq          int       `json:"seq"`
	StartedAt    time.Time `json:"started_at"`
	DurationMS   float64   `json:"duration_ms"`
	SQLText      string    `json:"sql_text"`
	SQLTruncated bool      `json:"sql_truncated"`
	Fingerprint  string    `json:"sql_fingerprint"`
	Kind         string    `json:"sql_kind"`
	Protocol     string    `json:"protocol"`
	Params       string    `json:"params,omitempty"` // JSON-масив значень із Bind
	Status       string    `json:"status"`
	SQLState     string    `json:"sqlstate,omitempty"`
	ErrorMessage string    `json:"error_message,omitempty"`
	ErrorPos     int       `json:"error_position,omitempty"`
	RowsAffected *int64    `json:"rows_affected,omitempty"`
	IsCatalog    bool      `json:"is_catalog"`
	IsService    bool      `json:"is_service"`
}

// Writer: append-only файл, fsync пачками.
type Writer struct {
	mu    sync.Mutex
	f     *os.File
	w     *bufio.Writer
	dirty bool
}

func NewWriter(path string) (*Writer, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return nil, err
	}
	jw := &Writer{f: f, w: bufio.NewWriterSize(f, 64<<10)}
	go jw.syncLoop()
	return jw, nil
}

func (jw *Writer) Write(q *Query) error {
	b, err := json.Marshal(q)
	if err != nil {
		return err
	}
	jw.mu.Lock()
	defer jw.mu.Unlock()
	if _, err := jw.w.Write(append(b, '\n')); err != nil {
		return err
	}
	jw.dirty = true
	return nil
}

func (jw *Writer) syncLoop() {
	t := time.NewTicker(50 * time.Millisecond)
	for range t.C {
		jw.mu.Lock()
		if jw.dirty {
			_ = jw.w.Flush()
			_ = jw.f.Sync()
			jw.dirty = false
		}
		jw.mu.Unlock()
	}
}

func (jw *Writer) Close() error {
	jw.mu.Lock()
	defer jw.mu.Unlock()
	_ = jw.w.Flush()
	_ = jw.f.Sync()
	return jw.f.Close()
}

// Shipper читає spool із збереженого офсету і вантажить пачки в obs.queries.
// Офсет посувається ЛИШЕ після успішного запису — тому падіння бази означає
// затримку, а не втрату.
type Shipper struct {
	pool      *pgxpool.Pool
	spool     string
	offsetTag string

	// Застряглий shipper — це журнал, який мовчки не наповнюється. Раніше про
	// це свідчив лише WARN у логах, тобто ніщо. Тепер після stallThreshold
	// невдач поспіль відкривається запис у obs.journal_gaps, і адмінка
	// зобовʼязана показати цей проміжок поверх графіків активності.
	failures int
	gapID    int64
}

const stallThreshold = 20 // ~10 секунд при тику 500 мс

// Скільки рядків беремо за один прохід. Разом із «доганяємо без пауз» нижче
// це визначає пропускну здатність: раніше один прохід на тик давав стелю
// 1000 рядків/с, а пік навантаження на 40 сесіях — близько 1600. Журнал не
// втрачався, але відставав на десятки секунд, і «останні запити» в адмінці
// показували те, що вже минуло.
const batchLimit = 2000

func NewShipper(pool *pgxpool.Pool, spool string) *Shipper {
	return &Shipper{pool: pool, spool: spool, offsetTag: spool + ".offset"}
}

// adoptOpenGap підхоплює проміжок, відкритий ПОПЕРЕДНІМ процесом.
//
// gapID живе в памʼяті, тож застряглий shipper, який перезапустили, лишав
// свій проміжок відкритим назавжди: адмінка вічно показувала «журнал
// неповний», а справжню наступну зупинку в цьому шумі вже не видно.
// Проміжок закриється сам на першій успішній пачці — тобто рівно тоді, коли
// журнал справді відновився.
func (s *Shipper) adoptOpenGap(ctx context.Context) {
	var id int64
	err := s.pool.QueryRow(ctx, `
		SELECT id FROM obs.journal_gaps
		 WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1`).Scan(&id)
	if err != nil {
		return // проміжків немає — звичайний випадок
	}
	s.gapID = id
	slog.Warn("підхоплено незакритий проміжок журналу від попереднього запуску",
		"gap_id", id)
}

func (s *Shipper) noteFailure(ctx context.Context, err error) {
	s.failures++
	if s.failures < stallThreshold || s.gapID != 0 {
		return
	}
	q := `INSERT INTO obs.journal_gaps (started_at, reason, details)
	      VALUES (now(), 'shipper_stalled', jsonb_build_object('error', $1::text))
	      RETURNING id`
	if e := s.pool.QueryRow(ctx, q, err.Error()).Scan(&s.gapID); e != nil {
		slog.Error("не вдалося записати journal_gaps", "err", e)
		return
	}
	slog.Error("SHIPPER ЗАСТРЯГ: журнал не наповнюється", "gap_id", s.gapID, "err", err)
}

func (s *Shipper) noteSuccess(ctx context.Context) {
	if s.gapID != 0 {
		if _, err := s.pool.Exec(ctx,
			`UPDATE obs.journal_gaps SET ended_at = now() WHERE id = $1`, s.gapID); err == nil {
			slog.Warn("shipper відновився, проміжок закрито", "gap_id", s.gapID)
			s.gapID = 0
		}
	}
	s.failures = 0
}

func (s *Shipper) Run(ctx context.Context) {
	s.adoptOpenGap(ctx)
	t := time.NewTicker(500 * time.Millisecond)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			n, err := s.once(ctx)
			switch {
			case err != nil:
				slog.Warn("shipper", "err", err, "поспіль_невдач", s.failures+1)
				s.noteFailure(ctx, err)
			default:
				s.noteSuccess(ctx)
				if n > 0 {
					slog.Debug("shipper", "rows", n)
				}
			}
		}
	}
}

func (s *Shipper) readOffset() int64 {
	b, err := os.ReadFile(s.offsetTag)
	if err != nil {
		return 0
	}
	var off int64
	_, _ = fmt.Sscanf(string(b), "%d", &off)
	return off
}

func (s *Shipper) writeOffset(off int64) error {
	tmp := s.offsetTag + ".tmp"
	if err := os.WriteFile(tmp, []byte(fmt.Sprintf("%d", off)), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, s.offsetTag)
}

func (s *Shipper) once(ctx context.Context) (int, error) {
	f, err := os.Open(s.spool)
	if err != nil {
		return 0, nil // ще не створений
	}
	defer f.Close()

	off := s.readOffset()
	if _, err := f.Seek(off, 0); err != nil {
		return 0, err
	}

	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 1<<20), 8<<20)

	var (
		batch   []*Query
		consumed int64
	)
	for sc.Scan() && len(batch) < batchLimit {
		line := sc.Bytes()
		consumed += int64(len(line)) + 1
		var q Query
		if err := json.Unmarshal(line, &q); err != nil {
			// Пошкоджений рядок пропускаємо, але гучно: втрата запису в
			// журналі аудиту не має проходити тихо.
			slog.Error("нечитабельний рядок spool", "err", err)
			continue
		}
		batch = append(batch, &q)
	}
	if len(batch) == 0 {
		return 0, sc.Err()
	}

	if err := s.insert(ctx, batch); err != nil {
		// Пачка йде одним COPY, тож один непридатний рядок валить усі. Поки
		// причина може бути тимчасовою (база лежить, мережа) — просто чекаємо:
		// офсет не рухаємо і спробуємо ті самі рядки наступного разу.
		if s.failures < stallThreshold {
			return 0, err
		}
		// Після stallThreshold невдач поспіль зрозуміло, що чекати нема чого:
		// або база недоступна (тоді порядкова вставка теж не пройде і нічого не
		// зміниться), або серед рядків є отруйний. Розділяємо пачку, щоб один
		// зіпсований запис не зупиняв ВЕСЬ журнал назавжди. Саме так і сталося:
		// duration_ms, порахований від нульового часу, не вміщувався в колонку,
		// і shipper повторював ту саму пачку без кінця.
		n, err := s.insertOneByOne(ctx, batch)
		if n == 0 {
			return 0, err
		}
	}
	if err := s.writeOffset(off + consumed); err != nil {
		return 0, err
	}
	return len(batch), nil
}

// insertOneByOne вставляє рядки поодинці й повертає кількість успішних.
// Рядок, який не проходить сам по собі, втрачений безповоротно — тому про
// нього кричимо в лог із текстом запиту: у журналі аудиту тиха втрата
// неприпустима, а зупинити через нього весь журнал — ще гірше.
func (s *Shipper) insertOneByOne(ctx context.Context, batch []*Query) (int, error) {
	ok := 0
	var last error
	for _, q := range batch {
		if err := s.insert(ctx, []*Query{q}); err != nil {
			last = err
			slog.Error("ЗАПИС ВТРАЧЕНО: рядок не приймається базою",
				"err", err, "session", q.SessionID, "seq", q.Seq,
				"duration_ms", q.DurationMS, "sql", q.SQLText)
			continue
		}
		ok++
	}
	if ok == 0 {
		return 0, last
	}
	slog.Warn("пачку розібрано порядково", "прийнято", ok, "втрачено", len(batch)-ok)
	return ok, nil
}

func (s *Shipper) insert(ctx context.Context, batch []*Query) error {
	rows := make([][]any, 0, len(batch))
	for _, q := range batch {
		rows = append(rows, []any{
			q.SessionID, q.StudentID, q.Seq, q.StartedAt, q.DurationMS,
			q.SQLText, q.SQLTruncated, q.Fingerprint, q.Kind, q.Protocol,
			nullStr(q.Params),
			q.Status, nullStr(q.SQLState), nullStr(q.ErrorMessage), nullInt(q.ErrorPos),
			q.RowsAffected, q.IsCatalog, q.IsService,
		})
	}
	_, err := s.pool.CopyFrom(ctx,
		pgx.Identifier{"obs", "queries"},
		[]string{"session_id", "student_id", "seq", "started_at", "duration_ms",
			"sql_text", "sql_truncated", "sql_fingerprint", "sql_kind", "protocol",
			"params",
			"status", "sqlstate", "error_message", "error_position",
			"rows_affected", "is_catalog", "is_service"},
		pgx.CopyFromRows(rows))
	return err
}

func nullStr(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func nullInt(i int) any {
	if i == 0 {
		return nil
	}
	return i
}
