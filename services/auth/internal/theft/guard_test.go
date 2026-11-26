package theft

import (
	"bytes"
	"log/slog"
	"testing"
	"time"
)

// RT-6.34, #154 step 3: the same session from two networks at once is a theft;
// one network change after another is reported, not a theft; the same network
// says nothing.
func TestOneNetworkAtATimeIsNoTheft(t *testing.T) {
	g := &Guard{Window: 30 * time.Minute}
	base := time.Date(2026, 11, 26, 9, 0, 0, 0, time.UTC)
	for _, at := range []time.Duration{0, 10 * time.Minute, 20 * time.Minute} {
		g.Now = func() time.Time { return base.Add(at) }
		if v := g.Observe("sid:s1", "203.0.113.7"); v != Same {
			t.Fatalf("%v at %v: %v, want same", "203.0.113.7", at, v)
		}
	}
}

func TestAMoveIsReportedNotATheft(t *testing.T) {
	g := &Guard{Window: 30 * time.Minute}
	base := time.Date(2026, 11, 26, 9, 0, 0, 0, time.UTC)
	var logs bytes.Buffer
	g.Log = slog.New(slog.NewTextHandler(&logs, nil))
	g.Now = func() time.Time { return base }
	g.Observe("sid:s1", "203.0.113.7")
	g.Now = func() time.Time { return base.Add(10 * time.Minute) }
	if v := g.Observe("sid:s1", "198.51.100.9"); v != Moved {
		t.Fatalf("a network change: %v, want moved", v)
	}
	if v := g.Observe("sid:s1", "198.51.100.9"); v != Same {
		t.Fatalf("staying on the new network: %v, want same", v)
	}
	if !bytes.Contains(logs.Bytes(), []byte("a new network for the same id")) ||
		bytes.Contains(logs.Bytes(), []byte("two networks at once")) {
		t.Fatalf("the log: %s", logs.String())
	}
}

func TestTwoNetworksAtOnceIsATheft(t *testing.T) {
	g := &Guard{Window: 30 * time.Minute}
	base := time.Date(2026, 11, 26, 9, 0, 0, 0, time.UTC)
	var logs bytes.Buffer
	g.Log = slog.New(slog.NewTextHandler(&logs, nil))
	// A, then B (a move), then A again while B is recent: both are in use.
	for i, net := range []string{"203.0.113.7", "198.51.100.9", "203.0.113.7"} {
		g.Now = func() time.Time { return base.Add(time.Duration(i) * 10 * time.Minute) }
		v := g.Observe("sid:s1", net)
		if i < 2 && v != Moved && v != Same {
			t.Fatalf("issue %d: %v", i, v)
		}
		if i == 2 {
			if v != Concurrent {
				t.Fatalf("the third issue from the first network: %v, want concurrent", v)
			}
			if !bytes.Contains(logs.Bytes(), []byte("two networks at once")) {
				t.Fatalf("the log: %s", logs.String())
			}
		}
	}
}

// A copy used only after the real session stopped is not caught: once the old
// network is out of the window, its trace is gone, so the next issue starts
// afresh — a sequential use looks like a move at most (the search's limit).
func TestSequentialUseIsNotCaught(t *testing.T) {
	g := &Guard{Window: 30 * time.Minute}
	base := time.Date(2026, 11, 26, 9, 0, 0, 0, time.UTC)
	g.Now = func() time.Time { return base }
	g.Observe("sid:s1", "203.0.113.7")
	// 40 min later the old network is out of the window: no comparison is left.
	g.Now = func() time.Time { return base.Add(40 * time.Minute) }
	if v := g.Observe("sid:s1", "198.51.100.9"); v != Same {
		t.Fatalf("a new trace after the window: %v, want same", v)
	}
	// A change shortly after that is a move, and only a return to a network
	// still in the window is a theft.
	g.Now = func() time.Time { return base.Add(45 * time.Minute) }
	if v := g.Observe("sid:s1", "203.0.113.7"); v != Moved {
		t.Fatalf("a change within the window: %v, want moved", v)
	}
	g.Now = func() time.Time { return base.Add(50 * time.Minute) }
	if v := g.Observe("sid:s1", "198.51.100.9"); v != Concurrent {
		t.Fatalf("a return to a network in the window: %v, want concurrent", v)
	}
}

func TestAMissingIdOrNetworkIsNeverATheft(t *testing.T) {
	g := &Guard{}
	if v := g.Observe("", "203.0.113.7"); v != Same {
		t.Fatalf("no id: %v", v)
	}
	if v := g.Observe("sid:s1", ""); v != Same {
		t.Fatalf("no network: %v", v)
	}
}

func TestForgetDropsTheTraces(t *testing.T) {
	g := &Guard{Window: 30 * time.Minute}
	base := time.Date(2026, 11, 26, 9, 0, 0, 0, time.UTC)
	g.Now = func() time.Time { return base }
	g.Observe("sid:s1", "203.0.113.7")
	g.Now = func() time.Time { return base.Add(time.Minute) }
	g.Observe("sid:s1", "198.51.100.9")
	g.Forget("sid:s1")
	g.Now = func() time.Time { return base.Add(2 * time.Minute) }
	if v := g.Observe("sid:s1", "203.0.113.7"); v != Same {
		t.Fatalf("after Forget: %v, want same", v)
	}
}

func TestVerdictNames(t *testing.T) {
	if Same.String() != "same" || Moved.String() != "moved" || Concurrent.String() != "concurrent" {
		t.Fatalf("%v %v %v", Same, Moved, Concurrent)
	}
}
