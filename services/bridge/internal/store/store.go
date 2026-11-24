// Package store is the bridge's id map, its position per home and its reaction
// counts, in one JetStream KV bucket of the bus account: `pos.<home>` holds the
// last stream sequence that home's reader has handled, `p.<home>.<seq>` the
// Mattermost post a mirrored message became, and `r.<post>.<emoji>` how many
// agents reacted with that emoji (the bridge's bot holds one reaction per post
// and emoji).
package store

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mirror"
)

// KV is a Store over JetStream KV.
type KV struct {
	kv jetstream.KeyValue
}

// Open binds to the bucket. It never creates it, and the bridge's NATS user has
// no right to create a stream: a stream's sources would let it read what it
// must not (a consumer or a source with a DM filter into its own bucket). The
// operator creates the bucket before the first start:
//
//	nats kv add BUS_BRIDGE --history 1
func Open(ctx context.Context, nc *nats.Conn, bucket string) (*KV, error) {
	js, err := jetstream.New(nc)
	if err != nil {
		return nil, fmt.Errorf("store: %w", err)
	}
	kv, err := js.KeyValue(ctx, bucket)
	if errors.Is(err, jetstream.ErrBucketNotFound) {
		return nil, fmt.Errorf("store: the bucket %q does not exist; the operator creates it "+
			"(nats kv add %s --history 1): %w", bucket, bucket, err)
	}
	if err != nil {
		return nil, fmt.Errorf("store: %w", err)
	}
	return &KV{kv: kv}, nil
}

// Position is the last stream sequence this home's reader has handled.
func (s *KV) Position(ctx context.Context, home string) (uint64, bool, error) {
	e, err := s.kv.Get(ctx, "pos."+home)
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return 0, false, nil
	}
	if err != nil {
		return 0, false, err
	}
	n, err := strconv.ParseUint(string(e.Value()), 10, 64)
	if err != nil {
		return 0, false, fmt.Errorf("store: position %q: %w", e.Value(), err)
	}
	return n, true, nil
}

// Post finds the mirror of a message.
func (s *KV) Post(ctx context.Context, home string, seq uint64) (mirror.Post, bool, error) {
	e, err := s.kv.Get(ctx, postKey(home, seq))
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return mirror.Post{}, false, nil
	}
	if err != nil {
		return mirror.Post{}, false, err
	}
	var p mirror.Post
	if err := json.Unmarshal(e.Value(), &p); err != nil {
		return mirror.Post{}, false, fmt.Errorf("store: %s: %w", e.Key(), err)
	}
	return p, true, nil
}

// Save remembers a mirror and moves the home's position.
func (s *KV) Save(ctx context.Context, home string, seq uint64, p mirror.Post) error {
	raw, err := json.Marshal(p)
	if err != nil {
		return err
	}
	if _, err := s.kv.Put(ctx, postKey(home, seq), raw); err != nil {
		return err
	}
	_, err = s.kv.Put(ctx, "pos."+home, []byte(strconv.FormatUint(seq, 10)))
	return err
}

// Reaction adds or removes one sender's reaction of a home's message (its
// stream sequence) and emoji, and returns how many senders reacted: the
// bridge's bot reacts on 1 and unreacts on 0. A sender reacting twice is one
// reaction, as on the bus, so a replay counts nothing.
func (s *KV) Reaction(ctx context.Context, home string, seq uint64, emoji, sender string, add bool) (int, error) {
	key := "r." + home + "." + strconv.FormatUint(seq, 10) + "." + hex.EncodeToString([]byte(emoji))
	for range 5 {
		e, err := s.kv.Get(ctx, key)
		senders := map[string]bool{}
		var rev uint64
		switch {
		case errors.Is(err, jetstream.ErrKeyNotFound):
		case err != nil:
			return 0, err
		default:
			if err := json.Unmarshal(e.Value(), &senders); err != nil {
				return 0, fmt.Errorf("store: %s: %w", key, err)
			}
			rev = e.Revision()
		}
		if add {
			senders[sender] = true
		} else {
			delete(senders, sender)
		}
		n := len(senders)
		if n == 0 {
			if rev != 0 {
				if err := s.kv.Delete(ctx, key, jetstream.LastRevision(rev)); err != nil &&
					!errors.Is(err, jetstream.ErrKeyNotFound) && !errors.Is(err, jetstream.ErrKeyDeleted) {
					if isWrongSequence(err) {
						continue
					}
					return 0, err
				}
			}
			return 0, nil
		}
		value, err := json.Marshal(senders)
		if err != nil {
			return 0, err
		}
		if rev == 0 {
			_, err = s.kv.Create(ctx, key, value)
		} else {
			_, err = s.kv.Update(ctx, key, value, rev)
		}
		if err == nil {
			return n, nil
		}
		if isWrongSequence(err) || errors.Is(err, jetstream.ErrKeyExists) {
			continue
		}
		return 0, err
	}
	return 0, jetstream.ErrKeyExists
}

func isWrongSequence(err error) bool {
	var jsErr *jetstream.APIError
	if errors.As(err, &jsErr) {
		return jsErr.ErrorCode == jetstream.JSErrCodeStreamWrongLastSequence
	}
	return false
}

func postKey(home string, seq uint64) string {
	return "p." + home + "." + strconv.FormatUint(seq, 10)
}
