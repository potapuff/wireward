package journal

// Тести шляху запису журналу: spool у форматі JSONL і дрібні перетворювачі
// для CopyFrom.

import (
	"bufio"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestWriterПишеJSONLІРядокЧитаєтьсяНазад(t *testing.T) {
	path := filepath.Join(t.TempDir(), "journal.jsonl")
	jw, err := NewWriter(path)
	if err != nil {
		t.Fatalf("NewWriter: %v", err)
	}

	rows := int64(42)
	want := &Query{
		SessionID:    "0f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
		StudentID:    "11111111-2222-3333-4444-555555555555",
		Seq:          7,
		StartedAt:    time.Date(2026, 9, 3, 12, 30, 45, 123456000, time.UTC),
		DurationMS:   12.345,
		SQLText:      "SELECT * FROM orders WHERE name = 'Іван'",
		SQLTruncated: false,
		Fingerprint:  "select * from orders where name = ?",
		Kind:         "select",
		Protocol:     "extended",
		Params:       `["Іван",42,null]`,
		Status:       "ok",
		RowsAffected: &rows,
		IsCatalog:    false,
	}
	if err := jw.Write(want); err != nil {
		t.Fatalf("Write: %v", err)
	}
	if err := jw.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}

	got := readSpool(t, path)
	if len(got) != 1 {
		t.Fatalf("у spool %d рядків, очікували 1", len(got))
	}
	g := got[0]
	if g.SessionID != want.SessionID || g.StudentID != want.StudentID || g.Seq != want.Seq {
		t.Errorf("ідентифікатори не збіглися: %+v", g)
	}
	if !g.StartedAt.Equal(want.StartedAt) {
		t.Errorf("StartedAt = %v, очікували %v", g.StartedAt, want.StartedAt)
	}
	if g.DurationMS != want.DurationMS {
		t.Errorf("DurationMS = %v, очікували %v", g.DurationMS, want.DurationMS)
	}
	if g.SQLText != want.SQLText || g.Fingerprint != want.Fingerprint {
		t.Errorf("текст запиту спотворено: %q / %q", g.SQLText, g.Fingerprint)
	}
	if g.Kind != want.Kind || g.Protocol != want.Protocol || g.Status != want.Status {
		t.Errorf("класифікація не збіглася: %+v", g)
	}
	if g.Params != want.Params {
		t.Errorf("Params = %q, очікували %q", g.Params, want.Params)
	}
	if g.RowsAffected == nil || *g.RowsAffected != rows {
		t.Errorf("RowsAffected = %v, очікували %d", g.RowsAffected, rows)
	}
	if g.IsCatalog {
		t.Errorf("IsCatalog = true")
	}
}

func TestWriterКоженЗаписОкремийРядок(t *testing.T) {
	path := filepath.Join(t.TempDir(), "journal.jsonl")
	jw, err := NewWriter(path)
	if err != nil {
		t.Fatalf("NewWriter: %v", err)
	}
	// Текст із переводами рядка не має ламати формат: JSON їх екранує.
	for i := 1; i <= 3; i++ {
		q := &Query{
			SessionID: "s", StudentID: "st", Seq: i, Status: "ok",
			StartedAt: time.Unix(0, 0).UTC(),
			SQLText:   "SELECT\n1\n-- коментар\n",
			Kind:      "select", Protocol: "simple",
		}
		if err := jw.Write(q); err != nil {
			t.Fatalf("Write %d: %v", i, err)
		}
	}
	if err := jw.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}

	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("ReadFile: %v", err)
	}
	if n := strings.Count(string(raw), "\n"); n != 3 {
		t.Errorf("у файлі %d переводів рядка, очікували рівно 3", n)
	}
	got := readSpool(t, path)
	if len(got) != 3 {
		t.Fatalf("розібрано %d записів, очікували 3", len(got))
	}
	for i, q := range got {
		if q.Seq != i+1 {
			t.Errorf("порядок порушено: запис %d має seq %d", i, q.Seq)
		}
		if q.SQLText != "SELECT\n1\n-- коментар\n" {
			t.Errorf("текст спотворено: %q", q.SQLText)
		}
	}
}

// Файл відкривається в режимі append: другий Writer дописує, а не затирає.
// Від цього залежить, чи переживе журнал перезапуск проксі.
func TestWriterДописуєПісляПерезапуску(t *testing.T) {
	path := filepath.Join(t.TempDir(), "journal.jsonl")
	for i := 1; i <= 2; i++ {
		jw, err := NewWriter(path)
		if err != nil {
			t.Fatalf("NewWriter %d: %v", i, err)
		}
		if err := jw.Write(&Query{SessionID: "s", StudentID: "st", Seq: i,
			Status: "ok", Kind: "select", Protocol: "simple",
			StartedAt: time.Unix(0, 0).UTC()}); err != nil {
			t.Fatalf("Write %d: %v", i, err)
		}
		if err := jw.Close(); err != nil {
			t.Fatalf("Close %d: %v", i, err)
		}
	}
	if got := readSpool(t, path); len(got) != 2 {
		t.Errorf("у spool %d записів, очікували 2 — файл перезаписано замість дозапису", len(got))
	}
}

