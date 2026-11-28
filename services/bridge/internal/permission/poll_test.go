package permission

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mm"
)

const promptPost = "bridge-post1"

// opened is a rig with one open prompt of agent "coder" for alice (id abcde).
func opened(t *testing.T) *rig {
	t.Helper()
	r := newRig(t)
	if _, err := r.do("coder", prompt10(rid, "alice", "alice", "Bash: ls")); err != nil {
		t.Fatal(err)
	}
	return r
}

func (r *rig) poll() { r.h.Poll(context.Background()) }

func (r *rig) subjects() []string {
	r.pub.mu.Lock()
	defer r.pub.mu.Unlock()
	return append([]string(nil), r.pub.subj...)
}

const wantSubject = "bus.hu.dm.coder.alice"

func decode(t *testing.T, raw []byte) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatal(err)
	}
	return m
}

// RT-6.13, plan B piece 10: the person's answers are carried, each once.
func TestRT613PlanBPiece10PollCarriesTheAnswersOnce(t *testing.T) {
	r := opened(t)
	r.bridge.posts = []mm.Post{
		{ID: promptPost, UserID: "bot", CreateAt: 1000, Reactions: []mm.Reaction{
			{UserID: acct, EmojiName: "+1"}, {UserID: other, EmojiName: "-1"}}},
		{ID: "p-top", UserID: acct, CreateAt: 1001, Message: "Yes ABCDE"},
		{ID: "p-top2", UserID: acct, CreateAt: 1002, Message: " n abcde "},
		{ID: "p-reply", UserID: acct, CreateAt: 1003, RootID: promptPost, Message: "sure, but only once", FileCount: 1},
	}
	r.poll()
	r.poll() // not due yet
	r.now = r.now.Add(FastEvery)
	r.poll() // due again: nothing new, nothing twice
	subj := r.subjects()
	if len(subj) != 4 {
		t.Fatalf("carried %d: %v", len(subj), subj)
	}
	for _, s := range subj {
		if s != wantSubject {
			t.Fatalf("subject %q", s)
		}
	}
	got := map[string]map[string]any{}
	for _, d := range r.pub.data {
		m := decode(t, d)
		id, _ := m["mm"].(string)
		if m["kind"] == "reaction" {
			id = "reaction"
		}
		got[id] = m
	}
	if m := got["reaction"]; m["emoji"] != "+1" || m["name"] != "Alice" || m["mm"] != promptPost || m["v"] != float64(1) {
		t.Fatalf("reaction: %v", m)
	}
	if m := got["p-top"]; m["kind"] != "post" || m["text"] != "Yes ABCDE" || m["name"] != "Alice" || m["files"] != float64(0) || m["mm_root"] != nil {
		t.Fatalf("top-level: %v", m)
	}
	if m := got["p-reply"]; m["mm_root"] != promptPost || m["files"] != float64(1) || m["text"] != "sure, but only once" {
		t.Fatalf("reply: %v", m)
	}
}

func TestRT613PlanBPiece10PollCarriesNothingElse(t *testing.T) {
	for _, tc := range []struct {
		name string
		post mm.Post
	}{
		{"another user", mm.Post{ID: "x", UserID: other, CreateAt: 1001, Message: "yes abcde"}},
		{"another id", mm.Post{ID: "x", UserID: acct, CreateAt: 1001, Message: "yes fghij"}},
		{"no yes/no", mm.Post{ID: "x", UserID: acct, CreateAt: 1001, Message: "abcde"}},
		{"trailing words", mm.Post{ID: "x", UserID: acct, CreateAt: 1001, Message: "yes abcde please"}},
		{"older", mm.Post{ID: "x", UserID: acct, CreateAt: 999, Message: "yes abcde"}},
		{"same time", mm.Post{ID: "x", UserID: acct, CreateAt: 1000, Message: "yes abcde"}},
		{"deleted", mm.Post{ID: "x", UserID: acct, CreateAt: 1001, DeleteAt: 1500, Message: "yes abcde"}},
		{"system", mm.Post{ID: "x", UserID: acct, CreateAt: 1001, Type: "system_join", Message: "yes abcde"}},
		{"webhook", mm.Post{ID: "x", UserID: acct, CreateAt: 1001, Message: "yes abcde", Props: map[string]any{"from_webhook": "true"}}},
		{"override", mm.Post{ID: "x", UserID: acct, CreateAt: 1001, Message: "yes abcde", Props: map[string]any{"override_username": "x"}}},
		{"another thread", mm.Post{ID: "x", UserID: acct, CreateAt: 1001, RootID: "elsewhere", Message: "yes abcde"}},
		{"reaction by another", mm.Post{ID: promptPost, UserID: "bot", CreateAt: 1000,
			Reactions: []mm.Reaction{{UserID: other, EmojiName: "+1"}}}},
		{"reaction on another post", mm.Post{ID: "x", UserID: "bot", CreateAt: 1001,
			Reactions: []mm.Reaction{{UserID: acct, EmojiName: "+1"}}}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := opened(t)
			r.bridge.posts = []mm.Post{tc.post}
			r.poll()
			if s := r.subjects(); len(s) != 0 {
				t.Fatalf("carried %v", s)
			}
		})
	}
}

