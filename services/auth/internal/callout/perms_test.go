package callout

import (
	"strings"
	"testing"
)

// A configured presence prefix is what the credential gets, nothing on the
// default one.
func TestAConfiguredPresencePrefixIsWhatTheCredentialGets(t *testing.T) {
	p := Subjects{Prefix: "bus", Presence: "here", Stats: "st", Req: "rq"}.permissions("agent-x", "andreas.dembach", "home", []string{"home"})
	for _, want := range []string{"here.andreas~dembach.agent-x", "here.crown.home.agent-x", "here.member.home.agent-x", "here.ask.home.agent-x",
		"bus.ch.home.agent-x", "bus.dm.*.agent-x", "st.agent-x", "rq.*.agent-x"} {
		if !p.Pub.Allow.Contains(want) {
			t.Errorf("publish %s missing: %v", want, p.Pub.Allow)
		}
	}
	for _, got := range p.Pub.Allow {
		if strings.HasPrefix(got, "presence.") {
			t.Errorf("publish right on the default presence prefix: %s", got)
		}
	}
	if len(p.Pub.Allow) != 8 || len(p.Sub.Allow) != 1 || !p.Sub.Allow.Contains("deliver.agent-x.>") || len(p.Pub.Deny)+len(p.Sub.Deny) != 0 {
		t.Errorf("pub %v, sub %v", p.Pub, p.Sub)
	}
}

func TestSubjectPrefixesAreChecked(t *testing.T) {
	ok := Config{Account: "BUS"}.WithDefaults()
	if ok.Subjects.Presence != DefaultPresence {
		t.Fatalf("presence default %q", ok.Subjects.Presence)
	}
	if err := ok.check(); err != nil {
		t.Fatal(err)
	}
	for name, edit := range map[string]func(*Config){
		"dot in presence prefix": func(c *Config) { c.Subjects.Presence = "a.b" },
		"wildcard in presence":   func(c *Config) { c.Subjects.Presence = "a*" },
		"presence equals prefix": func(c *Config) { c.Subjects.Presence = c.Subjects.Prefix },
		"presence equals custom": func(c *Config) { c.Subjects.Prefix, c.Subjects.Presence = "x", "x" },
		"presence is deliver":    func(c *Config) { c.Subjects.Presence = DeliverPrefix },
	} {
		c := ok
		edit(&c)
		err := c.check()
		if err == nil || !strings.Contains(err.Error(), "presence prefix") {
			t.Errorf("%s: %v", name, err)
		}
	}
	c := ok
	c.Subjects.Prefix = DeliverPrefix
	if err := c.check(); err == nil || !strings.Contains(err.Error(), "deliver prefix") {
		t.Errorf("subject prefix deliver: %v", err)
	}
}

func TestOwnerToken(t *testing.T) {
	for in, want := range map[string]string{
		"alice": "alice", "andreas.dembach": "andreas~dembach", "a_b-c": "a_b-c",
		"Alice": "-", " alice": "-", "": "-", "al.ice\n": "-", "a~b": "-", ".a": "-",
	} {
		if got := ownerToken(in); got != want {
			t.Errorf("ownerToken(%q) = %q, want %q", in, got, want)
		}
	}
}

// The permission set holds exactly the one presence subject, with the owner.
func TestThePermissionHoldsExactlyOnePresenceSubject(t *testing.T) {
	p := Subjects{Prefix: "bus", Presence: "presence", Req: "req"}.permissions("agent-x", "Alice", "home", []string{"home"})
	var got []string
	for _, s := range p.Pub.Allow {
		if strings.HasPrefix(s, "presence.") && !strings.HasPrefix(s, "presence.crown.") && !strings.HasPrefix(s, "presence.member.") &&
			!strings.HasPrefix(s, "presence.ask.") {
			got = append(got, s)
		}
	}
	if len(got) != 1 || got[0] != "presence.-.agent-x" {
		t.Errorf("presence rights %v, want only presence.-.agent-x", got)
	}
	p = Subjects{Prefix: "bus", Presence: "presence", Req: "req"}.permissions("agent-x", "alice", "home", nil)
	if !p.Pub.Allow.Contains("presence.alice.agent-x") || p.Pub.Allow.Contains("presence.agent-x") {
		t.Errorf("pub %v", p.Pub.Allow)
	}
}

