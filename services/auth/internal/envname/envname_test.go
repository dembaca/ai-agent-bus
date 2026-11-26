package envname

import (
	"fmt"
	"strings"
	"testing"
)

// recorder collects the warnings of an Env.
type recorder struct{ lines []string }

func (r *recorder) warn(msg string, args ...any) {
	r.lines = append(r.lines, fmt.Sprint(append([]any{msg}, args...)...))
}

func newEnv(m map[string]string) (*Env, *recorder) {
	r := &recorder{}
	return New(Map(m), r.warn), r
}

func TestNewNameOnly(t *testing.T) {
	e, r := newEnv(map[string]string{"CB_AUTH_LOBBY": "lobby"})
	v, ok, err := e.Get("CB_AUTH_LOBBY")
	if err != nil || !ok || v != "lobby" || len(r.lines) != 0 {
		t.Fatalf("got %q %v %v, warnings %q", v, ok, err, r.lines)
	}
}

func TestUnset(t *testing.T) {
	e, r := newEnv(nil)
	if v, ok, err := e.Get("CB_AUTH_LOBBY"); err != nil || ok || v != "" || len(r.lines) != 0 {
		t.Fatalf("got %q %v %v, warnings %q", v, ok, err, r.lines)
	}
}

func TestOldNameOnlyWarnsOnce(t *testing.T) {
	e, r := newEnv(map[string]string{"AUTH_MATTERMOST_URL": "https://chat.example"})
	for range 3 {
		v, ok, err := e.Get("CB_AUTH_MM_URL")
		if err != nil || !ok || v != "https://chat.example" {
			t.Fatalf("got %q %v %v", v, ok, err)
		}
	}
	if err := e.Check(Service()...); err != nil {
		t.Fatal(err)
	}
	if len(r.lines) != 1 || !strings.Contains(r.lines[0], "CB_AUTH_MM_URL") || !strings.Contains(r.lines[0], "AUTH_MATTERMOST_URL") {
		t.Fatalf("want one warning naming both names, got %q", r.lines)
	}
}

func TestBothEqualPassWithAWarning(t *testing.T) {
	e, r := newEnv(map[string]string{"CB_AUTH_BRAKE_FILE": "/run/brake", "AUTH_BRAKE_FILE": "/run/brake"})
	v, _, err := e.Get("CB_AUTH_BRAKE_FILE")
	if err != nil || v != "/run/brake" {
		t.Fatalf("got %q %v", v, err)
	}
	if len(r.lines) != 1 || !strings.Contains(r.lines[0], "remove") || !strings.Contains(r.lines[0], "AUTH_BRAKE_FILE") {
		t.Fatalf("want one warning that the old name can go, got %q", r.lines)
	}
}

func TestBothDifferentRefuseWithoutTheValues(t *testing.T) {
	e, _ := newEnv(map[string]string{"CB_AUTH_NATS_SEED_FILE": "/run/a-secret-path", "AUTH_NATS_SEED_FILE": "/run/other"})
	_, _, err := e.Get("CB_AUTH_NATS_SEED_FILE")
	if err == nil {
		t.Fatal("two different values were accepted")
	}
	msg := err.Error()
	if !strings.Contains(msg, "CB_AUTH_NATS_SEED_FILE") || !strings.Contains(msg, " AUTH_NATS_SEED_FILE") ||
		strings.Contains(msg, "a-secret-path") || strings.Contains(msg, "/run/other") {
		t.Fatalf("error must name both names and no value: %q", msg)
	}
	if e.Value("CB_AUTH_NATS_SEED_FILE") != "" {
		t.Fatal("Value read a disagreement as set")
	}
}

// Empty counts as set: it never falls back, and next to a value it disagrees.
func TestEmptyCountsAsSet(t *testing.T) {
	e, r := newEnv(map[string]string{"CB_AUTH_APPROVER_GROUP": ""})
	if v, ok, err := e.Get("CB_AUTH_APPROVER_GROUP"); err != nil || !ok || v != "" || len(r.lines) != 0 {
		t.Fatalf("got %q %v %v", v, ok, err)
	}
	e, _ = newEnv(map[string]string{"CB_AUTH_APPROVER_GROUP": "", "AUTH_APPROVER_GROUP": "/approvers"})
	if _, _, err := e.Get("CB_AUTH_APPROVER_GROUP"); err == nil {
		t.Fatal("an empty new name next to a set old one was not a disagreement")
	}
	e, _ = newEnv(map[string]string{"AUTH_APPROVER_GROUP": ""})
	if _, ok, err := e.Get("CB_AUTH_APPROVER_GROUP"); err != nil || !ok {
		t.Fatalf("an empty old name is set: %v %v", ok, err)
	}
}

