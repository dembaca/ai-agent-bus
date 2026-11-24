// bridged mirrors the posts of NATS homes into Mattermost with one bot, one
// way (ADR-016 point 4, RT-6.36). NATS stays the source: the bridge reads a
// home's stream, writes each message once, and remembers the id so a restart
// goes on where it stopped.
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/config"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mirror"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mm"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/reader"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/store"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	if err := run(log); err != nil {
		log.Error("bridge: stopped", "error", err)
		os.Exit(1)
	}
}

func run(log *slog.Logger) error {
	cfg, err := config.FromEnv()
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	nc, err := connect(cfg)
	if err != nil {
		return err
	}
	defer nc.Close()
	js, err := nc.JetStream()
	if err != nil {
		return fmt.Errorf("jetstream: %w", err)
	}
	kv, err := store.Open(ctx, nc, cfg.KVBucket)
	if err != nil {
		return err
	}
	client := mm.New(cfg.MMURL, cfg.MMToken)
	if _, err := client.Me(ctx); err != nil {
		return fmt.Errorf("the bridge bot: %w", err)
	}

	var wg sync.WaitGroup
	for _, home := range cfg.Homes {
		home := home
		cid := channelOnce(cfg.MMTeam, home, client)
		r := &reader.Reader{Home: home, Log: log, Store: kv, Stream: cfg.NATSStream, Prefix: "bus",
			Mirror: &mirror.Mirror{Home: home, Prefix: cfg.Prefix, Log: log, Sink: client, Store: kv,
				Links: mirror.LinksNone{}}}
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := r.Run(ctx, js, cid); err != nil && ctx.Err() == nil {
				log.Error("bridge: a home stopped", "home", home, "error", err)
				stop()
			}
		}()
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	})
	srv := &http.Server{Addr: cfg.Listen, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("bridge: healthz", "error", err)
		}
	}()
	log.Info("bridge: up", "homes", cfg.Homes, "listen", cfg.Listen)

	<-ctx.Done()
	_ = srv.Shutdown(context.Background())
	wg.Wait()
	return nil
}

// connect is the bridge's own NATS user: read-only on the bus (RT-6.36).
func connect(cfg config.Config) (*nats.Conn, error) {
	opts := []nats.Option{nats.Name("bridge"), nats.MaxReconnects(-1),
		nats.ReconnectWait(2 * time.Second)}
	seed, err := nats.NkeyOptionFromSeed(cfg.SeedFile)
	if err != nil {
		return nil, fmt.Errorf("BRIDGE_NATS_SEED_FILE: %w", err)
	}
	opts = append(opts, seed)
	if cfg.CAFile != "" {
		pool := x509.NewCertPool()
		pem, err := os.ReadFile(cfg.CAFile)
		if err != nil {
			return nil, fmt.Errorf("BRIDGE_CA_FILE: %w", err)
		}
		if !pool.AppendCertsFromPEM(pem) {
			return nil, fmt.Errorf("BRIDGE_CA_FILE %s holds no certificate", cfg.CAFile)
		}
		opts = append(opts, nats.Secure(&tls.Config{MinVersion: tls.VersionTLS12, RootCAs: pool}),
			nats.TLSHandshakeFirst())
	}
	return nats.Connect(cfg.NATSURL, opts...)
}

// channelOnce resolves a home's Mattermost channel id on first use.
func channelOnce(team, name string, client *mm.Client) func(context.Context) (string, error) {
	var (
		mu  sync.Mutex
		id  string
		err error
		had bool
	)
	return func(ctx context.Context) (string, error) {
		mu.Lock()
		defer mu.Unlock()
		if had {
			return id, err
		}
		id, err = client.ChannelIDByName(ctx, team, name)
		had = err == nil
		return id, err
	}
}
