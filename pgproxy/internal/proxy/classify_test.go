package proxy

// Тести класифікації операторів.
//
// Це серце журналу: від Kind і GrowsData залежить мʼяка квота, від IsCatalog —
// чи не роздується журнал службовими запитами DBeaver, від DroppedDatabase —
// чи побачить адмінка подію видалення бази. Тому тут перевіряються саме
// РЕАЛЬНІ форми запису, які надсилають psql, DBeaver і DataGrip: коментарі
// перед оператором, провідні пробіли й переводи рядка, довільний регістр,
// лапки навколо імені бази, «;» у кінці й кілька операторів у рядку.

import (
	"strings"
	"testing"
)

// blockedDefault — той самий набір, що й PROXY_BLOCKED_PARAMS за замовчуванням
// (див. internal/config/config.go).
func blockedDefault() map[string]bool {
	return map[string]bool{
		"work_mem":                            true,
		"statement_timeout":                   true,
		"idle_in_transaction_session_timeout": true,
		"temp_file_limit":                     true,
	}
}

func TestFingerprint(t *testing.T) {
	tests := []struct {
		name string
		sql  string
		want string
	}{
		{"числовий літерал стає ?", "SELECT * FROM t WHERE id = 42", "select * from t where id = ?"},
		{"рядковий літерал стає ?", "SELECT * FROM t WHERE name = 'Іван'", "select * from t where name = ?"},
		{"подвоєна лапка всередині рядка", "SELECT 'it''s ok'", "select ?"},
		{"дробове число теж літерал", "SELECT 3.14", "select ?"},
		{"регістр зводиться до нижнього", "SeLeCt X fRoM T", "select x from t"},
		{"пробіли й переводи рядка стискаються", "SELECT\n\t*\n  FROM   t", "select * from t"},
		{"провідні пробіли обрізаються", "   \n SELECT 1  \n ", "select ?"},
		{"рядковий коментар видаляється", "SELECT 1 -- скільки завгодно тексту\n", "select ?"},
		{"блоковий коментар видаляється", "/* підказка\n у два рядки */ SELECT 1", "select ?"},
		{"цифра всередині ідентифікатора лишається", "SELECT col1 FROM t2", "select col1 from t2"},
		{"порожній рядок", "", ""},
		{"два різні написання дають однаковий відбиток", "select * from t where id=1", "select * from t where id=?"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := Fingerprint(tc.sql); got != tc.want {
				t.Errorf("Fingerprint(%q) = %q, очікували %q", tc.sql, got, tc.want)
			}
		})
	}
}

// Головна властивість відбитка: N спроб «того самого» запиту з різними
// літералами мають злитися в одну групу — це і є сигнал «студент застряг».
func TestFingerprintGroupsRetries(t *testing.T) {
	attempts := []string{
		"SELECT * FROM orders WHERE id = 1",
		"select * from orders where id = 2",
		"SELECT  *  FROM  orders  WHERE  id  =  37",
		"SELECT * FROM orders WHERE id = 999 -- ще раз\n",
	}
	first := Fingerprint(attempts[0])
	for _, sql := range attempts[1:] {
		if got := Fingerprint(sql); got != first {
			t.Errorf("Fingerprint(%q) = %q, а мав збігтися з %q", sql, got, first)
		}
	}
}

