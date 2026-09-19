package proxy

// Тести чистих функцій стану сесії: розбір тега CommandComplete, розшифровка
// двійкових параметрів Bind і побудова stmt.

import (
	"encoding/hex"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgproto3"
)

// mustHex — тестові вектори записані шістнадцятковим рядком (як їх друкує
// psql для bytea): так їх легко звірити з виводом numeric_send()/
// timestamptz_send() на живому кластері, не переписуючи вручну на []byte{...}.
func mustHex(s string) []byte {
	b, err := hex.DecodeString(s)
	if err != nil {
		panic(err)
	}
	return b
}

func TestRowsFromTag(t *testing.T) {
	tests := []struct {
		name   string
		tag    string
		wantN  int64
		wantOK bool
	}{
		{"SELECT з кількістю", "SELECT 42", 42, true},
		{"SELECT нуль рядків", "SELECT 0", 0, true},
		{"INSERT має oid і кількість", "INSERT 0 5", 5, true},
		{"INSERT нуль рядків", "INSERT 0 0", 0, true},
		{"UPDATE", "UPDATE 3", 3, true},
		{"DELETE", "DELETE 17", 17, true},
		{"COPY", "COPY 1000", 1000, true},
		{"MOVE", "MOVE 7", 7, true},
		{"FETCH", "FETCH 7", 7, true},
		{"пробіли навколо", "  SELECT 42  ", 42, true},
		{"велике число", "COPY 2147483648", 2147483648, true},
		{"тег без числа", "BEGIN", 0, false},
		{"порожній тег (EmptyQueryResponse)", "", 0, false},
		{"CREATE TABLE", "CREATE TABLE", 0, false},
		{"останнє слово не число", "SELECT abc", 0, false},
		{"самі пробіли", "   ", 0, false},
		{"сміття з одного слова", "\x00\x01", 0, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			n, ok := rowsFromTag(tc.tag)
			if n != tc.wantN || ok != tc.wantOK {
				t.Errorf("rowsFromTag(%q) = (%d, %v), очікували (%d, %v)",
					tc.tag, n, ok, tc.wantN, tc.wantOK)
			}
		})
	}
}

