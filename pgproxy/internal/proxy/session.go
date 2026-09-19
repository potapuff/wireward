package proxy

// Стан сесії та розбір обох протоколів.
//
// Простий протокол (`psql`) тривіальний: одне повідомлення Query з текстом.
// DBeaver, DataGrip і взагалі все на pgjdbc/psycopg працюють РОЗШИРЕНИМ:
// текст запиту (Parse), значення параметрів (Bind) і виконання (Execute)
// приходять окремими повідомленнями, а виконань може бути кілька «в польоті»
// до одного Sync. Тому тут не «поточний запит», а черга.
//
// Три випадки, через які наївна реалізація втрачає саме те, заради чого все
// будувалося:
//
//  1. Синтаксична помилка в розширеному протоколі падає на Parse — Execute не
//     буде взагалі, і черга порожня. Запис доводиться складати з останнього
//     Parse (див. lastParsed).
//  2. Після ErrorResponse бекенд ПРОПУСКАЄ решту пакета до Sync. Ці оператори
//     студент надіслав, але вони не виконалися — журналюємо їх окремо,
//     зі SQLSTATE 25P02, а не втрачаємо.
//  3. Клієнти читають результат порціями: Execute з обмеженням рядків дає
//     PortalSuspended, і наступний Execute продовжує той самий портал. Це одна
//     робота студента, а не N запитів, тож запис накопичується до
//     CommandComplete.

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"math"
	"net"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgproto3"

	"wireward/pgproxy/internal/auth"
	"wireward/pgproxy/internal/journal"
)

const maxParamLen = 512 // довші значення параметрів обрізаємо

type stmt struct {
	name        string
	sql         string
	fingerprint string
	kind        string
	isCatalog   bool
	isService   bool
	blocked     string // непорожнє => спроба змінити заборонений параметр
	paramOIDs   []uint32
}

type portal struct {
	name   string
	st     *stmt
	params []any
}

type pending struct {
	seq int
	st  *stmt
	// Межі оператора в тексті пакета простого протоколу. Позицію помилки
	// бекенд рахує по всьому пакету, а в журналі лежить один оператор.
	spanStart int
	spanEnd   int
	params    []any
	portal    string
	protocol  string
	startedAt time.Time
	// dataRows рахуємо самі, бо при порційному читанні тег CommandComplete
	// показує лише ОСТАННЮ порцію, а не весь результат.
	dataRows int64
	sawData  bool
	tagRows  int64
	haveTag  bool
	// suspended => портал справді призупинено (PortalSuspended). Лише тоді
	// наступний Execute на цей портал є продовженням.
	suspended bool
	timer     *time.Timer
}

// rowCount: якщо були DataRow — рахунок наш (він переживає порційне читання);
// інакше беремо число з тега, як для INSERT/UPDATE/DELETE.
func (p *pending) rowCount() (int64, bool) {
	if p.sawData {
		return p.dataRows, true
	}
	if p.haveTag {
		return p.tagRows, true
	}
	return 0, false
}

type session struct {
	id        string
	student   *auth.Student
	srv       *Server
	startedAt time.Time
	hj        *pgconn.HijackedConn

	writeMu sync.Mutex // клієнту пишуть обидві горутини
	be      *pgproto3.Backend

	mu sync.Mutex

	seq int

	// Простий протокол. Один Query може містити кілька операторів через «;»,
	// і кожен із них — окрема робота студента: свій текст, свій результат,
	// своя тривалість. Тому simpleCur — це ПОТОЧНИЙ оператор пакета, а
	// simpleRest — ті, до яких бекенд ще не дійшов. Оператор закривається на
	// своєму CommandComplete, а не на ReadyForQuery.
	simpleCur  *pending
	simpleRest []*pending
	simpleSQL  string

	// Розширений протокол.
	stmts      map[string]*stmt
	portals    map[string]*portal
	queue      []*pending
	lastParsed *stmt
	lastBound  *portal

	queries, errorsN, catalogN int
	endReason                  string

	// Мʼяка квота. Оновлюється не частіше ніж раз на quotaTTL, щоб не робити
	// запит до бази на кожен оператор студента.
	quota        auth.Quota
	quotaChecked time.Time
}

const quotaTTL = 30 * time.Second

func (s *Server) newSession(ctx context.Context, st *auth.Student, conn net.Conn,
	startup map[string]string, listenPort int, tlsVersion string,
	hj *pgconn.HijackedConn) *session {

	sess := &session{
		id: uuid.NewString(), student: st, srv: s, startedAt: time.Now(), hj: hj,
		stmts: map[string]*stmt{}, portals: map[string]*portal{},
	}

	host, _, _ := net.SplitHostPort(conn.RemoteAddr().String())
	ci := ParseClient(startup)
	params, _ := json.Marshal(ci.Params)
	_, err := s.pool.Exec(ctx, `
		INSERT INTO obs.sessions (id, student_id, client_addr, application_name,
		                          client_name, client_version, client_purpose,
		                          client_guessed, startup_params,
		                          listen_port, tls_version, protocol_version, started_at)
		VALUES ($1::uuid, $2::uuid, $3::inet, $4, $5, $6, $7, $8, $9::jsonb,
		        $10, $11, $12, $13)`,
		sess.id, st.ID, host, nullIfEmpty(ci.Raw),
		nullIfEmpty(ci.Name), nullIfEmpty(ci.Version), nullIfEmpty(ci.Purpose),
		ci.Guessed, string(params),
		listenPort, tlsVersion, hj.ParameterStatuses["server_version"], sess.startedAt)
	if err != nil {
		slog.Warn("сесія не записана", "err", err)
	}
	return sess
}

