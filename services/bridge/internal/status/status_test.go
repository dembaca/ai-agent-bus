package status

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/presence"
)

var now = time.Date(2026, 11, 27, 12, 0, 0, 0, time.UTC)

type fakeBot struct {
	f    *fakeBots
	name string
}

func (b *fakeBot) rec(s string) { b.f.calls[b.name] = append(b.f.calls[b.name], s) }

func (b *fakeBot) SetStatus(_ context.Context, status string) error {
	b.rec("status " + status)
	return nil
}

func (b *fakeBot) SetCustomStatus(_ context.Context, emoji, text string, expires time.Time) error {
	b.rec(fmt.Sprintf("custom %s|%s|%d", emoji, text, expires.Unix()))
	return b.f.customErr
}

func (b *fakeBot) ClearCustomStatus(context.Context) error {
	b.rec("clear")
	return nil
}

type fakeBots struct {
	calls     map[string][]string
	none      bool
	customErr error
}

func (f *fakeBots) Bot(_ context.Context, name, _ string) Bot {
	if f.none {
		return nil
	}
	return &fakeBot{f, name}
}

type fakeSwitches map[string]bool

func (s fakeSwitches) On(piece int, channel string) bool { return piece == Piece && s[channel] }

type env struct {
	snap   *presence.Snapshot
	shown  *presence.ShownStore
	claims *presence.Claims
	sw     fakeSwitches
	bots   *fakeBots
	m      *Mirror
}

