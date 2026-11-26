package config

import (
	"reflect"
	"strings"
	"testing"
)

// mapEnv is an Env over a map: a key that is present is set, also when empty.
func mapEnv(env map[string]string, warn Warn) *Env {
	return NewEnv(func(k string) (string, bool) { v, ok := env[k]; return v, ok }, warn)
}

// warnings records what an Env warns.
type warnings []string

func (w *warnings) warn(msg string, _ ...any) { *w = append(*w, msg) }

// The reading rule of docs/env-names.md: the new name first, else the old
// one with one deprecation warning naming the new name; both set and
// different refuse, naming both and no value; both equal pass with a warning.
func TestTheReadingRule(t *testing.T) {
	for _, tc := range []struct {
		name  string
		env   map[string]string
		want  string
		err   bool
		warns int
	}{
		{"new only", map[string]string{"CB_BRIDGE_X": "a"}, "a", false, 0},
		{"old only", map[string]string{"BRIDGE_X": "a"}, "a", false, 1},
		{"neither", map[string]string{}, "", false, 0},
		{"both equal", map[string]string{"CB_BRIDGE_X": "a", "BRIDGE_X": "a"}, "a", false, 1},
		{"both different", map[string]string{"CB_BRIDGE_X": "s3cr3t-new", "BRIDGE_X": "s3cr3t-old"}, "", true, 0},
		{"new empty is set, old unset", map[string]string{"CB_BRIDGE_X": ""}, "", false, 0},
		{"new empty is set, never falls back", map[string]string{"CB_BRIDGE_X": "", "BRIDGE_X": "a"}, "", true, 0},
		{"old empty is set", map[string]string{"BRIDGE_X": ""}, "", false, 1},
		{"both empty", map[string]string{"CB_BRIDGE_X": "", "BRIDGE_X": ""}, "", false, 1},
	} {
		var w warnings
		v, err := mapEnv(tc.env, w.warn).Get("CB_BRIDGE_X", "BRIDGE_X")
		if (err != nil) != tc.err || v != tc.want {
			t.Errorf("%s: got %q, %v; want %q, error %v", tc.name, v, err, tc.want, tc.err)
		}
		if err != nil {
			if !strings.Contains(err.Error(), "CB_BRIDGE_X") || !strings.Contains(err.Error(), "BRIDGE_X") {
				t.Errorf("%s: the error does not name both names: %v", tc.name, err)
			}
			if strings.Contains(err.Error(), "s3cr3t") {
				t.Errorf("%s: the error names a value: %v", tc.name, err)
			}
		}
		if len(w) != tc.warns {
			t.Errorf("%s: %d warnings, want %d: %q", tc.name, len(w), tc.warns, w)
		}
		for _, m := range w {
			if !strings.Contains(m, "CB_BRIDGE_X") {
				t.Errorf("%s: the warning does not name the new name: %q", tc.name, m)
			}
		}
	}
}

// One deprecation warning per old name and process (one Env), however often
// the name is read.
func TestTheDeprecationWarningComesOnce(t *testing.T) {
	var w warnings
	e := mapEnv(map[string]string{"BRIDGE_X": "a", "BRIDGE_Y": "b"}, w.warn)
	for range 3 {
		if _, err := e.Get("CB_BRIDGE_X", "BRIDGE_X"); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := e.Get("CB_BRIDGE_Y", "BRIDGE_Y"); err != nil {
		t.Fatal(err)
	}
	if len(w) != 2 {
		t.Fatalf("%d warnings, want one per old name: %q", len(w), w)
	}
}

// In 0.103.0 a deployment that still sets the old names starts as before:
// every one of the 19 is read, each with its warning.
func TestTheOldNamesStillWork(t *testing.T) {
	var w warnings
	c, err := Load(mapEnv(map[string]string{
		"BRIDGE_NATS_URL": "tls://nats.example:4222", "BRIDGE_NATS_SEED_FILE": "/run/seed",
		"BRIDGE_CA_FILE": "/run/ca", "BRIDGE_MM_URL": "http://chat.example/",
		"BRIDGE_MM_ALLOW_HTTP": "true", "BRIDGE_MM_TOKEN_FILE": "/run/token",
		"BRIDGE_MM_TEAM": "ai-agents", "BRIDGE_HOMES": "a, b", "BRIDGE_PROPS_PREFIX": "abc_",
		"BRIDGE_KV_BUCKET": "KV", "BRIDGE_SEEN_BUCKET": "SEEN", "BRIDGE_STREAM": "S",
		"BRIDGE_BUS_PREFIX": "b2", "BRIDGE_DELIVER": "d.b", "BRIDGE_INBOX": "_INBOX_b",
		"BRIDGE_LISTEN_ADDR": ":1", "BRIDGE_MM_ADMIN_TOKEN_FILE": "/run/admin",
		"BRIDGE_BOT_PREFIX": "bb-", "BRIDGE_HIDE_BOT_DM": "false",
	}, w.warn))
	if err != nil {
		t.Fatal(err)
	}
	want := Config{NATSURL: "tls://nats.example:4222", SeedFile: "/run/seed", CAFile: "/run/ca",
		MMURL: "http://chat.example", MMAllowHTTP: true, MMPlainHTTP: true, MMToken: "/run/token",
		MMTeam: "ai-agents", Homes: []string{"a", "b"}, Prefix: "abc_", KVBucket: "KV", SeenBucket: "SEEN",
		Listen: ":1", NATSStream: "S", BusPrefix: "b2", Deliver: "d.b", Inbox: "_INBOX_b",
		AdminToken: "/run/admin", BotPrefix: "bb-", HideBotDM: false}
	if !reflect.DeepEqual(c, want) {
		t.Fatalf("got %+v\nwant %+v", c, want)
	}
	if len(w) != 19 {
		t.Fatalf("%d warnings, want 19: %q", len(w), w)
	}
	for _, n := range []string{"CB_BRIDGE_MM_BOT_PREFIX", "CB_BRIDGE_MM_HIDE_BOT_DM"} {
		if !strings.Contains(strings.Join(w, "\n"), n) {
			t.Errorf("no warning names %s", n)
		}
	}
}

// A new and an old name that disagree stop the start, also for a setting
// read after the others.
func TestLoadRefusesNamesThatDisagree(t *testing.T) {
	env := map[string]string{
		"CB_BRIDGE_NATS_URL": "tls://nats.example:4222", "CB_BRIDGE_NATS_SEED_FILE": "/run/seed",
		"CB_BRIDGE_MM_URL": "https://chat.example", "CB_BRIDGE_MM_TOKEN_FILE": "/run/token",
		"CB_BRIDGE_MM_TEAM": "ai-agents", "CB_BRIDGE_HOMES": "nats-pilot",
	}
	if _, err := Load(mapEnv(env, nil)); err != nil {
		t.Fatal(err)
	}
	for k, v := range map[string]string{"BRIDGE_HOMES": "other", "BRIDGE_MM_TEAM": "other", "BRIDGE_MM_TOKEN_FILE": "/run/other"} {
		bad := map[string]string{k: v}
		for kk, vv := range env {
			bad[kk] = vv
		}
		if _, err := Load(mapEnv(bad, nil)); err == nil || !strings.Contains(err.Error(), k) {
			t.Errorf("%s: %v", k, err)
		}
	}
}