func (sess *session) finish(ctx context.Context) {
	// Портали, які клієнт не дочитав, усе одно мають лишити слід.
	sess.mu.Lock()
	var orphans []*journal.Query
	for _, p := range append(sess.queue, append([]*pending{sess.simpleCur}, sess.simpleRest...)...) {
		if p == nil {
			continue
		}
		orphans = append(orphans, sess.record(p, "", "", 0))
	}
	sess.queue, sess.simpleCur, sess.simpleRest = nil, nil, nil
	q, e, c, reason := sess.queries, sess.errorsN, sess.catalogN, sess.endReason
	sess.mu.Unlock()

	for _, r := range orphans {
		sess.emit(r)
	}
	if reason == "" {
		reason = "client_disconnect"
	}
	if _, err := sess.srv.pool.Exec(ctx, `
		UPDATE obs.sessions
		   SET ended_at = now(), end_reason = $2, queries_total = $3,
		       errors_total = $4, catalog_queries = $5
		 WHERE id = $1::uuid`, sess.id, reason, q, e, c); err != nil {
		slog.Warn("сесію не закрито", "err", err)
	}
}

// ------------------------------------------------------------ клієнт -> бекенд

// fromClient повертає false, якщо повідомлення пересилати НЕ треба
// (оператор відхилено політикою).
func (sess *session) fromClient(msg pgproto3.FrontendMessage) bool {
	switch m := msg.(type) {

	case *pgproto3.Query:
		return sess.onSimpleQuery(m.String)

	case *pgproto3.Parse:
		return sess.onParse(m)

	case *pgproto3.Bind:
		return sess.onBind(m)

	case *pgproto3.Execute:
		return sess.onExecute(m)

	case *pgproto3.Close:
		sess.onClose(m)

	case *pgproto3.Terminate:
		sess.setEndReason("terminate")
	}
	return true
}

func (sess *session) onSimpleQuery(sql string) bool {
	if name, blocked := BlockedParam(sql, sess.srv.cfg.BlockedParams); blocked {
		sess.rejectSimple(sql, name)
		return false
	}

	// Пакет із кількох операторів бекенд виконує як одну транзакцію: або
	// пропускаємо його цілком, або не пропускаємо жоден оператор. Тому квота
	// перевіряється по всіх одразу, а відмова оформлюється на весь текст —
	// інакше студент побачив би, що частина пакета виконалася, а частина ні.
	spans := StatementSpans(sql)
	if len(spans) == 0 {
		// Порожній Query (лише коментарі чи пробіли): бекенд відповість
		// EmptyQueryResponse, і запис має бути один — на те, що надіслано.
		spans = [][2]int{{0, len(sql)}}
	}
	sts := make([]*stmt, 0, len(spans))
	for _, sp := range spans {
		st := newStmt("", sql[sp[0]:sp[1]], sess.srv.cfg.BlockedParams)
		if sess.overQuota(st) {
			sess.rejectQuota(newStmt("", sql, nil), "simple", "")
			return false
		}
		sts = append(sts, st)
	}

	sess.mu.Lock()
	sess.simpleRest, sess.simpleSQL = nil, sql
	for i, st := range sts {
		sess.seq++
		p := &pending{seq: sess.seq, st: st, protocol: "simple",
			spanStart: spans[i][0], spanEnd: spans[i][1]}
		sess.simpleRest = append(sess.simpleRest, p)
	}
	sess.startNextSimpleLocked()
	sess.mu.Unlock()
	return true
}

// startNextSimpleLocked робить наступний оператор пакета поточним і лише тоді
// запускає його таймер: відлік має починатися, коли бекенд справді взявся за
// цей оператор, а не коли надійшов увесь пакет.
func (sess *session) startNextSimpleLocked() {
	if len(sess.simpleRest) == 0 {
		sess.simpleCur = nil
		return
	}
	p := sess.simpleRest[0]
	sess.simpleRest = sess.simpleRest[1:]
	sess.armPending(p)
	sess.simpleCur = p
}

func (sess *session) onParse(m *pgproto3.Parse) bool {
	st := newStmt(m.Name, m.Query, sess.srv.cfg.BlockedParams)
	st.paramOIDs = m.ParameterOIDs

	sess.mu.Lock()
	sess.stmts[m.Name] = st
	sess.lastParsed = st
	if st.blocked == "" {
		sess.mu.Unlock()
		return true
	}
	// Заборонений оператор гаситься ЦІЛКОМ: до бекенда не йде ні Parse, ні
	// Bind, ні Execute. Інакше він однаково впаде вже на бекенді, і запис
	// зʼявиться двічі — наша відмова плюс помилка СУБД.
	// Sync пропускаємо як є: бекенд відповість на нього ReadyForQuery, і
	// клієнт побачить нормальну послідовність.
	sess.seq++
	rec := sess.rejectRecord(st, nil, "", "extended", st.blocked)
	sess.lastParsed = nil
	sess.mu.Unlock()

	sess.emit(rec)
	sess.sendRejection(rec.SQLState, rec.ErrorMessage, false)
	return false
}

