package presence

import (
	"context"
	"io"
	"log/slog"
	"slices"
	"testing"
	"time"

	"github.com/nats-io/nats.go/jetstream"
)

func TestClaimSubject(t *testing.T) {
	ch, name, ok := ClaimSubject("presence.crown.home.agent-a")
	if !ok || ch != "home" || name != "agent-a" {
		t.Fatalf("got %q %q %v", ch, name, ok)
	}
	for _, s := range []string{"presence.crown.home", "presence.crown.home.a.b", "presence.alice.agent-a",
		"presence.crown.Home.a", "presence.crown.home.-a", "presence.crown..a", "x.crown.home.a"} {
		if _, _, ok := ClaimSubject(s); ok {
			t.Errorf("%q accepted", s)
		}
	}
}

func TestDecodeClaim(t *testing.T) {
	if err := DecodeClaim([]byte(`{"v":1}`)); err != nil {
		t.Fatal(err)
	}
	for _, b := range []string{`not json`, `null`, `[1]`, `"x"`, `3`, ``} {
		if DecodeClaim([]byte(b)) == nil {
			t.Errorf("%q accepted", b)
		}
	}
}

var t0 = time.Date(2026, 11, 27, 10, 0, 0, 0, time.UTC)

func fresh(snap *Snapshot, at time.Time, names ...string) {
	for _, n := range names {
		snap.Observed(n, "owner", "", Online, at)
	}
}

func TestClaimsHolderIsTheFirstFreshClaimant(t *testing.T) {
	c := &Claims{Clock: func() time.Time { return t0 }}
	if c.Read() || c.Holder("home", New(), time.Minute, nil) != "" || !c.Now().IsZero() {
		t.Fatal("claims known before they were read")
	}
	c.Begin()
	c.Observed("home", "b", 2, t0.Add(-2*time.Hour))
	c.Observed("home", "a", 1, t0.Add(-3*time.Hour))
	c.Observed("home", "old", 3, t0.Add(-25*time.Hour)) // older than ClaimKeep
	c.Observed("other", "c", 4, t0.Add(-time.Hour))
	c.Seed(t0)
	if c.Read() {
		t.Fatal("read before Commit")
	}
	c.Commit()
	if !c.Read() || !c.Now().Equal(t0) {
		t.Fatalf("read %v now %v", c.Read(), c.Now())
	}
	if got := c.Claimants("home"); !slices.Equal(got, []string{"a", "b"}) {
		t.Fatalf("claimants %v", got)
	}
	snap := New()
	fresh(snap, t0, "a", "b", "old")
	if got := c.Holder("home", snap, 90*time.Second, nil); got != "a" {
		t.Fatalf("holder %q", got)
	}
	// the first claimant not fresh: the next one
	snap.Observed("a", "owner", "", Online, t0.Add(-time.Hour))
	if got := c.Holder("home", snap, 90*time.Second, nil); got != "b" {
		t.Fatalf("holder %q", got)
	}
	snap.Observed("a", "owner", "", Bye, t0)
	if got := c.Holder("home", snap, 90*time.Second, nil); got != "b" {
		t.Fatalf("holder %q", got)
	}
	// membership
	fresh(snap, t0, "a")
	member := func(ch, name string) bool { return name != "a" }
	if got := c.Holder("home", snap, 90*time.Second, member); got != "b" {
		t.Fatalf("holder %q", got)
	}
	if got := c.Holder("none", snap, 90*time.Second, nil); got != "" {
		t.Fatalf("holder %q", got)
	}
}

func TestClaimsOrderByTimeThenSequenceAndNewestWins(t *testing.T) {
	c := &Claims{Clock: func() time.Time { return t0 }}
	c.Begin()
	c.Observed("home", "x", 5, t0.Add(-time.Hour))
	c.Observed("home", "y", 4, t0.Add(-time.Hour))   // same time, lower sequence first
	c.Observed("home", "x", 1, t0.Add(-2*time.Hour)) // older than the one kept: ignored
	c.Seed(t0)
	c.Commit()
	snap := New()
	fresh(snap, t0, "x", "y")
	if got := c.Holder("home", snap, time.Minute, nil); got != "y" {
		t.Fatalf("holder %q", got)
	}
	c.Observed("home", "x", 9, t0.Add(-90*time.Minute)) // older than the kept one: ignored
	if got := c.Holder("home", snap, time.Minute, nil); got != "y" {
		t.Fatalf("holder %q", got)
	}
	// a replay that no longer has a claim leaves nothing behind
	c.Begin()
	c.Observed("home", "y", 6, t0.Add(-time.Hour))
	c.Commit()
	if got := c.Claimants("home"); !slices.Equal(got, []string{"y"}) {
		t.Fatalf("claimants %v", got)
	}
}

func TestClaimsNowRunsOnByTheLocalClock(t *testing.T) {
	local := t0
	c := &Claims{Clock: func() time.Time { return local }}
	c.Seed(t0)
	local = t0.Add(5 * time.Minute)
	if got := c.Now(); !got.Equal(t0.Add(5 * time.Minute)) {
		t.Fatalf("now %v", got)
	}
	c.Seed(t0.Add(-time.Hour)) // older: no change
	if got := c.Now(); !got.Equal(t0.Add(5 * time.Minute)) {
		t.Fatalf("now %v", got)
	}
}

func TestTheClaimsReaderReplaysTheClaims(t *testing.T) {
	nc, js := natsJS(t, jetstream.StreamConfig{Name: "PRESENCE", Subjects: []string{"presence.>"},
		NoAck: true, MaxMsgsPerSubject: 1, Storage: jetstream.MemoryStorage})
	_ = js
	mustPublish(t, nc, "presence.crown.home.agent-a", `{"v":1}`)
	mustPublish(t, nc, "presence.crown.home.agent-b", `{"v":1}`)
	mustPublish(t, nc, "presence.crown.other.agent-c", `{"v":1}`)
	mustPublish(t, nc, "presence.crown.home.agent-d", `not a json object`)

	claims := &Claims{}
	r := &ClaimsReader{Log: slog.New(slog.NewTextHandler(io.Discard, nil)), Deliver: "deliver.bridge.crown",
		Stream: "PRESENCE", Into: claims, Ready: make(chan struct{}), CaughtUp: make(chan struct{}),
		Heartbeat: 100 * time.Millisecond, Inactive: 500 * time.Millisecond}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- r.Run(ctx, nc) }()
	select {
	case <-r.CaughtUp:
	case <-time.After(10 * time.Second):
		t.Fatal("the claims were not read")
	}
	if !claims.Read() {
		t.Fatal("not read after CaughtUp")
	}
	if got := claims.Claimants("home"); !slices.Equal(got, []string{"agent-a", "agent-b"}) {
		t.Fatalf("claimants %v", got)
	}
	if got := claims.Claimants("other"); !slices.Equal(got, []string{"agent-c"}) {
		t.Fatalf("other %v", got)
	}
	snap := New()
	fresh(snap, time.Now(), "agent-a", "agent-b", "agent-c", "agent-d")
	if got := claims.Holder("home", snap, 90*time.Second, nil); got != "agent-a" {
		t.Fatalf("holder %q", got)
	}
	cancel()
	<-done
}
