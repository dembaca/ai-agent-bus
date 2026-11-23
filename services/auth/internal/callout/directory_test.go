package callout

import (
	"testing"
	"time"

	"github.com/nats-io/nats.go/jetstream"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/directory"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

// RT-6.39: the channel directory (ADR-016) changes no agent's name and no presence
// token before the import: every identity's anchor is its name of today, so a
// login is byte for byte what it was without the directory. After the import
// an identity known before keeps it; only a new one gets its `sub`. And no
// agent reads or writes the bucket.
func TestTheDirectoryKeepsNamesAndPresenceTokens(t *testing.T) {
	l := newLab(t)
	nc, _ := l.admin()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	dir, err := directory.Open(ctx5(t), js, "")
	if err != nil {
		t.Fatal(err)
	}
	l.svc.SetAnchors(directory.Anchors{Store: dir})

	// presenceOK: the agent may publish its presence under `owner` (its
	// anchor), and not under `not`.
	presenceOK := func(a *agent, not string) {
		t.Helper()
		if err := a.nc.Publish(a.presence(), []byte("{}")); err != nil || a.denied(t) {
			t.Fatalf("%s: presence %s refused", a.owner, a.presence())
		}
		if not != "" {
			if err := a.nc.Publish("presence."+ownerToken(not)+"."+a.name, []byte("{}")); err != nil || !a.denied(t) {
				t.Fatalf("%s: presence under %s allowed", a.owner, not)
			}
		}
	}

	// Before the import: exactly as without the directory. The lab computes
	// the expected name from the owner's name, as main does.
	alice, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil)
	if err != nil {
		t.Fatalf("alice before the import: %v", err)
	}
	presenceOK(alice, "sub-alice")
	if id, _, ok, err := dir.Identity(ctx5(t), "sub-alice"); err != nil || !ok || id.Anchor != "alice" || id.Display != "alice" {
		t.Fatalf("alice's identity %+v %v %v", id, ok, err)
	}

	// The import has run: alice keeps her anchor, a new identity gets its sub.
	if err := dir.MarkImported(ctx5(t), time.Now()); err != nil {
		t.Fatal(err)
	}
	again, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil)
	if err != nil {
		t.Fatalf("alice after the import: %v", err)
	}
	presenceOK(again, "sub-alice")
	l.chat.Users["carol"] = token.User{Username: "carol", AuthKnown: true, AuthService: token.OpenIDService, AuthData: "sub-carol"}
	l.chat.Channels["carol"] = []string{"home"}
	carol, err := l.connect("sub-carol", l.realm.Token("carol", nil), Hello{Home: "home"}, nil)
	if err != nil {
		t.Fatalf("carol after the import: %v", err)
	}
	presenceOK(carol, "carol")

	// No agent reads or writes the bucket: no $KV, no $JS right (ADR-014 point 16).
	for _, subj := range []string{
		"$KV." + directory.DefaultBucket + ".ch.home",
		"$KV." + directory.DefaultBucket + ".anchor.alice",
		"$JS.API.DIRECT.GET.KV_" + directory.DefaultBucket + ".$KV." + directory.DefaultBucket + ".id.sub-alice",
		"$JS.API.STREAM.INFO.KV_" + directory.DefaultBucket,
	} {
		if err := carol.nc.Publish(subj, []byte(`{"name":"home","public":true}`)); err != nil || !carol.denied(t) {
			t.Fatalf("an agent may publish on %s", subj)
		}
	}
	if c, _, ok, err := dir.Channel(ctx5(t), "home"); err != nil || ok {
		t.Fatalf("the bucket was written by an agent: %+v %v", c, err)
	}
}
