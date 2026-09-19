package auth

import (
	"sync"
	"time"
)

// Throttle — лічильник невдалих входів на роль.
//
// Захищає не токен, а ЧЕРГУ. Токен — 256 біт (`secrets.token_urlsafe(32)`),
// підібрати його не можна за жодної швидкості перевірки. А от кожна перевірка
// коштує 16 МБ і місце в одному з трьох слотів `verifySlots`. Своє `role_name`
// знає кожен студент — воно в нього в рядку підключення. Цикл із власним
// іменем і сміттєвим токеном тримає всі слоти зайнятими, і група не може
// увійти посеред заняття. Виглядає це не як атака, а як «проксі не працює».
//
// Тому ключ — саме `role_name`, а не адреса: студенти сидять за університетським
// NAT, і обмеження за IP поклало б клас цілком.
//
// Замок при цьому НЕ глухий, і це не дрібниця. Перелік ролей студенту видно:
// у службовій базі `postgres` (куди він має CONNECT, щоб робити DROP DATABASE
// власної бази) `pg_roles` лишається читабельним — перевірено, 179 імен `u_*`.
// Тобто глухий замок дав би одному студенту змогу вимкнути іншого, знаючи лише
// його імʼя ролі: один DoS обміняли б на інший. Тому замкнена роль зберігає
// право на ОДНУ перевірку раз на `probe`: справжній студент заходить із другої
// спроби, а потік падає з необмеженого до одного argon2 на кілька секунд.
type Throttle struct {
	mu     sync.Mutex
	max    int           // скільки невдач поспіль дозволено
	window time.Duration // за який час вони мають статися
	block  time.Duration // на скільки замикається роль
	probe  time.Duration // як часто замкнена роль усе ж перевіряється
	seen   map[string]*attempts
	now    func() time.Time // підміняється в тестах
}

type attempts struct {
	fails   int
	since   time.Time
	blocked time.Time
	probeAt time.Time
}

func NewThrottle(max int, window, block time.Duration) *Throttle {
	if max < 1 {
		max = 1
	}
	probe := block / 10
	if probe < time.Second {
		probe = time.Second
	}
	return &Throttle{max: max, window: window, block: block, probe: probe,
		seen: map[string]*attempts{}, now: time.Now}
}

// LoginThrottle — той, який стоїть на шляху Authenticate. Значення за
// замовчуванням перекриваються з конфігурації при старті.
var LoginThrottle = NewThrottle(10, time.Minute, time.Minute)

// Allow каже, чи можна витрачати на цю роль перевірку argon2.
func (t *Throttle) Allow(key string) bool {
	if t == nil {
		return true
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	a := t.seen[key]
	now := t.now()
	if a == nil || !now.Before(a.blocked) {
		return true
	}
	// Замкнено — але шпарина лишається: одна перевірка на probe.
	if now.Before(a.probeAt) {
		return false
	}
	a.probeAt = now.Add(t.probe)
	return true
}

// Fail рахує невдалу спробу і повертає true рівно в той момент, коли роль
// замикається. Саме на цьому переході пишеться подія: якби подія йшла на кожну
// відмову, потік спроб залив би db_events так само, як заливав слоти.
func (t *Throttle) Fail(key string) bool {
	if t == nil {
		return false
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	now := t.now()
	a := t.seen[key]
	if a == nil || now.Sub(a.since) > t.window {
		a = &attempts{since: now}
		t.seen[key] = a
	}
	a.fails++
	if a.fails < t.max {
		return false
	}
	a.fails, a.since = 0, now
	a.blocked, a.probeAt = now.Add(t.block), now.Add(t.probe)
	t.sweep(now)
	return true
}

// Reset викликається після вдалого входу: студент, який згадав свій токен, не
// має доїдати чужий ліміт.
func (t *Throttle) Reset(key string) {
	if t == nil {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	delete(t.seen, key)
}

// Blocked — скільки ролей замкнено зараз. Читає `make health`: сплеск означає
// або атаку, або клієнта, що крутить старий токен у циклі, і обидва варті ока.
func (t *Throttle) Blocked() int {
	t.mu.Lock()
	defer t.mu.Unlock()
	now, n := t.now(), 0
	for _, a := range t.seen {
		if now.Before(a.blocked) {
			n++
		}
	}
	return n
}

// sweep прибирає протухлі записи. Без нього мапа росла б на кожну неіснуючу
// роль, яку хтось назве, — тобто необмежено.
func (t *Throttle) sweep(now time.Time) {
	if len(t.seen) < 1024 {
		return
	}
	for k, a := range t.seen {
		if now.After(a.blocked) && now.Sub(a.since) > t.window {
			delete(t.seen, k)
		}
	}
}
