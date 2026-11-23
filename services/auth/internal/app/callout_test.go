package app

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nkeys"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

func env(over map[string]string) func(string) string {
	base := map[string]string{
		"AUTH_NATS_URL": "tls://nats.example:4222", "AUTH_NATS_SEED_FILE": "/run/secrets/nats",
		"AUTH_ISSUER_SEED_FILE": "/run/secrets/issuer", "AUTH_JS_SEED_FILE": "/run/secrets/js", "AUTH_XKEY_SEED_FILE": "/run/secrets/xkey", "AUTH_ACCOUNT": "BUS",
		"AUTH_KEYCLOAK_ISSUER": "https://kc.example/realms/x", "AUTH_KEYCLOAK_JWKS_URL": "https://kc.example/certs",
		"AUTH_KEYCLOAK_CLIENT_ID": "agent-bus", "AUTH_MATTERMOST_URL": "https://chat.example",
		"AUTH_MATTERMOST_TEAM": "team", "AUTH_MATTERMOST_TOKEN_FILE": "/run/secrets/mm",
	}
	for k, v := range over {
		base[k] = v
	}
	return func(k string) string { return base[k] }
}

func TestWithoutANATSURLThereIsNoCallout(t *testing.T) {
	_, on, err := LoadSettings(func(string) string { return "" })
	if on || err != nil {
		t.Fatalf("%v, %v", on, err)
	}
}

func TestSettings(t *testing.T) {
	s, on, err := LoadSettings(env(map[string]string{"AUTH_REQUEST_TIMEOUT": "1s", "AUTH_NAME_BINDING": "openid-only"}))
	if !on || err != nil {
		t.Fatalf("%v, %v", on, err)
	}
	if s.Callout.Timeout != time.Second || s.Callout.Subjects.Prefix != "bus" || s.Callout.Subjects.Presence != "presence" || s.Callout.NamePrefix != "agent-" {
		t.Fatalf("%+v", s.Callout)
	}
	if _, ok := s.Token.Binding.(token.OpenIDOnly); !ok {
		t.Fatalf("binding %T", s.Token.Binding)
	}
	if s, _, _ := LoadSettings(env(nil)); s.Token.Binding != (token.Open{}) {
		t.Fatalf("default binding %T", s.Token.Binding)
	}
	if s, _, err := LoadSettings(env(map[string]string{"AUTH_DIRECTORY_BUCKET": "TENANT_A-dir"})); err != nil || s.DirectoryBucket != "TENANT_A-dir" {
		t.Fatalf("directory bucket: %q %v", s.DirectoryBucket, err)
	}
	if _, _, err := LoadSettings(env(map[string]string{"AUTH_NATS_URL": "nats://127.0.0.1:4222"})); err != nil {
		t.Fatalf("plain NATS on loopback: %v", err)
	}
	if s, _, err := LoadSettings(env(map[string]string{"AUTH_XKEY_SEED_FILE": "", "AUTH_PLAIN_REQUESTS": "allow"})); err != nil || !s.PlainRequests {
		t.Fatalf("explicit plain requests: %+v, %v", s.PlainRequests, err)
	}
	for name, over := range map[string]map[string]string{
		"no account":                  {"AUTH_ACCOUNT": ""},
		"no issuer seed":              {"AUTH_ISSUER_SEED_FILE": ""},
		"no JetStream seed":           {"AUTH_JS_SEED_FILE": ""},
		"no chat token":               {"AUTH_MATTERMOST_TOKEN_FILE": ""},
		"bad timeout":                 {"AUTH_REQUEST_TIMEOUT": "soon"},
		"bad binding":                 {"AUTH_NAME_BINDING": "first-login"},
		"no xkey":                     {"AUTH_XKEY_SEED_FILE": ""},
		"xkey and plain":              {"AUTH_PLAIN_REQUESTS": "allow"},
		"plain, wrongly":              {"AUTH_XKEY_SEED_FILE": "", "AUTH_PLAIN_REQUESTS": "yes"},
		"cert, no key":                {"AUTH_NATS_CERT_FILE": "/run/secrets/cert"},
		"plain NATS off loopback":     {"AUTH_NATS_URL": "nats://nats.example:4222"},
		"one plain URL in a list":     {"AUTH_NATS_URL": "tls://a.example:4222,nats://b.example:4222"},
		"directory bucket with a dot": {"AUTH_DIRECTORY_BUCKET": "BUS.DIRECTORY"},
		"directory bucket wildcard":   {"AUTH_DIRECTORY_BUCKET": "*"},
	} {
		if _, _, err := LoadSettings(env(over)); !errors.Is(err, ErrConfig) {
			t.Errorf("%s: %v", name, err)
		}
	}
}

