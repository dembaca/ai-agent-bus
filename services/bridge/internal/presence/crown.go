package presence

import (
	"encoding/json"
	"errors"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"
)

// ClaimKeep is how long a crown claim counts: the PRESENCE stream's max age
// (24 h), just over the 23 h after which a member claims again (RT-6.38).
const ClaimKeep = 24 * time.Hour

// nameRE is an agent's NATS name as a subject token: the request scaffold's
// rule.
var nameRE = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)

// ClaimSubject splits presence.crown.<channel>.<name>, a crown claim
// (RT-6.38 step 1).
func ClaimSubject(subject string) (channel, name string, ok bool) {
	parts := strings.Split(subject, ".")
	if len(parts) != 4 || parts[0] != "presence" || parts[1] != "crown" ||
		!channelRE.MatchString(parts[2]) || !nameRE.MatchString(parts[3]) {
		return "", "", false
	}
	return parts[2], parts[3], true
}

// DecodeClaim checks a claim's body: a JSON object ({"v":1} today), as the MCP
// server reads it.
func DecodeClaim(body []byte) error {
	var obj map[string]any
	if err := json.Unmarshal(body, &obj); err != nil || obj == nil {
		return errors.New("a claim is no JSON object")
	}
	return nil
}

type claim struct {
	seq uint64
	at  time.Time // the stream's time of the claim
}

// Claims keeps the newest crown claim per channel and name, as the bridge's
// own consumer replays them at every start (crown_consumer.go), and the
// server's time, which runs on from the newest one seen. The zero value is
// ready; it is safe for concurrent use.
type Claims struct {
	mu      sync.RWMutex
	by      map[string]map[string]claim // channel -> name -> claim
	staging map[string]map[string]claim // a new consumer's replay until it caught up
	srvNow  time.Time
	seenAt  time.Time
	read    bool // a replay was read to its end
	// Clock is the local clock; nil is time.Now (tests set it).
	Clock func() time.Time
}

func (c *Claims) clock() time.Time {
	if c.Clock != nil {
		return c.Clock()
	}
	return time.Now()
}

// Begin starts a replay by a new consumer: its claims replace the old ones at
// Commit, so a claim gone from the stream leaves nothing behind.
func (c *Claims) Begin() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.staging = map[string]map[string]claim{}
}

// Commit ends the replay.
func (c *Claims) Commit() {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.staging != nil {
		c.by, c.staging = c.staging, nil
	}
	c.read = true
}

// Read says whether the claims were read once to the end: before that no
// holder is known, and no crown is shown or header line written.
func (c *Claims) Read() bool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.read
}

// Seed sets the server's time from the server's own answer (the consumer's
// creation), so after a start an old claim reads as old.
func (c *Claims) Seed(at time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.see(at)
}

func (c *Claims) see(at time.Time) {
	if at.After(c.srvNow) {
		c.srvNow, c.seenAt = at, c.clock()
	}
}

// Observed takes a claim of name in channel, its stream sequence and time.
// A claim older than the one kept (by time, then sequence) changes nothing.
func (c *Claims) Observed(channel, name string, seq uint64, at time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.see(at)
	m := c.by
	if c.staging != nil {
		m = c.staging
	}
	if m == nil {
		m = map[string]map[string]claim{}
		c.by = m
	}
	ch := m[channel]
	if ch == nil {
		ch = map[string]claim{}
		m[channel] = ch
	}
	if old, ok := ch[name]; ok && (at.Before(old.at) || at.Equal(old.at) && seq <= old.seq) {
		return
	}
	ch[name] = claim{seq: seq, at: at}
}

// Now is the server's time: the newest seen, run on by the local clock since.
// Zero before anything was seen.
func (c *Claims) Now() time.Time {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if c.srvNow.IsZero() {
		return time.Time{}
	}
	return c.srvNow.Add(c.clock().Sub(c.seenAt))
}

// Claimants are the names with a claim in channel younger than ClaimKeep.
func (c *Claims) Claimants(channel string) []string {
	now := c.Now()
	c.mu.RLock()
	defer c.mu.RUnlock()
	var out []string
	for name, cl := range c.by[channel] {
		if now.Sub(cl.at) < ClaimKeep {
			out = append(out, name)
		}
	}
	slices.Sort(out)
	return out
}

// PresenceOf is what Holder reads of the presence snapshot.
type PresenceOf interface {
	Get(name string) (Info, bool)
}

// Holder is the crown of channel by the claims (RT-6.38 step 2): of the
// claimants whose presence is fresh by the server's time (Fresh, window) and,
// with member set, that are members of channel, the first by (claim time,
// stream sequence); "" for none, and before the claims were read. The MCP
// server's own view also leaves out a claimant that does not stand by the
// owner rule (lease.standing): where that differs, the bridge names another
// holder than the sessions, so no crown shows and no header line is written.
func (c *Claims) Holder(channel string, p PresenceOf, window time.Duration,
	member func(channel, name string) bool) string {
	if !c.Read() {
		return ""
	}
	now := c.Now()
	c.mu.RLock()
	type cand struct {
		name string
		cl   claim
	}
	var cands []cand
	for name, cl := range c.by[channel] {
		if now.Sub(cl.at) < ClaimKeep {
			cands = append(cands, cand{name, cl})
		}
	}
	c.mu.RUnlock()
	slices.SortFunc(cands, func(a, b cand) int {
		if d := a.cl.at.Compare(b.cl.at); d != 0 {
			return d
		}
		switch {
		case a.cl.seq < b.cl.seq:
			return -1
		case a.cl.seq > b.cl.seq:
			return 1
		}
		return strings.Compare(a.name, b.name)
	})
	for _, k := range cands {
		info, ok := p.Get(k.name)
		if !ok || !Fresh(info, now, window) {
			continue
		}
		if member != nil && !member(channel, k.name) {
			continue
		}
		return k.name
	}
	return ""
}
