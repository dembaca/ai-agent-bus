package store

import (
	"context"
	_ "embed"
	"encoding/json"
	"fmt"

	"github.com/nats-io/nats.go/jetstream"
)

// bucketJSON is the bucket's stream configuration, the file the operator
// applies:
//
//	nats stream add --config services/bridge/internal/store/bucket.json
//
// It is not `nats kv add`, which turns direct get on: a direct get answers with
// a key's value as the payload, and a request's reply subject is not checked,
// so direct get would let this user's credential carry chosen bytes onto any
// subject (a forged post, a forged attestation). See Open.
//
//go:embed bucket.json
var bucketJSON []byte

// OperatorConfig is the configuration bucket.json holds.
func OperatorConfig() (jetstream.StreamConfig, error) {
	var cfg jetstream.StreamConfig
	if err := json.Unmarshal(bucketJSON, &cfg); err != nil {
		return cfg, fmt.Errorf("store: bucket.json: %w", err)
	}
	return cfg, nil
}

// CreateBucket makes the bucket as the operator does. The bridge never calls it
// (its NATS user has no stream-create right, and a stream's sources would let
// it read what it must not); the tests use it to stand in for the operator.
func CreateBucket(ctx context.Context, js jetstream.JetStream) error {
	cfg, err := OperatorConfig()
	if err != nil {
		return err
	}
	if _, err := js.CreateStream(ctx, cfg); err != nil {
		return fmt.Errorf("store: creating the bucket: %w", err)
	}
	return nil
}
