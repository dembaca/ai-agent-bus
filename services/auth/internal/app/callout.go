// Package app wires the auth callout from the environment: the token check
// against Keycloak, the Mattermost directory, the keys from files and the
// connection to NATS.
package app

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
	"github.com/nats-io/nkeys"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/admin"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/callout"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/consumers"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/directory"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/envname"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/mattermost"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/theft"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

// ErrConfig marks a configuration the service refuses to start with.
var ErrConfig = errors.New("configuration")

// bucketRE: what NATS allows as a key-value bucket's name.
var bucketRE = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)

// Settings is the callout's configuration from the environment. Secrets are
// file paths, never values.
type Settings struct {
	NATSURL        string
	NATSSeedFile   string // the service's own NATS user (an auth_user)
	IssuerSeedFile string // the account key that signs responses and user JWTs
	XKeySeedFile   string // decrypts requests; required unless PlainRequests
	PlainRequests  bool   // CB_AUTH_PLAIN_REQUESTS=allow: the server sends requests unencrypted
	NATSCAFile     string // CA for the server's TLS certificate; empty: the system's
	NATSCertFile   string // client certificate, with NATSKeyFile, if the server asks for one
	NATSKeyFile    string
	BrakeFile      string
	// Consumer management, required: the user nkey seed of the service's
	// user in the tenant account, with JetStream API rights, and the server
	// it connects to (default NATSURL). Credentials carry the deliver
	// subjects; without consumer management nobody reconciles or brakes them.
	JSSeedFile     string
	JSURL          string
	Callout        callout.Config
	Token          token.Config
	Binding        string // "open" or "openid-only"
	MattermostURL  string
	MattermostTeam string
	MattermostFile string // the Mattermost access token
	// DirectoryBucket is the channel directory's key-value bucket (ADR-016),
	// in the tenant account; empty: directory.DefaultBucket.
	DirectoryBucket string
	// DirectoryImported: the operator ran the one-time import
	// (CB_AUTH_DIRECTORY_IMPORTED=true). The directory then must hold its mark:
	// a missing bucket or mark refuses, never falls back to Mattermost.
	DirectoryImported bool
	// AdminListenAddr is where the directory's admin API listens
	// (CB_AUTH_ADMIN_LISTEN_ADDR); empty: no admin API. Without a certificate
	// it must be a loopback address.
	AdminListenAddr  string
	AdminTLSCertFile string
	AdminTLSKeyFile  string
	// DirectoryAdminGroup is the token group that makes a directory admin
	// (CB_AUTH_DIRECTORY_ADMIN_GROUP); required with AdminListenAddr.
	DirectoryAdminGroup string
	// StatsChannel is the stats channel the STATS import reads (RT-6.45,
	// CB_MM_STATS_CHANNEL as the MCP server reads it: default agents-stats,
	// off, none, 0, false or empty: no import).
	StatsChannel string
}

