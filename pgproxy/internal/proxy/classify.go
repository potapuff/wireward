package proxy

import (
	"regexp"
	"strings"
)

const maxSQLText = 64 << 10 // 64 КБ; довше обрізаємо з прапорцем

var (
	reString  = regexp.MustCompile(`'(?:[^']|'')*'`)
	reNumber  = regexp.MustCompile(`\b\d+(\.\d+)?\b`)
	reSpace   = regexp.MustCompile(`\s+`)
	reComment = regexp.MustCompile(`(?s)/\*.*?\*/|--[^\n]*`)
	// reCatalogFunc — виклики метаданих без FROM: current_schemas(), has_*_privilege().
	reCatalogFunc = regexp.MustCompile(`(?i)\b(current_schemas?\(|has_[a-z_]+_privilege\(|pg_get_[a-z_]+\()`)
	// reCatalogIdent перевіряє ОДНЕ джерело (з FROM/JOIN): схема pg_catalog./
	// information_schema. або відоме імʼя системного каталогу без схеми.
	// Раніше тут був перелік конкретних каталогів, і його не вистачало:
	// pg_user, pg_extension і pg_depend у нього не входили, тож службові
	// запити DataGrip осідали в журналі як робота студента (З2 у 07-roadmap).
	// Тепер правило просте — будь-яке джерело з префіксом pg_ або зі схеми
	// каталогу. Так можна саме тому, що службові запити більше НЕ
	// викидаються, а лише ховаються: хибне спрацювання коштує схованого
	// рядка, а не втраченого (ADR-018).
	reCatalogIdent = regexp.MustCompile(`(?i)^(?:pg_catalog|information_schema)\.|^pg_[a-z0-9_]+\b`)
	// Службові функції, якими клієнт опитує сервер без жодного FROM.
	reServiceFunc = regexp.MustCompile(`(?i)\b(version\(|current_setting\(|pg_postmaster_start_time\(|pg_backend_pid\(|pg_client_encoding\(|current_schema\(|current_database\(|pg_is_in_recovery\()`)
	// `declare` тут — це DECLARE ... CURSOR, яким DataGrip і DBeaver гортають
	// результат посторінково: одне натискання «наступна сторінка» дає рядок у
	// журналі, і в покритті лабораторних це чистий клієнтський шум (З-питання
	// в 07-roadmap). Ризик був у тому, що курсори — ще й тема курсу; викладач
	// підтвердив 06.09.2026, що в жодній лабораторній їх немає, тож ціна
	// хибного спрацювання — схований рядок, а не втрачений (ADR-018).
	// FETCH/CLOSE навмисно НЕ додані: без DECLARE вони не з'являються, а
	// `FETCH` — ще й слово з форм, які студент пише сам.
	reServiceWord = regexp.MustCompile(`(?is)^\s*(show|set|reset|discard|deallocate|declare)\b`)
	reClauseStart  = regexp.MustCompile(`(?i)\bfrom\b|\bjoin\b`)
	reClauseEnd    = regexp.MustCompile(`(?i)\bwhere\b|\bon\b|\bgroup\s+by\b|\border\s+by\b|\blimit\b|\boffset\b|\bhaving\b|\bunion\b|\bintersect\b|\bexcept\b|\bwindow\b|\breturning\b|\bfor\s+(?:update|share|no\s+key\s+update|key\s+share)\b|\bjoin\b`)
	reSourceIdent  = regexp.MustCompile(`^"(?:[^"]|"")+"|^[a-zA-Z_][a-zA-Z0-9_$]*(?:\.[a-zA-Z_][a-zA-Z0-9_$]*)?`)
)

// Fingerprint — нормалізований відбиток запиту: літерали на ?, регістр і
// пробіли зведені. Це не оптимізація, а фіча: дає групування «студент N разів
// поспіль виконав по суті той самий запит, і всі N упали» — сигнал «застряг».
func Fingerprint(sql string) string {
	s := reComment.ReplaceAllString(sql, " ")
	s = reString.ReplaceAllString(s, "?")
	s = reNumber.ReplaceAllString(s, "?")
	s = reSpace.ReplaceAllString(s, " ")
	return strings.ToLower(strings.TrimSpace(s))
}

