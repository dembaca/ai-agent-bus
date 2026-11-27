package callout

// The approvers' refresh (RT-6.42 step 3, #266): records with the approver
// flag from Keycloak's group, without a login.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	gojwt "github.com/golang-jwt/jwt/v5"
	"github.com/nats-io/nats.go"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/consumers"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/directory"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/testrealm"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

// fakeGroup is Keycloak's approvers' group: its members, or an error.
type fakeGroup struct {
	mu      sync.Mutex
	members []Approver
	err     error
	panics  bool
	calls   int
}

func (g *fakeGroup) Members(_ context.Context, max int) ([]Approver, bool, error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.calls++
	if g.panics {
		panic("keycloak stand-in")
	}
	if g.err != nil {
		return nil, false, g.err
	}
	ms := append([]Approver(nil), g.members...)
	if len(ms) > max {
		return ms[:max], true, nil
	}
	return ms, false, nil
}

func (g *fakeGroup) set(names ...string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.members = nil
	for _, n := range names {
		g.members = append(g.members, Approver{Sub: "sub-" + n, Username: n})
	}
}

func (g *fakeGroup) count() int {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.calls
}

// refreshLab is accountLab with a chat and a directory that know the given
// people: each an OpenID account of "sub-<name>" whose identity the
// directory holds (anchor = name).
func refreshLab(t *testing.T, people ...string) (*lab, *fakeAttester, *testrealm.Chat, ApproverRefresh, *fakeGroup) {
	t.Helper()
	l, f, dir := accountLab(t)
	chat := testrealm.NewChat(people...)
	for _, p := range people {
		if _, err := dir.Login(ctx5(t), "sub-"+p, p, p); err != nil {
			t.Fatal(err)
		}
	}
	g := &fakeGroup{}
	r := ApproverRefresh{Group: g, Chat: l.realm.Verifier(chat), Anchors: directory.Persons{Store: dir}, Period: 5 * time.Minute}
	return l, f, chat, r, g
}

func (f *fakeAttester) recordsOf(account string) (out []AccountRecord) {
	for _, r := range f.records() {
		if r.Account == account {
			out = append(out, r)
		}
	}
	return out
}

// RT-6.42 step 3: a member of the approvers' group whose Mattermost account
// is bound to their identity gets the record with the flag, with no login:
// the subject, format and anchor of a login's record, valid one period and
// the grace from the run.
func TestTheRefreshWritesAnApproversRecordWithoutALogin(t *testing.T) {
	l, f, _, r, g := refreshLab(t, "alice")
	g.set("alice")
	before := time.Now().Unix()
	next, run := l.svc.refreshApprovers(ctx5(t), r, nil)
	if run.err != nil || run.members != 1 || run.written != 1 || run.failed != 0 {
		t.Fatalf("run %+v", run)
	}
	if _, ok := next["sub-alice"]; !ok {
		t.Errorf("alice is not kept as an approver: %v", next)
	}
	id := testrealm.ChatID("alice")
	f.mu.Lock()
	subject := f.got[len(f.got)-1].subject
	f.mu.Unlock()
	if subject != "presence.account.mattermost."+id+".v1" {
		t.Errorf("subject %q", subject)
	}
	rs := f.recordsOf(id)
	if len(rs) != 1 {
		t.Fatalf("%d records", len(rs))
	}
	rec := rs[0]
	if rec.V != 1 || rec.Anchor != "alice" || rec.Name != "alice" || !rec.Approver || rec.Iat < before ||
		rec.TokenIat != rec.Iat || rec.Exp-rec.Iat != int64((r.Period+ApproverGrace)/time.Second) {
		t.Errorf("record %+v", rec)
	}
	// The body is the login's format, field for field (decoded strictly on the hosts).
	f.mu.Lock()
	body := f.got[len(f.got)-1].body
	f.mu.Unlock()
	var keys map[string]json.RawMessage
	if err := json.Unmarshal(body, &keys); err != nil || len(keys) != 8 {
		t.Errorf("body %s", body)
	}
}