// LoadSettings reads the callout's settings, by their CB_AUTH_ names with the
// old AUTH_ names as a fallback (envname, docs/env-names.md). It reports false
// when CB_AUTH_NATS_URL is not set: the service then runs without the callout.
// Two names of one setting with different values refuse, with or without it.
func LoadSettings(env *envname.Env) (Settings, bool, error) {
	if err := env.Check(envname.Service()...); err != nil {
		return Settings{}, true, fmt.Errorf("%w: %v", ErrConfig, err)
	}
	// Check above refused every disagreement, so Value reads each name right.
	get := func(k string) string { return strings.TrimSpace(env.Value(k)) }
	s := Settings{NATSURL: get("CB_AUTH_NATS_URL")}
	if s.NATSURL == "" {
		return Settings{}, false, nil
	}
	s.NATSSeedFile = get("CB_AUTH_NATS_SEED_FILE")
	s.IssuerSeedFile = get("CB_AUTH_ISSUER_SEED_FILE")
	s.XKeySeedFile = get("CB_AUTH_XKEY_SEED_FILE")
	s.NATSCAFile = get("CB_AUTH_NATS_CA_FILE")
	s.NATSCertFile = get("CB_AUTH_NATS_CERT_FILE")
	s.NATSKeyFile = get("CB_AUTH_NATS_KEY_FILE")
	switch v := get("CB_AUTH_PLAIN_REQUESTS"); v {
	case "":
	case "allow":
		s.PlainRequests = true
	default:
		return Settings{}, true, fmt.Errorf("%w: CB_AUTH_PLAIN_REQUESTS: only \"allow\", got %q", ErrConfig, v)
	}
	if (s.XKeySeedFile == "") == !s.PlainRequests {
		return Settings{}, true, fmt.Errorf("%w: CB_AUTH_XKEY_SEED_FILE must be set (or, to accept unencrypted requests, CB_AUTH_PLAIN_REQUESTS=allow instead)", ErrConfig)
	}
	if err := natsURLSecure(s.NATSURL); err != nil {
		return Settings{}, true, fmt.Errorf("%w: CB_AUTH_NATS_URL: %v", ErrConfig, err)
	}
	if (s.NATSCertFile == "") != (s.NATSKeyFile == "") {
		return Settings{}, true, fmt.Errorf("%w: CB_AUTH_NATS_CERT_FILE and CB_AUTH_NATS_KEY_FILE go together", ErrConfig)
	}
	s.BrakeFile = get("CB_AUTH_BRAKE_FILE")
	s.JSSeedFile = get("CB_AUTH_JS_SEED_FILE")
	s.JSURL = get("CB_AUTH_JS_URL")
	if s.JSSeedFile == "" {
		return Settings{}, true, fmt.Errorf("%w: CB_AUTH_JS_SEED_FILE must be set with CB_AUTH_NATS_URL: without consumer management credentials would carry deliver subjects nobody reconciles or brakes", ErrConfig)
	}
	if s.JSURL == "" {
		s.JSURL = s.NATSURL
	}
	if err := natsURLSecure(s.JSURL); err != nil {
		return Settings{}, true, fmt.Errorf("%w: CB_AUTH_JS_URL: %v", ErrConfig, err)
	}
	s.Callout = callout.Config{
		Account:    get("CB_AUTH_ACCOUNT"),
		Lobby:      get("CB_AUTH_LOBBY"),
		NamePrefix: get("CB_AUTH_NAME_PREFIX"),
		Subjects: callout.Subjects{Prefix: get("CB_AUTH_SUBJECT_PREFIX"), Presence: get("CB_AUTH_PRESENCE_PREFIX"), Stats: get("CB_AUTH_STATS_PREFIX"),
			Archive: get("CB_AUTH_STATS_ARCHIVE_PREFIX")},
	}
	s.Token = token.Config{
		Issuer:        get("CB_AUTH_KEYCLOAK_ISSUER"),
		JWKSURL:       get("CB_AUTH_KEYCLOAK_JWKS_URL"),
		ClientID:      get("CB_AUTH_KEYCLOAK_CLIENT_ID"),
		Audience:      get("CB_AUTH_KEYCLOAK_AUDIENCE"),
		ApproverGroup: get("CB_AUTH_APPROVER_GROUP"),
		ApproverClaim: get("CB_AUTH_APPROVER_CLAIM"),
	}
	s.Binding = get("CB_AUTH_NAME_BINDING")
	s.MattermostURL = get("CB_AUTH_MM_URL")
	s.MattermostTeam = get("CB_AUTH_MM_TEAM")
	s.MattermostFile = get("CB_AUTH_MM_TOKEN_FILE")
	s.DirectoryBucket = get("CB_AUTH_DIRECTORY_BUCKET")
	switch v := get("CB_AUTH_DIRECTORY_IMPORTED"); v {
	case "", "false":
	case "true":
		s.DirectoryImported = true
	default:
		return Settings{}, true, fmt.Errorf("%w: CB_AUTH_DIRECTORY_IMPORTED: true or false, got %q", ErrConfig, v)
	}
	s.AdminListenAddr = get("CB_AUTH_ADMIN_LISTEN_ADDR")
	s.AdminTLSCertFile = get("CB_AUTH_ADMIN_TLS_CERT_FILE")
	s.AdminTLSKeyFile = get("CB_AUTH_ADMIN_TLS_KEY_FILE")
	s.DirectoryAdminGroup = get("CB_AUTH_DIRECTORY_ADMIN_GROUP")
	s.StatsChannel = statsChannel(env)
	if err := adminSettings(s); err != nil {
		return Settings{}, true, err
	}
	if s.DirectoryBucket != "" && !bucketRE.MatchString(s.DirectoryBucket) {
		return Settings{}, true, fmt.Errorf("%w: CB_AUTH_DIRECTORY_BUCKET: letters, digits, - and _ only, got %q", ErrConfig, s.DirectoryBucket)
	}

	for k, v := range map[string]string{
		"CB_AUTH_NATS_SEED_FILE": s.NATSSeedFile, "CB_AUTH_ISSUER_SEED_FILE": s.IssuerSeedFile,
		"CB_AUTH_ACCOUNT": s.Callout.Account, "CB_AUTH_KEYCLOAK_ISSUER": s.Token.Issuer,
		"CB_AUTH_KEYCLOAK_JWKS_URL": s.Token.JWKSURL, "CB_AUTH_KEYCLOAK_CLIENT_ID": s.Token.ClientID,
		"CB_AUTH_MM_URL": s.MattermostURL, "CB_AUTH_MM_TEAM": s.MattermostTeam,
		"CB_AUTH_MM_TOKEN_FILE": s.MattermostFile,
	} {
		if v == "" {
			return Settings{}, true, fmt.Errorf("%w: %s must be set with CB_AUTH_NATS_URL", ErrConfig, k)
		}
	}
	for k, dst := range map[string]*time.Duration{
		"CB_AUTH_REQUEST_TIMEOUT": &s.Callout.Timeout, "CB_AUTH_TOKEN_LEEWAY": &s.Token.Leeway,
	} {
		if v := get(k); v != "" {
			d, err := time.ParseDuration(v)
			if err != nil {
				return Settings{}, true, fmt.Errorf("%w: %s: %v", ErrConfig, k, err)
			}
			*dst = d
		}
	}
	switch s.Binding {
	case "", "open":
		s.Token.Binding = token.Open{}
	case "openid-only":
		s.Token.Binding = token.OpenIDOnly{}
	default:
		return Settings{}, true, fmt.Errorf("%w: CB_AUTH_NAME_BINDING: open or openid-only, got %q", ErrConfig, s.Binding)
	}
	// The operator console's path (RT-6.43): a person's credential for a
	// token of the console's own client, never of the bus's.
	s.Callout.Console = callout.Console{ClientID: get("CB_AUTH_CONSOLE_CLIENT_ID"),
		OperatorGroup: get("CB_AUTH_OPERATOR_GROUP"), PersonPrefix: get("CB_AUTH_PERSON_PREFIX")}
	if v := get("CB_AUTH_PERSON_MAX_LOGINS"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 1 || n > 64 {
			return Settings{}, true, fmt.Errorf("%w: CB_AUTH_PERSON_MAX_LOGINS: a number from 1 to 64, got %q", ErrConfig, v)
		}
		s.Callout.Console.MaxLogins = n
	}
	if v := get("CB_AUTH_CONSOLE_METRICS"); v != "" {
		on, err := strconv.ParseBool(v)
		if err != nil {
			return Settings{}, true, fmt.Errorf("%w: CB_AUTH_CONSOLE_METRICS %q is neither true nor false", ErrConfig, v)
		}
		s.Callout.Console.Metrics = on
	}
	if c := s.Callout.Console.ClientID; c != "" && (c == s.Token.ClientID || c == s.Token.Audience) {
		return Settings{}, true, fmt.Errorf("%w: CB_AUTH_CONSOLE_CLIENT_ID must differ from the bus's client and audience", ErrConfig)
	}
	s.Callout = s.Callout.WithDefaults()
	return s, true, nil
}