// Kind визначається за першим значущим словом.
func Kind(sql string) string {
	s := strings.TrimSpace(reComment.ReplaceAllString(sql, " "))
	s = strings.TrimLeft(s, "( \t\r\n")
	f := strings.ToLower(firstWord(s))
	switch f {
	case "select", "table", "values", "with":
		return "select"
	case "insert":
		return "insert"
	case "update":
		return "update"
	case "delete":
		return "delete"
	case "copy":
		return "copy"
	case "create", "alter", "drop", "truncate", "comment", "grant", "revoke", "reindex":
		return "ddl"
	case "begin", "commit", "rollback", "savepoint", "start", "end", "release":
		return "tcl"
	default:
		return "other"
	}
}

// IsCatalog — «звернення до системного каталогу без інших джерел». Не точний
// аналіз: розібрати всі джерела запиту без парсера SQL неможливо. Head хибних
// спрацювань: emit() (session.go) не зберігає запис із IsCatalog=true поштучно
// (ADR-008), тож хибнопозитивний виліт тут коштує роботи студента, зниклої з
// журналу — набагато дорожче за хибнонегативний (зайвий рядок). Тому перевірка
// зіставляє джерела в FROM/JOIN, а не шукає підрядок будь-де в тексті: інакше
// той самий рядок ловився б і в 'у pg_class зберігаються...', і в JOIN
// власної таблиці з pg_class.
func IsCatalog(sql string) bool {
	s := reString.ReplaceAllString(reComment.ReplaceAllString(sql, " "), "?")
	if !reClauseStart.MatchString(s) {
		// Немає FROM/JOIN — це виклик функції метаданих сам по собі
		// (current_schemas(), has_table_privilege()) або звичайний вираз.
		return reCatalogFunc.MatchString(s)
	}
	sources, ok := catalogSources(s)
	return ok && len(sources) > 0
}

// IsService — «це не запит студента, а розмова клієнта із сервером».
//
// Окрема категорія від IsCatalog: SHOW TRANSACTION ISOLATION LEVEL,
// SET extra_float_digits, SELECT version() до каталогу не звертаються взагалі,
// а DataGrip шле перше з них ПЕРЕД КОЖНИМ запитом студента — тобто половина
// рядків на екрані це воно (виміряно 03.09.2026, З2 у 07-roadmap).
//
// Порожній текст теж службовий: драйвер шле порожній Query, показувати нема
// чого.
//
// Помилковий SET сюди НЕ потрапляє з екрана: ховаються лише успішні службові
// запити (див. front). Заборонений work_mem має лишатися видимим — це сигнал,
// а не шум.
func IsService(sql string) bool {
	s := strings.TrimSpace(reString.ReplaceAllString(reComment.ReplaceAllString(sql, " "), "?"))
	if s == "" {
		return true
	}
	if reServiceWord.MatchString(s) {
		return true
	}
	// SELECT version(), SELECT current_setting(...) — без жодного джерела.
	return !reClauseStart.MatchString(s) && reServiceFunc.MatchString(s)
}

// catalogSources повертає джерела з усіх FROM/JOIN, якщо ВСІ вони належать
// системному каталогу; ok=false, щойно трапилось джерело поза каталогом —
// функція, що повертає набір рядків, чи власна таблиця студента.
//
// Підзапит замість джерела (у т.ч. LATERAL) РОЗБИРАЄМО РЕКУРСИВНО, а не
// відкидаємо як невідоме джерело: драйвери (pgjdbc, і DataGrip з ним)
// регулярно тягнуть метадані запитом виду
// `FROM pg_catalog.pg_type t WHERE ... (SELECT 1 FROM pg_catalog.pg_class ...)`
// — і, якщо все, аж до найглибшого підзапиту, читає лише каталог, це так
// само службовий шум, як і плаский запит без підзапитів. Раніше такий запит
// лишався видимим — саме клас проблеми зі знахідок сеансу DataGrip 08.09
// (З17 у 07-roadmap): викладач підтвердив, що лабораторні НЕ звертаються до
// pg_catalog/information_schema напряму, тож ризик, який раніше стримував
// ширшу евристику (студентська вправа на кшталт `select * from pg_class`),
// тут не застосовний — хибне спрацювання як і раніше лише ховає рядок
// (ADR-018), а не губить його.
func catalogSources(s string) (sources []string, ok bool) {
	for _, m := range reClauseStart.FindAllStringIndex(s, -1) {
		seg := s[m[1]:]
		if end := reClauseEnd.FindStringIndex(seg); end != nil {
			seg = seg[:end[0]]
		}
		for _, part := range splitTopLevelComma(seg) {
			part = strings.TrimSpace(part)
			if part == "" {
				continue
			}
			inner := part
			if low := strings.ToLower(part); strings.HasPrefix(low, "lateral") {
				inner = strings.TrimSpace(part[len("lateral"):])
			}
			if strings.HasPrefix(inner, "(") {
				sub, subOK := subquerySources(inner)
				if !subOK {
					return nil, false
				}
				sources = append(sources, sub...)
				continue
			}
			id := reSourceIdent.FindString(part)
			if id == "" || !reCatalogIdent.MatchString(id) {
				return nil, false
			}
			sources = append(sources, id)
		}
	}
	return sources, true
}

