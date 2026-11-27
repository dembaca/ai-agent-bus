package presence

import (
	"encoding/json"
	"errors"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"
)

// Shown is the status an agent's presence states for the bridge to show under
// the agent's own bot (ADR-014 phase 5, plan B piece 9; RT-6.6 step 1, RT-6.37
// step 6): the emoji of its state, the text, when it ends, and whether the
// agent's session holds its home's crown. Self-stated: the bridge shows the
// crown only where the claims agree (Claims.Holder), never by this alone.
type Shown struct {
	Emoji string
	Text  string
	Until time.Time
	Crown bool
}

// ShownEmojis are the states' own emoji a Shown may carry (the MCP server's
// SHOWN_EMOJIS): working, listening, idle and on the bus. Never the crown's,
// which the bridge sets by the claims.
var ShownEmojis = []string{"hammer_and_wrench", "ear", "zzz", "satellite_antenna"}

// ShownTextMax is Mattermost's custom status text limit, in characters.
const ShownTextMax = 100

// The epoch seconds a Shown's end may name, as the MCP server bounds a card's
// times: 2020-01-01 to 2100-01-01.
const (
	shownEpochMin = 1_577_836_800
	shownEpochMax = 4_102_444_800
)

var errShown = errors.New("shown: out of bounds")

// DecodeShown reads the `shown` field of a presence body: nil and no error
// when the body states none (an older MCP server, or piece 9 off in its home);
// an error when it states one out of bounds, which shows nothing. The text is
// cleaned as the MCP server cleans a card line: control and format characters
// become spaces, runs of space one, at most ShownTextMax characters.
func DecodeShown(body []byte) (*Shown, error) {
	var raw struct {
		Shown json.RawMessage `json:"shown"`
	}
	if err := json.Unmarshal(body, &raw); err != nil {
		return nil, err
	}
	if len(raw.Shown) == 0 || string(raw.Shown) == "null" {
		return nil, nil
	}
	var f struct {
		Emoji *string         `json:"emoji"`
		Text  *string         `json:"text"`
		Until json.RawMessage `json:"until"`
		Crown json.RawMessage `json:"crown"`
	}
	if err := json.Unmarshal(raw.Shown, &f); err != nil {
		return nil, errShown
	}
	if f.Emoji == nil || !slices.Contains(ShownEmojis, *f.Emoji) || f.Text == nil {
		return nil, errShown
	}
	text := CleanText(*f.Text, ShownTextMax)
	// an integer only: no fraction, no exponent, no string, no bool
	until, err := strconv.ParseInt(string(f.Until), 10, 64)
	if text == "" || err != nil || until < shownEpochMin || until > shownEpochMax {
		return nil, errShown
	}
	return &Shown{Emoji: *f.Emoji, Text: text, Until: time.Unix(until, 0), Crown: string(f.Crown) == "true"}, nil
}

// CleanText is s with control and format characters as spaces, runs of space
// as one, trimmed, and cut to max characters.
func CleanText(s string, max int) string {
	s = strings.Map(func(r rune) rune {
		if unicode.IsControl(r) || unicode.Is(unicode.Cf, r) || r == unicode.ReplacementChar {
			return ' '
		}
		return r
	}, s)
	s = strings.Join(strings.Fields(s), " ")
	if r := []rune(s); len(r) > max {
		s = strings.TrimRight(string(r[:max]), " ")
	}
	return s
}

type shownAt struct {
	shown *Shown
	at    time.Time
}

// ShownStore keeps the status each name's newest presence states (Reader
// fills it). The zero value is ready; it is safe for concurrent use.
type ShownStore struct {
	mu     sync.RWMutex
	by     map[string]shownAt
	warned map[string]bool
}

// Observed takes the status a presence of name stated at its stream time at;
// nil: none. An older presence never replaces a newer one.
func (s *ShownStore) Observed(name string, shown *Shown, at time.Time) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.by == nil {
		s.by = map[string]shownAt{}
	}
	if old, ok := s.by[name]; ok && at.Before(old.at) {
		return
	}
	s.by[name] = shownAt{shown: shown, at: at}
	if shown != nil {
		delete(s.warned, name)
	}
}

// Get is the status name's newest presence states, nil for none.
func (s *ShownStore) Get(name string) *Shown {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.by[name].shown
}

// Warn says whether a status of name out of bounds is to be logged: once,
// until one decodes again.
func (s *ShownStore) Warn(name string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.warned == nil {
		s.warned = map[string]bool{}
	}
	if s.warned[name] {
		return false
	}
	s.warned[name] = true
	return true
}