// adminSettings checks the admin API's settings.
func adminSettings(s Settings) error {
	if (s.AdminTLSCertFile == "") != (s.AdminTLSKeyFile == "") {
		return fmt.Errorf("%w: CB_AUTH_ADMIN_TLS_CERT_FILE and CB_AUTH_ADMIN_TLS_KEY_FILE go together", ErrConfig)
	}
	if s.AdminListenAddr == "" {
		return nil
	}
	if s.DirectoryAdminGroup == "" {
		return fmt.Errorf("%w: CB_AUTH_DIRECTORY_ADMIN_GROUP must be set with CB_AUTH_ADMIN_LISTEN_ADDR", ErrConfig)
	}
	host, _, err := net.SplitHostPort(s.AdminListenAddr)
	if err != nil {
		return fmt.Errorf("%w: CB_AUTH_ADMIN_LISTEN_ADDR: host:port expected", ErrConfig)
	}
	if s.AdminTLSCertFile == "" {
		if ip := net.ParseIP(host); ip == nil || !ip.IsLoopback() {
			return fmt.Errorf("%w: CB_AUTH_ADMIN_LISTEN_ADDR: a loopback IP address (127.0.0.0/8, ::1; not a name) unless CB_AUTH_ADMIN_TLS_CERT_FILE and _KEY_FILE are set", ErrConfig)
		}
	}
	return nil
}

