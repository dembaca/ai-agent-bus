package callout

// RT-6.42 step 3 (#266, the owner's decision of 2026-11-27): a login with
// either proof (OpenID or LDAP) writes the account record, its proof for the
// bridge and the binding, kept in the directory; the sweep publishes both
// records again from there while Mattermost shows the account as proven, and
// unbinds it once it does not.

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	gojwt "github.com/golang-jwt/jwt/v5"
	natsjwt "github.com/nats-io/jwt/v2"
	"github.com/nats-io/nats.go/jetstream"
	"github.com/nats-io/nkeys"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/directory"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/testrealm"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

// on is what f published on the subject that ends with suffix, newest last.
func (f *fakeAttester) on(suffix string) (out [][]byte) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, p := range f.got {
		if strings.HasSuffix(p.subject, suffix) {
			out = append(out, p.body)
		}
	}
	return out
}

// ldapUser is a chat account that logs in over LDAP with that entry id.
func ldapUser(l *lab, name, entry string) {
	l.chat.Users[name] = token.User{Username: name, ID: testrealm.ChatID(name), AuthKnown: true,
		AuthService: token.LDAPService, AuthData: entry}
	l.chat.Channels[name] = []string{"home"}
}

func TestAnLDAPLoginWritesTheRecordItsProofAndTheBinding(t *testing.T) {
	l, f, dir := accountLab(t)
	ldapUser(l, "lena", "8d1f-uuid")
	tok := l.realm.Token("lena", func(c gojwt.MapClaims) { c["ldap_id"] = "8d1f-uuid" })
	if _, err := l.connect("lena", tok, Hello{Home: "home"}, nil); err != nil {
		t.Fatal(err)
	}
	id := testrealm.ChatID("lena")
	recs := f.on("." + id + ".v1")
	if len(recs) != 1 {
		t.Fatalf("%d records", len(recs))
	}
	var rec AccountRecord
	if err := json.Unmarshal(recs[0], &rec); err != nil || rec.Account != id || rec.Anchor != "lena" {
		t.Fatalf("record %+v %v", rec, err)
	}
	proofs := f.on("." + id + ".auth.v1")
	var p ProofRecord
	if len(proofs) != 1 || json.Unmarshal(proofs[0], &p) != nil || p != (ProofRecord{V: 1, Account: id,
		AuthService: "ldap", AuthData: "8d1f-uuid", Iat: rec.Iat}) {
		t.Fatalf("proofs %q", proofs)
	}
	b, _, ok, err := dir.Binding(ctx5(t), id)
	if err != nil || !ok || b.Sub != "sub-lena" || b.Anchor != "lena" || b.AuthService != "ldap" ||
		b.AuthData != "8d1f-uuid" || string(b.Record) != string(recs[0]) {
		t.Fatalf("binding %+v %v %v", b, ok, err)
	}
}

func TestAnOpenIDLoginWritesItsProofAndTheBindingToo(t *testing.T) {
	l, f, dir := accountLab(t)
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil); err != nil {
		t.Fatal(err)
	}
	id := testrealm.ChatID("alice")
	var p ProofRecord
	if proofs := f.on("." + id + ".auth.v1"); len(proofs) != 1 || json.Unmarshal(proofs[0], &p) != nil ||
		p.AuthService != "openid" || p.AuthData != "sub-alice" {
		t.Fatalf("proofs %q", proofs)
	}
	if b, _, ok, err := dir.Binding(ctx5(t), id); err != nil || !ok || b.AuthService != "openid" || b.AuthData != "sub-alice" {
		t.Fatalf("binding %+v %v %v", b, ok, err)
	}
}

