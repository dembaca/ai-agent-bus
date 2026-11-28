package callout

// The attestation (attested trust lists, RT-6.41): at every credential issue the
// service vouches, on a subject only it may publish on, for the agent's owner
// and the trust list the host named. Agents read it to resolve an owner's
// anchor and to decide on peers; they cannot write it (perms.go gives an agent
// a three-token presence subject and nothing with the fourth token ".v1").

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/directory"
)

// MaxAttestation bounds the encoded body. The PRESENCE stream drops a message
// above its max_msg_size (4096, consumers.PresenceStreamJSON) without a word,
// so this is that limit, not more.
const MaxAttestation = 4096

// Attester publishes an attestation on a subject (NATSAttester). An error
// refuses the login: a credential without its attestation would look whole.
type Attester interface {
	Publish(ctx context.Context, subject string, body []byte) error
}

// Trust resolves the names of a host's trust list (directory.Trust). cut says
// that names were left out.
type Trust interface {
	Resolve(ctx context.Context, names []string) ([]directory.Trusted, bool, error)
}

// Attestation is the body published for an agent.
type Attestation struct {
	V       int                 `json:"v"`
	Agent   string              `json:"agent"`
	Home    string              `json:"home"`
	Owner   directory.Trusted   `json:"owner"`
	Trusted []directory.Trusted `json:"trusted"`
	Cut     bool                `json:"cut"`
	Iat     int64               `json:"iat"`
	Exp     int64               `json:"exp"`
}

// encode is the compact JSON of a, with entries dropped from the end of
// Trusted (and Cut set) until it fits MaxAttestation.
func (a Attestation) encode() ([]byte, error) {
	b, _, err := a.fit()
	return b, err
}

// fit is encode that also returns the attestation as encoded.
func (a Attestation) fit() ([]byte, Attestation, error) {
	if a.Trusted == nil {
		a.Trusted = []directory.Trusted{}
	}
	for {
		b, err := json.Marshal(a)
		if err != nil || len(b) <= MaxAttestation || len(a.Trusted) == 0 {
			return b, a, err
		}
		a.Trusted, a.Cut = a.Trusted[:len(a.Trusted)-1], true
	}
}

// attestSubject is where the attestation of agent is published.
func (s *Service) attestSubject(agent string) string {
	return s.cfg.Subjects.Presence + ".attest." + agent + ".v1"
}

// SetAttester sets the attestation publisher. nil (the default) makes none and
// is for unit tests of this package only: the service's wiring
// (app.RunCallout) always sets it.
func (s *Service) SetAttester(a Attester) {
	s.cmu.Lock()
	defer s.cmu.Unlock()
	s.attester = a
}

// SetTrust sets where a host's trust list is resolved. nil (the default)
// takes each name as the anchor, which is what the directory gives every
// identity before the import.
func (s *Service) SetTrust(t Trust) {
	s.cmu.Lock()
	defer s.cmu.Unlock()
	s.trust = t
}

func (s *Service) attestHooks() (Attester, Trust) {
	s.cmu.RLock()
	defer s.cmu.RUnlock()
	return s.attester, s.trust
}

// TrustIndex keeps the login service's trust index (directory.TrustIndex,
// RT-6.43 step 3): per agent, from this issue's own checks only, the owner's
// anchor, the anchors its attestation names as trusted and the channels
// granted. An operator's credential reads it (Persons.Visible).
type TrustIndex interface {
	Index(ctx context.Context, r directory.AgentTrust) error
}

// SetTrustIndex sets the trust index. nil (the default) writes none, and an
// operator then sees no channel through trust.
func (s *Service) SetTrustIndex(ix TrustIndex) {
	s.cmu.Lock()
	defer s.cmu.Unlock()
	s.trustIndex = ix
}

func (s *Service) trustIndexHook() TrustIndex {
	s.cmu.RLock()
	defer s.cmu.RUnlock()
	return s.trustIndex
}