// A crown claim is allowed only in the home (RT-6.38): in a guest channel it is
// refused, so a guest cannot claim and block a home it only visits; the
// membership stays for every granted channel. A home that is not granted gives
// no claim at all.
func TestACrownClaimIsAllowedOnlyInTheHome(t *testing.T) {
	s := Subjects{Prefix: "bus", Presence: "presence", Req: "req"}
	p := s.permissions("agent-x", "alice", "home", []string{"home", "ga"})
	if !p.Pub.Allow.Contains("presence.crown.home.agent-x") || p.Pub.Allow.Contains("presence.crown.ga.agent-x") {
		t.Errorf("crown rights %v", p.Pub.Allow)
	}
	if !p.Pub.Allow.Contains("presence.member.ga.agent-x") || !p.Pub.Allow.Contains("bus.ch.ga.agent-x") {
		t.Errorf("a guest channel keeps its post and membership rights: %v", p.Pub.Allow)
	}
	p = s.permissions("agent-x", "alice", "home", []string{"ga"}) // the home was not granted
	for _, got := range p.Pub.Allow {
		if strings.HasPrefix(got, "presence.crown.") {
			t.Errorf("crown right without the home: %s", got)
		}
	}
}

// The grammar is by token count: an owner named "crown", "member" or "ask" still
// gives a three-token presence subject, the claim, the membership and the ask
// record (RT-6.43) have four.
func TestOwnersNamedCrownAndMemberGiveAThreeTokenPresence(t *testing.T) {
	for _, owner := range []string{"crown", "member", "ask"} {
		p := Subjects{Prefix: "bus", Presence: "presence", Req: "req"}.permissions("agent-x", owner, "home", []string{"home", "ga"})
		want := []string{"presence." + owner + ".agent-x", "presence.crown.home.agent-x", "presence.member.home.agent-x",
			"presence.member.ga.agent-x"}
		for _, w := range want {
			if !p.Pub.Allow.Contains(w) {
				t.Errorf("owner %s: %s missing: %v", owner, w, p.Pub.Allow)
			}
		}
		for _, s := range p.Pub.Allow {
			if strings.HasPrefix(s, "presence.") && len(strings.Split(s, ".")) == 3 && s != "presence."+owner+".agent-x" {
				t.Errorf("owner %s: another three-token presence subject %s", owner, s)
			}
		}
	}
}

// The console's asks and STATS lines (RT-6.43): an agent may publish its one ask
// record only in its home and under its own name, and its STATS line only under
// its own name. A guest channel, another home or another name gets no right, and
// there is no subject per ask.
func TestAnAgentPublishesItsOwnAskRecordAndStatsOnly(t *testing.T) {
	s := Config{Account: "BUS"}.WithDefaults().Subjects
	p := s.permissions("agent-x", "alice", "home", []string{"home", "ga"})
	for _, want := range []string{"presence.ask.home.agent-x", "stats.agent-x"} {
		if !p.Pub.Allow.Contains(want) {
			t.Errorf("publish %s missing: %v", want, p.Pub.Allow)
		}
	}
	var asks, stats []string
	for _, got := range p.Pub.Allow {
		switch {
		case strings.HasPrefix(got, "presence.ask."):
			asks = append(asks, got)
		case strings.HasPrefix(got, "stats."):
			stats = append(stats, got)
		}
		if strings.Contains(got, "*") && !strings.HasPrefix(got, "bus.dm.*.") && !strings.HasPrefix(got, s.Req+".*.") || strings.Contains(got, ">") {
			t.Errorf("a wildcard right: %s", got)
		}
	}
	if len(asks) != 1 || len(stats) != 1 {
		t.Errorf("ask rights %v, stats rights %v: want exactly one each", asks, stats)
	}
	p = s.permissions("agent-x", "alice", "home", []string{"ga"}) // the home was not granted
	for _, got := range p.Pub.Allow {
		if strings.HasPrefix(got, "presence.ask.") {
			t.Errorf("ask right without the home: %s", got)
		}
	}
	if !p.Pub.Allow.Contains("stats.agent-x") {
		t.Errorf("the STATS line does not depend on the home: %v", p.Pub.Allow)
	}
}

