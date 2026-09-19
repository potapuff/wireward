package proxy

// Тести розпізнавання клієнта за StartupMessage.
//
// Головне правило, яке тут і перевіряється: здогад ніколи не подається як
// факт. Якщо клієнт назвався — Guessed=false навіть для незнайомого імені;
// якщо не назвався — Guessed=true і лише за непрямою ознакою pgjdbc.

import (
	"strings"
	"testing"
)

func TestParseClientВідоміКлієнти(t *testing.T) {
	tests := []struct {
		name        string
		app         string
		wantName    string
		wantVersion string
		wantPurpose string
	}{
		{"psql без версії", "psql", "psql", "", ""},
		{"DBeaver з призначенням Main",
			"DBeaver 24.3.0 - Main <db_test>", "DBeaver", "24.3.0", "Main"},
		{"DBeaver з призначенням Metadata",
			"DBeaver 24.3.0 - Metadata <db_test>", "DBeaver", "24.3.0", "Metadata"},
		{"DBeaver у нижньому регістрі", "dbeaver 25.0", "DBeaver", "25.0", ""},
		{"DataGrip", "DataGrip 2025.2", "DataGrip", "2025.2", ""},
		{"DataGrip новішої версії (З17: перевірка, що версія не захардкожена)",
			"DataGrip 2025.3", "DataGrip", "2025.3", ""},
		{"IntelliJ зводиться до DataGrip", "IntelliJ IDEA 2024.1", "DataGrip", "2024.1", ""},
		{"pgjdbc за назвою драйвера", "PostgreSQL JDBC Driver", "pgjdbc", "", ""},
		{"psycopg", "psycopg 3.2", "psycopg", "3.2", ""},
		{"pgAdmin: CONN — не призначення", "pgAdmin 4 - CONN:8817", "pgAdmin", "4", ""},
		{"pgcli", "pgcli 4.1.0", "pgcli", "4.1.0", ""},
		{"navicat", "Navicat", "Navicat", "", ""},
		{"tableplus", "TablePlus", "TablePlus", "", ""},
		{"beekeeper", "Beekeeper Studio", "Beekeeper", "", ""},
		{"pgx", "pgx", "pgx", "", ""},
		{"libpq", "libpq", "libpq", "", ""},
		{"власні службові зʼєднання", "observer-shipper", "Wireward", "", ""},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := ParseClient(map[string]string{"application_name": tc.app})
			if got.Name != tc.wantName {
				t.Errorf("Name = %q, очікували %q", got.Name, tc.wantName)
			}
			if got.Version != tc.wantVersion {
				t.Errorf("Version = %q, очікували %q", got.Version, tc.wantVersion)
			}
			if got.Purpose != tc.wantPurpose {
				t.Errorf("Purpose = %q, очікували %q", got.Purpose, tc.wantPurpose)
			}
			if got.Guessed {
				t.Errorf("Guessed = true, хоча клієнт представився")
			}
			if got.Raw != tc.app {
				t.Errorf("Raw = %q, очікували %q", got.Raw, tc.app)
			}
		})
	}
}

func TestParseClientНезнайомийАлеНазвався(t *testing.T) {
	info := ParseClient(map[string]string{"application_name": "MyOwnTool 1.0"})
	if info.Name != "MyOwnTool" {
		t.Errorf("Name = %q, очікували MyOwnTool", info.Name)
	}
	if info.Version != "1.0" {
		t.Errorf("Version = %q, очікували 1.0", info.Version)
	}
	if info.Guessed {
		t.Errorf("Guessed = true, хоча клієнт назвав себе сам — це факт, а не здогад")
	}
}

func TestParseClientЗдогадПроPgjdbc(t *testing.T) {
	t.Run("порожній application_name + extra_float_digits", func(t *testing.T) {
		info := ParseClient(map[string]string{
			"user": "s_01", "database": "db_01", "extra_float_digits": "3",
		})
		if info.Name != "pgjdbc" || !info.Guessed {
			t.Errorf("Name = %q, Guessed = %v; очікували pgjdbc/true", info.Name, info.Guessed)
		}
		if info.Raw != "" {
			t.Errorf("Raw = %q, очікували порожнє", info.Raw)
		}
	})

	t.Run("немає ні назви, ні ознаки — нічого не вигадуємо", func(t *testing.T) {
		info := ParseClient(map[string]string{"user": "s_01", "database": "db_01"})
		if info.Name != "" || info.Guessed {
			t.Errorf("Name = %q, Guessed = %v; очікували порожнє/false", info.Name, info.Guessed)
		}
	})

	t.Run("клієнт назвався — здогад не потрібен", func(t *testing.T) {
		info := ParseClient(map[string]string{
			"application_name": "psql", "extra_float_digits": "3",
		})
		if info.Name != "psql" || info.Guessed {
			t.Errorf("Name = %q, Guessed = %v; очікували psql/false", info.Name, info.Guessed)
		}
	})

	t.Run("порожній набір параметрів", func(t *testing.T) {
		info := ParseClient(map[string]string{})
		if info.Name != "" || info.Guessed || info.Raw != "" {
			t.Errorf("несподіваний результат: %+v", info)
		}
		if info.Params == nil {
			t.Errorf("Params має бути ініціалізованою мапою, а не nil")
		}
	})

	t.Run("nil-мапа не панікує", func(t *testing.T) {
		info := ParseClient(nil)
		if info.Name != "" || info.Guessed {
			t.Errorf("несподіваний результат: %+v", info)
		}
	})
}

