// Package links resolves an agent's NATS name to the Mattermost username of
// its bot, for the mirror's `shown_as` (RT-6.37 step 4): the name's presence
// and the bot's status tag name each other, and the presence is fresh. It is
// display only; nothing decides by it.
//
// Phase 1 of the bridge checks the tag and the freshness; the owner match
// (the bot's owner being the presence subject's owner) follows with the
// roster's descriptions.
package links

import (
	"context"
	"strings"
	"sync"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/presence"
)

// Member is one of the home's bots, as the roster shows it.
type Member struct {
	ID       string
	Username string
	Status   string // its custom status text, where the ⇄tag sits
}

// Roster lists the home's members.
type Roster interface {
	Members(ctx context.Context) ([]Member, error)
}

// Resolver resolves names from the presence snapshot and the roster.
type Resolver struct {
	Presence *presence.Snapshot
	Roster   Roster
	Window   time.Duration // how old a presence may be
	TTL      time.Duration // how long the roster is kept

	mu      sync.Mutex
	members []Member
	at      time.Time
}

// ShownAs is the bot's username when the link holds, else "".
func (r *Resolver) ShownAs(ctx context.Context, agent string) (string, error) {
	if r.Presence == nil || r.Roster == nil {
		return "", nil
	}
	info, ok := r.Presence.Get(agent)
	if !ok || !presence.Fresh(info, r.Presence.Newest(), r.Window) || info.Bot == "" {
		return "", nil
	}
	members, err := r.roster(ctx)
	if err != nil {
		return "", err
	}
	tag := presence.LinkTag(agent)
	if tag == "" {
		return "", nil
	}
	for _, m := range members {
		if m.ID == info.Bot && strings.EqualFold(presence.LinkOf(m.Status), tag) {
			return m.Username, nil
		}
	}
	return "", nil
}

func (r *Resolver) roster(ctx context.Context) ([]Member, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.at.Add(r.TTL).After(time.Now()) && r.members != nil {
		return r.members, nil
	}
	members, err := r.Roster.Members(ctx)
	if err != nil {
		return nil, err
	}
	r.members, r.at = members, time.Now()
	return members, nil
}
