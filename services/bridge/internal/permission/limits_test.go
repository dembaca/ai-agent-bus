package permission

import (
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mm"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/presence"
)

func wantRefused(t *testing.T, r *rig, agent, payload, want string) {
	t.Helper()
	if _, err := r.do(agent, payload); err == nil || err.Error() != want {
		t.Fatalf("error %v, want %q", err, want)
	}
}

// RT-6.13: the channel is bound to the agent: its attestation states the home
// and it announced a fresh membership there; the same words for both failures.
func TestRT613PlanBPiece10ChannelIsBoundToTheAgent(t *testing.T) {
	const notHome = "not the agent's home"
	for _, tc := range []struct {
		name string
		mut  func(r *rig)
		want string
	}{
		{"another home stated", func(r *rig) { r.att.att.Home = "elsewhere" }, notHome},
		{"no home stated", func(r *rig) { r.att.att.HasHome, r.att.att.Home = false, "" }, notHome},
		{"empty home stated", func(r *rig) { r.att.att.Home = "" }, notHome},
		{"no fresh membership", func(r *rig) { r.pres.not["home/a"] = true }, notHome},
		{"no presence", func(r *rig) { r.h.Presence = nil }, notHome},
		{"no attestation", func(r *rig) { r.att.att = nil }, "no fresh attestation of the agent"},
		{"expired attestation", func(r *rig) { r.att.gone = map[string]bool{"a": true} }, "no fresh attestation of the agent"},
		{"no attest reader", func(r *rig) { r.h.Attest = nil }, "no fresh attestation of the agent"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := newRig(t)
			tc.mut(r)
			wantRefused(t, r, "a", prompt10(rid, "alice", "alice", "x"), tc.want)
			wantRefused(t, r, "a", note10(rid, "x", false), tc.want)
			if len(r.h.open) != 0 || len(r.bridge.created) != 0 {
				t.Fatal("kept or posted")
			}
		})
	}
}

// A note of an agent whose home no longer checks out is refused, though its
// prompt is open.
func TestRT613PlanBPiece10NoteNeedsTheHomeToo(t *testing.T) {
	r := newRig(t)
	if _, err := r.do("a", prompt10(rid, "alice", "alice", "x")); err != nil {
		t.Fatal(err)
	}
	r.pres.not["home/a"] = true
	wantRefused(t, r, "a", note10(rid, "x", false), "not the agent's home")
}

func TestRT613PlanBPiece10TargetIsTheOwnerOrATrustedHuman(t *testing.T) {
	r := newRig(t)
	r.acc.owner["carol"] = other
	r.acc.rec[other] = r.acc.rec[acct]
	wantRefused(t, r, "a", prompt10(rid, "carol", "alice", "x"), "not the agent's owner or one of its trusted humans")
	if len(r.bridge.created) != 0 {
		t.Fatal("posted")
	}
	if _, err := r.do("a", prompt10(rid, "alice", "alice", "x")); err != nil {
		t.Fatal(err)
	}
	// a trusted human passes the check (bob has no account in this rig)
	if _, err := r.do("a", prompt10("fghij", "bob", "bob", "x")); err == nil ||
		!strings.Contains(err.Error(), "no single Mattermost account") {
		t.Fatalf("a trusted human passes the check: %v", err)
	}
}

func TestRT613PlanBPiece10RateOfAnAgentToAPerson(t *testing.T) {
	const tooMany = "too many prompts; try again in a minute"
	ids := []string{"aaaaa", "aaaab", "aaaac", "aaaad", "aaaae", "aaaaf", "aaaag"}
	r := newRig(t)
	for _, id := range ids[:PerMinute] {
		if _, err := r.do("a", prompt10(id, "alice", "alice", "x")); err != nil {
			t.Fatal(err)
		}
		r.now = r.now.Add(time.Second)
		if _, err := r.do("a", note10(id, "x", true)); err != nil { // closing frees the open slot, not the rate
			t.Fatal(err)
		}
	}
	wantRefused(t, r, "a", prompt10(ids[6], "alice", "alice", "x"), tooMany)
	// another agent to the same person is not limited by it
	if _, err := r.do("b", prompt10(ids[6], "alice", "alice", "x")); err != nil {
		t.Fatal(err)
	}
	// the window slides: the first prompt is a minute old, the others are not
	r.now = r.now.Add(rateWindow - time.Duration(PerMinute)*time.Second)
	if _, err := r.do("a", prompt10(ids[6], "alice", "alice", "x")); err != nil {
		t.Fatalf("after the window: %v", err)
	}
	wantRefused(t, r, "a", prompt10(ids[5], "alice", "alice", "x"), tooMany)
}

