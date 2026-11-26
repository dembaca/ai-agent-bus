package store

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// Records is the bridge's bucket of whole-state records (issue #151 step 3):
// one key per store of the slash commands — `modes`, `mandates`, `debug` — each
// holding the JSON an earlier registrar kept in a file. The bridge saves the
// whole state after every change, so the last write wins and one bridge process
// owns the bucket.
//
// The keys are deleted, so the bucket must not deny deletes: a setting the
// operator takes back is a key that goes.
type Records struct {
	kv jetstream.KeyValue
}

//go:embed records_bucket.json
var recordsJSON []byte

// RecordsConfig is the configuration records_bucket.json holds.
func RecordsConfig() (jetstream.StreamConfig, error) {
	var cfg jetstream.StreamConfig
	if err := json.Unmarshal(recordsJSON, &cfg); err != nil {
		return cfg, fmt.Errorf("store: records_bucket.json: %w", err)
	}
	return cfg, nil
}

// CreateRecordsBucket makes the bucket as the operator does; the bridge never
// calls it (its NATS user creates no stream), the tests stand in for the
// operator with it.
func CreateRecordsBucket(ctx context.Context, js jetstream.JetStream) error {
	cfg, err := RecordsConfig()
	if err != nil {
		return err
	}
	if _, err := js.CreateStream(ctx, cfg); err != nil {
		return fmt.Errorf("store: creating the records bucket: %w", err)
	}
	return nil
}

// OpenRecords binds to the operator's records bucket, refusing direct get like
// Open and OpenSeen: a direct get answers with a key's value onto a reply
// subject the server checks against no publish permission.
func OpenRecords(ctx context.Context, nc *nats.Conn, bucket string) (*Records, error) {
	kv, err := openBucket(ctx, nc, bucket, "services/bridge/internal/store/records_bucket.json", nil)
	if err != nil {
		return nil, err
	}
	return &Records{kv: kv}, nil
}

// Load returns what name holds, or nil when it holds nothing.
func (r *Records) Load(name string) ([]byte, error) {
	e, err := r.kv.Get(context.Background(), name)
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return e.Value(), nil
}

// Save replaces what name holds. The stores marshal the whole state, so a
// later save carries every earlier change.
func (r *Records) Save(name string, data []byte) error {
	_, err := r.kv.Put(context.Background(), name, data)
	return err
}

// Delete removes name; one that is gone already is no error.
func (r *Records) Delete(name string) error {
	err := r.kv.Delete(context.Background(), name)
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return nil
	}
	return err
}