func TestKind(t *testing.T) {
	tests := []struct {
		name string
		sql  string
		want string
	}{
		{"простий select", "SELECT 1", "select"},
		{"провідні пробіли й перевід рядка", "  \n\t SELECT 1", "select"},
		{"рядковий коментар перед оператором", "-- що це таке\nSELECT 1", "select"},
		{"блоковий коментар перед оператором", "/* коментар */ SELECT 1", "select"},
		{"дужка перед select", "(SELECT 1) UNION (SELECT 2)", "select"},
		{"with вважаємо select", "WITH x AS (SELECT 1) SELECT * FROM x", "select"},
		{"table це select", "TABLE orders", "select"},
		{"values це select", "VALUES (1),(2)", "select"},
		{"крапка з комою одразу за словом", "select;", "select"},
		{"insert", "insert into t values (1)", "insert"},
		{"update", "UPDATE t SET x = 1", "update"},
		{"delete", "DELETE FROM t", "delete"},
		{"copy", "COPY t FROM STDIN", "copy"},
		{"create це ddl", "CREATE TABLE t (id int)", "ddl"},
		{"alter це ddl", "ALTER TABLE t ADD COLUMN y int", "ddl"},
		{"drop це ddl", "DROP TABLE t", "ddl"},
		{"truncate це ddl", "TRUNCATE t", "ddl"},
		{"comment це ddl", "COMMENT ON TABLE t IS 'опис'", "ddl"},
		{"grant це ddl", "GRANT SELECT ON t TO other", "ddl"},
		{"revoke це ddl", "REVOKE SELECT ON t FROM other", "ddl"},
		{"reindex це ddl", "REINDEX INDEX i", "ddl"},
		{"begin це tcl", "BEGIN", "tcl"},
		{"commit це tcl", "commit", "tcl"},
		{"rollback це tcl", "ROLLBACK", "tcl"},
		{"savepoint це tcl", "SAVEPOINT s1", "tcl"},
		{"start transaction це tcl", "START TRANSACTION", "tcl"},
		{"end це tcl", "END", "tcl"},
		{"release це tcl", "RELEASE SAVEPOINT s1", "tcl"},
		{"explain нам невідомий", "EXPLAIN SELECT 1", "other"},
		{"set нам невідомий", "SET search_path = public", "other"},
		{"vacuum нам невідомий", "VACUUM ANALYZE", "other"},
		{"порожній рядок", "", "other"},
		{"самі пробіли", "   \n  ", "other"},
		{"тільки коментар", "-- нічого не роблю", "other"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := Kind(tc.sql); got != tc.want {
				t.Errorf("Kind(%q) = %q, очікували %q", tc.sql, got, tc.want)
			}
		})
	}
}

// Відома межа евристики: перше слово відрізається лише по пробілах, дужці та
// «;». Запис без пробілу після ключового слова (psql його приймає) класифікації
// не піддається. На практиці ані psql, ані pgjdbc так не форматують, тому це
// зафіксовано як поточна поведінка, а не як помилка.
func TestKindВідомаМежаБезПробілу(t *testing.T) {
	if got := Kind("SELECT*FROM t"); got != "other" {
		t.Errorf("Kind(\"SELECT*FROM t\") = %q, очікували поточну поведінку %q", got, "other")
	}
}

func TestIsCatalog(t *testing.T) {
	tests := []struct {
		name string
		sql  string
		want bool
	}{
		{"явна схема pg_catalog", "SELECT * FROM pg_catalog.pg_class", true},
		{"information_schema", "SELECT * FROM information_schema.tables", true},
		{"pg_class без схеми", "select relname from pg_class", true},
		{"pg_namespace", "SELECT * FROM pg_namespace", true},
		{"pg_attribute", "SELECT * FROM pg_attribute WHERE attrelid = 1", true},
		{"pg_tables", "SELECT * FROM pg_tables", true},
		{"pg_indexes", "SELECT * FROM pg_indexes", true},
		{"pg_stat_activity через pg_stat_", "SELECT * FROM pg_stat_activity", true},
		{"pg_get_expr через pg_get_", "SELECT pg_get_expr(adbin, adrelid)", true},
		{"current_schemas", "SELECT current_schemas(true)", true},
		{"current_schema без s", "SELECT current_schema()", true},
		{"has_table_privilege", "SELECT has_table_privilege('t', 'SELECT')", true},
		{"регістр не має значення", "SELECT * FROM PG_CATALOG.PG_CLASS", true},
		{"звичайна таблиця студента", "SELECT * FROM orders WHERE id = 1", false},
		{"власна таблиця з префіксом pg", "SELECT * FROM pgrocks", false},
		{"ddl студента", "CREATE TABLE students (id int)", false},
		{"порожній рядок", "", false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := IsCatalog(tc.sql); got != tc.want {
				t.Errorf("IsCatalog(%q) = %v, очікували %v", tc.sql, got, tc.want)
			}
		})
	}
}

