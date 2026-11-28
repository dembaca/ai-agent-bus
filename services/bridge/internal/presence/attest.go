package presence

import (
	"bytes"
	"encoding/json"
	"errors"
	"regexp"
	"strings"
	"sync"
	"time"
)

// MaxTrusted is how many trusted humans an attestation holds.
const MaxTrusted = 32

var (
	// attestAnchorRE is the login service's anchor.
	attestAnchorRE = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,63}$`)
	// attestNameRE is a human's name as the login service reads it.
	attestNameRE = regexp.MustCompile(`^[a-z][a-z0-9._-]{0,63}$`)
)

// Attestation is what the bridge keeps of the login service's attestation of
// one agent (presence.attest.<agent>.v1, written by services/auth
// internal/callout): whom it names as owner and as trusted humans, and the
// home it states.
type Attestation struct {
	Owner   string   // the owner's anchor
	Trusted []string // the trusted humans' anchors
	Home    string   // the home the login service stated
	HasHome bool     // false: an older login service stated none
	Exp     time.Time
}

// Names says whether anchor is the owner or one of the trusted humans.
func (a Attestation) Names(anchor string) bool {
	if anchor == a.Owner {
		return true
	}
	for _, t := range a.Trusted {
		if t == anchor {
			return true
		}
	}
	return false
}

type attestPerson struct {
	Anchor *string `json:"anchor"`
	Name   *string `json:"name"`
}

func (p *attestPerson) ok() bool {
	return p != nil && p.Anchor != nil && attestAnchorRE.MatchString(*p.Anchor) &&
		p.Name != nil && attestNameRE.MatchString(*p.Name)
}

// AttestAgent is the agent of presence.attest.<agent>.v1.
func AttestAgent(subject string) (agent string, ok bool) {
	parts := strings.Split(subject, ".")
	if len(parts) != 4 || parts[0] != "presence" || parts[1] != "attest" || parts[3] != "v1" ||
		!nameRE.MatchString(parts[2]) {
		return "", false
	}
	return parts[2], true
}

// DecodeAttestation reads an attestation body strictly (as the MCP's
// natsbus.decode_attestation): v 1, the agent of the subject, an owner and at
// most MaxTrusted trusted persons with well-formed anchors and names, cut a
// boolean, no other field, exp after iat. home may be absent.
func DecodeAttestation(agent string, body []byte) (Attestation, error) {
	var raw struct {
		V       *int            `json:"v"`
		Agent   *string         `json:"agent"`
		Home    *string         `json:"home"`
		Owner   *attestPerson   `json:"owner"`
		Trusted *[]attestPerson `json:"trusted"`
		Cut     *bool           `json:"cut"`
		Iat     *int64          `json:"iat"`
		Exp     *int64          `json:"exp"`
	}
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&raw); err != nil || dec.More() {
		return Attestation{}, errors.New("not an attestation v1")
	}
	switch {
	case raw.V == nil || *raw.V != 1:
		return Attestation{}, errors.New("v is not 1")
	case raw.Agent == nil || *raw.Agent != agent:
		return Attestation{}, errors.New("attestation of another agent")
	case !raw.Owner.ok():
		return Attestation{}, errors.New("owner has a bad anchor or name")
	case raw.Trusted == nil || len(*raw.Trusted) > MaxTrusted:
		return Attestation{}, errors.New("trusted is not a list of at most 32")
	case raw.Cut == nil:
		return Attestation{}, errors.New("cut is not a boolean")
	case raw.Home != nil && *raw.Home != "" && !channelRE.MatchString(*raw.Home):
		return Attestation{}, errors.New("home is no channel name")
	case raw.Iat == nil || raw.Exp == nil || *raw.Exp <= *raw.Iat:
		return Attestation{}, errors.New("exp is not after iat")
	}
	a := Attestation{Owner: *raw.Owner.Anchor, Trusted: []string{}, Exp: time.Unix(*raw.Exp, 0)}
	for i := range *raw.Trusted {
		t := &(*raw.Trusted)[i]
		if !t.ok() {
			return Attestation{}, errors.New("a trusted entry has a bad anchor or name")
		}
		a.Trusted = append(a.Trusted, *t.Anchor)
	}
	if raw.Home != nil {
		a.Home, a.HasHome = *raw.Home, true
	}
	return a, nil
}

// Attestations keeps, per agent, the newest attestation by stream sequence.
type Attestations struct {
	// Now is the server's time (Snapshot.ServerNow); a zero time (nothing seen
	// yet) makes every attestation stale.
	Now func() time.Time

	mu sync.RWMutex
	m  map[string]attestEntry
}

type attestEntry struct {
	a   Attestation
	seq uint64
}

// Observed keeps a for agent unless one of a later stream sequence is held.
func (s *Attestations) Observed(agent string, a Attestation, seq uint64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.m == nil {
		s.m = map[string]attestEntry{}
	}
	if old, ok := s.m[agent]; ok && old.seq >= seq {
		return
	}
	s.m[agent] = attestEntry{a, seq}
}

// Get is the agent's attestation, false when there is none or it expired
// before the server's time now.
func (s *Attestations) Get(agent string) (Attestation, bool) {
	s.mu.RLock()
	e, ok := s.m[agent]
	s.mu.RUnlock()
	if !ok || s.Now == nil {
		return Attestation{}, false
	}
	now := s.Now()
	if now.IsZero() || e.a.Exp.Before(now) {
		return Attestation{}, false
	}
	return e.a, true
}