// Name equality alone is no proof: a password account, and an LDAP account
// whose token has no LDAP id, log in, and get no record, no proof and no
// binding; an LDAP account whose token names another LDAP id is refused.
func TestANameAloneWritesNoRecordNoProofAndNoBinding(t *testing.T) {
	l, f, dir := accountLab(t)
	l.chat.Users["pw"] = tokenUserPassword("pw")
	l.chat.Channels["pw"] = []string{"home"}
	ldapUser(l, "lena", "8d1f-uuid")
	for name, tok := range map[string]string{
		"pw":   l.realm.Token("pw", func(c gojwt.MapClaims) { c["ldap_id"] = "8d1f-uuid" }),
		"lena": l.realm.Token("lena", nil),
	} {
		if _, err := l.connect(name, tok, Hello{Home: "home"}, nil); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
	}
	if _, err := l.connect("lena", l.realm.Token("lena", func(c gojwt.MapClaims) { c["ldap_id"] = "0000" }),
		Hello{Home: "home"}, nil); err == nil {
		t.Fatal("an LDAP account of another entry was let in")
	}
	f.mu.Lock()
	for _, p := range f.got {
		if strings.Contains(p.subject, ".account.") {
			t.Errorf("published %s", p.subject)
		}
	}
	f.mu.Unlock()
	if got, err := dir.BindingAccounts(ctx5(t)); err != nil || len(got) != 0 {
		t.Fatalf("bindings %v %v", got, err)
	}
}

// The sweep publishes a binding's record, as last written, and its proof
// again while the chat shows the account as proven, so neither ages out of
// PRESENCE; and it does so from the directory alone: after a restart (a new
// store on the same bucket, a stream that lost both records) the person is
// bound again without another login.
func TestTheBindingOutlivesARestartWithoutAnotherLogin(t *testing.T) {
	l, f, _ := accountLab(t)
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil); err != nil {
		t.Fatal(err)
	}
	id := testrealm.ChatID("alice")
	written := f.on("." + id + ".v1")
	// the restart: a new store on the same bucket, nothing on the stream
	nc, _ := l.admin()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	dir2, err := directory.Open(ctx5(t), js, "", false)
	if err != nil {
		t.Fatal(err)
	}
	l.svc.SetAnchors(directory.Anchors{Store: dir2})
	f2 := &fakeAttester{}
	l.svc.SetAttester(f2)
	kept, dropped, err := l.svc.sweepBindings(ctx5(t), l.chat)
	if err != nil || kept != 1 || dropped != 0 {
		t.Fatalf("sweep: %d kept, %d unbound, %v", kept, dropped, err)
	}
	if got := f2.on("." + id + ".v1"); len(got) != 1 || string(got[0]) != string(written[len(written)-1]) {
		t.Fatalf("the record again: %q, want %q", got, written)
	}
	var p ProofRecord
	if got := f2.on("." + id + ".auth.v1"); len(got) != 1 || json.Unmarshal(got[0], &p) != nil ||
		p.AuthService != "openid" || p.AuthData != "sub-alice" {
		t.Fatalf("the proof again: %q", got)
	}
}

// A binding the chat no longer shows is unbound: a record with anchor null
// and the binding dropped (a changed auth_data, another way of logging in,
// deactivated, a guest, a bot, gone).
func TestTheSweepUnbindsAnAccountThatNoLongerLogsInAsProven(t *testing.T) {
	for name, edit := range map[string]func(*token.User){
		"another auth_data":  func(u *token.User) { u.AuthData = "sub-mallory" },
		"another way to log": func(u *token.User) { u.AuthService = token.LDAPService },
		"deactivated":        func(u *token.User) { u.Deactivated = true },
		"a guest":            func(u *token.User) { u.Guest = true },
		"a bot":              func(u *token.User) { u.Bot = true },
		"gone":               nil,
	} {
		t.Run(name, func(t *testing.T) {
			l, f, dir := accountLab(t)
			if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil); err != nil {
				t.Fatal(err)
			}
			id := testrealm.ChatID("alice")
			if edit == nil {
				delete(l.chat.Users, "alice")
			} else {
				u := l.chat.Users["alice"]
				edit(&u)
				l.chat.Users["alice"] = u
			}
			kept, dropped, err := l.svc.sweepBindings(ctx5(t), l.chat)
			if err != nil || kept != 0 || dropped != 1 {
				t.Fatalf("sweep: %d kept, %d unbound, %v", kept, dropped, err)
			}
			recs := f.on("." + id + ".v1")
			var last map[string]any
			if err := json.Unmarshal(recs[len(recs)-1], &last); err != nil {
				t.Fatal(err)
			}
			if a, ok := last["anchor"]; !ok || a != nil || last["account"] != id || last["approver"] != false {
				t.Fatalf("the last record %v", last)
			}
			if _, _, ok, err := dir.Binding(ctx5(t), id); ok || err != nil {
				t.Fatalf("the binding stayed: %v %v", ok, err)
			}
			// nothing left to sweep
			if kept, dropped, err := l.svc.sweepBindings(ctx5(t), l.chat); err != nil || kept+dropped != 0 {
				t.Fatalf("second sweep: %d %d %v", kept, dropped, err)
			}
		})
	}
}

