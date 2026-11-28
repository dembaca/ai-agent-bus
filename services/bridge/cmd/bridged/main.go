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
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/agentbots"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/commands"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/config"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/inbound"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/links"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mirror"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mm"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mmws"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/presence"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/reader"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/registrar/core"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/registrar/mattermost"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/release"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/requests"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/signing"
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
	cfg, err := config.FromEnv(log.Warn)
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
	// The account records map a person's account to the anchor (RT-6.42) and,
	// the other way round, an agent bot's owner to the account (RT-6.44).
	accounts := &inbound.Accounts{Log: log, Deliver: cfg.Deliver + ".accounts", Stream: "PRESENCE"}
	bots, err := agentBots(ctx, cfg, snapshot, accounts, nc, log)
	if err != nil {
		return err
	}
	h := &health{}
	var wg sync.WaitGroup
	// Plan B's switches per channel, as the login service writes them from the
	// channel directory (RT-6.39 step 5), through the bridge's own consumer
	// bridge_planb made at every start: the mirror reads piece 6 (asks, RT-6.11),
	// and piece 4 decides how a person's post binds (inbound.ProofPiece, #266).
	planb := &presence.Switches{}
	// Plan B piece 9 (piece9.go): the agents' bots show their status, and the
	// crown holder's header line is written with its own bot; nil without
	// the agents' own bots.
	p9 := newPiece9(cfg, log, client, bots, snapshot, planb)
	// The request scaffold (ADR-014 phase 5): requests on <req>.<kind>.<agent>
	// get this phase's "not built" answer on deliver.<agent>.req.<request id>.
	// A lost message fails the request; the handler retries a refused
	// subscription once a minute and only reports why it is not answering, so
	// an image rolled out before the lab's NATS rights leaves requests
	// unanswered and never stops the bridge; /healthz names the failure.
	if cfg.ReqPrefix != "" {
		rq := &requests.Handler{Log: log, Prefix: cfg.ReqPrefix, Handlers: p9.handlers()}
		h.requests = rq
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := rq.Run(ctx, nc); err != nil && ctx.Err() == nil {
				log.Error("bridge: the request scaffold stopped", "error", err)
			}
		}()
	}
	pr := &presence.Reader{Log: log, Deliver: cfg.Deliver + ".presence", Into: snapshot}
	pbr := &presence.PlanBReader{Log: log, Deliver: cfg.Deliver + ".planb", Stream: "PRESENCE", Into: planb}
	pr.Shown = p9.shownStore()
	wg.Add(1)
	go func() {
		defer wg.Done()
		if err := pbr.Run(ctx, nc); err != nil && ctx.Err() == nil {
			log.Error("bridge: plan B switches stopped", "error", err)
		}
	}()
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
		mr := &mirror.Mirror{Home: home, Prefix: cfg.Prefix, Log: log, Sink: client, Store: kv, Links: lk,
			Switches: planb, People: client, Directory: accounts}
		if bots != nil {
			mr.Bots = bots
		}
		r := &reader.Reader{Home: home, Deliver: cfg.Deliver + "." + home, Stream: cfg.NATSStream,
			Log: log, Store: kv, Prefix: cfg.BusPrefix, PropsPrefix: cfg.Prefix, Mirror: mr}
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
		log.Warn("bridge: CB_BRIDGE_MM_URL is http to a host that is not loopback (CB_BRIDGE_MM_ALLOW_HTTP);" +
			" the bot token goes to Mattermost in clear text")
	}
	wg.Add(2)
	go func() {
		defer wg.Done()
		_ = ws.Run(ctx)
	}()
	in := &inbound.Bridge{Homes: cfg.Homes, Team: cfg.MMTeam, BusPrefix: cfg.BusPrefix, Log: log,
		MM: client, NATS: inbound.Confirmed{NC: nc}, Store: kv, Seen: seen, Anchors: accounts, Events: ws.Events(), Up: ws.Up}
	// In a home with plan B piece 4 on, a person's post binds by the proof of
	// the account's binding, which only a system admin's token can compare
	// (auth_service, auth_data; RT-6.42 step 3, #266): the admin bot's. Without
	// it, every person of such a home goes out under "-" (one warning when the
	// bridge first meets one); with piece 4 off, the record alone binds. The
	// signal names piece 6 while it is on (RT-6.11 step 7).
	in.Switches = planb
	if cfg.AdminToken != "" {
		in.Users = mm.New(cfg.MMURL, cfg.AdminToken)
	}
	if bots != nil {
		in.BotPrefix = cfg.BotPrefix
	}
	go func() {
		defer wg.Done()
		_ = in.Run(ctx)
	}()

	// The sweep of the agents' idle bots (RT-6.44 step 8): in memory, every
	// bot counts as seen at this start.
	if bots != nil {
		sw := &agentbots.Sweeper{Bots: bots, Keep: cfg.BotKeep, Start: time.Now()}
		wg.Add(1)
		go func() {
			defer wg.Done()
			sw.Run(ctx, agentbots.SweepInterval)
		}()
		// the bot records (RT-6.44 step 3): at the start and every minute,
		// valid for three, each bot that holds a client, and by the find-only
		// pass the existing bot of each present agent that holds none
		wg.Add(1)
		go func() {
			defer wg.Done()
			bots.RunRecords(ctx, agentbots.RecordEvery)
		}()
	}
	p9.run(ctx, &wg, nc)

	mux := http.NewServeMux()
	mux.Handle("GET /healthz", h)

	// The slash commands and their signed records (issue #151 step 3) and the
	// release record (step 4): the bridge's own key, the operator's records
	// bucket, and a listener of their own for the commands. Off while
	// CB_BRIDGE_COMMANDS_LISTEN_ADDR is empty and no release is configured.
	started := time.Now()
	var att *core.Attestor
	if cfg.CommandsListen != "" || cfg.ReleaseSignersFile != "" || cfg.ReleaseVersion != "" {
		var err error
		if att, err = signing.Open(cfg.SigningKeyFile); err != nil {
			return err
		}
	}
	var cmds *commands.Server
	if cfg.CommandsListen != "" {
		records, err := store.OpenRecords(ctx, nc, cfg.RecordsBucket)
		if err != nil {
			return err
		}
		cmds, err = commands.New(commands.Config{
			MMURL: cfg.MMURL, MMTeam: cfg.MMTeam, AdminToken: cfg.AdminToken, AllowHTTP: cfg.MMAllowHTTP,
			Attestor: att, Persister: records, ModeTokens: cfg.ModeCommandTokens,
			MandateTokens: cfg.MandateCommandTokens, DebugTokens: cfg.DebugCommandTokens,
			TrustedProxies: cfg.TrustedProxies, Log: log,
		})
		if err != nil {
			return err
		}
		cmds.PruneDebug(ctx)
		cmds.Reads(mux)
		log.Info("bridge: slash commands on", "addr", cfg.CommandsListen, "kid", att.Kid(),
			"mode", len(cfg.ModeCommandTokens) > 0, "mandate", len(cfg.MandateCommandTokens) > 0,
			"debug", len(cfg.DebugCommandTokens) > 0)
		if cfg.CommandsPlainHTTP {
			log.Warn("bridge: CB_BRIDGE_COMMANDS_LISTEN_ADDR is not loopback and has no TLS " +
				"(CB_BRIDGE_COMMANDS_ALLOW_HTTP); the command tokens and the records go in clear text")
		}
	}
	// The release record (RT-6.32, #151 step 4): the registrar's route is always
	// answered, with the registrar's exact 404 while no release is set — a host
	// treats it as normal, and an absent route would count as a failed read. The
	// record itself is signed with the same key.
	var rel http.Handler = http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		_, _ = io.WriteString(w, `{"error":"no release set"}`)
	})
	if cfg.ReleaseSignersFile != "" || cfg.ReleaseVersion != "" {
		var text string
		if cfg.ReleaseSignersFile != "" {
			var err error
			if text, err = release.SignersText(cfg.ReleaseSignersFile); err != nil {
				return fmt.Errorf("CB_BRIDGE_RELEASE_SIGNERS_FILE: %w", err)
			}
		}
		h, err := release.New(text, cfg.ReleaseVersion, att, started)
		if err != nil {
			return err
		}
		rel = h
		log.Info("bridge: the release record is served", "version", cfg.ReleaseVersion,
			"signers", cfg.ReleaseSignersFile != "", "kid", att.Kid())
	}
	gated, err := commands.ReadLimit(rel, cfg.TrustedProxies)
	if err != nil {
		return err
	}
	mux.Handle("GET /register/v1/release", gated)
	mainLn, err := net.Listen("tcp", cfg.Listen)
	if err != nil {
		return fmt.Errorf("CB_BRIDGE_LISTEN_ADDR: %w", err)
	}
	srv := &http.Server{Handler: mux, ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout: 30 * time.Second, IdleTimeout: 120 * time.Second}
	go func() {
		if err := srv.Serve(mainLn); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("bridge: healthz", "error", err)
		}
	}()
	var cmdSrv *http.Server
	if cmds != nil {
		cmdLn, err := net.Listen("tcp", cfg.CommandsListen)
		if err != nil {
			return fmt.Errorf("CB_BRIDGE_COMMANDS_LISTEN_ADDR: %w", err)
		}
		if cfg.CommandsTLSCertFile != "" {
			tcfg, err := commands.TLSConfig(cfg.CommandsTLSCertFile, cfg.CommandsTLSKeyFile)
			if err != nil {
				return err
			}
			cmdLn = tls.NewListener(cmdLn, tcfg)
		}
		cmdSrv = &http.Server{Handler: cmds.Commands(), ReadHeaderTimeout: 5 * time.Second,
			ReadTimeout: 30 * time.Second, IdleTimeout: 120 * time.Second}
		go func() {
			if err := cmdSrv.Serve(cmdLn); err != nil && !errors.Is(err, http.ErrServerClosed) {
				log.Error("bridge: the slash commands stopped", "error", err)
			}
		}()
	}
	log.Info("bridge: up", "homes", cfg.Homes, "listen", cfg.Listen)

	<-ctx.Done()
	_ = srv.Shutdown(context.Background())
	if cmdSrv != nil {
		_ = cmdSrv.Shutdown(context.Background())
	}
	wg.Wait()
	if h.failed.Load() {
		return errors.New("a home stopped; the bridge exits so the supervisor restarts it")
	}
	return nil
}

