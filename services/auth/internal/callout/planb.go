package callout

import (
	"context"
	"encoding/json"
	"sync"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/directory"
)

// Plan B's switches at a credential (ADR-014 phase 5, RT-6.39 step 5). An
// administrator switches plan B pieces on per channel in the directory; at
// every credential the service writes, for each granted channel, the
// pieces switched on there on <presence>.planb.<channel>.v1, a subject only
// it may publish. The host never sees its user JWT, so the record beside it
// is what the session reads: each agent's presence consumer delivers the
// records of its own channels (consumers.Manager), the bridge's that of every
// channel. A channel without a record has every piece off.

// PlanB reads the stored plan B pieces of channels (directory.Store).
type PlanB interface {
	PlanB(ctx context.Context, channels []string) (map[string][]int, error)
}

// PlanBPublisher writes one channel's record (NATSPlanB).
type PlanBPublisher interface {
	Publish(ctx context.Context, subject string, body []byte) error
}

// PlanBRepublish is how long an unchanged record goes unwritten: after it the
// next credential in the channel writes it again, so the PRESENCE stream
// (consumers.PresenceMaxAge, 24 h) keeps it while agents log in there.
const PlanBRepublish = time.Hour

// PlanBRecord is the body on <presence>.planb.<channel>.v1: the pieces
// switched on, sorted, each once; empty when none is.
type PlanBRecord struct {
	V       int    `json:"v"`
	Channel string `json:"channel"`
	Pieces  []int  `json:"pieces"`
}

type planBHook struct {
	mu   sync.Mutex
	src  PlanB
	pub  PlanBPublisher
	sent map[string]planBSent // channel -> what was written last, and when
}

type planBSent struct {
	body string
	at   time.Time
}

// SetPlanB sets where the switches are read and how their records are
// written. Unset (the default), no record is written and every piece stays
// off on the hosts.
func (s *Service) SetPlanB(src PlanB, pub PlanBPublisher) {
	s.planb.mu.Lock()
	defer s.planb.mu.Unlock()
	s.planb.src, s.planb.pub, s.planb.sent = src, pub, map[string]planBSent{}
}

func (s *Service) planBSubject(channel string) string {
	return s.cfg.Subjects.Presence + ".planb." + channel + ".v1"
}

// publishPlanB writes the record of each granted channel that changed since
// this process last wrote it, or was written more than PlanBRepublish ago.
// Never a reason to refuse the credential: a read or write that fails is
// logged and leaves the records as the stream has them (off where it has
// none), and the next credential tries again. An entry outside 1 to 13 is
// dropped and logged.
func (s *Service) publishPlanB(ctx context.Context, agent string, channels []string, now time.Time) {
	s.planb.mu.Lock()
	src, pub := s.planb.src, s.planb.pub
	s.planb.mu.Unlock()
	if src == nil || pub == nil || len(channels) == 0 {
		return
	}
	stored, err := src.PlanB(ctx, channels)
	if err != nil {
		s.log.Warn("auth callout", "event", "plan B not read", "agent", agent, "reason", err.Error())
		return
	}
	for _, ch := range channels {
		valid, dropped := directory.Pieces(stored[ch])
		if valid == nil {
			valid = []int{}
		}
		body, err := json.Marshal(PlanBRecord{V: 1, Channel: ch, Pieces: valid})
		if err != nil || !s.planBDue(ch, string(body), now) {
			continue
		}
		if len(dropped) > 0 {
			s.log.Warn("auth callout", "event", "plan B entry dropped", "channel", ch, "dropped", dropped)
		}
		if err := pub.Publish(ctx, s.planBSubject(ch), body); err != nil {
			s.log.Warn("auth callout", "event", "plan B record not written", "channel", ch, "reason", err.Error())
			return
		}
		s.planb.mu.Lock()
		s.planb.sent[ch] = planBSent{body: string(body), at: now}
		s.planb.mu.Unlock()
	}
}

func (s *Service) planBDue(ch, body string, now time.Time) bool {
	s.planb.mu.Lock()
	defer s.planb.mu.Unlock()
	last, ok := s.planb.sent[ch]
	return !ok || last.body != body || now.Sub(last.at) >= PlanBRepublish
}

// NATSPlanB publishes on the service's JetStream connection in the tenant
// account. Unlike the attestation it is not read back: the record is no
// condition of the credential, and a server configuration without the right
// (`<presence>.planb.*.v1` for the service's JetStream user) must not hold
// every login up to its deadline. Such a refusal shows only in the
// connection's error log, and the hosts keep every piece off.
type NATSPlanB struct {
	NC *nats.Conn
}

// Publish implements PlanBPublisher: the record is flushed within ctx.
func (n NATSPlanB) Publish(ctx context.Context, subject string, body []byte) error {
	if err := n.NC.Publish(subject, body); err != nil {
		return err
	}
	return n.NC.FlushWithContext(ctx)
}
