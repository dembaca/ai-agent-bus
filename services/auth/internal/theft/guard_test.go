package theft

import (
	"bytes"
	"log/slog"
	"testing"
	"time"
)

func at(g *Guard, base time.Time, d time.Duration) {
	g.Now = func() time.Time { return base.Add(d) }
}

// RT-6.34, #154 step 3: the same network says nothing; a network change after
// the old one is no longer in use is reported, never refused; two networks in
// use at once refuse the id, and a retry from either does not pass.
func TestOneNetworkAtATimeIsNoTheft(t *testing.T) {
	g := &Guard{}
	base := time.Date(2026, 11, 26, 9, 0, 0, 0, time.UTC)
	for _, d := range []time.Duration{0, 10 * time.Minute, 20 * time.Minute} {
		at(g, base, d)
		if v := g.Observe("sid:s1", "203.0.113.0/24"); v != Same {
			t.Fatalf("at %v: %v, want same", d, v)
		}
	}
}

func TestAMoveIsReportedNotRefused(t *testing.T) {
	g := &Guard{}
	base := time.Date(2026, 11, 26, 9, 0, 0, 0, time.UTC)
	var logs bytes.Buffer
	g.Log = slog.New(slog.NewTextHandler(&logs, nil))
	at(g, base, 0)
	g.Observe("sid:s1", "203.0.113.0/24")
	// The old network is no longer in use: the next issue is a move.
	at(g, base, 15*time.Minute)
	if v := g.Observe("sid:s1", "198.51.100.0/24"); v != Moved {
		t.Fatalf("a change after the overlap: %v, want moved", v)
	}
	if v := g.Observe("sid:s1", "198.51.100.0/24"); v != Same {
		t.Fatalf("staying: %v, want same", v)
	}
	if !bytes.Contains(logs.Bytes(), []byte("a new network for the same id")) ||
		bytes.Contains(logs.Bytes(), []byte("two networks at once")) {
		t.Fatalf("the log: %s", logs.String())
	}
}

func TestTwoNetworksAtOnceRefuseAndARetryDoesNotPass(t *testing.T) {
	g := &Guard{}
	base := time.Date(2026, 11, 26, 9, 0, 0, 0, time.UTC)
	var logs bytes.Buffer
	g.Log = slog.New(slog.NewTextHandler(&logs, nil))
	at(g, base, 0)
	g.Observe("sid:s1", "203.0.113.0/24")
	at(g, base, time.Minute)
	if v := g.Observe("sid:s1", "198.51.100.0/24"); v != Concurrent {
		t.Fatalf("a second network within the overlap: %v, want concurrent", v)
	}
	// The retry from either side is still refused: the refusal must not undo itself.
	for _, net := range []string{"198.51.100.0/24", "203.0.113.0/24"} {
		at(g, base, 2*time.Minute)
		if v := g.Observe("sid:s1", net); v != Concurrent {
			t.Fatalf("a retry from %s: %v, want concurrent", net, v)
		}
	}
	if !bytes.Contains(logs.Bytes(), []byte("two networks at once")) {
		t.Fatalf("the log: %s", logs.String())
	}
	// After the block, the id starts afresh (the session may be a new login).
	at(g, base, 45*time.Minute)
	if v := g.Observe("sid:s1", "203.0.113.0/24"); v != Same {
		t.Fatalf("after the block: %v, want same", v)
	}
}

// A copy used only after the real session stopped is not caught: when the old
// network is out of use, the next issue is a move at most.
func TestSequentialUseIsNotCaught(t *testing.T) {
	g := &Guard{}
	base := time.Date(2026, 11, 26, 9, 0, 0, 0, time.UTC)
	at(g, base, 0)
	g.Observe("sid:s1", "203.0.113.0/24")
	at(g, base, 20*time.Minute)
	if v := g.Observe("sid:s1", "198.51.100.0/24"); v != Moved {
		t.Fatalf("after the old network's overlap: %v, want moved", v)
	}
}

func TestAMissingIdOrNetworkIsNeverATheft(t *testing.T) {
	g := &Guard{}
	if v := g.Observe("", "203.0.113.0/24"); v != Same {
		t.Fatalf("no id: %v", v)
	}
	if v := g.Observe("sid:s1", ""); v != Same {
		t.Fatalf("no network: %v", v)
	}
}

func TestTheNetworksAreCapped(t *testing.T) {
	g := &Guard{MaxNetworks: 2}
	base := time.Date(2026, 11, 26, 9, 0, 0, 0, time.UTC)
	for i, net := range []string{"203.0.113.0/24", "198.51.100.0/24", "192.0.2.0/24"} {
		at(g, base, time.Duration(i)*time.Hour*2) // each after the overlap: a move
		g.Observe("sid:s1", net)
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	if n := len(g.seen["sid:s1"].networks); n > 2 {
		t.Fatalf("%d networks kept, want at most 2", n)
	}
}

func TestVerdictNames(t *testing.T) {
	if Same.String() != "same" || Moved.String() != "moved" || Concurrent.String() != "concurrent" {
		t.Fatalf("%v %v %v", Same, Moved, Concurrent)
	}
}
