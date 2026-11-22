package callout

import (
	"errors"
	"fmt"
	"time"
)

// Defaults of the configuration.
const (
	DefaultTimeout    = 1500 * time.Millisecond
	DefaultNamePrefix = "agent-"
	DefaultPrefix     = "bus"
)

// Config is the callout's part of the service's configuration. Keys are not
// in it: they come from files the caller reads.
type Config struct {
	// Account is the NATS account the agents land in, the tenant (the
	// user JWT's audience in a server configured with accounts).
	Account string
	// Lobby is the lobby's channel name; empty: no lobby.
	Lobby string
	// NamePrefix comes before the hash in an agent's name.
	NamePrefix string
	// Subjects of the credential.
	Subjects Subjects
	// Timeout bounds one request: the token check, the chat lookups and the
	// answer. It must stay below the server's auth callout timeout.
	Timeout time.Duration
}

// WithDefaults fills the defaults in.
func (c Config) WithDefaults() Config {
	if c.NamePrefix == "" {
		c.NamePrefix = DefaultNamePrefix
	}
	if c.Subjects.Prefix == "" {
		c.Subjects.Prefix = DefaultPrefix
	}
	if c.Timeout == 0 {
		c.Timeout = DefaultTimeout
	}
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
	for what, v := range map[string]string{"name prefix": c.NamePrefix, "subject prefix": c.Subjects.Prefix} {
		if v == "" || !plain(v) {
			return fmt.Errorf("callout: %s %q: letters, digits, - and _ only", what, v)
		}
	}
	if c.Timeout <= 0 || c.Timeout > 10*time.Second {
		return fmt.Errorf("callout: timeout %s: must be above 0 and at most 10s", c.Timeout)
	}
	return nil
}

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
