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
	Home        string
	Log         *slog.Logger
	Mirror      *mirror.Mirror
	Store       mirror.Store
	Stream      string // "BUS"
	Prefix      string // the bus prefix, "bus" unless configured
	PropsPrefix string // the homes' MM_PROPS_PREFIX, for the message decode
}

// Run consumes until ctx ends.
func (r *Reader) Run(ctx context.Context, js nats.JetStreamContext,
	channelID func(context.Context) (string, error)) error {
	pos, ok, err := r.Store.Position(ctx, r.Home)
	if err != nil {
		return err
	}
	start := nats.DeliverNew() // a first start mirrors what comes now, not the history
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
			wait := time.Second
			for {
				err := r.one(ctx, m, channelID)
				if err == nil {
					_ = m.Ack() // BUS is a no-ack stream: the ack is a no-op there
					break
				}
				var refused *mirror.Refused
				if errors.As(err, &refused) {
					// Mattermost will refuse it again: log it, remember that it
					// was seen and go on; one post must not stop a home.
					r.Log.Warn("bridge: refused, skipped", "home", r.Home,
						"subject", m.Subject, "error", err)
					_ = r.save(ctx, mustSeq(m), mirror.Post{})
					break
				}
				if ctx.Err() != nil {
					return nil
				}
				// Transient (5xx, 429, a network or a store hiccup): retry the
				// same message with a backoff rather than drop it or end.
				r.Log.Error("bridge: not mirrored, retrying", "home", r.Home,
					"subject", m.Subject, "in", wait.String(), "error", err)
				select {
				case <-ctx.Done():
					return nil
				case <-time.After(wait):
				}
				if wait < 30*time.Second {
					wait *= 2
				}
			}
		}
	}
}

// save remembers a skipped message although the reader may be stopping.
func (r *Reader) save(ctx context.Context, seq uint64, p mirror.Post) error {
	sctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer cancel()
	return r.Store.Save(sctx, r.Home, seq, p)
}

func mustSeq(m *nats.Msg) uint64 {
	md, err := m.Metadata()
	if err != nil {
		return 0
	}
	return md.Sequence.Stream
}

func (r *Reader) one(ctx context.Context, m *nats.Msg,
	channelID func(context.Context) (string, error)) error {
	parts := strings.Split(m.Subject, ".")
	// <prefix>.ch.<home>.<sender>; anything else under the wildcard is not this
	// home's post and must not stop the reader (a fifth token, a sub-subject).
	if len(parts) != 4 || parts[0] != r.Prefix || parts[1] != "ch" || parts[2] != r.Home {
		r.Log.Warn("bridge: unexpected subject skipped", "subject", m.Subject)
		return r.save(ctx, mustSeq(m), mirror.Post{})
	}
	seq := mustSeq(m)
	msg, err := message.Decode(m.Data, r.PropsPrefix)
	if err != nil {
		// A message this bridge cannot read is not the bridge's to fix: log it
		// and advance past it, so one broken post does not stop the home.
		r.Log.Warn("bridge: unreadable message skipped", "subject", m.Subject, "error", err)
		return r.save(ctx, seq, mirror.Post{})
	}
	return r.Mirror.Handle(ctx, channelID, parts[3], seq, msg)
}
