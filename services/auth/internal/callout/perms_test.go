package callout

import (
	"strings"
	"testing"
)

// A configured presence prefix is what the credential gets, nothing on the
// default one.
func TestAConfiguredPresencePrefixIsWhatTheCredentialGets(t *testing.T) {
	p := Subjects{Prefix: "bus", Presence: "here"}.permissions("agent-x", "andreas.dembach", []string{"home"})
	for _, want := range []string{"here.andreas~dembach.agent-x", "here.crown.home.agent-x", "bus.ch.home.agent-x", "bus.dm.*.agent-x"} {
		if !p.Pub.Allow.Contains(want) {
			t.Errorf("publish %s missing: %v", want, p.Pub.Allow)
		}
	}
	for _, got := range p.Pub.Allow {
		if strings.HasPrefix(got, "presence.") {
			t.Errorf("publish right on the default presence prefix: %s", got)
		}
	}
	if len(p.Pub.Allow) != 4 || len(p.Sub.Allow) != 1 || !p.Sub.Allow.Contains("deliver.agent-x.>") || len(p.Pub.Deny)+len(p.Sub.Deny) != 0 {
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
	p := Subjects{Prefix: "bus", Presence: "presence"}.permissions("agent-x", "Alice", []string{"home"})
	var got []string
	for _, s := range p.Pub.Allow {
		if strings.HasPrefix(s, "presence.") && !strings.HasPrefix(s, "presence.crown.") {
			got = append(got, s)
		}
	}
	if len(got) != 1 || got[0] != "presence.-.agent-x" {
		t.Errorf("presence rights %v, want only presence.-.agent-x", got)
	}
	p = Subjects{Prefix: "bus", Presence: "presence"}.permissions("agent-x", "alice", nil)
	if !p.Pub.Allow.Contains("presence.alice.agent-x") || p.Pub.Allow.Contains("presence.agent-x") {
		t.Errorf("pub %v", p.Pub.Allow)
	}
}