// Був баг: IsCatalog шукав підрядок будь-де в тексті, тож під відсічку
// потрапляв і запит студента до власних таблиць, у якому імʼя каталогу
// трапилося в рядковому літералі або в JOIN. Наслідок не косметичний: emit()
// (session.go) мовчки ВИКИДАЄ запис із IsCatalog=true — робота студента
// зникала з журналу. Тепер перевіряються фактичні джерела в FROM/JOIN.
func TestIsCatalogХибнопозитивніВтрачаютьЗапис(t *testing.T) {
	tests := []struct {
		name string
		sql  string
	}{
		{"імʼя каталогу всередині рядкового літерала",
			"SELECT * FROM notes WHERE body = 'у pg_class зберігаються таблиці'"},
		{"власна таблиця приєднана до каталогу",
			"SELECT o.id, c.relname FROM orders o JOIN pg_class c ON c.oid = o.rel"},
		{"каталог приєднаний до власної таблиці (інший порядок)",
			"SELECT o.id FROM pg_class c JOIN orders o ON o.rel = c.oid"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if IsCatalog(tc.sql) {
				t.Errorf("IsCatalog(%q) = true, але запит має власні джерела і мусить потрапити в журнал", tc.sql)
			}
		})
	}
}

// Підзапит і LATERAL — джерело, яке без парсера не перевірити; вважаємо його
// «іншим джерелом», щоб не занизити хибнопозитивно. VALUES(...) без FROM/JOIN
// узагалі не чіпляється до цієї евристики.
func TestIsCatalogНевизначеніДжерела(t *testing.T) {
	tests := []struct {
		name string
		sql  string
		want bool
	}{
		// З17: підзапит, що ЦІЛКОМ читає каталог, тепер розбирається
		// рекурсивно і теж вважається каталогом — не тільки плаский FROM.
		{"підзапит, що весь читає каталог", "SELECT * FROM (SELECT * FROM pg_class) x", true},
		{"підзапит, що читає власну таблицю студента", "SELECT * FROM (SELECT * FROM orders) x", false},
		{"кілька джерел через кому, одне з них — каталог", "SELECT * FROM orders, pg_class", false},
		{"кілька джерел через кому, обидва — каталог", "SELECT * FROM pg_class, pg_namespace", true},
		{"LATERAL підзапит, що весь читає каталог", "SELECT * FROM pg_class c, LATERAL (SELECT 1 FROM pg_namespace) x", true},
		{"LATERAL підзапит на власну таблицю", "SELECT * FROM pg_class c, LATERAL (SELECT 1 FROM orders) x", false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := IsCatalog(tc.sql); got != tc.want {
				t.Errorf("IsCatalog(%q) = %v, очікували %v", tc.sql, got, tc.want)
			}
		})
	}
}

func TestTruncate(t *testing.T) {
	tests := []struct {
		name     string
		sql      string
		wantLen  int
		wantFlag bool
	}{
		{"короткий не чіпаємо", "SELECT 1", len("SELECT 1"), false},
		{"порожній", "", 0, false},
		{"рівно межа", strings.Repeat("a", maxSQLText), maxSQLText, false},
		{"на байт довше", strings.Repeat("a", maxSQLText+1), maxSQLText, true},
		{"значно довше", strings.Repeat("b", maxSQLText*3), maxSQLText, true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, flag := Truncate(tc.sql)
			if len(got) != tc.wantLen || flag != tc.wantFlag {
				t.Errorf("Truncate(len=%d) = (len %d, %v), очікували (len %d, %v)",
					len(tc.sql), len(got), flag, tc.wantLen, tc.wantFlag)
			}
			if !strings.HasPrefix(tc.sql, got) {
				t.Errorf("обрізаний текст не є префіксом вихідного")
			}
		})
	}
}

