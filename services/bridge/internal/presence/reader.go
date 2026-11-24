package presence

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/nats-io/nats.go"
)

// ConsumerName is the operator's presence consumer. The bridge has no right to
// make or fetch from a consumer; the operator creates this one and the server
// delivers to deliver.bridge.presence.
const ConsumerName = "bridge_presence"

// Reader follows the operator's presence push consumer into a snapshot. The
// consumer's deliver policy is the latest presence per subject, so a new
// consumer starts with a full picture. After a bridge restart the consumer is
// not recreated, so a name is unknown until that agent's next presence: a
// display fallback to the NATS name, never a lost post.
type Reader struct {
	Log     *slog.Logger
	Deliver string // deliver.bridge.presence
	Into    *Snapshot
	Ready   chan struct{} // closed once the subscription is active (tests wait on it)
}

// Run consumes until ctx ends.
func (r *Reader) Run(ctx context.Context, nc *nats.Conn) error {
	ch := make(chan *nats.Msg, 256)
	sub, err := nc.ChanSubscribe(r.Deliver, ch)
	if err != nil {
		return fmt.Errorf("presence: subscribe %s: %w", r.Deliver, err)
	}
	defer func() { _ = sub.Unsubscribe() }()
	if err := nc.Flush(); err != nil {
		return fmt.Errorf("presence: subscribe %s: %w", r.Deliver, err)
	}
	if r.Ready != nil {
		close(r.Ready)
	}
	for {
		select {
		case <-ctx.Done():
			return nil
		case m := <-ch:
			r.one(m)
		}
	}
}

func (r *Reader) one(m *nats.Msg) {
	owner, name, ok := Subject(m.Subject)
	if !ok {
		return
	}
	status, bot, err := Decode(m.Data)
	if err != nil {
		r.Log.Warn("presence: unreadable", "subject", m.Subject, "error", err)
		return
	}
	// The stream's own time, not this host's clock: a presence is fresh
	// relative to what the server delivered, as the MCP reads it too.
	at := time.Now()
	if md, err := m.Metadata(); err == nil && !md.Timestamp.IsZero() {
		at = md.Timestamp
	}
	r.Into.Observed(name, owner, bot, status, at)
}