func TestDecodeBinary(t *testing.T) {
	uuidRaw, _ := hex.DecodeString("0123456789abcdef0123456789abcdef")

	tests := []struct {
		name   string
		oid    uint32
		raw    []byte
		want   any
		wantOK bool
	}{
		{"bool true", oidBool, []byte{1}, true, true},
		{"bool false", oidBool, []byte{0}, false, true},
		{"int2", oidInt2, []byte{0x00, 0x2a}, int64(42), true},
		{"int2 відʼємний", oidInt2, []byte{0xff, 0xff}, int64(-1), true},
		{"int4", oidInt4, []byte{0, 0, 0, 100}, int64(100), true},
		{"int4 відʼємний", oidInt4, []byte{0xff, 0xff, 0xff, 0xff}, int64(-1), true},
		{"int8", oidInt8, []byte{0, 0, 0, 0, 0, 0, 0, 7}, int64(7), true},
		{"float4", oidF4, []byte{0x3f, 0x80, 0x00, 0x00}, float32(1), true},
		{"float8", oidF8, []byte{0x3f, 0xf0, 0, 0, 0, 0, 0, 0}, float64(1), true},
		{"text", oidText, []byte("Іван"), "Іван", true},
		{"varchar", oidVar, []byte("db_test"), "db_test", true},
		{"text порожній", oidText, []byte{}, "", true},
		{"uuid", oidUUID, uuidRaw, "01234567-89ab-cdef-0123-456789abcdef", true},
		{"date — епоха PostgreSQL", oidDate, []byte{0, 0, 0, 0}, "2000-01-01", true},
		{"date вперед на день", oidDate, []byte{0, 0, 0, 1}, "2000-01-02", true},
		{"date назад", oidDate, []byte{0xff, 0xff, 0xff, 0xff}, "1999-12-31", true},

		// timestamptz/timestamp: мкс від 2000-01-01, UTC. Байти взяті з
		// timestamptz_send() на живому кластері 04.09.2026.
		{"timestamptz — епоха", oidTimestamptz, mustHex("0000000000000000"), "2000-01-01 00:00:00+00", true},
		{"timestamptz секунда до епохи", oidTimestamptz, mustHex("fffffffffff0bdc0"), "1999-12-31 23:59:59+00", true},
		{"timestamptz з мікросекундами", oidTimestamptz, mustHex("0002fda643c4c614"), "2026-09-04 12:34:56.789012+00", true},
		{"timestamptz закороткий", oidTimestamptz, []byte{0, 0, 0, 1}, nil, false},

		// numeric: ndigits/weight/sign/dscale + цифри в базі 10000. Байти —
		// з numeric_send() на живому кластері 04.09.2026 (див. коментар
		// decodeNumeric у session.go).
		{"numeric 123.456", oidNumeric, mustHex("0002000000000003007b11d0"), "123.456", true},
		{"numeric 0", oidNumeric, mustHex("0000000000000000"), "0", true},
		{"numeric -42", oidNumeric, mustHex("0001000040000000002a"), "-42", true},
		{"numeric 0.001", oidNumeric, mustHex("0001ffff00000003000a"), "0.001", true},
		{"numeric NaN", oidNumeric, mustHex("00000000c0000000"), "NaN", true},
		{"numeric 100", oidNumeric, mustHex("00010000000000000064"), "100", true},
		{"numeric 1.5", oidNumeric, mustHex("000200000000000100011388"), "1.5", true},
		{"numeric задовгий/короткий заголовок", oidNumeric, []byte{0, 1}, nil, false},
		{"numeric довжина не збігається з ndigits", oidNumeric, mustHex("00020000000000000064"), nil, false},

		// Масиви: байти — з array_send() на живому кластері 04.09.2026.
		{"int4[] {1,2,3}", oidArrInt4,
			mustHex("0000000100000000000000170000000300000001000000040000000100000004000000020000000400000003"),
			"{1,2,3}", true},
		{"text[] з NULL", oidArrText,
			mustHex("00000001000000010000001900000004000000010000000161000000026263ffffffff0000000164"),
			"{a,bc,NULL,d}", true},
		{"int4[] порожній", oidArrInt4, mustHex("000000000000000000000017"), "{}", true},
		{"float8[] {1.5,2.25}", oidArrF8,
			mustHex("0000000100000000000002bd0000000200000001000000083ff8000000000000000000084002000000000000"),
			"{1.5,2.25}", true},
		{"масив закороткий заголовок", oidArrInt4, []byte{0, 0, 0, 1}, nil, false},

		// Невідомий OID і закороткі байти: розшифровка не вдається, але й
		// паніки бути не може — далі виклик підставить шістнадцятковий запис.
		{"невідомий OID", 99999, []byte{0, 1}, nil, false},
		{"OID нуль (Parse не назвав тип)", 0, []byte{0, 0, 0, 1}, nil, false},
		{"bool закороткий", oidBool, []byte{}, nil, false},
		{"bool задовгий", oidBool, []byte{0, 0}, nil, false},
		{"int2 закороткий", oidInt2, []byte{1}, nil, false},
		{"int4 закороткий", oidInt4, []byte{0, 0, 1}, nil, false},
		{"int8 закороткий", oidInt8, []byte{0, 0, 0, 1}, nil, false},
		{"float4 закороткий", oidF4, []byte{0x3f}, nil, false},
		{"float8 закороткий", oidF8, []byte{0x3f, 0xf0}, nil, false},
		{"uuid закороткий", oidUUID, []byte{1, 2, 3}, nil, false},
		{"date закороткий", oidDate, []byte{0, 0}, nil, false},
		{"порожні байти невідомого типу", 9999, []byte{}, nil, false},
		{"nil-зріз", oidInt4, nil, nil, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := decodeBinary(tc.oid, tc.raw)
			if ok != tc.wantOK {
				t.Fatalf("decodeBinary(%d, %x) ok = %v, очікували %v", tc.oid, tc.raw, ok, tc.wantOK)
			}
			if got != tc.want {
				t.Errorf("decodeBinary(%d, %x) = %#v, очікували %#v", tc.oid, tc.raw, got, tc.want)
			}
		})
	}
}

