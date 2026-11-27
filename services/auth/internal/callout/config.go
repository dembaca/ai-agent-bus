package callout

import (
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/consumers"
)

// Defaults of the configuration.
const (
	DefaultTimeout = 1500 * time.Millisecond
	// DefaultNamePrefix is empty (#274): an agent's NATS name is its hash
	// alone; only the bridge puts its bot prefix before it, for Mattermost.
	DefaultNamePrefix = ""
	DefaultPrefix     = "bus"
	DefaultPresence   = "presence"
	DefaultStats      = consumers.DefaultStatsPrefix
	DefaultReq        = "req" // request subjects, <req>.<kind>.<agent> (ADR-014 phase 5)
	// DeliverPrefix starts the subjects push consumers deliver to; fixed,
	// and neither of the two prefixes may take it.
	DeliverPrefix = "deliver"
)

// Config is the callout's part of the service's configuration. Keys are not
// in it: they come from files the caller reads.
type Config struct {
	// Account is the NATS account the agents land in, the tenant (the
	// user JWT's audience in a server configured with accounts).
	Account string
	// Lobby is the lobby's channel name; empty: no lobby.
	Lobby string
	// NamePrefix comes before the hash in an agent's name; empty by default
	// (#274). The MCP server's natsbus name_prefix must be the same.
	NamePrefix string
	// Subjects of the credential.
	Subjects Subjects
	// Timeout bounds one request: the token check, the chat lookups and the
	// answer. It must stay below the server's auth callout timeout.
	Timeout time.Duration
	// Console is the operator console's path (RT-6.43, person.go); its
	// ClientID empty: no person's credential is issued.
	Console Console
}

// WithDefaults fills the defaults in.
func (c Config) WithDefaults() Config {
	if c.Subjects.Prefix == "" {
		c.Subjects.Prefix = DefaultPrefix
	}
	if c.Subjects.Presence == "" {
		c.Subjects.Presence = DefaultPresence
	}
	if c.Subjects.Stats == "" {
		c.Subjects.Stats = DefaultStats
	}
	if c.Subjects.Req == "" {
		c.Subjects.Req = DefaultReq
	}
	if c.Timeout == 0 {
		c.Timeout = DefaultTimeout
	}
	c.Console = c.Console.withDefaults()
	return c
}

func (c Config) check() error {
	if c.Account == "" {
		return errors.New("callout: the account must be set")
	}
	if c.Lobby != "" {
		if err := checkToken(c.Lobby); err != nil {
			return fmt.Errorf("callout: lobby %q: %w", c.Lobby, err)
		}
	}
	if c.NamePrefix != "" && (!plain(c.NamePrefix) || c.NamePrefix[0] == '_' || c.NamePrefix[0] == '-') {
		return fmt.Errorf("callout: name prefix %q: empty, or a letter or digit, then letters, digits, - and _", c.NamePrefix)
	}
	for what, v := range map[string]string{"subject prefix": c.Subjects.Prefix, "presence prefix": c.Subjects.Presence} {
		if v == "" || !plain(v) {
			return fmt.Errorf("callout: %s %q: letters, digits, - and _ only", what, v)
		}
	}
	for what, v := range map[string]string{"subject prefix": c.Subjects.Prefix, "presence prefix": c.Subjects.Presence} {
		if v == DeliverPrefix {
			return fmt.Errorf("callout: %s %q is the deliver prefix: an agent could publish onto deliver subjects", what, v)
		}
	}
	for what, v := range map[string]string{"subject prefix": c.Subjects.Prefix, "presence prefix": c.Subjects.Presence, "stats prefix": c.Subjects.Stats} {
		if v == inboxPrefix {
			return fmt.Errorf("callout: %s %q is the inbox prefix: an agent could publish onto replies", what, v)
		}
	}
	if c.Subjects.Presence == c.Subjects.Prefix {
		return fmt.Errorf("callout: presence prefix %q must differ from the subject prefix (they would overlap)", c.Subjects.Presence)
	}
	if err := c.Subjects.checkStats(); err != nil {
		return err
	}
	if err := c.Subjects.checkReq(); err != nil {
		return err
	}
	if c.Timeout <= 0 || c.Timeout > 10*time.Second {
		return fmt.Errorf("callout: timeout %s: must be above 0 and at most 10s", c.Timeout)
	}
	return c.checkConsole()
}

// checkStats checks the STATS prefix (RT-6.43): a plain token, not the deliver
// prefix, and neither of the other two: an agent's STATS right would
// otherwise be a post or a presence subject.
func (s Subjects) checkStats() error {
	switch {
	case s.Stats == "" || !plain(s.Stats):
		return fmt.Errorf("callout: stats prefix %q: letters, digits, - and _ only", s.Stats)
	case s.Stats == DeliverPrefix:
		return fmt.Errorf("callout: stats prefix %q is the deliver prefix: an agent could publish onto deliver subjects", s.Stats)
	case s.Stats == s.Prefix || s.Stats == s.Presence:
		return fmt.Errorf("callout: stats prefix %q must differ from the subject and the presence prefix (they would overlap)", s.Stats)
	}
	return nil
}

// checkReq checks the request prefix (ADR-014 phase 5): a plain token, not the
// deliver prefix, not the inbox prefix and none of the other prefixes: an agent's
// request right on `deliver` would let it publish where the bridge answers, and one
// overlapping a post subject would be a channel right.
func (s Subjects) checkReq() error {
	switch {
	case s.Req == "" || !plain(s.Req):
		return fmt.Errorf("callout: request prefix %q: letters, digits, - and _ only", s.Req)
	case s.Req == DeliverPrefix:
		return fmt.Errorf("callout: request prefix %q is the deliver prefix: an agent could publish onto deliver subjects", s.Req)
	case strings.HasPrefix(s.Req, inboxPrefix) || strings.HasPrefix(s.Req, "$"):
		return fmt.Errorf("callout: request prefix %q starts with the inbox prefix %q or $: an agent could publish onto replies or a system subject", s.Req, inboxPrefix)
	case s.Req == s.Prefix || s.Req == s.Presence || s.Req == s.Stats:
		return fmt.Errorf("callout: request prefix %q must differ from the other prefixes (they would overlap)", s.Req)
	}
	return nil
}

// inboxPrefix starts the reply subjects of requests (nats.go's _INBOX): no
// prefix of the bus may take it.
const inboxPrefix = "_INBOX"

func plain(s string) bool {
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-', r == '_':
		default:
			return false
		}
	}
	return true
}
