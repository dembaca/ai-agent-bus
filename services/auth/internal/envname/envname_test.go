package envname

import (
	"fmt"
	"os"
	"reflect"
	"regexp"
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

// The Go services have no plugin manifest: a value that is exactly ${NAME}
// is a deployment mistake and refuses, naming the variable, under the new
// name and under the old one, never falling back past it.
func TestUnexpandedPlaceholderRefuses(t *testing.T) {
	for _, m := range []map[string]string{
		{"CB_AUTH_NATS_CA_FILE": "${CB_AUTH_NATS_CA_FILE}", "AUTH_NATS_CA_FILE": "/run/ca"},
		{"CB_AUTH_NATS_CA_FILE": "${CB_AUTH_NATS_CA_FILE}"},
		{"AUTH_NATS_CA_FILE": "${AUTH_NATS_CA_FILE}"},
	} {
		e, _ := newEnv(m)
		var bad string
		for k, v := range m {
			if strings.HasPrefix(v, "${") {
				bad = k
			}
		}
		_, _, err := e.Get("CB_AUTH_NATS_CA_FILE")
		if err == nil || !strings.HasPrefix(err.Error(), bad+" ") || strings.Contains(err.Error(), "/run/ca") {
			t.Errorf("%v: want a refusal naming %s and no value, got %v", m, bad, err)
		}
		if e.Check("CB_AUTH_NATS_CA_FILE") == nil {
			t.Errorf("%v: Check passed a placeholder", m)
		}
	}
	// Only the exact form is a placeholder; a value that contains one is a value.
	e, _ := newEnv(map[string]string{"CB_AUTH_LOBBY": "x${CB_AUTH_LOBBY}"})
	if v, _, err := e.Get("CB_AUTH_LOBBY"); err != nil || v != "x${CB_AUTH_LOBBY}" {
		t.Fatalf("got %q %v", v, err)
	}
}

func panics(f func()) (p bool) {
	defer func() { p = recover() != nil }()
	f()
	return false
}

// Value never reads a refusal as unset: before a Check covered the name, or
// for a name its Check refused, it panics.
func TestValueNeedsAPassingCheck(t *testing.T) {
	e, _ := newEnv(map[string]string{"CB_AUTH_LOBBY": "a", "AUTH_LOBBY": "b", "CB_AUTH_ACCOUNT": "BUS"})
	if !panics(func() { e.Value("CB_AUTH_ACCOUNT") }) {
		t.Fatal("Value before any Check did not panic")
	}
	if e.Check("CB_AUTH_ACCOUNT") != nil || e.Value("CB_AUTH_ACCOUNT") != "BUS" {
		t.Fatal("a checked name does not read")
	}
	if !panics(func() { e.Value("CB_AUTH_LOBBY") }) {
		t.Fatal("Value of a name no Check covered did not panic")
	}
	if e.Check("CB_AUTH_LOBBY") == nil {
		t.Fatal("Check passed a disagreement")
	}
	if !panics(func() { e.Value("CB_AUTH_LOBBY") }) {
		t.Fatal("Value read a disagreement")
	}
	if e.Check() != nil || e.Value("CB_AUTH_ACCOUNT") != "BUS" {
		t.Fatal("a later Check forgot a covered name")
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

// The rename table is the login service's table of docs/env-names.md, row
// for row: 48 new names, each with the one old name the doc gives it (#168
// carried CB_AUTH_CONSOLE_METRICS in), or none for a name new since the
// rename, whose row gives it as its own old name (the LDAP id claim, #266).
func TestTheRenamesAreTheDocsTable(t *testing.T) {
	doc, err := os.ReadFile("../../../../docs/env-names.md")
	if err != nil {
		t.Fatal(err)
	}
	_, section, ok := strings.Cut(string(doc), "\n### The login service and authd (48)\n")
	if !ok {
		t.Fatal("docs/env-names.md has no section \"The login service and authd (48)\"")
	}
	section, _, _ = strings.Cut(section, "\n### ")
	row := regexp.MustCompile("(?m)^\\| `([A-Z0-9_]+)` \\| `(CB_[A-Z0-9_]+)` \\|")
	want := map[string][]string{}
	for _, m := range row.FindAllStringSubmatch(section, -1) {
		if m[1] == m[2] { // a name new since the rename: no old spelling
			want[m[2]] = nil
			continue
		}
		want[m[2]] = []string{m[1]}
	}
	if len(want) != 48 {
		t.Fatalf("the doc's login table has %d rows, want 48: %v", len(want), want)
	}
	if n := len(Service()) + len(Authd()); n != 48 {
		t.Fatalf("Service and Authd list %d names, want 48", n)
	}
	if !reflect.DeepEqual(renamed, want) {
		t.Fatalf("the rename table is %v\nthe doc says %v", renamed, want)
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
