package theft

import (
	"log/slog"
	"sync"
	"time"
)

// Guard watches where a Keycloak session and a host key are used from
// (decision 10, step 3 of #154): one of them appearing at an issue from two
// networks *at the same time* is a theft; a network change one after another
// is reported, not a theft. Same-network theft, and a copy used only after the
// real session stopped, are not caught (the search's limits).
//
// It is the detector alone: what follows a theft — ending the Keycloak session
// through the admin API, telling the owner — is the caller's, so this package
// needs no credential and stays testable.
type Guard struct {
	// Window is how long a network counts as recently used. A network that comes
	// back within it after another one was used means both are in use: the
	// concurrent case. Older traces are forgotten, so a move is a move.
	Window time.Duration
	// Log gets a theft or a move; nil is slog.Default().
	Log *slog.Logger
	// Now is the clock; nil is time.Now.
	Now func() time.Time

	mu   sync.Mutex
	seen map[string]*entry
}

type entry struct {
	// Networks seen within the window, with their last use.
	networks map[string]time.Time
	last     string
}

// Verdict is what an observation says about the id it belongs to.
type Verdict int

const (
	// Same: the same network as the last issue of that id.
	Same Verdict = iota
	// Moved: a network not seen within the window while another one was used
	// before — a person changing networks, reported, never a theft.
	Moved
	// Concurrent: two networks in use at once — the same session or host key
	// seen from a network that was already in use, while another one was used
	// since. A theft.
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

// Observe notes that id was used from network now and returns the verdict. id
// names the thing watched, e.g. "sid:"+session or "nkey:"+host key; network is
// the client's source as the login service sees it (a host, not a port).
func (g *Guard) Observe(id, network string) Verdict {
	if id == "" || network == "" {
		return Same // nothing to compare: never a theft on a missing value
	}
	now := time.Now()
	if g.Now != nil {
		now = g.Now()
	}
	window := g.Window
	if window <= 0 {
		window = 30 * time.Minute
	}
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
	if e.last == network {
		e.networks[network] = now
		return Same
	}
	_, wasUsed := e.networks[network]
	verdict := Moved
	if wasUsed {
		verdict = Concurrent
	}
	e.networks[network] = now
	e.last = network
	g.report(id, verdict, network)
	return verdict
}

// Forget drops an id's traces, e.g. when its session ended.
func (g *Guard) Forget(id string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	delete(g.seen, id)
}

// sweep forgets networks not used within the window, and ids whose every
// network is that old: a moved session's old network stops counting, so a
// return to it later is a move, not a theft.
func (g *Guard) sweep(now time.Time, window time.Duration) {
	edge := now.Add(-window)
	for id, e := range g.seen {
		for n, at := range e.networks {
			if at.Before(edge) {
				delete(e.networks, n)
			}
		}
		if len(e.networks) == 0 {
			delete(g.seen, id)
			continue
		}
		if _, ok := e.networks[e.last]; !ok {
			// The last network aged out while another is still recent: the next
			// issue is compared with what remains, which is the honest picture.
			for n := range e.networks {
				e.last = n
				break
			}
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
		// No address and no token: the id and the network are what an operator acts on.
		log.Warn("login: one id used from two networks at once", "id", id, "network", network)
	case Moved:
		log.Info("login: a new network for the same id", "id", id, "network", network)
	}
}