func TestRT613PlanBPiece10PollNotCarriedWhenTheAnchorChanged(t *testing.T) {
	r := opened(t)
	r.bridge.posts = []mm.Post{{ID: "p1", UserID: acct, CreateAt: 1001, Message: "yes abcde"}}
	r.acc.anchor[acct] = "bob"
	r.now = r.now.Add(anchorTTL + time.Second) // past the cache
	r.poll()
	if s := r.subjects(); len(s) != 0 {
		t.Fatalf("carried %v", s)
	}
	// and with piece 4 on: a proof that no longer holds
	r = opened(t)
	r.sw[4] = true
	r.users.u.AuthData = "changed"
	r.now = r.now.Add(anchorTTL + time.Second) // past the cache
	r.bridge.posts = []mm.Post{{ID: "p1", UserID: acct, CreateAt: 1001, Message: "yes abcde"}}
	r.poll()
	if s := r.subjects(); len(s) != 0 {
		t.Fatalf("carried %v", s)
	}
}

func TestRT613PlanBPiece10PollRetriesAfterAFailedPublish(t *testing.T) {
	r := opened(t)
	r.bridge.posts = []mm.Post{{ID: "p1", UserID: acct, CreateAt: 1001, Message: "yes abcde"}}
	r.pub.fail = true
	r.poll()
	if len(r.subjects()) != 0 {
		t.Fatal("carried on a refused publish")
	}
	r.pub.fail = false
	r.now = r.now.Add(FastEvery)
	r.poll()
	if s := r.subjects(); len(s) != 1 || s[0] != wantSubject {
		t.Fatalf("not carried again: %v", s)
	}
}

func TestRT613PlanBPiece10PollTiming(t *testing.T) {
	r := newRig(t)
	r.h.Bridge = &countingPoster{fakePoster: r.bridge}
	if _, err := r.do("coder", prompt10(rid, "alice", "alice", "Bash: ls")); err != nil {
		t.Fatal(err)
	}
	r.poll() // due at once
	k := key{"coder", rid}
	p := r.h.open[k]
	if p.next.Sub(r.now) != FastEvery {
		t.Fatalf("first interval %v", p.next.Sub(r.now))
	}
	// just before FastFor: still fast; at FastFor: slow
	r.now = r.now.Add(FastFor - time.Second)
	r.poll()
	if p.next.Sub(r.now) != FastEvery {
		t.Fatalf("interval before 10 min: %v", p.next.Sub(r.now))
	}
	r.now = r.now.Add(2 * time.Second)
	r.poll()
	if p.next.Sub(r.now) != SlowEvery {
		t.Fatalf("interval after 10 min: %v", p.next.Sub(r.now))
	}
	// not polled before it is due
	n := countPolls(r)
	r.now = r.now.Add(SlowEvery - time.Second)
	r.poll()
	if countPolls(r) != n {
		t.Fatal("polled before due")
	}
}

type countingPoster struct {
	*fakePoster
	n int
}

func countPolls(r *rig) int { return r.h.open[key{"coder", rid}].poster.(*countingPoster).n }

func (c *countingPoster) PostsSince(ctx context.Context, dm string, since int64) ([]mm.Post, error) {
	c.n++
	return c.fakePoster.PostsSince(ctx, dm, since)
}

func TestRT613PlanBPiece10PollStopsAfterExpiry(t *testing.T) {
	r := opened(t)
	r.bridge.posts = []mm.Post{{ID: "p1", UserID: acct, CreateAt: 1001, Message: "yes abcde"}}
	r.now = r.now.Add(Lifetime)
	r.poll()
	if len(r.subjects()) != 0 || len(r.h.open) != 0 {
		t.Fatalf("expired prompt polled or kept")
	}
}

// RT-6.13, plan B piece 10: the prompt post's reactions are read from the post
// itself when the since read does not hold it.
func TestRT613PlanBPiece10PollReadsThePromptsReactionsFromThePost(t *testing.T) {
	r := opened(t)
	r.bridge.byID = map[string]mm.Post{promptPost: {ID: promptPost, CreateAt: 1000,
		Reactions: []mm.Reaction{{UserID: acct, EmojiName: "-1"}, {UserID: other, EmojiName: "+1"}}}}
	r.poll()
	if s := r.subjects(); len(s) != 1 || s[0] != wantSubject {
		t.Fatalf("carried %v", s)
	}
	if m := decode(t, r.pub.data[0]); m["kind"] != "reaction" || m["emoji"] != "-1" || m["mm"] != promptPost {
		t.Fatalf("reaction: %v", m)
	}
}

// RT-6.13, plan B piece 10: a prompt whose time cannot be read after it was
// posted stays open, polled from the bridge's clock less a minute.
func TestRT613PlanBPiece10PromptWhoseTimeIsNotReadStaysOpen(t *testing.T) {
	r := newRig(t)
	r.bridge.failByID = context.DeadlineExceeded
	res, err := r.do("coder", prompt10(rid, "alice", "alice", "Bash: ls"))
	if err != nil || res["post"] != promptPost {
		t.Fatalf("%v %v", res, err)
	}
	r.bridge.failByID = nil
	at := r.now.Add(-clockSlack).UnixMilli()
	r.bridge.posts = []mm.Post{{ID: promptPost, CreateAt: at + 10},
		{ID: "p-top", UserID: acct, CreateAt: at + 20, Message: "yes " + rid}}
	r.poll()
	if s := r.subjects(); len(s) != 1 {
		t.Fatalf("carried %v", s)
	}
}
