package inbound

// The scenario "People's posts onto NATS" (RT-6.42): a fake Mattermost
// (httptest, its REST answers scripted), an embedded nats-server with the
// operator's streams, buckets and consumers, and the bridge's inbound path
// fed with the WebSocket reader's typed events.

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mirror"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mm"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mmws"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/store"
)

const (
	botID   = "bbbbbbbbbbbbbbbbbbbbbbbbbb" // the bridge's own bot
	aliceID = "aaaaaaaaaaaaaaaaaaaaaaaaaa"
	bobID   = "cccccccccccccccccccccccccc"
	otherID = "dddddddddddddddddddddddddd" // another bot
	cid     = "hhhhhhhhhhhhhhhhhhhhhhhhhh" // the home's channel
	elsewID = "eeeeeeeeeeeeeeeeeeeeeeeeee" // a channel the bridge does not serve
)

func pid(n int) string { return strings.Repeat(string(rune('p'+n%10)), 25) + string(rune('0'+n%10)) }

// fakeMM answers the bridge's REST reads.
type fakeMM struct {
	mu    sync.Mutex
	posts []map[string]any // what /channels/<cid>/posts?since= returns
	since atomic.Int64
	fail  atomic.Bool // every answer a 500
	// failUser answers a 500 for that user's next reads (a count per id)
	failUser map[string]int
	srv      *httptest.Server
}

func newFakeMM(t *testing.T) *fakeMM {
	t.Helper()
	f := &fakeMM{failUser: map[string]int{}}
	users := map[string]map[string]any{
		botID:   {"id": botID, "username": "bridge", "is_bot": true},
		aliceID: {"id": aliceID, "username": "alice", "is_bot": false},
		bobID:   {"id": bobID, "username": "bob", "is_bot": false},
		otherID: {"id": otherID, "username": "ci-bot", "is_bot": true},
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v4/", func(w http.ResponseWriter, r *http.Request) {
		if f.fail.Load() {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		p := strings.TrimPrefix(r.URL.Path, "/api/v4")
		switch {
		case p == "/system/ping":
			_, _ = w.Write([]byte(`{"status":"OK"}`)) // net/http sets the Date header
		case p == "/users/me":
			_ = json.NewEncoder(w).Encode(users[botID])
		case strings.HasPrefix(p, "/users/"):
			f.mu.Lock()
			if n := f.failUser[strings.TrimPrefix(p, "/users/")]; n > 0 {
				f.failUser[strings.TrimPrefix(p, "/users/")] = n - 1
				f.mu.Unlock()
				w.WriteHeader(http.StatusBadGateway)
				return
			}
			f.mu.Unlock()
			u, ok := users[strings.TrimPrefix(p, "/users/")]
			if !ok {
				w.WriteHeader(http.StatusNotFound)
				return
			}
			_ = json.NewEncoder(w).Encode(u)
		case p == "/teams/name/team/channels/name/home":
			_ = json.NewEncoder(w).Encode(map[string]any{"id": cid})
		case p == "/channels/"+cid+"/posts":
			var since int64
			_ = json.Unmarshal([]byte(r.URL.Query().Get("since")), &since)
			f.since.Store(since)
			f.mu.Lock()
			defer f.mu.Unlock()
			order := []string{}
			byID := map[string]any{}
			for _, post := range f.posts {
				order = append(order, post["id"].(string))
				byID[post["id"].(string)] = post
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"order": order, "posts": byID})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	})
	f.srv = httptest.NewServer(mux)
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeMM) setPosts(posts ...map[string]any) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.posts = posts
}

// lab is a server with the operator's streams, buckets and account consumer.
type lab struct {
	nc   *nats.Conn
	js   jetstream.JetStream
	kv   *store.KV
	seen *store.Seen
	acc  *Accounts
	out  chan *nats.Msg // everything on bus.hu.>
	sig  chan *nats.Msg // everything on presence.bridge.>
}