// subquerySources розбирає джерело виду "(SELECT ...) аліас" — знаходить
// відповідну закривальну дужку й рекурсивно перевіряє вміст як звичайний
// SQL-текст через catalogSources.
func subquerySources(s string) ([]string, bool) {
	depth := 0
	for i, r := range s {
		switch r {
		case '(':
			depth++
		case ')':
			depth--
			if depth == 0 {
				return catalogSources(s[1:i])
			}
		}
	}
	return nil, false
}

// splitTopLevelComma ділить FROM-список на джерела комами, що не заховані
// у дужках підзапиту чи виклику функції.
func splitTopLevelComma(s string) []string {
	var out []string
	depth, start := 0, 0
	for i, r := range s {
		switch r {
		case '(':
			depth++
		case ')':
			if depth > 0 {
				depth--
			}
		case ',':
			if depth == 0 {
				out = append(out, s[start:i])
				start = i + 1
			}
		}
	}
	out = append(out, s[start:])
	return out
}

func firstWord(s string) string {
	for i, r := range s {
		if r == ' ' || r == '\t' || r == '\n' || r == '\r' || r == '(' || r == ';' {
			return s[:i]
		}
	}
	return s
}

// Truncate обрізає надто довгий текст запиту, повертаючи прапорець.
func Truncate(sql string) (string, bool) {
	if len(sql) <= maxSQLText {
		return sql, false
	}
	return sql[:maxSQLText], true
}

// statements розбиває текст на окремі оператори, знімаючи коментарі.
//
// Потрібне тому, що простий протокол дозволяє надіслати кілька операторів
// одним Query, а коментар перед оператором — звичайна річ і в DBeaver, і в
// зошиті студента. Перевірки, прибиті до початку тексту, обходилися і тим,
// і тим. Рядкові літерали, ідентифікатори в лапках і $$-літерали проходять
// наскрізь, щоб крапка з комою всередині них не рвала оператор.
func statements(sql string) []string {
	out, _ := scanStatements(sql)
	return out
}

// StatementSpans повертає межі операторів в оригінальному тексті. Потрібні,
// щоб зіставити позицію помилки, яку бекенд рахує по ВСЬОМУ пакету, з
// конкретним оператором.
func StatementSpans(sql string) [][2]int {
	_, spans := scanStatements(sql)
	return spans
}

// SplitStatements повертає оператори В ОРИГІНАЛЬНОМУ вигляді — з коментарями і
// власним форматуванням студента. Саме це йде в журнал: коментар студента над
// запитом — частина того, що він написав, і викидати його не можна.
// Для перевірок політики натомість береться statements(): там коментарі лише
// заважають.
func SplitStatements(sql string) []string {
	_, spans := scanStatements(sql)
	out := make([]string, 0, len(spans))
	for _, sp := range spans {
		out = append(out, sql[sp[0]:sp[1]])
	}
	return out
}

