package links

import (
	"context"
	"testing"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/presence"
)

type roster struct{ members []Member }

func (r roster) Members(context.Context) ([]Member, error) { return r.members, nil }

func TestShownAsNeedsTheTagAndAFreshPresence(t *testing.T) {
	name := "abcdefghijklmnopqrstuvwxy"
	tag := presence.LinkTag(name)
	snap := presence.New()
	snap.Observed(name, "alice", "bot-1", presence.Online, time.Now())
	r := &Resolver{Presence: snap, Window: 75 * time.Second, TTL: time.Minute,
		Roster: roster{members: []Member{{ID: "bot-1", Username: "agent-a", Status: "working · ⇄" + tag + " · ~home"}}}}
	got, err := r.ShownAs(context.Background(), name)
	if err != nil || got != "agent-a" {
		t.Fatalf("%q %v", got, err)
	}
	// the bot's status shows another tag: no link
	r2 := &Resolver{Presence: snap, Window: 75 * time.Second, TTL: time.Minute,
		Roster: roster{members: []Member{{ID: "bot-1", Username: "agent-a", Status: "working · ⇄zzzzzzzzzzzzzzzz"}}}}
	if got, _ := r2.ShownAs(context.Background(), name); got != "" {
		t.Fatalf("a wrong tag linked: %q", got)
	}
	// no bot id in the presence: no link
	snap2 := presence.New()
	snap2.Observed(name, "alice", "", presence.Online, time.Now())
	r3 := &Resolver{Presence: snap2, Window: 75 * time.Second, TTL: time.Minute,
		Roster: roster{members: []Member{{ID: "bot-1", Username: "agent-a", Status: "⇄" + tag}}}}
	if got, _ := r3.ShownAs(context.Background(), name); got != "" {
		t.Fatalf("no bot id linked: %q", got)
	}
	// an old presence: no link
	snap3 := presence.New()
	snap3.Observed(name, "alice", "bot-1", presence.Online, time.Now().Add(-2*time.Minute))
	snap3.Observed("agent-other", "alice", "bot-2", presence.Online, time.Now()) // a newer presence
	r4 := &Resolver{Presence: snap3, Window: 75 * time.Second, TTL: time.Minute,
		Roster: roster{members: []Member{{ID: "bot-1", Username: "agent-a", Status: "⇄" + tag}}}}
	if got, _ := r4.ShownAs(context.Background(), name); got != "" {
		t.Fatalf("an old presence linked: %q", got)
	}
}