// bind — короткий конструктор повідомлення Bind для тестів.
func bind(formats []int16, params [][]byte) *pgproto3.Bind {
	return &pgproto3.Bind{
		DestinationPortal:    "",
		PreparedStatement:    "",
		ParameterFormatCodes: formats,
		Parameters:           params,
	}
}

func TestDecodeParams(t *testing.T) {
	t.Run("без параметрів повертає nil", func(t *testing.T) {
		if got := decodeParams(bind(nil, nil), nil); got != nil {
			t.Errorf("очікували nil, отримали %#v", got)
		}
		if got := decodeParams(bind(nil, [][]byte{}), nil); got != nil {
			t.Errorf("очікували nil, отримали %#v", got)
		}
	})

	t.Run("текстовий формат кладемо як є", func(t *testing.T) {
		got := decodeParams(bind(nil, [][]byte{[]byte("42"), []byte("Іван")}), nil)
		want := []any{"42", "Іван"}
		assertParams(t, got, want)
	})

	t.Run("порожній список форматів означає текст", func(t *testing.T) {
		got := decodeParams(bind([]int16{}, [][]byte{[]byte("0001")}), []uint32{oidInt4})
		assertParams(t, got, []any{"0001"})
	})

	t.Run("NULL має довжину -1 і приходить як nil", func(t *testing.T) {
		got := decodeParams(bind(nil, [][]byte{nil, []byte("x"), nil}), nil)
		assertParams(t, got, []any{nil, "x", nil})
	})

	t.Run("NULL у двійковому форматі теж nil", func(t *testing.T) {
		got := decodeParams(bind([]int16{1}, [][]byte{nil}), []uint32{oidInt4})
		assertParams(t, got, []any{nil})
	})

	t.Run("один код формату діє на всі параметри", func(t *testing.T) {
		got := decodeParams(bind([]int16{1},
			[][]byte{{0, 0, 0, 1}, {0, 0, 0, 2}}), []uint32{oidInt4, oidInt4})
		assertParams(t, got, []any{int64(1), int64(2)})
	})

	t.Run("коди формату по одному на параметр", func(t *testing.T) {
		got := decodeParams(bind([]int16{0, 1},
			[][]byte{[]byte("текст"), {0, 0, 0, 5}}), []uint32{oidText, oidInt4})
		assertParams(t, got, []any{"текст", int64(5)})
	})

	t.Run("двійковий параметр без відомого OID стає шістнадцятковим", func(t *testing.T) {
		got := decodeParams(bind([]int16{1}, [][]byte{{0xde, 0xad}}), []uint32{1700})
		assertParams(t, got, []any{"\\xdead"})
	})

	t.Run("OID не назвали — теж шістнадцятковий", func(t *testing.T) {
		got := decodeParams(bind([]int16{1}, [][]byte{{0x00, 0x64}}), nil)
		assertParams(t, got, []any{"\\x0064"})
	})

	t.Run("довгі двійкові дані не потрапляють у журнал цілком", func(t *testing.T) {
		raw := make([]byte, 65) // 64 — межа шістнадцяткового запису
		got := decodeParams(bind([]int16{1}, [][]byte{raw}), []uint32{1700})
		s, ok := got[0].(string)
		if !ok || !strings.HasPrefix(s, "<двійкові дані, 65 ") {
			t.Errorf("очікували позначку розміру, отримали %#v", got[0])
		}
	})

	t.Run("рівно 64 байти ще пишемо шістнадцятково", func(t *testing.T) {
		raw := make([]byte, 64)
		got := decodeParams(bind([]int16{1}, [][]byte{raw}), []uint32{1700})
		s := got[0].(string)
		if len(s) != 2+128 {
			t.Errorf("очікували \\x + 128 символів, отримали %d символів", len(s))
		}
	})

	t.Run("довгий текстовий параметр обрізається", func(t *testing.T) {
		raw := []byte(strings.Repeat("a", maxParamLen+10))
		got := decodeParams(bind(nil, [][]byte{raw}), nil)
		s := got[0].(string)
		if !strings.HasSuffix(s, "…") {
			t.Errorf("очікували позначку обрізання наприкінці, отримали хвіст %q", s[len(s)-8:])
		}
		if len(s) != maxParamLen+len("…") {
			t.Errorf("довжина %d, очікували %d", len(s), maxParamLen+len("…"))
		}
	})

	t.Run("рівно maxParamLen не обрізається", func(t *testing.T) {
		raw := []byte(strings.Repeat("a", maxParamLen))
		got := decodeParams(bind(nil, [][]byte{raw}), nil)
		if got[0].(string) != string(raw) {
			t.Errorf("значення рівно на межі змінилося")
		}
	})

	// Клієнт міг надіслати менше OID, ніж параметрів (Parse із нулями):
	// це не має призводити до виходу за межі зрізу.
	t.Run("OID менше, ніж параметрів", func(t *testing.T) {
		got := decodeParams(bind([]int16{1},
			[][]byte{{0, 0, 0, 1}, {0, 0, 0, 2}}), []uint32{oidInt4})
		assertParams(t, got, []any{int64(1), "\\x00000002"})
	})

	// Форматів менше, ніж параметрів, і їх не один: за замовчуванням текст.
	t.Run("кодів формату менше, ніж параметрів", func(t *testing.T) {
		got := decodeParams(bind([]int16{1, 0, 0},
			[][]byte{{0, 0, 0, 1}, []byte("b"), []byte("c"), []byte("d")}),
			[]uint32{oidInt4})
		assertParams(t, got, []any{int64(1), "b", "c", "d"})
	})
}

