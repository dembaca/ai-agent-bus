package callout

import (
	"fmt"
	"regexp"
	"strings"
)

// NameHashLen is the length of an agent name's hash part (#274): the first 25
// characters of the lower-case base32 of 16 bytes of SHA-256 (125 bits). Not
// 26, the length of a Mattermost id: a hosted session holds Mattermost ids and
// NATS names side by side and tells them apart by their form, and a name
// without a prefix must never read as an id. The MCP server's
// natsbus.NAME_HASH_LEN and the bridge's presence.LinkTag use the same length.
const NameHashLen = 25

// nameRE is an agent name's form, the consumers' agentRE: no ".", "*", ">" or
// whitespace, and no "_", "$" or "-" first.
var nameRE = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`)

// reservedNames are the subject tokens that stand where an agent's name stands
// in some subject, or beside it (perms.go, the consumers' filters, the bridge):
// <bus>.ch|dm, <presence>.crown|ask|member|attest|bot|bridge|person|account|
// planb|directory|notice|hu (and notice.<owner>.copied|moved.v1, account.mattermost.
// <id>.v1), <stats>.import, deliver.<name>.x|presence|dm|planb|
// hu|notice|metrics|accounts, the console's service mark console-metrics, and the records' version v1. Since #274 an agent
// name has no prefix that sets it apart from them, so a name equal to one is
// refused (checkName). A hash never is one; a configured prefix could make one.
// The MCP server's natsbus.RESERVED_NAMES is the same list.
var reservedNames = []string{
	"account", "accounts", "ask", "attest", "bot", "bridge", "bus", "ch",
	"console-metrics", "copied", "crown", "deliver", "directory", "dm", "hu",
	"import", "mattermost", "member", "metrics", "moved", "notice", "person",
	"planb", "presence", "req", "stats", "v1", "x",
}

// checkName refuses an agent name a subject could read as another token
// (#274): one not in nameRE's form, one of the reserved tokens or the
// configured prefixes (in any case), and one that starts with the person
// prefix while the console's path is on (RT-6.42).
func (c Config) checkName(n string) error {
	if !nameRE.MatchString(n) {
		return fmt.Errorf("callout: agent name %q: not a subject token of an agent", n)
	}
	for _, r := range c.reserved() {
		if strings.EqualFold(n, r) {
			return fmt.Errorf("callout: agent name %q is a reserved subject token", n)
		}
	}
	if p := c.Console.PersonPrefix; c.Console.ClientID != "" && p != "" && strings.HasPrefix(n, p) {
		return fmt.Errorf("callout: agent name %q starts with the person prefix %q", n, p)
	}
	return nil
}

// reserved is reservedNames with the configured prefixes.
func (c Config) reserved() []string {
	return append(append([]string{}, reservedNames...),
		c.Subjects.Prefix, c.Subjects.Presence, c.Subjects.Stats, c.Subjects.Req, DeliverPrefix, inboxPrefix)
}
