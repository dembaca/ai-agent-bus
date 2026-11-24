// Package store is the bridge's id map and its position, in one JetStream KV
// bucket of the bus account: `pos` holds the last stream sequence, and
// `p.<home>.<seq>` the Mattermost post a mirrored message became.
package store

import (
	"context"
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

// Open binds to the bucket, creating it when it does not exist.
func Open(ctx context.Context, nc *nats.Conn, bucket string) (*KV, error) {
	js, err := jetstream.New(nc)
	if err != nil {
		return nil, fmt.Errorf("store: %w", err)
	}
	kv, err := js.KeyValue(ctx, bucket)
	if errors.Is(err, jetstream.ErrBucketNotFound) {
		kv, err = js.CreateKeyValue(ctx, jetstream.KeyValueConfig{
			Bucket: bucket, History: 1, Storage: jetstream.FileStorage,
		})
	}
	if err != nil {
		return nil, fmt.Errorf("store: %w", err)
	}
	return &KV{kv: kv}, nil
}

// Position is the last stream sequence this bridge has handled.
func (s *KV) Position(ctx context.Context) (uint64, bool, error) {
	e, err := s.kv.Get(ctx, "pos")
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

// Save remembers a mirror and moves the position.
func (s *KV) Save(ctx context.Context, home string, seq uint64, p mirror.Post) error {
	raw, err := json.Marshal(p)
	if err != nil {
		return err
	}
	if _, err := s.kv.Put(ctx, postKey(home, seq), raw); err != nil {
		return err
	}
	_, err = s.kv.Put(ctx, "pos", []byte(strconv.FormatUint(seq, 10)))
	return err
}

func postKey(home string, seq uint64) string {
	return "p." + home + "." + strconv.FormatUint(seq, 10)
}
