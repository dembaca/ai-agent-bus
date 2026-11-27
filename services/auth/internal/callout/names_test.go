package callout

import (
	"strings"
	"testing"
)

// TestReservedNamesAreRefused (#274): without a prefix an agent's name cannot
// be told from the other tokens of its subjects by its start, so the service
// never issues one that is such a token, in any case.
func TestReservedNamesAreRefused(t *testing.T) {
	c := Config{Account: "BUS", Subjects: Subjects{Prefix: "chat", Presence: "pres", Stats: "st", Req: "rq"}}.WithDefaults()
	c.Console = Console{ClientID: "console", OperatorGroup: "/ops", PersonPrefix: "human-"}.withDefaults()
	for _, n := range []string{
		"account", "accounts", "ask", "attest", "bot", "bridge", "bus", "ch", "crown",
		"deliver", "directory", "dm", "hu", "import", "member", "metrics", "notice",
		"person", "planb", "presence", "req", "stats", "v1", "x",
		"chat", "pres", "st", "rq", // the configured prefixes
		"Bridge", "PERSON", // in any case
	} {
		if c.checkName(n) == nil {
			t.Errorf("%q: issued", n)
		}
	}
	for _, n := range []string{
		"_INBOX", "_x", "$SYS", "-a", "a.b", "a*", "a>", "a b", "a\tb", "", strings.Repeat("a", 129),
		"human-alice", // the person prefix
	} {
		if c.checkName(n) == nil {
			t.Errorf("%q: issued", n)
		}
	}
	for _, n := range []string{name("alice", "UABC", ""), "agent-" + name("bob", "UABC", ""), "bridged", "persons"} {
		if err := c.checkName(n); err != nil {
			t.Errorf("%q: %v", n, err)
		}
	}
	for _, r := range reservedNames {
		if r != strings.ToLower(r) || checkToken(r) != nil {
			t.Errorf("reserved %q is no lower-case token", r)
		}
	}
}

// TestNamePrefixIsEmptyByDefault (#274): the default is no prefix; a configured
// one starts with a letter or digit.
func TestNamePrefixIsEmptyByDefault(t *testing.T) {
	c := Config{Account: "BUS"}.WithDefaults()
	if c.NamePrefix != "" || c.check() != nil {
		t.Fatalf("default name prefix %q: %v", c.NamePrefix, c.check())
	}
	for _, ok := range []string{"agent-", "a", "A_1-"} {
		c.NamePrefix = ok
		if err := c.check(); err != nil {
			t.Errorf("%q: %v", ok, err)
		}
	}
	for _, bad := range []string{"_a", "-a", "a.b", "a b", "$a"} {
		c.NamePrefix = bad
		if c.check() == nil {
			t.Errorf("%q: accepted", bad)
		}
	}
}