// RT-6.42 step 3: who leaves the group loses the flag at the next run, by a
// record without it; who is still in keeps it, renewed.
func TestAMemberWhoLeftTheGroupLosesTheFlagAtTheNextRun(t *testing.T) {
	l, f, _, r, g := refreshLab(t, "alice", "bob")
	g.set("alice", "bob")
	last, _ := l.svc.refreshApprovers(ctx5(t), r, nil)
	g.set("bob")
	l.svc.now = func() time.Time { return time.Now().Add(r.Period) }
	last, run := l.svc.refreshApprovers(ctx5(t), r, last)
	if run.written != 1 || run.revoked != 1 {
		t.Fatalf("run %+v", run)
	}
	a := f.recordsOf(testrealm.ChatID("alice"))
	if len(a) != 2 || !a[0].Approver || a[1].Approver || a[1].Anchor != "alice" {
		t.Errorf("alice's records %+v", a)
	}
	b := f.recordsOf(testrealm.ChatID("bob"))
	if len(b) != 2 || !b[1].Approver || b[1].Iat <= b[0].Iat {
		t.Errorf("bob's records %+v", b)
	}
	if _, ok := last["sub-alice"]; ok {
		t.Error("alice is still kept as an approver")
	}
	// Revoked once: the run after writes nothing more for her.
	l.svc.now = func() time.Time { return time.Now().Add(2 * r.Period) }
	if _, run := l.svc.refreshApprovers(ctx5(t), r, last); run.revoked != 0 {
		t.Errorf("run %+v", run)
	}
	if n := len(f.recordsOf(testrealm.ChatID("alice"))); n != 2 {
		t.Errorf("%d records for alice", n)
	}
}

// RT-6.42 step 3: an account the login would not bind gets nothing: a
// password account, one of another `sub`, a guest, a bot, a deactivated one,
// none of that name, and an identity the directory holds no anchor for.
func TestUnboundAccountsGetNothing(t *testing.T) {
	l, f, chat, r, g := refreshLab(t, "pw", "other", "guest", "bot", "gone", "alice")
	chat.Users["pw"] = tokenUserPassword("pw")
	u := chat.Users["other"]
	u.AuthData = "sub-someone-else"
	chat.Users["other"] = u
	u = chat.Users["guest"]
	u.Guest = true
	chat.Users["guest"] = u
	u = chat.Users["bot"]
	u.Bot = true
	chat.Users["bot"] = u
	u = chat.Users["gone"]
	u.Deactivated = true
	chat.Users["gone"] = u
	chat.Users["noanchor"] = token.User{Username: "noanchor", ID: testrealm.ChatID("noanchor"), AuthKnown: true,
		AuthService: token.OpenIDService, AuthData: "sub-noanchor"}
	g.set("pw", "other", "guest", "bot", "gone", "nochat", "noanchor", "alice")
	_, run := l.svc.refreshApprovers(ctx5(t), r, nil)
	if run.err != nil || run.members != 8 || run.written != 1 || run.skipped != 7 || run.failed != 0 {
		t.Fatalf("run %+v", run)
	}
	rs := f.records()
	if len(rs) != 1 || rs[0].Account != testrealm.ChatID("alice") {
		t.Errorf("records %+v", rs)
	}
}

// RT-6.42 step 3: a member whose account is no longer bound gets no record,
// with or without the group: the one before runs out (one period and the
// grace), and none binds the account another identity holds now.
func TestAnAccountUnboundSinceTheLastRunGetsNothing(t *testing.T) {
	l, f, chat, r, g := refreshLab(t, "alice", "bob")
	g.set("alice", "bob")
	last, _ := l.svc.refreshApprovers(ctx5(t), r, nil)
	for _, n := range []string{"alice", "bob"} {
		u := chat.Users[n]
		u.AuthData = "sub-new-holder"
		chat.Users[n] = u
	}
	g.set("bob") // alice left as well, bob is still in
	_, run := l.svc.refreshApprovers(ctx5(t), r, last)
	if run.written != 0 || run.revoked != 0 || run.skipped != 2 {
		t.Fatalf("run %+v", run)
	}
	if n := len(f.records()); n != 2 {
		t.Errorf("%d records, want the first run's 2", n)
	}
}