func (sess *session) onBind(m *pgproto3.Bind) bool {
	sess.mu.Lock()
	defer sess.mu.Unlock()
	st := sess.stmts[m.PreparedStatement]
	if st != nil && st.blocked != "" {
		// Parse цього оператора ми вже погасили — Bind теж не пересилаємо.
		sess.portals[m.DestinationPortal] = &portal{name: m.DestinationPortal, st: st}
		return false
	}
	if st == nil {
		// Клієнт посилається на оператор, якого ми не бачили (перепідключення
		// чи наш недогляд). Не вигадуємо текст — лишаємо порожній stmt.
		st = &stmt{name: m.PreparedStatement, sql: "", kind: "other"}
	}
	p := &portal{name: m.DestinationPortal, st: st, params: decodeParams(m, st.paramOIDs)}
	sess.portals[m.DestinationPortal] = p
	sess.lastBound = p
	return true
}

func (sess *session) onExecute(m *pgproto3.Execute) bool {
	sess.mu.Lock()
	p := sess.portals[m.Portal]
	if p == nil {
		sess.mu.Unlock()
		return true
	}
	// Продовження вже початого порталу: клієнт читає результат порціями.
	// Це та сама робота студента, тож новий запис не заводимо.
	//
	// Перевіряти саме suspended, а не лише збіг імені: pgjdbc виконує пакет
	// (addBatch) через ОДИН безіменний портал, перевʼязуючи його щоразу.
	// Порівняння тільки за іменем ховало б усі оператори пакета, крім першого.
	for _, pend := range sess.queue {
		if pend.portal == m.Portal && pend.suspended {
			pend.suspended = false
			sess.mu.Unlock()
			return true
		}
	}
	if p.st.blocked != "" {
		// Відмову вже надіслано на Parse; тут просто мовчки не пересилаємо.
		sess.mu.Unlock()
		return false
	}
	if sess.overQuotaLocked(p.st) {
		sess.seq++
		rec := sess.quotaRecord(p.st, p.params, m.Portal, "extended")
		sess.mu.Unlock()
		sess.emit(rec)
		sess.sendRejection(rec.SQLState, rec.ErrorMessage, false)
		return false
	}
	sess.seq++
	pend := sess.startPending(p.st, p.params, m.Portal, "extended")
	sess.queue = append(sess.queue, pend)
	sess.mu.Unlock()
	return true
}

func (sess *session) onClose(m *pgproto3.Close) {
	sess.mu.Lock()
	var flush *journal.Query
	switch m.ObjectType {
	case 'S':
		delete(sess.stmts, m.Name)
	case 'P':
		// Портал закривають, не дочитавши — фіксуємо те, що встигли.
		for i, pend := range sess.queue {
			if pend.portal == m.Name {
				flush = sess.record(pend, "", "", 0)
				sess.queue = append(sess.queue[:i], sess.queue[i+1:]...)
				break
			}
		}
		delete(sess.portals, m.Name)
	}
	sess.mu.Unlock()
	if flush != nil {
		sess.emit(flush)
	}
}

// ------------------------------------------------------------ бекенд -> клієнт

func (sess *session) fromBackend(msg pgproto3.BackendMessage) {
	switch m := msg.(type) {

	case *pgproto3.ParseComplete:
		sess.mu.Lock()
		sess.lastParsed = nil
		sess.mu.Unlock()

	case *pgproto3.BindComplete:
		sess.mu.Lock()
		sess.lastBound = nil
		sess.mu.Unlock()

	case *pgproto3.DataRow:
		sess.mu.Lock()
		if p := sess.head(); p != nil {
			p.dataRows++
			p.sawData = true
		}
		sess.mu.Unlock()

	case *pgproto3.CommandComplete:
		sess.onComplete(string(m.CommandTag))

	case *pgproto3.EmptyQueryResponse:
		sess.onComplete("")

	case *pgproto3.PortalSuspended:
		// Порція прочитана, портал живий: запис лишаємо відкритим і чекаємо
		// наступного Execute на цей самий портал.
		sess.mu.Lock()
		if p := sess.head(); p != nil {
			p.suspended = true
		}
		sess.mu.Unlock()

	case *pgproto3.ErrorResponse:
		sess.onError(m)

	case *pgproto3.ReadyForQuery:
		sess.onReady()
	}
}

// head повертає поточний запис. Викликається під замком.
func (sess *session) head() *pending {
	if sess.simpleCur != nil {
		return sess.simpleCur
	}
	if len(sess.queue) > 0 {
		return sess.queue[0]
	}
	return nil
}

