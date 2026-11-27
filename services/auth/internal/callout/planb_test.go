package callout

import (
	"context"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

// planBMap is the directory's stored lists, by channel.
type planBMap map[string][]int

func (m planBMap) PlanB(_ context.Context, channels []string) (map[string][]int, error) {
	out := map[string][]int{}
	for _, ch := range channels {
		out[ch] = m[ch]
	}
	return out, nil
}

type planBErr struct{}

func (planBErr) PlanB(context.Context, []string) (map[string][]int, error) {
	return nil, errors.New("bucket gone")
}

// records is what f got, by subject.
func records(t *testing.T, f *fakeAttester) map[string]PlanBRecord {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	out := map[string]PlanBRecord{}
	for _, p := range f.got {
		var r PlanBRecord
		if err := json.Unmarshal(p.body, &r); err != nil {
			t.Fatal(err)
		}
		out[p.subject] = r
	}
	return out
}

// RT-6.39 step 5: at a credential every granted channel gets its record, the
// lobby too: a channel with no piece switched on an empty list, one with some
// its pieces sorted, and an entry outside 1 to 13 (written around the admin
// API) is dropped and logged, the valid ones kept.
func TestACredentialWritesThePlanBPiecesOfEachGrantedChannel(t *testing.T) {
	l := newLab(t)
	f := &fakeAttester{}
	l.svc.SetPlanB(planBMap{"some": {3, 1}, "odd": {2, 14, 0, 2}, "secret": {5}}, f)
	l.chat.Channels["alice"] = []string{"home", "some", "odd"}
	if _, err := l.connect("alice", l.realm.Token("alice", nil),
		Hello{Home: "home", Guests: []string{"some", "odd", "secret"}, Lobby: true}, nil); err != nil {
		t.Fatal(err)
	}
	got := records(t, f)
	want := map[string][]int{"home": {}, "some": {1, 3}, "odd": {2}, "lobby": {}}
	if len(got) != len(want) {
		t.Errorf("records %v", got)
	}
	for ch, pieces := range want {
		r, ok := got["presence.planb."+ch+".v1"]
		if !ok || r.V != 1 || r.Channel != ch || !slices.Equal(r.Pieces, pieces) {
			t.Errorf("%s: %+v (%v), want %v", ch, r, ok, pieces)
		}
	}
	// A channel not granted gets no record from this login.
	if _, ok := got["presence.planb.secret.v1"]; ok {
		t.Error("a channel that was not granted got a record")
	}
	// The body is compact, an empty list is [] rather than null.
	f.mu.Lock()
	for _, p := range f.got {
		if p.subject == "presence.planb.home.v1" && string(p.body) != `{"v":1,"channel":"home","pieces":[]}` {
			t.Errorf("body %s", p.body)
		}
	}
	f.mu.Unlock()
	if logs := l.logs.String(); !strings.Contains(logs, `"event":"plan B entry dropped","channel":"odd","dropped":[14,0,2]`) {
		t.Errorf("log:\n%s", logs)
	}
}

// An unchanged record is not written again until PlanBRepublish has passed; a
// change is written at the next credential.
func TestAnUnchangedPlanBRecordWaitsForPlanBRepublish(t *testing.T) {
	l := newLab(t)
	f := &fakeAttester{}
	src := planBMap{"home": {1}}
	l.svc.SetPlanB(src, f)
	t0 := time.Unix(1_800_000_000, 0)
	count := func() int { f.mu.Lock(); defer f.mu.Unlock(); return len(f.got) }
	l.svc.publishPlanB(context.Background(), "agent-a", []string{"home"}, t0)
	l.svc.publishPlanB(context.Background(), "agent-b", []string{"home"}, t0.Add(time.Minute))
	if n := count(); n != 1 {
		t.Fatalf("%d writes, want 1", n)
	}
	src["home"] = []int{1, 2}
	l.svc.publishPlanB(context.Background(), "agent-a", []string{"home"}, t0.Add(2*time.Minute))
	if r := records(t, f)["presence.planb.home.v1"]; count() != 2 || !slices.Equal(r.Pieces, []int{1, 2}) {
		t.Fatalf("after the change: %d writes, %+v", count(), r)
	}
	l.svc.publishPlanB(context.Background(), "agent-a", []string{"home"}, t0.Add(2*time.Minute+PlanBRepublish))
	if n := count(); n != 3 {
		t.Fatalf("%d writes after PlanBRepublish, want 3", n)
	}
}

// A switch is no condition of the credential: a directory that cannot be read
// or a record that cannot be written refuses no login, and a write that
// failed is tried again at the next one.
func TestAFailedPlanBReadOrWriteRefusesNoLogin(t *testing.T) {
	l := newLab(t)
	f := &fakeAttester{}
	l.svc.SetPlanB(planBErr{}, f)
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil); err != nil {
		t.Fatalf("refused when the directory could not be read: %v", err)
	}
	if !strings.Contains(l.logs.String(), `"event":"plan B not read"`) || len(records(t, f)) != 0 {
		t.Errorf("records %v, log:\n%s", records(t, f), l.logs.String())
	}
	f.err = errors.New("no route")
	l.svc.SetPlanB(planBMap{"home": {4}}, f)
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil); err != nil {
		t.Fatalf("refused when the record could not be written: %v", err)
	}
	if !strings.Contains(l.logs.String(), `"event":"plan B record not written","channel":"home"`) {
		t.Errorf("log:\n%s", l.logs.String())
	}
	f.mu.Lock()
	f.err = nil
	f.mu.Unlock()
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil); err != nil {
		t.Fatal(err)
	}
	if r := records(t, f)["presence.planb.home.v1"]; !slices.Equal(r.Pieces, []int{4}) {
		t.Errorf("not written again: %+v", r)
	}
}

