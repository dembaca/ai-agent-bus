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
	"net/url"
	"os"
	"regexp"
	"strings"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
	"github.com/nats-io/nkeys"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/callout"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/consumers"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/directory"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/mattermost"
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
	PlainRequests  bool   // AUTH_PLAIN_REQUESTS=allow: the server sends requests unencrypted
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
}

// LoadSettings reads the callout's settings. It reports false when
// AUTH_NATS_URL is not set: the service then runs without the callout.
func LoadSettings(getenv func(string) string) (Settings, bool, error) {
	get := func(k string) string { return strings.TrimSpace(getenv(k)) }
	s := Settings{NATSURL: get("AUTH_NATS_URL")}
	if s.NATSURL == "" {
		return Settings{}, false, nil
	}
	s.NATSSeedFile = get("AUTH_NATS_SEED_FILE")
	s.IssuerSeedFile = get("AUTH_ISSUER_SEED_FILE")
	s.XKeySeedFile = get("AUTH_XKEY_SEED_FILE")
	s.NATSCAFile = get("AUTH_NATS_CA_FILE")
	s.NATSCertFile = get("AUTH_NATS_CERT_FILE")
	s.NATSKeyFile = get("AUTH_NATS_KEY_FILE")
	switch v := get("AUTH_PLAIN_REQUESTS"); v {
	case "":
	case "allow":
		s.PlainRequests = true
	default:
		return Settings{}, true, fmt.Errorf("%w: AUTH_PLAIN_REQUESTS: only \"allow\", got %q", ErrConfig, v)
	}
	if (s.XKeySeedFile == "") == !s.PlainRequests {
		return Settings{}, true, fmt.Errorf("%w: AUTH_XKEY_SEED_FILE must be set (or, to accept unencrypted requests, AUTH_PLAIN_REQUESTS=allow instead)", ErrConfig)
	}
	if err := natsURLSecure(s.NATSURL); err != nil {
		return Settings{}, true, fmt.Errorf("%w: AUTH_NATS_URL: %v", ErrConfig, err)
	}
	if (s.NATSCertFile == "") != (s.NATSKeyFile == "") {
		return Settings{}, true, fmt.Errorf("%w: AUTH_NATS_CERT_FILE and AUTH_NATS_KEY_FILE go together", ErrConfig)
	}
	s.BrakeFile = get("AUTH_BRAKE_FILE")
	s.JSSeedFile = get("AUTH_JS_SEED_FILE")
	s.JSURL = get("AUTH_JS_URL")
	if s.JSSeedFile == "" {
		return Settings{}, true, fmt.Errorf("%w: AUTH_JS_SEED_FILE must be set with AUTH_NATS_URL: without consumer management credentials would carry deliver subjects nobody reconciles or brakes", ErrConfig)
	}
	if s.JSURL == "" {
		s.JSURL = s.NATSURL
	}
	if err := natsURLSecure(s.JSURL); err != nil {
		return Settings{}, true, fmt.Errorf("%w: AUTH_JS_URL: %v", ErrConfig, err)
	}
	s.Callout = callout.Config{
		Account:    get("AUTH_ACCOUNT"),
		Lobby:      get("AUTH_LOBBY"),
		NamePrefix: get("AUTH_NAME_PREFIX"),
		Subjects:   callout.Subjects{Prefix: get("AUTH_SUBJECT_PREFIX"), Presence: get("AUTH_PRESENCE_PREFIX")},
	}
	s.Token = token.Config{
		Issuer:        get("AUTH_KEYCLOAK_ISSUER"),
		JWKSURL:       get("AUTH_KEYCLOAK_JWKS_URL"),
		ClientID:      get("AUTH_KEYCLOAK_CLIENT_ID"),
		Audience:      get("AUTH_KEYCLOAK_AUDIENCE"),
		ApproverGroup: get("AUTH_APPROVER_GROUP"),
		ApproverClaim: get("AUTH_APPROVER_CLAIM"),
	}
	s.Binding = get("AUTH_NAME_BINDING")
	s.MattermostURL = get("AUTH_MATTERMOST_URL")
	s.MattermostTeam = get("AUTH_MATTERMOST_TEAM")
	s.MattermostFile = get("AUTH_MATTERMOST_TOKEN_FILE")
	s.DirectoryBucket = get("AUTH_DIRECTORY_BUCKET")
	if s.DirectoryBucket != "" && !bucketRE.MatchString(s.DirectoryBucket) {
		return Settings{}, true, fmt.Errorf("%w: AUTH_DIRECTORY_BUCKET: letters, digits, - and _ only, got %q", ErrConfig, s.DirectoryBucket)
	}

	for k, v := range map[string]string{
		"AUTH_NATS_SEED_FILE": s.NATSSeedFile, "AUTH_ISSUER_SEED_FILE": s.IssuerSeedFile,
		"AUTH_ACCOUNT": s.Callout.Account, "AUTH_KEYCLOAK_ISSUER": s.Token.Issuer,
		"AUTH_KEYCLOAK_JWKS_URL": s.Token.JWKSURL, "AUTH_KEYCLOAK_CLIENT_ID": s.Token.ClientID,
		"AUTH_MATTERMOST_URL": s.MattermostURL, "AUTH_MATTERMOST_TEAM": s.MattermostTeam,
		"AUTH_MATTERMOST_TOKEN_FILE": s.MattermostFile,
	} {
		if v == "" {
			return Settings{}, true, fmt.Errorf("%w: %s must be set with AUTH_NATS_URL", ErrConfig, k)
		}
	}
	for k, dst := range map[string]*time.Duration{
		"AUTH_REQUEST_TIMEOUT": &s.Callout.Timeout, "AUTH_TOKEN_LEEWAY": &s.Token.Leeway,
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
		return Settings{}, true, fmt.Errorf("%w: AUTH_NAME_BINDING: open or openid-only, got %q", ErrConfig, s.Binding)
	}
	s.Callout = s.Callout.WithDefaults()
	return s, true, nil
}

