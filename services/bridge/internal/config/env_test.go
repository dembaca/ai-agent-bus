package config

import (
	"fmt"
	"os"
	"reflect"
	"regexp"
	"strings"
	"testing"
	"time"
)

// mapEnv is an Env over a map: a key that is present is set, also when empty.
func mapEnv(env map[string]string, warn Warn) *Env {
	return NewEnv(func(k string) (string, bool) { v, ok := env[k]; return v, ok }, warn)
}

// warnings records what an Env warns, the message and its args as one line.
type warnings []string

func (w *warnings) warn(msg string, args ...any) {
	*w = append(*w, strings.TrimSpace(fmt.Sprintln(append([]any{msg}, args...)...)))
}

// namesBoth reports whether s names newName and, apart from it, oldName,
// which is a part of newName (BRIDGE_X in CB_BRIDGE_X).
func namesBoth(s, newName, oldName string) bool {
	return strings.Contains(s, newName) && strings.Contains(strings.ReplaceAll(s, newName, ""), oldName)
}

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
		{"old only, secret-looking", map[string]string{"BRIDGE_X": "s3cr3t-old"}, "s3cr3t-old", false, 1},
		{"both equal, secret-looking", map[string]string{"CB_BRIDGE_X": "s3cr3t", "BRIDGE_X": "s3cr3t"}, "s3cr3t", false, 1},
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
			if !namesBoth(err.Error(), "CB_BRIDGE_X", "BRIDGE_X") {
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
			if !namesBoth(m, "CB_BRIDGE_X", "BRIDGE_X") {
				t.Errorf("%s: the warning does not name both names: %q", tc.name, m)
			}
			// Message and args together, never a value: a value may be a secret.
			if strings.Contains(m, "s3cr3t") {
				t.Errorf("%s: the warning names a value: %q", tc.name, m)
			}
		}
	}
}

// A setting that was never renamed gets its own name twice: one read, and no
// deprecation warning about itself.
func TestANameWithoutAnOldOneIsReadOnce(t *testing.T) {
	var w warnings
	looks := 0
	e := NewEnv(func(k string) (string, bool) { looks++; return "v", k == "CB_BRIDGE_X" }, w.warn)
	if v, err := e.Get("CB_BRIDGE_X", "CB_BRIDGE_X"); err != nil || v != "v" || looks != 1 || len(w) != 0 {
		t.Fatalf("got %q, %v, %d lookups, warnings %q", v, err, looks, w)
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
		AdminToken: "/run/admin", BotPrefix: "bb-", HideBotDM: false,
		RecordsBucket: "BUS_BRIDGE_RECORDS", BotKeep: 24 * time.Hour}
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
		if _, err := Load(mapEnv(bad, nil)); err == nil || !namesBoth(err.Error(), "CB_"+k, k) {
			t.Errorf("%s: %v", k, err)
		}
	}
}

// The bridge has no plugin manifest: a value that is exactly ${NAME} is a
// deployment mistake and refuses, naming the variable, under the new name
// and under the old one, never falling back past it.
func TestAnUnexpandedPlaceholderRefuses(t *testing.T) {
	for _, tc := range []struct {
		env map[string]string
		bad string
	}{
		{map[string]string{"CB_BRIDGE_CA_FILE": "${CB_BRIDGE_CA_FILE}", "BRIDGE_CA_FILE": "/run/ca"}, "CB_BRIDGE_CA_FILE"},
		{map[string]string{"CB_BRIDGE_CA_FILE": "${CB_BRIDGE_CA_FILE}"}, "CB_BRIDGE_CA_FILE"},
		{map[string]string{"BRIDGE_CA_FILE": "${BRIDGE_CA_FILE}"}, "BRIDGE_CA_FILE"},
		{map[string]string{"CB_BRIDGE_CA_FILE": "/run/ca", "BRIDGE_CA_FILE": "${CB_BRIDGE_CA_FILE}"}, "BRIDGE_CA_FILE"},
	} {
		_, err := mapEnv(tc.env, nil).Get("CB_BRIDGE_CA_FILE", "BRIDGE_CA_FILE")
		if err == nil || !strings.HasPrefix(err.Error(), tc.bad+" ") || strings.Contains(err.Error(), "/run/ca") {
			t.Errorf("%v: want a refusal naming %s and no value, got %v", tc.env, tc.bad, err)
		}
	}
	// Only the exact form is a placeholder; a value that contains one is a value.
	if v, err := mapEnv(map[string]string{"CB_BRIDGE_X": "x${Y}"}, nil).Get("CB_BRIDGE_X", "BRIDGE_X"); err != nil || v != "x${Y}" {
		t.Fatalf("got %q, %v", v, err)
	}
	// Load stops on it too.
	if _, err := Load(mapEnv(map[string]string{"CB_BRIDGE_CA_FILE": "${CB_BRIDGE_CA_FILE}"}, nil)); err == nil ||
		!strings.Contains(err.Error(), "CB_BRIDGE_CA_FILE") {
		t.Fatalf("Load: %v", err)
	}
}

