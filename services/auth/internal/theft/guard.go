package theft

import (
	"log/slog"
	"sync"
	"time"
)

// Guard watches where a Keycloak session and a host key are used from
// (decision 10, step 3 of #154). A single network change is a person moving and
// is only reported; alternating back and forth (A, B, A, B) within the window
// is the sign of a copy and refuses that session for a while — one change or a
// single return cannot produce it, so a laptop that reconnects on Wi-Fi and
// then moves to VPN or tethering stays in.
//
// A host key is reported, never refused: a copied key is the owner's to rotate,
// and a refusal would only lock the owner out of the bus. What the guard cannot
// see is named in the building block view and the CHANGELOG: two users on one
// prefix (a shared NAT or proxy, or a thief in the same /24 or /64), a copy used
// only after the real session stopped, a thief who keeps the owner out by
// alternating (the DoS the refusal costs), and another replica's guard (the
// memory is per process, so a restart forgets the blocks).
type Guard struct {
	// Window is how long a network change counts. Older changes are forgotten,
	// so a return after it is a move and a session blocked by alternation starts
	// afresh once its traces are gone.
	Window time.Duration
	// Block is how long a session is refused after alternating.
	Block time.Duration
	// Log gets a refusal or a report; nil is slog.Default().
	Log *slog.Logger
	// Now is the clock; nil is time.Now.
	Now func() time.Time

	mu   sync.Mutex
	seen map[string]*entry
}

type entry struct {
	// trail is the networks used, oldest first, one per change within the
	// window.
	trail []trace
	// blockedUntil: every issue of the id is refused while the clock is before
	// it (sessions only).
	blockedUntil time.Time
}

type trace struct {
	network string
	at      time.Time
}

// Verdict is what an observation says about the id it belongs to.
type Verdict int

const (
	// Same: the network of the id's last issue.
	Same Verdict = iota
	// Moved: a single change from a network that is no longer in use — a person
	// changing networks, reported, never refused.
	Moved
	// Copied: the id alternated between networks within the window, or the
	// session is still blocked after such a verdict. A session is refused, a
	// host key reported.
	Copied
)

func (v Verdict) String() string {
	switch v {
	case Moved:
		return "moved"
	case Copied:
		return "copied"
	}
	return "same"
}

const (
	defaultWindow = 30 * time.Minute
	defaultBlock  = 30 * time.Minute
	// maxTrail caps the changes remembered per id (the newest kept).
	maxTrail = 16
)

// Session notes that a Keycloak session was used from network and returns the
// verdict; a copied session is refused for Block. network is the client's
// network as the login service sees it (a prefix, callout.source).
func (g *Guard) Session(sid, network string) Verdict {
	if sid == "" {
		return Same // nothing to compare: never a refusal on a missing value
	}
	return g.observe("sid:"+sid, network, true)
}

// HostKey notes that a host key was used from network and returns the verdict.
// It never refuses: a copied key is the owner's to rotate.
func (g *Guard) HostKey(nkey, network string) Verdict {
	if nkey == "" {
		return Same
	}
	return g.observe("nkey:"+nkey, network, false)
}

func (g *Guard) observe(id, network string, block bool) Verdict {
	if network == "" {
		return Same
	}
	now, window, blockFor := g.clock()
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.seen == nil {
		g.seen = map[string]*entry{}
	}
	g.sweep(now, window)
	e := g.seen[id]
	if e == nil {
		g.seen[id] = &entry{trail: []trace{{network: network, at: now}}}
		return Same
	}
	if block && now.Before(e.blockedUntil) {
		// Still refused: a retry from the same network must not pass either.
		return Copied
	}
	if len(e.trail) == 0 {
		// Every trace aged out while the session was blocked.
		e.trail = []trace{{network: network, at: now}}
		return Same
	}
	last := len(e.trail) - 1
	if e.trail[last].network == network {
		e.trail[last].at = now
		return Same
	}
	e.trail = append(e.trail, trace{network: network, at: now})
	if len(e.trail) > maxTrail {
		e.trail = e.trail[len(e.trail)-maxTrail:]
	}
	// What marks a copy is a return, not a change: A, B, C, D (a roaming mobile
	// network) is no copy, and one return alone is a person coming back. Two
	// returns to an earlier network within the window are, adjacent (A, B, A, B)
	// or not (A, B, A, C, A); a rotation among three (A, B, C, A, B, C) is the
	// limit the docs name.
	returns := 0
	for i := 2; i < len(e.trail); i++ {
		if e.trail[i].network == e.trail[i-2].network {
			returns++
		}
	}
	if returns < 2 {
		g.report(id, Moved, network)
		return Moved
	}
	if block {
		e.blockedUntil = now.Add(blockFor)
	}
	g.report(id, Copied, network)
	return Copied
}

func (g *Guard) clock() (now time.Time, window, block time.Duration) {
	now = time.Now()
	if g.Now != nil {
		now = g.Now()
	}
	window, block = g.Window, g.Block
	if window <= 0 {
		window = defaultWindow
	}
	if block <= 0 {
		block = defaultBlock
	}
	return now, window, block
}

// sweep forgets changes outside the window: a return to a network after it is
// a move, not a theft.
func (g *Guard) sweep(now time.Time, window time.Duration) {
	edge := now.Add(-window)
	for id, e := range g.seen {
		cut := 0
		for cut < len(e.trail) && e.trail[cut].at.Before(edge) {
			cut++
		}
		if cut > 0 {
			e.trail = append(e.trail[:0], e.trail[cut:]...)
		}
		if len(e.trail) == 0 && !now.Before(e.blockedUntil) {
			delete(g.seen, id)
		}
	}
}

func (g *Guard) report(id string, v Verdict, network string) {
	log := g.Log
	if log == nil {
		log = slog.Default()
	}
	switch v {
	case Copied:
		// The id and the network prefix only: no host address, no token, no
		// text. A host key's owner has to rotate it.
		log.Warn("login: an id alternated between networks (a copy is in use)", "id", id, "network", network)
	case Moved:
		log.Info("login: a new network for the same id", "id", id, "network", network)
	}
}
