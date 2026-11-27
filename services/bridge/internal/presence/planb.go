package presence

import (
	"bytes"
	"encoding/json"
	"errors"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"
)

// Plan B's switches (ADR-014 phase 5, RT-6.39 step 5): an administrator
// switches plan B pieces on per channel in the channel directory, and the
// login service, the directory's only reader, writes each channel's pieces on
// presence.planb.<channel>.v1 at every credential issued there. The bridge
// holds no right on the directory's bucket; it reads these records through
// its own consumer, bridge_planb (planb_consumer.go), made at every start so
// that every current record is replayed. A channel without a record, or whose
// record is two republish periods old, has every piece off.

// MaxPiece is the highest plan B piece with a switch (14, registration ends,
// has none).
const MaxPiece = 13

// channelRE is a channel's token on the bus, as the directory keeps it.
var channelRE = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)

// PlanBSubject gives the channel of a plan B record's subject.
func PlanBSubject(subject string) (channel string, ok bool) {
	parts := strings.Split(subject, ".")
	if len(parts) != 4 || parts[0] != "presence" || parts[1] != "planb" || parts[3] != "v1" ||
		!channelRE.MatchString(parts[2]) {
		return "", false
	}
	return parts[2], true
}

// DecodePlanB reads a record strictly: exactly v (1), channel (this one) and
// pieces (1 to MaxPiece, each once); returns the pieces sorted.
func DecodePlanB(data []byte, channel string) ([]int, error) {
	var keys map[string]json.RawMessage
	if err := json.Unmarshal(data, &keys); err != nil {
		return nil, errors.New("plan B record: not a JSON object")
	}
	if len(keys) != 3 || keys["v"] == nil || keys["channel"] == nil || keys["pieces"] == nil {
		return nil, errors.New("plan B record: the keys are not v, channel and pieces")
	}
	var r struct {
		V       int    `json:"v"`
		Channel string `json:"channel"`
		Pieces  []int  `json:"pieces"`
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	if err := dec.Decode(&r); err != nil {
		return nil, errors.New("plan B record: a key of the wrong type")
	}
	if r.V != 1 {
		return nil, errors.New("plan B record: v is not 1")
	}
	if r.Channel != channel {
		return nil, errors.New("plan B record: of another channel")
	}
	if r.Pieces == nil {
		return nil, errors.New("plan B record: pieces is not a list")
	}
	out := slices.Clone(r.Pieces)
	slices.Sort(out)
	for i, n := range out {
		if n < 1 || n > MaxPiece || (i > 0 && out[i-1] == n) {
			return nil, errors.New("plan B record: a piece outside 1 to 13, or twice")
		}
	}
	if len(out) == 0 {
		return nil, nil
	}
	return out, nil
}

// PlanBStale is how old a record may be (by the server's time) before it
// counts as off: two republish periods of the login service (callout.
// PlanBRepublish, one hour). A record the login service no longer renews, from
// a failed write, a rollback or a lost right, thus switches nothing on for
// longer than that.
const PlanBStale = 2 * time.Hour

type planBRec struct {
	pieces []int
	at     time.Time // the stream's time of the record
}

// Switches keeps the newest plan B record per channel. The zero value is
// ready; it is safe for concurrent use.
type Switches struct {
	mu      sync.RWMutex
	recs    map[string]planBRec
	staging map[string]planBRec // a new consumer's replay until it caught up
	// the newest server time seen, and the local time it was seen at: the
	// server's "now" runs on from there
	srvNow, seenAt time.Time
	// Clock is the local clock; nil is time.Now (tests set it).
	Clock func() time.Time
}

func (s *Switches) clock() time.Time {
	if s.Clock != nil {
		return s.Clock()
	}
	return time.Now()
}

// Begin starts a replay by a new consumer: its records go to a map of their
// own, which replaces the old one at Commit, so a record that has gone from
// the stream leaves no switch behind.
func (s *Switches) Begin() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.staging = map[string]planBRec{}
}

// Commit ends the replay: the records read replace the earlier ones.
func (s *Switches) Commit() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.staging != nil {
		s.recs, s.staging = s.staging, nil
	}
}

// Observed takes a channel's newest record, stamped with the stream's time;
// nil pieces: every piece off. The consumer delivers in stream order, so the
// last one observed is the newest.
func (s *Switches) Observed(channel string, pieces []int, at time.Time) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if at.After(s.srvNow) {
		s.srvNow, s.seenAt = at, s.clock()
	}
	m := s.recs
	if s.staging != nil {
		m = s.staging
	}
	if m == nil {
		m = map[string]planBRec{}
		s.recs = m
	}
	if len(pieces) == 0 {
		delete(m, channel)
		return
	}
	m[channel] = planBRec{pieces: slices.Clone(pieces), at: at}
}

// Pieces are the plan B pieces switched on in the channel, sorted; nil when
// none is, the channel has no record that decodes, or its record is older
// than PlanBStale by the server's time.
func (s *Switches) Pieces(channel string) []int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	rec, ok := s.recs[channel]
	if !ok || s.srvNow.IsZero() {
		return nil
	}
	if now := s.srvNow.Add(s.clock().Sub(s.seenAt)); now.Sub(rec.at) >= PlanBStale {
		return nil
	}
	return slices.Clone(rec.pieces)
}

// On says whether the piece is switched on in the channel.
func (s *Switches) On(piece int, channel string) bool {
	return slices.Contains(s.Pieces(channel), piece)
}
