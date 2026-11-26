package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The homes' servers refuse a props prefix longer than 16 characters
// (a2a.PREFIX_MAX), and the bridge's prop keys must use the homes' prefix: the
// same limit here turns a misconfiguration into a start-up error instead of a
// mismatch discovered later.
func TestThePropsPrefixHasTheHomesLimit(t *testing.T) {
	base := map[string]string{
		"BRIDGE_NATS_URL": "tls://nats.example:4222", "BRIDGE_NATS_SEED_FILE": "/run/seed",
		"BRIDGE_MM_URL": "https://chat.example", "BRIDGE_MM_TOKEN_FILE": "/run/token",
		"BRIDGE_MM_TEAM": "ai-agents", "BRIDGE_HOMES": "nats-pilot",
	}
	load := func(prefix string) (Config, error) {
		env := map[string]string{}
		for k, v := range base {
			env[k] = v
		}
		env["BRIDGE_PROPS_PREFIX"] = prefix
		return Load(func(k string) string { return env[k] })
	}
	if _, err := load("abcdefghijklmno_"); err != nil { // 16 characters: the limit itself
		t.Fatalf("a 16-character prefix: %v", err)
	}
	if _, err := load("abcdefghijklmnop_"); err == nil { // 17
		t.Fatal("a 17-character prefix was accepted")
	}
}

// The bot token goes to Mattermost on every call: an http BRIDGE_MM_URL is
// accepted only for a loopback host or with BRIDGE_MM_ALLOW_HTTP (#131).
func TestPlainHTTPNeedsLoopbackOrTheOptIn(t *testing.T) {
	load := func(mmURL, allow string) (Config, error) {
		env := map[string]string{
			"BRIDGE_NATS_URL": "tls://nats.example:4222", "BRIDGE_NATS_SEED_FILE": "/run/seed",
			"BRIDGE_MM_URL": mmURL, "BRIDGE_MM_TOKEN_FILE": "/run/token",
			"BRIDGE_MM_TEAM": "ai-agents", "BRIDGE_HOMES": "nats-pilot", "BRIDGE_MM_ALLOW_HTTP": allow,
		}
		return Load(func(k string) string { return env[k] })
	}
	for _, tc := range []struct {
		url, allow string
		ok, plain  bool
	}{
		{"https://chat.example", "", true, false},
		{"https://chat.example", "true", true, false},
		{"http://chat.example", "", false, false},
		{"http://chat.example", "false", false, false},
		{"http://chat.example", "true", true, true},
		{"HTTP://chat.example", "true", true, true},
		{"http://chat.example", "yes please", false, false},
		{"http://127.0.0.1:8065", "", true, false},
		{"http://localhost:8065", "true", true, false},
		{"ftp://chat.example", "true", false, false},
		{"chat.example", "", false, false},
		{"http://:8065", "true", false, false},
	} {
		c, err := load(tc.url, tc.allow)
		if (err == nil) != tc.ok {
			t.Errorf("BRIDGE_MM_URL %s, BRIDGE_MM_ALLOW_HTTP %q: got error %v, want ok %v", tc.url, tc.allow, err, tc.ok)
		}
		if err == nil && c.MMAllowHTTP != (tc.allow == "true") {
			t.Errorf("%s %q: MMAllowHTTP %v", tc.url, tc.allow, c.MMAllowHTTP)
		}
		if err == nil && c.MMPlainHTTP != tc.plain {
			t.Errorf("%s %q: MMPlainHTTP %v, want %v (the start-up warning)", tc.url, tc.allow, c.MMPlainHTTP, tc.plain)
		}
	}
}