// serveAdmin listens on the admin address and serves the directory API until
// ctx ends. The listen error is returned at once; a later serve error goes to
// the returned channel.
func serveAdmin(ctx context.Context, s Settings, h http.Handler, log *slog.Logger) (<-chan error, error) {
	ln, err := net.Listen("tcp", s.AdminListenAddr)
	if err != nil {
		return nil, fmt.Errorf("admin API: %w", err)
	}
	srv := &http.Server{Handler: h, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second,
		WriteTimeout: 120 * time.Second}
	errc := make(chan error, 1)
	go func() {
		var err error
		if s.AdminTLSCertFile != "" {
			err = srv.ServeTLS(ln, s.AdminTLSCertFile, s.AdminTLSKeyFile)
		} else {
			err = srv.Serve(ln)
		}
		if !errors.Is(err, http.ErrServerClosed) {
			errc <- fmt.Errorf("admin API: %w", err)
		}
		close(errc)
	}()
	go func() {
		<-ctx.Done()
		sctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(sctx)
	}()
	log.Info("directory admin API serving", "addr", ln.Addr().String(), "tls", s.AdminTLSCertFile != "")
	return errc, nil
}

// RunCallout starts the callout and serves until ctx ends. Errors before it
// serves wrap ErrConfig when the configuration is at fault.
func RunCallout(ctx context.Context, s Settings, log *slog.Logger) error {
	if s.JSSeedFile == "" {
		return fmt.Errorf("%w: CB_AUTH_JS_SEED_FILE must be set", ErrConfig)
	}
	issuer, err := seed(s.IssuerSeedFile, "CB_AUTH_ISSUER_SEED_FILE")
	if err != nil {
		return err
	}
	var xkey nkeys.KeyPair
	if s.XKeySeedFile != "" {
		if xkey, err = seed(s.XKeySeedFile, "CB_AUTH_XKEY_SEED_FILE"); err != nil {
			return err
		}
	}
	user, err := seed(s.NATSSeedFile, "CB_AUTH_NATS_SEED_FILE")
	if err != nil {
		return err
	}
	jsUser, err := seed(s.JSSeedFile, "CB_AUTH_JS_SEED_FILE")
	if err != nil {
		return err
	}
	mmToken, err := secret(s.MattermostFile, "CB_AUTH_MM_TOKEN_FILE")
	if err != nil {
		return err
	}

	startCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	mm, err := mattermost.New(startCtx, s.MattermostURL, s.MattermostTeam, mmToken)
	if err != nil {
		return err
	}
	if !mm.AuthVisible() {
		log.Warn("the Mattermost token cannot see how accounts log in; every login is refused until it can",
			"needs", "a system admin's token")
	}
	tc := s.Token
	tc.KeyFetchError = func(err error) { log.Warn("JWKS fetch failed", "reason", err.Error()) }
	if s.Callout.Console.ClientID != "" {
		tc.OtherClients = []string{s.Callout.Console.ClientID}
	}
	v, err := token.New(ctx, tc, mm)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrConfig, err)
	}
	pv, err := personVerifier(ctx, s, tc, mm)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrConfig, err)
	}
	brake := callout.NewBrake(s.BrakeFile, func(line string) {
		log.Warn("emergency brake: a line that is no username holds nobody", "line", line)
	})
	connect := func(url, name string, kp nkeys.KeyPair) (*nats.Conn, error) {
		return nats.Connect(url, connectOptions(s, url, name, kp, log)...)
	}
	nc, err := connect(s.NATSURL, "authd", user)
	if err != nil {
		return fmt.Errorf("NATS: %w", err)
	}
	defer nc.Close()
	jsnc, err := connect(s.JSURL, "authd-js", jsUser)
	if err != nil {
		return fmt.Errorf("NATS (JetStream): %w", err)
	}
	defer jsnc.Close()
	mgr, err := consumers.New(jsnc, consumers.Config{Bus: s.Callout.Subjects.Prefix, Presence: s.Callout.Subjects.Presence,
		Stats: s.Callout.Subjects.Stats, Log: log})
	if err != nil {
		return fmt.Errorf("%w: %v", ErrConfig, err)
	}
	if err := mgr.EnsureStreams(startCtx); err != nil {
		return fmt.Errorf("streams: %w", err)
	}
	// The STATS history (RT-6.45): the archive stream, which copies every
	// STATS line by itself, and a look at its fill now and every hour.
	archive, err := mgr.EnsureArchive(startCtx, s.Callout.Subjects.Archive, 0)
	if err != nil {
		return fmt.Errorf("streams: %w", err)
	}
	archive.WatchFill(ctx, time.Hour)
	// The console's unattended metrics page (#158): its two consumers are
	// made at this start and kept, idempotent, like every consumer of the
	// bus — the console's service account itself has no JetStream right.
	// Off unless CB_AUTH_CONSOLE_METRICS=true: nobody's consumers exist
	// while the page is off.
	if s.Callout.Console.Metrics {
		if err := mgr.EnsureMetrics(startCtx); err != nil {
			return fmt.Errorf("metrics consumers: %w", err)
		}
		metricsTick(ctx, mgr, log, time.Hour, 5*time.Minute)
	}
	if err := mgr.WatchByes(ctx); err != nil {
		return err
	}
	mgr.WatchBrake(ctx, brake)
	js, err := jetstream.New(jsnc)
	if err != nil {
		return fmt.Errorf("JetStream: %w", err)
	}
	dir, err := directory.Open(startCtx, js, s.DirectoryBucket, s.DirectoryImported)
	if err != nil {
		return err
	}
	// The directory answers membership once the import has run; until then
	// Mattermost does, by username, as before (ADR-016, decision 2).
	svc, err := callout.New(s.Callout, issuer, xkey, v, directory.Channels{Store: dir, Fallback: mm}, brake, log)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrConfig, err)
	}
	svc.SetAnchors(directory.Anchors{Store: dir, Log: log})
	svc.SetTrust(directory.Trust{Store: dir})
	trustIndex := &directory.TrustIndex{Store: dir, Log: log}
	svc.SetTrustIndex(trustIndex)
	// A session alternating between networks is refused, a host key reported (#154 step 3).
	svc.SetWatcher(&theft.Guard{Log: log})
	svc.SetAttester(callout.NATSAttester{NC: jsnc, Stream: consumers.PresenceStream})
	// A copied session or host key is noticed for the owner (#154 step 3).
	svc.SetNoticer(callout.NATSNoticer{NC: jsnc})
	if pv != nil {
		svc.SetPersons(pv, directory.Persons{Store: dir, Log: log})
		log.Info("the console's path on", "client", s.Callout.Console.ClientID, "operators", s.Callout.Console.OperatorGroup)
	}
	runCtx, stop := context.WithCancel(ctx)
	defer stop()
	// The trust index's sweep runs beside the logins, never inside one; it
	// stops with the service.
	swept := make(chan struct{})
	go func() {
		defer close(swept)
		trustIndex.Sweep(runCtx)
	}()
	defer func() { stop(); <-swept }()
	var adminErr <-chan error
	if s.AdminListenAddr != "" {
		binding := s.Binding
		if binding == "" {
			binding = "open"
		}
		acfg := admin.Config{AdminGroup: s.DirectoryAdminGroup, NameBinding: binding, Log: log}
		if s.StatsChannel != "" { // the STATS import (RT-6.45)
			acfg.Stats = &admin.StatsImport{Channel: s.StatsChannel, Source: mm, Store: archive}
		}
		h, err := admin.New(acfg, v, dir, mm)
		if err != nil {
			return fmt.Errorf("%w: %v", ErrConfig, err)
		}
		if adminErr, err = serveAdmin(runCtx, s, h, log); err != nil {
			return err
		}
	}
	svc.SetConsumers(mgr)
	log.Info("consumer management on", "bus", s.Callout.Subjects.Prefix, "presence", s.Callout.Subjects.Presence, "stats", s.Callout.Subjects.Stats, "stats archive", archive.Prefix())
	log.Info("auth callout serving", "account", s.Callout.Account, "binding", s.Binding)
	if adminErr != nil {
		go func() {
			if err := <-adminErr; err != nil {
				log.Error("directory admin API stopped", "reason", err.Error())
				stop()
			}
		}()
	}
	err = svc.Serve(runCtx, nc)
	if err == nil && ctx.Err() == nil && runCtx.Err() != nil {
		return errors.New("the directory admin API stopped")
	}
	return err
}

