package directory

import (
	"context"
	"errors"
	"log/slog"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go/jetstream"
)

// The trust index an operator's channels are read from (RT-6.43 step 3):
// the channels of an agent that is the person's or trusts the person
// directly, only while its record is unexpired, never another anchor's.
func TestTrustOpensTheAgentsChannelsToTheTrustedOperatorOnly(t *testing.T) {
	s, _ := open(t)
	now := time.Unix(1_800_000_000, 0)
	p := Persons{Store: s, Now: func() time.Time { return now }}
	for sub, name := range map[string]string{"sub-ann": "ann.k", "sub-cat": "cat.k", "sub-an": "ann"} {
		if _, err := s.Login(ctx(t), sub, name, name); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := s.PutChannel(ctx(t), Channel{Name: "open", Owner: "sub-x", Public: true}, 0); err != nil {
		t.Fatal(err)
	}
	// A channel nobody's record opens: an agent that claims to trust ann,
	// without a record the login service wrote, opens nothing.
	if _, err := s.PutChannel(ctx(t), Channel{Name: "claimed", Owner: "sub-x", Members: []string{"sub-x"}}, 0); err != nil {
		t.Fatal(err)
	}
	visible := func(sub string) []string {
		t.Helper()
		got, err := p.Visible(ctx(t), sub)
		if err != nil {
			t.Fatal(err)
		}
		return got
	}
	if got := visible("sub-ann"); !slices.Equal(got, []string{"open"}) {
		t.Fatalf("before any record: %v", got)
	}
	exp := now.Add(15 * time.Minute).Unix()
	if err := s.PutAgentTrust(ctx(t), AgentTrust{Agent: "agent-bob1", Owner: "bob.k", Trusted: []string{"ann.k"},
		Channels: []string{"bobhome", "guestof-x", "Not A Channel"}, Exp: exp}); err != nil {
		t.Fatal(err)
	}
	if err := s.PutAgentTrust(ctx(t), AgentTrust{Agent: "agent-ann1", Owner: "ann.k", Channels: []string{"annhome"}, Exp: exp}); err != nil {
		t.Fatal(err)
	}
	// Trust added: the trusting agent's home and guest channel, and the
	// person's own agent's home.
	if got := visible("sub-ann"); !slices.Equal(got, []string{"annhome", "bobhome", "guestof-x", "open"}) {
		t.Fatalf("trusted: %v", got)
	}
	// Another anchor, and one that is a prefix of the trusted one, see none of it.
	for _, sub := range []string{"sub-cat", "sub-an"} {
		if got := visible(sub); !slices.Equal(got, []string{"open"}) {
			t.Fatalf("%s sees %v", sub, got)
		}
	}
	// Trust removed: the next credential's record replaces the one before.
	if err := s.PutAgentTrust(ctx(t), AgentTrust{Agent: "agent-bob1", Owner: "bob.k", Channels: []string{"bobhome"}, Exp: exp}); err != nil {
		t.Fatal(err)
	}
	if got := visible("sub-ann"); !slices.Equal(got, []string{"annhome", "open"}) {
		t.Fatalf("after the trust was dropped: %v", got)
	}
	// Expired: the record counts for nothing once its credential ran out.
	now = time.Unix(exp, 0)
	if got := visible("sub-ann"); !slices.Equal(got, []string{"open"}) {
		t.Fatalf("after expiry: %v", got)
	}
}

// Expired records are deleted by the sweep, unexpired ones stay; a record
// without agent or owner is refused.
func TestThePruneDropsOnlyExpiredRecords(t *testing.T) {
	s, _ := open(t)
	now := time.Unix(1_800_000_000, 0)
	for agent, exp := range map[string]int64{"agent-old": now.Unix(), "agent-new": now.Unix() + 1} {
		if err := s.PutAgentTrust(ctx(t), AgentTrust{Agent: agent, Owner: "ann.k", Channels: []string{agent[6:]}, Exp: exp}); err != nil {
			t.Fatal(err)
		}
	}
	if err := s.PruneAgentTrust(ctx(t), now); err != nil {
		t.Fatal(err)
	}
	keys, err := s.keysWith(ctx(t), agentTrustPrefix)
	if err != nil || len(keys) != 1 || keys[0] != agentTrustKey("agent-new") {
		t.Fatalf("keys %v, %v", keys, err)
	}
	for _, r := range []AgentTrust{{Owner: "ann.k"}, {Agent: "agent-x"}} {
		if err := s.PutAgentTrust(ctx(t), r); err == nil {
			t.Errorf("%+v was written", r)
		}
	}
}

// Index only writes, even with an expired record in the bucket: the sweep
// never runs inside a login. Sweep, in the background, purges the expired
// records every Every and stops with its context.
func TestTheTrustIndexSweepsInTheBackgroundOnly(t *testing.T) {
	s, _ := open(t)
	now := time.Unix(1_800_000_000, 0)
	ix := &TrustIndex{Store: s, Now: func() time.Time { return now }, Every: 10 * time.Millisecond}
	count := func() int {
		t.Helper()
		keys, err := s.keysWith(ctx(t), agentTrustPrefix)
		if err != nil {
			t.Fatal(err)
		}
		return len(keys)
	}
	for agent, exp := range map[string]int64{"agent-a": now.Unix() - 1, "agent-b": now.Unix() + 900} {
		if err := ix.Index(ctx(t), AgentTrust{Agent: agent, Owner: "ann.k", Exp: exp}); err != nil {
			t.Fatal(err)
		}
	}
	if n := count(); n != 2 {
		t.Fatalf("%d records after Index alone", n)
	}
	sctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		ix.Sweep(sctx)
	}()
	deadline := time.Now().Add(3 * time.Second)
	for count() != 1 {
		if time.Now().After(deadline) {
			t.Fatalf("%d records, the sweep did not run", count())
		}
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("the sweep did not stop with its context")
	}
}