func TestRT613PlanBPiece10RatePerAgentAcrossPeople(t *testing.T) {
	r := newRig(t)
	r.acc.owner["bob"] = other
	r.acc.rec[other] = r.acc.rec[acct]
	r.acc.anchor[other], r.acc.proof[other] = "bob", "bob"
	ids := []string{"aaaaa", "aaaab", "aaaac", "aaaad", "aaaae", "aaaaf", "aaaag"}
	for i, id := range ids[:PerMinute] {
		to := []string{"alice", "bob"}[i%2] // three each: no pair limit, six for the agent
		if _, err := r.do("a", prompt10(id, to, "alice", "x")); err != nil {
			t.Fatal(i, err)
		}
	}
	wantRefused(t, r, "a", prompt10(ids[6], "bob", "alice", "x"), "too many prompts; try again in a minute")
}

func TestRT613PlanBPiece10CapPerOwner(t *testing.T) {
	r := newRig(t)
	r.h.open = map[key]*prompt{}
	for i := 0; i < PerOwner; i++ {
		r.h.open[key{fmt.Sprintf("n%d", i/PerAgent), fmt.Sprintf("id%d", i)}] = &prompt{opened: r.now, home: "home", owner: "alice"}
	}
	wantRefused(t, r, "a", prompt10(rid, "alice", "alice", "x"), "too many open prompts of this agent's owner")
	// an agent of another owner is not held up by it
	r.att.by = map[string]presence.Attestation{"z": {Owner: "bob", Trusted: []string{"alice"}, Home: "home", HasHome: true}}
	if _, err := r.do("z", prompt10(rid, "alice", "alice", "x")); err != nil {
		t.Fatal(err)
	}
}

// One PostsSince per (bot, DM) and poll round, however many prompts share it;
// each prompt still takes only its own answers.
func TestRT613PlanBPiece10OnePollPerSharedDM(t *testing.T) {
	r := newRig(t)
	cp := &countingPoster{fakePoster: r.bridge}
	r.h.Bridge = cp
	for _, id := range []string{"aaaaa", "aaaab", "aaaac"} {
		if _, err := r.do("a", prompt10(id, "alice", "alice", "x")); err != nil {
			t.Fatal(err)
		}
	}
	r.bridge.posts = []mm.Post{
		{ID: "r1", UserID: acct, RootID: "bridge-post2", CreateAt: 2000, Message: "yes"},
		{ID: "r2", UserID: acct, CreateAt: 2001, Message: "no aaaac"},
	}
	r.poll()
	if cp.n != 1 {
		t.Fatalf("%d reads for one DM, want 1", cp.n)
	}
	if s := r.subjects(); len(s) != 2 {
		t.Fatalf("carried %v, want the thread reply and the id answer", s)
	}
	// another person's DM reads on its own
	r.acc.owner["bob"] = other
	r.acc.rec[other] = r.acc.rec[acct]
	r.acc.anchor[other], r.acc.proof[other] = "bob", "bob"
	if _, err := r.do("a", prompt10("aaaad", "bob", "alice", "x")); err != nil {
		t.Fatal(err)
	}
	cp.n = 0
	r.now = r.now.Add(time.Minute)
	r.poll()
	if cp.n != 2 {
		t.Fatalf("%d reads for two DMs, want 2", cp.n)
	}
}
