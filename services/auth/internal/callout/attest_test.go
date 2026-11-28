package callout

// The attestation: what the service vouches for at every issue, the subject
// only it may publish on, and that no agent can write it.

import (
	"context"
	"encoding/json"
	"errors"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/consumers"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/directory"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

type published struct {
	subject string
	body    []byte
}

type fakeAttester struct {
	mu  sync.Mutex
	got []published
	err error
	// failOn: a publish whose subject contains it fails
	failOn string
}

func (f *fakeAttester) Publish(_ context.Context, subject string, body []byte) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.err != nil {
		return f.err
	}
	if f.failOn != "" && strings.Contains(subject, f.failOn) {
		return errors.New("publish refused")
	}
	f.got = append(f.got, published{subject, body})
	return nil
}

func (f *fakeAttester) last(t *testing.T) (published, Attestation) {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.got) == 0 {
		t.Fatal("nothing was attested")
	}
	p := f.got[len(f.got)-1]
	var a Attestation
	if err := json.Unmarshal(p.body, &a); err != nil {
		t.Fatal(err)
	}
	return p, a
}

// trustMap resolves names as after the import: by a table, the rest cut.
type trustMap map[string]string

func (m trustMap) Resolve(_ context.Context, names []string) ([]directory.Trusted, bool, error) {
	out, cut := []directory.Trusted{}, false
	for _, n := range names {
		if a, ok := m[n]; ok {
			out = append(out, directory.Trusted{Anchor: a, Name: n})
		} else {
			cut = true
		}
	}
	return out, cut, nil
}

type trustErr struct{}

func (trustErr) Resolve(context.Context, []string) ([]directory.Trusted, bool, error) {
	return nil, false, errors.New("directory down")
}

type fixedAnchor string

func (f fixedAnchor) Anchor(context.Context, string, string) (string, error) { return string(f), nil }

func TestAnAttestationNamesOwnerTrustedAndExpiry(t *testing.T) {
	l := newLab(t)
	f := &fakeAttester{}
	l.svc.SetAttester(f)
	before := time.Now().Unix()
	a, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home", Trusted: []string{"bob", "carol"}}, nil)
	if err != nil {
		t.Fatal(err)
	}
	p, got := f.last(t)
	if p.subject != "presence.attest."+a.name+".v1" || len(strings.Split(p.subject, ".")) != 4 {
		t.Errorf("subject %s", p.subject)
	}
	// Before the import the names are the anchors; the body is compact.
	want := `"v":1,"agent":"` + a.name + `","home":"home","owner":{"anchor":"alice","name":"alice"},` +
		`"trusted":[{"anchor":"bob","name":"bob"},{"anchor":"carol","name":"carol"}],"cut":false,"iat":`
	if !strings.Contains(string(p.body), want) || strings.Contains(string(p.body), " ") {
		t.Errorf("body %s", p.body)
	}
	if got.Iat < before || got.Exp-got.Iat != int64(Lifetime/time.Second) {
		t.Errorf("iat %d exp %d", got.Iat, got.Exp)
	}
}

func TestWithoutATrustListTheAttestationIsEmptyAndWhole(t *testing.T) {
	l := newLab(t)
	f := &fakeAttester{}
	l.svc.SetAttester(f)
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil); err != nil {
		t.Fatal(err)
	}
	p, got := f.last(t)
	if !strings.Contains(string(p.body), `"trusted":[],"cut":false`) || got.Cut || len(got.Trusted) != 0 {
		t.Errorf("body %s", p.body)
	}
}

func TestTheAttestationOwnerIsTheAnchor(t *testing.T) {
	l := newLab(t)
	f := &fakeAttester{}
	l.svc.SetAttester(f)
	l.svc.SetAnchors(fixedAnchor("sub-alice"))
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil); err != nil {
		t.Fatal(err)
	}
	if _, got := f.last(t); got.Owner != (directory.Trusted{Anchor: "sub-alice", Name: "alice"}) {
		t.Errorf("owner %+v", got.Owner)
	}
}

