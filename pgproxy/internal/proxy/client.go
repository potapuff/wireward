package proxy

// Розпізнавання клієнта.
//
// Клієнт сам представляється в StartupMessage — здебільшого через
// `application_name`. Зберігати цей рядок як є мало: за ним не пофільтруєш і
// не порахуєш, скільки студентів сидить у DataGrip, а скільки в psql. Тому
// розкладаємо на назву й версію, а сирі параметри лишаємо поруч: якщо
// зʼявиться клієнт, якого ми не знаємо, розібратися буде з чим.
//
// Головне правило: здогад ніколи не подається як факт. Якщо клієнт не
// представився, ставимо прапорець `guessed`, і адмінка показує це окремо.

import (
	"regexp"
	"strings"
	"unicode/utf8"
)

// Параметри, які вже мають власні колонки або нічого не кажуть про клієнта.
var boringParams = map[string]bool{
	"user": true, "database": true, "application_name": true,
	"client_encoding": true, "DateStyle": true, "TimeZone": true,
	"replication": true,
}

// maxClientField — стеля для Name/Version/Purpose/Raw і кожного значення в
// Params. Студент керує startup-пакетом повністю (до 10000 Б, межа самого
// протоколу), а client_name лежить під btree-індексом sessions_client_idx:
// один надто довгий application_name валив INSERT у obs.sessions.
const maxClientField = 256

// clip обрізає рядок до maxClientField байтів, не розрубуючи руну.
func clip(s string) string {
	if len(s) <= maxClientField {
		return s
	}
	s = s[:maxClientField]
	for len(s) > 0 && !utf8.RuneStart(s[len(s)-1]) {
		s = s[:len(s)-1]
	}
	return s
}

type ClientInfo struct {
	Name    string            // DBeaver, DataGrip, psql, …; порожнє, якщо не знаємо
	Version string            // 24.3.0, 2025.2, …
	Purpose string            // DBeaver і DataGrip дописують призначення: Main, Metadata
	Guessed bool              // назву не отримали, а вивели з непрямих ознак
	Raw     string            // application_name як є
	Params  map[string]string // решта параметрів startup
}

// «DBeaver 24.3.0 - Main <db_test>», «DataGrip 2025.2», «pgAdmin 4 - CONN:1234»
// Один регексп на всі ці форми виходить нечитабельним і все одно ловить не все,
// тому розбираємо рядок частинами.
var (
	reVersion = regexp.MustCompile(`\b\d+(?:\.\d+)*\b`)
	// Слово після « - », але не ідентифікатор виду «CONN:8817»: двокрапка
	// одразу за словом означає технічний ярлик, а не призначення.
	rePurpose = regexp.MustCompile(`^\s*-\s*(\p{L}+)(?:[^:\p{L}]|$)`)
)

// known зводить різні написання до одного імені, щоб фільтр в адмінці
// не розсипався на «DBeaver», «dbeaver» і «DBeaver 24.3».
var known = []struct {
	match string
	name  string
}{
	{"dbeaver", "DBeaver"},
	{"datagrip", "DataGrip"},
	{"intellij", "DataGrip"},
	{"pgadmin", "pgAdmin"},
	{"psql", "psql"},
	{"pgcli", "pgcli"},
	{"psycopg", "psycopg"},
	{"jdbc", "pgjdbc"},
	{"navicat", "Navicat"},
	{"tableplus", "TablePlus"},
	{"beekeeper", "Beekeeper"},
	{"pgx", "pgx"},
	{"libpq", "libpq"},
	{"observer", "Wireward"},
}

func ParseClient(params map[string]string) ClientInfo {
	app := strings.TrimSpace(params["application_name"])
	info := ClientInfo{Raw: clip(app), Params: map[string]string{}}
	for k, v := range params {
		if !boringParams[k] {
			info.Params[k] = clip(v)
		}
	}

	if app != "" {
		low := strings.ToLower(app)
		for _, k := range known {
			if strings.Contains(low, k.match) {
				info.Name = k.name
				break
			}
		}

		// Голова рядка — до версії або до « - », хвіст — призначення.
		head, tail := app, ""
		if i := strings.Index(app, " - "); i >= 0 {
			head, tail = app[:i], app[i+1:]
		}
		if loc := reVersion.FindStringIndex(head); loc != nil {
			info.Version = clip(head[loc[0]:loc[1]])
			head = head[:loc[0]]
		}
		// «Main», «Metadata» — так підписуються DBeaver і DataGrip.
		// «CONN:8817» від pgAdmin призначенням не є і сюди не потрапляє.
		if m := rePurpose.FindStringSubmatch(tail); m != nil {
			info.Purpose = clip(m[1])
		}
		if info.Name == "" {
			// Клієнт назвався якось інакше — беремо як є. Це не здогад:
			// він справді так себе назвав, просто ми його не знаємо.
			info.Name = clip(strings.TrimSpace(strings.Trim(head, " -")))
		}
		if info.Name != "" {
			return info
		}
	}

	// Клієнт не представився. Єдиний здогад, який дозволяємо собі робити:
	// pgjdbc завжди надсилає extra_float_digits, а більшість інших — ні.
	// Позначаємо як здогад, щоб адмінка не видавала його за факт.
	if _, ok := params["extra_float_digits"]; ok {
		info.Name, info.Guessed = "pgjdbc", true
	}
	return info
}
