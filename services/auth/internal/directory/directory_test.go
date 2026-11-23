package directory

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

func ctx(t *testing.T) context.Context {
	t.Helper()
	c, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	return c
}

// open starts a NATS server with JetStream and returns the store, and the
// connection so a test can cut it.
func open(t *testing.T) (*Store, *nats.Conn) {
	t.Helper()
	srv, err := server.NewServer(&server.Options{Host: "127.0.0.1", Port: -1, JetStream: true,
		StoreDir: t.TempDir(), NoLog: true, NoSigs: true})
	if err != nil {
		t.Fatal(err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(5 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(srv.Shutdown)
	nc, err := nats.Connect(srv.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	s, err := Open(ctx(t), js, "", false)
	if err != nil {
		t.Fatal(err)
	}
	again, err := Open(ctx(t), js, "", false) // an existing, safe bucket is taken
	if err != nil || again == nil {
		t.Fatalf("open again: %v", err)
	}
	return s, nc
}

// byName is the Mattermost lookup the directory replaces.
type byName map[string][]string

func (b byName) Memberships(_ context.Context, user string, chs []string) (map[string]bool, error) {
	out := map[string]bool{}
	for _, c := range chs {
		for _, m := range b[user] {
			out[c] = out[c] || m == c
		}
	}
	return out, nil
}

func TestMembershipsArePublicOrMemberAndUnknownIsNo(t *testing.T) {
	s, _ := open(t)
	for _, c := range []Channel{
		{Name: "home", Owner: "sub-alice", Members: []string{"sub-alice", "sub-bob"}},
		{Name: "town", Owner: "sub-alice", Public: true},
		{Name: "secret", Owner: "sub-carol", Members: []string{"sub-carol"}},
	} {
		if _, err := s.PutChannel(ctx(t), c, 0); err != nil {
			t.Fatal(err)
		}
	}
	got, err := s.Memberships(ctx(t), "sub-bob", []string{"home", "town", "secret", "missing", "bad.name", "*"})
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]bool{"home": true, "town": true, "secret": false, "missing": false, "bad.name": false, "*": false}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("%s: %v, want %v", k, got[k], v)
		}
	}
	if _, err := s.Memberships(ctx(t), "sub.*", []string{"home"}); !errors.Is(err, ErrBadKey) {
		t.Fatalf("a sub with a wildcard: %v", err)
	}
}

func TestAReadThatFailsIsUnknownNotNo(t *testing.T) {
	s, nc := open(t)
	nc.Close()
	if got, err := s.Memberships(ctx(t), "sub-alice", []string{"home"}); err == nil {
		t.Fatalf("a closed connection answered %v", got)
	}
	ch := Channels{Store: s, Fallback: byName{"alice": {"home"}}}
	if got, err := ch.MembershipsFor(ctx(t), "alice", "sub-alice", []string{"home"}); err == nil {
		t.Fatalf("the switch answered %v without the directory", got)
	}
}

func TestAChannelIsWrittenOnlyOverTheRevisionRead(t *testing.T) {
	s, _ := open(t)
	rev, err := s.PutChannel(ctx(t), Channel{Name: "home", Owner: "sub-alice"}, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.PutChannel(ctx(t), Channel{Name: "home", Owner: "sub-mallory"}, 0); !errors.Is(err, ErrConflict) {
		t.Fatalf("a second create: %v", err)
	}
	next, err := s.PutChannel(ctx(t), Channel{Name: "home", Owner: "sub-alice", Public: true}, rev)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.PutChannel(ctx(t), Channel{Name: "home", Owner: "sub-mallory"}, rev); !errors.Is(err, ErrConflict) {
		t.Fatalf("a write over an old revision: %v", err)
	}
	c, got, ok, err := s.Channel(ctx(t), "home")
	if err != nil || !ok || got != next || !c.Public || c.Owner != "sub-alice" {
		t.Fatalf("%+v rev %d (want %d) %v %v", c, got, next, ok, err)
	}
}

// Keycloak subs are UUIDs.
const (
	subNew   = "0b6f3c1e-1111-4222-8333-444455556666"
	subOther = "7D4C0A2B-AAAA-4BBB-8CCC-DDDDEEEEFFFF"
	subSquat = "9e8d7c6b-5a49-4382-9716-151413121110"
)

// Decisions 7 and 9: an anchor is given once and never changed; before the
// import it is today's name, after it a new identity's sub; no anchor twice.
func TestAnchorsAreFixedAndNeverShared(t *testing.T) {
	s, _ := open(t)
	a := Anchors{Store: s}
	anchor := func(sub, display string) string {
		t.Helper()
		got, err := a.Anchor(ctx(t), sub, display)
		if err != nil {
			t.Fatal(err)
		}
		return got
	}
	if got := anchor("sub-alice", "alice"); got != "alice" {
		t.Fatalf("before the import: %q", got)
	}
	if got := anchor("sub-dot", "first.last"); got != "first.last" {
		t.Fatalf("a name with a dot: %q (the callout hashes it as it is)", got)
	}
	for _, odd := range []string{"trailing.", "two..dots"} { // valid usernames, invalid as raw KV keys
		if got := anchor("sub-"+strings.ReplaceAll(odd, ".", "x"), odd); got != odd {
			t.Fatalf("%q: %q", odd, got)
		}
	}
	// A rename in Keycloak changes nothing but the name shown.
	if got := anchor("sub-alice", "alice.renamed"); got != "alice" {
		t.Fatalf("a rename in Keycloak changed the anchor: %q", got)
	}
	if id, _, _, _ := s.Identity(ctx(t), "sub-alice"); id.Display != "alice.renamed" {
		t.Fatalf("the display name did not follow: %+v", id)
	}
	// A new person now called alice (decided, ADR-016): the name anchor is
	// held, so they get their own sub, and keep it.
	if got := anchor(subOther, "alice"); got != strings.ToLower(subOther) {
		t.Fatalf("the new alice: %q", got)
	}
	if err := s.MarkImported(ctx(t), time.Now()); err != nil {
		t.Fatal(err)
	}
	if err := s.MarkImported(ctx(t), time.Now().Add(time.Hour)); err != nil { // written once, kept
		t.Fatal(err)
	}
	if got := anchor("sub-alice", "alice"); got != "alice" {
		t.Fatalf("an identity known before the import: %q", got)
	}
	if got := anchor(subNew, "newbie"); got != subNew {
		t.Fatalf("a new identity after the import: %q", got)
	}
	if _, err := a.Anchor(ctx(t), "sub.>", "x"); !errors.Is(err, ErrBadKey) {
		t.Fatalf("a sub with a wildcard: %v", err)
	}
	if _, err := a.Anchor(ctx(t), "shortsub", "x"); !errors.Is(err, ErrAnchorShape) {
		t.Fatalf("a sub shaped like a username: %v", err)
	}
}

// A username equal to another identity's sub cannot take that identity's
// place before the import: names and subs never share an anchor.
func TestANameCannotSquatAnotherIdentitysSub(t *testing.T) {
	s, _ := open(t)
	a := Anchors{Store: s}
	got, err := a.Anchor(ctx(t), subSquat, subNew) // calls itself the victim's sub
	if err != nil || got != subSquat {
		t.Fatalf("the squatter: %q %v", got, err)
	}
	if err := s.MarkImported(ctx(t), time.Now()); err != nil {
		t.Fatal(err)
	}
	got, err = a.Anchor(ctx(t), subNew, "victim")
	if err != nil || got != subNew {
		t.Fatalf("the victim after the import: %q %v", got, err)
	}
}

// The import's mark is expected (AUTH_DIRECTORY_IMPORTED): a lost bucket is
// never made anew, and a missing mark refuses; never a fall back to the chat.
func TestALostBucketAfterTheImportRefusesEverything(t *testing.T) {
	s, nc := open(t)
	js, _ := jetstream.New(nc)
	if err := s.MarkImported(ctx(t), time.Now()); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(ctx(t), js, "", true); err != nil {
		t.Fatalf("with its mark: %v", err)
	}
	if err := js.DeleteKeyValue(ctx(t), DefaultBucket); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(ctx(t), js, "", true); !errors.Is(err, ErrImportMissing) {
		t.Fatalf("a lost bucket: %v", err)
	}
	// Made anew by someone else, without the mark: the store refuses.
	fresh, err := Open(ctx(t), js, "", false)
	if err != nil {
		t.Fatal(err)
	}
	strict, err := Open(ctx(t), js, "", true)
	if err != nil {
		t.Fatal(err)
	}
	_ = fresh
	ch := Channels{Store: strict, Fallback: byName{"alice": {"home"}}}
	if got, err := ch.MembershipsFor(ctx(t), "alice", "sub-alice", []string{"home"}); !errors.Is(err, ErrImportMissing) {
		t.Fatalf("membership without the mark: %v %v", got, err)
	}
	if got, err := (Anchors{Store: strict}).Anchor(ctx(t), "sub-alice", "alice"); !errors.Is(err, ErrImportMissing) {
		t.Fatalf("an anchor without the mark: %q %v", got, err)
	}
}

// An existing bucket is checked: one that republishes its records (to a
// subject an agent could read) is refused.
func TestAnUnsafeBucketIsRefused(t *testing.T) {
	_, nc := open(t)
	js, _ := jetstream.New(nc)
	cfg := Config("UNSAFE")
	cfg.RePublish = &jetstream.RePublish{Source: "$KV.UNSAFE.>", Destination: "bus.leak.>"}
	if _, err := js.CreateKeyValue(ctx(t), cfg); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(ctx(t), js, "UNSAFE", false); !errors.Is(err, ErrUnsafeBucket) {
		t.Fatalf("a republishing bucket: %v", err)
	}
}

// RT-6.39 step 3, decision 2: Mattermost answers until the import's last step; then only the
// directory, by sub.
func TestTheImportSwitchesMembershipToTheDirectory(t *testing.T) {
	s, _ := open(t)
	ch := Channels{Store: s, Fallback: byName{"alice": {"home"}}}
	if _, err := s.PutChannel(ctx(t), Channel{Name: "other", Owner: "sub-alice", Members: []string{"sub-alice"}}, 0); err != nil {
		t.Fatal(err)
	}
	got, err := ch.MembershipsFor(ctx(t), "alice", "sub-alice", []string{"home", "other"})
	if err != nil || !got["home"] || got["other"] {
		t.Fatalf("before the import (Mattermost): %v %v", got, err)
	}
	if err := s.MarkImported(ctx(t), time.Now()); err != nil {
		t.Fatal(err)
	}
	got, err = ch.MembershipsFor(ctx(t), "alice", "sub-alice", []string{"home", "other"})
	if err != nil || got["home"] || !got["other"] {
		t.Fatalf("after the import (the directory): %v %v", got, err)
	}
	if _, err := ch.Memberships(ctx(t), "alice", []string{"other"}); !errors.Is(err, ErrNoSub) {
		t.Fatalf("after the import a name alone: %v", err)
	}
}
