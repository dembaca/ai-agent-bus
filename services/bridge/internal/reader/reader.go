// Package reader reads a home's push consumer and hands its messages to the
// mirror. The consumer is not the bridge's: the operator creates it and the
// server delivers to `deliver.bridge.<home>`; the bridge only subscribes and
// acks. It holds no consumer-create and no message-next right, because a pull
// names its own reply subject — which the server checks against no publish
// permission — and a pulled message is stored again under its original subject:
// a replay of the whole home, a trusted human's "go" re-issued (RT-6.36 step 1).
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
	Deliver     string // deliver.bridge.<home>: the operator's push consumer's subject
	Log         *slog.Logger
	Mirror      *mirror.Mirror
	Store       mirror.Store
	Prefix      string        // the bus prefix, "bus" unless configured
	PropsPrefix string        // the homes' MM_PROPS_PREFIX, for the message decode
	Ready       chan struct{} // closed once the subscription is active (tests wait on it)
	// Stuck is told when this home has been retrying one message for more than
	// StuckAfter, and false when it went through: the probe fails meanwhile, the
	// reader keeps trying (a revoked token is fixed, not skipped).
	Stuck      func(bool)
	StuckAfter time.Duration
}

// Run consumes until ctx ends.
func (r *Reader) Run(ctx context.Context, nc *nats.Conn,
	channelID func(context.Context) (string, error)) error {
	ch := make(chan *nats.Msg, 256)
	// A core subscription never acks by itself: Ack is the bridge's call, after
	// the mirror is written.
	sub, err := nc.ChanSubscribe(r.Deliver, ch)
	if err != nil {
		return fmt.Errorf("reader: subscribe %s: %w", r.Deliver, err)
	}
	defer func() { _ = sub.Unsubscribe() }()
	// The subscription must be in place before the server delivers: a push
	// consumer counts a delivery with no listener as delivered.
	if err := nc.Flush(); err != nil {
		return fmt.Errorf("reader: subscribe %s: %w", r.Deliver, err)
	}
	if r.Ready != nil {
		close(r.Ready)
	}
	r.Log.Info("bridge: reading a home", "home", r.Home, "deliver", r.Deliver)
	for {
		select {
		case <-ctx.Done():
			return nil
		case m := <-ch:
			if err := r.retry(ctx, m, channelID); err != nil {
				return err
			}
			if ctx.Err() != nil {
				return nil
			}
			// Ack after the mirror is written: an unacked message is delivered
			// again, and the store's position then skips what is already done.
			if err := m.Ack(); err != nil {
				r.Log.Warn("bridge: ack refused", "home", r.Home, "error", err)
			}
		}
	}
}

// retry mirrors one message; a refusal is skipped, everything else retried in
// place with a backoff, so a message never kills the home nor disappears.
func (r *Reader) retry(ctx context.Context, m *nats.Msg,
	channelID func(context.Context) (string, error)) error {
	wait := time.Second
	since := time.Now()
	stuck := false
	defer func() {
		if stuck && r.Stuck != nil {
			r.Stuck(false)
		}
	}()
	for {
		err := r.one(ctx, m, channelID)
		if err == nil {
			return nil
		}
		var refused *mirror.Refused
		if errors.As(err, &refused) {
			r.Log.Warn("bridge: refused, skipped", "home", r.Home,
				"subject", m.Subject, "error", err)
			return r.save(ctx, mustSeq(m), mirror.Post{})
		}
		if ctx.Err() != nil {
			return nil
		}
		if !stuck && r.Stuck != nil {
			after := r.StuckAfter
			if after == 0 {
				after = time.Minute
			}
			if time.Since(since) > after {
				stuck = true
				r.Stuck(true)
			}
		}
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
	// <prefix>.ch.<home>.<sender>; anything else under the consumer's filter is
	// not this home's post and must not stop the reader (a sub-subject).
	if len(parts) != 4 || parts[0] != r.Prefix || parts[1] != "ch" || parts[2] != r.Home {
		r.Log.Warn("bridge: unexpected subject skipped", "subject", m.Subject)
		return r.save(ctx, mustSeq(m), mirror.Post{})
	}
	seq := mustSeq(m)
	// The consumer delivers again what was not acked (a restart, an ack that
	// timed out): the position says what is done, so nothing is mirrored twice.
	pos, ok, err := r.Store.Position(ctx, r.Home)
	if err != nil {
		return err
	}
	if ok && seq <= pos {
		return nil
	}
	msg, err := message.Decode(m.Data, r.PropsPrefix)
	if err != nil {
		// A message this bridge cannot read is not the bridge's to fix: log it
		// and move past it, so one broken post does not stop the home.
		r.Log.Warn("bridge: unreadable message skipped", "subject", m.Subject, "error", err)
		return r.save(ctx, seq, mirror.Post{})
	}
	return r.Mirror.Handle(ctx, channelID, parts[3], seq, msg)
}