func (sess *session) onComplete(tag string) {
	sess.mu.Lock()
	if p := sess.simpleCur; p != nil {
		// Кожен оператор пакета має власний CommandComplete — саме на ньому
		// запис і закривається. Раніше весь пакет ставав одним записом, тобто
		// з «SELECT 1; DROP TABLE t;» у журналі лишався один рядок із текстом
		// обох операторів і результатом останнього.
		if n, ok := rowsFromTag(tag); ok {
			p.tagRows, p.haveTag = n, true
		}
		rec := sess.record(p, "", "", 0)
		sess.startNextSimpleLocked()
		sess.mu.Unlock()
		sess.emit(rec)
		return
	}
	if len(sess.queue) == 0 {
		sess.mu.Unlock()
		return
	}
	p := sess.queue[0]
	sess.queue = sess.queue[1:]
	if n, ok := rowsFromTag(tag); ok {
		p.tagRows, p.haveTag = n, true
	}
	rec := sess.record(p, "", "", 0)
	sess.mu.Unlock()
	sess.emit(rec)
}

func (sess *session) onError(m *pgproto3.ErrorResponse) {
	sess.mu.Lock()

	if sess.simpleCur != nil {
		// Помилка обриває ВЕСЬ пакет: бекенд відкочує транзакцію і йде до
		// ReadyForQuery, не виконавши решти. Оператор, на якому впало,
		// записуємо з його SQLSTATE, решту — з 25P02, як і в розширеному
		// протоколі. Мовчки втрачати їх не можна: студент їх надіслав.
		//
		// Котрий саме оператор винен — каже позиція помилки, а не порядок.
		// Простий протокол розбирає ВЕСЬ текст пакета перед виконанням, тож
		// синтаксична помилка в третьому операторі приходить тоді, коли
		// поточним ще є перший. Без цієї прив'язки 42601 діставався б не тому
		// операторові, а студент бачив би в журналі помилку в тексті, де її
		// немає.
		rest := append([]*pending{sess.simpleCur}, sess.simpleRest...)
		blame := 0
		if b := charToByte(sess.simpleSQL, int(m.Position)); b >= 0 {
			for i, p := range rest {
				if b >= p.spanStart && b <= p.spanEnd {
					blame = i
					break
				}
			}
		}
		var out []*journal.Query
		for i, p := range rest {
			if i != blame {
				out = append(out, sess.record(p, "25P02",
					"пропущено: у тому самому пакеті раніше сталася помилка", 0))
				continue
			}
			out = append(out, sess.record(p, m.Code, m.Message,
				rebasePos(sess.simpleSQL, p.spanStart, int(m.Position))))
		}
		sess.simpleCur, sess.simpleRest, sess.simpleSQL = nil, nil, ""
		sess.mu.Unlock()
		for _, r := range out {
			sess.emit(r)
		}
		return
	}

	var out []*journal.Query

	if len(sess.queue) > 0 {
		p := sess.queue[0]
		sess.queue = sess.queue[1:]
		out = append(out, sess.record(p, m.Code, m.Message, int(m.Position)))
	} else {
		// Помилка ДО будь-якого Execute: оператор не пройшов Parse або Bind.
		// Саме так виглядає опечатка, надіслана DBeaver чи DataGrip, — і саме
		// цей випадок губиться, якщо покладатися тільки на чергу виконань.
		var st *stmt
		var params []any
		if sess.lastBound != nil {
			st, params = sess.lastBound.st, sess.lastBound.params
		} else if sess.lastParsed != nil {
			st = sess.lastParsed
		}
		if st != nil && st.sql != "" {
			sess.seq++
			p := sess.startPending(st, params, "", "extended")
			out = append(out, sess.record(p, m.Code, m.Message, int(m.Position)))
		}
		sess.lastParsed, sess.lastBound = nil, nil
	}

	// Решту пакета бекенд пропустить до Sync. Ці оператори студент надіслав,
	// але вони не виконалися — фіксуємо це явно, а не мовчки втрачаємо.
	for _, p := range sess.queue {
		out = append(out, sess.record(p, "25P02",
			"пропущено: у тому самому пакеті раніше сталася помилка", 0))
	}
	sess.queue = nil
	sess.mu.Unlock()

	for _, r := range out {
		sess.emit(r)
	}
}

func (sess *session) onReady() {
	sess.mu.Lock()
	sess.lastParsed, sess.lastBound = nil, nil
	if sess.simpleCur == nil && len(sess.simpleRest) == 0 {
		sess.mu.Unlock()
		return
	}
	// Сюди доходить лише те, на що бекенд не відповів ні CommandComplete, ні
	// ErrorResponse. Такого бути не повинно, але втратити запис через власне
	// нерозуміння відповіді — гірше, ніж записати його без результату.
	var out []*journal.Query
	for _, p := range append([]*pending{sess.simpleCur}, sess.simpleRest...) {
		if p == nil {
			continue
		}
		out = append(out, sess.record(p, "", "", 0))
	}
	sess.simpleCur, sess.simpleRest, sess.simpleSQL = nil, nil, ""
	sess.mu.Unlock()
	for _, r := range out {
		sess.emit(r)
	}
}

// charToByte переводить позицію помилки (бекенд рахує СИМВОЛИ від одиниці) у
// зміщення в байтах. -1, якщо позиції немає або вона поза текстом.
func charToByte(sql string, pos int) int {
	if pos <= 0 {
		return -1
	}
	n := 0
	for i := range sql {
		if n == pos-1 {
			return i
		}
		n++
	}
	if n == pos-1 {
		return len(sql)
	}
	return -1
}