// The prune purges: an ended agent's name keeps one marker, not its
// records.
func TestThePruneLeavesNoHistory(t *testing.T) {
	s, _ := open(t)
	now := time.Unix(1_800_000_000, 0)
	for range 3 {
		if err := s.PutAgentTrust(ctx(t), AgentTrust{Agent: "agent-old", Owner: "ann.k", Exp: now.Unix()}); err != nil {
			t.Fatal(err)
		}
	}
	if err := s.PruneAgentTrust(ctx(t), now); err != nil {
		t.Fatal(err)
	}
	h, err := s.kv.History(ctx(t), agentTrustKey("agent-old"))
	if err != nil || len(h) != 1 || h[0].Operation() != jetstream.KeyValuePurge {
		t.Fatalf("history %v, %v", h, err)
	}
}

// The purge names the revision it read: a record the next issue wrote in
// between stays.
func TestThePurgeKeepsARecordRewrittenMeanwhile(t *testing.T) {
	s, _ := open(t)
	now := time.Unix(1_800_000_000, 0)
	r := AgentTrust{Agent: "agent-a", Owner: "ann.k", Channels: []string{"annhome"}, Exp: now.Unix()}
	if err := s.PutAgentTrust(ctx(t), r); err != nil {
		t.Fatal(err)
	}
	_, rev, ok, err := s.agentTrust(ctx(t), agentTrustKey("agent-a"))
	if err != nil || !ok {
		t.Fatalf("read: %v %v", ok, err)
	}
	r.Exp = now.Unix() + 900
	if err := s.PutAgentTrust(ctx(t), r); err != nil {
		t.Fatal(err)
	}
	if err := s.purgeAt(ctx(t), agentTrustKey("agent-a"), rev); err != nil {
		t.Fatal(err)
	}
	got, _, ok, err := s.agentTrust(ctx(t), agentTrustKey("agent-a"))
	if err != nil || !ok || got.Exp != r.Exp {
		t.Fatalf("the rewritten record: %+v %v %v", got, ok, err)
	}
}

// An owner or trusted anchor that is no anchor refuses the record.
func TestARecordWithJunkAnchorsIsRefused(t *testing.T) {
	s, _ := open(t)
	for _, r := range []AgentTrust{
		{Agent: "agent-x", Owner: "Ann K"},
		{Agent: "agent-x", Owner: "ann.k", Trusted: []string{"bob", ""}},
		{Agent: "agent-x", Owner: "ann.k", Trusted: []string{"../bob"}},
		{Agent: "agent-x", Owner: "ann.k", Trusted: []string{strings.Repeat("b", 65)}},
	} {
		if err := s.PutAgentTrust(ctx(t), r); !errors.Is(err, ErrBadKey) {
			t.Errorf("%+v: %v", r, err)
		}
	}
	if err := s.PutAgentTrust(ctx(t), AgentTrust{Agent: "agent-x", Owner: "ann.k", Trusted: []string{"bob", "sub-cat.1"}}); err != nil {
		t.Fatal(err)
	}
}

// A trust record that cannot be read opens nothing, the others still do; the
// warning names its key, never its content, and the sweep purges it. A
// bucket that cannot be read fails the list closed.
func TestACorruptTrustRecordOpensNothingAndIsSwept(t *testing.T) {
	s, nc := open(t)
	now := time.Unix(1_800_000_000, 0)
	if _, err := s.Login(ctx(t), "sub-ann", "ann.k", "ann.k"); err != nil {
		t.Fatal(err)
	}
	if err := s.PutAgentTrust(ctx(t), AgentTrust{Agent: "agent-ok", Owner: "ann.k", Channels: []string{"annhome"}, Exp: now.Unix() + 900}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.kv.Put(ctx(t), agentTrustKey("agent-bad"), []byte(`{"owner":"ann.k","channels":["secret"`)); err != nil {
		t.Fatal(err)
	}
	var logs strings.Builder
	p := Persons{Store: s, Now: func() time.Time { return now }, Log: slog.New(slog.NewTextHandler(&logs, nil))}
	got, err := p.Visible(ctx(t), "sub-ann")
	if err != nil || !slices.Equal(got, []string{"annhome"}) {
		t.Fatalf("visible %v, %v", got, err)
	}
	if !strings.Contains(logs.String(), agentTrustKey("agent-bad")) || strings.Contains(logs.String(), "secret") {
		t.Errorf("the warning: %s", logs.String())
	}
	if err := s.PruneAgentTrust(ctx(t), now); err != nil {
		t.Fatal(err)
	}
	keys, err := s.keysWith(ctx(t), agentTrustPrefix)
	if err != nil || !slices.Equal(keys, []string{agentTrustKey("agent-ok")}) {
		t.Fatalf("after the sweep: %v, %v", keys, err)
	}
	nc.Close()
	if got, err := p.Visible(ctx(t), "sub-ann"); err == nil {
		t.Fatalf("listed %v without the bucket", got)
	}
}