type downChat struct{ known bool }

func (d downChat) UserByID(_ context.Context, id string) (token.User, error) {
	if d.known {
		return token.User{}, errors.New("connection refused")
	}
	return token.User{ID: id, AuthKnown: false}, nil
}

// A chat that cannot be asked, or does not say how the account logs in,
// leaves the binding as it is: nothing published, nothing dropped.
func TestTheSweepLeavesABindingTheChatCannotAnswerFor(t *testing.T) {
	for _, chat := range []downChat{{known: true}, {known: false}} {
		l, f, dir := accountLab(t)
		if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil); err != nil {
			t.Fatal(err)
		}
		before := len(f.on(".v1"))
		kept, dropped, err := l.svc.sweepBindings(ctx5(t), chat)
		if err == nil || kept+dropped != 0 || len(f.on(".v1")) != before {
			t.Fatalf("%+v: %d kept, %d unbound, %v", chat, kept, dropped, err)
		}
		if _, _, ok, err := dir.Binding(ctx5(t), testrealm.ChatID("alice")); !ok || err != nil {
			t.Fatalf("%+v: the binding went: %v", chat, err)
		}
	}
}

// A newer login of the same person is never overtaken by the sweep: it
// republishes what the directory holds now, the newest record.
func TestTheSweepRepublishesTheNewestRecord(t *testing.T) {
	l, f, _ := accountLab(t)
	now := time.Now().Unix()
	for _, iat := range []int64{now - 60, now - 10} {
		tok := l.realm.Token("alice", func(c gojwt.MapClaims) { c["iat"] = iat })
		if _, err := l.connect("alice", tok, Hello{Home: "home"}, nil); err != nil {
			t.Fatal(err)
		}
	}
	id := testrealm.ChatID("alice")
	written := f.on("." + id + ".v1")
	if _, _, err := l.svc.sweepBindings(ctx5(t), l.chat); err != nil {
		t.Fatal(err)
	}
	all := f.on("." + id + ".v1")
	var rec AccountRecord
	if len(written) != 2 || json.Unmarshal(all[len(all)-1], &rec) != nil || rec.TokenIat != now-10 ||
		string(all[len(all)-1]) != string(written[1]) {
		t.Fatalf("republished %s, want the newest %s", all[len(all)-1], written[len(written)-1])
	}
}