// agentBots is the agents' own bots (RT-6.44) when CB_BRIDGE_MM_ADMIN_TOKEN_FILE
// is set, after the start checks; nil keeps the one bridge bot.
func agentBots(ctx context.Context, cfg config.Config, snapshot *presence.Snapshot,
	accounts *inbound.Accounts, nc *nats.Conn, log *slog.Logger) (*agentbots.Bots, error) {
	if cfg.AdminToken == "" {
		return nil, nil
	}
	adm, err := mattermost.New(mattermost.Config{URL: cfg.MMURL, AllowHTTP: cfg.MMAllowHTTP,
		Token: cfg.AdminToken, Team: cfg.MMTeam, HideBotDM: cfg.HideBotDM,
		TokenDescription: agentbots.TokenDescription}, log)
	if err != nil {
		return nil, err
	}
	bots := &agentbots.Bots{Admin: adm, Presence: snapshot, Owners: accounts, Prefix: cfg.BotPrefix, URL: cfg.MMURL, Log: log,
		NATS: inbound.Confirmed{NC: nc}}
	if err := bots.Check(ctx, cfg.MMToken); err != nil {
		return nil, err
	}
	log.Info("bridge: the agents' copies go under their own bots", "prefix", cfg.BotPrefix, "keep", cfg.BotKeep.String())
	return bots, nil
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
		return nil, fmt.Errorf("CB_BRIDGE_NATS_SEED_FILE: %w", err)
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
				return nil, fmt.Errorf("CB_BRIDGE_CA_FILE: %w", err)
			}
			if !pool.AppendCertsFromPEM(pem) {
				return nil, fmt.Errorf("CB_BRIDGE_CA_FILE %s holds no certificate", cfg.CAFile)
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