// RunCallout starts the callout and serves until ctx ends. Errors before it
// serves wrap ErrConfig when the configuration is at fault.
func RunCallout(ctx context.Context, s Settings, log *slog.Logger) error {
	if s.JSSeedFile == "" {
		return fmt.Errorf("%w: AUTH_JS_SEED_FILE must be set", ErrConfig)
	}
	issuer, err := seed(s.IssuerSeedFile, "AUTH_ISSUER_SEED_FILE")
	if err != nil {
		return err
	}
	var xkey nkeys.KeyPair
	if s.XKeySeedFile != "" {
		if xkey, err = seed(s.XKeySeedFile, "AUTH_XKEY_SEED_FILE"); err != nil {
			return err
		}
	}
	user, err := seed(s.NATSSeedFile, "AUTH_NATS_SEED_FILE")
	if err != nil {
		return err
	}
	jsUser, err := seed(s.JSSeedFile, "AUTH_JS_SEED_FILE")
	if err != nil {
		return err
	}
	mmToken, err := secret(s.MattermostFile, "AUTH_MATTERMOST_TOKEN_FILE")
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
	v, err := token.New(ctx, tc, mm)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrConfig, err)
	}
	brake := callout.NewBrake(s.BrakeFile, func(line string) {
		log.Warn("emergency brake: a line that is no username holds nobody", "line", line)
	})
	connect := func(url, name string, kp nkeys.KeyPair) (*nats.Conn, error) {
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
		return nats.Connect(url, opts...)
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
	mgr, err := consumers.New(jsnc, consumers.Config{Bus: s.Callout.Subjects.Prefix, Presence: s.Callout.Subjects.Presence, Log: log})
	if err != nil {
		return fmt.Errorf("%w: %v", ErrConfig, err)
	}
	if err := mgr.EnsureStreams(startCtx); err != nil {
		return fmt.Errorf("streams: %w", err)
	}
	if err := mgr.WatchByes(ctx); err != nil {
		return err
	}
	mgr.WatchBrake(ctx, brake)
	js, err := jetstream.New(jsnc)
	if err != nil {
		return fmt.Errorf("JetStream: %w", err)
	}
	dir, err := directory.Open(startCtx, js, s.DirectoryBucket)
	if err != nil {
		return err
	}
	// The directory answers membership once the import has run; until then
	// Mattermost does, by username, as before (ADR-016, decision 2).
	svc, err := callout.New(s.Callout, issuer, xkey, v, directory.Channels{Store: dir, Fallback: mm}, brake, log)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrConfig, err)
	}
	svc.SetAnchors(directory.Anchors{Store: dir})
	svc.SetConsumers(mgr)
	log.Info("consumer management on", "bus", s.Callout.Subjects.Prefix, "presence", s.Callout.Subjects.Presence)
	log.Info("auth callout serving", "account", s.Callout.Account, "binding", s.Binding)
	return svc.Serve(ctx, nc)
}

// natsURLSecure: every URL of the list must be tls:// (or wss://), except on
// a loopback host, since the callout's connection carries hosts' tokens.
func natsURLSecure(urls string) error {
	for _, raw := range strings.Split(urls, ",") {
		u, err := url.Parse(strings.TrimSpace(raw))
		if err != nil || u.Host == "" {
			return fmt.Errorf("%q is not a URL", raw)
		}
		if u.Scheme == "tls" || u.Scheme == "wss" {
			continue
		}
		h := u.Hostname()
		if ip := net.ParseIP(h); h == "localhost" || (ip != nil && ip.IsLoopback()) {
			continue
		}
		return fmt.Errorf("%q: tls:// required off a loopback host", raw)
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