func TestAMissingOrBadKeyFileStopsTheStart(t *testing.T) {
	dir := t.TempDir()
	notASeed := filepath.Join(dir, "issuer")
	if err := os.WriteFile(notASeed, []byte("not a seed\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	for name, path := range map[string]string{"missing": filepath.Join(dir, "nope"), "not a seed": notASeed} {
		s, _, err := LoadSettings(env(map[string]string{"AUTH_ISSUER_SEED_FILE": path}))
		if err != nil {
			t.Fatal(err)
		}
		err = RunCallout(context.Background(), s, slog.New(slog.DiscardHandler))
		if !errors.Is(err, ErrConfig) {
			t.Errorf("%s: %v", name, err)
		}
		if err != nil && strings.Contains(err.Error(), "not a seed\n") {
			t.Errorf("%s: the file's content is in the error", name)
		}
	}
}

func TestConsumerManagementSettings(t *testing.T) {
	s, _, err := LoadSettings(env(nil))
	if err != nil || s.JSSeedFile != "/run/secrets/js" || s.JSURL != "tls://nats.example:4222" {
		t.Fatalf("default: %+v, %v", s, err)
	}
	s, _, err = LoadSettings(env(map[string]string{"AUTH_JS_URL": "tls://js.example:4222"}))
	if err != nil || s.JSURL != "tls://js.example:4222" {
		t.Fatalf("own URL: %+v, %v", s, err)
	}
	for name, over := range map[string]map[string]string{
		"plain URL off lo":   {"AUTH_JS_URL": "nats://js.example:4222"},
		"URL that is no URL": {"AUTH_JS_URL": "nonsense"},
	} {
		if _, _, err := LoadSettings(env(over)); !errors.Is(err, ErrConfig) {
			t.Errorf("%s: %v", name, err)
		}
	}
}

// No half mode: a credential carries deliver subjects, so without consumer
// management the service does not start.
func TestWithoutAJetStreamSeedTheServiceDoesNotStart(t *testing.T) {
	for name, over := range map[string]map[string]string{
		"no seed":         {"AUTH_JS_SEED_FILE": ""},
		"URL, no seed":    {"AUTH_JS_SEED_FILE": "", "AUTH_JS_URL": "tls://js.example:4222"},
		"blank seed path": {"AUTH_JS_SEED_FILE": "  "},
	} {
		_, _, err := LoadSettings(env(over))
		if !errors.Is(err, ErrConfig) || !strings.Contains(err.Error(), "AUTH_JS_SEED_FILE") {
			t.Errorf("%s: %v", name, err)
		}
	}
	// RunCallout refuses settings built without it, too.
	s, _, _ := LoadSettings(env(nil))
	s.JSSeedFile = ""
	err := RunCallout(context.Background(), s, slog.New(slog.DiscardHandler))
	if !errors.Is(err, ErrConfig) || !strings.Contains(err.Error(), "AUTH_JS_SEED_FILE") {
		t.Errorf("RunCallout: %v", err)
	}
}

func TestABadJetStreamSeedFileStopsTheStart(t *testing.T) {
	dir := t.TempDir()
	write := func(name, content string) string {
		p := filepath.Join(dir, name)
		if err := os.WriteFile(p, []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
		return p
	}
	acct, _ := nkeys.CreateAccount()
	acctSeed, _ := acct.Seed()
	curve, _ := nkeys.CreateCurveKeys()
	curveSeed, _ := curve.Seed()
	usr, _ := nkeys.CreateUser()
	usrSeed, _ := usr.Seed()
	over := map[string]string{
		"AUTH_ISSUER_SEED_FILE": write("issuer", string(acctSeed)), "AUTH_XKEY_SEED_FILE": write("xkey", string(curveSeed)),
		"AUTH_NATS_SEED_FILE": write("nats", string(usrSeed)),
	}
	for name, path := range map[string]string{"missing": filepath.Join(dir, "nope"), "not a seed": write("js", "not a seed\n")} {
		over["AUTH_JS_SEED_FILE"] = path
		s, _, err := LoadSettings(env(over))
		if err != nil {
			t.Fatal(err)
		}
		err = RunCallout(context.Background(), s, slog.New(slog.DiscardHandler))
		if err == nil || !errors.Is(err, ErrConfig) || !strings.Contains(err.Error(), "AUTH_JS_SEED_FILE") {
			t.Errorf("%s: %v", name, err)
		}
		if err != nil && strings.Contains(err.Error(), "not a seed\n") {
			t.Errorf("%s: the file's content is in the error", name)
		}
	}
}