func TestBlockedParam(t *testing.T) {
	tests := []struct {
		name     string
		sql      string
		wantName string
		wantHit  bool
	}{
		{"простий SET", "SET work_mem = '1GB'", "work_mem", true},
		{"SET без пробілів", "SET work_mem='1GB'", "work_mem", true},
		{"SET ... TO", "SET work_mem TO '1GB'", "work_mem", true},
		{"SET SESSION", "SET SESSION work_mem = '1GB'", "work_mem", true},
		{"SET LOCAL", "set local work_mem to '64MB'", "work_mem", true},
		{"RESET", "RESET statement_timeout", "statement_timeout", true},
		{"set_config", "SELECT set_config('work_mem', '1GB', false)", "work_mem", true},
		{"верхній регістр імені параметра", "SET WORK_MEM = '1GB'", "work_mem", true},
		{"провідні пробіли й перевід рядка", "\n   SET work_mem = '1GB'", "work_mem", true},
		{"TO DEFAULT теж заборонено", "SET statement_timeout TO DEFAULT", "statement_timeout", true},
		{"idle_in_transaction_session_timeout", "SET idle_in_transaction_session_timeout = 0",
			"idle_in_transaction_session_timeout", true},
		{"temp_file_limit", "set temp_file_limit to -1", "temp_file_limit", true},
		{"дозволений параметр", "SET search_path = public, myschema", "", false},
		{"дозволений RESET", "RESET search_path", "", false},
		{"параметр із схожим початком не плутаємо",
			"SET session_replication_role = 'replica'", "", false},
		{"звичайний select", "SELECT * FROM t", "", false},
		{"порожній рядок", "", "", false},
	}
	blocked := blockedDefault()
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			name, hit := BlockedParam(tc.sql, blocked)
			if name != tc.wantName || hit != tc.wantHit {
				t.Errorf("BlockedParam(%q) = (%q, %v), очікували (%q, %v)",
					tc.sql, name, hit, tc.wantName, tc.wantHit)
			}
		})
	}
}

// Порожній або nil набір нічого не забороняє — на цьому тримається
// rejectSimple(), який навмисно передає nil.
func TestBlockedParamПорожнійНабір(t *testing.T) {
	for _, blocked := range []map[string]bool{nil, {}} {
		if name, hit := BlockedParam("SET work_mem = '1GB'", blocked); hit {
			t.Errorf("з порожнім набором нічого не має блокуватися, отримали %q", name)
		}
	}
}

// Обхід заборони SET (був баг: обидва регексп-и прибиті до початку тексту й
// не знімали коментарі). docs/04-proxy-protocol.md вимагає перехоплювати «і
// SET, і SET SESSION, і RESET, і SET LOCAL, і виклик set_config()» — а
// перехоплювалося лише те, що стоїть найпершим у тексті. Наслідок за ADR-010 —
// work_mem на сервері з 4 ГБ. Закрито розбором тексту на окремі оператори.
func TestBlockedParamОбхідЗаборони(t *testing.T) {
	blocked := blockedDefault()
	tests := []struct {
		name     string
		sql      string
		wantName string
	}{
		{"коментар перед SET", "-- нічого такого\nSET work_mem = '1GB'", "work_mem"},
		{"блоковий коментар перед SET", "/* x */ SET work_mem = '1GB'", "work_mem"},
		{"другий оператор у простому протоколі", "BEGIN; SET work_mem = '1GB';", "work_mem"},
		{"SET за крапкою з комою всередині літерала", "SELECT ';'; SET work_mem = '1GB'", "work_mem"},
		{"SET після $$-літерала з крапкою з комою", "SELECT $$a;b$$; SET work_mem = '1GB'", "work_mem"},
		{"вкладений блоковий коментар", "/* /* лаб */ */ SET work_mem = '1GB'", "work_mem"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			name, hit := BlockedParam(tc.sql, blocked)
			if !hit {
				t.Errorf("BlockedParam(%q) пропустив зміну забороненого параметра (очікували %q)",
					tc.sql, tc.wantName)
			} else if name != tc.wantName {
				t.Errorf("BlockedParam(%q) = %q, очікували %q", tc.sql, name, tc.wantName)
			}
		})
	}
}

