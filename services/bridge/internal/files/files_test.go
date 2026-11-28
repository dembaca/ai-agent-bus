package files

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/agentbots"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/inbound"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mm"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/presence"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/requests"
)

const (
	goodID    = "abcdef0123456789"
	agent     = "agent-alice"
	botTok    = "bot-token"
	bridgeTok = "bridge-token"
	channel   = "home"
	cid       = "channel0000000000000000001"
	fileID    = "file0000000000000000000001"
	postID    = "post0000000000000000000001"
)

var quiet = slog.New(slog.NewTextHandler(io.Discard, nil))

// png is a file whose magic bytes say png.
func png(n int) []byte {
	return append([]byte("\x89PNG\r\n\x1a\n"), strings.Repeat("x", n)...)
}

// sum is a whole file's sha256, as the sender declares it.
func sum(data []byte) string {
	s := sha256.Sum256(data)
	return hex.EncodeToString(s[:])
}

// part is one put part's payload, the chunk named by the test so a small file
// can be sent in parts too.
func part(whole []byte, name, ch, declared string, n int, last bool, chunk []byte) string {
	return fmt.Sprintf(`{"op":"put","name":%q,"channel":%q,"length":%d,"sha256":%q,`+
		`"part":%d,"last":%t,"data":%q}`,
		name, ch, len(whole), declared, n, last, base64.StdEncoding.EncodeToString(chunk))
}

// putAll is the payload the MCP side sends for a whole small file: part 1,
// the last.
func putAll(name, ch string, whole []byte) string {
	return part(whole, name, ch, sum(whole), 1, true, whole)
}

// fakeMM is the part of Mattermost the file piece uses. Both bots' tokens are
// taken, and every call records which bot made it, so a test tells the
// agent's own from the bridge's.
type fakeMM struct {
	mu       sync.Mutex
	srv      *httptest.Server
	calls    []string
	team     string
	channels map[string]string // channel name -> id
	names    map[string]string // channel id -> name
	posts    map[string]map[string]any
	infos    map[string]map[string]any
	data     map[string][]byte
	previews map[string][]byte
	users    map[string]map[string]any
	uploads  []upload
	nextFile int
}

// upload is one upload the fake took, with the bot that made it.
type upload struct {
	bot       string
	channelID string
	name      string
	data      []byte
}

func newFakeMM(t *testing.T) *fakeMM {
	t.Helper()
	f := &fakeMM{team: "team", channels: map[string]string{}, names: map[string]string{},
		posts: map[string]map[string]any{}, infos: map[string]map[string]any{},
		data: map[string][]byte{}, previews: map[string][]byte{}, users: map[string]map[string]any{}}
	f.users["u-alice"] = map[string]any{"id": "u-alice", "username": "alice", "is_bot": false}
	f.users["u-carol"] = map[string]any{"id": "u-carol", "username": "carol", "is_bot": false}
	f.users["u-bot"] = map[string]any{"id": "u-bot", "username": "bridge-agent-alice", "is_bot": true}
	f.srv = httptest.NewServer(http.HandlerFunc(f.serve))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeMM) client() *mm.Client { return mm.New(f.srv.URL, botTok).As("bot-id") }
func (f *fakeMM) bridge() *mm.Client { return mm.New(f.srv.URL, bridgeTok).As("bridge-id") }

func (f *fakeMM) seen() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.calls...)
}

func (f *fakeMM) took() []upload {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]upload(nil), f.uploads...)
}