// With the real stream and the minimal JetStream user: the record is stored on
// its subject by the service, and no agent may write it, not even one whose
// owner is called "planb".
func TestOnlyTheServiceWritesAPlanBRecord(t *testing.T) {
	l := newLabCfg(t, nil, 2800*time.Millisecond, nil)
	adm, js := l.admin()
	if r, err := adm.Request("$JS.API.STREAM.CREATE.PRESENCE", []byte(presenceStreamCfg), 3*time.Second); err != nil || strings.Contains(string(r.Data), `"error"`) {
		t.Fatalf("stream PRESENCE: %v", err)
	}
	denied := make(chan error, 4)
	svcNC, err := nats.Connect(l.url, nats.UserInfo("jsmin", "pwd"),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, err error) { denied <- err }))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(svcNC.Close)
	l.svc.SetPlanB(planBMap{"home": {2, 7}}, NATSPlanB{NC: svcNC})
	l.chat.Users["planb"] = token.User{Username: "planb", AuthKnown: true, AuthService: token.OpenIDService, AuthData: "sub-planb"}
	l.chat.Channels["planb"] = []string{"home"}
	atk, err := l.connect("planb", l.realm.Token("planb", nil), Hello{Home: "home"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	m, err := js.GetLastMsg("PRESENCE", "presence.planb.home.v1")
	if err != nil {
		t.Fatalf("no record: %v", err)
	}
	if string(m.Data) != `{"v":1,"channel":"home","pieces":[2,7]}` {
		t.Errorf("stored %s", m.Data)
	}
	for subj, want := range map[string]bool{
		"presence.planb." + atk.name: false, // its own presence, three tokens
		"presence.planb.home.v1":     true,
		"presence.planb.other.v1":    true,
	} {
		if err := atk.nc.Publish(subj, []byte(`{"v":1,"channel":"home","pieces":[1,2,3,4,5,6,7,8,9,10,11,12,13]}`)); err != nil {
			t.Fatal(err)
		}
		if got := atk.denied(t); got != want {
			t.Errorf("publish %s: denied %v, want %v", subj, got, want)
		}
	}
	if m, err := js.GetLastMsg("PRESENCE", "presence.planb.home.v1"); err != nil || string(m.Data) != `{"v":1,"channel":"home","pieces":[2,7]}` {
		t.Errorf("stored after the forgeries: %s, %v", m.Data, err)
	}
	select {
	case err := <-denied:
		t.Errorf("the service's own user was refused: %v", err)
	default:
	}
}

// One channel whose record cannot be written does not stop the others'.
func TestAFailedPlanBWriteGoesOnWithTheNextChannel(t *testing.T) {
	l := newLab(t)
	f := &fakeAttester{failOn: ".planb.bad."}
	l.svc.SetPlanB(planBMap{"bad": {1}, "good": {2}}, f)
	l.svc.publishPlanB(context.Background(), "agent-a", []string{"bad", "good"}, time.Unix(1_800_000_000, 0))
	got := records(t, f)
	if r, ok := got["presence.planb.good.v1"]; !ok || !slices.Equal(r.Pieces, []int{2}) || len(got) != 1 {
		t.Fatalf("records %v", got)
	}
}

// The credential does not wait for plan B's records: a write that hangs
// delays no answer (RT-6.39 step 5), and is written once it goes on.
func TestThePlanBWriteDoesNotDelayTheCredential(t *testing.T) {
	l := newLab(t)
	l.noSettle = true
	b := blockingPub{entered: make(chan struct{}, 8), release: make(chan struct{})}
	l.svc.SetPlanB(planBMap{"home": {1}}, b)
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil); err != nil {
		t.Fatalf("refused: %v", err)
	}
	select {
	case <-b.entered: // the write is under way, the login is through
	case <-time.After(5 * time.Second):
		t.Fatal("no record was written")
	}
	close(b.release)
	l.svc.bg.Wait()
}

type blockingPub struct{ entered, release chan struct{} }

func (b blockingPub) Publish(ctx context.Context, _ string, _ []byte) error {
	b.entered <- struct{}{}
	select {
	case <-b.release:
	case <-ctx.Done():
	}
	return nil
}