// The request subjects (CB_AUTH_REQ_PREFIX, ADR-014 phase 5): an agent publishes
// a history, file or header request only under its own name, and never on a
// deliver subject, which is where the bridge answers.
func TestAnAgentPublishesRequestsOnlyUnderItsOwnName(t *testing.T) {
	s := Config{Account: "BUS"}.WithDefaults().Subjects
	if s.Req != DefaultReq {
		t.Fatalf("request default %q", s.Req)
	}
	p := s.permissions("agent-x", "alice", "home", []string{"home"})
	for _, subject := range []string{"req.history.agent-x", "req.file.agent-x", "req.header.agent-x"} {
		allowed := false
		for _, f := range p.Pub.Allow {
			if match(f, subject) {
				allowed = true
			}
		}
		if !allowed {
			t.Errorf("publish %s missing: %v", subject, p.Pub.Allow)
		}
	}
	for _, subject := range []string{"req.history.agent-y", "req.file.agent-y",
		"deliver.agent-x.req.r1", "deliver.*.req.r1"} {
		for _, f := range p.Pub.Allow {
			if match(f, subject) {
				t.Errorf("publish %s allows %s", f, subject)
			}
		}
	}
}

// The STATS prefix (CB_AUTH_STATS_PREFIX, RT-6.43) is a plain token of its own: not
// the deliver prefix and neither of the other two.
func TestTheStatsPrefixIsChecked(t *testing.T) {
	ok := Config{Account: "BUS"}.WithDefaults()
	if ok.Subjects.Stats != DefaultStats {
		t.Fatalf("stats default %q", ok.Subjects.Stats)
	}
	for name, edit := range map[string]func(*Config){
		"dot":             func(c *Config) { c.Subjects.Stats = "a.b" },
		"wildcard":        func(c *Config) { c.Subjects.Stats = ">" },
		"deliver":         func(c *Config) { c.Subjects.Stats = DeliverPrefix },
		"subject prefix":  func(c *Config) { c.Subjects.Stats = c.Subjects.Prefix },
		"presence prefix": func(c *Config) { c.Subjects.Stats = c.Subjects.Presence },
		"custom presence": func(c *Config) { c.Subjects.Presence, c.Subjects.Stats = "x", "x" },
	} {
		c := ok
		edit(&c)
		if err := c.check(); err == nil || !strings.Contains(err.Error(), "stats prefix") {
			t.Errorf("%s: %v", name, err)
		}
	}
}

// The request prefix (CB_AUTH_REQ_PREFIX, ADR-014 phase 5) is a plain token of its
// own: not the deliver prefix, and none of the other prefixes.
func TestTheRequestPrefixIsChecked(t *testing.T) {
	ok := Config{Account: "BUS"}.WithDefaults()
	if ok.Subjects.Req != DefaultReq {
		t.Fatalf("request default %q", ok.Subjects.Req)
	}
	for name, edit := range map[string]func(*Config){
		"dot":             func(c *Config) { c.Subjects.Req = "a.b" },
		"wildcard":        func(c *Config) { c.Subjects.Req = ">" },
		"deliver":         func(c *Config) { c.Subjects.Req = DeliverPrefix },
		"subject prefix":  func(c *Config) { c.Subjects.Req = c.Subjects.Prefix },
		"presence prefix": func(c *Config) { c.Subjects.Req = c.Subjects.Presence },
		"stats prefix":    func(c *Config) { c.Subjects.Req = c.Subjects.Stats },
		"inbox suffix":    func(c *Config) { c.Subjects.Req = "_INBOX_bridge" },
		"dollar":          func(c *Config) { c.Subjects.Req = "$req" },
		"custom presence": func(c *Config) { c.Subjects.Presence, c.Subjects.Req = "x", "x" },
	} {
		c := ok
		edit(&c)
		if err := c.check(); err == nil || !strings.Contains(err.Error(), "request prefix") {
			t.Errorf("%s: %v", name, err)
		}
	}
}