// newEnv has agent "a" with a fresh presence, a claim in "home" and a shown
// status "working · ~home" that lasts 10 minutes.
func newEnv(t *testing.T) *env {
	t.Helper()
	e := &env{snap: presence.New(), shown: &presence.ShownStore{}, sw: fakeSwitches{"home": true},
		bots: &fakeBots{calls: map[string][]string{}}}
	e.claims = &presence.Claims{Clock: func() time.Time { return now }}
	e.claims.Begin()
	e.claims.Observed("home", "a", 2, now.Add(-time.Hour))
	e.claims.Seed(now)
	e.claims.Commit()
	e.snap.Observed("a", "o", "", presence.Online, now.Add(-10*time.Second))
	e.shown.Observed("a", &presence.Shown{Home: "home", Emoji: "hammer_and_wrench", Text: "working · ~home",
		Until: now.Add(10 * time.Minute)}, now)
	e.m = &Mirror{Homes: []string{"home"}, Presence: e.snap, Shown: e.shown, Claims: e.claims,
		Switches: e.sw, Bots: e.bots, Log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	return e
}

func (e *env) want(t *testing.T, name string, want ...string) {
	t.Helper()
	if got := e.bots.calls[name]; !slices.Equal(got, want) {
		t.Fatalf("calls of %s: %q, want %q", name, got, want)
	}
}

func (e *env) pass(t *testing.T, writes int) {
	t.Helper()
	if got := e.m.Pass(context.Background()); got != writes {
		t.Fatalf("Pass wrote %d, want %d (calls %q)", got, writes, e.bots.calls)
	}
}

func unix(d time.Duration) int64 { return now.Add(d).Unix() }

func TestSwitchOffWritesNothing(t *testing.T) {
	e := newEnv(t)
	e.sw["home"] = false
	e.pass(t, 0)
	e.want(t, "a")
}

func TestClaimsNotReadWriteNothing(t *testing.T) {
	e := newEnv(t)
	e.m.Claims = &presence.Claims{Clock: func() time.Time { return now }}
	e.pass(t, 0)
	e.m.Claims = nil
	e.pass(t, 0)
	e.want(t, "a")
}

func TestAFreshAgentShowsItsStatusOnce(t *testing.T) {
	e := newEnv(t)
	e.pass(t, 1)
	e.want(t, "a", "status online", fmt.Sprintf("custom hammer_and_wrench|working · ~home|%d", unix(10*time.Minute)))
	e.pass(t, 0)
	e.want(t, "a", "status online", fmt.Sprintf("custom hammer_and_wrench|working · ~home|%d", unix(10*time.Minute)))
}

func TestAnAgentWithoutClaimOrStatusIsNotShown(t *testing.T) {
	e := newEnv(t)
	e.shown.Observed("a", nil, now.Add(time.Second))
	e.pass(t, 0)
	e.snap.Observed("z", "o", "", presence.Online, now)
	e.shown.Observed("z", &presence.Shown{Home: "home", Emoji: "zzz", Text: "x", Until: now.Add(time.Minute)}, now)
	e.pass(t, 0) // z has no claim
	e.want(t, "a")
	e.want(t, "z")
}

func TestTheCrownShowsOnTheHolderWhoSaysSo(t *testing.T) {
	e := newEnv(t)
	e.shown.Observed("a", &presence.Shown{Home: "home", Emoji: "hammer_and_wrench", Text: "working · ~home",
		Until: now.Add(10 * time.Minute), Crown: true}, now.Add(time.Second))
	e.pass(t, 1)
	e.want(t, "a", "status online", fmt.Sprintf("custom crown|coordinator · working · ~home|%d", unix(10*time.Minute)))
}

func TestNoCrownWhenAnotherFreshAgentHoldsItByTheClaims(t *testing.T) {
	e := newEnv(t)
	e.claims.Observed("home", "b", 1, now.Add(-2*time.Hour))
	e.snap.Observed("b", "o", "", presence.Online, now)
	e.shown.Observed("a", &presence.Shown{Home: "home", Emoji: "ear", Text: "listening · ~home",
		Until: now.Add(10 * time.Minute), Crown: true}, now.Add(time.Second))
	e.pass(t, 1)
	e.want(t, "a", "status online", fmt.Sprintf("custom ear|listening · ~home|%d", unix(10*time.Minute)))
}

func TestNoCrownWithoutTheSessionsOwnWord(t *testing.T) {
	e := newEnv(t) // a holds by the claims, shown.Crown is false
	e.pass(t, 1)
	if c := e.bots.calls["a"]; !strings.Contains(c[1], "custom hammer_and_wrench|") {
		t.Fatalf("calls %q", c)
	}
}

func TestAStatedCrownIsStripped(t *testing.T) {
	e := newEnv(t)
	e.shown.Observed("a", &presence.Shown{Home: "home", Emoji: "zzz", Text: "coordinator · \U0001F451working · ~home",
		Until: now.Add(10 * time.Minute)}, now.Add(time.Second))
	e.pass(t, 1)
	e.want(t, "a", "status online", fmt.Sprintf("custom zzz|working · ~home|%d", unix(10*time.Minute)))
}

func TestAwayShowsAway(t *testing.T) {
	e := newEnv(t)
	e.snap.Observed("a", "o", "", presence.Away, now.Add(-10*time.Second))
	e.pass(t, 1)
	if c := e.bots.calls["a"]; c[0] != "status away" {
		t.Fatalf("calls %q", c)
	}
}

func TestAStalePresenceShowsOfflineOnce(t *testing.T) {
	e := newEnv(t)
	e.snap.Observed("a", "o", "", presence.Online, now.Add(-100*time.Second))
	e.pass(t, 1)
	e.want(t, "a", "status offline", "clear")
	e.pass(t, 0)
	e.want(t, "a", "status offline", "clear")
}

func TestTheSwitchTurnedOffClearsWhatWasSet(t *testing.T) {
	e := newEnv(t)
	e.pass(t, 1)
	e.sw["home"] = false
	e.pass(t, 1)
	e.want(t, "a", "status online", fmt.Sprintf("custom hammer_and_wrench|working · ~home|%d", unix(10*time.Minute)),
		"status offline", "clear")
	e.pass(t, 0)
	e.pass(t, 0)
	if len(e.bots.calls["a"]) != 4 {
		t.Fatalf("calls %q", e.bots.calls["a"])
	}
}

func TestUntilIsCappedAtMaxUntil(t *testing.T) {
	e := newEnv(t)
	e.shown.Observed("a", &presence.Shown{Home: "home", Emoji: "ear", Text: "x", Until: now.Add(5 * time.Hour)}, now.Add(time.Second))
	e.pass(t, 1)
	e.want(t, "a", "status online", fmt.Sprintf("custom ear|x|%d", unix(MaxUntil)))
}

func TestAnEndedStatusIsCleared(t *testing.T) {
	e := newEnv(t)
	e.shown.Observed("a", &presence.Shown{Home: "home", Emoji: "ear", Text: "x", Until: now.Add(-time.Minute)}, now.Add(time.Second))
	e.pass(t, 1)
	e.want(t, "a", "status online", "clear")
}

func TestNoBotWritesNothingAndTriesAgain(t *testing.T) {
	e := newEnv(t)
	e.bots.none = true
	e.pass(t, 0)
	e.want(t, "a")
	e.bots.none = false
	e.pass(t, 1)
	e.want(t, "a", "status online", fmt.Sprintf("custom hammer_and_wrench|working · ~home|%d", unix(10*time.Minute)))
}

func TestAFailedWriteIsTriedAgain(t *testing.T) {
	e := newEnv(t)
	e.bots.customErr = errors.New("boom")
	e.pass(t, 0)
	e.bots.customErr = nil
	e.pass(t, 1)
	custom := fmt.Sprintf("custom hammer_and_wrench|working · ~home|%d", unix(10*time.Minute))
	e.want(t, "a", "status online", custom, "status online", custom)
	e.pass(t, 0)
}

func TestPlain(t *testing.T) {
	for in, want := range map[string]string{
		"coordinator":                   "",
		"coordinator ·":                 "",
		"coordinator · coordinator · x": "x",
		"working · ~home":               "working · ~home",
		"\U0001F451":                    "",
		"a \U0001F451 b":                "a b",
	} {
		if got := Plain(in); got != want {
			t.Errorf("Plain(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestWithHead(t *testing.T) {
	if got := WithHead("coordinator · ", "working · ~home"); got != "coordinator · working · ~home" {
		t.Fatalf("got %q", got)
	}
	long := strings.Repeat("x", 200) + " · ~home"
	got := WithHead("coordinator · ", long)
	if n := len([]rune(got)); n > presence.ShownTextMax || !strings.HasPrefix(got, "coordinator · x") ||
		!strings.HasSuffix(got, " · ~home") {
		t.Fatalf("got %d runes: %q", n, got)
	}
	// exactly at the limit: unchanged
	exact := strings.Repeat("y", presence.ShownTextMax-14)
	if got := WithHead("coordinator · ", exact); got != "coordinator · "+exact {
		t.Fatalf("got %q", got)
	}
}

func TestAStatusIsShownOnlyInTheHomeItNames(t *testing.T) {
	e := newEnv(t)
	e.sw["other"] = true
	e.m.Homes = []string{"other", "home"}
	e.claims.Begin()
	e.claims.Observed("home", "a", 2, now.Add(-time.Hour))
	e.claims.Observed("other", "a", 1, now.Add(-2*time.Hour))
	e.claims.Commit()
	e.pass(t, 1)
	if got := e.m.applied["a"].home; got != "home" {
		t.Fatalf("shown for %q, want home", got)
	}
	e.shown.Observed("a", &presence.Shown{Home: "elsewhere", Emoji: "zzz", Text: "x",
		Until: now.Add(time.Minute)}, now.Add(time.Second))
	e.pass(t, 1) // named no served home it claims in: cleared
	e.want(t, "a", "status online", "custom hammer_and_wrench|working · ~home|"+fmt.Sprint(unix(10*time.Minute)),
		"status offline", "clear")
}

func TestAGracefulStopSetsEveryShownBotOffline(t *testing.T) {
	e := newEnv(t)
	e.pass(t, 1)
	if got := e.m.Stop(context.Background()); got != 1 {
		t.Fatalf("Stop wrote %d, want 1", got)
	}
	calls := e.bots.calls["a"]
	if !slices.Equal(calls[len(calls)-2:], []string{"status offline", "clear"}) {
		t.Fatalf("calls %q", calls)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	e2 := newEnv(t)
	e2.pass(t, 1)
	if got := e2.m.Stop(ctx); got != 0 {
		t.Fatalf("Stop with an ended context wrote %d", got)
	}
}

func TestAStaleNameIsForgottenAndClearedOnce(t *testing.T) {
	e := newEnv(t)
	e.pass(t, 1)
	// the presence and its status go stale together, as the reader stamps both
	e.snap.Observed("a", "o", "", presence.Online, now.Add(-2*time.Minute))
	old := e.shown.Get("a")
	e.shown.Prune(now.Add(time.Hour)) // a newer entry would win: start over with the stale one
	e.shown.Observed("a", old, now.Add(-2*time.Minute))
	e.pass(t, 1) // offline and no status, then forgotten
	if e.shown.Get("a") != nil {
		t.Fatal("the stale name's status is kept")
	}
	e.pass(t, 0)
	if _, ok := e.m.applied["a"]; ok {
		t.Fatal("a cleared bot is still recorded")
	}
}

func TestPlainCaseAndBullets(t *testing.T) {
	for in, want := range map[string]string{
		"Coordinator \u2022 working · ~home":    "working · ~home",
		"COORDINATOR\u2219 coordinator\u2027 x": "x",
		"coordinators meet · ~home":             "coordinators meet · ~home",
		"CoOrDiNaToR":                           "",
	} {
		if got := Plain(in); got != want {
			t.Errorf("Plain(%q) = %q, want %q", in, got, want)
		}
	}
}