// RT-6.44: the agents' own bots are off without an admin token; with one the
// prefix is required and must be a username's start, as the hosts decode it.
func TestTheAgentBotSettings(t *testing.T) {
	load := func(extra map[string]string) (Config, error) {
		env := map[string]string{
			"BRIDGE_NATS_URL": "tls://nats.example:4222", "BRIDGE_NATS_SEED_FILE": "/run/seed",
			"BRIDGE_MM_URL": "https://chat.example", "BRIDGE_MM_TOKEN_FILE": "/run/token",
			"BRIDGE_MM_TEAM": "ai-agents", "BRIDGE_HOMES": "nats-pilot",
		}
		for k, v := range extra {
			env[k] = v
		}
		return Load(func(k string) string { return env[k] })
	}
	c, err := load(nil)
	if err != nil || c.AdminToken != "" || c.BotPrefix != "" || !c.HideBotDM {
		t.Fatalf("off: %+v %v", c, err)
	}
	for _, tc := range []struct {
		env map[string]string
		ok  bool
	}{
		{map[string]string{"BRIDGE_MM_ADMIN_TOKEN_FILE": "/run/admin", "BRIDGE_BOT_PREFIX": "bb-"}, true},
		{map[string]string{"BRIDGE_MM_ADMIN_TOKEN_FILE": "/run/admin", "BRIDGE_BOT_PREFIX": "a" + strings.Repeat("b", 31)}, true},
		{map[string]string{"BRIDGE_MM_ADMIN_TOKEN_FILE": "/run/admin"}, false},
		{map[string]string{"BRIDGE_BOT_PREFIX": "bb-"}, false},
		{map[string]string{"BRIDGE_MM_ADMIN_TOKEN_FILE": "/run/admin", "BRIDGE_BOT_PREFIX": "Bb-"}, false},
		{map[string]string{"BRIDGE_MM_ADMIN_TOKEN_FILE": "/run/admin", "BRIDGE_BOT_PREFIX": "-bb"}, false},
		{map[string]string{"BRIDGE_MM_ADMIN_TOKEN_FILE": "/run/admin", "BRIDGE_BOT_PREFIX": "a" + strings.Repeat("b", 32)}, false},
		{map[string]string{"BRIDGE_MM_ADMIN_TOKEN_FILE": "/run/admin", "BRIDGE_BOT_PREFIX": "bb@"}, false},
		{map[string]string{"BRIDGE_HIDE_BOT_DM": "maybe"}, false},
	} {
		if _, err := load(tc.env); (err == nil) != tc.ok {
			t.Errorf("%v: %v", tc.env, err)
		}
	}
	if c, _ := load(map[string]string{"BRIDGE_HIDE_BOT_DM": "false"}); c.HideBotDM {
		t.Fatal("BRIDGE_HIDE_BOT_DM=false kept it on")
	}
}

// RT-6.44: the admin token must not be the posting bot's token.
func TestTheAdminTokenIsNotThePostingToken(t *testing.T) {
	dir := t.TempDir()
	write := func(name, v string) string {
		p := filepath.Join(dir, name)
		if err := os.WriteFile(p, []byte(v), 0o600); err != nil {
			t.Fatal(err)
		}
		return p
	}
	post, same, other, empty := write("post", "s3cr3t\n"), write("same", "s3cr3t"), write("other", "adm"), write("empty", " ")
	for _, tc := range []struct {
		admin string
		ok    bool
	}{{other, true}, {same, false}, {empty, false}, {filepath.Join(dir, "missing"), false}} {
		for k, v := range map[string]string{
			"BRIDGE_NATS_URL": "tls://nats.example:4222", "BRIDGE_NATS_SEED_FILE": "/run/seed",
			"BRIDGE_MM_URL": "https://chat.example", "BRIDGE_MM_TOKEN_FILE": post,
			"BRIDGE_MM_TEAM": "ai-agents", "BRIDGE_HOMES": "nats-pilot",
			"BRIDGE_MM_ADMIN_TOKEN_FILE": tc.admin, "BRIDGE_BOT_PREFIX": "bb-",
		} {
			t.Setenv(k, v)
		}
		c, err := FromEnv()
		if (err == nil) != tc.ok {
			t.Errorf("%s: %v", tc.admin, err)
		}
		if err == nil && c.AdminToken != "adm" {
			t.Errorf("admin token not read")
		}
		if err != nil && strings.Contains(err.Error(), "s3cr3t") {
			t.Errorf("the error names a token: %v", err)
		}
	}
}