// No prefix may be the inbox prefix: an agent's rights would then reach reply
// subjects.
func TestNoPrefixIsTheInbox(t *testing.T) {
	ok := Config{Account: "BUS"}.WithDefaults()
	for name, edit := range map[string]func(*Config){
		"subject":  func(c *Config) { c.Subjects.Prefix = "_INBOX" },
		"presence": func(c *Config) { c.Subjects.Presence = "_INBOX" },
		"stats":    func(c *Config) { c.Subjects.Stats = "_INBOX" },
		"request":  func(c *Config) { c.Subjects.Req = "_INBOX" },
	} {
		c := ok
		edit(&c)
		if err := c.check(); err == nil || !strings.Contains(err.Error(), "inbox prefix") {
			t.Errorf("%s: %v", name, err)
		}
	}
}

// RT-6.45: no agent may publish an imported STATS line, <stats>.import.<bot>:
// three tokens, where an agent's one STATS right has two, whatever channels
// its credential carries and whatever its name (even "import", which no
// agent's name can be: a name is the name prefix and 26 characters).
func TestNoAgentMayPublishAnImportedStatsLine(t *testing.T) {
	c := Config{Account: "BUS"}.WithDefaults()
	for _, chans := range [][]string{nil, {"home"}, {"home", "ga", "gb"}} {
		for _, agent := range []string{"agent-x", "import"} {
			p := c.Subjects.permissions(agent, "alice", "home", chans)
			for _, subject := range []string{"stats.import.agent-x", "stats.import.bot", "stats.import.import"} {
				for _, f := range p.Pub.Allow {
					if match(f, subject) {
						t.Errorf("%s, channels %v: publish %s allows %s", agent, chans, f, subject)
					}
				}
			}
		}
	}
}

// Direct messages between agents (ADR-014 phase 5, piece 3; RT-6.36 step 7): an
// agent may send to any recipient's direct subject under its own name - the
// recipient token is a wildcard - and may read only its own deliver subjects.
// So a third agent can neither publish a DM under another's name nor read a DM
// between two others; it can only be the recipient of one addressed to it.
func TestAnAgentMaySendADirectMessageToAnyAgentAndReadOnlyItsOwn(t *testing.T) {
	s := Subjects{Prefix: "bus", Presence: "presence"}
	a := s.permissions("agent-a", "alice", "home", []string{"home"})
	if !a.Pub.Allow.Contains("bus.dm.*.agent-a") {
		t.Fatalf("no right to send a direct message: %v", a.Pub.Allow)
	}
	// The recipient is free (a DM to any agent), the sender is pinned.
	if a.Pub.Allow.Contains("bus.dm.*.agent-b") || a.Pub.Allow.Contains("bus.dm.agent-a.*") {
		t.Errorf("a direct-message right beyond its own name as sender: %v", a.Pub.Allow)
	}
	if !a.Sub.Allow.Contains("deliver.agent-a.>") {
		t.Errorf("no right to read its own deliver subjects: %v", a.Sub.Allow)
	}
	for _, subject := range a.Sub.Allow {
		if strings.Contains(subject, "deliver.agent-b") || strings.HasPrefix(subject, "bus.dm.") {
			t.Errorf("another agent's direct message is readable: %s", subject)
		}
	}
	// A second credential: the same wildcard recipient, its own sender token.
	b := s.permissions("agent-b", "bob", "home", []string{"home"})
	if !b.Pub.Allow.Contains("bus.dm.*.agent-b") || b.Pub.Allow.Contains("bus.dm.*.agent-a") {
		t.Errorf("agent-b's direct-message rights: %v", b.Pub.Allow)
	}
}
