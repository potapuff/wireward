// pgproxy — проксі протоколу PostgreSQL, який веде журнал усіх запитів
// студента, включно з тими, що впали на синтаксисі.
//
// Чому не pgAudit: він пише на старті виконання і не бачить запитів, що не
// дійшли до виконавця, а також не дає ні тексту помилки, ні тривалості, ні
// кількості рядків в одному записі. Докладно — docs/02-decisions.md, ADR-001.
package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"errors"
	"io"
	"log/slog"
	"math/big"
	"net"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"wireward/pgproxy/internal/auth"
	"wireward/pgproxy/internal/config"
	"wireward/pgproxy/internal/journal"
	"wireward/pgproxy/internal/proxy"
)

func main() {
	level := slog.LevelInfo
	if os.Getenv("PGPROXY_DEBUG") == "1" {
		level = slog.LevelDebug
	}
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: level})))

	cfg, err := config.Load()
	if err != nil {
		slog.Error("конфігурація", "err", err)
		os.Exit(1)
	}

	auth.SetVerifyParallelism(cfg.VerifyParallel)
	auth.LoginThrottle = auth.NewThrottle(cfg.LoginFails, time.Minute,
		time.Duration(cfg.LoginBlockSec)*time.Second)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	if cfg.Passthrough {
		// Аварійний режим. Журнал при цьому ПОРОЖНІЙ, і тверді ліміти
		// (ADR-010) теж не діють. Кричимо про це в лог кожні п'ять хвилин,
		// щоб режим не залишився ввімкненим непоміченим.
		slog.Warn("PROXY_PASSTHROUGH=1 — журнал НЕ ведеться, обмеження не діють")
		go func() {
			t := time.NewTicker(5 * time.Minute)
			for range t.C {
				slog.Warn("PROXY_PASSTHROUGH усе ще ввімкнено — журнал порожній")
			}
		}()
		runPassthrough(ctx, cfg)
		return
	}

	poolCfg, err := pgxpool.ParseConfig(cfg.ObserverDSN)
	if err != nil {
		slog.Error("DSN observer", "err", err)
		os.Exit(1)
	}
	// Явно, а не за замовчуванням: типовий розмір пулу залежить від кількості
	// ядер і легко перевищує CONNECTION LIMIT ролі obs_shipper.
	poolCfg.MaxConns = cfg.PoolMax
	poolCfg.MinConns = 1
	pool, err := pgxpool.NewWithConfig(ctx, poolCfg)
	if err != nil {
		slog.Error("пул observer", "err", err)
		os.Exit(1)
	}
	defer pool.Close()
	if err := waitForDB(ctx, pool); err != nil {
		slog.Error("база недоступна", "err", err)
		os.Exit(1)
	}

	tlsCfg, err := loadOrCreateTLS(cfg.TLSCert, cfg.TLSKey)
	if err != nil {
		slog.Error("TLS", "err", err)
		os.Exit(1)
	}

	jw, err := journal.NewWriter(cfg.SpoolPath)
	if err != nil {
		slog.Error("spool", "err", err)
		os.Exit(1)
	}
	defer jw.Close()

	go journal.NewShipper(pool, cfg.SpoolPath).Run(ctx)

	srv := proxy.New(cfg, pool, jw, tlsCfg)
	go func() {
		if err := srv.Listen(ctx, cfg.ListenPrimary); err != nil {
			slog.Error("listen", "addr", cfg.ListenPrimary, "err", err)
		}
	}()
	// Другий порт існує тому, що нестандартні порти ріжуть корпоративні мережі.
	if err := srv.Listen(ctx, cfg.ListenFallback); err != nil {
		slog.Error("listen", "addr", cfg.ListenFallback, "err", err)
	}
}

func waitForDB(ctx context.Context, pool *pgxpool.Pool) error {
	var last error
	for i := 0; i < 30; i++ {
		if err := pool.Ping(ctx); err == nil {
			return nil
		} else {
			last = err
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(2 * time.Second):
		}
	}
	return last
}

// runPassthrough — чистий TCP-форвардер без розбору протоколу.
func runPassthrough(ctx context.Context, cfg *config.Config) {
	fwd := func(addr string) {
		ln, err := net.Listen("tcp", addr)
		if err != nil {
			slog.Error("listen", "addr", addr, "err", err)
			return
		}
		slog.Info("слухаємо (passthrough)", "addr", addr)
		go func() {
			<-ctx.Done()
			_ = ln.Close()
		}()
		// Той самий цикл із backoff, що й на основному шляху. Тут це навіть
		// важливіше: до правки будь-яка помилка Accept (зокрема EMFILE)
		// мовчки виходила з циклу — слухач помирав назавжди, без рядка в лозі
		// й без падіння процесу, а контейнер лишався «здоровим». А в
		// passthrough немає ні стелі рукостискань, ні стелі бекендів, і кожен
		// клієнт — це два дескриптори, тож упертися в nofile тут найлегше.
		_ = proxy.AcceptLoop(ctx, ln, func(c net.Conn) {
			go func() {
				defer c.Close()
				b, err := net.Dial("tcp", cfg.BackendHost+":"+itoa(cfg.BackendPort))
				if err != nil {
					return
				}
				defer b.Close()
				go func() { _, _ = io.Copy(b, c) }()
				_, _ = io.Copy(c, b)
			}()
		})
	}
	go fwd(cfg.ListenPrimary)
	fwd(cfg.ListenFallback)
	<-ctx.Done()
}

func itoa(i int) string { return big.NewInt(int64(i)).String() }

// loadOrCreateTLS: у продакшені сертифікат кладе адміністратор, у розробці
// генеруємо самопідписаний, інакше проксі не підніметься взагалі (TLS у нас
// обовʼязковий). Студентам у такому разі потрібен sslmode=require, а не
// verify-full — це і є різниця між стендом і сервером.
func loadOrCreateTLS(certPath, keyPath string) (*tls.Config, error) {
	if _, err := os.Stat(certPath); err == nil {
		cert, err := tls.LoadX509KeyPair(certPath, keyPath)
		if err != nil {
			return nil, err
		}
		return &tls.Config{Certificates: []tls.Certificate{cert}, MinVersion: tls.VersionTLS12}, nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}

	slog.Warn("сертифіката немає — генерую самопідписаний", "path", certPath)
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, err
	}
	serial, _ := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	tmpl := x509.Certificate{
		SerialNumber:          serial,
		Subject:               pkix.Name{CommonName: "observer-pgproxy"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().AddDate(2, 0, 0),
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageKeyEncipherment,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		BasicConstraintsValid: true,
		DNSNames:              []string{"localhost", "observer-pgproxy"},
		IPAddresses:           []net.IP{net.ParseIP("127.0.0.1")},
	}
	der, err := x509.CreateCertificate(rand.Reader, &tmpl, &tmpl, &key.PublicKey, key)
	if err != nil {
		return nil, err
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	kb, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		return nil, err
	}
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: kb})
	if err := os.WriteFile(certPath, certPEM, 0o600); err != nil {
		return nil, err
	}
	if err := os.WriteFile(keyPath, keyPEM, 0o600); err != nil {
		return nil, err
	}
	cert, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		return nil, err
	}
	return &tls.Config{Certificates: []tls.Certificate{cert}, MinVersion: tls.VersionTLS12}, nil
}
