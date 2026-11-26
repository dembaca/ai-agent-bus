package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The slash commands (issue #151 step 3): off without a listen address, and
// with one needing the bridge's own key, the Mattermost admin token, at least
// one command's token file, and TLS off a loopback listener unless the operator
// sets the switch.
func TestTheCommandsSettings(t *testing.T) {
	dir := t.TempDir()
	write := func(name, v string, mode os.FileMode) string {
		p := filepath.Join(dir, name)
		if err := os.WriteFile(p, []byte(v), mode); err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(p, mode); err != nil {
			t.Fatal(err)
		}
		return p
	}
	modeFile := write("mode-tokens", " mod-a , mod-b \n", 0o600)
	openModeFile := write("open-mode-tokens", "mod-a", 0o644)
	key := filepath.Join(dir, "signing-key") // its content is not read here
	load := func(extra map[string]string) (Config, error) {
		env := loadable()
		for k, v := range extra {
			env[k] = v
		}
		return Load(mapEnv(env, nil))
	}

	c, err := load(nil)
	if err != nil || c.CommandsListen != "" || len(c.ModeCommandTokens) != 0 ||
		c.CommandsPlainHTTP || c.RecordsBucket != "BUS_BRIDGE_RECORDS" || c.TrustedProxyHops != 0 {
		t.Fatalf("off: %+v %v", c, err)
	}

	with := func(extra map[string]string) map[string]string {
		base := map[string]string{
			"CB_BRIDGE_COMMANDS_LISTEN_ADDR":    "127.0.0.1:8081",
			"CB_BRIDGE_SIGNING_KEY_FILE":        key,
			"CB_BRIDGE_MM_ADMIN_TOKEN_FILE":     "/run/admin",
			"CB_BRIDGE_MM_BOT_PREFIX":           "bb-",
			"CB_BRIDGE_MODE_COMMAND_TOKEN_FILE": modeFile,
		}
		for k, v := range extra {
			base[k] = v
		}
		return base
	}
	for _, tc := range []struct {
		name  string
		env   map[string]string
		ok    bool
		wants string // a part of the error, or of the accepted config
	}{
		{"no key", with(map[string]string{"CB_BRIDGE_SIGNING_KEY_FILE": ""}), false, "CB_BRIDGE_SIGNING_KEY_FILE"},
		{"no admin token", with(map[string]string{"CB_BRIDGE_MM_ADMIN_TOKEN_FILE": ""}), false, "CB_BRIDGE_MM_ADMIN_TOKEN_FILE"},
		{"no command token", with(map[string]string{"CB_BRIDGE_MODE_COMMAND_TOKEN_FILE": ""}), false, "CB_BRIDGE_MODE_COMMAND_TOKEN_FILE"},
		{"cert without key", with(map[string]string{"CB_BRIDGE_COMMANDS_TLS_CERT_FILE": "/run/cert"}), false, "together, or neither"},
		{"key without cert", with(map[string]string{"CB_BRIDGE_COMMANDS_TLS_KEY_FILE": "/run/key"}), false, "together, or neither"},
		{"world-readable token file", with(map[string]string{"CB_BRIDGE_MODE_COMMAND_TOKEN_FILE": openModeFile}), false, "0600"},
		{"all loopback", with(nil), true, ""},
		{"localhost", with(map[string]string{"CB_BRIDGE_COMMANDS_LISTEN_ADDR": "localhost:8081"}), true, ""},
		{"ipv6 loopback", with(map[string]string{"CB_BRIDGE_COMMANDS_LISTEN_ADDR": "[::1]:8081"}), true, ""},
		{"cert and key", with(map[string]string{"CB_BRIDGE_COMMANDS_TLS_CERT_FILE": "/run/cert",
			"CB_BRIDGE_COMMANDS_TLS_KEY_FILE": "/run/key"}), true, ""},
		{"no host:port", with(map[string]string{"CB_BRIDGE_COMMANDS_LISTEN_ADDR": "8081"}), false, "host:port"},
		{"all interfaces without TLS", with(map[string]string{"CB_BRIDGE_COMMANDS_LISTEN_ADDR": "0.0.0.0:8081"}),
			false, "CB_BRIDGE_COMMANDS_ALLOW_HTTP"},
		{"all interfaces, plain, allowed", with(map[string]string{"CB_BRIDGE_COMMANDS_LISTEN_ADDR": "0.0.0.0:8081",
			"CB_BRIDGE_COMMANDS_ALLOW_HTTP": "true"}), true, "plain"},
	} {
		c, err := load(tc.env)
		if (err == nil) != tc.ok {
			t.Errorf("%s: error %v, want ok %v", tc.name, err, tc.ok)
			continue
		}
		if err != nil {
			if !strings.Contains(err.Error(), tc.wants) {
				t.Errorf("%s: %v does not name %s", tc.name, err, tc.wants)
			}
			continue
		}
		switch tc.wants {
		case "plain":
			// The bridge warns about this one at start.
			if !c.CommandsPlainHTTP || !c.CommandsAllowHTTP {
				t.Errorf("%s: CommandsPlainHTTP %v, allow %v", tc.name, c.CommandsPlainHTTP, c.CommandsAllowHTTP)
			}
		default:
			if c.CommandsPlainHTTP {
				t.Errorf("%s: CommandsPlainHTTP set without the switch", tc.name)
			}
		}
	}

	c, err = load(with(nil))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(c.ModeCommandTokens, "|") != "mod-a|mod-b" {
		t.Fatalf("mode tokens: %v", c.ModeCommandTokens)
	}
	if c, err := load(with(map[string]string{"CB_BRIDGE_RECORDS_BUCKET": "MY_RECORDS"})); err != nil || c.RecordsBucket != "MY_RECORDS" {
		t.Fatalf("records bucket: %+v %v", c, err)
	}
}

// CB_BRIDGE_TRUSTED_PROXY_HOPS is a count of proxies: zero (the default), a
// positive number, never negative or junk.
func TestTheTrustedProxyHopsSetting(t *testing.T) {
	load := func(v string) (Config, error) {
		env := loadable()
		if v != "" {
			env["CB_BRIDGE_TRUSTED_PROXY_HOPS"] = v
		}
		return Load(mapEnv(env, nil))
	}
	for v, want := range map[string]int{"": 0, "0": 0, "1": 1, "3": 3} {
		if c, err := load(v); err != nil || c.TrustedProxyHops != want {
			t.Errorf("%q: %d, %v", v, c.TrustedProxyHops, err)
		}
	}
	for _, v := range []string{"-1", "many"} {
		if _, err := load(v); err == nil || !strings.Contains(err.Error(), "CB_BRIDGE_TRUSTED_PROXY_HOPS") {
			t.Errorf("%q: %v", v, err)
		}
	}
}
