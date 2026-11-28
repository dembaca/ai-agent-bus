// Package store is the bridge's id map, its position per home and its reaction
// counts, in one JetStream KV bucket of the bus account: `pos.<home>` holds the
// last stream sequence that home's reader has handled, `p.<home>.<seq>` the
// Mattermost post a mirrored message became, `m.<home>.<post>` the way back
// from a Mattermost post to its stream sequence (the `root` and `ref` of a
// person's post or reaction, RT-6.42 step 5), `wm.<home>` the newest
// `create_at` of a person's post or reaction the bridge carried onto NATS (the
// catch-up's watermark, RT-6.42 step 1), and `r.<post>.<emoji>` how many
// agents reacted with that emoji (the bridge's bot holds one reaction per post
// and emoji). The keys of people's events it carried live in a bucket of their
// own with an age limit (Seen).
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

// Open binds to the bucket the operator created (OperatorConfig), and refuses a
// bucket it cannot hold safe. The bridge's NATS user creates no stream: a
// stream's sources would let it read what it must not (a DM filter into its own
// bucket), so the bucket is not the bridge's to make.
//
// Nor may the bucket answer direct gets: a direct get returns a key's value as
// the request's reply payload, and the server checks a reply subject against no
// publish permission, so with direct get the credential could carry chosen
// bytes onto any subject — a forged post in another home, a forged attestation.
// Through the message-get API the same trick delivers wrapped JSON instead.
func Open(ctx context.Context, nc *nats.Conn, bucket string) (*KV, error) {
	kv, err := openBucket(ctx, nc, bucket, "services/bridge/internal/store/bucket.json", nil)
	if err != nil {
		return nil, err
	}
	return &KV{kv: kv}, nil
}

// openBucket binds to an operator's bucket, refusing direct get and whatever else
// check says is unsafe.
func openBucket(ctx context.Context, nc *nats.Conn, bucket, file string,
	check func(jetstream.StreamConfig) error) (jetstream.KeyValue, error) {
	js, err := jetstream.New(nc)
	if err != nil {
		return nil, fmt.Errorf("store: %w", err)
	}
	stream, err := js.Stream(ctx, "KV_"+bucket)
	if errors.Is(err, jetstream.ErrStreamNotFound) {
		return nil, fmt.Errorf("store: the bucket %q does not exist; the operator creates it "+
			"from %s (nats stream add --config %s): %w", bucket, file, file, err)
	}
	if err != nil {
		return nil, fmt.Errorf("store: %w", err)
	}
	info, err := stream.Info(ctx)
	if err != nil {
		return nil, fmt.Errorf("store: %w", err)
	}
	if info.Config.AllowDirect {
		return nil, fmt.Errorf("store: the bucket %q has direct get on; the operator creates it "+
			"from %s (allow_direct false), "+
			"since a direct get answers with a key's value onto a reply subject the server does not check",
			bucket, file)
	}
	if check != nil {
		if err := check(info.Config); err != nil {
			return nil, fmt.Errorf("store: the bucket %q: %w; the operator creates it from %s", bucket, err, file)
		}
	}
	kv, err := js.KeyValue(ctx, bucket)
	if err != nil {
		return nil, fmt.Errorf("store: %w", err)
	}
	return kv, nil
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

// Save remembers a mirror and moves the home's position. A message that
// became a Mattermost post also gets the way back (Seq), written first, so a
// position never runs ahead of it. A reaction's entry (Reaction) holds the
// reacted post for the sequence but writes no way back: the post's own stays,
// so a person's reply or reaction on the copy names the post's sequence (#297).
func (s *KV) Save(ctx context.Context, home string, seq uint64, p mirror.Post) error {
	raw, err := json.Marshal(p)
	if err != nil {
		return err
	}
	if validKey(p.ID) && !p.Reaction {
		if _, err := s.kv.Put(ctx, "m."+home+"."+p.ID, []byte(strconv.FormatUint(seq, 10))); err != nil {
			return err
		}
	}
	if _, err := s.kv.Put(ctx, postKey(home, seq), raw); err != nil {
		return err
	}
	_, err = s.kv.Put(ctx, "pos."+home, []byte(strconv.FormatUint(seq, 10)))
	return err
}

// Seq is the stream sequence of the message a Mattermost post of this home
// mirrors or carries (RT-6.42 step 5: `root` and `ref`); false when the bridge
// does not know one.
func (s *KV) Seq(ctx context.Context, home, post string) (uint64, bool, error) {
	if !validKey(post) {
		return 0, false, nil
	}
	return s.uint(ctx, "m."+home+"."+post)
}

// Watermark is the newest `create_at` (ms) of a person's post or reaction this
// home carried onto NATS (RT-6.42 step 1); false before the first.
func (s *KV) Watermark(ctx context.Context, home string) (int64, bool, error) {
	n, ok, err := s.uint(ctx, "wm."+home)
	return int64(n), ok, err
}

// SetWatermark moves a home's watermark.
func (s *KV) SetWatermark(ctx context.Context, home string, ms int64) error {
	if ms < 0 {
		ms = 0
	}
	_, err := s.kv.Put(ctx, "wm."+home, []byte(strconv.FormatInt(ms, 10)))
	return err
}

func (s *KV) uint(ctx context.Context, key string) (uint64, bool, error) {
	e, err := s.kv.Get(ctx, key)
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return 0, false, nil
	}
	if err != nil {
		return 0, false, err
	}
	n, err := strconv.ParseUint(string(e.Value()), 10, 64)
	if err != nil {
		return 0, false, fmt.Errorf("store: %s %q: %w", key, e.Value(), err)
	}
	return n, true, nil
}

// validKey is a Mattermost id or emoji name that is one KV key token.
func validKey(s string) bool {
	if s == "" || len(s) > 64 {
		return false
	}
	for _, r := range s {
		if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '_' || r == '-') {
			return false
		}
	}
	return true
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

// OwnReaction says whether sender's own bot holds its reaction of a home's
// message and emoji (RT-6.44): the removal goes to that bot, whatever state
// the bot is in by then.
func (s *KV) OwnReaction(ctx context.Context, home string, seq uint64, emoji, sender string) (bool, error) {
	_, err := s.kv.Get(ctx, ownKey(home, seq, emoji, sender))
	switch {
	case errors.Is(err, jetstream.ErrKeyNotFound):
		return false, nil
	case err != nil:
		return false, err
	}
	return true, nil
}

// SetOwnReaction records or clears sender's own bot's reaction.
func (s *KV) SetOwnReaction(ctx context.Context, home string, seq uint64, emoji, sender string, on bool) error {
	key := ownKey(home, seq, emoji, sender)
	if on {
		_, err := s.kv.Put(ctx, key, []byte("1"))
		return err
	}
	if err := s.kv.Delete(ctx, key); err != nil && !errors.Is(err, jetstream.ErrKeyNotFound) {
		return err
	}
	return nil
}

func ownKey(home string, seq uint64, emoji, sender string) string {
	return "o." + home + "." + strconv.FormatUint(seq, 10) + "." + hex.EncodeToString([]byte(emoji)) +
		"." + hex.EncodeToString([]byte(sender))
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
