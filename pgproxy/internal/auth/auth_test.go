package auth

// Тести виведення пароля ролі.
//
// Пароль ніде не зберігається: проксі щоразу виводить його з master_secret і
// student_id. Та сама формула продубльована в front/app/credentials.py
// (role_password). Якщо реалізації розійдуться байт у байт — проксі просто не
// увійде до СУБД, тому тут є і золотий вектор, звірений із Python-версією.

import (
	"strings"
	"testing"
)

func TestRolePasswordЗолотийВектор(t *testing.T) {
	// hmac-sha256(master, student_id) → base64url без «=».
	// Звірено з: printf '<id>' | openssl dgst -sha256 -hmac '<master>' -binary
	//            | base64 | tr '+/' '-_' | tr -d '='
	// Та сама послідовність, що й у front/app/credentials.py:role_password.
	const (
		master    = "master-secret"
		studentID = "11111111-2222-3333-4444-555555555555"
		want      = "8ejIZWgbjD6m_NUz96RvGsvauokrkNEoaovaeZKegDs"
	)
	if got := RolePassword([]byte(master), studentID); got != want {
		t.Errorf("RolePassword = %q, очікували %q", got, want)
	}
}

func TestRolePasswordДетермінованість(t *testing.T) {
	master := []byte("master-secret")
	const id = "11111111-2222-3333-4444-555555555555"
	first := RolePassword(master, id)
	for i := 0; i < 100; i++ {
		if got := RolePassword(master, id); got != first {
			t.Fatalf("виклик %d дав інший пароль: %q проти %q", i, got, first)
		}
	}
}

func TestRolePasswordРізніСтуденти(t *testing.T) {
	master := []byte("master-secret")
	ids := []string{
		"11111111-2222-3333-4444-555555555555",
		"11111111-2222-3333-4444-555555555556",
		"00000000-0000-0000-0000-000000000000",
		"",
		"не-uuid, а просто рядок",
	}
	seen := map[string]string{}
	for _, id := range ids {
		pw := RolePassword(master, id)
		if prev, dup := seen[pw]; dup {
			t.Errorf("студенти %q і %q отримали однаковий пароль", prev, id)
		}
		seen[pw] = id
	}
}

// Ротація master_secret має перевидати геть усі паролі — на цьому тримається
// відкликання доступу.
func TestRolePasswordРотаціяМайстраЗмінюєПароль(t *testing.T) {
	const id = "11111111-2222-3333-4444-555555555555"
	a := RolePassword([]byte("master-secret"), id)
	b := RolePassword([]byte("master-secret-2"), id)
	c := RolePassword([]byte(""), id)
	if a == b || a == c || b == c {
		t.Errorf("різні master_secret дали однаковий пароль: %q / %q / %q", a, b, c)
	}
}

// Пароль іде в SCRAM і в SQL-провізіонінг, тож він має бути придатним для
// передавання як є: 43 символи base64url, без «=» і без спецсимволів.
func TestRolePasswordФормат(t *testing.T) {
	const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
	for _, id := range []string{
		"11111111-2222-3333-4444-555555555555",
		"00000000-0000-0000-0000-000000000000",
		"ffffffff-ffff-ffff-ffff-ffffffffffff",
	} {
		pw := RolePassword([]byte("master-secret"), id)
		if len(pw) != 43 { // 32 байти → 43 символи base64 без вирівнювання
			t.Errorf("довжина пароля %d, очікували 43: %q", len(pw), pw)
		}
		if strings.Contains(pw, "=") {
			t.Errorf("у паролі лишилося вирівнювання: %q", pw)
		}
		for _, r := range pw {
			if !strings.ContainsRune(alphabet, r) {
				t.Errorf("недопустимий символ %q у паролі %q", r, pw)
			}
		}
	}
}

func TestQuotaExceeded(t *testing.T) {
	tests := []struct {
		name string
		q    Quota
		want bool
	}{
		{"ліміт не досягнуто", Quota{Used: 10, Limit: 100}, false},
		{"рівно на межі — вичерпано", Quota{Used: 100, Limit: 100}, true},
		{"понад ліміт", Quota{Used: 101, Limit: 100}, true},
		{"ліміт нуль означає без обмеження", Quota{Used: 1 << 40, Limit: 0}, false},
		{"відʼємний ліміт теж без обмеження", Quota{Used: 10, Limit: -1}, false},
		{"порожня квота", Quota{}, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := tc.q.Exceeded(); got != tc.want {
				t.Errorf("Quota%+v.Exceeded() = %v, очікували %v", tc.q, got, tc.want)
			}
		})
	}
}

func TestSetVerifyParallelism(t *testing.T) {
	// Черга перевірок argon2 обмежує пікову памʼять; нуль і відʼємне значення
	// не мають давати канал нульової місткості (це заблокувало б вхід назавжди).
	defer SetVerifyParallelism(3) // повертаємо типове значення для решти тестів
	for _, n := range []int{-1, 0, 1} {
		SetVerifyParallelism(n)
		if got := cap(verifySlots); got != 1 {
			t.Errorf("SetVerifyParallelism(%d) дав місткість %d, очікували 1", n, got)
		}
	}
	SetVerifyParallelism(8)
	if got := cap(verifySlots); got != 8 {
		t.Errorf("SetVerifyParallelism(8) дав місткість %d", got)
	}
}
