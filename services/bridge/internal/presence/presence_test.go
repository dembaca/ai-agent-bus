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
	if Fresh(Info{Status: Offline, At: now}, now, 75*time.Second) {
		t.Fatal("an offline presence is fresh")
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
