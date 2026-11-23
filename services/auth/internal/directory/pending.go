package directory

import (
	"context"
	"encoding/hex"
	"errors"
	"slices"
	"strings"

	"github.com/nats-io/nats.go/jetstream"
)

const pendingPrefix = "pending."

// ErrNotFound: there is no such record.
var ErrNotFound = errors.New("directory: not found")

// Pending is a Mattermost password account's claim on channels, found by the
// import: it names a username, not an identity. It is no member until an
// admin has confirmed it and an identity of that name logs in (the name is
// then bound to the identity's `sub`).
type Pending struct {
	Name        string   `json:"name"`
	Channels    []string `json:"channels"`
	Confirmed   bool     `json:"confirmed"`
	ConfirmedBy string   `json:"confirmed_by,omitempty"`
}

func pendingKey(name string) string {
	return pendingPrefix + hex.EncodeToString([]byte(name))
}

// ListChannels returns every channel; an empty bucket gives an empty list.
func (s *Store) ListChannels(ctx context.Context) ([]Channel, error) {
	names, err := s.keysWith(ctx, channelPrefix)
	if err != nil {
		return nil, err
	}
	out := make([]Channel, 0, len(names))
	for _, k := range names {
		var c Channel
		if _, ok, err := s.get(ctx, k, &c); err != nil {
			return nil, err
		} else if ok {
			out = append(out, c)
		}
	}
	slices.SortFunc(out, func(a, b Channel) int { return strings.Compare(a.Name, b.Name) })
	return out, nil
}

func (s *Store) keysWith(ctx context.Context, prefix string) ([]string, error) {
	keys, err := s.kv.Keys(ctx)
	if errors.Is(err, jetstream.ErrNoKeysFound) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var out []string
	for _, k := range keys {
		if strings.HasPrefix(k, prefix) {
			out = append(out, k)
		}
	}
	return out, nil
}

// AddPending records that the password account `name` was a member of
// channel: it creates the entry or appends the channel. Idempotent. A
// confirmation already given stays.
func (s *Store) AddPending(ctx context.Context, name, channel string) error {
	if name == "" {
		return ErrBadKey
	}
	if err := checkChannel(channel); err != nil {
		return err
	}
	for range 5 {
		var p Pending
		rev, ok, err := s.get(ctx, pendingKey(name), &p)
		if err != nil {
			return err
		}
		if !ok {
			p = Pending{Name: name}
		}
		if slices.Contains(p.Channels, channel) {
			return nil
		}
		p.Channels = append(p.Channels, channel)
		slices.Sort(p.Channels)
		if _, err := s.put(ctx, pendingKey(name), p, rev); errors.Is(err, ErrConflict) {
			continue
		} else if err != nil {
			return err
		}
		return nil
	}
	return ErrConflict
}

// ListPending returns every pending entry, by name.
func (s *Store) ListPending(ctx context.Context) ([]Pending, error) {
	keys, err := s.keysWith(ctx, pendingPrefix)
	if err != nil {
		return nil, err
	}
	out := make([]Pending, 0, len(keys))
	for _, k := range keys {
		var p Pending
		if _, ok, err := s.get(ctx, k, &p); err != nil {
			return nil, err
		} else if ok {
			out = append(out, p)
		}
	}
	slices.SortFunc(out, func(a, b Pending) int { return strings.Compare(a.Name, b.Name) })
	return out, nil
}

// ConfirmPending marks the entry confirmed by the identity bySub; ErrNotFound
// when there is none.
func (s *Store) ConfirmPending(ctx context.Context, name, bySub string) error {
	for range 5 {
		var p Pending
		rev, ok, err := s.get(ctx, pendingKey(name), &p)
		if err != nil {
			return err
		}
		if !ok {
			return ErrNotFound
		}
		p.Confirmed, p.ConfirmedBy = true, bySub
		if _, err := s.put(ctx, pendingKey(name), p, rev); errors.Is(err, ErrConflict) {
			continue
		} else if err != nil {
			return err
		}
		return nil
	}
	return ErrConflict
}

// redeem gives `sub` the channels of a confirmed pending entry for `name`
// and deletes the entry. An unconfirmed entry is left alone; a channel that
// no longer exists is skipped. An error refuses the login.
func (s *Store) redeem(ctx context.Context, sub, name string) error {
	for range 5 {
		var p Pending
		rev, ok, err := s.get(ctx, pendingKey(name), &p)
		if err != nil {
			return err
		}
		if !ok || !p.Confirmed {
			return nil
		}
		for _, ch := range p.Channels {
			if err := s.addMember(ctx, ch, sub); err != nil {
				return err
			}
		}
		err = s.kv.Delete(ctx, pendingKey(name), jetstream.LastRevision(rev))
		switch {
		case err == nil || errors.Is(err, jetstream.ErrKeyNotFound):
			return nil
		case isWrongSequence(err):
			continue
		}
		return err
	}
	return ErrConflict
}

func (s *Store) addMember(ctx context.Context, channel, sub string) error {
	for range 5 {
		c, rev, ok, err := s.Channel(ctx, channel)
		if err != nil {
			return err
		}
		if !ok || slices.Contains(c.Members, sub) {
			return nil
		}
		c.Members = append(c.Members, sub)
		if _, err := s.PutChannel(ctx, c, rev); errors.Is(err, ErrConflict) {
			continue
		} else if err != nil {
			return err
		}
		return nil
	}
	return ErrConflict
}