// scanStatements проходить текст один раз і повертає дві узгоджені між собою
// проекції: оператори без коментарів і межі тих самих операторів в оригіналі.
func scanStatements(sql string) ([]string, [][2]int) {
	var out []string
	var spans [][2]int
	var cur []byte
	segStart := 0
	flush := func(end int) {
		if s := strings.TrimSpace(string(cur)); s != "" {
			out = append(out, s)
			a, b := segStart, end
			for a < b && isSpaceByte(sql[a]) {
				a++
			}
			for b > a && isSpaceByte(sql[b-1]) {
				b--
			}
			spans = append(spans, [2]int{a, b})
		}
		cur = cur[:0]
		segStart = end + 1
	}
	for i := 0; i < len(sql); {
		switch c := sql[i]; {
		case c == '-' && i+1 < len(sql) && sql[i+1] == '-':
			for i < len(sql) && sql[i] != '\n' {
				i++
			}
			cur = append(cur, ' ')
		case c == '/' && i+1 < len(sql) && sql[i+1] == '*':
			// Блокові коментарі в PostgreSQL вкладаються.
			for depth, j := 1, i+2; ; {
				if j >= len(sql) {
					i = len(sql)
					break
				}
				if sql[j] == '/' && j+1 < len(sql) && sql[j+1] == '*' {
					depth, j = depth+1, j+2
					continue
				}
				if sql[j] == '*' && j+1 < len(sql) && sql[j+1] == '/' {
					depth, j = depth-1, j+2
					if depth == 0 {
						i = j
						break
					}
					continue
				}
				j++
			}
			cur = append(cur, ' ')
		case c == '\'':
			// E'...' сприймає \' як екранування, звичайний літерал — ні.
			esc := i > 0 && (sql[i-1] == 'e' || sql[i-1] == 'E') &&
				(i == 1 || !identByte(sql[i-2]))
			cur, i = append(cur, c), i+1
			for i < len(sql) {
				if esc && sql[i] == '\\' && i+1 < len(sql) {
					cur, i = append(cur, sql[i], sql[i+1]), i+2
					continue
				}
				if sql[i] == '\'' {
					if i+1 < len(sql) && sql[i+1] == '\'' {
						cur, i = append(cur, '\'', '\''), i+2
						continue
					}
					cur, i = append(cur, '\''), i+1
					break
				}
				cur, i = append(cur, sql[i]), i+1
			}
		case c == '"':
			cur, i = append(cur, c), i+1
			for i < len(sql) {
				if sql[i] == '"' {
					if i+1 < len(sql) && sql[i+1] == '"' {
						cur, i = append(cur, '"', '"'), i+2
						continue
					}
					cur, i = append(cur, '"'), i+1
					break
				}
				cur, i = append(cur, sql[i]), i+1
			}
		case c == '$':
			tag, ok := dollarTag(sql, i)
			if !ok {
				cur, i = append(cur, c), i+1
				continue
			}
			end := strings.Index(sql[i+len(tag):], tag)
			if end < 0 {
				cur, i = append(cur, sql[i:]...), len(sql)
				continue
			}
			stop := i + len(tag) + end + len(tag)
			cur, i = append(cur, sql[i:stop]...), stop
		case c == ';':
			flush(i)
			i++
		default:
			cur, i = append(cur, c), i+1
		}
	}
	flush(len(sql))
	return out, spans
}

func isSpaceByte(c byte) bool {
	return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\v' || c == '\f'
}

func identByte(c byte) bool {
	return c == '_' || c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9'
}

// dollarTag повертає відкривальний тег $$-літерала ("$$", "$tag$"), якщо він
// починається на позиції i. Позиційний параметр $1 тегом не є.
func dollarTag(sql string, i int) (string, bool) {
	for j := i + 1; j < len(sql); j++ {
		if sql[j] == '$' {
			return sql[i : j+1], true
		}
		if !identByte(sql[j]) || sql[j] >= '0' && sql[j] <= '9' && j == i+1 {
			return "", false
		}
	}
	return "", false
}

var reSetParam = regexp.MustCompile(`(?is)^\s*set\s+(?:session\s+|local\s+)?([a-z_][a-z0-9_]*)\s*(?:=|\bto\b)`)
var reResetParam = regexp.MustCompile(`(?is)^\s*reset\s+([a-z_][a-z0-9_]*)`)
var reSetConfig = regexp.MustCompile(`(?is)\bset_config\s*\(\s*'([^']+)'`)

