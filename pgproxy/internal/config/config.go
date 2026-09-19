// Package config читає налаштування pgproxy з оточення і секретів.
package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
)

type Config struct {
	// Два порти, бо PostgreSQL слухає рівно один, а нестандартні порти
	// ріжуть корпоративні мережі. Обидва обслуговує той самий обробник.
	ListenPrimary  string
	ListenFallback string

	BackendHost string
	BackendPort int

	// DSN журналу. Пише лише shipper і лише роллю obs_shipper.
	ObserverDSN string

	TLSCert string
	TLSKey  string

	SpoolPath string

	// Ключ, з якого виводиться пароль ролі студента: HMAC(master, student_id).
	MasterSecret []byte

	// Аварійний режим: чистий TCP-форвардер без розбору протоколу.
	// Журнал при цьому ПОРОЖНІЙ — див. ADR-002.
	Passthrough bool

	// Тверді ліміти, які СУБД накласти не може (ADR-010): statement_timeout і
	// work_mem мають контекст USERSET, студент знімає їх одним SET.
	StatementTimeoutMS int
	BlockedParams      map[string]bool
	MaxBackends        int

	// Розмір пулу до бази observer. Має бути МЕНШИМ за CONNECTION LIMIT ролі
	// obs_shipper, інакше під навантаженням автентифікація починає падати з
	// 53300 «too many connections» — а це виглядає як невірний токен.
	PoolMax int32

	// Скільки перевірок токена виконувати одночасно. Кожна коштує стільки
	// памʼяті, скільки записано в хеші (типово 64 МБ), тож це і є стеля
	// споживання проксі під час напливу підключень.
	VerifyParallel int

	// Обмежувач входу: скільки невдалих спроб на ОДНУ роль допускається за
	// хвилину і на скільки секунд роль після цього замикається. Ключ — роль,
	// а не адреса: група сидить за одним NAT.
	LoginFails    int
	LoginBlockSec int
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func envInt(k string, def int) int {
	if v := os.Getenv(k); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func secret(name string) ([]byte, error) {
	b, err := os.ReadFile("/run/secrets/" + name)
	if err != nil {
		return nil, fmt.Errorf("секрет %s: %w", name, err)
	}
	return []byte(strings.TrimSpace(string(b))), nil
}

func Load() (*Config, error) {
	master, err := secret("master_secret")
	if err != nil {
		return nil, err
	}
	shipperPW, err := secret("pg_shipper_password")
	if err != nil {
		return nil, err
	}

	host := env("BACKEND_HOST", "postgres")
	port := envInt("BACKEND_PORT", 5432)

	blocked := map[string]bool{}
	for _, p := range strings.Split(env("PROXY_BLOCKED_PARAMS",
		"work_mem,statement_timeout,idle_in_transaction_session_timeout,temp_file_limit"), ",") {
		if p = strings.ToLower(strings.TrimSpace(p)); p != "" {
			blocked[p] = true
		}
	}

	return &Config{
		ListenPrimary:  ":" + env("PGPROXY_PORT_PRIMARY", "55432"),
		ListenFallback: ":" + env("PGPROXY_PORT_FALLBACK", "8080"),
		BackendHost:    host,
		BackendPort:    port,
		ObserverDSN: fmt.Sprintf(
			"host=%s port=%d dbname=observer user=obs_shipper password=%s sslmode=disable",
			host, port, string(shipperPW)),
		TLSCert:            env("PGPROXY_TLS_CERT", "/var/lib/pgproxy/server.crt"),
		TLSKey:             env("PGPROXY_TLS_KEY", "/var/lib/pgproxy/server.key"),
		SpoolPath:          env("PGPROXY_SPOOL", "/var/lib/pgproxy/journal.jsonl"),
		MasterSecret:       master,
		Passthrough:        env("PROXY_PASSTHROUGH", "0") == "1",
		StatementTimeoutMS: envInt("PROXY_STATEMENT_TIMEOUT_MS", 30000),
		BlockedParams:      blocked,
		MaxBackends:        envInt("PROXY_MAX_BACKENDS", 100),
		PoolMax:            int32(envInt("PGPROXY_POOL_MAX", 8)),
		VerifyParallel:     envInt("PGPROXY_VERIFY_PARALLEL", 3),
		LoginFails:         envInt("PGPROXY_LOGIN_FAILS", 10),
		LoginBlockSec:      envInt("PGPROXY_LOGIN_BLOCK_SEC", 60),
	}, nil
}
