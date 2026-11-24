package presence

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/nats-io/nats.go"
)

// Reader follows the PRESENCE stream: the latest presence per name, one
// message per subject, so every start replays what the stream holds and needs
// no position of its own.
type Reader struct {
	Log    *slog.Logger
	Stream string // "PRESENCE"
	Into   *Snapshot
}

// Run consumes until ctx ends.
func (r *Reader) Run(ctx context.Context, js nats.JetStreamContext) error {
	sub, err := js.PullSubscribe("presence.>", "", nats.BindStream(r.Stream),
		nats.DeliverAll(), nats.ManualAck(), nats.MaxAckPending(256))
	if err != nil {
		return fmt.Errorf("presence: consumer: %w", err)
	}
	defer func() { _ = sub.Unsubscribe() }()
	for {
		if ctx.Err() != nil {
			return nil
		}
		msgs, err := sub.Fetch(64, nats.MaxWait(2*time.Second))
		if errors.Is(err, nats.ErrTimeout) {
			continue
		}
		if err != nil {
			return fmt.Errorf("presence: fetch: %w", err)
		}
		for _, m := range msgs {
			r.one(m)
			_ = m.Ack()
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
	r.Into.Observed(name, owner, bot, status, time.Now())
}