// connectOptions: one NATS connection of the login service (RT-6.35). To a
// tls:// server the TLS handshake comes first, before the server's INFO, as
// the agents' hosts do: the server then needs handshake_first in its tls block
// (services/auth/README.md). A loopback nats:// stays plain.
func connectOptions(s Settings, urls, name string, kp nkeys.KeyPair, log *slog.Logger) []nats.Option {
	pub, _ := kp.PublicKey()
	opts := []nats.Option{nats.Name(name), nats.Nkey(pub, kp.Sign),
		nats.MaxReconnects(-1), nats.ReconnectWait(2 * time.Second),
		nats.DisconnectErrHandler(func(_ *nats.Conn, err error) {
			log.Warn("NATS disconnected", "connection", name, "reason", fmt.Sprint(err))
		}),
		nats.ReconnectHandler(func(nc *nats.Conn) {
			log.Info("NATS reconnected", "connection", name, "server", nc.ConnectedUrlRedacted())
		}),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, err error) {
			log.Error("NATS error", "connection", name, "reason", err.Error())
		})}
	if s.NATSCAFile != "" {
		opts = append(opts, nats.RootCAs(s.NATSCAFile))
	}
	if s.NATSCertFile != "" {
		opts = append(opts, nats.ClientCert(s.NATSCertFile, s.NATSKeyFile))
	}
	if tlsFirst(urls) {
		opts = append(opts, nats.TLSHandshakeFirst())
	}
	return opts
}

