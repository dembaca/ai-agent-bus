package directory

// RT-6.42 step 3: the guard of the account record keeps the newest token's
// issue time per identity, with a tie-break that fails closed.

import (
	"context"
	"errors"
	"testing"

	"github.com/nats-io/nats.go/jetstream"
)

// racyKV is a bucket where every Create finds the key taken (someone was
// faster) for the first `lose` calls; Get answers "not found".
type racyKV struct {
	jetstream.KeyValue
	lose, creates int
}

func (k *racyKV) Get(context.Context, string) (jetstream.KeyValueEntry, error) {
	return nil, jetstream.ErrKeyNotFound
}

func (k *racyKV) Create(_ context.Context, _ string, _ []byte, _ ...jetstream.KVCreateOpt) (uint64, error) {
	k.creates++
	if k.creates <= k.lose {
		return 0, jetstream.ErrKeyExists
	}
	return 1, nil
}

func TestNewestTokenTriesAgainAfterALostRace(t *testing.T) {
	kv := &racyKV{lose: 3}
	ok, err := New(kv, false).NewestToken(context.Background(), "sub-a", 100, true)
	if err != nil || !ok || kv.creates != 4 {
		t.Errorf("ok %v, err %v, %d creates", ok, err, kv.creates)
	}
}

func TestNewestTokenGivesUpAfterEightLostRaces(t *testing.T) {
	kv := &racyKV{lose: 1000}
	ok, err := New(kv, false).NewestToken(context.Background(), "sub-a", 100, true)
	if ok || !errors.Is(err, ErrConflict) || kv.creates != maxTokenIatTries {
		t.Errorf("ok %v, err %v, %d creates", ok, err, kv.creates)
	}
}

func TestNewestTokenRefusesABadKey(t *testing.T) {
	if _, err := New(&racyKV{}, false).NewestToken(context.Background(), "a.b", 1, false); !errors.Is(err, ErrBadKey) {
		t.Errorf("err %v", err)
	}
}

func TestNewestTokenKeepsTheNewestAndBreaksTiesTowardNoApprover(t *testing.T) {
	st, _ := open(t)
	ctx := context.Background()
	for i, step := range []struct {
		iat      int64
		approver bool
		want     bool
	}{
		{100, true, true},   // the first is kept
		{90, true, false},   // older: refused
		{100, true, true},   // the same token again renews
		{100, false, true},  // same second, without the group: wins
		{100, true, false},  // same second, with the group: never raises the flag again
		{100, false, true},  // renews
		{101, true, true},   // a newer token decides, group or not
		{100, false, false}, // and the older one is refused whatever it says
	} {
		got, err := st.NewestToken(ctx, "sub-a", step.iat, step.approver)
		if err != nil || got != step.want {
			t.Errorf("step %d (%+v): %v, %v", i, step, got, err)
		}
	}
	// Per identity.
	if ok, err := st.NewestToken(ctx, "sub-b", 1, true); err != nil || !ok {
		t.Errorf("another identity: %v %v", ok, err)
	}
}