// rebasePos переводить позицію помилки з координат усього пакета в координати
// одного оператора: у журналі лежить саме оператор, і каретка має вказувати
// на місце в ньому.
func rebasePos(sql string, spanStart, pos int) int {
	b := charToByte(sql, pos)
	if b < spanStart {
		return 0
	}
	return utf8.RuneCountInString(sql[spanStart:b]) + 1
}

// ------------------------------------------------------------ записи

// startPending викликається під замком.
func (sess *session) startPending(st *stmt, params []any, portalName, protocol string) *pending {
	p := &pending{seq: sess.seq, st: st, params: params, portal: portalName,
		protocol: protocol}
	sess.armPending(p)
	return p
}

// armPending запускає відлік для оператора. Викликається під замком.
func (sess *session) armPending(p *pending) {
	p.startedAt = time.Now()
	// Власний таймер, незалежний від statement_timeout у сесії: студент може
	// зняти серверний параметр одним SET, наш — ні (ADR-010).
	if ms := sess.srv.cfg.StatementTimeoutMS; ms > 0 && sess.hj != nil {
		pid, key := sess.hj.PID, sess.hj.SecretKey
		p.timer = time.AfterFunc(time.Duration(ms)*time.Millisecond, func() {
			slog.Info("таймаут запиту, скасовуємо", "session", sess.id)
			sess.srv.cancelBackend(pid, key)
		})
	}
}

// record викликається під замком.
func (sess *session) record(p *pending, sqlstate, errMsg string, errPos int) *journal.Query {
	if p.timer != nil {
		p.timer.Stop()
	}
	// Оператор, до якого бекенд так і не дійшов (решта пакета після помилки,
	// незакритий портал при відключенні), відліку не мав. Без цієї перевірки
	// duration_ms рахувався б від нульового часу — число, яке не вміщується в
	// колонку, і shipper зупиняється на ньому НАЗАВЖДИ, бо повторює ту саму
	// пачку. Один такий запис зупиняв увесь журнал.
	if p.startedAt.IsZero() {
		p.startedAt = time.Now()
	}
	text, truncated := Truncate(p.st.sql)
	rec := &journal.Query{
		SessionID:    sess.id,
		StudentID:    sess.student.ID,
		Seq:          p.seq,
		StartedAt:    p.startedAt,
		DurationMS:   float64(time.Since(p.startedAt).Microseconds()) / 1000.0,
		SQLText:      text,
		SQLTruncated: truncated,
		Fingerprint:  p.st.fingerprint,
		Kind:         p.st.kind,
		Protocol:     p.protocol,
		Status:       "ok",
		IsCatalog:    p.st.isCatalog,
		IsService:    p.st.isService,
	}
	if n, ok := p.rowCount(); ok {
		rec.RowsAffected = &n
	}
	if len(p.params) > 0 {
		if b, err := json.Marshal(p.params); err == nil {
			rec.Params = string(b)
		}
	}
	if sqlstate != "" {
		rec.Status = "error"
		rec.SQLState, rec.ErrorMessage, rec.ErrorPos = sqlstate, errMsg, errPos
	}
	// Лічильник сесії рахує службові запити окремо від роботи студента —
	// саме він дає «сидів у GUI» проти «не працював». errorsN туди ж: інакше
	// permission denied на запиті, який клієнт шле сам (не з волі студента,
	// і студент не міг би його уникнути), роздував би "помилок за сесію".
	if rec.IsCatalog || rec.IsService {
		sess.catalogN++
	} else {
		sess.queries++
		if sqlstate != "" {
			sess.errorsN++
		}
	}
	return rec
}

// noteDroppedDatabase фіксує момент, коли студент видалив СВОЮ базу з клієнта.
// Пишемо тільки успішні DROP і тільки на власну базу студента: чужу він і так
// не видалить, а невдала спроба вже є в журналі запитів як помилка.
func (sess *session) noteDroppedDatabase(rec *journal.Query) {
	if rec.Status != "ok" || rec.Kind != "ddl" {
		return
	}
	name, ok := DroppedDatabase(rec.SQLText)
	if !ok || name != strings.ToLower(sess.student.DBName) {
		return
	}
	_, err := sess.srv.pool.Exec(context.Background(), `
		INSERT INTO obs.db_events (student_id, event, actor, details)
		VALUES ($1::uuid, 'dropped', 'student',
		        jsonb_build_object('via', 'sql', 'session_id', $2::text, 'database', $3::text))`,
		sess.student.ID, sess.id, name)
	if err != nil {
		slog.Warn("подію dropped не записано", "err", err)
		return
	}
	slog.Info("студент видалив свою базу з клієнта", "db", name, "student", sess.student.ID)
}

func (sess *session) emit(rec *journal.Query) {
	// Службові запити клієнта ЗБЕРІГАЮТЬСЯ і ховаються при показі, а не
	// викидаються (ADR-018 скасовує цю частину ADR-008). Причина в ціні
	// помилки: `select * from pg_class` у курсі баз даних — нормальна
	// навчальна вправа, і будь-яке розширення евристики означало б, що
	// справжній запит студента зникає назавжди й непомітно.
	if err := sess.srv.jw.Write(rec); err != nil {
		slog.Error("запис журналу не вдався", "err", err)
	}
	sess.noteDroppedDatabase(rec)
}

