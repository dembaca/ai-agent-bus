// Package reader pulls a home's messages from the BUS stream and hands them to
// the mirror. The position lives in the bridge's store, and every start uses a
// named pull consumer with the narrowest filter the stream allows, so the
// bridge's NATS user needs rights on that one consumer and nothing broad.
package reader

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

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

// ConsumerName is the bridge's consumer for a home: one name per home, the
// NATS user's rights are granted on exactly these.
func ConsumerName(home string) string {
	safe := strings.Map(func(r rune) rune {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-', r == '_':
			return r
		default:
			return '_'
		}
	}, home)
	return "bridge_" + safe
}

// Run consumes until ctx ends.
func (r *Reader) Run(ctx context.Context, js jetstream.JetStream,
	channelID func(context.Context) (string, error)) error {
	pos, ok, err := r.Store.Position(ctx, r.Home)
	if err != nil {
		return err
	}
	cfg := jetstream.ConsumerConfig{
		Name:          ConsumerName(r.Home),
		FilterSubject: r.Prefix + ".ch." + r.Home + ".*",
		AckPolicy:     jetstream.AckNonePolicy,
		MaxAckPending: 64,
	}
	if ok {
		cfg.DeliverPolicy = jetstream.DeliverByStartSequencePolicy
		cfg.OptStartSeq = pos + 1
	} else {
		cfg.DeliverPolicy = jetstream.DeliverNewPolicy // a first start mirrors what comes now
	}
	// The consumer is recreated on every start: the store's position is the
	// truth, and a consumer that survived a crash would have moved past a post
	// whose Mattermost copy was written but not yet recorded (AckNone streams
	// deliver once).
	if err := js.DeleteConsumer(ctx, r.Stream, cfg.Name); err != nil &&
		!errors.Is(err, jetstream.ErrConsumerNotFound) {
		return fmt.Errorf("reader: consumer delete: %w", err)
	}
	cons, err := js.CreateOrUpdateConsumer(ctx, r.Stream, cfg)
	if err != nil {
		return fmt.Errorf("reader: consumer: %w", err)
	}
	r.Log.Info("bridge: reading a home", "home", r.Home, "from", pos, "consumer", cfg.Name)
	for {
		if ctx.Err() != nil {
			return nil
		}
		batch, err := cons.Fetch(16, jetstream.FetchMaxWait(2*time.Second))
		if err != nil {
			if errors.Is(err, nats.ErrTimeout) || errors.Is(err, context.DeadlineExceeded) {
				continue
			}
			return fmt.Errorf("reader: fetch: %w", err)
		}
		got := 0
		for m := range batch.Messages() {
			got++
			if err := r.retry(ctx, m, channelID); err != nil {
				return err
			}
		}
		if berr := batch.Error(); berr != nil && !errors.Is(berr, nats.ErrTimeout) &&
			!errors.Is(berr, context.DeadlineExceeded) {
			return fmt.Errorf("reader: batch: %w", berr)
		}
		if got == 0 {
			continue
		}
	}
}

// retry mirrors one message; a refusal is skipped, everything else retried in
// place with a backoff, so a message never kills the home nor disappears.
func (r *Reader) retry(ctx context.Context, m jetstream.Msg,
	channelID func(context.Context) (string, error)) error {
	wait := time.Second
	for {
		err := r.one(ctx, m, channelID)
		if err == nil {
			return nil
		}
		var refused *mirror.Refused
		if errors.As(err, &refused) {
			r.Log.Warn("bridge: refused, skipped", "home", r.Home,
				"subject", m.Subject(), "error", err)
			return r.save(ctx, mustSeq(m), mirror.Post{})
		}
		if ctx.Err() != nil {
			return nil
		}
		r.Log.Error("bridge: not mirrored, retrying", "home", r.Home,
			"subject", m.Subject(), "in", wait.String(), "error", err)
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

// save remembers a skipped message although the reader may be stopping.
func (r *Reader) save(ctx context.Context, seq uint64, p mirror.Post) error {
	sctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer cancel()
	return r.Store.Save(sctx, r.Home, seq, p)
}

func mustSeq(m jetstream.Msg) uint64 {
	md, err := m.Metadata()
	if err != nil {
		return 0
	}
	return md.Sequence.Stream
}

func (r *Reader) one(ctx context.Context, m jetstream.Msg,
	channelID func(context.Context) (string, error)) error {
	parts := strings.Split(m.Subject(), ".")
	// <prefix>.ch.<home>.<sender>; anything else under the wildcard is not this
	// home's post and must not stop the reader (a fifth token, a sub-subject).
	if len(parts) != 4 || parts[0] != r.Prefix || parts[1] != "ch" || parts[2] != r.Home {
		r.Log.Warn("bridge: unexpected subject skipped", "subject", m.Subject())
		return r.save(ctx, mustSeq(m), mirror.Post{})
	}
	seq := mustSeq(m)
	msg, err := message.Decode(m.Data(), r.PropsPrefix)
	if err != nil {
		// A message this bridge cannot read is not the bridge's to fix: log it
		// and advance past it, so one broken post does not stop the home.
		r.Log.Warn("bridge: unreadable message skipped", "subject", m.Subject(), "error", err)
		return r.save(ctx, seq, mirror.Post{})
	}
	return r.Mirror.Handle(ctx, channelID, parts[3], seq, msg)
}