func TestParseClientПараметри(t *testing.T) {
	info := ParseClient(map[string]string{
		"user":               "s_01",
		"database":           "db_01",
		"application_name":   "psql",
		"client_encoding":    "UTF8",
		"DateStyle":          "ISO, DMY",
		"TimeZone":           "Europe/Kyiv",
		"replication":        "false",
		"extra_float_digits": "3",
		"options":            "-c search_path=lab",
	})
	// Нецікаві параметри мають власні колонки або нічого не кажуть про клієнта.
	for _, k := range []string{"user", "database", "application_name",
		"client_encoding", "DateStyle", "TimeZone", "replication"} {
		if _, ok := info.Params[k]; ok {
			t.Errorf("параметр %q не мав потрапити в Params", k)
		}
	}
	if info.Params["extra_float_digits"] != "3" {
		t.Errorf("extra_float_digits загубився: %#v", info.Params)
	}
	if info.Params["options"] != "-c search_path=lab" {
		t.Errorf("options загубився: %#v", info.Params)
	}
	if len(info.Params) != 2 {
		t.Errorf("у Params %d записів, очікували 2: %#v", len(info.Params), info.Params)
	}
}

// Студент може підставити в application_name що завгодно — це рядок, який він
// повністю контролює. Розбір не має ні панікувати, ні виходити за межі зрізів.
func TestParseClientСміття(t *testing.T) {
	tests := []struct {
		name string
		app  string
	}{
		{"порожній рядок", ""},
		{"самі пробіли", "   \t\n  "},
		{"один дефіс", "-"},
		{"саме роздільник призначення", " - "},
		{"роздільник без хвоста", "DBeaver - "},
		{"роздільник на початку", " - Main"},
		{"двокрапка після слова", "- CONN:1"},
		{"саме число", "1.2.3"},
		{"крапки без цифр", "..."},
		{"нульові байти", "\x00\x00\x00"},
		{"керуючі символи", "\x01\x02 - \x03"},
		{"неповний UTF-8", "\xff\xfe"},
		{"емодзі", "🙂 1.0 - 🙃"},
		{"багато роздільників", "a - b - c - d"},
		{"дуже багато версій", "1 2 3 4 5 6 7 8 9"},
		{"перевід рядка всередині", "DBeaver\n24.3.0\n- Main"},
		{"дуже довгий рядок", strings.Repeat("x", 100000)},
		{"довгий рядок із роздільниками", strings.Repeat("a - 1 ", 10000)},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			info := ParseClient(map[string]string{"application_name": tc.app})
			// Єдина обовʼязкова інваріанта: Raw — це те, що надіслав клієнт
			// (з обрізаними пробілами й до maxClientField байтів), і розбір
			// завершився без паніки.
			if want := clip(strings.TrimSpace(tc.app)); info.Raw != want {
				t.Errorf("Raw = %q, очікували %q", info.Raw, want)
			}
			if info.Params == nil {
				t.Errorf("Params = nil")
			}
			// Здогад можливий лише за відсутності назви.
			if info.Guessed && info.Name != "pgjdbc" {
				t.Errorf("Guessed = true при Name = %q", info.Name)
			}
		})
	}
}

// ПІДОЗРА НА БАГ (client.go:70-110): довжина не обмежена ніде.
//
// StartupMessage читається без SetMaxBodyLen (pgproto3 без нього нічого не
// обмежує), а ParseClient кладе application_name у Name і Raw як є. Далі
// newSession() пише Name у obs.sessions.client_name, над якою є btree-індекс
// sessions_client_idx: значення понад ~2704 байти валить INSERT, і ВСЯ сесія
// не потрапляє в obs.sessions. Тобто студент одним рядком підключення робить
// свою сесію невидимою для адмінки.
// Був баг: довжина Name/Raw/Params не обмежувалась. Startup-пакет студент
// керує повністю (до 10000 Б — межа самого протоколу pgproto3), а
// client_name лежить під btree-індексом sessions_client_idx: надто довгий
// application_name валив INSERT у obs.sessions.
func TestParseClientДовжинаМаєБутиОбмежена(t *testing.T) {
	const maxLen = 256
	info := ParseClient(map[string]string{
		"application_name": strings.Repeat("x", 10000),
		"options":          strings.Repeat("y", 10000),
	})
	if len(info.Name) > maxLen {
		t.Errorf("len(Name) = %d, очікували не більше %d", len(info.Name), maxLen)
	}
	if len(info.Raw) > maxLen {
		t.Errorf("len(Raw) = %d, очікували не більше %d", len(info.Raw), maxLen)
	}
	if len(info.Params["options"]) > maxLen {
		t.Errorf("len(Params[options]) = %d, очікували не більше %d",
			len(info.Params["options"]), maxLen)
	}
}