// RT-6.42 step 3 (#266): a console login proves the binding as an agent's
// login does: the record, its proof and the binding.
func TestAConsoleLoginProvesTheBindingToo(t *testing.T) {
	c, _ := personLab(t)
	c.chat.Users["ann"] = token.User{Username: "ann", ID: testrealm.ChatID("ann"), AuthKnown: true,
		AuthService: token.OpenIDService, AuthData: "sub-ann"}
	js, err := jetstream.New(c.nc)
	if err != nil {
		t.Fatal(err)
	}
	dir, err := directory.Open(ctx5(t), js, "", false)
	if err != nil {
		t.Fatal(err)
	}
	c.svc.SetAnchors(directory.Anchors{Store: dir})
	srv, _ := nkeys.CreateServer()
	key, _ := nkeys.CreateUser()
	pub, _ := key.PublicKey()
	sig, _ := key.Sign([]byte("nonce-console"))
	now := time.Now()
	req, err := c.svc.request(serverRequest(t, srv, func(q *natsjwt.AuthorizationRequestClaims) {
		q.ClientInformation.Nonce = "nonce-console"
		q.ConnectOptions.Nkey = pub
		q.ConnectOptions.SignedNonce = base64.RawURLEncoding.EncodeToString(sig)
		q.ConnectOptions.Token = c.consoleToken("ann", nil)
	}), now)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.svc.authorize(ctx5(t), req, now); err != nil {
		t.Fatal(err)
	}
	id := testrealm.ChatID("ann")
	b, _, ok, err := dir.Binding(ctx5(t), id)
	if err != nil || !ok || b.Sub != "sub-ann" || b.Anchor != "ann.k" || b.AuthService != "openid" || b.AuthData != "sub-ann" {
		t.Fatalf("binding %+v %v %v", b, ok, err)
	}
	m, err := c.js.GetLastMsg("PRESENCE", "presence.account.mattermost."+id+".v1")
	var rec AccountRecord
	if err != nil || json.Unmarshal(m.Data, &rec) != nil || rec.Anchor != "ann.k" || rec.Account != id {
		t.Fatalf("record %+v %v", rec, err)
	}
	m, err = c.js.GetLastMsg("PRESENCE", "presence.account.mattermost."+id+".auth.v1")
	var p ProofRecord
	if err != nil || json.Unmarshal(m.Data, &p) != nil || p.AuthData != "sub-ann" {
		t.Fatalf("proof %+v %v", p, err)
	}
}

// A binding holds 30 days from the login that proved it (directory.
// BindingMaxAge): the hourly check unbinds an older one, without asking the
// chat, as it does a changed account; the next proven login renews it. So a
// person disabled in Keycloak whose Mattermost account stays active stops
// approving within 30 days.
func TestABindingOlderThanThirtyDaysIsUnboundAndALoginRenewsIt(t *testing.T) {
	l, f, dir := accountLab(t)
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil); err != nil {
		t.Fatal(err)
	}
	id := testrealm.ChatID("alice")
	age := func(d time.Duration) {
		t.Helper()
		b, _, ok, err := dir.Binding(ctx5(t), id)
		if err != nil || !ok {
			t.Fatal(ok, err)
		}
		b.At = time.Now().Add(-d).Unix()
		if err := dir.PutBinding(ctx5(t), b); err != nil {
			t.Fatal(err)
		}
	}
	age(directory.BindingMaxAge - time.Hour)
	if kept, dropped, err := l.svc.sweepBindings(ctx5(t), l.chat); err != nil || kept != 1 || dropped != 0 {
		t.Fatalf("29 days: %d kept, %d unbound, %v", kept, dropped, err)
	}
	age(directory.BindingMaxAge + time.Hour)
	if kept, dropped, err := l.svc.sweepBindings(ctx5(t), downChat{known: true}); err != nil || kept != 0 || dropped != 1 {
		t.Fatalf("31 days, the chat not asked: %d kept, %d unbound, %v", kept, dropped, err)
	}
	recs := f.on("." + id + ".v1")
	var last map[string]any
	if err := json.Unmarshal(recs[len(recs)-1], &last); err != nil || last["anchor"] != nil {
		t.Fatalf("the last record %v %v", last, err)
	}
	if _, _, ok, err := dir.Binding(ctx5(t), id); ok || err != nil {
		t.Fatalf("the binding stayed: %v %v", ok, err)
	}
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil); err != nil {
		t.Fatal(err)
	}
	if b, _, ok, err := dir.Binding(ctx5(t), id); !ok || err != nil || b.Expired(time.Now()) {
		t.Fatalf("a login did not renew it: %+v %v %v", b, ok, err)
	}
}

