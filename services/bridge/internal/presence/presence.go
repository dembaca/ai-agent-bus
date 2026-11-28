// Package presence keeps the latest presence of the NATS names in a home: the
// owner token of the subject, the bot id the body names, the status and when
// the bridge saw it; and the channels each name announced a membership of
// (RT-6.37), which the history read checks before it reads a channel. It is
// what the mirror's `shown_as` is resolved from.
package presence

import (
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"time"
)

// Status is a presence status.
type Status string

const (
	Online  Status = "online"
	Away    Status = "away"
	Offline Status = "offline"
	Bye     Status = "bye" // a session that ended (the MCP posts it on its way out)
)

// Info is one name's latest presence.
type Info struct {
	Owner  string
	Bot    string // the agent's Mattermost bot id, "" when it named none
	Status Status
	At     time.Time
}

// MemberKeep is how long a membership counts (RT-6.37, RT-6.42 step 11). The
// MCP server announces it every 6 h, so a request for a channel needs one
// younger than this by the server's time: just over one renewal, so a missed
// renewal closes the channel, and a revocation that the login service has not
// yet written as a tombstone (a later piece, #244) is bounded by the same keep
// rather than by the stream's 24 h. A channel an agent never announced, or one
// it left long enough ago, is no channel of that agent.
const MemberKeep = 7 * time.Hour

// member is one agent's announced membership of a channel, with the stream's
// sequence and time: an older message never overrides a newer one.
type member struct {
	at  time.Time
	seq uint64
}

// Snapshot is the latest presence per name, and the channels each name
// announced a membership of; the reader updates it, the links resolver and
// the history read it.
type Snapshot struct {
	mu      sync.RWMutex
	by      map[string]Info
	members map[string]map[string]member // channel -> name -> membership
	seen    time.Time                    // the newest observation, for a readiness check
	seenAt  time.Time                    // the local time seen was last advanced
	// Clock is the local clock; nil is time.Now (tests set it).
	Clock func() time.Time
}

// New returns an empty snapshot.
func New() *Snapshot {
	return &Snapshot{by: map[string]Info{}, members: map[string]map[string]member{}}
}

func (s *Snapshot) clock() time.Time {
	if s.Clock != nil {
		return s.Clock()
	}
	return time.Now()
}

// observe advances the snapshot's own clock by one message's stream time: the
// server's "now" runs on from there, so a membership ages even while nothing
// is delivered.
func (s *Snapshot) observe(at time.Time) {
	if at.After(s.seen) {
		s.seen, s.seenAt = at, s.clock()
	}
}

// Observed records one presence message.
func (s *Snapshot) Observed(name, owner, bot string, status Status, at time.Time) {
	if name == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.by[name] = Info{Owner: owner, Bot: bot, Status: status, At: at}
	s.observe(at)
}

// ObservedMember records that name announced a membership of channel, stamped
// with the stream's time and sequence; an older observation of the same pair
// is ignored.
func (s *Snapshot) ObservedMember(channel, name string, at time.Time, seq uint64) {
	if channel == "" || name == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.members == nil {
		s.members = map[string]map[string]member{}
	}
	ch := s.members[channel]
	if ch == nil {
		ch = map[string]member{}
		s.members[channel] = ch
	}
	if old, ok := ch[name]; ok && !(at.After(old.at) || (at.Equal(old.at) && seq > old.seq)) {
		return
	}
	ch[name] = member{at: at, seq: seq}
	s.observe(at)
}