// Порожні поля з omitempty не мають зʼявлятися в рядку: інакше spool росте на
// кожному записі даремно.
func TestWriterПропускаєПорожніПоля(t *testing.T) {
	path := filepath.Join(t.TempDir(), "journal.jsonl")
	jw, err := NewWriter(path)
	if err != nil {
		t.Fatalf("NewWriter: %v", err)
	}
	if err := jw.Write(&Query{SessionID: "s", StudentID: "st", Seq: 1,
		Status: "ok", Kind: "select", Protocol: "simple",
		StartedAt: time.Unix(0, 0).UTC()}); err != nil {
		t.Fatalf("Write: %v", err)
	}
	if err := jw.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("ReadFile: %v", err)
	}
	line := string(raw)
	for _, k := range []string{"params", "sqlstate", "error_message",
		"error_position", "rows_affected"} {
		if strings.Contains(line, `"`+k+`"`) {
			t.Errorf("порожнє поле %q потрапило в рядок: %s", k, line)
		}
	}
	// А обовʼязкові поля мають бути завжди, навіть у «нульовому» значенні.
	for _, k := range []string{"session_id", "student_id", "seq", "started_at",
		"duration_ms", "sql_text", "sql_truncated", "sql_fingerprint",
		"sql_kind", "protocol", "status", "is_catalog"} {
		if !strings.Contains(line, `"`+k+`"`) {
			t.Errorf("обовʼязкове поле %q відсутнє в рядку: %s", k, line)
		}
	}
}

// Запис помилки має донести SQLSTATE, текст і позицію — саме з них в адмінці
// складається «де студент застряг».
func TestWriterЗаписПомилки(t *testing.T) {
	path := filepath.Join(t.TempDir(), "journal.jsonl")
	jw, err := NewWriter(path)
	if err != nil {
		t.Fatalf("NewWriter: %v", err)
	}
	if err := jw.Write(&Query{
		SessionID: "s", StudentID: "st", Seq: 1,
		StartedAt: time.Unix(0, 0).UTC(),
		SQLText:   "SELCT 1", Kind: "other", Protocol: "extended",
		Status:       "error",
		SQLState:     "42601",
		ErrorMessage: `syntax error at or near "SELCT"`,
		ErrorPos:     1,
		IsCatalog:    false,
	}); err != nil {
		t.Fatalf("Write: %v", err)
	}
	if err := jw.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	got := readSpool(t, path)
	if len(got) != 1 {
		t.Fatalf("розібрано %d записів", len(got))
	}
	if got[0].Status != "error" || got[0].SQLState != "42601" || got[0].ErrorPos != 1 {
		t.Errorf("поля помилки не збіглися: %+v", got[0])
	}
	if got[0].ErrorMessage != `syntax error at or near "SELCT"` {
		t.Errorf("ErrorMessage = %q", got[0].ErrorMessage)
	}
}

func readSpool(t *testing.T, path string) []*Query {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer f.Close()
	var out []*Query
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		var q Query
		if err := json.Unmarshal(sc.Bytes(), &q); err != nil {
			t.Fatalf("рядок spool не розбирається: %v (%s)", err, sc.Text())
		}
		out = append(out, &q)
	}
	if err := sc.Err(); err != nil {
		t.Fatalf("читання spool: %v", err)
	}
	return out
}

func TestNullStr(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want any
	}{
		{"порожній рядок стає NULL", "", nil},
		{"непорожній лишається", "42601", "42601"},
		{"пробіл не порожній", " ", " "},
		{"нуль-символ не порожній", "\x00", "\x00"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := nullStr(tc.in); got != tc.want {
				t.Errorf("nullStr(%q) = %#v, очікували %#v", tc.in, got, tc.want)
			}
		})
	}
}

func TestNullInt(t *testing.T) {
	tests := []struct {
		name string
		in   int
		want any
	}{
		{"нуль стає NULL", 0, nil},
		{"позиція 1 лишається", 1, 1},
		{"велика позиція", 100500, 100500},
		{"відʼємне лишається", -1, -1},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := nullInt(tc.in); got != tc.want {
				t.Errorf("nullInt(%d) = %#v, очікували %#v", tc.in, got, tc.want)
			}
		})
	}
}
