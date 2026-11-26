package app

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nkeys"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/callout"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/envname"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

func env(over map[string]string) *envname.Env { return envname.New(envname.Map(envMap(over)), nil) }

func envMap(over map[string]string) map[string]string {
	base := map[string]string{
		"CB_AUTH_NATS_URL": "tls://nats.example:4222", "CB_AUTH_NATS_SEED_FILE": "/run/secrets/nats",
		"CB_AUTH_ISSUER_SEED_FILE": "/run/secrets/issuer", "CB_AUTH_JS_SEED_FILE": "/run/secrets/js", "CB_AUTH_XKEY_SEED_FILE": "/run/secrets/xkey", "CB_AUTH_ACCOUNT": "BUS",
		"CB_AUTH_KEYCLOAK_ISSUER": "https://kc.example/realms/x", "CB_AUTH_KEYCLOAK_JWKS_URL": "https://kc.example/certs",
		"CB_AUTH_KEYCLOAK_CLIENT_ID": "agent-bus", "CB_AUTH_MM_URL": "https://chat.example",
		"CB_AUTH_MM_TEAM": "team", "CB_AUTH_MM_TOKEN_FILE": "/run/secrets/mm",
	}
	for k, v := range over {
		base[k] = v
	}
	return base
}

func TestWithoutANATSURLThereIsNoCallout(t *testing.T) {
	_, on, err := LoadSettings(envname.New(envname.Map(nil), nil))
	if on || err != nil {
		t.Fatalf("%v, %v", on, err)
	}
}