// Seed sets the snapshot's clock from an answer of the server itself (the
// creation time of the bridge's new membership consumer), so that after a
// start a membership replayed from the stream reads as old, not as fresh for
// another MemberKeep before a newer record has been seen.
func (s *Snapshot) Seed(at time.Time) {
	if at.IsZero() {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.observe(at)
}

// ServerNow is the snapshot's idea of the NATS server's clock: the newest
// stream time seen, run on by the local clock since. It is what freshness is
// measured against, never this host's clock alone (RT-6.37, RT-6.42).
func (s *Snapshot) ServerNow() time.Time {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.seen.IsZero() {
		return time.Time{}
	}
	return s.seen.Add(s.clock().Sub(s.seenAt))
}

// MemberFresh says name announced a membership of channel within MemberKeep
// of the snapshot's server time: the channel is one the agent is in now.
func (s *Snapshot) MemberFresh(channel, name string) bool {
	now := s.ServerNow()
	if now.IsZero() {
		return false
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	m, ok := s.members[channel][name]
	return ok && now.Sub(m.at) <= MemberKeep
}

// Get reads one name's presence.
func (s *Snapshot) Get(name string) (Info, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	i, ok := s.by[name]
	return i, ok
}

// Names lists every name the snapshot holds a presence of.
func (s *Snapshot) Names() []string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]string, 0, len(s.by))
	for n := range s.by {
		out = append(out, n)
	}
	return out
}

// Newest is the time of the newest presence seen, the snapshot's own clock:
// comparing a presence with it avoids the two hosts' clock difference.
func (s *Snapshot) Newest() time.Time {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.seen
}

// Fresh says whether i is newer than window against now (the snapshot's newest)
// and still says the agent is there: offline and bye do not.
func Fresh(i Info, now time.Time, window time.Duration) bool {
	return !i.At.IsZero() && now.Sub(i.At) <= window &&
		i.Status != Offline && i.Status != Bye
}

// Decode reads a presence body: {"v":1,"status":...,"bot":...}.
func Decode(body []byte) (Status, string, error) {
	var raw struct {
		V      *int   `json:"v"`
		Status string `json:"status"`
		Bot    string `json:"bot"`
	}
	if err := json.Unmarshal(body, &raw); err != nil {
		return "", "", err
	}
	if raw.V == nil || *raw.V != 1 {
		return "", "", errors.New("not presence v1")
	}
	switch st := Status(raw.Status); st {
	case Online, Away, Offline, Bye:
		return st, raw.Bot, nil
	default:
		return "", "", errors.New("unknown presence status")
	}
}

// Subject splits presence.<owner>.<name>.
func Subject(subject string) (owner, name string, ok bool) {
	parts := strings.Split(subject, ".")
	if len(parts) != 3 || parts[0] != "presence" || parts[1] == "" || parts[2] == "" {
		return "", "", false
	}
	// A crown claim or a membership has four tokens; those are no presence.
	return parts[1], parts[2], true
}

// nameRE (crown.go) is an agent name as the bus writes it: the subject's last
// token.

// Membership splits presence.member.<channel>.<name> (RT-6.37): the channel an
// agent announced it is in, and the agent.
func Membership(subject string) (channel, name string, ok bool) {
	parts := strings.Split(subject, ".")
	if len(parts) != 4 || parts[0] != "presence" || parts[1] != "member" ||
		!channelRE.MatchString(parts[2]) || !nameRE.MatchString(parts[3]) {
		return "", "", false
	}
	return parts[2], parts[3], true
}

// DecodeMember reads a membership body (RT-6.37): {"v":1}. A later field the
// MCP adds is ignored, as the MCP's own claim decoder ignores the bridge's.
func DecodeMember(body []byte) error {
	var raw struct {
		V *int `json:"v"`
	}
	if err := json.Unmarshal(body, &raw); err != nil {
		return err
	}
	if raw.V == nil || *raw.V != 1 {
		return errors.New("not a membership v1")
	}
	return nil
}

// NameHashLen is the length of an agent name's hash part (#274; the login
// service's callout.NameHashLen): 25, never a Mattermost id's 26.
const NameHashLen = 25

// LinkTag is the 16 characters of an agent name's hash part that its bot's
// status shows (RT-6.37); "" for a shorter name.
func LinkTag(name string) string {
	if len(name) < NameHashLen {
		return ""
	}
	return name[len(name)-NameHashLen:][:16]
}

// LinkOf reads the tag a bot's status text shows after the ⇄ mark (RT-6.37).
func LinkOf(text string) string {
	const mark = "⇄"
	i := strings.Index(text, mark)
	if i < 0 {
		return ""
	}
	rest := []rune(text[i+len(mark):])
	if len(rest) < 16 {
		return ""
	}
	tag := string(rest[:16])
	for _, r := range tag {
		if r <= ' ' {
			return ""
		}
	}
	return tag
}