func TestDroppedDatabase(t *testing.T) {
	tests := []struct {
		name     string
		sql      string
		wantName string
		wantHit  bool
	}{
		{"простий DROP DATABASE", "DROP DATABASE db_test", "db_test", true},
		{"нижній регістр", "drop database db_test", "db_test", true},
		{"IF EXISTS", "DROP DATABASE IF EXISTS db_test", "db_test", true},
		{"IF EXISTS у нижньому регістрі", "drop database if exists db_test", "db_test", true},
		{"крапка з комою в кінці", "DROP DATABASE db_test;", "db_test", true},
		{"лапки навколо імені", `DROP DATABASE "db_test"`, "db_test", true},
		{"лапки разом з IF EXISTS", `DROP DATABASE IF EXISTS "db_test";`, "db_test", true},
		{"імʼя у верхньому регістрі зводимо донизу", "DROP DATABASE DB_TEST", "db_test", true},
		{"зайві пробіли й переводи рядка", "  \n DROP   DATABASE\n  db_test", "db_test", true},
		{"DROP TABLE це не база", "DROP TABLE db_test", "", false},
		{"DROP SCHEMA це не база", "DROP SCHEMA db_test", "", false},
		{"CREATE DATABASE це не видалення", "CREATE DATABASE db_test", "", false},
		{"звичайний select", "SELECT * FROM db_test", "", false},
		{"порожній рядок", "", "", false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			name, hit := DroppedDatabase(tc.sql)
			if name != tc.wantName || hit != tc.wantHit {
				t.Errorf("DroppedDatabase(%q) = (%q, %v), очікували (%q, %v)",
					tc.sql, name, hit, tc.wantName, tc.wantHit)
			}
		})
	}
}

// ПІДОЗРА НА БАГ (classify.go:117).
//
// Регексп прибитий до початку тексту, не знімає коментарі й читає лише
// [a-z0-9_], тож у трьох реальних формах подія «студент видалив свою базу»
// в obs.db_events не потрапляє — а саме заради неї функція й існує. Найгірший
// випадок — імʼя в лапках із дефісом: повертається ОБРІЗАНЕ імʼя ("db"),
// noteDroppedDatabase порівнює його з db_name студента, не збігається — і
// подія так само губиться, тільки тихо.
func TestDroppedDatabaseПропущеніФорми(t *testing.T) {
	tests := []struct {
		name     string
		sql      string
		wantName string
	}{
		{"коментар перед оператором", "-- прибираю за собою\nDROP DATABASE db_test", "db_test"},
		{"другий оператор у простому протоколі", "SELECT 1; DROP DATABASE db_test;", "db_test"},
		{"дефіс усередині імені в лапках", `DROP DATABASE "db-test"`, "db-test"},
		{"регістр у лапках зберігається", `DROP DATABASE "DbTest"`, "DbTest"},
		{"без лапок зводиться до нижнього регістру", "DROP DATABASE DbTest", "dbtest"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			name, hit := DroppedDatabase(tc.sql)
			if !hit {
				t.Errorf("DroppedDatabase(%q) не побачив видалення бази", tc.sql)
			} else if name != tc.wantName {
				t.Errorf("DroppedDatabase(%q) = %q, очікували %q", tc.sql, name, tc.wantName)
			}
		})
	}
}

func TestGrowsData(t *testing.T) {
	// kind навмисно рахуємо тим самим Kind(), що й у newStmt(): перевіряємо
	// пару так, як вона працює в бою.
	tests := []struct {
		name string
		sql  string
		want bool
	}{
		{"insert збільшує", "INSERT INTO t VALUES (1)", true},
		{"update збільшує", "UPDATE t SET x = repeat('a', 1000)", true},
		{"copy збільшує", "COPY t FROM STDIN", true},
		{"select не збільшує", "SELECT * FROM t", false},
		{"delete звільняє", "DELETE FROM t", false},
		{"tcl не збільшує", "BEGIN", false},
		{"other не збільшує", "EXPLAIN SELECT 1", false},
		{"create збільшує", "CREATE TABLE t (id int)", true},
		{"create index збільшує", "create index i on t(x)", true},
		{"alter збільшує", "ALTER TABLE t ADD COLUMN y text", true},
		{"reindex збільшує", "REINDEX INDEX i", true},
		{"drop звільняє", "DROP TABLE t", false},
		{"truncate звільняє", "TRUNCATE t", false},
		{"comment не збільшує", "COMMENT ON TABLE t IS 'опис'", false},
		{"grant не збільшує", "GRANT SELECT ON t TO other", false},
		{"коментар перед create не заважає", "/* лаб 3 */ CREATE TABLE t (id int)", true},
		{"перевід рядка перед create не заважає", "\n\n  create table t (id int)", true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			kind := Kind(tc.sql)
			if got := GrowsData(kind, tc.sql); got != tc.want {
				t.Errorf("GrowsData(%q, %q) = %v, очікували %v", kind, tc.sql, got, tc.want)
			}
		})
	}
}

