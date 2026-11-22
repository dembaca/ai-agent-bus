// Package app wires the auth callout from the environment: the token check
// against Keycloak, the Mattermost directory, the keys from files and the
// connection to NATS.
package app

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nkeys"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/callout"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/mattermost"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

// ErrConfig marks a configuration the service refuses to start with.
var ErrConfig = errors.New("configuration")

// Settings is the callout's configuration from the environment. Secrets are
// file paths, never values.
type Settings struct {
	NATSURL        string
	NATSSeedFile   string // the service's own NATS user (an auth_user)
	IssuerSeedFile string // the account key that signs responses and user JWTs
	XKeySeedFile   string // decrypts requests; empty: the server sends them plain
	BrakeFile      string
	Callout        callout.Config
	Token          token.Config
	Binding        string // "open" or "openid-only"
	MattermostURL  string
	MattermostTeam string
	MattermostFile string // the Mattermost access token
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
	s.BrakeFile = get("AUTH_BRAKE_FILE")
	s.Callout = callout.Config{
		Account:    get("AUTH_ACCOUNT"),
		Lobby:      get("AUTH_LOBBY"),
		NamePrefix: get("AUTH_NAME_PREFIX"),
		Subjects: callout.Subjects{
			Prefix:   get("AUTH_SUBJECT_PREFIX"),
			Stream:   get("AUTH_STREAM"),
			Presence: get("AUTH_PRESENCE_BUCKET"),
		},
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
	svc, err := callout.New(s.Callout, issuer, xkey, v, mm, callout.NewBrake(s.BrakeFile), log)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrConfig, err)
	}
	pub, _ := user.PublicKey()
	nc, err := nats.Connect(s.NATSURL, nats.Name("authd"), nats.Nkey(pub, user.Sign),
		nats.MaxReconnects(-1), nats.ReconnectWait(2*time.Second))
	if err != nil {
		return fmt.Errorf("NATS: %w", err)
	}
	defer nc.Close()
	log.Info("auth callout serving", "account", s.Callout.Account, "binding", s.Binding)
	return svc.Serve(ctx, nc)
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
