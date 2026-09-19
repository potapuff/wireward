package auth

import (
	"testing"
	"time"
)

func fixed(t0 *time.Time) func() time.Time { return func() time.Time { return *t0 } }

func TestThrottleBlocksAfterMaxFails(t *testing.T) {
	now := time.Now()
	th := NewThrottle(3, time.Minute, time.Minute)
	th.now = fixed(&now)

	for i := 0; i < 2; i++ {
		if tripped := th.Fail("u_abc"); tripped {
			t.Fatalf("замкнулося на %d-й невдачі, а дозволено 3", i+1)
		}
		if !th.Allow("u_abc") {
			t.Fatal("роль замкнено раніше ліміту")
		}
	}
	if !th.Fail("u_abc") {
		t.Fatal("третя невдача мала замкнути роль")
	}
	if th.Allow("u_abc") {
		t.Fatal("після замикання перевірка argon2 не має відбуватися")
	}
	if th.Blocked() != 1 {
		t.Fatalf("Blocked()=%d, очікувалося 1", th.Blocked())
	}

	// Замок відпускає сам, без втручання: студент, який переплутав токен,
	// не має чекати на викладача.
	now = now.Add(61 * time.Second)
	if !th.Allow("u_abc") {
		t.Fatal("замок не відпустив після block")
	}
}

// Замок не має бути глухим: перелік ролей студенту видно в базі postgres, тож
// глухий замок дав би змогу вимкнути однокурсника, знаючи лише його імʼя ролі.
func TestThrottleLeavesAProbeWindow(t *testing.T) {
	now := time.Now()
	th := NewThrottle(2, time.Minute, 60*time.Second) // probe = 6 c
	th.now = fixed(&now)

	th.Fail("u_abc")
	th.Fail("u_abc")
	if th.Allow("u_abc") {
		t.Fatal("одразу після замикання перевірок бути не має")
	}

	now = now.Add(7 * time.Second)
	if !th.Allow("u_abc") {
		t.Fatal("замкнена роль має отримувати одну спробу на probe")
	}
	// І рівно одну: друга поспіль знову впирається.
	if th.Allow("u_abc") {
		t.Fatal("шпарина пропустила більше однієї перевірки")
	}
}

func TestThrottleIsPerRole(t *testing.T) {
	now := time.Now()
	th := NewThrottle(2, time.Minute, time.Minute)
	th.now = fixed(&now)

	th.Fail("u_one")
	th.Fail("u_one")
	if th.Allow("u_one") {
		t.Fatal("перша роль мала замкнутися")
	}
	// Головне в усій вправі: сусід по NAT входить далі.
	if !th.Allow("u_two") {
		t.Fatal("замкнено чужу роль — це і є пастка з обмеженням за адресою")
	}
}

func TestThrottleWindowExpires(t *testing.T) {
	now := time.Now()
	th := NewThrottle(3, time.Minute, time.Minute)
	th.now = fixed(&now)

	th.Fail("u_abc")
	th.Fail("u_abc")
	// Дві помилки за півгодини — це не атака, а звичайна людина.
	now = now.Add(30 * time.Minute)
	if tripped := th.Fail("u_abc"); tripped {
		t.Fatal("невдачі за межами вікна не мають сумуватися")
	}
	if !th.Allow("u_abc") {
		t.Fatal("роль замкнено на розрізнених помилках")
	}
}

func TestThrottleResetOnSuccess(t *testing.T) {
	now := time.Now()
	th := NewThrottle(3, time.Minute, time.Minute)
	th.now = fixed(&now)

	th.Fail("u_abc")
	th.Fail("u_abc")
	th.Reset("u_abc")
	if tripped := th.Fail("u_abc"); tripped {
		t.Fatal("після вдалого входу лічильник має починатися з нуля")
	}
}

func TestThrottleNilIsPermissive(t *testing.T) {
	var th *Throttle
	if !th.Allow("u_abc") || th.Fail("u_abc") {
		t.Fatal("невимкнений обмежувач не має ламати вхід")
	}
}
