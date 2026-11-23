package directory

import (
	"context"
	"encoding/hex"
	"errors"
	"log/slog"
	"strconv"
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
	// A UUID that starts with a letter has a Mattermost username's shape too.
	subLetter = "ab12cd34-0000-4111-8222-333344445555"
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
	long := "a.rather.long.username.of.30ch" // the server takes up to 64; the web form 22
	if len(long) != 30 {
		t.Fatal(len(long))
	}
	if got := anchor("sub-long", long); got != long {
		t.Fatalf("a 30-character name before the import: %q", got)
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
	first, err := s.kv.Get(ctx(t), importedKey)
	if err != nil {
		t.Fatal(err)
	}
	if err := s.MarkImported(ctx(t), time.Now().Add(time.Hour)); !errors.Is(err, ErrConflict) { // written once, kept
		t.Fatalf("a second mark: %v", err)
	}
	if again, err := s.kv.Get(ctx(t), importedKey); err != nil || string(again.Value()) != string(first.Value()) ||
		again.Revision() != first.Revision() {
		t.Fatalf("a second import changed the mark: %v", err)
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
	if _, err := a.Anchor(ctx(t), "_starts-odd", "x"); !errors.Is(err, ErrAnchorShape) {
		t.Fatalf("a sub that is no owner token: %v", err)
	}
}

// A username equal to another identity's sub cannot take that identity's
// place: one index key per value, and the identity whose value is held gets
// it with a suffix. No value is ever given twice.
func TestANameCannotSquatAnotherIdentitysSub(t *testing.T) {
	s, _ := open(t)
	a := Anchors{Store: s}
	squat, err := a.Anchor(ctx(t), subSquat, subLetter) // calls itself the victim's sub
	if err != nil || squat != subLetter {
		t.Fatalf("the squatter: %q %v", squat, err)
	}
	if err := s.MarkImported(ctx(t), time.Now()); err != nil {
		t.Fatal(err)
	}
	victim, err := a.Anchor(ctx(t), subLetter, "victim")
	if err != nil || victim != subLetter+".1" {
		t.Fatalf("the victim after the import: %q %v", victim, err)
	}
	if again, err := a.Anchor(ctx(t), subLetter, "victim"); err != nil || again != victim {
		t.Fatalf("the victim's anchor moved: %q %v", again, err)
	}
}

// An identity whose record was not written after its anchor was claimed
// (a crash between the two writes) finds its anchor again, after the import
// too: the reverse key anchorof.<sub> is read first.
func TestAnOrphanedAnchorIsFoundAgainAfterTheImport(t *testing.T) {
	s, _ := open(t)
	if _, err := s.claimAnchor(ctx(t), "sub-orphan", "orphan"); err != nil {
		t.Fatal(err)
	}
	if _, _, ok, _ := s.Identity(ctx(t), "sub-orphan"); ok {
		t.Fatal("the identity was written")
	}
	if err := s.MarkImported(ctx(t), time.Now()); err != nil {
		t.Fatal(err)
	}
	if got, err := (Anchors{Store: s}).Anchor(ctx(t), "sub-orphan", "orphan"); err != nil || got != "orphan" {
		t.Fatalf("the orphan after the import: %q %v", got, err)
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

// A long sub that starts with a letter still has room for a suffix: it is
// shortened, the value stays within the 64 an owner token may have, and the
// index keeps it unique.
func TestALongSubStillGetsASuffix(t *testing.T) {
	s, _ := open(t)
	a := Anchors{Store: s}
	long := "a" + strings.Repeat("b", 63) // 64, a username's shape
	if got, err := a.Anchor(ctx(t), "sub-squatter", long); err != nil || got != long {
		t.Fatalf("the squatter: %q %v", got, err)
	}
	if err := s.MarkImported(ctx(t), time.Now()); err != nil {
		t.Fatal(err)
	}
	got, err := a.Anchor(ctx(t), long, "victim")
	if err != nil || got != long[:62]+".1" || len(got) != 64 {
		t.Fatalf("the long sub: %q %v", got, err)
	}
}

// Nine names on the nine values lock an identity out: the issue is refused,
// never a shared anchor.
func TestNineSquattersAreRefusedNotShared(t *testing.T) {
	s, _ := open(t)
	a := Anchors{Store: s}
	if _, err := a.Anchor(ctx(t), "sub-0", subLetter); err != nil {
		t.Fatal(err)
	}
	for i := 1; i <= maxSuffix; i++ {
		if _, err := a.Anchor(ctx(t), "sub-"+strconv.Itoa(i), subLetter+"."+strconv.Itoa(i)); err != nil {
			t.Fatal(err)
		}
	}
	if err := s.MarkImported(ctx(t), time.Now()); err != nil {
		t.Fatal(err)
	}
	if got, err := a.Anchor(ctx(t), subLetter, "victim"); !errors.Is(err, ErrAnchorTaken) {
		t.Fatalf("the victim: %q %v", got, err)
	}
}

// The anchor recorded for a sub is checked before it is used: it must be an
// owner token, and its index must name that sub.
func TestARecordedAnchorIsCheckedBeforeUse(t *testing.T) {
	s, _ := open(t)
	if got, err := (Anchors{Store: s}).Anchor(ctx(t), "sub-alice", "alice"); err != nil || got != "alice" {
		t.Fatalf("alice: %q %v", got, err)
	}
	for name, value := range map[string]string{"another's anchor": "alice", "no owner token": "Alice*"} {
		if _, err := s.kv.Put(ctx(t), anchorOfPrefix+hex.EncodeToString([]byte("sub-mallory")), []byte(value)); err != nil {
			t.Fatal(err)
		}
		if got, err := s.claimAnchor(ctx(t), "sub-mallory", "mallory"); !errors.Is(err, ErrAnchorTaken) {
			t.Fatalf("%s: %q %v", name, got, err)
		}
	}
}

// A chat account has one identity: the index is per name, the same identity
// may set it again, another gets ErrConflict.
func TestAccountsAreOneIdentityPerName(t *testing.T) {
	s, _ := open(t)
	for _, sub := range []string{"sub-a", "sub-b"} {
		if _, err := s.Login(ctx(t), sub, "x-"+sub, ""); err != nil {
			t.Fatal(err)
		}
	}
	if err := s.SetAccount(ctx(t), "sub-a", "mattermost", "alice"); err != nil {
		t.Fatal(err)
	}
	if err := s.SetAccount(ctx(t), "sub-a", "mattermost", "alice"); err != nil {
		t.Fatalf("again: %v", err)
	}
	if err := s.SetAccount(ctx(t), "sub-b", "mattermost", "alice"); !errors.Is(err, ErrConflict) {
		t.Fatalf("another identity: %v", err)
	}
	if err := s.SetAccount(ctx(t), "sub-nobody", "mattermost", "zed"); !errors.Is(err, ErrNoIdentity) {
		t.Fatalf("no identity: %v", err)
	}
	id, ok, err := s.ByAccount(ctx(t), "mattermost", "alice")
	if err != nil || !ok || id.Sub != "sub-a" || id.Accounts["mattermost"] != "alice" {
		t.Fatalf("ByAccount: %+v %v %v", id, ok, err)
	}
	if _, ok, _ := s.ByAccount(ctx(t), "mattermost", "nobody"); ok {
		t.Fatal("an unknown name resolved")
	}
	// A rename frees the old name.
	if err := s.SetAccount(ctx(t), "sub-a", "mattermost", "alice2"); err != nil {
		t.Fatal(err)
	}
	if _, ok, _ := s.ByAccount(ctx(t), "mattermost", "alice"); ok {
		t.Fatal("the old name still resolves")
	}
	if err := s.SetAccount(ctx(t), "sub-b", "mattermost", "alice"); err != nil {
		t.Fatalf("the freed name: %v", err)
	}
}

func TestResolveTrusted(t *testing.T) {
	s, _ := open(t)
	if _, err := s.Login(ctx(t), "sub-a", "alice", ""); err != nil { // anchor: the sub
		t.Fatal(err)
	}
	if err := s.SetAccount(ctx(t), "sub-a", "mattermost", "alice"); err != nil {
		t.Fatal(err)
	}
	// Before the import the name is the anchor; a bad shape is dropped.
	got, cut, err := s.ResolveTrusted(ctx(t), []string{"alice", "bob", "Bad Name"}, false)
	if err != nil || !cut || len(got) != 2 || got[0] != (Trusted{"alice", "alice"}) || got[1] != (Trusted{"bob", "bob"}) {
		t.Fatalf("before: %+v cut=%v %v", got, cut, err)
	}
	// After it the identity's anchor; a name nobody has is dropped, not guessed.
	got, cut, err = s.ResolveTrusted(ctx(t), []string{"alice", "bob"}, true)
	if err != nil || !cut || len(got) != 1 || got[0] != (Trusted{"sub-a", "alice"}) {
		t.Fatalf("after: %+v cut=%v %v", got, cut, err)
	}
	got, cut, err = s.ResolveTrusted(ctx(t), []string{"alice"}, true)
	if err != nil || cut || len(got) != 1 {
		t.Fatalf("all resolved: %+v cut=%v %v", got, cut, err)
	}
	// More than MaxTrusted: the first ones, and cut.
	var many []string
	for i := range MaxTrusted + 3 {
		many = append(many, "u"+strconv.Itoa(i))
	}
	got, cut, err = s.ResolveTrusted(ctx(t), many, false)
	if err != nil || !cut || len(got) != MaxTrusted || got[MaxTrusted-1].Name != "u"+strconv.Itoa(MaxTrusted-1) {
		t.Fatalf("many: %d cut=%v %v", len(got), cut, err)
	}
	// A read error is an error, never a partial answer.
	s2, nc := open(t)
	nc.Close()
	if got, _, err := s2.ResolveTrusted(ctx(t), []string{"alice"}, true); err == nil || got != nil {
		t.Fatalf("a failed read: %+v %v", got, err)
	}
}

// RT-6.41 step 2, issue #59: the account index follows a verified login; the import's
// SetAccount never takes a name over.
func TestAnAccountIsClaimedOnlyAfterTheImportAndMovesWithItsHolder(t *testing.T) {
	s, _ := open(t)
	var logs strings.Builder
	a := Anchors{Store: s, Log: slog.New(slog.NewTextHandler(&logs, nil))}
	for _, sub := range []string{"sub-a", "sub-b"} {
		if _, err := s.Login(ctx(t), sub, "x", ""); err != nil {
			t.Fatal(err)
		}
	}
	// Before the import nothing is written.
	if err := a.Account(ctx(t), "sub-a", "alice"); err != nil {
		t.Fatal(err)
	}
	if _, _, _, ok, _ := s.Account(ctx(t), "mattermost", "alice"); ok {
		t.Fatal("an index entry was written before the import")
	}
	if err := s.MarkImported(ctx(t), time.Now()); err != nil {
		t.Fatal(err)
	}
	// A new person: entry set.
	if err := a.Account(ctx(t), "sub-a", "alice"); err != nil {
		t.Fatal(err)
	}
	if id, ok, err := s.ByAccount(ctx(t), "mattermost", "alice"); err != nil || !ok || id.Sub != "sub-a" {
		t.Fatalf("new person: %+v %v %v", id, ok, err)
	}
	if err := a.Account(ctx(t), "sub-a", "alice"); err != nil || logs.Len() != 0 {
		t.Fatalf("the same holder again: %v, log %q", err, logs.String())
	}
	// Rename: the new key points at sub, the old one is gone.
	if err := a.Account(ctx(t), "sub-a", "alice2"); err != nil {
		t.Fatal(err)
	}
	if id, ok, _ := s.ByAccount(ctx(t), "mattermost", "alice2"); !ok || id.Sub != "sub-a" {
		t.Fatalf("renamed: %+v %v", id, ok)
	}
	if _, _, _, ok, _ := s.Account(ctx(t), "mattermost", "alice"); ok {
		t.Fatal("the old key is still there")
	}
	// The name moves to another sub.
	if err := a.Account(ctx(t), "sub-b", "alice2"); err != nil {
		t.Fatal(err)
	}
	id, ok, err := s.ByAccount(ctx(t), "mattermost", "alice2")
	if err != nil || !ok || id.Sub != "sub-b" || id.MovedFrom["mattermost"] != "sub-a" {
		t.Fatalf("moved: %+v %v %v", id, ok, err)
	}
	old, _, _, _ := s.Identity(ctx(t), "sub-a")
	if old.Accounts["mattermost"] != "" {
		t.Fatalf("the old holder keeps the name: %+v", old)
	}
	if sub, _, from, ok, _ := s.Account(ctx(t), "mattermost", "alice2"); !ok || sub != "sub-b" || from != "sub-a" {
		t.Fatalf("Account: %q %q %v", sub, from, ok)
	}
	for _, w := range []string{"level=WARN", "chat account name moved", "name=alice2", "from=sub-a", "to=sub-b"} {
		if !strings.Contains(logs.String(), w) {
			t.Errorf("log lacks %q: %s", w, logs.String())
		}
	}
	// The import's SetAccount never takes a name over.
	if err := s.SetAccount(ctx(t), "sub-a", "mattermost", "alice2"); !errors.Is(err, ErrConflict) {
		t.Fatalf("SetAccount over a held name: %v", err)
	}
	if id, ok, _ := s.ByAccount(ctx(t), "mattermost", "alice2"); !ok || id.Sub != "sub-b" {
		t.Fatalf("SetAccount took it over: %+v", id)
	}
	if _, err := s.ClaimAccount(ctx(t), "sub-nobody", "mattermost", "zed"); !errors.Is(err, ErrNoIdentity) {
		t.Fatalf("no identity: %v", err)
	}
	if _, _, _, ok, _ := s.Account(ctx(t), "mattermost", "zed"); ok {
		t.Fatal("a name was written for an identity that does not exist")
	}
}

func TestAHalfWrittenMoveResolvesToNotFound(t *testing.T) {
	s, _ := open(t)
	for _, sub := range []string{"sub-a", "sub-b"} {
		if _, err := s.Login(ctx(t), sub, "x", ""); err != nil {
			t.Fatal(err)
		}
	}
	if err := s.SetAccount(ctx(t), "sub-a", "mattermost", "alice"); err != nil {
		t.Fatal(err)
	}
	// The key moved, the records did not follow.
	key, _ := accountKey("mattermost", "alice")
	if _, err := s.kv.Put(ctx(t), key, []byte("sub-b")); err != nil {
		t.Fatal(err)
	}
	if id, ok, _ := s.ByAccount(ctx(t), "mattermost", "alice"); ok {
		t.Fatalf("a half-written move resolved to %+v", id)
	}
}

func TestTheAdminsAccountWritesAreOverExactRevisions(t *testing.T) {
	s, _ := open(t)
	for _, sub := range []string{"sub-a", "sub-b"} {
		if _, err := s.Login(ctx(t), sub, "x", ""); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := s.PutAccount(ctx(t), "sub-a", "mattermost", "alice", 0); err != nil {
		t.Fatal(err)
	}
	if _, err := s.PutAccount(ctx(t), "sub-b", "mattermost", "alice", 0); !errors.Is(err, ErrConflict) {
		t.Fatalf("create over an existing key: %v", err)
	}
	if _, err := s.PutAccount(ctx(t), "sub-b", "mattermost", "nobody", 5); !errors.Is(err, ErrNotFound) {
		t.Fatalf("replace a missing key: %v", err)
	}
	if _, err := s.PutAccount(ctx(t), "sub-nobody", "mattermost", "zed", 0); !errors.Is(err, ErrNoIdentity) {
		t.Fatalf("unknown identity: %v", err)
	}
	_, rev, _, _, _ := s.Account(ctx(t), "mattermost", "alice")
	if _, err := s.PutAccount(ctx(t), "sub-b", "mattermost", "alice", rev+1); !errors.Is(err, ErrConflict) {
		t.Fatalf("replace over a wrong revision: %v", err)
	}
	mv, err := s.PutAccount(ctx(t), "sub-b", "mattermost", "alice", rev)
	from := mv.From
	if err != nil || from != "sub-a" {
		t.Fatalf("replace: %q %v", from, err)
	}
	if id, ok, _ := s.ByAccount(ctx(t), "mattermost", "alice"); !ok || id.Sub != "sub-b" {
		t.Fatalf("after replace: %+v", id)
	}
	old, _, _, _ := s.Identity(ctx(t), "sub-a")
	if old.Accounts["mattermost"] != "" {
		t.Fatalf("old holder keeps the name: %+v", old)
	}
	_, rev, _, _, _ = s.Account(ctx(t), "mattermost", "alice")
	if _, err := s.DropAccount(ctx(t), "mattermost", "alice", rev+1); !errors.Is(err, ErrConflict) {
		t.Fatalf("drop over a wrong revision: %v", err)
	}
	if _, err := s.DropAccount(ctx(t), "mattermost", "none", 1); !errors.Is(err, ErrNotFound) {
		t.Fatalf("drop a missing key: %v", err)
	}
	if sub, err := s.DropAccount(ctx(t), "mattermost", "alice", rev); err != nil || sub != "sub-b" {
		t.Fatalf("drop: %q %v", sub, err)
	}
	b, _, _, _ := s.Identity(ctx(t), "sub-b")
	if _, _, _, ok, _ := s.Account(ctx(t), "mattermost", "alice"); ok || b.Accounts["mattermost"] != "" {
		t.Fatalf("after drop: key %v, identity %+v", ok, b)
	}
}

// RT-6.41 step 2, issue #59: a move that stops after its key moved is
// reported at once and finished, with the old holder released, by the next
// write of that name; at no stop does the name resolve to the old holder
// once its key has moved.
func TestAnInterruptedMoveIsReportedAndFinished(t *testing.T) {
	for _, stop := range []string{"key moved", "holder written"} {
		t.Run(stop, func(t *testing.T) {
			s, _ := open(t)
			var logs strings.Builder
			a := Anchors{Store: s, Log: slog.New(slog.NewTextHandler(&logs, nil))}
			for _, sub := range []string{"sub-a", "sub-b"} {
				if _, err := s.Login(ctx(t), sub, "x", ""); err != nil {
					t.Fatal(err)
				}
			}
			if err := s.MarkImported(ctx(t), time.Now()); err != nil {
				t.Fatal(err)
			}
			if err := a.Account(ctx(t), "sub-a", "alice"); err != nil {
				t.Fatal(err)
			}
			boom := errors.New("stopped")
			accountStep = func(at string) error {
				if at == stop {
					return boom
				}
				return nil
			}
			t.Cleanup(func() { accountStep = func(string) error { return nil } })
			if err := a.Account(ctx(t), "sub-b", "alice"); !errors.Is(err, boom) {
				t.Fatalf("the stopped move: %v", err)
			}
			if !strings.Contains(logs.String(), "from=sub-a") || !strings.Contains(logs.String(), "complete=false") {
				t.Fatalf("a half move is not reported: %s", logs.String())
			}
			if id, ok, _ := s.ByAccount(ctx(t), "mattermost", "alice"); ok && id.Sub != "sub-b" {
				t.Fatalf("after the key moved the name resolves to %+v", id)
			}
			accountStep = func(string) error { return nil }
			logs.Reset()
			if err := a.Account(ctx(t), "sub-b", "alice"); err != nil {
				t.Fatal(err)
			}
			if !strings.Contains(logs.String(), "from=sub-a") || !strings.Contains(logs.String(), "complete=true") {
				t.Fatalf("the finished move is not reported: %s", logs.String())
			}
			id, ok, _ := s.ByAccount(ctx(t), "mattermost", "alice")
			if !ok || id.Sub != "sub-b" || id.MovedFrom["mattermost"] != "sub-a" {
				t.Fatalf("finished: %+v %v", id, ok)
			}
			if old, _, _, _ := s.Identity(ctx(t), "sub-a"); old.Accounts["mattermost"] != "" {
				t.Fatalf("the old holder keeps the name: %+v", old)
			}
			if _, err := s.kv.Get(ctx(t), moveKey("mattermost", "alice")); !errors.Is(err, jetstream.ErrKeyNotFound) {
				t.Fatalf("the mark stays: %v", err)
			}
			logs.Reset()
			if err := a.Account(ctx(t), "sub-b", "alice"); err != nil || logs.Len() != 0 {
				t.Fatalf("a settled name is reported again: %v %s", err, logs.String())
			}
		})
	}
}

// The key moves only over the revision read: a holder that changed
// meanwhile is the one the name moves from.
func TestAMoveIsOverTheKeysRevision(t *testing.T) {
	s, _ := open(t)
	for _, sub := range []string{"sub-a", "sub-b", "sub-c"} {
		if _, err := s.Login(ctx(t), sub, "x", ""); err != nil {
			t.Fatal(err)
		}
	}
	if err := s.SetAccount(ctx(t), "sub-a", "mattermost", "alice"); err != nil {
		t.Fatal(err)
	}
	once := true
	accountStep = func(at string) error {
		if at == "before key" && once {
			once = false
			if _, err := s.ClaimAccount(ctx(t), "sub-c", "mattermost", "alice"); err != nil {
				t.Error(err)
			}
		}
		return nil
	}
	t.Cleanup(func() { accountStep = func(string) error { return nil } })
	mv, err := s.ClaimAccount(ctx(t), "sub-b", "mattermost", "alice")
	from := mv.From
	if err != nil || from != "sub-c" {
		t.Fatalf("moved from %q (%v), want sub-c", from, err)
	}
	if c, _, _, _ := s.Identity(ctx(t), "sub-c"); c.Accounts["mattermost"] != "" {
		t.Fatalf("the holder of meanwhile keeps the name: %+v", c)
	}
}

// Releasing a name leaves a record alone that lists another one; a rename
// clears the note of where the old name came from.
func TestReleaseChecksTheNameAndARenameClearsMovedFrom(t *testing.T) {
	s, _ := open(t)
	for _, sub := range []string{"sub-a", "sub-b"} {
		if _, err := s.Login(ctx(t), sub, "x", ""); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := s.ClaimAccount(ctx(t), "sub-a", "mattermost", "anna"); err != nil {
		t.Fatal(err)
	}
	if err := s.releaseAccount(ctx(t), "sub-a", "mattermost", "alice"); err != nil {
		t.Fatal(err)
	}
	if a, _, _, _ := s.Identity(ctx(t), "sub-a"); a.Accounts["mattermost"] != "anna" {
		t.Fatalf("a release of another name took %+v", a)
	}
	if _, err := s.ClaimAccount(ctx(t), "sub-b", "mattermost", "anna"); err != nil {
		t.Fatal(err)
	}
	if _, err := s.ClaimAccount(ctx(t), "sub-b", "mattermost", "bea"); err != nil {
		t.Fatal(err)
	}
	if b, _, _, _ := s.Identity(ctx(t), "sub-b"); b.MovedFrom["mattermost"] != "" {
		t.Fatalf("a rename kept moved_from: %+v", b)
	}
	if _, _, from, ok, _ := s.Account(ctx(t), "mattermost", "bea"); !ok || from != "" {
		t.Fatalf("bea: %q %v", from, ok)
	}
}

// stopAt makes the move stop at one step until the returned func is called.
func stopAt(t *testing.T, step string) (restart func()) {
	restore := SetAccountStepForTest(func(at string) error {
		if at == step {
			return errors.New("stopped at " + at)
		}
		return nil
	})
	t.Cleanup(restore)
	return restore
}

func imported3(t *testing.T) (*Store, Anchors, *strings.Builder) {
	s, _ := open(t)
	var logs strings.Builder
	a := Anchors{Store: s, Log: slog.New(slog.NewTextHandler(&logs, nil))}
	for _, sub := range []string{"sub-a", "sub-b", "sub-c"} {
		if _, err := s.Login(ctx(t), sub, "x", ""); err != nil {
			t.Fatal(err)
		}
	}
	if err := s.MarkImported(ctx(t), time.Now()); err != nil {
		t.Fatal(err)
	}
	if err := a.Account(ctx(t), "sub-a", "alice"); err != nil {
		t.Fatal(err)
	}
	return s, a, &logs
}

func listsAlice(t *testing.T, s *Store, sub string) bool {
	id, _, _, err := s.Identity(ctx(t), sub)
	if err != nil {
		t.Fatal(err)
	}
	return id.Accounts["mattermost"] == "alice"
}

// A move a→b stops; c then takes the name: the mark of the stopped move is
// not lost, a loses the name and that is logged.
func TestAStoppedMoveIsNotLostToTheNextOne(t *testing.T) {
	s, a, logs := imported3(t)
	restart := stopAt(t, "key moved")
	if err := a.Account(ctx(t), "sub-b", "alice"); err == nil {
		t.Fatal("the move did not stop")
	}
	restart()
	if err := a.Account(ctx(t), "sub-c", "alice"); err != nil {
		t.Fatal(err)
	}
	if listsAlice(t, s, "sub-a") || listsAlice(t, s, "sub-b") {
		t.Fatal("an earlier holder keeps the name")
	}
	if id, ok, _ := s.ByAccount(ctx(t), "mattermost", "alice"); !ok || id.Sub != "sub-c" {
		t.Fatalf("after the takeover: %+v %v", id, ok)
	}
	for _, w := range []string{"unfinished move", "released=sub-a", "from=sub-b", "to=sub-c", "complete=true"} {
		if !strings.Contains(logs.String(), w) {
			t.Errorf("log lacks %q: %s", w, logs.String())
		}
	}
	if _, err := s.kv.Get(ctx(t), moveKey("mattermost", "alice")); !errors.Is(err, jetstream.ErrKeyNotFound) {
		t.Fatalf("the mark stays: %v", err)
	}
}

// A mark whose key never moved is stale once its holder has left the name:
// the next holder of the freed name invents no move.
func TestAStaleMarkInventsNoMove(t *testing.T) {
	s, a, logs := imported3(t)
	restart := stopAt(t, "before key")
	if err := a.Account(ctx(t), "sub-b", "alice"); err == nil {
		t.Fatal("the move did not stop")
	}
	restart()
	if err := a.Account(ctx(t), "sub-a", "anna"); err != nil { // a renames, alice is free
		t.Fatal(err)
	}
	logs.Reset()
	if err := a.Account(ctx(t), "sub-c", "alice"); err != nil {
		t.Fatal(err)
	}
	if logs.Len() != 0 {
		t.Fatalf("a move was invented: %s", logs.String())
	}
	if _, _, from, ok, _ := s.Account(ctx(t), "mattermost", "alice"); !ok || from != "" {
		t.Fatalf("moved_from %q (%v), want none", from, ok)
	}
	if id, _, _, _ := s.Identity(ctx(t), "sub-a"); id.Accounts["mattermost"] != "anna" {
		t.Fatalf("a lost its new name: %+v", id)
	}
	if _, err := s.kv.Get(ctx(t), moveKey("mattermost", "alice")); !errors.Is(err, jetstream.ErrKeyNotFound) {
		t.Fatalf("the stale mark stays: %v", err)
	}
}

// An admin's DELETE while a move is unfinished: no one keeps the name.
func TestADropReleasesAnUnfinishedMove(t *testing.T) {
	s, a, _ := imported3(t)
	restart := stopAt(t, "key moved")
	if err := a.Account(ctx(t), "sub-b", "alice"); err == nil {
		t.Fatal("the move did not stop")
	}
	restart()
	_, rev, _, ok, err := s.Account(ctx(t), "mattermost", "alice")
	if err != nil || !ok {
		t.Fatal(ok, err)
	}
	if _, err := s.DropAccount(ctx(t), "mattermost", "alice", rev); err != nil {
		t.Fatal(err)
	}
	if listsAlice(t, s, "sub-a") || listsAlice(t, s, "sub-b") {
		t.Fatal("a record keeps the dropped name")
	}
	if _, err := s.kv.Get(ctx(t), moveKey("mattermost", "alice")); !errors.Is(err, jetstream.ErrKeyNotFound) {
		t.Fatalf("the mark stays: %v", err)
	}
}

// An admin's PUT that stops after its key moved is finished, and reported,
// by the next login of the new holder.
func TestAStoppedAdminPutIsFinishedByTheNextLogin(t *testing.T) {
	s, a, logs := imported3(t)
	_, rev, _, _, _ := s.Account(ctx(t), "mattermost", "alice")
	restart := stopAt(t, "key moved")
	mv, err := s.PutAccount(ctx(t), "sub-b", "mattermost", "alice", rev)
	if err == nil || mv.From != "sub-a" {
		t.Fatalf("the stopped PUT: %+v %v", mv, err)
	}
	restart()
	logs.Reset()
	if err := a.Account(ctx(t), "sub-b", "alice"); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(logs.String(), "from=sub-a") || listsAlice(t, s, "sub-a") {
		t.Fatalf("not finished: a lists alice %v, log %s", listsAlice(t, s, "sub-a"), logs.String())
	}
}
