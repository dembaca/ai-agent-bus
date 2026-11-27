package presence

import (
	"bytes"
	"encoding/json"
	"errors"
	"regexp"
	"slices"
	"strings"
	"sync"
)

// Plan B's switches (ADR-014 phase 5, RT-6.39 step 5): an administrator
// switches plan B pieces on per channel in the channel directory, and the
// login service, the directory's only reader, writes each channel's pieces on
// presence.planb.<channel>.v1 at every credential issued there. The bridge
// holds no right on the directory's bucket; the operator's presence consumer
// delivers these records beside the presences (its second filter,
// presence.planb.*.v1). A channel without a record has every piece off.

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

// Switches keeps the newest plan B record per channel. The zero value is
// ready; it is safe for concurrent use.
type Switches struct {
	mu     sync.RWMutex
	pieces map[string][]int
}

// Observed takes a channel's newest record; nil pieces: every piece off. The
// consumer delivers in stream order, so the last one observed is the newest.
func (s *Switches) Observed(channel string, pieces []int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.pieces == nil {
		s.pieces = map[string][]int{}
	}
	if len(pieces) == 0 {
		delete(s.pieces, channel)
		return
	}
	s.pieces[channel] = slices.Clone(pieces)
}

// Pieces are the plan B pieces switched on in the channel, sorted; nil when
// none is, or the channel has no record that decodes.
func (s *Switches) Pieces(channel string) []int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return slices.Clone(s.pieces[channel])
}

// On says whether the piece is switched on in the channel.
func (s *Switches) On(piece int, channel string) bool {
	return slices.Contains(s.Pieces(channel), piece)
}