// RT-6.42 step 3: Keycloak down writes nothing and keeps the approvers of the
// run before, so who left in between is revoked at the next run that reads
// the group; a panic is caught, never a crash.
func TestKeycloakDownWritesNothingAndTheNextRunCatchesUp(t *testing.T) {
	l, f, _, r, g := refreshLab(t, "alice")
	g.set("alice")
	last, _ := l.svc.refreshApprovers(ctx5(t), r, nil)
	g.err = errors.New("connection refused")
	last, run := l.svc.refreshOnce(ctx5(t), r, last)
	if run.err == nil || run.written+run.revoked != 0 || len(last) != 1 {
		t.Fatalf("run %+v, last %v", run, last)
	}
	g.err, g.panics = nil, true
	last, run = l.svc.refreshOnce(ctx5(t), r, last)
	if run.recovers != 1 || len(last) != 1 {
		t.Fatalf("run %+v, last %v", run, last)
	}
	if n := len(f.records()); n != 1 {
		t.Errorf("%d records while Keycloak was down", n)
	}
	g.panics = false
	g.set()
	if _, run = l.svc.refreshOnce(ctx5(t), r, last); run.revoked != 1 {
		t.Errorf("run %+v", run)
	}
	if rs := f.records(); len(rs) != 2 || rs[1].Approver {
		t.Errorf("records %+v", rs)
	}
}

// The chat down for one member is a failure for that member alone, retried
// at the next run; the others are written.
func TestAChatLookupThatFailsIsRetriedNextRun(t *testing.T) {
	l, f, _, r, g := refreshLab(t, "alice", "bob")
	g.set("alice", "bob")
	r.Chat = failingFor{r.Chat, "alice"}
	_, run := l.svc.refreshApprovers(ctx5(t), r, nil)
	if run.failed != 1 || run.written != 1 {
		t.Fatalf("run %+v", run)
	}
	if !strings.Contains(l.logs.String(), "lookup failed") {
		t.Errorf("logs: %s", l.logs.String())
	}
	r.Chat = r.Chat.(failingFor).ChatBinding
	if _, run = l.svc.refreshApprovers(ctx5(t), r, nil); run.written != 2 {
		t.Errorf("run %+v", run)
	}
	if n := len(f.recordsOf(testrealm.ChatID("alice"))); n != 1 {
		t.Errorf("%d records for alice", n)
	}
}

type failingFor struct {
	ChatBinding
	name string
}

func (f failingFor) BoundAccount(ctx context.Context, username, sub string) (string, error) {
	if username == f.name {
		return "", token.ErrDirectory
	}
	return f.ChatBinding.BoundAccount(ctx, username, sub)
}

// RT-6.42 step 3: the refresh and logins share one guard. A login's token
// issued before the run does not replace the run's record; one issued after
// it does, so a token without the group takes the flag away at once.
func TestTheRefreshAndLoginsShareTheTokenGuard(t *testing.T) {
	l, f, _, r, g := refreshLab(t, "alice")
	l.chat.Users["alice"] = token.User{Username: "alice", ID: testrealm.ChatID("alice"), AuthKnown: true,
		AuthService: token.OpenIDService, AuthData: "sub-alice"}
	g.set("alice")
	if _, run := l.svc.refreshApprovers(ctx5(t), r, nil); run.written != 1 {
		t.Fatalf("run %+v", run)
	}
	older := time.Now().Add(-time.Minute).Unix()
	if _, err := l.connect("alice", l.realm.Token("alice", func(c gojwt.MapClaims) { c["iat"] = older; delete(c, "groups") }), Hello{Home: "home"}, nil); err != nil {
		t.Fatal(err)
	}
	if rs := f.recordsOf(testrealm.ChatID("alice")); len(rs) != 1 || !rs[0].Approver {
		t.Fatalf("an older token replaced the run's record: %+v", rs)
	}
	newer := time.Now().Add(2 * time.Second).Unix()
	if _, err := l.connect("alice", l.realm.Token("alice", func(c gojwt.MapClaims) { c["iat"] = newer; delete(c, "groups") }), Hello{Home: "home"}, nil); err != nil {
		t.Fatal(err)
	}
	if rs := f.recordsOf(testrealm.ChatID("alice")); len(rs) != 2 || rs[1].Approver || rs[1].TokenIat != newer {
		t.Errorf("records %+v", rs)
	}
}