// tlsFirst: every URL of the list is tls://. natsURLSecure refuses a list that
// mixes tls:// with another scheme: one connection has one order, and a
// handshake_first server never lets a classic client in.
func tlsFirst(urls string) bool {
	for raw := range strings.SplitSeq(urls, ",") {
		u, err := url.Parse(strings.TrimSpace(raw))
		if err != nil || u.Scheme != "tls" {
			return false
		}
	}
	return true
}

// natsURLSecure: every URL of the list must be tls://, except on a loopback
// host, since the callout's connection carries hosts' tokens. A list is tls://
// throughout or not at all: over tls:// the handshake comes first (tlsFirst),
// and a list that mixes would reach a handshake_first server in the classic
// order, which it refuses.
//
// Websockets (ws://, wss://) are refused: the NATS hosts' side accepts them
// (RT-6.36), the service does not yet. Before it may, the sender-authenticity
// suite has to run against a server with a websocket listener (the callout's
// token, the subject rights, the auth callout over the WebSocket transport),
// and the service's own TLS-first rule has no meaning there.
func natsURLSecure(urls string) error {
	var tlsURLs, others int
	for raw := range strings.SplitSeq(urls, ",") {
		u, err := url.Parse(strings.TrimSpace(raw))
		if err != nil || u.Host == "" {
			return fmt.Errorf("%q is not a URL", raw)
		}
		if u.Scheme == "ws" || u.Scheme == "wss" {
			return fmt.Errorf("%q: the login service does not speak websockets yet; use tls:// (RT-6.36)", raw)
		}
		if u.Scheme == "tls" {
			tlsURLs++
			continue
		}
		others++
		h := u.Hostname()
		if ip := net.ParseIP(h); h == "localhost" || (ip != nil && ip.IsLoopback()) {
			continue
		}
		return fmt.Errorf("%q: tls:// required off a loopback host", raw)
	}
	if tlsURLs > 0 && others > 0 {
		return fmt.Errorf("tls:// mixed with another scheme: every URL tls:// or none, since over tls:// the TLS handshake comes first")
	}
	return nil
}

