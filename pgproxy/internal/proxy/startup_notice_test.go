package proxy

// Попередження «ви не у своїй базі».
//
// Підключення до службової бази postgres дозволене навмисно (інакше не зробити
// DROP DATABASE власної), але мовчазним воно виглядало як поломка: перший же
// CREATE TABLE давав 42501 permission denied for schema public. Тут
// перевіряється, що попередження зʼявляється саме там, де треба, і не заважає
// в нормальному випадку.

import (
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgproto3"

	"wireward/pgproxy/internal/auth"
)

func TestServiceDBNoticeСлужбоваБаза(t *testing.T) {
	n := serviceDBNotice(auth.ServiceDB, "db_2f1c")
	if n == nil {
		t.Fatal("для підключення до postgres попередження не надіслано")
	}
	// Це саме NoticeResponse: зʼєднання лишається робочим.
	var _ pgproto3.BackendMessage = n

	if n.Severity != "WARNING" || n.SeverityUnlocalized != "WARNING" {
		t.Errorf("Severity = %q/%q, очікували WARNING",
			n.Severity, n.SeverityUnlocalized)
	}
	// 01000 (warning), а не 00000: клієнти на кшталт pgjdbc показують
	// попередження помітніше за NOTICE.
	if n.Code != "01000" {
		t.Errorf("Code = %q, очікували 01000", n.Code)
	}
	if n.Message == "" {
		t.Error("порожній Message")
	}
	// Головна цінність тексту — назвати справжню базу студента, інакше порада
	// «підключіться до своєї» нічим не допомагає.
	for _, f := range []struct{ name, val string }{
		{"Message", n.Message}, {"Hint", n.Hint},
	} {
		if !strings.Contains(f.val, "db_2f1c") {
			t.Errorf("%s не називає базу студента: %q", f.name, f.val)
		}
	}
	if !strings.Contains(n.Detail, "permission denied for schema public") {
		t.Errorf("Detail не пояснює, яку саме помилку студент побачить: %q", n.Detail)
	}
	if !strings.Contains(n.Hint, "DROP DATABASE") {
		t.Errorf("Hint не пояснює, навіщо тут узагалі можна бути: %q", n.Hint)
	}
}

func TestServiceDBNoticeМовчитьДеТреба(t *testing.T) {
	tests := []struct {
		name     string
		database string
		ownDB    string
	}{
		{"своя база — попереджати нема про що", "db_2f1c", "db_2f1c"},
		{"імені власної бази не знаємо — порада була б порожньою",
			auth.ServiceDB, ""},
		{"власна база звалася б postgres", auth.ServiceDB, auth.ServiceDB},
		{"чужа база — це окремий шлях відмови (ErrWrongDB), не попередження",
			"db_чужа", "db_2f1c"},
		{"регістр значення не має: службова база — рівно postgres",
			"POSTGRES", "db_2f1c"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if n := serviceDBNotice(tc.database, tc.ownDB); n != nil {
				t.Errorf("зайве попередження: %+v", n)
			}
		})
	}
}

// Попередження їде в тому ж потоці, що й решта стартової послідовності, тому
// воно мусить кодуватися без помилки — інакше Flush обірве рукостискання.
func TestServiceDBNoticeКодуєтьсяВПротокол(t *testing.T) {
	n := serviceDBNotice(auth.ServiceDB, "db_2f1c")
	buf, err := n.Encode(nil)
	if err != nil {
		t.Fatalf("Encode: %v", err)
	}
	if len(buf) == 0 || buf[0] != 'N' {
		t.Fatalf("очікували повідомлення типу 'N' (NoticeResponse), маємо % x",
			buf[:min(len(buf), 8)])
	}
	var back pgproto3.NoticeResponse
	if err := back.Decode(buf[5:]); err != nil {
		t.Fatalf("Decode: %v", err)
	}
	if back.Message != n.Message || back.Hint != n.Hint || back.Code != n.Code {
		t.Errorf("після кодування зміст змінився: %+v", back)
	}
}
