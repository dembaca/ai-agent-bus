package directory

import (
	"slices"
	"testing"
	"time"
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

// TrustIndex writes each record and sweeps at most every PruneEvery.
func TestTheTrustIndexSweepsNowAndThen(t *testing.T) {
	s, _ := open(t)
	now := time.Unix(1_800_000_000, 0)
	ix := &TrustIndex{Store: s, Now: func() time.Time { return now }}
	put := func(agent string, exp int64) {
		t.Helper()
		if err := ix.Index(ctx(t), AgentTrust{Agent: agent, Owner: "ann.k", Exp: exp}); err != nil {
			t.Fatal(err)
		}
	}
	count := func() int {
		keys, err := s.keysWith(ctx(t), agentTrustPrefix)
		if err != nil {
			t.Fatal(err)
		}
		return len(keys)
	}
	put("agent-a", now.Unix()+60)
	now = now.Add(2 * time.Minute) // agent-a expired, the sweep not due
	put("agent-b", now.Unix()+900)
	if n := count(); n != 2 {
		t.Fatalf("%d records before the sweep is due", n)
	}
	now = now.Add(PruneEvery)
	put("agent-c", now.Unix()+900)
	if n := count(); n != 2 {
		t.Fatalf("%d records after the sweep", n)
	}
}