// Обхід мʼякої квоти (був баг: kind визначається першим словом, тож запис
// даних усередині оператора, що починається як читання, квота пропускала).
func TestGrowsDataОбхідКвоти(t *testing.T) {
	tests := []struct {
		name string
		sql  string
		want bool
	}{
		{"CTE із записом", "WITH x AS (SELECT 1 AS a) INSERT INTO t SELECT a FROM x", true},
		{"CTE з INSERT усередині", "WITH w AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM w", true},
		{"CTE з UPDATE усередині", "WITH w AS (UPDATE t SET x = 1 RETURNING *) SELECT * FROM w", true},
		{"SELECT INTO", "SELECT * INTO new_t FROM old_t", true},
		{"запис у другому операторі", "SELECT 1; INSERT INTO t VALUES (1)", true},

		// Хибнопозитивні тут дорогі: при вичерпаній квоті вони заблокували б
		// читання, а мʼяка квота саме цього робити не повинна.
		{"слово insert у літералі не рахується", "SELECT * FROM t WHERE x = 'insert into'", false},
		{"update як частина імені не рахується", "SELECT * FROM update_log", false},
		{"SELECT FOR UPDATE не збільшує", "SELECT * FROM t FOR UPDATE", false},
		{"CTE лише з читанням не збільшує", "WITH x AS (SELECT 1) SELECT * FROM x", false},
		{"коментар зі словом insert не рахується", "-- тут був insert\nSELECT 1", false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := GrowsData(Kind(tc.sql), tc.sql); got != tc.want {
				t.Errorf("GrowsData(%q) = %v, очікували %v", tc.sql, got, tc.want)
			}
		})
	}
}

// SplitStatements годує журнал: кожен оператор пакета стає окремим записом,
// тому тут важливий не лише поділ, а й ЗБЕРЕЖЕННЯ оригінального тексту —
// коментар студента над запитом є частиною того, що він написав.
func TestSplitStatements(t *testing.T) {
	tests := []struct {
		name string
		sql  string
		want []string
	}{
		{"один оператор", "SELECT 1", []string{"SELECT 1"}},
		{"без крапки з комою в кінці", "SELECT 1; SELECT 2",
			[]string{"SELECT 1", "SELECT 2"}},
		{"порожній хвіст не рахується", "SELECT 1;;  ;", []string{"SELECT 1"}},
		{"коментар лишається в тексті",
			"-- рахуємо студентів\nSELECT count(*) FROM s;\nSELECT 2",
			[]string{"-- рахуємо студентів\nSELECT count(*) FROM s", "SELECT 2"}},
		{"коментар усередині оператора",
			"SELECT /* тут був join */ 1; SELECT 2",
			[]string{"SELECT /* тут був join */ 1", "SELECT 2"}},
		{"крапка з комою в літералі не ділить",
			"SELECT 'a;b'; SELECT 2", []string{"SELECT 'a;b'", "SELECT 2"}},
		{"крапка з комою в ідентифікаторі не ділить",
			`SELECT "a;b" FROM t; SELECT 2`, []string{`SELECT "a;b" FROM t`, "SELECT 2"}},
		{"тіло функції в $$ не ділиться",
			"CREATE FUNCTION f() RETURNS int AS $$ BEGIN RETURN 1; END $$ LANGUAGE plpgsql; SELECT f()",
			[]string{"CREATE FUNCTION f() RETURNS int AS $$ BEGIN RETURN 1; END $$ LANGUAGE plpgsql",
				"SELECT f()"}},
		{"форматування зберігається",
			"SELECT a,\n       b\n  FROM t;", []string{"SELECT a,\n       b\n  FROM t"}},
		{"лише коментар — оператора немає", "-- нічого\n", nil},
		{"порожній рядок", "", nil},
		{"самі пробіли", "   \n\t ", nil},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := SplitStatements(tc.sql)
			if len(got) != len(tc.want) {
				t.Fatalf("отримали %d операторів %#v, очікували %d %#v",
					len(got), got, len(tc.want), tc.want)
			}
			for i := range got {
				if got[i] != tc.want[i] {
					t.Errorf("оператор %d: %q, очікували %q", i+1, got[i], tc.want[i])
				}
			}
		})
	}
}

