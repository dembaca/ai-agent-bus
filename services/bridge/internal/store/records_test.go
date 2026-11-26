package store

import (
	"context"
	"strings"
	"testing"

	"github.com/nats-io/nats.go/jetstream"
)

// Issue #151 step 3: the operator's records bucket binds, holds one whole-state
// value per key, and a key can go again (no deny_delete); direct get stays off.
func TestTheRecordsBucket(t *testing.T) {
	_, nc := open(t)
	ctx := context.Background()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := OpenRecords(ctx, nc, "BUS_BRIDGE_RECORDS"); err == nil {
		t.Fatal("bound to a bucket nobody made")
	}
	if err := CreateRecordsBucket(ctx, js); err != nil {
		t.Fatal(err)
	}
	r, err := OpenRecords(ctx, nc, "BUS_BRIDGE_RECORDS")
	if err != nil {
		t.Fatal(err)
	}
	if b, err := r.Load("modes"); b != nil || err != nil {
		t.Fatalf("a key before any save: %q %v", b, err)
	}
	if err := r.Save("modes", []byte(`{"first":1}`)); err != nil {
		t.Fatal(err)
	}
	if err := r.Save("modes", []byte(`{"second":2}`)); err != nil { // last write wins
		t.Fatal(err)
	}
	if err := r.Save("mandates", []byte(`{}`)); err != nil {
		t.Fatal(err)
	}
	if b, err := r.Load("modes"); err != nil || string(b) != `{"second":2}` {
		t.Fatalf("modes: %q %v", b, err)
	}
	if b, err := r.Load("mandates"); err != nil || string(b) != `{}` {
		t.Fatalf("mandates: %q %v", b, err)
	}
	if err := r.Delete("modes"); err != nil {
		t.Fatalf("a key could not be deleted: %v (deny_delete?)", err)
	}
	if b, err := r.Load("modes"); b != nil || err != nil {
		t.Fatalf("after delete: %q %v", b, err)
	}
	if err := r.Delete("modes"); err != nil {
		t.Fatalf("deleting a gone key: %v", err)
	}
	if b, err := r.Load("mandates"); err != nil || string(b) != `{}` {
		t.Fatalf("the other key went too: %q %v", b, err)
	}
}

// A records bucket with direct get on is refused like the others: its answer
// carries a key's value onto a reply subject the server does not check.
func TestARecordsBucketWithDirectGetIsRefused(t *testing.T) {
	_, nc := open(t)
	ctx := context.Background()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := js.CreateKeyValue(ctx, jetstream.KeyValueConfig{Bucket: "BUS_BRIDGE_RECORDS", History: 1}); err != nil {
		t.Fatal(err)
	}
	if _, err := OpenRecords(ctx, nc, "BUS_BRIDGE_RECORDS"); err == nil {
		t.Fatal("the bridge accepted a records bucket with direct get")
	}
}

// The operator's file and the store agree: a KV bucket, direct get off, one
// value per key, deletes allowed (a setting taken back is a key that goes).
func TestTheRecordsOperatorConfig(t *testing.T) {
	_, nc := open(t)
	cfg, err := RecordsConfig()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Name != "KV_BUS_BRIDGE_RECORDS" || cfg.AllowDirect || cfg.DenyDelete ||
		cfg.MaxMsgsPerSubject != 1 || cfg.Storage != jetstream.FileStorage || cfg.Discard != jetstream.DiscardOld ||
		len(cfg.Subjects) != 1 || cfg.Subjects[0] != "$KV.BUS_BRIDGE_RECORDS.>" || cfg.MaxAge != 0 {
		t.Fatalf("records_bucket.json: %+v", cfg)
	}
	if cfg.MaxBytes <= 0 {
		t.Fatalf("the bucket has no size limit: %+v", cfg)
	}
	// The error names the file the operator applies, as Open and OpenSeen do.
	if _, err := OpenRecords(context.Background(), nc, "NO_SUCH_BUCKET"); err == nil ||
		!strings.Contains(err.Error(), "records_bucket.json") {
		t.Fatalf("the missing-bucket error: %v", err)
	}
}
