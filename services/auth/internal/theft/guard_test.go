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

var base = time.Date(2026, 11, 26, 9, 0, 0, 0, time.UTC)

// RT-6.34, #154 step 3: the same network says nothing; one change and a single
// return are a person moving and only reported; alternating back and forth is
// the copy, and it refuses the session.
func TestOneNetworkAtATimeIsNoTheft(t *testing.T) {
	g := &Guard{}
	for _, d := range []time.Duration{0, 10 * time.Minute, 20 * time.Minute} {
		at(g, base, d)
		if v := g.Session("s1", "203.0.113.0/24"); v != Same {
			t.Fatalf("at %v: %v, want same", d, v)
		}
	}
}

func TestASingleMoveIsReportedNotRefused(t *testing.T) {
	g := &Guard{}
	var logs bytes.Buffer
	g.Log = slog.New(slog.NewTextHandler(&logs, nil))
	at(g, base, 0)
	g.Session("s1", "203.0.113.0/24")
	at(g, base, time.Minute)
	if v := g.Session("s1", "198.51.100.0/24"); v != Moved {
		t.Fatalf("a change: %v, want moved", v)
	}
	if v := g.Session("s1", "198.51.100.0/24"); v != Same {
		t.Fatalf("staying: %v, want same", v)
	}
	if !bytes.Contains(logs.Bytes(), []byte("a new network for the same id")) ||
		bytes.Contains(logs.Bytes(), []byte("a copy is in use")) {
		t.Fatalf("the log: %s", logs.String())
	}
}

// The laptop path: Wi-Fi, then VPN, then back to Wi-Fi is one return, not a
// copy — a single move cannot produce the alternation.
func TestTheLaptopPathIsNoTheft(t *testing.T) {
	g := &Guard{}
	for i, net := range []string{"203.0.113.0/24", "198.51.100.0/24", "203.0.113.0/24"} {
		at(g, base, time.Duration(i)*time.Minute)
		if v := g.Session("s1", net); v != Moved && v != Same {
			t.Fatalf("issue %d from %s: %v, want moved or same", i, net, v)
		}
	}
}

func TestAlternationRefusesTheSessionAndARetryDoesNotPass(t *testing.T) {
	g := &Guard{}
	var logs bytes.Buffer
	g.Log = slog.New(slog.NewTextHandler(&logs, nil))
	for i, net := range []string{"203.0.113.0/24", "198.51.100.0/24", "203.0.113.0/24"} {
		at(g, base, time.Duration(i)*time.Minute)
		if v := g.Session("s1", net); v != Moved && v != Same {
			t.Fatalf("issue %d: %v", i, v)
		}
	}
	at(g, base, 3*time.Minute)
	if v := g.Session("s1", "198.51.100.0/24"); v != Copied {
		t.Fatalf("the fourth network change: %v, want copied", v)
	}
	// The retry from either side is still refused: the refusal must not undo itself.
	for _, net := range []string{"198.51.100.0/24", "203.0.113.0/24"} {
		at(g, base, 4*time.Minute)
		if v := g.Session("s1", net); v != Copied {
			t.Fatalf("a retry from %s: %v, want copied", net, v)
		}
	}
	if !bytes.Contains(logs.Bytes(), []byte("a copy is in use")) {
		t.Fatalf("the log: %s", logs.String())
	}
	// After the block the session starts afresh (it may be a new login).
	at(g, base, 45*time.Minute)
	if v := g.Session("s1", "203.0.113.0/24"); v != Same {
		t.Fatalf("after the block: %v, want same", v)
	}
}

// A host key is reported, never blocked: a copied key is the owner's to rotate,
// and a refusal would lock the owner out instead.
func TestAHostKeyIsReportedNeverRefused(t *testing.T) {
	g := &Guard{}
	for i, net := range []string{"203.0.113.0/24", "198.51.100.0/24", "203.0.113.0/24", "198.51.100.0/24"} {
		at(g, base, time.Duration(i)*time.Minute)
		g.HostKey("UAHOST", net)
	}
	// The same network right after the verdict is not refused.
	at(g, base, 4*time.Minute)
	if v := g.HostKey("UAHOST", "198.51.100.0/24"); v != Same {
		t.Fatalf("a retry on the same network: %v, want same", v)
	}
	// A new session on the reported key starts afresh.
	if v := g.Session("s2", "203.0.113.0/24"); v != Same {
		t.Fatalf("a new session: %v, want same", v)
	}
}

// A copy used only after the real session stopped is not caught: once the old
// changes are out of the window, the next issue is a move at most.
func TestSequentialUseIsNotCaught(t *testing.T) {
	g := &Guard{}
	at(g, base, 0)
	g.Session("s1", "203.0.113.0/24")
	at(g, base, 20*time.Minute)
	if v := g.Session("s1", "198.51.100.0/24"); v != Moved {
		t.Fatalf("after the old network's window: %v, want moved", v)
	}
}

// The window edge: a change exactly at the window still counts, one before it
// does not.
func TestTheWindowEdge(t *testing.T) {
	g := &Guard{}
	at(g, base, 0)
	g.Session("s1", "203.0.113.0/24")
	at(g, base, 30*time.Minute)
	if v := g.Session("s1", "198.51.100.0/24"); v != Moved {
		t.Fatalf("a change exactly at the window: %v, want moved", v)
	}
	at(g, base, 30*time.Minute+time.Second)
	// The first trace is now just outside the window, so this second change is
	// the second one, not the third.
	if v := g.Session("s1", "192.0.2.0/24"); v != Moved {
		t.Fatalf("a change after the first trace's window: %v, want moved", v)
	}
}

func TestAMissingIdOrNetworkIsNeverATheft(t *testing.T) {
	g := &Guard{}
	if v := g.Session("", "203.0.113.0/24"); v != Same {
		t.Fatalf("no id: %v", v)
	}
	if v := g.Session("s1", ""); v != Same {
		t.Fatalf("no network: %v", v)
	}
	if v := g.HostKey("", "203.0.113.0/24"); v != Same {
		t.Fatalf("no id: %v", v)
	}
}

func TestTheTrailIsCapped(t *testing.T) {
	g := &Guard{}
	// Alternate often inside the window: the trail never grows past the cap.
	for i := 0; i < 40; i++ {
		at(g, base, time.Duration(i)*time.Second)
		net := "203.0.113.0/24"
		if i%2 == 0 {
			net = "198.51.100.0/24"
		}
		g.Session("s1", net)
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	if n := len(g.seen["sid:s1"].trail); n > maxTrail {
		t.Fatalf("%d changes kept, want at most %d", n, maxTrail)
	}
}

func TestVerdictNames(t *testing.T) {
	if Same.String() != "same" || Moved.String() != "moved" || Copied.String() != "copied" {
		t.Fatalf("%v %v %v", Same, Moved, Copied)
	}
}