func (f *fakeMM) serve(w http.ResponseWriter, r *http.Request) {
	who := ""
	switch r.Header.Get("Authorization") {
	case "Bearer " + botTok:
		who = "bot"
	case "Bearer " + bridgeTok:
		who = "bridge"
	default:
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	f.mu.Lock()
	f.calls = append(f.calls, r.Method+" "+r.URL.Path+" as "+who)
	f.mu.Unlock()
	out := func(v any) { _ = json.NewEncoder(w).Encode(v) }
	path := r.URL.Path
	switch {
	case r.Method == "GET" && strings.HasPrefix(path, "/api/v4/teams/name/"+f.team+"/channels/name/"):
		name := strings.TrimPrefix(path, "/api/v4/teams/name/"+f.team+"/channels/name/")
		f.mu.Lock()
		id, ok := f.channels[name]
		f.mu.Unlock()
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		out(map[string]any{"id": id, "name": name})
	case r.Method == "POST" && path == "/api/v4/files":
		if err := r.ParseMultipartForm(32 << 20); err != nil {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		file, header, err := r.FormFile("files")
		if err != nil {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		data, _ := io.ReadAll(file)
		f.mu.Lock()
		f.nextFile++
		id := fmt.Sprintf("file%022d", f.nextFile)
		f.uploads = append(f.uploads, upload{bot: who, channelID: r.FormValue("channel_id"),
			name: header.Filename, data: data})
		f.mu.Unlock()
		out(map[string]any{"file_infos": []any{map[string]any{
			"id": id, "name": header.Filename, "size": len(data)}}})
	case r.Method == "GET" && strings.HasPrefix(path, "/api/v4/files/") && strings.HasSuffix(path, "/info"):
		id := strings.TrimSuffix(strings.TrimPrefix(path, "/api/v4/files/"), "/info")
		f.mu.Lock()
		info, ok := f.infos[id]
		f.mu.Unlock()
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		out(info)
	case r.Method == "GET" && strings.HasPrefix(path, "/api/v4/files/"):
		id := strings.TrimPrefix(path, "/api/v4/files/")
		preview := strings.HasSuffix(id, "/preview")
		id = strings.TrimSuffix(id, "/preview")
		f.mu.Lock()
		var (
			data []byte
			ok   bool
		)
		if preview {
			data, ok = f.previews[id]
		} else {
			data, ok = f.data[id]
		}
		f.mu.Unlock()
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		_, _ = w.Write(data)
	case r.Method == "GET" && strings.HasPrefix(path, "/api/v4/posts/"):
		id := strings.TrimPrefix(path, "/api/v4/posts/")
		f.mu.Lock()
		p, ok := f.posts[id]
		f.mu.Unlock()
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		out(p)
	case r.Method == "GET" && strings.HasPrefix(path, "/api/v4/channels/"):
		id := strings.TrimPrefix(path, "/api/v4/channels/")
		f.mu.Lock()
		name, ok := f.names[id]
		f.mu.Unlock()
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		out(map[string]any{"id": id, "name": name})
	case r.Method == "GET" && strings.HasPrefix(path, "/api/v4/users/"):
		id := strings.TrimPrefix(path, "/api/v4/users/")
		f.mu.Lock()
		u, ok := f.users[id]
		f.mu.Unlock()
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		out(u)
	default:
		w.WriteHeader(http.StatusNotFound)
	}
}

// file is a file of the fake, with its post in the channel and its bytes.
func (f *fakeMM) file(t *testing.T, id, name, ext, mime string, size int64, preview bool, data []byte) {
	t.Helper()
	f.infos[id] = map[string]any{"id": id, "name": name, "extension": ext, "size": size,
		"mime_type": mime, "post_id": postID, "user_id": "u-alice", "has_preview_image": preview}
	f.posts[postID] = map[string]any{"id": postID, "channel_id": cid, "user_id": "u-alice"}
	f.data[id] = data
}

// bots is the agent's own bot, pointed at the fake.
type bots struct {
	client *mm.Client
	err    error
	mu     sync.Mutex
	called []string
}

func (b *bots) Client(_ context.Context, name, channel string) (*mm.Client, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.called = append(b.called, name+"@"+channel)
	return b.client, b.err
}

// people are the login service's records for the test.
type people struct {
	anchors map[string]string
	recs    map[string]inbound.Account
	waiting bool // the consumer has not read them all yet
}

func (p people) Anchor(uid string) string { return p.anchors[uid] }
func (p people) Record(uid string) (inbound.Account, bool) {
	if p.waiting {
		return inbound.Account{}, false
	}
	r, ok := p.recs[uid]
	return r, ok
}
func (p people) CaughtUp() bool { return !p.waiting }

// rig is one handler with the fake Mattermost, a membership and the switches.
type rig struct {
	h    *Handler
	mm   *fakeMM
	bots *bots
	snap *presence.Snapshot
	sw   *presence.Switches
	now  time.Time
	ppl  people
}

func newRig(t *testing.T, pieces ...int) *rig {
	t.Helper()
	now := time.Now()
	f := newFakeMM(t)
	f.channels[channel] = cid
	f.names[cid] = channel
	snap := presence.New()
	snap.ObservedMember(channel, agent, now, 1)
	sw := &presence.Switches{}
	sw.Observed(channel, pieces, now)
	b := &bots{client: f.client()}
	ppl := people{anchors: map[string]string{"u-alice": "alice"},
		recs: map[string]inbound.Account{"u-alice": {
			Account: "u-alice", Anchor: "alice", Name: "Alice", Approver: true,
			TokenIat: 10, Iat: 11, Exp: now.Add(10 * time.Minute).Unix(),
		}}}
	return &rig{h: &Handler{Log: quiet, Team: "team", Bots: b, Bridge: f.bridge(),
		Presence: snap, Switches: sw, People: ppl},
		mm: f, bots: b, snap: snap, sw: sw, now: now, ppl: ppl}
}

// ask runs one file request as the agent.
func (r *rig) ask(t *testing.T, payload string) ([]requests.Reply, error) {
	t.Helper()
	return r.askAs(t, agent, payload)
}

// askAs runs one file request as another agent.
func (r *rig) askAs(t *testing.T, who, payload string) ([]requests.Reply, error) {
	t.Helper()
	r.h.Now = func() time.Time { return r.now }
	return r.h.Piece(context.Background(), who,
		requests.Request{V: 1, Request: goodID, Kind: "file", Payload: json.RawMessage(payload)})
}

// one is the single reply's result, decoded.
func one(t *testing.T, replies []requests.Reply, err error) map[string]any {
	t.Helper()
	if err != nil {
		t.Fatalf("the piece answered an error: %v", err)
	}
	if len(replies) != 1 {
		t.Fatalf("got %d parts, want 1", len(replies))
	}
	return asJSON(t, replies[0].Result)
}

// asJSON decodes one part's result.
func asJSON(t *testing.T, raw json.RawMessage) map[string]any {
	t.Helper()
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatalf("the part is no object: %v: %s", err, raw)
	}
	return out
}

// theData joins the byte parts after the metadata and decodes them.
func theData(t *testing.T, replies []requests.Reply) []byte {
	t.Helper()
	var out []byte
	for i, r := range replies[1:] {
		m := asJSON(t, r.Result)
		keys(t, m, "data")
		raw, err := base64.StdEncoding.DecodeString(m["data"].(string))
		if err != nil {
			t.Fatalf("part %d is no base64: %v", i+2, err)
		}
		out = append(out, raw...)
	}
	return out
}

// keys pins a part's JSON keys exactly, sorted.
func keys(t *testing.T, m map[string]any, want ...string) {
	t.Helper()
	got := make([]string, 0, len(m))
	for k := range m {
		got = append(got, k)
	}
	sortStrings(got)
	sortStrings(want)
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("the keys are %v, want %v", got, want)
	}
}

func sortStrings(s []string) {
	for i := 1; i < len(s); i++ {
		for j := i; j > 0 && s[j] < s[j-1]; j-- {
			s[j], s[j-1] = s[j-1], s[j]
		}
	}
}

// RT-6.33 step 4, plan B piece 8: an upload's parts are kept in order, each
// acked, and only the assembled whole — its length and hash checked — goes
// to Mattermost, with the agent's own bot, never the bridge's.
func TestAnUploadInPartsIsAckedAndUploaded(t *testing.T) {
	r := newRig(t, Piece)
	whole := png(50)
	replies, err := r.ask(t, part(whole, "chart.png", channel, sum(whole), 1, false, whole[:30]))
	if err != nil {
		t.Fatal(err)
	}
	if len(replies) != 1 || string(replies[0].Result) != `{"ok":true,"part":1}` {
		t.Fatalf("part 1's ack: %s", replies[0].Result)
	}
	if took := r.mm.took(); len(took) != 0 {
		t.Fatalf("a part was uploaded: %+v", took)
	}
	replies, err = r.ask(t, part(whole, "chart.png", channel, sum(whole), 2, true, whole[30:]))
	if err != nil {
		t.Fatal(err)
	}
	final := one(t, replies, err)
	keys(t, final, "file_id", "name", "size")
	if final["name"] != "chart.png" || final["size"] != float64(len(whole)) {
		t.Fatalf("the final answer: %v", final)
	}
	if fid, _ := final["file_id"].(string); !fileIDRE.MatchString(fid) {
		t.Fatalf("the file id %q is no file id", final["file_id"])
	}
	// the upload went through the agent's own bot, to the agent's channel
	tool := r.mm.took()
	if len(tool) != 1 {
		t.Fatalf("the uploads: %+v", tool)
	}
	if tool[0].bot != "bot" || tool[0].channelID != cid || tool[0].name != "chart.png" ||
		string(tool[0].data) != string(whole) {
		t.Fatalf("the upload: %+v", tool[0])
	}
	if got := r.bots.called; len(got) != 1 || got[0] != agent+"@"+channel {
		t.Fatalf("the bots asked: %v", got)
	}
	// a whole small file in one part
	small := png(9)
	replies, err = r.ask(t, putAll("small.png", channel, small))
	if err != nil {
		t.Fatal(err)
	}
	if m := one(t, replies, err); m["size"] != float64(len(small)) || m["name"] != "small.png" {
		t.Fatalf("a one-part file: %v", m)
	}
}

// A refusal answers its own words and nothing is uploaded, the checks of
// part 1 all before any part is kept.
func TestTheUploadRefusals(t *testing.T) {
	whole := png(10)
	for _, tc := range []struct {
		name string
		rig  *rig
		pay  string
		want string
	}{
		{"a foreign channel", newRig(t, Piece), putAll("chart.png", "elsewhere", whole),
			"not a channel of that agent"},
		{"the piece off", newRig(t), putAll("chart.png", channel, whole),
			"piece 8 is off in home"},
		{"another piece on", newRig(t, 5), putAll("chart.png", channel, whole),
			"piece 8 is off in home"},
		{"no extension", newRig(t, Piece), putAll("chart", channel, whole),
			`file name "chart": a name with an extension, e.g. chart.png`},
		{"a dot without a stem", newRig(t, Piece), putAll(".png", channel, whole),
			`file name ".png": a name with an extension, e.g. chart.png`},
		{"svg", newRig(t, Piece), putAll("chart.svg", channel, whole),
			"SVG is refused: it can carry script; render it as PNG"},
		{"an extension not allowed", newRig(t, Piece), putAll("chart.exe", channel, whole),
			".exe is not allowed: csv, gif, jpeg, jpg, json, log, md, pdf, png, tsv, txt, webp, yaml, yml"},
		{"a type that does not match", newRig(t, Piece), putAll("chart.png", channel, []byte("plain text")),
			"chart.png is named .png but its bytes are a text file"},
		{"binary under a text name", newRig(t, Piece),
			putAll("notes.txt", channel, []byte{0x00, 0x01, 0x02}),
			"notes.txt is named .txt but its bytes are no allowed type (binary, or not UTF-8)"},
		{"empty", newRig(t, Piece), part([]byte{}, "empty.txt", channel, sum([]byte{}), 1, true, nil),
			"the file is empty"},
		{"over 10 MB", newRig(t, Piece),
			fmt.Sprintf(`{"op":"put","name":"big.png","channel":%q,"length":%d,"sha256":%q,`+
				`"part":1,"last":false,"data":""}`, channel, MaxFile+1, sum(whole)),
			"the file is over 10 MB"},
	} {
		_, err := tc.rig.ask(t, tc.pay)
		if err == nil || err.Error() != tc.want {
			t.Errorf("%s: err %v, want %q", tc.name, err, tc.want)
		}
		if took := tc.rig.mm.took(); len(took) != 0 {
			t.Errorf("%s: an upload went out: %+v", tc.name, took)
		}
	}
	// the refusals of the checks read no Mattermost at all
	r := newRig(t)
	if calls := r.mm.seen(); len(calls) != 0 {
		t.Fatalf("Mattermost was read: %v", calls)
	}
}

// The parts must be consecutive: a gap names the part the transfer waits for,
// and the transfer goes on for the right part behind it.
func TestAWrongPartNamesTheExpectedOne(t *testing.T) {
	r := newRig(t, Piece)
	whole := png(50)
	if _, err := r.ask(t, part(whole, "chart.png", channel, sum(whole), 1, false, whole[:20])); err != nil {
		t.Fatal(err)
	}
	if _, err := r.ask(t, part(whole, "chart.png", channel, sum(whole), 3, true, whole[20:])); err == nil ||
		err.Error() != "wrong part: expected 2" {
		t.Fatalf("a gap: %v", err)
	}
	// the transfer is still open for the right part
	replies, err := r.ask(t, part(whole, "chart.png", channel, sum(whole), 2, true, whole[20:]))
	if err != nil {
		t.Fatal(err)
	}
	if m := one(t, replies, err); m["size"] != float64(len(whole)) {
		t.Fatalf("the completed transfer: %v", m)
	}
	if _, err := r.ask(t, part(whole, "chart.png", channel, sum(whole), 2, true, whole[20:])); err == nil ||
		err.Error() != "the transfer was dropped: send again" {
		t.Fatalf("after the last part: %v", err)
	}
}

// An unfinished transfer is dropped 60 s after its last part; a part of it
// answers so, and a new first part opens a new one.
func TestTheTransferIsDroppedAfter60s(t *testing.T) {
	r := newRig(t, Piece)
	whole := png(50)
	if _, err := r.ask(t, part(whole, "chart.png", channel, sum(whole), 1, false, whole[:20])); err != nil {
		t.Fatal(err)
	}
	r.now = r.now.Add(dropAfter + time.Second)
	if _, err := r.ask(t, part(whole, "chart.png", channel, sum(whole), 2, true, whole[20:])); err == nil ||
		err.Error() != "the transfer was dropped: send again" {
		t.Fatalf("a dropped transfer: %v", err)
	}
	// the dropped one freed its slot: a new one goes through
	replies, err := r.ask(t, putAll("again.png", channel, whole))
	if err != nil {
		t.Fatal(err)
	}
	if m := one(t, replies, err); m["name"] != "again.png" {
		t.Fatalf("the new transfer: %v", m)
	}
}

// The assembled whole must match what the first part declared — its length
// and its hash — or nothing is uploaded and the transfer is gone.
func TestABadHashRefusesAndDropsTheTransfer(t *testing.T) {
	r := newRig(t, Piece)
	whole := png(50)
	other := png(51)
	if _, err := r.ask(t, part(whole, "chart.png", channel, sum(other), 1, false, whole[:20])); err != nil {
		t.Fatal(err)
	}
	if _, err := r.ask(t, part(whole, "chart.png", channel, sum(other), 2, true, whole[20:])); err == nil ||
		err.Error() != "the hash does not match: send again" {
		t.Fatalf("a hash mismatch: %v", err)
	}
	if _, err := r.ask(t, part(whole, "chart.png", channel, sum(other), 2, true, whole[20:])); err == nil ||
		err.Error() != "the transfer was dropped: send again" {
		t.Fatalf("the transfer was kept: %v", err)
	}
	// a declared length the whole does not reach answers the same words
	if _, err := r.ask(t, part(whole, "cut.png", channel, sum(whole), 1, false, whole[:20])); err != nil {
		t.Fatal(err)
	}
	replies, err := r.ask(t, part(whole, "cut.png", channel, sum(whole), 2, true, whole[20:30]))
	if err == nil || err.Error() != "the hash does not match: send again" {
		t.Fatalf("a length mismatch: %v (%v)", err, replies)
	}
	if took := r.mm.took(); len(took) != 0 {
		t.Fatalf("a broken whole was uploaded: %+v", took)
	}
}

// At most 16 transfers are open across the agents; the 17th is told busy, and
// an agent's own new first part replaces its open one instead.
func TestTheSeventeenthOpenTransferIsBusy(t *testing.T) {
	r := newRig(t, Piece)
	whole := png(9)
	names := make([]string, 16)
	for i := range names {
		names[i] = fmt.Sprintf("agent-%02d", i)
		r.snap.ObservedMember(channel, names[i], r.now, 1)
		if _, err := r.askAs(t, names[i], part(whole, "chart.png", channel, sum(whole), 1, false, whole)); err != nil {
			t.Fatalf("%s's part 1: %v", names[i], err)
		}
	}
	// the 17th, from an agent with a membership of the channel: busy
	_, err := r.ask(t, putAll("mine.png", channel, whole))
	if err == nil || err.Error() != "busy: too many open transfers, send again" {
		t.Fatalf("the 17th: %v", err)
	}
	// an agent with an open transfer replaces it, never busy
	replies, err := r.askAs(t, names[0], putAll("new.png", channel, whole))
	if err != nil {
		t.Fatalf("a replacement answered busy: %v", err)
	}
	if m := one(t, replies, err); m["name"] != "new.png" {
		t.Fatalf("the replacement: %v", m)
	}
	// an own part 2 goes through too, and completes its transfer
	if _, err := r.askAs(t, names[1], part(whole, "chart.png", channel, sum(whole), 2, true, nil)); err != nil {
		t.Fatalf("the agent's own part 2: %v", err)
	}
	// a stale transfer frees its slot for the 17th
	r.now = r.now.Add(dropAfter + time.Second)
	replies, err = r.ask(t, putAll("mine.png", channel, whole))
	if err != nil {
		t.Fatalf("after the drop: %v", err)
	}
	if m := one(t, replies, err); m["name"] != "mine.png" {
		t.Fatalf("after the drop: %v", m)
	}
}

// RT-6.23 step 2 over the bridge: a text file's metadata with its author's
// anchor and record, then its bytes; the reader checks length and hash.
func TestGetATextFile(t *testing.T) {
	r := newRig(t, Piece)
	text := []byte("hello world")
	r.mm.file(t, fileID, "notes.txt", "txt", "text/plain", int64(len(text)), false, text)
	replies, err := r.ask(t, `{"op":"get","id":"`+fileID+`"}`)
	if err != nil {
		t.Fatal(err)
	}
	if len(replies) != 2 {
		t.Fatalf("got %d parts, want the metadata and the bytes", len(replies))
	}
	meta := asJSON(t, replies[0].Result)
	keys(t, meta, "v", "id", "name", "extension", "size", "mime_type", "width", "height",
		"post_id", "channel", "channel_id", "author", "author_name", "anchor", "account",
		"bot", "kind", "length", "sha256", "preview")
	if meta["id"] != fileID || meta["name"] != "notes.txt" || meta["extension"] != "txt" ||
		meta["size"] != float64(len(text)) || meta["mime_type"] != "text/plain" ||
		meta["post_id"] != postID || meta["channel"] != channel || meta["channel_id"] != cid ||
		meta["author"] != "u-alice" || meta["author_name"] != "alice" || meta["anchor"] != "alice" ||
		meta["bot"] != false || meta["kind"] != "text" || meta["preview"] != false ||
		meta["length"] != float64(len(text)) {
		t.Fatalf("the metadata: %v", meta)
	}
	acc, _ := meta["account"].(map[string]any)
	if acc == nil || acc["name"] != "Alice" || acc["anchor"] != "alice" || acc["v"] != float64(1) ||
		acc["account"] != "u-alice" || acc["approver"] != true {
		t.Fatalf("the author's record: %v", acc)
	}
	if meta["sha256"] != sum(text) {
		t.Fatalf("the sha256: %v", meta["sha256"])
	}
	if got := theData(t, replies); string(got) != string(text) {
		t.Fatalf("the bytes: %q", got)
	}
	// the serving reads are the agent's own bot's; the bridge's resolved the
	// channel alone
	if got := r.bots.called; len(got) != 1 || got[0] != agent+"@"+channel {
		t.Fatalf("the bots asked: %v", got)
	}
	for _, call := range r.mm.seen() {
		if strings.HasSuffix(call, " as bridge") &&
			!strings.Contains(call, "/info") && !strings.Contains(call, "/posts/") &&
			!strings.Contains(call, "/channels/") {
			t.Fatalf("the bridge bot served: %s", call)
		}
	}
	// an unbound author carries no record and is no bot; a bot's author is
	r2 := newRig(t, Piece)
	r2.mm.infos[fileID] = map[string]any{"id": fileID, "name": "carol.txt", "extension": "txt",
		"size": 3, "mime_type": "text/plain", "post_id": postID, "user_id": "u-carol",
		"has_preview_image": false}
	r2.mm.posts[postID] = map[string]any{"id": postID, "channel_id": cid}
	r2.mm.data[fileID] = []byte("abc")
	replies, err = r2.ask(t, `{"op":"get","id":"`+fileID+`"}`)
	if err != nil {
		t.Fatal(err)
	}
	meta = asJSON(t, replies[0].Result)
	if _, has := meta["account"]; has {
		t.Fatalf("an unbound author carries a record: %v", meta)
	}
	if meta["author_name"] != "carol" || meta["anchor"] != "" || meta["bot"] != false {
		t.Fatalf("an unbound person: %v", meta)
	}
	r2.mm.infos[fileID]["user_id"] = "u-bot"
	replies, err = r2.ask(t, `{"op":"get","id":"`+fileID+`"}`)
	if err != nil {
		t.Fatal(err)
	}
	if meta = asJSON(t, replies[0].Result); meta["bot"] != true || meta["anchor"] != "" {
		t.Fatalf("a bot's author: %v", meta)
	}
}

// An image is served as its preview when it has one, else the original in a
// format shown as a picture — and an image that is neither (over 5 MB with no
// preview, or a format no client shows) is metadata alone, which the reader
// notes from the metadata.
func TestGetAnImage(t *testing.T) {
	r := newRig(t, Piece)
	original := png(20)
	preview := []byte("the preview bytes")
	r.mm.file(t, fileID, "photo.png", "png", "image/png", int64(len(original)), true, original)
	r.mm.previews[fileID] = preview
	replies, err := r.ask(t, `{"op":"get","id":"`+fileID+`"}`)
	if err != nil {
		t.Fatal(err)
	}
	meta := asJSON(t, replies[0].Result)
	if meta["kind"] != "image" || meta["preview"] != true || meta["width"] != float64(0) {
		t.Fatalf("the image's metadata: %v", meta)
	}
	if got := theData(t, replies); string(got) != string(preview) {
		t.Fatalf("the preview was not served: %q", got)
	}
	// no preview: the original, in a format shown as a picture
	r2 := newRig(t, Piece)
	r2.mm.file(t, fileID, "photo.png", "png", "image/png", int64(len(original)), false, original)
	replies, err = r2.ask(t, `{"op":"get","id":"`+fileID+`"}`)
	if err != nil {
		t.Fatal(err)
	}
	if got := theData(t, replies); string(got) != string(original) {
		t.Fatalf("the original was not served: %q", got)
	}
	// over 5 MB and no preview: metadata only, part 1 the last
	r3 := newRig(t, Piece)
	big := png(20)
	r3.mm.file(t, fileID, "big.png", "png", "image/png", ImageMax+1, false, big)
	replies, err = r3.ask(t, `{"op":"get","id":"`+fileID+`"}`)
	if err != nil {
		t.Fatal(err)
	}
	if len(replies) != 1 {
		t.Fatalf("an unservable image answered %d parts", len(replies))
	}
	meta = asJSON(t, replies[0].Result)
	if meta["kind"] != "image" || meta["length"] != float64(0) || meta["sha256"] != sum(nil) {
		t.Fatalf("the unservable image: %v", meta)
	}
	// a format no client shows as a picture, without a preview: metadata only
	r4 := newRig(t, Piece)
	r4.mm.file(t, fileID, "photo.bmp", "bmp", "image/bmp", 4, false, []byte("bmpe"))
	replies, err = r4.ask(t, `{"op":"get","id":"`+fileID+`"}`)
	if err != nil {
		t.Fatal(err)
	}
	if len(replies) != 1 || asJSON(t, replies[0].Result)["kind"] != "image" {
		t.Fatalf("a bmp: %v", replies)
	}
}

// Anything else is metadata only, no bytes: part 1 is the last.
func TestGetAnythingElseIsMetadataOnly(t *testing.T) {
	r := newRig(t, Piece)
	r.mm.file(t, fileID, "sheet.xlsx", "xlsx", "application/vnd.ms-excel", 12, false, []byte("excel bytes"))
	replies, err := r.ask(t, `{"op":"get","id":"`+fileID+`"}`)
	if err != nil {
		t.Fatal(err)
	}
	if len(replies) != 1 {
		t.Fatalf("got %d parts, want the metadata alone", len(replies))
	}
	meta := asJSON(t, replies[0].Result)
	if meta["kind"] != "other" || meta["length"] != float64(0) || meta["sha256"] != sum(nil) {
		t.Fatalf("the metadata: %v", meta)
	}
}

// The bytes come in parts of at most 700 KiB raw, each answer's body under
// the scaffold's 1 MiB bound, and the whole they build carries the metadata's
// length and hash.
func TestTheBytesComeInPartsOf700KiB(t *testing.T) {
	r := newRig(t, Piece)
	big := png(1500 * 1024) // 1.5 MB: three parts
	r.mm.file(t, fileID, "big.png", "png", "image/png", int64(len(big)), false, big)
	replies, err := r.ask(t, `{"op":"get","id":"`+fileID+`"}`)
	if err != nil {
		t.Fatal(err)
	}
	if want := 1 + (len(big)+partMax-1)/partMax; len(replies) != want {
		t.Fatalf("got %d parts, want %d", len(replies), want)
	}
	for i, reply := range replies {
		if len(reply.Result) > 1<<20 {
			t.Fatalf("part %d's body is %d bytes", i+1, len(reply.Result))
		}
	}
	meta := asJSON(t, replies[0].Result)
	got := theData(t, replies)
	if len(got) != int(meta["length"].(float64)) || sum(got) != meta["sha256"] {
		t.Fatalf("the bytes do not hold together: %d of %v", len(got), meta["length"])
	}
	if string(got) != string(big) {
		t.Fatal("the bytes are not the file's")
	}
}

// A text file is cut at 80 KB (the MCP side's TEXT_MAX*4), its hash the
// cut's.
func TestTheTextIsCutAt80KB(t *testing.T) {
	r := newRig(t, Piece)
	text := []byte(strings.Repeat("a", textBytes+1))
	r.mm.file(t, fileID, "long.txt", "txt", "text/plain", int64(len(text)), false, text)
	replies, err := r.ask(t, `{"op":"get","id":"`+fileID+`"}`)
	if err != nil {
		t.Fatal(err)
	}
	meta := asJSON(t, replies[0].Result)
	if meta["length"] != float64(textBytes) || meta["size"] != float64(len(text)) {
		t.Fatalf("the cut: %v", meta)
	}
	if got := theData(t, replies); len(got) != textBytes || sum(got) != meta["sha256"] {
		t.Fatalf("the cut bytes: %d of %v", len(got), meta["length"])
	}
}

// A file of a channel the agent did not announce is refused alike whether the
// piece is on there or not, and so is one of a channel with the piece off;
// the agent's own bot is never asked.
func TestTheGetRefusals(t *testing.T) {
	whole := png(9)
	// a foreign channel
	r := newRig(t, Piece)
	r.mm.channels["elsewhere"] = "another0000000000000000001"
	r.mm.names["another0000000000000000001"] = "elsewhere"
	r.mm.infos[fileID] = map[string]any{"id": fileID, "name": "chart.png", "extension": "png",
		"size": len(whole), "mime_type": "image/png", "post_id": postID, "user_id": "u-alice"}
	r.mm.posts[postID] = map[string]any{"id": postID, "channel_id": "another0000000000000000001"}
	r.mm.data[fileID] = whole
	if _, err := r.ask(t, `{"op":"get","id":"`+fileID+`"}`); err == nil ||
		err.Error() != "not a channel of that agent" {
		t.Fatalf("a foreign channel: %v", err)
	}
	if len(r.bots.called) != 0 {
		t.Fatalf("a bot was asked for: %v", r.bots.called)
	}
	// the piece off
	r2 := newRig(t)
	r2.mm.file(t, fileID, "chart.png", "png", "image/png", int64(len(whole)), false, whole)
	if _, err := r2.ask(t, `{"op":"get","id":"`+fileID+`"}`); err == nil ||
		err.Error() != "piece 8 is off in home" {
		t.Fatalf("the piece off: %v", err)
	}
	if len(r2.bots.called) != 0 {
		t.Fatalf("a bot was asked for: %v", r2.bots.called)
	}
	// the account records not read yet: no author is called unbound
	r3 := newRig(t, Piece)
	r3.mm.file(t, fileID, "chart.png", "png", "image/png", int64(len(whole)), false, whole)
	r3.ppl.waiting = true
	r3.h.People = r3.ppl
	if _, err := r3.ask(t, `{"op":"get","id":"`+fileID+`"}`); err == nil ||
		!strings.Contains(err.Error(), "not read yet") {
		t.Fatalf("unread records: %v", err)
	}
	// the agent's bot is not in the channel: its own words, nothing joined
	r4 := newRig(t, Piece)
	r4.mm.file(t, fileID, "chart.png", "png", "image/png", int64(len(whole)), false, whole)
	r4.bots.err = &agentbots.NotInChannel{Channel: channel}
	if _, err := r4.ask(t, `{"op":"get","id":"`+fileID+`"}`); err == nil ||
		err.Error() != "the agent's bot is not in home" {
		t.Fatalf("a bot outside the channel: %v", err)
	}
	// a file attached to no post: its channel cannot be found
	r5 := newRig(t, Piece)
	r5.mm.infos[fileID] = map[string]any{"id": fileID, "name": "loose.png", "extension": "png",
		"size": len(whole), "mime_type": "image/png", "user_id": "u-alice"}
	if _, err := r5.ask(t, `{"op":"get","id":"`+fileID+`"}`); err == nil ||
		!strings.Contains(err.Error(), "attached to no post") {
		t.Fatalf("a loose file: %v", err)
	}
	// a file Mattermost does not have
	if _, err := r5.ask(t, `{"op":"get","id":"`+fileID+`x"}`); err == nil {
		t.Fatal("an unknown file id is no error")
	}
}

// The payload is decoded strictly: one object, no unknown key, and the put's
// and the get's own bounds.
func TestThePayloadIsDecodedStrictly(t *testing.T) {
	r := newRig(t, Piece)
	whole := png(9)
	for _, tc := range []struct{ name, pay, want string }{
		{"an unknown key", `{"op":"put","x":1,"name":"chart.png","channel":"home","length":1,` +
			`"sha256":"` + sum(whole) + `","part":1,"last":true,"data":""}`, "unknown field"},
		{"data after the object", putAll("chart.png", channel, whole) + " 1", "data after the object"},
		{"no op", `{"name":"chart.png","channel":"home","length":1,"sha256":"` +
			sum(whole) + `","part":1,"last":true,"data":""}`, `op "" is neither put nor get`},
		{"a bad channel", `{"op":"put","name":"chart.png","channel":"Home","length":1,` +
			`"sha256":"` + sum(whole) + `","part":1,"last":true,"data":""}`, "no channel token"},
		{"no hash", `{"op":"put","name":"chart.png","channel":"home","length":1,` +
			`"part":1,"last":true,"data":""}`, "names no sha256"},
		{"part 0", `{"op":"put","name":"chart.png","channel":"home","length":1,` +
			`"sha256":"` + sum(whole) + `","part":0,"last":true,"data":""}`, "parts start at 1"},
		{"a bad file id", `{"op":"get","id":"short"}`, "no file id"},
	} {
		if _, err := r.ask(t, tc.pay); err == nil || !strings.Contains(err.Error(), tc.want) {
			t.Errorf("%s: err %v, want %q", tc.name, err, tc.want)
		}
	}
	if calls := r.mm.seen(); len(calls) != 0 {
		t.Fatalf("Mattermost was read: %v", calls)
	}
}

// The name rules: a directory is cut, control characters and separators are
// dropped (a space kept), a long name is cut with its extension kept; and a
// text part may end inside a multi-byte character, which reads as text.
func TestTheNameAndSniffRules(t *testing.T) {
	for _, tc := range []struct{ name, want string }{
		{"a/b/chart.png", "chart.png"},
		{`a\b\chart.png`, "chart.png"},
		{"chart.png ", "chart.png"},                                          // stripped
		{"cha\x00rt.png", "chart.png"},                                       // a control character dropped
		{"cha\u2028rt.png", "chart.png"},                                     // a separator dropped
		{"my chart.png", "my chart.png"},                                     // a space kept
		{"chart.png", "chart.png"},                                           // the extension kept
		{strings.Repeat("s", 99) + ".png", strings.Repeat("s", 96) + ".png"}, // cut at 100
	} {
		got, err := cleanName(tc.name)
		if err != nil || got != tc.want {
			t.Errorf("%q: %q %v, want %q", tc.name, got, err, tc.want)
		}
	}
	for _, bad := range []string{"chart", "chart.", ".png", " .png", "  "} {
		if _, err := cleanName(bad); err == nil {
			t.Errorf("%q is a name", bad)
		}
	}
	// a part that cuts a multi-byte character reads as text
	whole := []byte("héllo wörld")               // é and ö are two bytes each
	if got := sniff1(whole[:2]); got != "text" { // h + the é's first byte
		t.Fatalf("a cut rune: %q", got)
	}
	if got := sniff1(whole); got != "text" {
		t.Fatalf("the whole text: %q", got)
	}
	if got := sniff1(png(3)); got != "png" {
		t.Fatalf("png: %q", got)
	}
	if got := sniff1([]byte{0x00}); got != "" {
		t.Fatalf("binary: %q", got)
	}
	if got := sniff1(append([]byte("RIFF"), append(make([]byte, 4), []byte("WEBP")...)...)); got != "webp" {
		t.Fatalf("webp: %q", got)
	}
}
