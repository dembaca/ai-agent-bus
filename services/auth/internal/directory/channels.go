package directory

import (
	"context"
	"errors"
	"log/slog"
)

// ByName is the membership lookup by chat username that the directory
// replaces: Mattermost's (mattermost.Client), until the import has run.
type ByName interface {
	Memberships(ctx context.Context, username string, channels []string) (map[string]bool, error)
}

// ErrNoSub: after the import membership needs the identity's `sub`.
var ErrNoSub = errors.New("directory: membership needs the identity's sub")

// Channels is callout.Channels over the directory (ADR-016 point 2): until
// the import has written meta.imported, Mattermost (Fallback) answers by
// username, as before; from then on the directory answers by `sub`. No
// switch to set: the import decides, once, as its last step (decision 2).
type Channels struct {
	Store    *Store
	Fallback ByName
}

// Memberships is the lookup by username alone: Mattermost's until the
// import; after it the directory needs the `sub` and refuses.
func (c Channels) Memberships(ctx context.Context, username string, channels []string) (map[string]bool, error) {
	return c.MembershipsFor(ctx, username, "", channels)
}

// MembershipsFor answers for the identity: by username through Mattermost
// before the import, by `sub` through the directory after it. An error is an
// unknown answer, never a "no".
func (c Channels) MembershipsFor(ctx context.Context, username, sub string, channels []string) (map[string]bool, error) {
	imported, err := c.Store.Imported(ctx)
	if err != nil {
		return nil, err
	}
	if !imported {
		return c.Fallback.Memberships(ctx, username, channels)
	}
	if sub == "" {
		return nil, ErrNoSub
	}
	return c.Store.Memberships(ctx, sub, channels)
}

// Anchors gives the callout each identity's anchor (decisions 7 and 9).
type Anchors struct {
	Store *Store
	// Log gets the notice of a moved chat account name; nil is slog.Default().
	Log *slog.Logger
}

// Anchor records the login of `sub` as `display` and returns its anchor:
// for an identity the directory knows, the one it holds; for a new one,
// today's name `display` when it is free, before and after the import, else
// its `sub` (RT-6.39 step 2, #86). The host computes the agent's name and its
// presence token from `preferred_username`, so an anchor equal to it is what
// lets a new identity's home start over NATS. A name as anchor grants
// nothing: membership, the account index and the import's claims go by `sub`.
// The import's mark is still read, so a lost bucket refuses the login.
func (a Anchors) Anchor(ctx context.Context, sub, display string) (string, error) {
	if _, err := a.Store.Imported(ctx); err != nil {
		return "", err
	}
	id, err := a.Store.Login(ctx, sub, display, display)
	if err != nil {
		return "", err
	}
	if err := a.Store.redeem(ctx, sub, display); err != nil {
		return "", err
	}
	return id.Anchor, nil
}

// Account keeps the Mattermost account index current from a verified login:
// the caller (the callout) calls it only when the login's chat account is
// proven to be the token's (token.Identity.ChatOpenID). Before the import it
// writes nothing. An error fails the login: a stale index could hand trust
// meant for the new holder of a name to the old one. A name that moves from
// another identity is logged.
func (a Anchors) Account(ctx context.Context, sub, name string) error {
	imported, err := a.Store.Imported(ctx)
	if err != nil {
		return err
	}
	if !imported {
		return nil
	}
	m, err := a.Store.ClaimAccount(ctx, sub, "mattermost", name)
	log := a.Log
	if log == nil {
		log = slog.Default()
	}
	if m.Released != "" {
		log.Warn("directory: an unfinished move of a chat account name finished", "product", "mattermost",
			"name", name, "released", Short(m.Released))
	}
	if m.From != "" { // reported once the key moved, also when the records did not follow
		log.Warn("directory: chat account name moved", "product", "mattermost", "name", name,
			"from", Short(m.From), "to", Short(sub), "complete", err == nil)
	}
	return err
}

// Short keeps the first 8 characters of a `sub` for a log line.
func Short(sub string) string {
	if len(sub) <= 8 {
		return sub
	}
	return sub[:8] + "…"
}
