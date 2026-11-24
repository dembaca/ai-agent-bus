// Package presence keeps the latest presence of the NATS names in a home: the
// owner token of the subject, the bot id the body names, the status and when
// the bridge saw it. It is what the mirror's `shown_as` is resolved from.
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
)

// Info is one name's latest presence.
type Info struct {
	Owner  string
	Bot    string // the agent's Mattermost bot id, "" when it named none
	Status Status
	At     time.Time
}

// Snapshot is the latest presence per name; the reader updates it, the links
// resolver reads it.
type Snapshot struct {
	mu   sync.RWMutex
	by   map[string]Info
	seen time.Time // the newest observation, for a readiness check
}

// New returns an empty snapshot.
func New() *Snapshot { return &Snapshot{by: map[string]Info{}} }

// Observed records one presence message.
func (s *Snapshot) Observed(name, owner, bot string, status Status, at time.Time) {
	if name == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.by[name] = Info{Owner: owner, Bot: bot, Status: status, At: at}
	if at.After(s.seen) {
		s.seen = at
	}
}

// Get reads one name's presence.
func (s *Snapshot) Get(name string) (Info, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	i, ok := s.by[name]
	return i, ok
}

// Fresh says whether i is newer than window.
func Fresh(i Info, now time.Time, window time.Duration) bool {
	return !i.At.IsZero() && now.Sub(i.At) <= window && i.Status != Offline
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
	case Online, Away, Offline:
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

// LinkTag is the 16 characters of an agent name's hash part that its bot's
// status shows (RT-6.37); "" for a shorter name.
func LinkTag(name string) string {
	if len(name) < 26 {
		return ""
	}
	return name[len(name)-26:][:16]
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