func TestAfterTheImportTheTrustedAreTheirAnchorsAndTheUnresolvedAreCut(t *testing.T) {
	l := newLab(t)
	f := &fakeAttester{}
	l.svc.SetAttester(f)
	l.svc.SetTrust(trustMap{"bob": "sub-bob"})
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home", Trusted: []string{"bob", "nobody"}}, nil); err != nil {
		t.Fatal(err)
	}
	_, got := f.last(t)
	if len(got.Trusted) != 1 || got.Trusted[0] != (directory.Trusted{Anchor: "sub-bob", Name: "bob"}) || !got.Cut {
		t.Errorf("attestation %+v", got)
	}
}

func TestANameThatIsNoUsernameIsCutBeforeTheImport(t *testing.T) {
	l := newLab(t)
	f := &fakeAttester{}
	l.svc.SetAttester(f)
	names := []string{"bob", "Not A Name", "carol"}
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home", Trusted: names}, nil); err != nil {
		t.Fatal(err)
	}
	if _, got := f.last(t); len(got.Trusted) != 2 || !got.Cut {
		t.Errorf("attestation %+v", got)
	}
}

func TestMoreThanMaxTrustedNamesAreCut(t *testing.T) {
	l := newLab(t)
	f := &fakeAttester{}
	l.svc.SetAttester(f)
	var names []string
	for i := range directory.MaxTrusted + 5 {
		names = append(names, "u"+strconv.Itoa(i))
	}
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home", Trusted: names}, nil); err != nil {
		t.Fatal(err)
	}
	if _, got := f.last(t); len(got.Trusted) != directory.MaxTrusted || !got.Cut {
		t.Errorf("%d trusted, cut %v", len(got.Trusted), got.Cut)
	}
}

// The encoded body never passes MaxAttestation, which is no more than the
// PRESENCE stream keeps; entries go from the end and cut is set.
func TestAnOversizedAttestationDropsEntriesFromTheEnd(t *testing.T) {
	long := strings.Repeat("x", 64)
	var list []directory.Trusted
	for i := range directory.MaxTrusted {
		list = append(list, directory.Trusted{Anchor: long, Name: "n" + strconv.Itoa(i) + long[:60]})
	}
	a := Attestation{V: 1, Agent: "agent-x", Owner: directory.Trusted{Anchor: long, Name: long}, Trusted: list, Iat: 1, Exp: 2}
	full, _ := json.Marshal(a)
	if len(full) <= MaxAttestation {
		t.Fatalf("the test needs a body over the cap, got %d", len(full))
	}
	b, err := a.encode()
	if err != nil || len(b) > MaxAttestation {
		t.Fatalf("%d bytes, %v", len(b), err)
	}
	var got Attestation
	if err := json.Unmarshal(b, &got); err != nil {
		t.Fatal(err)
	}
	if !got.Cut || len(got.Trusted) == 0 || len(got.Trusted) >= len(list) || got.Trusted[0] != list[0] ||
		got.Trusted[len(got.Trusted)-1] != list[len(got.Trusted)-1] {
		t.Errorf("%d kept, cut %v", len(got.Trusted), got.Cut)
	}
	if !strings.Contains(consumers.PresenceStreamJSON("presence", 0), `"max_msg_size":`+strconv.Itoa(MaxAttestation)) {
		t.Errorf("MaxAttestation is not the PRESENCE stream's max_msg_size")
	}
}

// A credential without its attestation would look whole: a failed publish or
// resolution refuses the login.
func TestAFailedAttestationRefusesTheLogin(t *testing.T) {
	l := newLab(t)
	f := &fakeAttester{err: errors.New("no route")}
	l.svc.SetAttester(f)
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil); err == nil {
		t.Fatal("the login went through")
	}
	if !strings.Contains(l.logs.String(), "attestation: no route") {
		t.Errorf("log:\n%s", l.logs.String())
	}
	f.err = nil
	l.svc.SetTrust(trustErr{})
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home", Trusted: []string{"bob"}}, nil); err == nil {
		t.Fatal("the login went through with an unresolved trust list")
	}
	if len(f.got) != 0 {
		t.Errorf("a partial attestation was published: %v", f.got)
	}
}