// BlockedParam повертає імʼя параметра, який студент намагається змінити,
// якщо цей параметр під забороною (ADR-010).
//
// Перевіряти доводиться саме тут, а не в СУБД: statement_timeout, work_mem та
// idle_in_transaction_session_timeout мають контекст USERSET, тобто
// ALTER ROLE ... SET задає лише ТИПОВЕ значення, і студент знімає його одним
// SET. На сервері з 4 ГБ це прямий шлях до вичерпання памʼяті всім одразу.
func BlockedParam(sql string, blocked map[string]bool) (string, bool) {
	check := func(name string) (string, bool) {
		n := strings.ToLower(name)
		return n, blocked[n]
	}
	for _, st := range statements(sql) {
		if m := reSetParam.FindStringSubmatch(st); m != nil {
			if n, ok := check(m[1]); ok {
				return n, true
			}
		}
		if m := reResetParam.FindStringSubmatch(st); m != nil {
			if n, ok := check(m[1]); ok {
				return n, true
			}
		}
		if m := reSetConfig.FindStringSubmatch(st); m != nil {
			if n, ok := check(m[1]); ok {
				return n, true
			}
		}
	}
	return "", false
}

// Імʼя бази: або в подвійних лапках (тоді регістр значущий, а "" всередині
// означає одну лапку), або без них (тоді PostgreSQL зводить до нижнього регістру).
var reDropDB = regexp.MustCompile(`(?is)^\s*drop\s+database\s+(?:if\s+exists\s+)?(?:"((?:[^"]|"")*)"|([a-z_][a-z0-9_$]*))`)

// DroppedDatabase повертає імʼя бази, яку оператор видаляє.
//
// Потрібне, щоб виконати вимогу «створено/видалено треба логувати» повністю.
// Кнопка «Перестворити базу» в кабінеті пише подію сама, а от DROP DATABASE з
// psql застосунок не бачить: він помічає лише відсутність бази при наступному
// вході. Проксі ж бачить сам оператор і може записати подію вчасно.
func DroppedDatabase(sql string) (string, bool) {
	for _, st := range statements(sql) {
		m := reDropDB.FindStringSubmatch(st)
		if m == nil {
			continue
		}
		if m[1] != "" {
			return strings.ReplaceAll(m[1], `""`, `"`), true
		}
		return strings.ToLower(m[2]), true
	}
	return "", false
}

// GrowsData каже, чи оператор здатний ЗБІЛЬШИТИ обсяг даних.
//
// Потрібне для мʼякої квоти. Ключове рішення: при вичерпаній квоті
// блокуються лише такі оператори, а SELECT, DELETE, DROP і TRUNCATE
// лишаються. Інакше студент опинився б у пастці, де не можна навіть
// прибрати зайве — і єдиним виходом було б перестворення бази.
// reWritesInSelect ловить запис даних усередині оператора, що ПОЧИНАЄТЬСЯ як
// читання: CTE, який модифікує дані (`WITH x AS (INSERT ...) SELECT ...`), і
// `SELECT ... INTO нова_таблиця`. Обидві форми Kind() бачить як select.
var reWritesInSelect = regexp.MustCompile(`(?i)\b(insert|update|into)\b`)

// FOR UPDATE / FOR NO KEY UPDATE — блокування рядків, а не запис; прибираємо
// перед перевіркою, інакше звичайний SELECT упреться у вичерпану квоту.
var reLockClause = regexp.MustCompile(`(?i)\bfor\s+(?:no\s+key\s+)?update\b|\bfor\s+(?:key\s+)?share\b`)

func GrowsData(kind, sql string) bool {
	switch kind {
	case "insert", "update", "copy":
		return true
	case "ddl":
		// CREATE та ALTER місце займають, DROP і TRUNCATE — звільняють.
		f := strings.ToLower(firstWord(strings.TrimLeft(
			reComment.ReplaceAllString(sql, " "), "( \t\r\n")))
		return f == "create" || f == "alter" || f == "reindex"
	case "select":
		// Літерали прибираємо, щоб SELECT 'insert' не рахувався записом.
		for _, st := range statements(sql) {
			st = reString.ReplaceAllString(st, "?")
			if reWritesInSelect.MatchString(reLockClause.ReplaceAllString(st, " ")) {
				return true
			}
		}
		return false
	default:
		return false
	}
}
