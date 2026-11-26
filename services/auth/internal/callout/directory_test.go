package callout

import (
	"context"
	"errors"
	"testing"
	"time"

	gojwt "github.com/golang-jwt/jwt/v5"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/directory"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

// RT-6.39: the channel directory (ADR-016) changes no agent's name and no presence
// token before the import: every identity's anchor is its name of today, so a
// login is byte for byte what it was without the directory. After the import
// an identity known before keeps it; a new one takes its free username (#86),
// so the name the host computes from `preferred_username` (natsbus.agent_name)
// is the one issued, its owner token too; a held name gives the `sub`, and the
// host's own name is refused, never a wrong one. And no agent reads or writes
// the bucket.
func TestTheDirectoryKeepsNamesAndPresenceTokens(t *testing.T) {
	l := newLab(t)
	nc, _ := l.admin()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	dir, err := directory.Open(ctx5(t), js, "", false)
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

	// The import has run: alice keeps her anchor, a new identity takes its
	// free username.
	if err := dir.MarkImported(ctx5(t), time.Now()); err != nil {
		t.Fatal(err)
	}
	again, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil)
	if err != nil {
		t.Fatalf("alice after the import: %v", err)
	}
	presenceOK(again, "sub-alice")
	const carolSub = "0b6f3c1e-1111-4222-8333-444455556666" // Keycloak subs are UUIDs
	l.chat.Users["carol"] = token.User{Username: "carol", AuthKnown: true, AuthService: token.OpenIDService, AuthData: carolSub}
	l.chat.Channels["carol"] = []string{"home"}
	// The lab computes the name from "carol", as the host does (RT-6.39 step 2).
	carol, err := l.connect("carol", l.realm.Token("carol", func(c gojwt.MapClaims) { c["sub"] = carolSub }), Hello{Home: "home"}, nil)
	if err != nil {
		t.Fatalf("carol after the import: %v", err)
	}
	presenceOK(carol, carolSub)
	if _, err := carol.nc.SubscribeSync("deliver." + carol.name + ".>"); err != nil || carol.denied(t) {
		t.Fatalf("carol may not read her own deliver subjects as %s", carol.name)
	}
	if id, _, ok, err := dir.Identity(ctx5(t), carolSub); err != nil || !ok || id.Anchor != "carol" {
		t.Fatalf("carol's identity %+v %v %v", id, ok, err)
	}

	// A new identity whose username another identity holds (alice, renamed in
	// Keycloak, and a new alice): its anchor is its sub, so the name its host
	// computes from "alice" is refused (NameMismatch on the host), never a
	// credential under alice's name.
	const newAliceSub = "5c4b3a29-1111-4222-8333-444455556666"
	l.chat.Users["alice"] = token.User{Username: "alice", AuthKnown: true, AuthService: token.OpenIDService, AuthData: newAliceSub}
	other, err := l.connect("alice", l.realm.Token("alice", func(c gojwt.MapClaims) { c["sub"] = newAliceSub }), Hello{Home: "home"}, nil)
	if err != nil {
		t.Fatalf("the new alice after the import: %v", err)
	}
	if _, err := other.nc.SubscribeSync("deliver." + other.name + ".>"); err != nil || !other.denied(t) {
		t.Fatal("the new alice got alice's agent name")
	}
	if err := other.nc.Publish(other.presence(), []byte("{}")); err != nil || !other.denied(t) {
		t.Fatal("the new alice may publish presence under alice")
	}
	if id, _, ok, err := dir.Identity(ctx5(t), newAliceSub); err != nil || !ok || id.Anchor != newAliceSub {
		t.Fatalf("the new alice's identity %+v %v %v", id, ok, err)
	}

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
	// Nor subscribe to them, nor to everything.
	for _, subj := range []string{"$KV.>", "$KV." + directory.DefaultBucket + ".>", "$JS.API.>", ">"} {
		if _, err := carol.nc.SubscribeSync(subj); err != nil || !carol.denied(t) {
			t.Fatalf("an agent may subscribe to %s", subj)
		}
	}
	if c, _, ok, err := dir.Channel(ctx5(t), "home"); err != nil || ok {
		t.Fatalf("the bucket was written by an agent: %+v %v", c, err)
	}
}

