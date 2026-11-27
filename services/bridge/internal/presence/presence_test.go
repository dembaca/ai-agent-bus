package presence

import (
	"testing"
	"time"
)

func TestDecodeAndSubject(t *testing.T) {
	st, bot, err := Decode([]byte(`{"v":1,"status":"online","bot":"bot-1"}`))
	if err != nil || st != Online || bot != "bot-1" {
		t.Fatalf("%v %q %v", st, bot, err)
	}
	if st, _, err := Decode([]byte(`{"v":1,"status":"bye"}`)); err != nil || st != Bye {
		t.Fatalf("bye: %v %v", st, err)
	}
	for _, body := range []string{`{}`, `{"v":2,"status":"online"}`, `{"v":1,"status":"napping"}`} {
		if _, _, err := Decode([]byte(body)); err == nil {
			t.Errorf("accepted %q", body)
		}
	}
	if o, n, ok := Subject("presence.alice.agent-x"); !ok || o != "alice" || n != "agent-x" {
		t.Fatalf("%q %q %v", o, n, ok)
	}
	if _, _, ok := Subject("presence.crown.home.agent-x"); ok {
		t.Fatal("a crown claim read as a presence")
	}
}

func TestFreshAndTheLinkTag(t *testing.T) {
	now := time.Now()
	if !Fresh(Info{Status: Online, At: now.Add(-10 * time.Second)}, now, 75*time.Second) {
		t.Fatal("a fresh presence is not fresh")
	}
	if Fresh(Info{Status: Online, At: now.Add(-80 * time.Second)}, now, 75*time.Second) {
		t.Fatal("an old presence is fresh")
	}
	for _, st := range []Status{Offline, Bye} {
		if Fresh(Info{Status: st, At: now}, now, 75*time.Second) {
			t.Fatalf("%s counts as fresh", st)
		}
	}
	name := "agent-abcdefghijklmnopqrstuvwxyz"
	if got := LinkTag(name); got != name[len(name)-26:][:16] {
		t.Fatalf("%q", got)
	}
	if LinkTag("short") != "" {
		t.Fatal("a short name has a tag")
	}
	if got := LinkOf("working · ⇄abcdefghijklmnop · ~home"); got != "abcdefghijklmnop" {
		t.Fatalf("%q", got)
	}
	if LinkOf("working · ~home") != "" {
		t.Fatal("no mark, a tag")
	}
}

// RT-6.37: a membership is read from its own subject and counts while it is
// younger than MemberKeep by the server's time; an older one never overrides a
// newer, and a name's own presence is no membership.
func TestMembershipsAndTheirServerClock(t *testing.T) {
	now := time.Now()
	s := New()
	s.Clock = func() time.Time { return now }
	s.ObservedMember("home", "agent-a", now.Add(-time.Hour), 2)
	if !s.MemberFresh("home", "agent-a") {
		t.Fatal("a membership of an hour ago is not fresh")
	}
	if s.MemberFresh("home", "agent-b") || s.MemberFresh("elsewhere", "agent-a") {
		t.Fatal("a membership of another name or channel")
	}
	// an older message never overrides a newer one
	s.ObservedMember("home", "agent-a", now.Add(-2*time.Hour), 3)
	if got := s.ServerNow(); !got.Equal(now) && got.Sub(now) > time.Minute {
		t.Fatalf("the clock moved back: %v", got)
	}
	if !s.MemberFresh("home", "agent-a") {
		t.Fatal("an older observation overrode the newer")
	}
	// the server's now runs on: just past one renewal (6 h) it is still there,
	// beyond MemberKeep it has run out
	now = now.Add(6 * time.Hour)
	if !s.MemberFresh("home", "agent-a") {
		t.Fatal("a membership of seven hours is stale")
	}
	now = now.Add(2 * time.Hour)
	if s.MemberFresh("home", "agent-a") {
		t.Fatal("a membership older than MemberKeep is fresh")
	}
	if o, n, ok := Membership("presence.member.home.agent-a"); !ok || o != "home" || n != "agent-a" {
		t.Fatalf("%q %q %v", o, n, ok)
	}
	if _, _, ok := Membership("presence.member.home.agent-a.extra"); ok {
		t.Fatal("a five-token subject read as a membership")
	}
	if _, _, ok := Membership("presence.alice.agent-a"); ok {
		t.Fatal("a presence read as a membership")
	}
	for _, body := range []string{`{"v":1}`, `{"v":1,"future":"x"}`} {
		if err := DecodeMember([]byte(body)); err != nil {
			t.Errorf("refused %q: %v", body, err)
		}
	}
	for _, body := range []string{`{}`, `{"v":2}`, `not json`} {
		if err := DecodeMember([]byte(body)); err == nil {
			t.Errorf("accepted %q", body)
		}
	}
}
