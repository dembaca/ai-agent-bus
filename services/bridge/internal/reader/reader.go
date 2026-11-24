// Package reader pulls a home's messages from the BUS stream and hands them to
// the mirror. The position lives in the bridge's store, and every start uses
// an ephemeral pull consumer from that position: a restart neither repeats
// (the id map) nor loses (the stream) a message.
package reader

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/message"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mirror"
)

// Reader mirrors one home.
type Reader struct {
	Home   string
	Log    *slog.Logger
	Mirror *mirror.Mirror
	Store  mirror.Store
	Stream string // "BUS"
	Prefix string // "bus"
}

// Run consumes until ctx ends.
func (r *Reader) Run(ctx context.Context, js nats.JetStreamContext,
	channelID func(context.Context) (string, error)) error {
	pos, ok, err := r.Store.Position(ctx)
	if err != nil {
		return err
	}
	start := nats.DeliverAll()
	if ok {
		start = nats.StartSequence(pos + 1)
	}
	// An ephemeral pull consumer: the store's position is the truth, so a
	// durable consumer's own ack state cannot drift from it after a crash.
	sub, err := js.PullSubscribe(r.Prefix+".ch."+r.Home+".>", "",
		nats.BindStream(r.Stream), start, nats.ManualAck(), nats.MaxAckPending(64))
	if err != nil {
		return fmt.Errorf("reader: consumer: %w", err)
	}
	defer func() { _ = sub.Unsubscribe() }()
	r.Log.Info("bridge: reading a home", "home", r.Home, "from", pos)
	for {
		if ctx.Err() != nil {
			return nil
		}
		msgs, err := sub.Fetch(16, nats.MaxWait(2*time.Second))
		if errors.Is(err, nats.ErrTimeout) {
			continue
		}
		if err != nil {
			return fmt.Errorf("reader: fetch: %w", err)
		}
		for _, m := range msgs {
			if err := r.one(ctx, m, channelID); err != nil {
				// No ack: the message stays for the next pull; a broken
				// Mattermost or store is retried rather than dropped.
				r.Log.Error("bridge: not mirrored", "home", r.Home,
					"subject", m.Subject, "error", err)
				return err
			}
			_ = m.Ack() // BUS is a no-ack stream: the ack is a no-op there
		}
	}
}

func (r *Reader) one(ctx context.Context, m *nats.Msg,
	channelID func(context.Context) (string, error)) error {
	parts := strings.Split(m.Subject, ".")
	// <prefix>.ch.<home>.<sender>
	if len(parts) != 4 || parts[0] != r.Prefix || parts[1] != "ch" || parts[2] != r.Home {
		return fmt.Errorf("reader: unexpected subject %q", m.Subject)
	}
	seq := m.MetaData().StreamSequence
	msg, err := message.Decode(m.Data)
	if err != nil {
		// A message this bridge cannot read is not the bridge's to fix: log it
		// and advance past it, so one broken post does not stop the home.
		r.Log.Warn("bridge: unreadable message skipped", "subject", m.Subject, "error", err)
		return r.Store.Save(ctx, r.Home, seq, mirror.Post{})
	}
	return r.Mirror.Handle(ctx, channelID, parts[3], seq, msg)
}