func secret(path, name string) (string, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("%w: %s: cannot read the file", ErrConfig, name)
	}
	v := strings.TrimSpace(string(b))
	if v == "" {
		return "", fmt.Errorf("%w: %s: the file is empty", ErrConfig, name)
	}
	return v, nil
}

func seed(path, name string) (nkeys.KeyPair, error) {
	v, err := secret(path, name)
	if err != nil {
		return nil, err
	}
	kp, err := nkeys.FromSeed([]byte(v))
	if err != nil {
		return nil, fmt.Errorf("%w: %s: not an nkey seed", ErrConfig, name)
	}
	return kp, nil
}

// personVerifier is the token check of the console's path (RT-6.43): the same
// realm and keys as the agents', the console's client as `azp` and audience,
// the bus's client refused in `aud`. nil when the path is off.
func personVerifier(ctx context.Context, s Settings, bus token.Config, dir token.Directory) (*token.Verifier, error) {
	if s.Callout.Console.ClientID == "" {
		return nil, nil
	}
	return token.New(ctx, personTokenConfig(s, bus), dir)
}

// personTokenConfig: the console's client as `azp` and audience; a token
// whose `aud` names the bus's client or the bus's audience is refused.
func personTokenConfig(s Settings, bus token.Config) token.Config {
	pc := bus
	pc.ClientID, pc.Audience, pc.ApproverGroup = s.Callout.Console.ClientID, "", ""
	pc.OtherClients = []string{bus.ClientID}
	if bus.Audience != "" && bus.Audience != bus.ClientID {
		pc.OtherClients = append(pc.OtherClients, bus.Audience)
	}
	return pc
}

// metricsTick re-ensures the console's metrics consumers hourly: their
// InactiveThreshold (5 min) ages them out while the console is down, and
// the tick makes them again with their full 7-day replay (#168's round:
// a kept consumer never replays, so a console restart would lose the
// history otherwise).
func metricsTick(ctx context.Context, mgr *consumers.Manager, log *slog.Logger,
	every, threshold time.Duration) {
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case <-time.After(every):
				cctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
				if err := mgr.EnsureMetrics(cctx); err != nil {
					log.Warn("metrics consumers: re-ensure failed", "err", err)
				}
				cancel()
			}
		}
	}()
}

// defaultStatsChannel is the MCP server's default stats channel.
const defaultStatsChannel = "agents-stats"

// statsChannel reads CB_MM_STATS_CHANNEL the way the MCP server does
// (stats.channel()): unset gives agents-stats, a leading ~ goes, and off,
// none, 0, false or empty switch the STATS import off ("").
func statsChannel(env *envname.Env) string {
	v, set, _ := env.Get("CB_MM_STATS_CHANNEL") // checked in LoadSettings
	if !set {
		v = defaultStatsChannel
	}
	v = strings.TrimLeft(strings.TrimSpace(v), "~")
	switch strings.ToLower(v) {
	case "", "off", "none", "0", "false":
		return ""
	}
	return v
}
