package callout

import (
	"context"
	"errors"
	"slices"
	"sync"
	"testing"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/directory"
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
