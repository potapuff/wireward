package proxy

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"sync"
	"testing"
	"time"
)

// countingHandler збирає значення атрибута count із записаних рядків.
type countingHandler struct {
	mu     sync.Mutex
	counts []int
}

func (h *countingHandler) Enabled(context.Context, slog.Level) bool { return true }
func (h *countingHandler) WithAttrs([]slog.Attr) slog.Handler       { return h }
func (h *countingHandler) WithGroup(string) slog.Handler            { return h }
func (h *countingHandler) Handle(_ context.Context, r slog.Record) error {
	r.Attrs(func(a slog.Attr) bool {
		if a.Key == "count" {
			h.mu.Lock()
			h.counts = append(h.counts, int(a.Value.Int64()))
			h.mu.Unlock()
		}
		return true
	})
	return nil
}

func (h *countingHandler) total() (sum, lines int) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, c := range h.counts {
		sum += c
	}
	return sum, len(h.counts)
}

// Сплеск відмов, що весь укладається в одне вікно, мусить бути порахований
// повністю: рання версія логувала count=1 і мовчки накопичувала решту, поки
// колись потім не прийде наступна, вже не пов'язана подія.
func TestLogThrottleДораховуєСплескПоТаймеру(t *testing.T) {
	h := &countingHandler{}
	old := slog.Default()
	slog.SetDefault(slog.New(h))
	defer slog.SetDefault(old)

	lt := newLogThrottle(50*time.Millisecond, slog.LevelWarn, "тест")
	defer lt.stop() // щоб таймер не дописав рядок уже після відновлення slog
	const n = 1000
	for i := 0; i < n; i++ {
		lt.note("limit", 200)
	}
	if sum, lines := h.total(); sum != 1 || lines != 1 {
		t.Fatalf("одразу після сплеску: очікував один рядок count=1, маю sum=%d lines=%d", sum, lines)
	}

	time.Sleep(150 * time.Millisecond)
	sum, lines := h.total()
	if sum != n {
		t.Fatalf("після вікна: сума count = %d, очікував %d (рядків: %d)", sum, n, lines)
	}
	if lines != 2 {
		t.Fatalf("очікував рівно 2 рядки на сплеск (перший + добір), маю %d", lines)
	}
}

// Тиша після вікна не має породжувати порожніх рядків.
func TestLogThrottleБезПорожніхРядків(t *testing.T) {
	h := &countingHandler{}
	old := slog.Default()
	slog.SetDefault(slog.New(h))
	defer slog.SetDefault(old)

	lt := newLogThrottle(30*time.Millisecond, slog.LevelWarn, "тест")
	defer lt.stop()
	lt.note()
	time.Sleep(120 * time.Millisecond)
	if sum, lines := h.total(); sum != 1 || lines != 1 {
		t.Fatalf("очікував один рядок count=1, маю sum=%d lines=%d", sum, lines)
	}
}

type errListener struct {
	mu sync.Mutex
	n  int
}

func (l *errListener) Accept() (net.Conn, error) {
	l.mu.Lock()
	l.n++
	l.mu.Unlock()
	return nil, errors.New("EMFILE")
}
func (l *errListener) Close() error   { return nil }
func (l *errListener) Addr() net.Addr { return &net.TCPAddr{} }

// Постійна помилка Accept не має крутити цикл на повній швидкості: за 200 мс
// без backoff набігли б десятки тисяч ітерацій.
func TestAcceptLoopНеКрутитьсяНаПомилках(t *testing.T) {
	h := &countingHandler{}
	old := slog.Default()
	slog.SetDefault(slog.New(h))
	defer slog.SetDefault(old)

	ln := &errListener{}
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	if err := AcceptLoop(ctx, ln, func(net.Conn) { t.Fatal("зʼєднань не мало бути") }); err != nil {
		t.Fatalf("AcceptLoop: %v", err)
	}

	ln.mu.Lock()
	iters := ln.n
	ln.mu.Unlock()
	if iters > 50 {
		t.Fatalf("ітерацій за 200мс: %d — backoff не працює", iters)
	}
	if _, lines := h.total(); lines > 3 {
		t.Fatalf("рядків у лозі: %d — лог помилок Accept не тротлиться", lines)
	}
}

// Зупинка посеред вікна не має ковтати вже накопичені відмови.
func TestLogThrottleДобираєЗалишокНаЗупинці(t *testing.T) {
	h := &countingHandler{}
	old := slog.Default()
	slog.SetDefault(slog.New(h))
	defer slog.SetDefault(old)

	lt := newLogThrottle(time.Hour, slog.LevelWarn, "тест")
	for i := 0; i < 10; i++ {
		lt.note()
	}
	lt.stop()
	if sum, lines := h.total(); sum != 10 || lines != 2 {
		t.Fatalf("після stop: sum=%d lines=%d, очікував 10 у двох рядках", sum, lines)
	}
}

func TestPreauthLimit(t *testing.T) {
	if got := preauthLimit(100); got != 200 {
		t.Fatalf("preauthLimit(100) = %d, очікував 200", got)
	}
	if got := preauthLimit(4); got != 64 {
		t.Fatalf("preauthLimit(4) = %d, очікував нижню межу 64", got)
	}
}

func TestReleaseOnceНеЗабираєЧужийСлот(t *testing.T) {
	sem := make(chan struct{}, 2)
	sem <- struct{}{}
	sem <- struct{}{}
	rel := releaseOnce(sem)
	rel()
	rel()
	rel()
	if len(sem) != 1 {
		t.Fatalf("у семафорі лишилось %d слотів, очікував 1", len(sem))
	}
}