// accountSpy is directory.Anchors whose account step can fail and is counted.
type accountSpy struct {
	directory.Anchors
	calls []string
	err   error
}

func (a *accountSpy) Account(ctx context.Context, sub, name string) error {
	a.calls = append(a.calls, sub+"/"+name)
	if a.err != nil {
		return a.err
	}
	return a.Anchors.Account(ctx, sub, name)
}

// RT-6.41 step 2, issue #59: after the import the account index follows a login whose chat
// account is proven (OpenID), never one that logs in with a password, and an
// index that cannot be written fails the login.
func TestTheAccountIndexFollowsOnlyAProvenLogin(t *testing.T) {
	l := newLab(t)
	nc, _ := l.admin()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	dir, err := directory.Open(ctx5(t), js, "", false)
	if err != nil {
		t.Fatal(err)
	}
	spy := &accountSpy{Anchors: directory.Anchors{Store: dir}}
	l.svc.SetAnchors(spy)
	const carolSub = "0b6f3c1e-1111-4222-8333-444455556666"
	const malSub = "9e8d7c6b-5a49-4382-9716-151413121110"
	l.chat.Users["carol"] = token.User{Username: "carol", AuthKnown: true, AuthService: token.OpenIDService, AuthData: carolSub}
	l.chat.Channels["carol"] = []string{"home"}
	login := func(sub string) error {
		_, err := l.connect(sub, l.realm.Token("carol", func(c gojwt.MapClaims) { c["sub"] = sub }), Hello{Home: "home"}, nil)
		return err
	}

	// Before the import nothing is written, though the login is proven.
	if err := login(carolSub); err != nil {
		t.Fatal(err)
	}
	if _, _, _, ok, _ := dir.Account(ctx5(t), "mattermost", "carol"); ok {
		t.Fatal("the index was written before the import")
	}
	if err := dir.MarkImported(ctx5(t), time.Now()); err != nil {
		t.Fatal(err)
	}

	// A proven login sets the entry.
	if err := login(carolSub); err != nil {
		t.Fatal(err)
	}
	if id, ok, err := dir.ByAccount(ctx5(t), "mattermost", "carol"); err != nil || !ok || id.Sub != carolSub {
		t.Fatalf("after an OpenID login: %+v %v %v", id, ok, err)
	}

	// The chat account of that name now logs in with a password; its human
	// has another sub. The name binding lets it pass, the index must not move.
	l.chat.Users["carol"] = token.User{Username: "carol", AuthKnown: true}
	calls := len(spy.calls)
	if err := login(malSub); err != nil {
		t.Fatal(err)
	}
	if len(spy.calls) != calls {
		t.Fatalf("a password account reached the account index: %v", spy.calls[calls:])
	}
	if id, ok, _ := dir.ByAccount(ctx5(t), "mattermost", "carol"); !ok || id.Sub != carolSub {
		t.Fatalf("a password account took the name over: %+v %v", id, ok)
	}

	// An index that cannot be written fails the login.
	l.chat.Users["carol"] = token.User{Username: "carol", AuthKnown: true, AuthService: token.OpenIDService, AuthData: malSub}
	spy.err = errors.New("bucket unavailable")
	if err := login(malSub); err == nil {
		t.Fatal("the login passed although the index could not be written")
	}
	spy.err = nil
	if id, ok, _ := dir.ByAccount(ctx5(t), "mattermost", "carol"); !ok || id.Sub != carolSub {
		t.Fatalf("a failed login changed the index: %+v %v", id, ok)
	}
	// With a working index the proven new holder takes the name.
	if err := login(malSub); err != nil {
		t.Fatal(err)
	}
	if id, ok, _ := dir.ByAccount(ctx5(t), "mattermost", "carol"); !ok || id.Sub != malSub || id.MovedFrom["mattermost"] != carolSub {
		t.Fatalf("the proven new holder: %+v %v", id, ok)
	}
}
