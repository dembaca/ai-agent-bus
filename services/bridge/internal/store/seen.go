package store

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// Seen is the bucket of the keys of people's events the bridge carried onto
// NATS (RT-6.42 step 1, `BUS_BRIDGE_SEEN`): `p.<post>` for a person's post it
// carried, `r.<post>` the kind it last carried per emoji and sender of that
// post's reactions. An event of the opposite kind replaces the kind, which is
// what clears a reaction's key: a 👍 taken back and given again counts again.
// The bucket keeps a key one hour (`max_age`) and drops the oldest when it is
// full (`discard: old`), so the keys never fill BUS_BRIDGE, whose `discard:
// new` would then refuse the first step's writes.
type Seen struct {
	kv jetstream.KeyValue
}

//go:embed seen_bucket.json
var seenJSON []byte

// SeenConfig is the configuration seen_bucket.json holds.
func SeenConfig() (jetstream.StreamConfig, error) {
	var cfg jetstream.StreamConfig
	if err := json.Unmarshal(seenJSON, &cfg); err != nil {
		return cfg, fmt.Errorf("store: seen_bucket.json: %w", err)
	}
	return cfg, nil
}

// CreateSeenBucket makes the bucket as the operator does; the bridge never
// calls it, the tests stand in for the operator with it.
func CreateSeenBucket(ctx context.Context, js jetstream.JetStream) error {
	cfg, err := SeenConfig()
	if err != nil {
		return err
	}
	if _, err := js.CreateStream(ctx, cfg); err != nil {
		return fmt.Errorf("store: creating the seen bucket: %w", err)
	}
	return nil
}

// OpenSeen binds to the operator's seen bucket. Besides direct get it refuses
// a bucket whose age limit is not between an hour and a day — shorter, a key
// is gone while it is still needed and an event goes out twice; none, the keys
// fill the bucket — and one with `discard: new`, which then refuses every write.
func OpenSeen(ctx context.Context, nc *nats.Conn, bucket string) (*Seen, error) {
	kv, err := openBucket(ctx, nc, bucket, "services/bridge/internal/store/seen_bucket.json",
		func(c jetstream.StreamConfig) error {
			if c.MaxAge < time.Hour || c.MaxAge > 24*time.Hour {
				return fmt.Errorf("max_age %s, not between an hour and a day", c.MaxAge)
			}
			if c.Discard != jetstream.DiscardOld {
				return errors.New("discard is not old")
			}
			return nil
		})
	if err != nil {
		return nil, err
	}
	return &Seen{kv: kv}, nil
}

// Post says whether the bridge carried this person's post.
func (s *Seen) Post(ctx context.Context, post string) (bool, error) {
	if !validKey(post) {
		return false, fmt.Errorf("store: post id %q", post)
	}
	_, err := s.kv.Get(ctx, "p."+post)
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return false, nil
	}
	return err == nil, err
}

// MarkPost remembers a carried post.
func (s *Seen) MarkPost(ctx context.Context, post string) error {
	if !validKey(post) {
		return fmt.Errorf("store: post id %q", post)
	}
	_, err := s.kv.Put(ctx, "p."+post, []byte("1"))
	return err
}

// Reactions is the kind ("reaction" or "unreaction") the bridge last carried
// per "<emoji> <sender>" of a post; empty when it carried none within the hour.
func (s *Seen) Reactions(ctx context.Context, post string) (map[string]string, error) {
	if !validKey(post) {
		return nil, fmt.Errorf("store: post id %q", post)
	}
	e, err := s.kv.Get(ctx, "r."+post)
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return map[string]string{}, nil
	}
	if err != nil {
		return nil, err
	}
	out := map[string]string{}
	if err := json.Unmarshal(e.Value(), &out); err != nil {
		return nil, fmt.Errorf("store: r.%s: %w", post, err)
	}
	return out, nil
}

// SetReactions writes a post's reaction kinds back.
func (s *Seen) SetReactions(ctx context.Context, post string, kinds map[string]string) error {
	if !validKey(post) {
		return fmt.Errorf("store: post id %q", post)
	}
	raw, err := json.Marshal(kinds)
	if err != nil {
		return err
	}
	_, err = s.kv.Put(ctx, "r."+post, raw)
	return err
}