func assertParams(t *testing.T, got []any, want []any) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("отримали %d параметрів (%#v), очікували %d (%#v)", len(got), got, len(want), want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("параметр %d = %#v, очікували %#v", i, got[i], want[i])
		}
	}
}

func TestNewStmt(t *testing.T) {
	t.Run("звичайний оператор", func(t *testing.T) {
		st := newStmt("S_1", "SELECT * FROM orders WHERE id = 42", blockedDefault())
		if st.name != "S_1" {
			t.Errorf("name = %q", st.name)
		}
		if st.sql != "SELECT * FROM orders WHERE id = 42" {
			t.Errorf("sql змінено: %q", st.sql)
		}
		if st.kind != "select" {
			t.Errorf("kind = %q, очікували select", st.kind)
		}
		if st.fingerprint != "select * from orders where id = ?" {
			t.Errorf("fingerprint = %q", st.fingerprint)
		}
		if st.isCatalog {
			t.Errorf("isCatalog = true для запиту до власної таблиці")
		}
		if st.blocked != "" {
			t.Errorf("blocked = %q, очікували порожнє", st.blocked)
		}
		if st.paramOIDs != nil {
			t.Errorf("paramOIDs мають лишатися nil до присвоєння з Parse")
		}
	})

	t.Run("службовий запит позначається як каталог", func(t *testing.T) {
		st := newStmt("", "SELECT relname FROM pg_class", nil)
		if !st.isCatalog {
			t.Errorf("isCatalog = false для pg_class")
		}
	})

	t.Run("заборонений параметр фіксується", func(t *testing.T) {
		st := newStmt("", "SET work_mem = '1GB'", blockedDefault())
		if st.blocked != "work_mem" {
			t.Errorf("blocked = %q, очікували work_mem", st.blocked)
		}
		if st.kind != "other" {
			t.Errorf("kind = %q, очікували other", st.kind)
		}
	})

	t.Run("nil-набір заборон нічого не блокує", func(t *testing.T) {
		st := newStmt("", "SET work_mem = '1GB'", nil)
		if st.blocked != "" {
			t.Errorf("blocked = %q, очікували порожнє", st.blocked)
		}
	})

	t.Run("порожній текст не панікує", func(t *testing.T) {
		st := newStmt("", "", blockedDefault())
		if st.kind != "other" || st.fingerprint != "" || st.isCatalog || st.blocked != "" {
			t.Errorf("несподіваний stmt для порожнього тексту: %+v", st)
		}
	})
}