// Обидві проекції сканера мусять давати однакову КІЛЬКІСТЬ операторів:
// перевірки політики працюють із текстом без коментарів, журнал — з оригіналом,
// і розбіжність тут означала б, що заблокований оператор і записаний оператор —
// різні речі.
func TestSplitStatementsУзгодженаЗStatements(t *testing.T) {
	for _, sql := range []string{
		"SELECT 1; SELECT 2; SELECT 3",
		"-- к\nSET work_mem='1GB'; SELECT 1",
		"SELECT 'a;b'; /* c */ SELECT 2;",
		"BEGIN; INSERT INTO t VALUES (1); COMMIT;",
		"",
		"   ",
		"$$;$$",
	} {
		if a, b := len(statements(sql)), len(SplitStatements(sql)); a != b {
			t.Errorf("%q: statements=%d, SplitStatements=%d", sql, a, b)
		}
	}
}

// IsService — категорія «клієнт розмовляє із сервером». Виміряна на живому
// сеансі DataGrip 03.09.2026: SHOW TRANSACTION ISOLATION LEVEL шлеться перед
// КОЖНИМ запитом студента, тобто половина рядків на екрані — це воно.
func TestIsService(t *testing.T) {
	service := []string{
		"SHOW TRANSACTION ISOLATION LEVEL",
		"show DateStyle",
		"SET application_name = 'DataGrip'",
		"SET extra_float_digits = 3",
		"RESET ALL",
		"DISCARD ALL",
		"DEALLOCATE ALL",
		// Посторінкове гортання DataGrip/DBeaver; курсорів у лабораторних
		// немає (підтверджено викладачем 06.09.2026).
		`DECLARE "paged" CURSOR FOR select id, name from products order by id`,
		"select version()",
		"SELECT pg_postmaster_start_time()",
		"select current_setting('server_version_num')",
		"",
		"   ",
		"-- лише коментар",
	}
	for _, sql := range service {
		if !IsService(sql) {
			t.Errorf("IsService(%q) = false, а це службовий запит клієнта", sql)
		}
	}

	work := []string{
		"SELECT * FROM students",
		"SELECT count(*) FROM pg_class", // каталог, але це IsCatalog, не IsService
		"INSERT INTO t VALUES (1)",
		"SELECT version FROM releases",           // колонка, а не функція
		"SELECT * FROM settings WHERE show = 1",  // show як ідентифікатор усередині
		"BEGIN",
		"SELECT 1",
	}
	for _, sql := range work {
		if IsService(sql) {
			t.Errorf("IsService(%q) = true, а це робота студента", sql)
		}
	}
}

// Каталоги, яких не знав попередній перелік. Саме через них службові запити
// DataGrip осідали в журналі як робота студента (З2).
func TestIsCatalogРозширенийПерелік(t *testing.T) {
	for _, sql := range []string{
		"select usesuper from pg_user where usename = current_user",
		"SELECT extname FROM pg_extension",
		"select * from pg_depend d join pg_class c on c.oid = d.refobjid",
		"select * from pg_am",
		"SELECT * FROM information_schema.columns",
	} {
		if !IsCatalog(sql) {
			t.Errorf("IsCatalog(%q) = false, а це звернення до каталогу", sql)
		}
	}
	// Власна таблиця студента не стає каталогом від згадки в літералі чи від
	// приєднання до каталогу: втратити її з екрана дорожче, ніж показати зайве.
	for _, sql := range []string{
		"SELECT * FROM students",
		"select * from my_table t join pg_class c on c.relname = t.name",
		"select 'pg_class' as note",
	} {
		if IsCatalog(sql) {
			t.Errorf("IsCatalog(%q) = true, а це робота студента", sql)
		}
	}
}