// With the real stream and the minimal JetStream user: the attestation is
// stored on its subject by the service, no agent can write it (not even one
// whose owner is called "attest"), and every agent's presence consumer
// delivers it.
func TestOnlyTheServiceWritesTheAttestation(t *testing.T) {
	l := newLabCfg(t, nil, 2800*time.Millisecond, nil)
	adm, js := l.admin()
	for name, cfg := range map[string]string{"BUS": busStreamCfg, "PRESENCE": presenceStreamCfg} {
		if r, err := adm.Request("$JS.API.STREAM.CREATE."+name, []byte(cfg), 3*time.Second); err != nil || strings.Contains(string(r.Data), `"error"`) {
			t.Fatalf("stream %s: %v", name, err)
		}
	}
	denied := make(chan error, 4)
	svcNC, err := nats.Connect(l.url, nats.UserInfo("jsmin", "pwd"),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, err error) { denied <- err }))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(svcNC.Close)
	mgr, err := consumers.New(svcNC, consumers.Config{Bus: "bus", Presence: "presence", Log: slogTo(l.logs)})
	if err != nil {
		t.Fatal(err)
	}
	l.svc.SetConsumers(mgr)
	l.svc.SetAttester(NATSAttester{NC: svcNC, Stream: consumers.PresenceStream})
	l.chat.Users["attest"] = token.User{Username: "attest", AuthKnown: true, AuthService: token.OpenIDService, AuthData: "sub-attest"}
	l.chat.Channels["attest"] = []string{"home"}
	alice, bob := l.twoAgents()
	atk, err := l.connect("attest", l.realm.Token("attest", nil), Hello{Home: "home"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	stored := func(agent string) Attestation {
		t.Helper()
		m, err := js.GetLastMsg("PRESENCE", "presence.attest."+agent+".v1")
		if err != nil {
			t.Fatalf("no attestation of %s: %v", agent, err)
		}
		var a Attestation
		if err := json.Unmarshal(m.Data, &a); err != nil {
			t.Fatal(err)
		}
		return a
	}
	if a := stored(alice.name); a.Agent != alice.name || a.Owner.Name != "alice" {
		t.Errorf("alice's attestation: %+v", a)
	}
	// An owner called "attest" gets its three-token presence, nothing with .v1.
	for subj, want := range map[string]bool{
		"presence.attest." + atk.name:           false,
		"presence.attest." + atk.name + ".v1":   true,
		"presence.attest." + alice.name + ".v1": true,
		"presence.attest." + bob.name + ".v1":   true,
		"presence.attest.agent-other.v1":        true,
	} {
		if err := atk.nc.Publish(subj, []byte(`{"v":1,"forged":true}`)); err != nil {
			t.Fatal(err)
		}
		if got := atk.denied(t); got != want {
			t.Errorf("publish %s: denied %v, want %v", subj, got, want)
		}
	}
	// No agent may publish a notice either (RT-6.34): it is the login
	// service's alone, whatever the owner.
	for _, subj := range []string{"presence.notice.alice.v1", "presence.notice.-.v1", "presence.notice.bob.v1"} {
		if err := alice.nc.Publish(subj, []byte(`{"v":1,"kind":"session"}`)); err != nil {
			t.Fatal(err)
		}
		if !alice.denied(t) {
			t.Errorf("an agent published a notice on %s", subj)
		}
	}
	// A normal agent cannot either.
	for _, subj := range []string{"presence.attest." + bob.name + ".v1", "presence.attest." + alice.name + ".v1", "presence.attest.agent-other.v1"} {
		if err := alice.nc.Publish(subj, []byte(`{"v":1,"forged":true}`)); err != nil {
			t.Fatal(err)
		}
		if !alice.denied(t) {
			t.Errorf("alice published %s", subj)
		}
	}
	// What is stored is still the service's.
	for _, a := range []*agent{alice, bob, atk} {
		if got := stored(a.name); got.Agent != a.name || got.V != 1 || got.Exp == 0 {
			t.Errorf("attestation of %s: %+v", a.name, got)
		}
	}
	select {
	case err := <-denied:
		t.Errorf("the service's own user was refused: %v", err)
	default:
	}
}

func TestAnAgentsPresenceConsumerDeliversEveryAttestation(t *testing.T) {
	c := newConsumerLab(t, nil)
	c.setup()
	c.svc.SetAttester(NATSAttester{NC: c.nc, Stream: consumers.PresenceStream})
	a, err := c.login("alice", newHost(), Hello{Home: "home"})
	if err != nil {
		t.Fatal(err)
	}
	in := c.listen(a, "deliver."+a.name+".presence")
	b, err := c.login("bob", newHost(), Hello{Home: "home"})
	if err != nil {
		t.Fatal(err)
	}
	want := "presence.attest." + b.name + ".v1"
	deadline := time.After(3 * time.Second)
	for {
		select {
		case m := <-in:
			if m.Subject == want {
				return
			}
		case <-deadline:
			t.Fatalf("alice was not delivered %s", want)
		}
	}
}

// A publish the NATS server refuses shows only asynchronously; the service
// reads the attestation back and refuses the login when it is not stored.
func TestAnAttestationThatIsNotStoredRefusesTheLogin(t *testing.T) {
	c := newConsumerLab(t, nil)
	c.setup()
	nc, err := nats.Connect(c.url, nats.UserInfo("jsnoatt", "pwd"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	c.svc.SetAttester(NATSAttester{NC: nc, Stream: consumers.PresenceStream})
	if a, err := c.login("alice", newHost(), Hello{Home: "home"}); err == nil {
		t.Fatalf("logged in without a stored attestation: %s", a.name)
	}
	c.svc.SetAttester(NATSAttester{NC: c.nc, Stream: consumers.PresenceStream})
	if _, err := c.login("alice", newHost(), Hello{Home: "home"}); err != nil {
		t.Fatalf("with the right: %v", err)
	}
}

// The record counts only with exactly the bytes just written: an agent that
// logs in again, whose new record the server refuses, does not pass on its
// older record with the older list.
func TestAnOlderStoredRecordDoesNotCountForANewLogin(t *testing.T) {
	c := newConsumerLab(t, nil)
	c.setup()
	c.svc.SetAttester(NATSAttester{NC: c.nc, Stream: consumers.PresenceStream})
	host := newHost()
	if _, err := c.login("alice", host, Hello{Home: "home", Trusted: []string{"bob"}}); err != nil {
		t.Fatal(err)
	}
	nc, err := nats.Connect(c.url, nats.UserInfo("jsnoatt", "pwd"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	c.svc.SetAttester(NATSAttester{NC: nc, Stream: consumers.PresenceStream})
	if a, err := c.login("alice", host, Hello{Home: "home", Trusted: []string{"carol"}}); err == nil {
		t.Fatalf("logged in again on the older record: %s", a.name)
	}
}

func TestTheHostsOwnCutMakesTheAttestationCut(t *testing.T) {
	l := newLab(t)
	f := &fakeAttester{}
	l.svc.SetAttester(f)
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home", Trusted: []string{"bob"}, TrustedCut: true}, nil); err != nil {
		t.Fatal(err)
	}
	p, got := f.last(t)
	if !got.Cut || len(got.Trusted) != 1 || !strings.Contains(string(p.body), `"cut":true`) {
		t.Errorf("attestation %s", p.body)
	}
}

// The attestation states the hello's home (RT-6.41, #255): the same granted channel the
// credential's crown claim is for, so a reader tells a home agent from a guest without
// falling back to the claim. A hello that names no home, or one the human is no member
// of, states "" - it got no crown claim either.
func TestTheAttestationNamesTheGrantedHome(t *testing.T) {
	l := newLab(t)
	f := &fakeAttester{}
	l.svc.SetAttester(f)
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil); err != nil {
		t.Fatal(err)
	}
	p, att := f.last(t)
	if att.Home != "home" || !strings.Contains(string(p.body), `"home":"home"`) {
		t.Errorf("attestation %s, home %q", p.body, att.Home)
	}
}

func TestAnAttestationWithoutAGrantedHomeStatesItEmpty(t *testing.T) {
	l := newLab(t)
	f := &fakeAttester{}
	l.svc.SetAttester(f)
	// A home the human is no member of: grant drops it, so no crown claim and no home.
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "elsewhere"}, nil); err != nil {
		t.Fatal(err)
	}
	p, att := f.last(t)
	if att.Home != "" || !strings.Contains(string(p.body), `"home":""`) {
		t.Errorf("attestation %s, home %q", p.body, att.Home)
	}
	// A hello with no home at all.
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{}, nil); err != nil {
		t.Fatal(err)
	}
	p, att = f.last(t)
	if att.Home != "" || !strings.Contains(string(p.body), `"home":""`) {
		t.Errorf("attestation %s, home %q", p.body, att.Home)
	}
}
