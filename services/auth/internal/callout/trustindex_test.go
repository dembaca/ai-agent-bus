package callout

import (
	"context"
	"encoding/hex"
	"errors"
	"slices"
	"strings"
	"sync"
	"testing"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/directory"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
	"github.com/nats-io/nats.go/jetstream"
)

type fakeIndex struct {
	mu  sync.Mutex
	got []directory.AgentTrust
	err error
}

func (f *fakeIndex) Index(_ context.Context, r directory.AgentTrust) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.err != nil {
		return f.err
	}
	f.got = append(f.got, r)
	return nil
}

// The trust index an operator's channels come from (RT-6.43 step 3) holds
// what this issue checked itself: the owner's anchor, the trusted anchors as
// attested, and only the granted channels without the lobby. A wished
// channel the owner is no member of opens nothing, whatever the host trusts.
func TestAnIssueIndexesTheAttestedTrustAndTheGrantedChannels(t *testing.T) {
	l := newLab(t)
	f := &fakeAttester{}
	ix := &fakeIndex{}
	l.svc.SetAttester(f)
	l.svc.SetTrust(trustMap{"bob": "sub-bob"})
	l.svc.SetTrustIndex(ix)
	a, err := l.connect("alice", l.realm.Token("alice", nil),
		Hello{Home: "home", Guests: []string{"elsewhere"}, Lobby: true, Trusted: []string{"bob", "nobody"}}, nil)
	if err != nil {
		t.Fatal(err)
	}
	_, att := f.last(t)
	if len(ix.got) != 1 {
		t.Fatalf("%d records", len(ix.got))
	}
	r := ix.got[0]
	if r.Agent != a.name || r.Owner != "alice" || !slices.Equal(r.Trusted, []string{"sub-bob"}) ||
		!slices.Equal(r.Channels, []string{"home"}) || r.Exp != att.Exp {
		t.Errorf("record %+v, attestation %+v", r, att)
	}
}

// A guest channel the owner is a member of is granted and goes into the
// record beside the home.
func TestAGrantedGuestChannelIsIndexed(t *testing.T) {
	l := newLab(t)
	ix := &fakeIndex{}
	l.svc.SetAttester(&fakeAttester{})
	l.svc.SetTrustIndex(ix)
	l.chat.Channels["alice"] = append(slices.Clone(l.chat.Channels["alice"]), "elsewhere")
	if _, err := l.connect("alice", l.realm.Token("alice", nil),
		Hello{Home: "home", Guests: []string{"elsewhere"}, Lobby: true, Trusted: []string{"bob"}}, nil); err != nil {
		t.Fatal(err)
	}
	if len(ix.got) != 1 {
		t.Fatalf("%d records", len(ix.got))
	}
	got := slices.Sorted(slices.Values(ix.got[0].Channels))
	if !slices.Equal(got, []string{"elsewhere", "home"}) {
		t.Errorf("channels %v", got)
	}
}

// A trust record the operator's channels cannot be read from refuses the
// operator's credential: fail closed, never a partial list.
func TestACorruptTrustRecordRefusesTheOperator(t *testing.T) {
	c, _ := personLab(t)
	js, err := jetstream.New(c.nc)
	if err != nil {
		t.Fatal(err)
	}
	dir, err := directory.Open(ctx5(t), js, "", false)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := dir.Login(ctx5(t), "sub-ann", "ann.k", "ann.k"); err != nil {
		t.Fatal(err)
	}
	c.svc.SetPersons(c.svc.personHooks().verifier, directory.Persons{Store: dir})
	if _, err := c.operator(c.consoleToken("ann", nil), newHost()); err != nil {
		t.Fatalf("before the corrupt record: %v", err)
	}
	kv, err := js.KeyValue(ctx5(t), directory.DefaultBucket)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := kv.Put(ctx5(t), "agenttrust."+hex.EncodeToString([]byte("agent-bad")), []byte("{not json")); err != nil {
		t.Fatal(err)
	}
	seen := len(c.logs.String())
	if _, err := c.operator(c.consoleToken("ann", nil), newHost()); err == nil {
		t.Fatal("the operator connected")
	}
	if !strings.Contains(c.logs.String()[seen:], token.ErrDirectory.Error()) {
		t.Errorf("no %q in the log", token.ErrDirectory.Error())
	}
}

// A record that cannot be written refuses the login: the one before, with a
// trust the host may have dropped, would otherwise stay. Without an
// attestation nothing is indexed.
func TestAFailedTrustIndexRefusesTheLogin(t *testing.T) {
	l := newLab(t)
	ix := &fakeIndex{}
	l.svc.SetTrustIndex(ix)
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home", Trusted: []string{"bob"}}, nil); err != nil {
		t.Fatal(err)
	}
	if len(ix.got) != 0 {
		t.Errorf("indexed without an attestation: %+v", ix.got)
	}
	l.svc.SetAttester(&fakeAttester{})
	ix.err = errors.New("bucket gone")
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home", Trusted: []string{"bob"}}, nil); err == nil {
		t.Fatal("the login went through")
	}
}
