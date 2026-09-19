package proxy

import (
	"context"
	"log/slog"
	"net"
	"sync"
	"time"
)

// AcceptLoop — цикл прийому зʼєднань із паузою на помилках.
//
// Без паузи вичерпання дескрипторів (EMFILE/ENFILE — реальний сценарій,
// відколи в compose.yaml стоїть ulimits.nofile, З21) крутить цикл на 100% CPU
// і за секунди виштовхує з json-file усю історію логів до інциденту. Схема та
// сама, що в net/http.Server: 5 мс, подвоєння, стеля 1 с, скидання на першому
// успішному зʼєднанні. Альтернатива «вийти з циклу на першій помилці» гірша за
// обидві: слухач помирає назавжди, а контейнер лишається «здоровим».
//
// Один код на два місця: основний шлях (Server.Listen) і аварійний
// PROXY_PASSTHROUGH (runPassthrough у cmd/pgproxy).
func AcceptLoop(ctx context.Context, ln net.Listener, onConn func(net.Conn)) error {
	const (
		delayMin = 5 * time.Millisecond
		delayMax = time.Second
	)
	// Лог помилок Accept теж тротлиться: поки backoff розганяється з 5 мс до
	// секунди, нетротльований slog встиг би написати сотні рядків саме тоді,
	// коли лог найцінніший.
	errLog := newLogThrottle(time.Second, slog.LevelWarn, "accept")
	defer errLog.stop() // недобрані помилки останнього вікна — в лог перед виходом
	var delay time.Duration
	for {
		c, err := ln.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			if delay == 0 {
				delay = delayMin
			} else if delay *= 2; delay > delayMax {
				delay = delayMax
			}
			errLog.note("err", err.Error(), "retry_in", delay.String())
			select {
			case <-time.After(delay):
			case <-ctx.Done():
				return nil
			}
			continue
		}
		delay = 0
		onConn(c)
	}
}

// logThrottle зливає шквал однотипних подій в один рядок на вікно.
//
// Перша подія вікна пишеться одразу (щоб інцидент було видно в ту ж секунду),
// решта накопичується і виводиться одним рядком по таймеру. Саме таймер, а не
// «наступна подія зробить flush»: сплеск на тисячу зʼєднань може вкластися в
// 50 мс і скінчитися — тоді без таймера в лозі лишився б count=1, а решта
// причепилася б до наступного, зовсім іншого інциденту.
type logThrottle struct {
	window time.Duration
	level  slog.Level
	msg    string

	mu    sync.Mutex
	n     int
	attrs []any
	timer *time.Timer
}

func newLogThrottle(window time.Duration, level slog.Level, msg string) *logThrottle {
	return &logThrottle{window: window, level: level, msg: msg}
}

func (t *logThrottle) note(attrs ...any) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.n++
	t.attrs = attrs
	if t.timer != nil { // вікно вже відкрите — доллємо до нього
		return
	}
	t.flushLocked()
	t.timer = time.AfterFunc(t.window, func() {
		t.mu.Lock()
		defer t.mu.Unlock()
		t.timer = nil
		if t.n > 0 {
			t.flushLocked()
		}
	})
}

// stop гасить таймер і виводить залишок вікна. Потрібен на завершенні роботи:
// інакше кілька останніх відмов губляться саме тоді, коли контейнер зупиняють
// посеред інциденту.
func (t *logThrottle) stop() {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.timer != nil {
		t.timer.Stop()
		t.timer = nil
	}
	if t.n > 0 {
		t.flushLocked()
	}
}

func (t *logThrottle) flushLocked() {
	n := t.n
	t.n = 0
	slog.Log(context.Background(), t.level, t.msg, append([]any{"count", n}, t.attrs...)...)
}