// The removal release flips oldNamesRemoved, which NewEnv copies into
// refuseOld: an old name alone then refuses, naming both names; the new name
// alone, or both equal, still pass.
func TestTheRemovalReleaseRefusesAnOldNameAlone(t *testing.T) {
	if e := NewEnv(nil, nil); e.refuseOld != oldNamesRemoved {
		t.Fatal("NewEnv does not copy oldNamesRemoved")
	}
	e := mapEnv(map[string]string{"BRIDGE_BOT_PREFIX": "bb-"}, nil)
	e.refuseOld = true
	v, err := e.Get("CB_BRIDGE_MM_BOT_PREFIX", "BRIDGE_BOT_PREFIX")
	if err == nil || v != "" || !strings.HasPrefix(err.Error(), "BRIDGE_BOT_PREFIX ") ||
		!namesBoth(err.Error(), "CB_BRIDGE_MM_BOT_PREFIX", "BRIDGE_BOT_PREFIX") || strings.Contains(err.Error(), "bb-") {
		t.Fatalf("want a refusal naming both names and no value, got %q, %v", v, err)
	}
	for _, tc := range []struct {
		env   map[string]string
		warns int
	}{
		{map[string]string{"CB_BRIDGE_MM_BOT_PREFIX": "bb-"}, 0},
		{map[string]string{"CB_BRIDGE_MM_BOT_PREFIX": "bb-", "BRIDGE_BOT_PREFIX": "bb-"}, 1},
	} {
		var w warnings
		e := mapEnv(tc.env, w.warn)
		e.refuseOld = true
		if v, err := e.Get("CB_BRIDGE_MM_BOT_PREFIX", "BRIDGE_BOT_PREFIX"); err != nil || v != "bb-" {
			t.Errorf("%v: got %q, %v", tc.env, v, err)
		}
		if len(w) != tc.warns {
			t.Errorf("%v: %d warnings, want %d (both equal still warns that the old name can go): %q", tc.env, len(w), tc.warns, w)
		}
	}
}

// loadable is the least environment Load accepts, under the new names.
func loadable() map[string]string {
	return map[string]string{
		"CB_BRIDGE_NATS_URL": "tls://nats.example:4222", "CB_BRIDGE_NATS_SEED_FILE": "/run/seed",
		"CB_BRIDGE_MM_URL": "https://chat.example", "CB_BRIDGE_MM_TOKEN_FILE": "/run/token",
		"CB_BRIDGE_MM_TEAM": "ai-agents", "CB_BRIDGE_HOMES": "nats-pilot",
	}
}

// The bridge's CA file: an empty CB_BRIDGE_CA_FILE beside a set
// BRIDGE_CA_FILE refuses (empty is set, it never falls back to the old
// name), and the new name alone is the CA.
func TestTheCAFileSetting(t *testing.T) {
	env := loadable()
	env["CB_BRIDGE_CA_FILE"], env["BRIDGE_CA_FILE"] = "", "/run/ca.pem"
	_, err := Load(mapEnv(env, nil))
	if err == nil || !namesBoth(err.Error(), "CB_BRIDGE_CA_FILE", "BRIDGE_CA_FILE") ||
		strings.Contains(err.Error(), "/run/ca.pem") {
		t.Fatalf("empty new name beside the old one: %v", err)
	}
	env = loadable()
	env["CB_BRIDGE_CA_FILE"] = "/run/ca.pem"
	var w warnings
	c, err := Load(mapEnv(env, w.warn))
	if err != nil || c.CAFile != "/run/ca.pem" || len(w) != 0 {
		t.Fatalf("new name alone: %q, %v, warnings %q", c.CAFile, err, w)
	}
}

// The 19 renames and the one new name Load reads are exactly the rows of the
// bridge's table in docs/env-names.md, old name to new name (a new name
// without an old spelling has itself in both columns). Load reads each
// setting's new name, then its old one if it has one; the recorded lookups
// pair up.
func TestTheRenamesAreTheDocsTable(t *testing.T) {
	doc, err := os.ReadFile("../../../../docs/env-names.md")
	if err != nil {
		t.Fatal(err)
	}
	_, section, ok := strings.Cut(string(doc), "\n### The bridge (32)\n")
	if !ok {
		t.Fatal("docs/env-names.md has no section \"The bridge (20)\"")
	}
	section, _, _ = strings.Cut(section, "\n### ")
	row := regexp.MustCompile("(?m)^\\| `([A-Z0-9_]+)` \\| `(CB_[A-Z0-9_]+)` \\|")
	want := map[string]string{}
	for _, m := range row.FindAllStringSubmatch(section, -1) {
		want[m[1]] = m[2]
	}
	if len(want) != 32 {
		t.Fatalf("the doc's bridge table has %d rows, want 32: %v", len(want), want)
	}
	var looked []string
	if _, err := Load(NewEnv(func(n string) (string, bool) { looked = append(looked, n); return "", false }, nil)); err == nil {
		t.Fatal("Load accepted an empty environment")
	}
	got := map[string]string{}
	for i := 0; i < len(looked); i++ {
		if !strings.HasPrefix(looked[i], "CB_") {
			t.Fatalf("an old name without its new one: %q", looked)
		}
		if i+1 < len(looked) && !strings.HasPrefix(looked[i+1], "CB_") {
			got[looked[i+1]] = looked[i]
			i++
			continue
		}
		got[looked[i]] = looked[i] // new, no old spelling
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("Load reads %v\nthe doc says %v", got, want)
	}
}
