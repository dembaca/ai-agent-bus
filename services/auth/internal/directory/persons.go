package directory

import (
	"context"
	"log/slog"
	"slices"
	"time"
)

// Persons is what the callout reads for an operator's credential (RT-6.43,
// the addition of 2026-11-25 to ADR-015, points 4 and 5): the anchor the
// directory holds for a `sub`, written by an earlier login or the import,
// never made here, and the channels that person may see.
type Persons struct {
	Store *Store
	// Now is the clock the trust index is read with; nil is time.Now.
	Now func() time.Time
	// Log gets the keys of trust records that cannot be read; nil is
	// slog.Default().
	Log *slog.Logger
}

// Anchor is the anchor of the identity `sub`; false when the directory has
// none (no credential for that person). It writes nothing.
func (p Persons) Anchor(ctx context.Context, sub string) (string, bool, error) {
	id, _, ok, err := p.Store.Identity(ctx, sub)
	if err != nil || !ok || id.Anchor == "" {
		return "", false, err
	}
	return id.Anchor, true, nil
}

// Visible lists, sorted, the channels the identity `sub` may see (ADR-016
// point 4, RT-6.43 step 3): the public ones, those it is a member of, and the
// channels of the agents that are the person's or trust the person directly,
// by the trust index the login service writes at each agent's credential
// (TrustOpened): only unexpired records, never over several steps. A trust
// record that cannot be read opens nothing and is logged by its key; a read
// of the bucket that fails is an error, no partial list.
func (p Persons) Visible(ctx context.Context, sub string) ([]string, error) {
	if err := checkKey(sub); err != nil {
		return nil, err
	}
	all, err := p.Store.ListChannels(ctx)
	if err != nil {
		return nil, err
	}
	var out []string
	for _, c := range all {
		if checkChannel(c.Name) == nil && (c.Public || slices.Contains(c.Members, sub)) {
			out = append(out, c.Name)
		}
	}
	id, _, ok, err := p.Store.Identity(ctx, sub)
	if err != nil {
		return nil, err
	}
	if ok && id.Anchor != "" {
		now := time.Now()
		if p.Now != nil {
			now = p.Now()
		}
		opened, unreadable, err := p.Store.TrustOpened(ctx, id.Anchor, now)
		if err != nil {
			return nil, err
		}
		if len(unreadable) > 0 {
			log := p.Log
			if log == nil {
				log = slog.Default()
			}
			// The keys only, never a value: they open nothing.
			log.Warn("trust records that cannot be read open nothing", "keys", unreadable)
		}
		out = append(out, opened...)
	}
	slices.Sort(out)
	return slices.Compact(out), nil
}
