package presence

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// Reader follows the PRESENCE stream into a snapshot. A named consumer with
// the narrowest filter (presence.*.*: a name's presence, never a crown claim or
// a membership). It is deleted and made anew on every start: the stream keeps
// one message per subject, so deliver-all replays the latest presence of every
// subject, and the snapshot after a restart is not empty until each agent's
// next heartbeat.
type Reader struct {
	Log    *slog.Logger
	Stream string // "PRESENCE"
	Into   *Snapshot
}

// ConsumerName is the bridge's presence consumer; the NATS user's rights are
// granted on exactly this name.
const ConsumerName = "bridge_presence"

// config is the one consumer the bridge makes on PRESENCE.
func config() jetstream.ConsumerConfig {
	return jetstream.ConsumerConfig{
		Name: ConsumerName, FilterSubject: "presence.*.*",
		DeliverPolicy: jetstream.DeliverAllPolicy, AckPolicy: jetstream.AckNonePolicy,
		MaxAckPending:     256,
		InactiveThreshold: time.Hour,
	}
}

// Exists tells whether PRESENCE is there, with the rights the bridge has: its
// own consumer's create and delete, no STREAM.INFO (whose subjects filter would
// list who writes direct messages to whom). A missing stream is not an error.
func Exists(ctx context.Context, js jetstream.JetStream) (bool, error) {
	err := js.DeleteConsumer(ctx, "PRESENCE", ConsumerName)
	switch {
	case err == nil, errors.Is(err, jetstream.ErrConsumerNotFound):
	case errors.Is(err, jetstream.ErrStreamNotFound):
		return false, nil
	default:
		return false, fmt.Errorf("presence: %w", err)
	}
	if _, err := js.CreateOrUpdateConsumer(ctx, "PRESENCE", config()); err != nil {
		if errors.Is(err, jetstream.ErrStreamNotFound) {
			return false, nil
		}
		return false, fmt.Errorf("presence: %w", err)
	}
	// Run makes its own, from the start of the stream
	if err := js.DeleteConsumer(ctx, "PRESENCE", ConsumerName); err != nil &&
		!errors.Is(err, jetstream.ErrConsumerNotFound) {
		return false, fmt.Errorf("presence: %w", err)
	}
	return true, nil
}

// Run consumes until ctx ends.
func (r *Reader) Run(ctx context.Context, js jetstream.JetStream) error {
	// A consumer that survived a restart has delivered the presences, so it
	// would replay only what comes next; without it, deliver-all replays the
	// latest per subject, and `shown_as` knows the names from the first post.
	if err := js.DeleteConsumer(ctx, r.Stream, ConsumerName); err != nil &&
		!errors.Is(err, jetstream.ErrConsumerNotFound) {
		return fmt.Errorf("presence: consumer delete: %w", err)
	}
	cons, err := js.CreateOrUpdateConsumer(ctx, r.Stream, jetstream.ConsumerConfig{
		Name: ConsumerName, FilterSubject: "presence.*.*",
		DeliverPolicy: jetstream.DeliverAllPolicy, AckPolicy: jetstream.AckNonePolicy,
		MaxAckPending:     256,
		InactiveThreshold: time.Hour,
	})
	if err != nil {
		return fmt.Errorf("presence: consumer: %w", err)
	}
	for {
		if ctx.Err() != nil {
			return nil
		}
		batch, err := cons.Fetch(64, jetstream.FetchMaxWait(2*time.Second))
		if err != nil {
			if errors.Is(err, nats.ErrTimeout) || errors.Is(err, context.DeadlineExceeded) {
				continue
			}
			return fmt.Errorf("presence: fetch: %w", err)
		}
		for m := range batch.Messages() {
			r.one(m)
		}
		if berr := batch.Error(); berr != nil && !errors.Is(berr, nats.ErrTimeout) &&
			!errors.Is(berr, context.DeadlineExceeded) {
			return fmt.Errorf("presence: batch: %w", berr)
		}
	}
}

func (r *Reader) one(m jetstream.Msg) {
	owner, name, ok := Subject(m.Subject())
	if !ok {
		return
	}
	status, bot, err := Decode(m.Data())
	if err != nil {
		r.Log.Warn("presence: unreadable", "subject", m.Subject(), "error", err)
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
