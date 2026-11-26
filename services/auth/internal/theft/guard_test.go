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

// Four different networks in a row are no alternation: a roaming mobile
// connection changes prefix without returning to one it left.
func TestRoamingNetworksAreNoCopy(t *testing.T) {
	g := &Guard{}
	for i, net := range []string{"203.0.113.0/24", "198.51.100.0/24", "192.0.2.0/24", "100.64.0.0/24"} {
		at(g, base, time.Duration(i)*time.Minute)
		if v := g.Session("s1", net); v == Copied {
			t.Fatalf("issue %d from %s: %v, want moved or same", i, net, v)
		}
	}
}

// A copy used only after the real session stopped is not caught: once the old
// changes are out of the window, the traces restart and even alternating is no
// theft (the window, not the schedule, is what forgets).
func TestSequentialUseIsNotCaught(t *testing.T) {
	g := &Guard{}
	at(g, base, 0)
	g.Session("s1", "203.0.113.0/24")
	at(g, base, 10*time.Minute)
	g.Session("s1", "198.51.100.0/24") // one change
	at(g, base, 50*time.Minute)        // both traces are out of the window
	if v := g.Session("s1", "203.0.113.0/24"); v != Same {
		t.Fatalf("after the window: %v, want same (a fresh trace)", v)
	}
	at(g, base, 51*time.Minute)
	if v := g.Session("s1", "198.51.100.0/24"); v != Moved {
		t.Fatalf("a change after the window: %v, want moved", v)
	}
}

// The window edge decides whether an old trace still counts: A, B, A, B with A
// just outside the window is no alternation, with A inside it would be.
func TestTheWindowEdge(t *testing.T) {
	g := &Guard{}
	at(g, base, 0)
	g.Session("s1", "203.0.113.0/24") // A, out of the window by t=32
	at(g, base, 29*time.Minute)
	g.Session("s1", "198.51.100.0/24") // B
	at(g, base, 31*time.Minute)
	if v := g.Session("s1", "203.0.113.0/24"); v != Moved {
		t.Fatalf("a return after A aged out: %v, want moved", v)
	}
	at(g, base, 32*time.Minute)
	// A@0 is out (edge 2 min): the trail is B, A, B - one return, still a move.
	// With A kept it would be A, B, A, B and refuse.
	if v := g.Session("s1", "198.51.100.0/24"); v != Moved {
		t.Fatalf("a second return with the first trace aged out: %v, want moved", v)
	}
}

// The block ends exactly at its end: one second before it still refuses, at it
// the session starts afresh.
func TestTheBlockEndsExactly(t *testing.T) {
	g := &Guard{Block: 30 * time.Minute}
	for i, net := range []string{"203.0.113.0/24", "198.51.100.0/24", "203.0.113.0/24", "198.51.100.0/24"} {
		at(g, base, time.Duration(i)*time.Minute)
		g.Session("s1", net)
	}
	// The fourth issue (t=3) blocked until t=33.
	at(g, base, 33*time.Minute-time.Second)
	if v := g.Session("s1", "198.51.100.0/24"); v != Copied {
		t.Fatalf("one second before the block's end: %v, want copied", v)
	}
	at(g, base, 33*time.Minute)
	if v := g.Session("s1", "198.51.100.0/24"); v == Copied {
		t.Fatalf("at the block's end: %v, want it to pass", v)
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