// ------------------------------------------------------------ відмови

func (sess *session) rejectSimple(sql, param string) {
	st := newStmt("", sql, nil)
	sess.mu.Lock()
	sess.seq++
	rec := sess.rejectRecord(st, nil, "", "simple", param)
	sess.mu.Unlock()
	sess.emit(rec)
	sess.sendRejection(rec.SQLState, rec.ErrorMessage, true)
}

// rejectRecord викликається під замком.
func (sess *session) rejectRecord(st *stmt, params []any, portalName, protocol, param string) *journal.Query {
	p := &pending{seq: sess.seq, st: st, params: params, portal: portalName,
		protocol: protocol, startedAt: time.Now()}
	return sess.record(p, "42501", fmt.Sprintf(
		"Wireward не дозволяє змінювати параметр %q: він впливає на памʼять сервера, спільну для всіх студентів",
		param), 0)
}

// sendRejection пише клієнту помилку замість того, щоб пропустити оператор.
// withReady=false для розширеного протоколу: там ReadyForQuery надішле бекенд
// у відповідь на Sync, і другий такий пакет збив би клієнта з ліку.
func (sess *session) sendRejection(code, msg string, withReady bool) {
	sess.writeMu.Lock()
	defer sess.writeMu.Unlock()
	sess.be.Send(&pgproto3.ErrorResponse{
		Severity: "ERROR", Code: code, Message: msg,
		Hint: "Це обмеження Wireward, а не бази. Значення за замовчуванням достатні для лабораторних.",
	})
	if withReady {
		sess.be.Send(&pgproto3.ReadyForQuery{TxStatus: 'I'})
	}
	_ = sess.be.Flush()
}

// ------------------------------------------------------------ мʼяка квота

// overQuota перевіряє, чи оператор збільшує дані при вичерпаній квоті.
func (sess *session) overQuota(st *stmt) bool {
	sess.mu.Lock()
	defer sess.mu.Unlock()
	return sess.overQuotaLocked(st)
}

// overQuotaLocked викликається під замком.
func (sess *session) overQuotaLocked(st *stmt) bool {
	if !GrowsData(st.kind, st.sql) {
		// SELECT, DELETE, DROP і TRUNCATE лишаємо завжди: інакше студент
		// опиниться в пастці, де не можна навіть прибрати зайве.
		return false
	}
	if time.Since(sess.quotaChecked) > quotaTTL {
		q, err := auth.LoadQuota(context.Background(), sess.srv.pool, sess.student.ID)
		if err != nil {
			// Свідомо не блокуємо роботу через власну несправність: краще
			// пропустити запис, ніж зупинити всіх студентів через зламаний
			// запит. Але мовчати про це не можна — саме так квота одного разу
			// вже перестала діяти непомітно (не було гранту на obs.ts_slots).
			slog.Error("КВОТА НЕ ПЕРЕВІРЯЄТЬСЯ: не вдалося прочитати ліміт",
				"student", sess.student.ID, "err", err)
		} else {
			sess.quota = q
		}
		sess.quotaChecked = time.Now()
	}
	return sess.quota.Exceeded()
}

// quotaRecord викликається під замком.
func (sess *session) quotaRecord(st *stmt, params []any, portalName, protocol string) *journal.Query {
	p := &pending{seq: sess.seq, st: st, params: params, portal: portalName,
		protocol: protocol, startedAt: time.Now()}
	// 53100 — disk_full: саме те, що студент побачив би від СУБД, якби
	// спрацювала квота файлової системи. Повідомлення інше лише тим, що
	// приходить ЗАЗДАЛЕГІДЬ і пояснює, що робити.
	return sess.record(p, "53100", fmt.Sprintf(
		"Місце вичерпано: %.1f МБ з %.1f МБ. Видаліть зайві дані (DELETE, DROP TABLE) "+
			"або перестворіть базу в кабінеті Wireward. Читання і видалення працюють як завжди.",
		float64(sess.quota.Used)/1048576, float64(sess.quota.Limit)/1048576), 0)
}

func (sess *session) rejectQuota(st *stmt, protocol, portalName string) {
	sess.mu.Lock()
	sess.seq++
	rec := sess.quotaRecord(st, nil, portalName, protocol)
	sess.mu.Unlock()
	sess.emit(rec)
	sess.sendRejection(rec.SQLState, rec.ErrorMessage, protocol == "simple")
}

func (sess *session) setEndReason(r string) {
	sess.mu.Lock()
	sess.endReason = r
	sess.mu.Unlock()
}

// ------------------------------------------------------------ дрібниці

func newStmt(name, sql string, blocked map[string]bool) *stmt {
	st := &stmt{
		name:        name,
		sql:         sql,
		fingerprint: Fingerprint(sql),
		kind:        Kind(sql),
		isCatalog:   IsCatalog(sql),
		isService:   IsService(sql),
	}
	if blocked != nil {
		if p, ok := BlockedParam(sql, blocked); ok {
			st.blocked = p
		}
	}
	return st
}