func TestUnexpandedPlaceholderCountsAsUnset(t *testing.T) {
	e, _ := newEnv(map[string]string{"CB_AUTH_LOBBY": "${CB_AUTH_LOBBY}", "AUTH_LOBBY": "lobby"})
	if v, _, err := e.Get("CB_AUTH_LOBBY"); err != nil || v != "lobby" {
		t.Fatalf("got %q %v", v, err)
	}
}

// Three names for one setting: every one that is set must agree.
func TestAliasesMustAllAgree(t *testing.T) {
	renamed["CB_TEST_ALIAS"] = []string{"TEST_ALIAS", "TEST_ALIAS_OLDEST"}
	defer delete(renamed, "CB_TEST_ALIAS")
	e, r := newEnv(map[string]string{"TEST_ALIAS": "x", "TEST_ALIAS_OLDEST": "x"})
	if v, _, err := e.Get("CB_TEST_ALIAS"); err != nil || v != "x" || len(r.lines) != 2 {
		t.Fatalf("got %q %v, warnings %q", v, err, r.lines)
	}
	e, _ = newEnv(map[string]string{"CB_TEST_ALIAS": "x", "TEST_ALIAS_OLDEST": "y"})
	if _, _, err := e.Get("CB_TEST_ALIAS"); err == nil || !strings.Contains(err.Error(), "TEST_ALIAS_OLDEST") {
		t.Fatalf("want a disagreement naming the oldest alias, got %v", err)
	}
}

// The removal release flips one switch: an old name alone then refuses.
func TestRemovalReleaseRefusesAnOldNameAlone(t *testing.T) {
	e, _ := newEnv(map[string]string{"AUTH_DIRECTORY_IMPORTED": "true"})
	e.refuseOld = true
	if _, _, err := e.Get("CB_AUTH_DIRECTORY_IMPORTED"); err == nil ||
		!strings.Contains(err.Error(), "CB_AUTH_DIRECTORY_IMPORTED") || !strings.HasPrefix(err.Error(), "AUTH_DIRECTORY_IMPORTED ") {
		t.Fatalf("want a refusal naming both names, got %v", err)
	}
	e, _ = newEnv(map[string]string{"AUTH_DIRECTORY_IMPORTED": "true", "CB_AUTH_DIRECTORY_IMPORTED": "true"})
	e.refuseOld = true
	if _, _, err := e.Get("CB_AUTH_DIRECTORY_IMPORTED"); err != nil {
		t.Fatalf("equal values still pass: %v", err)
	}
}

func TestCheckReportsEveryDisagreement(t *testing.T) {
	e, _ := newEnv(map[string]string{"CB_AUTH_LOBBY": "a", "AUTH_LOBBY": "b", "CB_AUTH_ACCOUNT": "A", "AUTH_ACCOUNT": "B"})
	err := e.Check(Service()...)
	if err == nil || !strings.Contains(err.Error(), "CB_AUTH_LOBBY") || !strings.Contains(err.Error(), "CB_AUTH_ACCOUNT") {
		t.Fatalf("got %v", err)
	}
}

// The table is the mapping of docs/env-names.md: 44 names, each CB_ plus the
// old name except the three Mattermost ones.
func TestTheTable(t *testing.T) {
	if n := len(Service()) + len(Authd()); n != 44 {
		t.Fatalf("%d names, want 44", n)
	}
	mm := map[string]string{"CB_AUTH_MM_URL": "AUTH_MATTERMOST_URL", "CB_AUTH_MM_TEAM": "AUTH_MATTERMOST_TEAM", "CB_AUTH_MM_TOKEN_FILE": "AUTH_MATTERMOST_TOKEN_FILE"}
	for n, old := range renamed {
		want := strings.TrimPrefix(n, "CB_")
		if m, ok := mm[n]; ok {
			want = m
		}
		if len(old) != 1 || old[0] != want {
			t.Errorf("%s: old %v, want %s", n, old, want)
		}
	}
}

func TestAnUnknownNamePanics(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatal("no panic")
		}
	}()
	New(Map(nil), nil).Get("CB_AUTH_TYPO")
}