// indexTrust writes the agent's record from its published attestation and
// the granted channels without the lobby. An error refuses the login: the
// record before, possibly with a trust the host has dropped, would stay.
func (s *Service) indexTrust(ctx context.Context, ix TrustIndex, a Attestation, channels []string) error {
	r := directory.AgentTrust{Agent: a.Agent, Owner: a.Owner.Anchor, Trusted: []string{}, Exp: a.Exp}
	for _, t := range a.Trusted {
		if t.Anchor != "" {
			r.Trusted = append(r.Trusted, t.Anchor)
		}
	}
	for _, ch := range channels {
		if ch != s.cfg.Lobby {
			r.Channels = append(r.Channels, ch)
		}
	}
	return ix.Index(ctx, r)
}

// attest resolves the host's names and publishes the attestation; it
// returns the attestation as published (Trusted cut to fit).
func (s *Service) attest(ctx context.Context, a Attester, t Trust, agent, home string, owner directory.Trusted, names []string, hostCut bool, iat, exp int64) (Attestation, error) {
	var (
		list []directory.Trusted
		cut  bool
		err  error
	)
	if t != nil {
		list, cut, err = t.Resolve(ctx, names)
	} else {
		// Before the import no store is read: the name is the anchor.
		list, cut, err = (*directory.Store)(nil).ResolveTrusted(ctx, names, false)
	}
	if err != nil {
		return Attestation{}, err
	}
	body, att, err := Attestation{V: 1, Agent: agent, Home: home, Owner: owner, Trusted: list, Cut: cut || hostCut, Iat: iat, Exp: exp}.fit()
	if err != nil {
		return Attestation{}, err
	}
	return att, a.Publish(ctx, s.attestSubject(agent), body)
}

// NATSAttester publishes on the service's JetStream connection in the tenant
// account. The PRESENCE stream does not acknowledge (no_ack), so there is no
// PubAck to wait for: the publish is flushed within ctx's deadline, which
// confirms the server read it.
type NATSAttester struct {
	NC     *nats.Conn
	Stream string // the stream that stores it (consumers.PresenceStream)
}

// Publish implements Attester. The PRESENCE stream acknowledges nothing
// (no_ack), and a refused publish shows only asynchronously; so the
// attestation counts as written only once the stream returns exactly these
// bytes as the last message on its subject. Until then, or past ctx, the
// login is refused: a credential without its attestation would look whole.
func (n NATSAttester) Publish(ctx context.Context, subject string, body []byte) error {
	return n.publish(ctx, subject, body, func(stored []byte) bool { return bytes.Equal(stored, body) })
}

// PublishRecord is Publish for a subject several logins may write (an account
// record: one per account, not per agent): it counts as written once the
// stream's last message on the subject is these bytes or one accept takes as
// at least as good, so a second login's record landing first does not make
// this one wait out its deadline (RecordPublisher).
func (n NATSAttester) PublishRecord(ctx context.Context, subject string, body []byte, accept func(stored []byte) bool) error {
	return n.publish(ctx, subject, body, func(stored []byte) bool { return bytes.Equal(stored, body) || accept(stored) })
}

func (n NATSAttester) publish(ctx context.Context, subject string, body []byte, ok func(stored []byte) bool) error {
	if err := n.NC.Publish(subject, body); err != nil {
		return err
	}
	if err := n.NC.FlushWithContext(ctx); err != nil {
		return err
	}
	req, _ := json.Marshal(map[string]string{"last_by_subj": subject})
	for {
		m, err := n.NC.RequestWithContext(ctx, "$JS.API.STREAM.MSG.GET."+n.Stream, req)
		if err != nil {
			return fmt.Errorf("reading it back: %w", err)
		}
		var got struct {
			Message *struct {
				Data []byte `json:"data"`
			} `json:"message"`
			Error *struct {
				Code        int    `json:"code"`
				Description string `json:"description"`
			} `json:"error"`
		}
		if err := json.Unmarshal(m.Data, &got); err != nil {
			return fmt.Errorf("reading it back: %w", err)
		}
		if got.Message != nil && ok(got.Message.Data) {
			return nil
		}
		if got.Error != nil && got.Error.Code != 404 {
			return fmt.Errorf("reading it back: %s", got.Error.Description)
		}
		select { // not stored yet (or an older one): look again shortly
		case <-ctx.Done():
			return fmt.Errorf("not stored: %w", ctx.Err())
		case <-time.After(10 * time.Millisecond):
		}
	}
}