// Типи, які pgjdbc (DBeaver, DataGrip) і psycopg надсилають двійково найчастіше.
const (
	oidBool        = 16
	oidInt8        = 20
	oidInt2        = 21
	oidInt4        = 23
	oidText        = 25
	oidF4          = 700
	oidF8          = 701
	oidVar         = 1043
	oidDate        = 1082
	oidTimestamp   = 1114
	oidTimestamptz = 1184
	oidNumeric     = 1700
	oidUUID        = 2950

	oidArrBool        = 1000
	oidArrInt2        = 1005
	oidArrInt4        = 1007
	oidArrText        = 1009
	oidArrVar         = 1015
	oidArrInt8        = 1016
	oidArrF4          = 1021
	oidArrF8          = 1022
	oidArrDate        = 1182
	oidArrTimestamptz = 1185
	oidArrTimestamp   = 1115
	oidArrNumeric     = 1231
	oidArrUUID        = 2951
)

// arrayElemOID — тип елемента для тих масивних OID, які проксі вміє
// розшифрувати. Тільки одновимірні масиви цих типів: цього досить для
// pgjdbc/psycopg, а N-вимірні від студентів курсу БД не трапляються.
var arrayElemOID = map[uint32]uint32{
	oidArrBool:        oidBool,
	oidArrInt2:        oidInt2,
	oidArrInt4:        oidInt4,
	oidArrInt8:        oidInt8,
	oidArrF4:          oidF4,
	oidArrF8:          oidF8,
	oidArrText:        oidText,
	oidArrVar:         oidVar,
	oidArrDate:        oidDate,
	oidArrTimestamptz: oidTimestamptz,
	oidArrTimestamp:   oidTimestamp,
	oidArrNumeric:     oidNumeric,
	oidArrUUID:        oidUUID,
}

// pgEpoch — від неї рахуються і date (дні), і timestamp/timestamptz (мкс).
var pgEpoch = time.Date(2000, 1, 1, 0, 0, 0, 0, time.UTC)

// decodeBinary перетворює двійкове значення на читабельне, якщо тип відомий.
// Без цього журнал показував би `\x0064` замість `100` — формально чесно, але
// для розбору зі студентом марно.
func decodeBinary(oid uint32, raw []byte) (any, bool) {
	be := func(n int) uint64 {
		var v uint64
		for _, b := range raw[:n] {
			v = v<<8 | uint64(b)
		}
		return v
	}
	switch oid {
	case oidBool:
		if len(raw) == 1 {
			return raw[0] != 0, true
		}
	case oidInt2:
		if len(raw) == 2 {
			return int64(int16(be(2))), true
		}
	case oidInt4:
		if len(raw) == 4 {
			return int64(int32(be(4))), true
		}
	case oidInt8:
		if len(raw) == 8 {
			return int64(be(8)), true
		}
	case oidF4:
		if len(raw) == 4 {
			return math.Float32frombits(uint32(be(4))), true
		}
	case oidF8:
		if len(raw) == 8 {
			return math.Float64frombits(be(8)), true
		}
	case oidText, oidVar:
		return string(raw), true
	case oidUUID:
		if len(raw) == 16 {
			h := hex.EncodeToString(raw)
			return h[0:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:], true
		}
	case oidDate:
		if len(raw) == 4 {
			return pgEpoch.AddDate(0, 0, int(int32(be(4)))).Format("2006-01-02"), true
		}
	case oidTimestamp, oidTimestamptz:
		if len(raw) == 8 {
			// Проксі не знає часового поясу сесії (це параметр СУБД, не
			// протоколу) — показуємо в UTC. Для журналу розбору помилки це
			// достатньо: абсолютний момент часу, а не локальна стрічка.
			micros := int64(be(8))
			return pgEpoch.Add(time.Duration(micros)*time.Microsecond).
				Format("2006-01-02 15:04:05.999999") + "+00", true
		}
	case oidNumeric:
		return decodeNumeric(raw)
	default:
		if elemOID, ok := arrayElemOID[oid]; ok {
			return decodeArray(elemOID, raw)
		}
	}
	return nil, false
}