func newLab(t *testing.T) *lab {
	t.Helper()
	srv, err := natsserver.NewServer(&natsserver.Options{Host: "127.0.0.1", Port: -1, JetStream: true,
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
	js, _ := jetstream.New(nc)
	ctx := context.Background()
	for _, c := range []jetstream.StreamConfig{
		{Name: "BUS", Subjects: []string{"bus.>"}, NoAck: true, Storage: jetstream.MemoryStorage},
		{Name: "PRESENCE", Subjects: []string{"presence.>"}, NoAck: true, MaxMsgsPerSubject: 1,
			Storage: jetstream.MemoryStorage},
	} {
		if _, err := js.CreateStream(ctx, c); err != nil {
			t.Fatal(err)
		}
	}
	if err := store.CreateBucket(ctx, js); err != nil {
		t.Fatal(err)
	}
	if err := store.CreateSeenBucket(ctx, js); err != nil {
		t.Fatal(err)
	}
	if _, err := js.CreateOrUpdateConsumer(ctx, "PRESENCE", jetstream.ConsumerConfig{
		Name: AccountsConsumer, Durable: AccountsConsumer, FilterSubject: "presence.account.mattermost.*.v1",
		DeliverSubject: "deliver.bridge.accounts", DeliverPolicy: jetstream.DeliverLastPerSubjectPolicy,
		AckPolicy: jetstream.AckExplicitPolicy, IdleHeartbeat: 100 * time.Millisecond,
	}); err != nil {
		t.Fatal(err)
	}
	l := &lab{nc: nc, js: js, out: make(chan *nats.Msg, 64), sig: make(chan *nats.Msg, 64)}
	if l.kv, err = store.Open(ctx, nc, "BUS_BRIDGE"); err != nil {
		t.Fatal(err)
	}
	if l.seen, err = store.OpenSeen(ctx, nc, "BUS_BRIDGE_SEEN"); err != nil {
		t.Fatal(err)
	}
	if _, err := nc.ChanSubscribe("bus.hu.>", l.out); err != nil {
		t.Fatal(err)
	}
	if _, err := nc.ChanSubscribe("presence.bridge.>", l.sig); err != nil {
		t.Fatal(err)
	}
	l.acc = &Accounts{Log: quiet(), Deliver: "deliver.bridge.accounts", Stream: "PRESENCE",
		Ready: make(chan struct{})}
	actx, cancel := context.WithCancel(ctx)
	t.Cleanup(cancel)
	go func() { _ = l.acc.Run(actx, nc) }()
	<-l.acc.Ready
	// the idle heartbeat says the (empty) records are read
	deadline := time.Now().Add(3 * time.Second)
	for !l.acc.CaughtUp() {
		if time.Now().After(deadline) {
			t.Fatal("the account records were never read")
		}
		time.Sleep(10 * time.Millisecond)
	}
	return l
}

// record publishes a login service record and waits until the bridge read it.
func (l *lab) record(t *testing.T, uid, body string, want string) {
	t.Helper()
	if err := l.nc.Publish("presence.account.mattermost."+uid+".v1", []byte(body)); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(3 * time.Second)
	for anchorOf(t, l.acc, uid) != want {
		if time.Now().After(deadline) {
			t.Fatalf("anchor of %s: %q, want %q", uid, anchorOf(t, l.acc, uid), want)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func anchorOf(t *testing.T, a *Accounts, uid string) string {
	t.Helper()
	t.Helper()
	return a.Anchor(uid)
}

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// rig is the inbound path with hand-fed events and a settable connection.
type rig struct {
	*lab
	mm     *fakeMM
	events chan mmws.Event
	up     atomic.Uint64
	b      *Bridge
}

func newRig(t *testing.T, seen Seen) *rig {
	t.Helper()
	l := newLab(t)
	r := &rig{lab: l, mm: newFakeMM(t), events: make(chan mmws.Event)}
	if seen == nil {
		seen = l.seen
	}
	r.b = &Bridge{Homes: []string{"home"}, Team: "team", BusPrefix: "bus", Log: quiet(),
		MM: mm.New(r.mm.srv.URL, "tok"), NATS: Confirmed{NC: l.nc}, Store: l.kv, Seen: seen, Anchors: l.acc,
		Events: r.events, Up: r.up.Load, SignalEvery: 50 * time.Millisecond, Retry: 50 * time.Millisecond}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { _ = r.b.Run(ctx); close(done) }()
	t.Cleanup(func() { cancel(); <-done })
	return r
}

// connect is the reader's marker of connection n.
func (r *rig) connect(n uint64) {
	r.up.Store(n)
	r.events <- &mmws.Reconnected{Conn: n}
}

func (r *rig) send(ev mmws.Event) { r.events <- ev }

// next is the next people's event on NATS, or a failure.
func (r *rig) next(t *testing.T) (string, map[string]any) {
	t.Helper()
	select {
	case m := <-r.out:
		var body map[string]any
		if err := json.Unmarshal(m.Data, &body); err != nil {
			t.Fatalf("%s: %v", m.Data, err)
		}
		return m.Subject, body
	case <-time.After(3 * time.Second):
		t.Fatal("nothing carried onto NATS")
	}
	return "", nil
}

// none says nothing more was carried. The events channel is unbuffered, so
// a sync event (a post of another channel) settles what came before.
func (r *rig) none(t *testing.T) {
	t.Helper()
	r.send(&mmws.Posted{PostID: pid(9), UserID: aliceID, ChannelID: elsewID})
	_ = r.nc.Flush()
	select {
	case m := <-r.out:
		t.Fatalf("carried %s: %s", m.Subject, m.Data)
	case <-time.After(150 * time.Millisecond):
	}
}

func now() int64 { return time.Now().UnixMilli() }

func alicePost(id string, at int64) *mmws.Posted {
	return &mmws.Posted{PostID: id, UserID: aliceID, ChannelID: cid, Message: "go ahead",
		CreateAt: at, UpdateAt: at, Props: map[string]any{"x_task": "t1", "from_bot": "true"}, FileCount: 2}
}

// RT-6.42 steps 2 to 5: a person's post goes out once, on the home's subject
// with the person's anchor last ("." as "~"), with the v1 body and no props.
func TestAPersonsPostGoesOutOnceUnderTheAnchor(t *testing.T) {
	r := newRig(t, nil)
	r.record(t, aliceID, `{"v":1,"account":"`+aliceID+`","anchor":"alice.example","name":"alice",`+
		`"approver":true,"token_iat":1,"iat":1,"exp":2}`, "alice.example")
	r.connect(1)
	// an agent's post the bridge mirrored at stream sequence 41 is the root
	if err := r.kv.Save(context.Background(), "home", 41, mirror.Post{ID: pid(0)}); err != nil {
		t.Fatal(err)
	}
	p := alicePost(pid(1), now())
	p.RootID = pid(0)
	r.send(p)
	subject, body := r.next(t)
	if subject != "bus.hu.home.alice~example" {
		t.Fatalf("subject %s", subject)
	}
	want := map[string]any{"v": 1.0, "kind": "post", "text": "go ahead", "name": "alice", "mm": pid(1),
		"mm_root": pid(0), "root": 41.0, "files": 2.0}
	if !equal(body, want) {
		t.Fatalf("body %v, want %v", body, want)
	}
	r.send(p) // the same post again (a live event and the catch-up's read)
	r.none(t)
}

func equal(a, b map[string]any) bool {
	ra, _ := json.Marshal(a)
	rb, _ := json.Marshal(b)
	return string(ra) == string(rb)
}

// RT-6.42 step 2: what is no person's own post is dropped without a trace on
// NATS — a bot's (Mattermost's is_bot), a webhook's, one with
// override_username, a system post, the bridge's own, a deleted or edited one.
func TestWhatIsNoPersonsOwnPostIsDropped(t *testing.T) {
	r := newRig(t, nil)
	r.connect(1)
	at := now()
	for _, p := range []*mmws.Posted{
		{PostID: pid(1), UserID: otherID, ChannelID: cid, Message: "bot", CreateAt: at},
		{PostID: pid(2), UserID: aliceID, ChannelID: cid, Message: "hook", CreateAt: at,
			Props: map[string]any{"from_webhook": "true"}},
		{PostID: pid(3), UserID: aliceID, ChannelID: cid, Message: "as someone", CreateAt: at,
			Props: map[string]any{"override_username": "boss"}},
		{PostID: pid(4), UserID: aliceID, ChannelID: cid, Type: "system_join_channel", CreateAt: at},
		{PostID: pid(5), UserID: botID, ChannelID: cid, Message: "mirror", CreateAt: at},
		{PostID: pid(6), UserID: aliceID, ChannelID: cid, Message: "gone", CreateAt: at, DeleteAt: at},
		{PostID: pid(7), UserID: aliceID, ChannelID: cid, Message: "changed", CreateAt: at, EditAt: at},
		{PostID: pid(8), UserID: aliceID, ChannelID: elsewID, Message: "elsewhere", CreateAt: at},
		{PostID: "not-an-id", UserID: aliceID, ChannelID: cid, Message: "x", CreateAt: at},
	} {
		r.send(p)
	}
	r.none(t)
	// a bot's reaction and the bridge's own are dropped too
	r.send(&mmws.Reaction{PostID: pid(1), UserID: otherID, ChannelID: cid, EmojiName: "+1", CreateAt: at, Added: true})
	r.send(&mmws.Reaction{PostID: pid(1), UserID: botID, ChannelID: cid, EmojiName: "+1", CreateAt: at, Added: true})
	r.none(t)
}

// RT-6.42 step 3 (points 3, 4, 10): without a record, with a record that
// does not decode, or with anchor null, the account is carried under "-".
func TestAnUnboundAccountIsCarriedUnderTheDash(t *testing.T) {
	r := newRig(t, nil)
	r.connect(1)
	r.send(alicePost(pid(1), now()))
	if subject, _ := r.next(t); subject != "bus.hu.home.-" {
		t.Fatalf("no record: %s", subject)
	}
	r.record(t, aliceID, `{"v":1,"account":"`+aliceID+`","anchor":"alice","name":"alice",`+
		`"approver":false,"token_iat":1,"iat":1,"exp":2}`, "alice")
	// a stray answer erases the record: unbound again, fails closed
	r.record(t, aliceID, `{"type":"io.nats.jetstream.api.v1.stream_msg_get_response"}`, "")
	r.send(alicePost(pid(2), now()))
	if subject, _ := r.next(t); subject != "bus.hu.home.-" {
		t.Fatalf("a record that does not decode: %s", subject)
	}
	r.record(t, aliceID, `{"v":1,"account":"`+aliceID+`","anchor":"alice","name":"alice",`+
		`"approver":false,"token_iat":1,"iat":1,"exp":2}`, "alice")
	r.record(t, aliceID, `{"v":1,"account":"`+aliceID+`","anchor":null,"name":"alice",`+
		`"approver":false,"token_iat":2,"iat":2,"exp":3}`, "")
	r.send(alicePost(pid(3), now()))
	if subject, _ := r.next(t); subject != "bus.hu.home.-" {
		t.Fatalf("anchor null: %s", subject)
	}
}

// RT-6.42 step 1: a reaction's key is its post, emoji, sender and kind, and
// the opposite kind clears it, so a 👍 taken back and given again counts.
func TestAReactionCountsOncePerKindAndAgainAfterItsRemoval(t *testing.T) {
	r := newRig(t, nil)
	r.connect(1)
	ctx := context.Background()
	if err := r.kv.Save(ctx, "home", 7, mirror.Post{ID: pid(1)}); err != nil {
		t.Fatal(err)
	}
	add := &mmws.Reaction{PostID: pid(1), UserID: bobID, ChannelID: cid, EmojiName: "+1", CreateAt: now(), Added: true}
	del := &mmws.Reaction{PostID: pid(1), UserID: bobID, ChannelID: cid, EmojiName: "+1", Added: false}
	r.send(add)
	_, body := r.next(t)
	if !equal(body, map[string]any{"v": 1.0, "kind": "reaction", "emoji": "+1", "name": "bob", "mm": pid(1), "ref": 7.0}) {
		t.Fatalf("reaction %v", body)
	}
	r.send(add)
	r.none(t)
	r.send(del)
	if _, body := r.next(t); body["kind"] != "unreaction" {
		t.Fatalf("removal %v", body)
	}
	r.send(del)
	r.none(t)
	r.send(add)
	if _, body := r.next(t); body["kind"] != "reaction" {
		t.Fatalf("given again %v", body)
	}
}

// RT-6.42 step 1 (point 11): after a reconnect the bridge reads the home's
// posts since its watermark minus a margin and carries what the socket
// missed: a new post, a reaction on an older post (an ask), a removal of a
// reaction it carried; never an older edited post, and nothing beyond ten
// minutes back. On the first start it carries nothing from before.
func TestTheCatchUpCarriesWhatTheSocketMissed(t *testing.T) {
	r := newRig(t, nil)
	t0 := now()
	r.mm.setPosts(map[string]any{"id": pid(1), "user_id": aliceID, "channel_id": cid, "message": "before",
		"create_at": t0 - 1000, "update_at": t0 - 1000})
	r.connect(1) // first start: no watermark, nothing from before
	r.none(t)
	wm, ok, err := r.kv.Watermark(context.Background(), "home")
	if !ok || err != nil || wm < t0-1000 || wm > now() {
		t.Fatalf("first start's watermark %d %v %v", wm, ok, err)
	}
	// a reaction carried live, later removed during the gap
	r.send(&mmws.Reaction{PostID: pid(2), UserID: bobID, ChannelID: cid, EmojiName: "eyes", CreateAt: wm + 1, Added: true})
	r.next(t)
	r.send(&mmws.Reaction{PostID: pid(7), UserID: bobID, ChannelID: cid, EmojiName: "eyes", CreateAt: wm + 1, Added: true})
	r.next(t)
	wm, _, _ = r.kv.Watermark(context.Background(), "home")
	r.up.Store(0) // the socket drops
	ask := pid(3) // an agent's ask, older than the watermark
	r.mm.setPosts(
		map[string]any{"id": pid(4), "user_id": aliceID, "channel_id": cid, "message": "old, edited",
			"create_at": wm - 60000, "update_at": wm + 100, "edit_at": wm + 100, "metadata": map[string]any{}},
		map[string]any{"id": ask, "user_id": otherID, "channel_id": cid, "message": "may I?",
			"create_at": wm - 120000, "update_at": wm + 200, "metadata": map[string]any{
				"reactions": []any{map[string]any{"user_id": aliceID, "post_id": ask, "emoji_name": "+1", "create_at": wm + 200}}}},
		map[string]any{"id": pid(2), "user_id": otherID, "channel_id": cid, "message": "watched",
			"create_at": wm - 1000, "update_at": wm + 300, "metadata": map[string]any{}},
		map[string]any{"id": pid(5), "user_id": aliceID, "channel_id": cid, "message": "during the gap",
			"create_at": wm + 400, "update_at": wm + 400, "metadata": map[string]any{}},
		// deleted since: its reactions are gone with it, which is no removal
		map[string]any{"id": pid(7), "user_id": otherID, "channel_id": cid, "message": "deleted",
			"create_at": wm - 2000, "update_at": wm + 500, "delete_at": wm + 500, "metadata": map[string]any{}},
	)
	r.connect(2)
	got := map[string]string{}
	for range 3 {
		_, body := r.next(t)
		got[body["mm"].(string)] = body["kind"].(string)
	}
	want := map[string]string{ask: "reaction", pid(2): "unreaction", pid(5): "post"}
	if !equal(toAny(got), toAny(want)) {
		t.Fatalf("caught up %v, want %v", got, want)
	}
	if since := r.mm.since.Load(); since != wm-DefaultMargin.Milliseconds() {
		t.Fatalf("read since %d, want the watermark %d minus the margin", since, wm)
	}
	r.none(t)
	// the same read again carries nothing twice
	r.connect(3)
	r.none(t)
	// a gap past ten minutes: what is older is not carried
	r.up.Store(0)
	time.Sleep(100 * time.Millisecond) // no signal tick moves the watermark now
	if err := r.kv.SetWatermark(context.Background(), "home", t0-30*60000); err != nil {
		t.Fatal(err)
	}
	r.mm.setPosts(map[string]any{"id": pid(6), "user_id": aliceID, "channel_id": cid, "message": "too old",
		"create_at": t0 - 20*60000, "update_at": t0 - 20*60000})
	r.connect(4)
	r.none(t) // settles the catch-up: the events channel is unbuffered
	if since := r.mm.since.Load(); since < t0-DefaultReach.Milliseconds()-1000 {
		t.Fatalf("read since %d: more than ten minutes back", since)
	}
}

func toAny(m map[string]string) map[string]any {
	out := map[string]any{}
	for k, v := range m {
		out[k] = v
	}
	return out
}

// RT-6.42 step 7 (point 12): the signal goes out while the socket the home
// caught up on is up, and not once it dropped; a refused bucket write stops
// the home and its signal, and the retry carries on once the bucket takes
// writes again.
func TestTheSignalFollowsTheSocketAndTheBuckets(t *testing.T) {
	broken := &flakySeen{}
	r := newRig(t, broken)
	broken.inner = r.seen
	r.connect(1)
	m := waitSignal(t, r.sig)
	var sig map[string]any
	if err := json.Unmarshal(m.Data, &sig); err != nil || m.Subject != "presence.bridge.home.v1" {
		t.Fatalf("%s %s", m.Subject, m.Data)
	}
	if sig["v"] != 1.0 || sig["home"] != "home" || sig["people"] != true ||
		sig["exp"].(float64)-sig["iat"].(float64) != DefaultSignalValid.Seconds() {
		t.Fatalf("signal %v", sig)
	}
	r.up.Store(0) // the socket dropped
	drain(r.sig)
	noSignal(t, r.sig)
	r.connect(2)
	waitSignal(t, r.sig)
	// the bucket refuses: the home stops, its signal with it
	broken.fail.Store(true)
	post := map[string]any{"id": pid(1), "user_id": aliceID, "channel_id": cid, "message": "go ahead",
		"create_at": now(), "update_at": now()}
	r.mm.setPosts(post)
	r.send(alicePost(pid(1), post["create_at"].(int64)))
	drain(r.sig)
	noSignal(t, r.sig)
	// and once it takes writes again, the retry's catch-up carries the post
	// the refusal held back, and the signal comes back
	broken.fail.Store(false)
	if _, body := r.next(t); body["mm"] != pid(1) {
		t.Fatalf("after the refusal %v", body)
	}
	waitSignal(t, r.sig)
}

type flakySeen struct {
	inner Seen
	fail  atomic.Bool
}

var errFull = errors.New("maximum bytes exceeded")

func (f *flakySeen) Post(ctx context.Context, p string) (bool, error) {
	if f.fail.Load() {
		return false, errFull
	}
	return f.inner.Post(ctx, p)
}
func (f *flakySeen) MarkPost(ctx context.Context, p string) error {
	if f.fail.Load() {
		return errFull
	}
	return f.inner.MarkPost(ctx, p)
}
func (f *flakySeen) Reactions(ctx context.Context, p string) (map[string]string, error) {
	if f.fail.Load() {
		return nil, errFull
	}
	return f.inner.Reactions(ctx, p)
}
func (f *flakySeen) SetReactions(ctx context.Context, p string, k map[string]string) error {
	if f.fail.Load() {
		return errFull
	}
	return f.inner.SetReactions(ctx, p, k)
}

func waitSignal(t *testing.T, ch chan *nats.Msg) *nats.Msg {
	t.Helper()
	select {
	case m := <-ch:
		return m
	case <-time.After(3 * time.Second):
		t.Fatal("no signal")
	}
	return nil
}

func noSignal(t *testing.T, ch chan *nats.Msg) {
	t.Helper()
	select {
	case m := <-ch:
		t.Fatalf("a signal while it must not: %s", m.Data)
	case <-time.After(300 * time.Millisecond):
	}
}

func drain(ch chan *nats.Msg) {
	time.Sleep(100 * time.Millisecond)
	for {
		select {
		case <-ch:
		default:
			return
		}
	}
}

// RT-6.42 step 3: the operator makes bridge_accounts afresh before every
// start, so the restarted bridge reads the last record of every account
// again; no home signals before the records are read.
func TestAfterARestartABoundPersonKeepsTheAnchor(t *testing.T) {
	r := newRig(t, nil)
	r.record(t, aliceID, `{"v":1,"account":"`+aliceID+`","anchor":"alice","name":"alice",`+
		`"approver":true,"token_iat":1,"iat":1,"exp":2}`, "alice")
	// the operator's step before the restart
	ctx := context.Background()
	if err := r.js.DeleteConsumer(ctx, "PRESENCE", AccountsConsumer); err != nil {
		t.Fatal(err)
	}
	again := &Accounts{Log: quiet(), Deliver: "deliver.bridge.accounts", Stream: "PRESENCE", Ready: make(chan struct{})}
	actx, cancel := context.WithCancel(ctx)
	defer cancel()
	go func() { _ = again.Run(actx, r.nc) }()
	<-again.Ready
	r.b.Anchors = again
	r.connect(1)
	noSignal(t, r.sig) // the records are not read yet
	if _, err := r.js.CreateOrUpdateConsumer(ctx, "PRESENCE", jetstream.ConsumerConfig{
		Name: AccountsConsumer, Durable: AccountsConsumer, FilterSubject: "presence.account.mattermost.*.v1",
		DeliverSubject: "deliver.bridge.accounts", DeliverPolicy: jetstream.DeliverLastPerSubjectPolicy,
		AckPolicy: jetstream.AckExplicitPolicy, IdleHeartbeat: 100 * time.Millisecond,
	}); err != nil {
		t.Fatal(err)
	}
	waitSignal(t, r.sig) // the retry catches up once they are
	if got := anchorOf(t, again, aliceID); got != "alice" || !again.CaughtUp() {
		t.Fatalf("after the restart: %q", got)
	}
	r.send(alicePost(pid(1), now()))
	if subject, _ := r.next(t); subject != "bus.hu.home.alice" {
		t.Fatalf("after the restart: %s", subject)
	}
}

// RT-6.42 step 3 (point 10): before the login service writes any record, the
// idle heartbeat says nothing is pending; every account goes out under "-".
func TestWithoutRecordsTheHeartbeatSaysCaughtUp(t *testing.T) {
	newLab(t) // waits until the idle heartbeat says the records are read
}

// RT-6.42 step 1 and its error table: a catch-up that fails half way moves
// no watermark, so the retry carries the older posts too; the read goes
// oldest first, a root before its reply.
func TestAnInterruptedCatchUpLosesNothing(t *testing.T) {
	r := newRig(t, nil)
	r.connect(1) // the first start sets the watermark
	r.none(t)
	wm, _, _ := r.kv.Watermark(context.Background(), "home")
	r.up.Store(0)
	r.mm.mu.Lock()
	r.mm.failUser[bobID] = 1 // Mattermost fails once on bob, the older post
	r.mm.mu.Unlock()
	r.mm.setPosts(
		map[string]any{"id": pid(2), "user_id": aliceID, "channel_id": cid, "message": "newer",
			"create_at": wm + 200, "update_at": wm + 200, "metadata": map[string]any{}},
		map[string]any{"id": pid(1), "user_id": bobID, "channel_id": cid, "message": "older",
			"create_at": wm + 100, "update_at": wm + 100, "metadata": map[string]any{}},
	)
	r.connect(2)
	// the retry (every 50 ms here) carries both, the older first
	_, first := r.next(t)
	_, second := r.next(t)
	if first["mm"] != pid(1) || second["mm"] != pid(2) {
		t.Fatalf("carried %v then %v, want the older then the newer", first["mm"], second["mm"])
	}
	r.none(t)
}

// RT-6.42 step 4: a publish the server refuses is an error, nothing is
// marked carried, the home stops and its signal with it.
func TestARefusedPublishStopsTheHome(t *testing.T) {
	r := newRig(t, nil)
	refusing := &refuser{}
	r.b.NATS = refusing
	r.connect(1)
	at := now()
	// the retry's catch-up finds the post again, and is refused again
	r.mm.setPosts(map[string]any{"id": pid(1), "user_id": aliceID, "channel_id": cid, "message": "go ahead",
		"create_at": at, "update_at": at})
	r.send(alicePost(pid(1), at))
	r.none(t)
	if done, _ := r.seen.Post(context.Background(), pid(1)); done {
		t.Fatal("a refused post is marked carried")
	}
	before := refusing.signals.Load()
	time.Sleep(300 * time.Millisecond)
	if refusing.signals.Load() != before {
		t.Fatal("a stopped home still signals")
	}
}

type refuser struct{ signals atomic.Int64 }

func (f *refuser) Publish(subject string, _ []byte) error {
	if strings.HasPrefix(subject, "presence.bridge.") {
		f.signals.Add(1)
		return nil
	}
	return errors.New("publish " + subject + ": refused: permissions violation")
}

// Confirmed returns the server's refusal of a publish as an error.
func TestConfirmedSeesARefusal(t *testing.T) {
	dir := t.TempDir()
	opts := &natsserver.Options{Host: "127.0.0.1", Port: -1, NoLog: true, NoSigs: true, StoreDir: dir,
		Users: []*natsserver.User{{Username: "u", Password: "pw",
			Permissions: &natsserver.Permissions{Publish: &natsserver.SubjectPermission{Allow: []string{"bus.hu.home.*"}}}}}}
	srv, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatal(err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(5 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(srv.Shutdown)
	nc, err := nats.Connect(srv.ClientURL(), nats.UserInfo("u", "pw"),
		nats.ErrorHandler(func(*nats.Conn, *nats.Subscription, error) {}))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	c := Confirmed{NC: nc}
	if err := c.Publish("bus.hu.home.alice", []byte("{}")); err != nil {
		t.Fatalf("an allowed publish: %v", err)
	}
	if err := c.Publish("bus.hu.other.alice", []byte("{}")); err == nil {
		t.Fatal("a refused publish passed")
	}
	if err := c.Publish("bus.hu.home.bob", []byte("{}")); err != nil {
		t.Fatalf("an allowed publish after a refusal: %v", err)
	}
}

// RT-6.42 step 1: while a home is connected and quiet its watermark moves
// along with Mattermost's clock, a minute behind, so a later reconnect does
// not reach back to the last carried post.
func TestAQuietHomesWatermarkMovesAlong(t *testing.T) {
	r := newRig(t, nil)
	if err := r.kv.SetWatermark(context.Background(), "home", now()-5*60000); err != nil {
		t.Fatal(err)
	}
	r.connect(1)
	deadline := time.Now().Add(3 * time.Second)
	for {
		wm, _, _ := r.kv.Watermark(context.Background(), "home")
		if wm >= now()-liveSlack.Milliseconds()-2000 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the watermark stays at %d", wm)
		}
		time.Sleep(20 * time.Millisecond)
	}
}
