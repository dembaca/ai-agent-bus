package directory

import (
	"context"
	"slices"
)

// Persons is what the callout reads for an operator's credential (RT-6.43,
// the addition of 2026-11-25 to ADR-015, points 4 and 5): the anchor the
// directory holds for a `sub`, written by an earlier login or the import,
// never made here, and the channels that person may see.
type Persons struct {
	Store *Store
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

// Visible lists, sorted, the channels the identity `sub` may see: the public
// ones and those it is a member of (ADR-016 point 4). The channels of agents
// that trust the person directly, without the person being a member, are not
// in it yet (an open point of RT-6.43).
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
	return out, nil
}
