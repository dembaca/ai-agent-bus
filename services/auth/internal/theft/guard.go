package theft

import (
	"log/slog"
	"sync"
	"time"
)

// Guard watches where a Keycloak session and a host key are used from
// (decision 10, step 3 of #154): one of them appearing at an issue from two
// networks *while both are in use* is a theft and refuses further issues for a
// while; a network change one after another is reported, not refused. What it
// cannot see is named where it is: a thief on the same network, a shared NAT or
// proxy (one address for everybody), and a copy used only after the real
// session stopped.
//
// It is the detector alone: what follows a theft — telling the owner — is the
// caller's, so this package holds no credential and stays testable.
type Guard struct {
	// Overlap is how long a network stays in use after its last issue. Two
	// networks used within it of each other are in use at once: the concurrent
	// case. A network back after the overlap is a move.
	Overlap time.Duration
	// Block is how long a concurrent id is refused after the verdict. A new
	// login (a new Keycloak session) starts with a new session id, but the host
	// key stays blocked for this long.
	Block time.Duration
	// Window is how long a network trace is kept: at least Overlap and Block.
	Window time.Duration
	// MaxNetworks caps the networks remembered per id (the newest kept).
	MaxNetworks int
	// Log gets a theft or a move; nil is slog.Default().
	Log *slog.Logger
	// Now is the clock; nil is time.Now.
	Now func() time.Time

	mu   sync.Mutex
	seen map[string]*entry
}

type entry struct {
	// Networks (a prefix, not one address) with their last use.
	networks map[string]time.Time
	last     string
	// Blocked until: every issue is refused while the clock is before it.
	blockedUntil time.Time
}

// Verdict is what an observation says about the id it belongs to.
type Verdict int

const (
	// Same: the same network as the last issue of that id.
	Same Verdict = iota
	// Moved: a new network while no other one is still in use — a person
	// changing networks, reported, never refused.
	Moved
	// Concurrent: two networks in use at once, or an id still blocked after
	// such a verdict — the issue is refused.
	Concurrent
)

func (v Verdict) String() string {
	switch v {
	case Moved:
		return "moved"
	case Concurrent:
		return "concurrent"
	}
	return "same"
}

const (
	defaultOverlap     = 10 * time.Minute
	defaultBlock       = 30 * time.Minute
	defaultWindow      = 30 * time.Minute
	defaultMaxNetworks = 8
)

// Observe notes that id was used from network now and returns the verdict. id
// names the thing watched, e.g. "sid:"+session or "nkey:"+host key; network is
// a prefix (callout.source): one address per network, so dual-stack, IPv6
// privacy addresses and a returning host are not mistaken for a theft.
func (g *Guard) Observe(id, network string) Verdict {
	if id == "" || network == "" {
		return Same // nothing to compare: never a refusal on a missing value
	}
	now, overlap, block, window := g.clock()
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.seen == nil {
		g.seen = map[string]*entry{}
	}
	g.sweep(now, window)
	e := g.seen[id]
	if e == nil {
		g.seen[id] = &entry{networks: map[string]time.Time{network: now}, last: network}
		return Same
	}
	if now.Before(e.blockedUntil) {
		// Still blocked: a retry from the same network must not pass either.
		return Concurrent
	}
	if e.last == network {
		e.networks[network] = now
		return Same
	}
	verdict := Moved
	for n, at := range e.networks {
		if n != network && now.Sub(at) < overlap {
			verdict = Concurrent
			break
		}
	}
	e.networks[network] = now
	e.last = network
	g.cap(e, g.maxNetworks())
	if verdict == Concurrent {
		e.blockedUntil = now.Add(block)
	}
	g.report(id, verdict, network)
	return verdict
}

func (g *Guard) clock() (now time.Time, overlap, block, window time.Duration) {
	now = time.Now()
	if g.Now != nil {
		now = g.Now()
	}
	overlap, block, window = g.Overlap, g.Block, g.Window
	if overlap <= 0 {
		overlap = defaultOverlap
	}
	if block <= 0 {
		block = defaultBlock
	}
	if window <= 0 {
		window = defaultWindow
	}
	if window < overlap {
		window = overlap
	}
	if window < block {
		window = block
	}
	return now, overlap, block, window
}

func (g *Guard) maxNetworks() int {
	if g.MaxNetworks <= 0 {
		return defaultMaxNetworks
	}
	return g.MaxNetworks
}

// cap keeps the newest MaxNetworks networks of an entry.
func (g *Guard) cap(e *entry, n int) {
	for len(e.networks) > n {
		oldest, at := "", time.Time{}
		for k, v := range e.networks {
			if oldest == "" || v.Before(at) {
				oldest, at = k, v
			}
		}
		delete(e.networks, oldest)
	}
}

// sweep forgets networks outside the window and ids with nothing left to say:
// a moved session's old network stops counting, and a block that ended leaves
// nothing behind.
func (g *Guard) sweep(now time.Time, window time.Duration) {
	edge := now.Add(-window)
	for id, e := range g.seen {
		for n, at := range e.networks {
			if at.Before(edge) {
				delete(e.networks, n)
			}
		}
		if len(e.networks) == 0 && !now.Before(e.blockedUntil) {
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
	case Concurrent:
		// The id and the network prefix only: no host address, no token, no text.
		log.Warn("login: one id used from two networks at once", "id", id, "network", network)
	case Moved:
		log.Info("login: a new network for the same id", "id", id, "network", network)
	}
}
