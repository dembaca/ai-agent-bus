// bridged mirrors the posts of NATS homes into Mattermost with one bot
// (ADR-016 point 4, RT-6.36). NATS stays the source: the bridge reads a
// home's stream, writes each message once, and remembers the id so a restart
// goes on where it stopped. The other way it carries people's posts and
// reactions in those home channels onto NATS (step 2, RT-6.42): it reads its
// bot's Mattermost WebSocket and publishes on <bus>.hu.<home>.<anchor>.
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
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/config"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/inbound"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/links"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mirror"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mm"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mmws"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/presence"
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
	kv, err := store.Open(ctx, nc, cfg.KVBucket)
	if err != nil {
		return err
	}
	seen, err := store.OpenSeen(ctx, nc, cfg.SeenBucket)
	if err != nil {
		return err
	}
	client := mm.New(cfg.MMURL, cfg.MMToken)
	if _, err := client.Me(ctx); err != nil {
		return fmt.Errorf("the bridge bot: %w", err)
	}

	// `shown_as` needs the presence of the names and the home's roster; without
	// presences the bridge runs as before, naming the NATS name.
	snapshot := presence.New()
	h := &health{}
	var wg sync.WaitGroup
	pr := &presence.Reader{Log: log, Deliver: cfg.Deliver + ".presence", Into: snapshot}
	wg.Add(1)
	go func() {
		defer wg.Done()
		if err := pr.Run(ctx, nc); err != nil && ctx.Err() == nil {
			log.Error("bridge: presence stopped", "error", err)
			h.failed.Store(true)
			stop()
		}
	}()
	for _, home := range cfg.Homes {
		home := home
		cid := channelOnce(cfg.MMTeam, home, client)
		lk := &links.Resolver{Presence: snapshot, Roster: roster{client: client, cid: cid},
			Window: 90 * time.Second, TTL: 30 * time.Second}
		r := &reader.Reader{Home: home, Deliver: cfg.Deliver + "." + home, Stream: cfg.NATSStream,
			Log: log, Store: kv, Prefix: cfg.BusPrefix, PropsPrefix: cfg.Prefix,
			Mirror: &mirror.Mirror{Home: home, Prefix: cfg.Prefix, Log: log, Sink: client, Store: kv,
				Links: lk}}
		r.Stuck = h.stuckOn
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := r.Run(ctx, nc, cid); err != nil && ctx.Err() == nil {
				// A consumer that cannot be created or fetched from is fatal for
				// that home (a refused post is not: the reader skips it).
				log.Error("bridge: a home stopped", "home", home, "error", err)
				h.failed.Store(true)
				stop()
			}
		}()
	}

	// People onto NATS (RT-6.42): the account records, the WebSocket and the
	// inbound path. A lost socket or a stopped home does not stop the bridge:
	// the signal runs out and sessions read people on Mattermost (step 9); a
	// subscription to the account records that cannot be made does.
	accounts := &inbound.Accounts{Log: log, Deliver: cfg.Deliver + ".accounts", Stream: "PRESENCE"}
	wg.Add(1)
	go func() {
		defer wg.Done()
		if err := accounts.Run(ctx, nc); err != nil && ctx.Err() == nil {
			log.Error("bridge: the account records stopped", "error", err)
			h.failed.Store(true)
			stop()
		}
	}()
	ws := mmws.New(cfg.MMURL, cfg.MMToken).WithPlainHTTP(cfg.MMAllowHTTP)
	if cfg.MMPlainHTTP {
		log.Warn("bridge: BRIDGE_MM_URL is http to a host that is not loopback (BRIDGE_MM_ALLOW_HTTP);" +
			" the bot token goes to Mattermost in clear text")
	}
	wg.Add(2)
	go func() {
		defer wg.Done()
		_ = ws.Run(ctx)
	}()
	in := &inbound.Bridge{Homes: cfg.Homes, Team: cfg.MMTeam, BusPrefix: cfg.BusPrefix, Log: log,
		MM: client, NATS: inbound.Confirmed{NC: nc}, Store: kv, Seen: seen, Anchors: accounts, Events: ws.Events(), Up: ws.Up}
	go func() {
		defer wg.Done()
		_ = in.Run(ctx)
	}()

	mux := http.NewServeMux()
	mux.Handle("GET /healthz", h)
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
	if h.failed.Load() {
		return errors.New("a home stopped; the bridge exits so the supervisor restarts it")
	}
	return nil
}

// connect is the bridge's own NATS user: it reads the bus and publishes only
// people's events and its signal in its homes (RT-6.36, RT-6.42).
func connect(cfg config.Config) (*nats.Conn, error) {
	// The answers to the bridge's own requests go to its own inbox prefix: with
	// the shared _INBOX.> it would read every answer the login service's user
	// gets — the channel directory's raw values among them (RT-6.36 step 1).
	opts := []nats.Option{nats.Name("bridge"), nats.MaxReconnects(-1),
		nats.ReconnectWait(2 * time.Second), nats.CustomInboxPrefix(cfg.Inbox),
		// A refused publish or subscribe is logged with its subject; the inbound
		// path also sees its own refusals as errors (inbound.Confirmed).
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, err error) {
			slog.Error("bridge: NATS refused", "error", err)
		})}
	seed, err := nats.NkeyOptionFromSeed(cfg.SeedFile)
	if err != nil {
		return nil, fmt.Errorf("BRIDGE_NATS_SEED_FILE: %w", err)
	}
	opts = append(opts, seed)
	if tlsFirst(cfg.NATSURL) {
		// Over tls:// the handshake comes first, as the login service and the
		// hosts do (RT-6.36): the lab server needs handshake_first in its tls
		// block, and a CA file is only needed when the system does not know it.
		tcfg := &tls.Config{MinVersion: tls.VersionTLS12}
		if cfg.CAFile != "" {
			pool := x509.NewCertPool()
			pem, err := os.ReadFile(cfg.CAFile)
			if err != nil {
				return nil, fmt.Errorf("BRIDGE_CA_FILE: %w", err)
			}
			if !pool.AppendCertsFromPEM(pem) {
				return nil, fmt.Errorf("BRIDGE_CA_FILE %s holds no certificate", cfg.CAFile)
			}
			tcfg.RootCAs = pool
		}
		opts = append(opts, nats.Secure(tcfg), nats.TLSHandshakeFirst())
	}
	return nats.Connect(cfg.NATSURL, opts...)
}

// tlsFirst is true when every URL of the list is tls:// (one pool, one scheme).
func tlsFirst(urls string) bool {
	for _, raw := range strings.Split(urls, ",") {
		u := strings.TrimSpace(raw)
		if !strings.HasPrefix(strings.ToLower(u), "tls://") {
			return false
		}
	}
	return urls != ""
}

// roster is the home's member list for the links resolver.
type roster struct {
	client *mm.Client
	cid    func(context.Context) (string, error)
}

// Members implements links.Roster.
func (r roster) Members(ctx context.Context) ([]links.Member, error) {
	cid, err := r.cid(ctx)
	if err != nil {
		return nil, err
	}
	users, err := r.client.Members(ctx, cid)
	if err != nil {
		return nil, err
	}
	out := make([]links.Member, 0, len(users))
	for _, u := range users {
		out = append(out, links.Member{ID: u.ID, Username: u.Username, Status: u.Status})
	}
	return out, nil
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