// The binding is written before the proof and the record: a write that fails
// later refuses the login and leaves no record on NATS that the hourly check
// does not know of; the check publishes both once it can.
func TestAFailedProofOrRecordLeavesNoRecordTheCheckDoesNotKnow(t *testing.T) {
	for _, fail := range []string{".auth.v1", ".v1"} {
		l, f, dir := accountLab(t)
		id := testrealm.ChatID("alice")
		f.failOn = "presence.account.mattermost." + id + fail // the proof, or the record
		if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil); err == nil {
			t.Fatalf("%s: the login passed", fail)
		}
		if _, _, ok, err := dir.Binding(ctx5(t), id); !ok || err != nil {
			t.Fatalf("%s: no binding: %v", fail, err)
		}
		if got := f.on("." + id + ".v1"); len(got) != 0 {
			t.Fatalf("%s: a record was published: %q", fail, got)
		}
		f2 := &fakeAttester{}
		l.svc.SetAttester(f2)
		if kept, _, err := l.svc.sweepBindings(ctx5(t), l.chat); err != nil || kept != 1 ||
			len(f2.on("."+id+".v1")) != 1 || len(f2.on("."+id+".auth.v1")) != 1 {
			t.Fatalf("%s: the check: %d kept, %v", fail, kept, err)
		}
	}
}

// A console token that says nothing about the approvers' group (here: the
// console's verifier has none) keeps the stored record's flag: a console login
// never lowers an approver's flag by a token that cannot tell (#266).
func TestAConsoleLoginThatCannotTellKeepsTheApproverFlag(t *testing.T) {
	c, _ := personLab(t)
	c.chat.Users["ann"] = token.User{Username: "ann", ID: testrealm.ChatID("ann"), AuthKnown: true,
		AuthService: token.OpenIDService, AuthData: "sub-ann"}
	c.chat.Channels["ann"] = []string{"home"}
	js, err := jetstream.New(c.nc)
	if err != nil {
		t.Fatal(err)
	}
	dir, err := directory.Open(ctx5(t), js, "", false)
	if err != nil {
		t.Fatal(err)
	}
	c.svc.SetAnchors(directory.Anchors{Store: dir})
	// an agent's login first, with the approvers' group, a minute ago
	tok := c.realm.Token("ann", func(m gojwt.MapClaims) { m["iat"] = time.Now().Add(-time.Minute).Unix() })
	if _, err := c.connect("ann", tok, Hello{Home: "home"}, nil); err != nil {
		t.Fatal(err)
	}
	id := testrealm.ChatID("ann")
	flag := func() bool {
		t.Helper()
		m, err := c.js.GetLastMsg("PRESENCE", "presence.account.mattermost."+id+".v1")
		var rec AccountRecord
		if err != nil || json.Unmarshal(m.Data, &rec) != nil {
			t.Fatalf("record %v", err)
		}
		return rec.Approver
	}
	if !flag() {
		t.Fatal("the agent's login wrote no approver")
	}
	srv, _ := nkeys.CreateServer()
	key, _ := nkeys.CreateUser()
	pub, _ := key.PublicKey()
	sig, _ := key.Sign([]byte("nonce-console"))
	now := time.Now()
	req, err := c.svc.request(serverRequest(t, srv, func(q *natsjwt.AuthorizationRequestClaims) {
		q.ClientInformation.Nonce = "nonce-console"
		q.ConnectOptions.Nkey = pub
		q.ConnectOptions.SignedNonce = base64.RawURLEncoding.EncodeToString(sig)
		q.ConnectOptions.Token = c.consoleToken("ann", nil)
	}), now)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.svc.authorize(ctx5(t), req, now); err != nil {
		t.Fatal(err)
	}
	m, err := c.js.GetLastMsg("PRESENCE", "presence.account.mattermost."+id+".v1")
	var rec AccountRecord
	if err != nil || json.Unmarshal(m.Data, &rec) != nil || rec.TokenIat < now.Unix()-1 || !rec.Approver {
		t.Fatalf("after the console's login: %+v %v", rec, err)
	}
}