func TestSettings(t *testing.T) {
	s, on, err := LoadSettings(env(map[string]string{"CB_AUTH_REQUEST_TIMEOUT": "1s", "CB_AUTH_NAME_BINDING": "openid-only"}))
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
	if s, _, err := LoadSettings(env(map[string]string{"CB_AUTH_DIRECTORY_IMPORTED": "true"})); err != nil || !s.DirectoryImported {
		t.Fatalf("directory imported: %v %v", s.DirectoryImported, err)
	}
	if s, _, err := LoadSettings(env(map[string]string{"CB_AUTH_DIRECTORY_BUCKET": "TENANT_A-dir"})); err != nil || s.DirectoryBucket != "TENANT_A-dir" {
		t.Fatalf("directory bucket: %q %v", s.DirectoryBucket, err)
	}
	if _, _, err := LoadSettings(env(map[string]string{"CB_AUTH_NATS_URL": "nats://127.0.0.1:4222"})); err != nil {
		t.Fatalf("plain NATS on loopback: %v", err)
	}
	if s, _, err := LoadSettings(env(map[string]string{"CB_AUTH_XKEY_SEED_FILE": "", "CB_AUTH_PLAIN_REQUESTS": "allow"})); err != nil || !s.PlainRequests {
		t.Fatalf("explicit plain requests: %+v, %v", s.PlainRequests, err)
	}
	for _, over := range []map[string]string{
		{"CB_AUTH_ADMIN_LISTEN_ADDR": "127.0.0.1:8081", "CB_AUTH_DIRECTORY_ADMIN_GROUP": "dir-admins"},
		{"CB_AUTH_ADMIN_LISTEN_ADDR": "[::1]:8081", "CB_AUTH_DIRECTORY_ADMIN_GROUP": "dir-admins"},
		{"CB_AUTH_ADMIN_LISTEN_ADDR": "127.9.9.9:8081", "CB_AUTH_DIRECTORY_ADMIN_GROUP": "dir-admins"},
		{"CB_AUTH_ADMIN_LISTEN_ADDR": "localhost:8081", "CB_AUTH_DIRECTORY_ADMIN_GROUP": "dir-admins",
			"CB_AUTH_ADMIN_TLS_CERT_FILE": "/run/secrets/c", "CB_AUTH_ADMIN_TLS_KEY_FILE": "/run/secrets/k"},
		{"CB_AUTH_ADMIN_LISTEN_ADDR": "0.0.0.0:8081", "CB_AUTH_DIRECTORY_ADMIN_GROUP": "dir-admins",
			"CB_AUTH_ADMIN_TLS_CERT_FILE": "/run/secrets/c", "CB_AUTH_ADMIN_TLS_KEY_FILE": "/run/secrets/k"},
	} {
		if s, _, err := LoadSettings(env(over)); err != nil || s.DirectoryAdminGroup != "dir-admins" {
			t.Errorf("admin settings %v: %v", over, err)
		}
	}
	for name, over := range map[string]map[string]string{
		"no account":                  {"CB_AUTH_ACCOUNT": ""},
		"no issuer seed":              {"CB_AUTH_ISSUER_SEED_FILE": ""},
		"no JetStream seed":           {"CB_AUTH_JS_SEED_FILE": ""},
		"no chat token":               {"CB_AUTH_MM_TOKEN_FILE": ""},
		"bad timeout":                 {"CB_AUTH_REQUEST_TIMEOUT": "soon"},
		"bad binding":                 {"CB_AUTH_NAME_BINDING": "first-login"},
		"no xkey":                     {"CB_AUTH_XKEY_SEED_FILE": ""},
		"xkey and plain":              {"CB_AUTH_PLAIN_REQUESTS": "allow"},
		"plain, wrongly":              {"CB_AUTH_XKEY_SEED_FILE": "", "CB_AUTH_PLAIN_REQUESTS": "yes"},
		"cert, no key":                {"CB_AUTH_NATS_CERT_FILE": "/run/secrets/cert"},
		"plain NATS off loopback":     {"CB_AUTH_NATS_URL": "nats://nats.example:4222"},
		"one plain URL in a list":     {"CB_AUTH_NATS_URL": "tls://a.example:4222,nats://b.example:4222"},
		"tls mixed with loopback":     {"CB_AUTH_NATS_URL": "tls://a.example:4222,nats://127.0.0.1:4222"},
		"tls mixed with wss":          {"CB_AUTH_NATS_URL": "tls://a.example:4222,wss://b.example:443"},
		"websocket URL":               {"CB_AUTH_NATS_URL": "wss://nats.example:443"},
		"websocket on loopback":       {"CB_AUTH_NATS_URL": "ws://127.0.0.1:8080"},
		"JetStream websocket":         {"CB_AUTH_JS_URL": "wss://nats.example:443"},
		"JetStream URL mixed":         {"CB_AUTH_JS_URL": "tls://a.example:4222,nats://localhost:4222"},
		"directory bucket with a dot": {"CB_AUTH_DIRECTORY_BUCKET": "BUS.DIRECTORY"},
		"directory bucket wildcard":   {"CB_AUTH_DIRECTORY_BUCKET": "*"},
		"directory imported, unclear": {"CB_AUTH_DIRECTORY_IMPORTED": "yes"},
		"admin address, no group":     {"CB_AUTH_ADMIN_LISTEN_ADDR": "127.0.0.1:8081"},
		"admin, no host":              {"CB_AUTH_ADMIN_LISTEN_ADDR": ":8081", "CB_AUTH_DIRECTORY_ADMIN_GROUP": "dir-admins"},
		"admin, localhost by name":    {"CB_AUTH_ADMIN_LISTEN_ADDR": "localhost:8081", "CB_AUTH_DIRECTORY_ADMIN_GROUP": "dir-admins"},
		"admin, all interfaces":       {"CB_AUTH_ADMIN_LISTEN_ADDR": "0.0.0.0:8081", "CB_AUTH_DIRECTORY_ADMIN_GROUP": "dir-admins"},
		"admin, non-loopback, no TLS": {"CB_AUTH_ADMIN_LISTEN_ADDR": "10.0.0.5:8081", "CB_AUTH_DIRECTORY_ADMIN_GROUP": "dir-admins"},
		"admin cert, no key":          {"CB_AUTH_ADMIN_TLS_CERT_FILE": "/run/secrets/c"},
		"admin key, no cert":          {"CB_AUTH_ADMIN_TLS_KEY_FILE": "/run/secrets/k"},
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
		s, _, err := LoadSettings(env(map[string]string{"CB_AUTH_ISSUER_SEED_FILE": path}))
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
	s, _, err = LoadSettings(env(map[string]string{"CB_AUTH_JS_URL": "tls://js.example:4222"}))
	if err != nil || s.JSURL != "tls://js.example:4222" {
		t.Fatalf("own URL: %+v, %v", s, err)
	}
	for name, over := range map[string]map[string]string{
		"plain URL off lo":   {"CB_AUTH_JS_URL": "nats://js.example:4222"},
		"URL that is no URL": {"CB_AUTH_JS_URL": "nonsense"},
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
		"no seed":         {"CB_AUTH_JS_SEED_FILE": ""},
		"URL, no seed":    {"CB_AUTH_JS_SEED_FILE": "", "CB_AUTH_JS_URL": "tls://js.example:4222"},
		"blank seed path": {"CB_AUTH_JS_SEED_FILE": "  "},
	} {
		_, _, err := LoadSettings(env(over))
		if !errors.Is(err, ErrConfig) || !strings.Contains(err.Error(), "CB_AUTH_JS_SEED_FILE") {
			t.Errorf("%s: %v", name, err)
		}
	}
	// RunCallout refuses settings built without it, too.
	s, _, _ := LoadSettings(env(nil))
	s.JSSeedFile = ""
	err := RunCallout(context.Background(), s, slog.New(slog.DiscardHandler))
	if !errors.Is(err, ErrConfig) || !strings.Contains(err.Error(), "CB_AUTH_JS_SEED_FILE") {
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
		"CB_AUTH_ISSUER_SEED_FILE": write("issuer", string(acctSeed)), "CB_AUTH_XKEY_SEED_FILE": write("xkey", string(curveSeed)),
		"CB_AUTH_NATS_SEED_FILE": write("nats", string(usrSeed)),
	}
	for name, path := range map[string]string{"missing": filepath.Join(dir, "nope"), "not a seed": write("js", "not a seed\n")} {
		over["CB_AUTH_JS_SEED_FILE"] = path
		s, _, err := LoadSettings(env(over))
		if err != nil {
			t.Fatal(err)
		}
		err = RunCallout(context.Background(), s, slog.New(slog.DiscardHandler))
		if err == nil || !errors.Is(err, ErrConfig) || !strings.Contains(err.Error(), "CB_AUTH_JS_SEED_FILE") {
			t.Errorf("%s: %v", name, err)
		}
		if err != nil && strings.Contains(err.Error(), "not a seed\n") {
			t.Errorf("%s: the file's content is in the error", name)
		}
	}
}

// The console's path (RT-6.43): off by default, its three settings read, and
// a console client that is the bus's own refused.
func TestTheConsolesSettings(t *testing.T) {
	s, _, err := LoadSettings(env(nil))
	if err != nil || s.Callout.Console.ClientID != "" || s.Callout.Console.PersonPrefix != "person-" {
		t.Fatalf("default %+v, %v", s.Callout.Console, err)
	}
	s, _, err = LoadSettings(env(map[string]string{"CB_AUTH_CONSOLE_CLIENT_ID": "bus-console",
		"CB_AUTH_OPERATOR_GROUP": "/BGL_BUS_OPERATORS", "CB_AUTH_PERSON_PREFIX": "op-"}))
	if err != nil || s.Callout.Console != (callout.Console{ClientID: "bus-console", OperatorGroup: "/BGL_BUS_OPERATORS", PersonPrefix: "op-", MaxLogins: 8}) {
		t.Fatalf("set %+v, %v", s.Callout.Console, err)
	}
	if _, _, err := LoadSettings(env(map[string]string{"CB_AUTH_CONSOLE_CLIENT_ID": envMap(nil)["CB_AUTH_KEYCLOAK_CLIENT_ID"]})); !errors.Is(err, ErrConfig) {
		t.Fatalf("the bus's client as the console's: %v", err)
	}
}

// The console's token check refuses the bus's client and the bus's audience
// in `aud`; the logins per person are bounded (RT-6.43).
func TestTheConsolesTokenCheckRefusesBothOfTheBus(t *testing.T) {
	s, _, err := LoadSettings(env(map[string]string{"CB_AUTH_CONSOLE_CLIENT_ID": "bus-console",
		"CB_AUTH_OPERATOR_GROUP": "/ops", "CB_AUTH_KEYCLOAK_AUDIENCE": "bus-api", "CB_AUTH_PERSON_MAX_LOGINS": "3"}))
	if err != nil || s.Callout.Console.MaxLogins != 3 {
		t.Fatalf("%+v, %v", s.Callout.Console, err)
	}
	pc := personTokenConfig(s, s.Token)
	if pc.ClientID != "bus-console" || pc.Audience != "" || !slices.Equal(pc.OtherClients, []string{"agent-bus", "bus-api"}) {
		t.Fatalf("%+v", pc)
	}
	for _, v := range []string{"0", "65", "x"} {
		if _, _, err := LoadSettings(env(map[string]string{"CB_AUTH_PERSON_MAX_LOGINS": v})); !errors.Is(err, ErrConfig) {
			t.Errorf("CB_AUTH_PERSON_MAX_LOGINS=%s: %v", v, err)
		}
	}
	if _, _, err := LoadSettings(env(map[string]string{"CB_AUTH_CONSOLE_CLIENT_ID": "bus-api", "CB_AUTH_KEYCLOAK_AUDIENCE": "bus-api"})); !errors.Is(err, ErrConfig) {
		t.Fatalf("the bus's audience as the console's client: %v", err)
	}
}

// The callout's CA file: an empty new name beside a set old name refuses
// (empty is set, it never falls back), the new name alone is read, and an
// unexpanded placeholder refuses instead of dropping the pinned CA.
func TestTheNATSCAFileSetting(t *testing.T) {
	_, _, err := LoadSettings(env(map[string]string{"CB_AUTH_NATS_CA_FILE": "", "AUTH_NATS_CA_FILE": "/run/ca.pem"}))
	if !errors.Is(err, ErrConfig) || !strings.Contains(err.Error(), "CB_AUTH_NATS_CA_FILE") ||
		!strings.Contains(err.Error(), " AUTH_NATS_CA_FILE") || strings.Contains(err.Error(), "/run/ca.pem") {
		t.Fatalf("empty new name beside the old one: %v", err)
	}
	s, on, err := LoadSettings(env(map[string]string{"CB_AUTH_NATS_CA_FILE": "/run/ca.pem"}))
	if err != nil || !on || s.NATSCAFile != "/run/ca.pem" {
		t.Fatalf("new name alone: %q %v %v", s.NATSCAFile, on, err)
	}
	_, _, err = LoadSettings(env(map[string]string{"CB_AUTH_NATS_CA_FILE": "${CB_AUTH_NATS_CA_FILE}"}))
	if !errors.Is(err, ErrConfig) || !strings.Contains(err.Error(), "CB_AUTH_NATS_CA_FILE") {
		t.Fatalf("placeholder: %v", err)
	}
}

// The old AUTH_ names still configure the callout until the removal release;
// two names of one setting that differ refuse (docs/env-names.md).
func TestOldNamesStillConfigureTheCallout(t *testing.T) {
	old := map[string]string{}
	for k, v := range envMap(nil) {
		k = strings.TrimPrefix(k, "CB_")
		k = strings.Replace(k, "AUTH_MM_", "AUTH_MATTERMOST_", 1)
		old[k] = v
	}
	s, on, err := LoadSettings(envname.New(envname.Map(old), nil))
	if err != nil || !on || s.MattermostURL != "https://chat.example" || s.Callout.Account != "BUS" {
		t.Fatalf("old names: %+v %v %v", s, on, err)
	}
	both := envMap(map[string]string{"AUTH_NATS_SEED_FILE": "/run/secrets/other"})
	if _, _, err := LoadSettings(envname.New(envname.Map(both), nil)); !errors.Is(err, ErrConfig) ||
		!strings.Contains(err.Error(), "CB_AUTH_NATS_SEED_FILE") {
		t.Fatalf("two different values: %v", err)
	}
	// Even without a NATS URL a disagreement refuses.
	if _, _, err := LoadSettings(envname.New(envname.Map(map[string]string{"CB_AUTH_LOBBY": "a", "AUTH_LOBBY": "b"}), nil)); !errors.Is(err, ErrConfig) {
		t.Fatalf("without a NATS URL: %v", err)
	}
}