// The cap: a group with more than MaxApprovers members is cut and logged,
// and nobody is revoked for being beyond it.
func TestTheCapCutsTheGroupAndRevokesNobody(t *testing.T) {
	l := newLab(t)
	f := &fakeAttester{}
	l.svc.SetAttester(f)
	l.svc.SetAnchors(guardedAnchor("x"))
	g := &fakeGroup{}
	for i := range MaxApprovers + 5 {
		n := fmt.Sprintf("m%03d", i)
		g.members = append(g.members, Approver{Sub: "sub-" + n, Username: n})
	}
	r := ApproverRefresh{Group: g, Chat: idChat{}, Anchors: nameAnchors{}, Period: time.Minute}
	last := map[string]approverRef{"sub-beyond": {account: testrealm.ChatID("beyond"), name: "beyond", anchor: "beyond"}}
	_, run := l.svc.refreshOnce(ctx5(t), r, last)
	if !run.cut || run.members != MaxApprovers || run.written != MaxApprovers || run.revoked != 0 {
		t.Fatalf("run %+v", run)
	}
	l.svc.logRun(run)
	if !strings.Contains(l.logs.String(), "group cut") {
		t.Errorf("logs: %s", l.logs.String())
	}
}

type idChat struct{}

func (idChat) BoundAccount(_ context.Context, username, _ string) (string, error) {
	return testrealm.ChatID(username), nil
}

type nameAnchors struct{}

func (nameAnchors) Anchor(_ context.Context, sub string) (string, bool, error) {
	return strings.TrimPrefix(sub, "sub-"), true, nil
}

// The period: a run at once and one every period, until the context ends.
func TestTheRefreshRunsEveryPeriodUntilItsContextEnds(t *testing.T) {
	l, _, _, r, g := refreshLab(t, "alice")
	g.set("alice")
	r.Period = 20 * time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); l.svc.RefreshApprovers(ctx, r) }()
	deadline := time.Now().Add(5 * time.Second)
	for g.count() < 3 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("the refresh did not stop with its context")
	}
	if n := g.count(); n < 3 {
		t.Errorf("%d runs", n)
	}
	if !strings.Contains(l.logs.String(), "run done") {
		t.Errorf("logs: %s", l.logs.String())
	}
}

// RT-6.42 step 3, the rights: the refresh publishes and reads back through
// the service's JetStream user with exactly the rights the example
// configuration gives it (jsmin), no new one.
func TestTheRefreshNeedsNoNewRight(t *testing.T) {
	l, _, _, r, g := refreshLab(t, "alice")
	adm, _ := l.admin()
	if rep, err := adm.Request("$JS.API.STREAM.CREATE.PRESENCE", []byte(presenceStreamCfg), 3*time.Second); err != nil || strings.Contains(string(rep.Data), `"error"`) {
		t.Fatalf("stream: %v", err)
	}
	denied := make(chan error, 4)
	svcNC, err := nats.Connect(l.url, nats.UserInfo("jsmin", "pwd"),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, err error) { denied <- err }))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(svcNC.Close)
	l.svc.SetAttester(NATSAttester{NC: svcNC, Stream: consumers.PresenceStream})
	g.set("alice")
	if _, run := l.svc.refreshApprovers(ctx5(t), r, nil); run.written != 1 || run.failed != 0 {
		t.Fatalf("run %+v (logs %s)", run, l.logs.String())
	}
	select {
	case err := <-denied:
		t.Fatalf("refused: %v", err)
	default:
	}
	msg, err := adm.Request("$JS.API.STREAM.MSG.GET.PRESENCE",
		[]byte(`{"last_by_subj":"presence.account.mattermost.`+testrealm.ChatID("alice")+`.v1"}`), 3*time.Second)
	if err != nil || !strings.Contains(string(msg.Data), `"message"`) {
		t.Fatalf("not stored: %v %s", err, msg.Data)
	}
}