// decodeArray розбирає бінарний формат array_send: ndim/flags/elemtype,
// потім по dim size+lower bound на вимір, потім елементи як
// length(int32)+байти (length=-1 — NULL). Елемент розшифровує та сама
// decodeBinary за OID, який прийшов від Parse, тобто нових спецвипадків не
// заводить. Багатовимірні масиви не розкладаються по вимірах — плоский
// список елементів, бо для журналу важливий вміст, а не форма.
func decodeArray(elemOID uint32, raw []byte) (any, bool) {
	if len(raw) < 12 {
		return nil, false
	}
	be32 := func(i int) uint32 {
		return uint32(raw[i])<<24 | uint32(raw[i+1])<<16 | uint32(raw[i+2])<<8 | uint32(raw[i+3])
	}
	ndim := int(be32(0))
	if ndim == 0 {
		return "{}", true
	}
	if ndim < 0 || ndim > 6 {
		return nil, false
	}
	pos := 12
	total := 1
	for d := 0; d < ndim; d++ {
		if pos+8 > len(raw) {
			return nil, false
		}
		n := int(int32(be32(pos)))
		if n < 0 {
			return nil, false
		}
		total *= n
		pos += 8
	}
	if total > 100000 {
		return nil, false // захист від сміттєвого/ворожого OID+довжини
	}
	elems := make([]string, 0, total)
	for i := 0; i < total; i++ {
		if pos+4 > len(raw) {
			return nil, false
		}
		l := int(int32(be32(pos)))
		pos += 4
		if l < 0 {
			elems = append(elems, "NULL")
			continue
		}
		if l < 0 || pos+l > len(raw) {
			return nil, false
		}
		if v, ok := decodeBinary(elemOID, raw[pos:pos+l]); ok {
			elems = append(elems, fmt.Sprintf("%v", v))
		} else {
			elems = append(elems, "\\x"+hex.EncodeToString(raw[pos:pos+l]))
		}
		pos += l
	}
	return "{" + strings.Join(elems, ",") + "}", true
}

// decodeNumeric розбирає бінарний формат numeric: ndigits/weight/sign/dscale
// і масив «цифр» у базі 10000 (int16 numeric_send, backend/utils/adt/numeric.c).
// Перевірено проти numeric_send() на живому кластері: 123.456, -42, 0.001,
// 100, 1.5, 0, NaN.
func decodeNumeric(raw []byte) (any, bool) {
	if len(raw) < 8 {
		return nil, false
	}
	be16 := func(i int) uint16 { return uint16(raw[i])<<8 | uint16(raw[i+1]) }
	ndigits := int(be16(0))
	weight := int(int16(be16(2)))
	sign := be16(4)
	dscale := int(be16(6))
	if len(raw) != 8+ndigits*2 {
		return nil, false
	}
	switch sign {
	case 0xC000:
		return "NaN", true
	case 0xD000:
		return "Infinity", true
	case 0xF000:
		return "-Infinity", true
	case 0x0000, 0x4000:
	default:
		return nil, false
	}
	digits := make([]int, ndigits)
	for i := 0; i < ndigits; i++ {
		digits[i] = int(be16(8 + i*2))
	}
	// digitAt(exp) — цифра на позиції 10000^exp, 0 поза відомим діапазоном:
	// weight — позиція digits[0], решта йдуть послідовно нижче.
	digitAt := func(exp int) int {
		d := weight - exp
		if d < 0 || d >= ndigits {
			return 0
		}
		return digits[d]
	}
	var sb strings.Builder
	if sign == 0x4000 {
		sb.WriteByte('-')
	}
	if weight >= 0 {
		for exp := weight; exp >= 0; exp-- {
			if exp == weight {
				sb.WriteString(strconv.Itoa(digitAt(exp)))
			} else {
				fmt.Fprintf(&sb, "%04d", digitAt(exp))
			}
		}
	} else {
		sb.WriteByte('0')
	}
	if dscale > 0 {
		sb.WriteByte('.')
		var frac strings.Builder
		for g := 1; g <= (dscale+3)/4; g++ {
			fmt.Fprintf(&frac, "%04d", digitAt(-g))
		}
		s := frac.String()
		if len(s) < dscale {
			s += strings.Repeat("0", dscale-len(s))
		}
		sb.WriteString(s[:dscale])
	}
	return sb.String(), true
}

// decodeParams дістає значення параметрів із Bind.
//
// Текстовий формат кладемо як є. Двійковий розшифровуємо за OID із Parse для
// поширених типів; для решти лишаємо шістнадцятковий запис із позначкою —
// краще позначений байтовий рядок, ніж мовчазна діра в журналі.
func decodeParams(m *pgproto3.Bind, oids []uint32) []any {
	if len(m.Parameters) == 0 {
		return nil
	}
	binaryAt := func(i int) bool {
		switch len(m.ParameterFormatCodes) {
		case 0:
			return false
		case 1:
			return m.ParameterFormatCodes[0] == 1
		default:
			return i < len(m.ParameterFormatCodes) && m.ParameterFormatCodes[i] == 1
		}
	}
	out := make([]any, 0, len(m.Parameters))
	for i, raw := range m.Parameters {
		switch {
		case raw == nil:
			out = append(out, nil)
		case binaryAt(i):
			var oid uint32
			if i < len(oids) {
				oid = oids[i]
			}
			if v, ok := decodeBinary(oid, raw); ok {
				out = append(out, v)
			} else if len(raw) <= 64 {
				out = append(out, "\\x"+hex.EncodeToString(raw))
			} else {
				out = append(out, fmt.Sprintf("<двійкові дані, %d Б>", len(raw)))
			}
		default:
			s := string(raw)
			if len(s) > maxParamLen {
				s = s[:maxParamLen] + "…"
			}
			out = append(out, s)
		}
	}
	return out
}

func rowsFromTag(tag string) (int64, bool) {
	f := strings.Fields(strings.TrimSpace(tag))
	if len(f) < 2 {
		return 0, false
	}
	var n int64
	if _, err := fmt.Sscanf(f[len(f)-1], "%d", &n); err != nil {
		return 0, false
	}
	return n, true
}
