package admin

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/directory"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/testrealm"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

func ctx(t *testing.T) context.Context {
	t.Helper()
	c, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	t.Cleanup(cancel)
	return c
}

// newStore starts a NATS server with JetStream and returns a directory over
// a fresh bucket.
func newStore(t *testing.T) *directory.Store {
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
	s, err := directory.Open(ctx(t), js, "", false)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

type env struct {
	realm *testrealm.Realm
	store *directory.Store
	srv   *httptest.Server
	admin string // alice, in the admin group
	bob   string // in another group
	carol string // in another group; owns channels
}

func setup(t *testing.T, cfg Config) *env {
	t.Helper()
	return setupWith(t, cfg, testrealm.NewChat("alice", "bob", "carol", "mal"), &fakeMM{})
}

func setupWith(t *testing.T, cfg Config, chat *testrealm.Chat, src Source) *env {
	t.Helper()
	realm := testrealm.New(t)
	v := realm.Verifier(chat)
	store := newStore(t)
	cfg.AdminGroup = testrealm.Group
	h, err := New(cfg, v, store, src)
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	other := func(c jwt.MapClaims) { c["groups"] = []string{"other"} }
	return &env{realm: realm, store: store, srv: srv, admin: realm.Token("alice", nil),
		bob: realm.Token("bob", other), carol: realm.Token("carol", other)}
}

func (e *env) do(t *testing.T, method, path, tok, body string, hdr ...string) (int, string, http.Header) {
	t.Helper()
	req, err := http.NewRequest(method, e.srv.URL+path, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	if tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	for i := 0; i+1 < len(hdr); i += 2 {
		req.Header.Set(hdr[i], hdr[i+1])
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(b), resp.Header
}

func want(t *testing.T, what string, got, code int, body string) {
	t.Helper()
	if got != code {
		t.Fatalf("%s: HTTP %d, want %d: %s", what, got, code, body)
	}
}

func TestNewNeedsAnAdminGroup(t *testing.T) {
	if _, err := New(Config{}, nil, nil, nil); err == nil {
		t.Fatal("no admin group accepted")
	}
	if _, err := New(Config{AdminGroup: "/"}, nil, nil, nil); err == nil {
		t.Fatal("empty admin group accepted")
	}
}

func TestAuthMatrix(t *testing.T) {
	e := setup(t, Config{Burst: 100})
	expired := e.realm.Token("alice", func(c jwt.MapClaims) { c["exp"] = time.Now().Add(-time.Hour).Unix() })
	wrongAud := e.realm.Token("alice", func(c jwt.MapClaims) { c["aud"] = []string{"other-client"} })
	for name, tok := range map[string]string{"no token": "", "wrong audience": wrongAud, "expired": expired, "garbage": "abc"} {
		code, body, _ := e.do(t, "GET", "/v1/directory/channels", tok, "")
		want(t, name, code, 401, body)
		if tok != "" && strings.Contains(body, tok) {
			t.Fatalf("%s: the token is echoed", name)
		}
	}
	code, body, _ := e.do(t, "GET", "/v1/directory/channels", e.bob, "")
	want(t, "non-admin list", code, 200, body)
	if strings.TrimSpace(body) != "[]" {
		t.Fatalf("empty directory: %s", body)
	}
	code, body, _ = e.do(t, "PUT", "/v1/directory/channels/town", e.bob, `{"owner":"sub-carol"}`, "If-None-Match", "*")
	want(t, "non-admin put", code, 403, body)
	for _, p := range []string{"/v1/directory/pending", "/v1/directory/import"} {
		m := "GET"
		if strings.HasSuffix(p, "import") {
			m = "POST"
		}
		code, body, _ = e.do(t, m, p, e.bob, "")
		want(t, "non-admin "+p, code, 403, body)
	}
	// the group with a leading slash, as Keycloak's full path gives it
	slash := e.realm.Token("bob", func(c jwt.MapClaims) { c["groups"] = []string{"/" + testrealm.Group} })
	code, body, _ = e.do(t, "PUT", "/v1/directory/channels/slash", slash, `{}`, "If-None-Match", "*")
	want(t, "slash group", code, 201, body)
}

func TestChannelPreconditions(t *testing.T) {
	e := setup(t, Config{Burst: 100})
	const path = "/v1/directory/channels/town"
	code, body, h := e.do(t, "PUT", path, e.admin, `{"owner":"sub-carol","public":true,"bindings":{"mattermost":"town"}}`, "If-None-Match", "*")
	want(t, "create", code, 201, body)
	tag := h.Get("ETag")
	if tag == "" {
		t.Fatal("no ETag")
	}
	code, body, _ = e.do(t, "PUT", path, e.admin, `{}`, "If-None-Match", "*")
	want(t, "create twice", code, 409, body)
	code, body, _ = e.do(t, "PUT", path, e.admin, `{"owner":"sub-carol"}`)
	want(t, "no precondition", code, 428, body)
	code, body, _ = e.do(t, "PUT", path, e.admin, `{"owner":"sub-carol"}`, "If-Match", `"999999"`)
	want(t, "stale", code, 409, body)
	// members are kept on update
	code, body, h = e.do(t, "PATCH", path+"/members", e.carol, `{"add":["sub-bob"]}`, "If-Match", tag)
	want(t, "add member", code, 200, body)
	code, body, h = e.do(t, "PUT", path, e.admin, `{"owner":"sub-carol","public":false}`, "If-Match", h.Get("ETag"))
	want(t, "update", code, 200, body)
	var c directory.Channel
	_ = json.Unmarshal([]byte(body), &c)
	if c.Public || len(c.Members) != 1 || c.Members[0] != "sub-bob" {
		t.Fatalf("members not kept: %+v", c)
	}
	code, body, h = e.do(t, "GET", path, e.bob, "")
	want(t, "get", code, 200, body)
	if h.Get("ETag") == "" {
		t.Fatal("no ETag on get")
	}
	code, body, _ = e.do(t, "GET", "/v1/directory/channels/none", e.bob, "")
	want(t, "missing", code, 404, body)
}

func TestMembersOnlyByTheOwner(t *testing.T) {
	e := setup(t, Config{Burst: 100})
	const path = "/v1/directory/channels/home"
	_, _, h := e.do(t, "PUT", path, e.admin, `{"owner":"sub-carol"}`, "If-None-Match", "*")
	tag := h.Get("ETag")
	code, body, _ := e.do(t, "PATCH", path+"/members", e.admin, `{"add":["sub-bob"]}`, "If-Match", tag)
	want(t, "admin, not owner", code, 403, body)
	code, body, _ = e.do(t, "PATCH", path+"/members", e.bob, `{"add":["sub-bob"]}`, "If-Match", tag)
	want(t, "other, who does not see the private channel", code, 404, body)
	code, body, _ = e.do(t, "PATCH", path+"/members", e.carol, `{"add":["sub-bob"]}`)
	want(t, "owner, no If-Match", code, 428, body)
	code, body, _ = e.do(t, "PATCH", path+"/members", e.carol, `{"add":["sub-bob"]}`, "If-Match", `"999999"`)
	want(t, "owner, stale", code, 409, body)
	code, body, _ = e.do(t, "PATCH", path+"/members", e.carol, `{"add":["sub-zed","sub-bob","sub-bob"]}`, "If-Match", tag)
	want(t, "owner", code, 200, body)
	var c directory.Channel
	_ = json.Unmarshal([]byte(body), &c)
	if strings.Join(c.Members, ",") != "sub-bob,sub-zed" {
		t.Fatalf("members: %v", c.Members)
	}
	code, body, h = e.do(t, "GET", path, e.bob, "")
	want(t, "get", code, 200, body)
	code, body, _ = e.do(t, "PATCH", path+"/members", e.carol, `{"remove":["sub-zed"]}`, "If-Match", h.Get("ETag"))
	want(t, "remove", code, 200, body)
	if strings.Contains(body, "sub-zed") {
		t.Fatalf("not removed: %s", body)
	}

	_, _, h = e.do(t, "PUT", "/v1/directory/channels/orphan", e.admin, `{"owner":""}`, "If-None-Match", "*")
	for name, tok := range map[string]string{"admin": e.admin, "owner-less carol": e.carol, "bob": e.bob} {
		code, body, _ = e.do(t, "PATCH", "/v1/directory/channels/orphan/members", tok, `{"add":["sub-bob"]}`, "If-Match", h.Get("ETag"))
		wantCode := 404 // a private channel one is not in looks missing
		if name == "admin" {
			wantCode = 403
		}
		want(t, "no owner, "+name, code, wantCode, body)
	}
}

func TestBodyRules(t *testing.T) {
	e := setup(t, Config{})
	big := `{"owner":"` + strings.Repeat("a", 70<<10) + `"}`
	code, body, _ := e.do(t, "PUT", "/v1/directory/channels/big", e.admin, big, "If-None-Match", "*")
	want(t, "too big", code, 413, body)
	code, body, _ = e.do(t, "PUT", "/v1/directory/channels/odd", e.admin, `{"owner":"","members":["x"]}`, "If-None-Match", "*")
	want(t, "unknown field", code, 400, body)
	code, body, _ = e.do(t, "PUT", "/v1/directory/channels/Odd", e.admin, `{}`, "If-None-Match", "*")
	want(t, "bad name", code, 400, body)
}

func TestRateLimit(t *testing.T) {
	e := setup(t, Config{Burst: 3, RatePerSec: 0.001})
	var codes []int
	for range 5 {
		code, _, _ := e.do(t, "GET", "/v1/directory/channels", e.bob, "")
		codes = append(codes, code)
	}
	if codes[2] != 200 || codes[3] != 429 || codes[4] != 429 {
		t.Fatalf("codes %v", codes)
	}
}

// RT-6.40 step 3, ADR-016 point 4: nobody reads the member list of a private channel they are
// not in; an admin reads every channel. A private channel looks missing.
func TestReadingShowsOnlyWhatOneMaySee(t *testing.T) {
	e := setup(t, Config{Burst: 100})
	for path, body := range map[string]string{
		"/v1/directory/channels/secret": `{"owner":"sub-carol"}`,
		"/v1/directory/channels/town":   `{"owner":"sub-carol","public":true}`,
	} {
		code, b, _ := e.do(t, "PUT", path, e.admin, body, "If-None-Match", "*")
		want(t, "create "+path, code, 201, b)
	}
	names := func(tok string) string {
		t.Helper()
		code, body, _ := e.do(t, "GET", "/v1/directory/channels", tok, "")
		want(t, "list", code, 200, body)
		var chs []directory.Channel
		_ = json.Unmarshal([]byte(body), &chs)
		var out []string
		for _, c := range chs {
			out = append(out, c.Name)
		}
		return strings.Join(out, ",")
	}
	if got := names(e.bob); got != "town" {
		t.Fatalf("bob lists %q", got)
	}
	if got := names(e.carol); got != "secret,town" {
		t.Fatalf("carol, the owner, lists %q", got)
	}
	if got := names(e.admin); got != "secret,town" {
		t.Fatalf("the admin lists %q", got)
	}
	code, body, _ := e.do(t, "GET", "/v1/directory/channels/secret", e.bob, "")
	want(t, "bob reads secret", code, 404, body)
	code, body, _ = e.do(t, "GET", "/v1/directory/channels/secret", e.carol, "")
	want(t, "carol reads secret", code, 200, body)
}

func TestPatchDoesNotRevealAPrivateChannel(t *testing.T) {
	e := setup(t, Config{Burst: 100})
	_, _, h := e.do(t, "PUT", "/v1/directory/channels/secret", e.admin, `{"owner":"sub-carol","public":false}`, "If-None-Match", "*")
	tag := h.Get("ETag")
	_, _, _ = e.do(t, "PATCH", "/v1/directory/channels/secret/members", e.carol, `{"add":["sub-bob"]}`, "If-Match", tag)
	_, _, h = e.do(t, "GET", "/v1/directory/channels/secret", e.carol, "")
	tag = h.Get("ETag")
	mal := e.realm.Token("mal", func(c jwt.MapClaims) { c["groups"] = []string{"other"} })
	code, missing, _ := e.do(t, "PATCH", "/v1/directory/channels/nowhere/members", mal, `{"add":["x"]}`, "If-Match", tag)
	want(t, "missing", code, 404, missing)
	code, body, _ := e.do(t, "PATCH", "/v1/directory/channels/secret/members", mal, `{"add":["x"]}`, "If-Match", tag)
	want(t, "non-member, private", code, 404, body)
	if body != missing {
		t.Fatalf("a private channel answers differently from a missing one: %q vs %q", body, missing)
	}
	code, body, _ = e.do(t, "PATCH", "/v1/directory/channels/secret/members", e.bob, `{"add":["x"]}`, "If-Match", tag)
	want(t, "member, not owner", code, 403, body)
}

func TestARateLimitIsPerIdentityNotPerAddress(t *testing.T) {
	e := setup(t, Config{Burst: 2, RatePerSec: 0.001})
	var codes []int
	for range 3 {
		code, _, _ := e.do(t, "GET", "/v1/directory/channels", e.bob, "")
		codes = append(codes, code)
	}
	if codes[0] != 200 || codes[1] != 200 || codes[2] != 429 {
		t.Fatalf("bob: %v", codes)
	}
	code, body, _ := e.do(t, "GET", "/v1/directory/channels", e.carol, "")
	want(t, "another sub, same address", code, 200, body)
}

func TestThePreAuthLimitAnswersAFloodWithoutTokens(t *testing.T) {
	e := setup(t, Config{PreAuthBurst: 3, PreAuthRatePerSec: 0.001})
	var codes []int
	for range 5 {
		code, _, _ := e.do(t, "GET", "/v1/directory/channels", "", "")
		codes = append(codes, code)
	}
	if codes[2] != 401 || codes[3] != 429 || codes[4] != 429 {
		t.Fatalf("codes %v", codes)
	}
}

func TestRefusalsAreLoggedWithoutTheToken(t *testing.T) {
	var buf bytes.Buffer
	e := setup(t, Config{Burst: 100, Log: slog.New(slog.NewTextHandler(&buf, nil))})
	code, body, _ := e.do(t, "PUT", "/v1/directory/channels/x", e.bob, `{}`, "If-None-Match", "*")
	want(t, "403", code, 403, body)
	code, body, _ = e.do(t, "GET", "/v1/directory/channels", "", "")
	want(t, "401", code, 401, body)
	log := buf.String()
	for _, w := range []string{"sub-bob", "preferred_username=bob", "status=403", "status=401", "remote=127.0.0.1", "reason="} {
		if !strings.Contains(log, w) {
			t.Fatalf("log lacks %q: %s", w, log)
		}
	}
	if strings.Contains(log, e.bob) {
		t.Fatal("the token is logged")
	}
}

func TestTheConfirmRouteWantsTheAdminGroup(t *testing.T) {
	e := setup(t, Config{Burst: 100})
	code, body, _ := e.do(t, "POST", "/v1/directory/pending/bob/confirm", e.bob, "", "If-Match", `"1"`)
	want(t, "non-admin confirm", code, 403, body)
}

// A token whose user name equals someone's sub string, with another sub, is
// not that someone: membership and ownership compare the sub.
func TestOwnershipComparesTheSubNotTheUserName(t *testing.T) {
	chat := testrealm.NewChat("alice", "carol", "mal")
	chat.Users["sub-carol"] = token.User{Username: "sub-carol", AuthKnown: true, AuthService: token.OpenIDService, AuthData: "sub-mal"}
	e := setupWith(t, Config{Burst: 100}, chat, &fakeMM{})
	_, _, h := e.do(t, "PUT", "/v1/directory/channels/secret", e.admin, `{"owner":"sub-carol","public":false,"bindings":{}}`, "If-None-Match", "*")
	mal := e.realm.Token("mal", func(c jwt.MapClaims) { c["groups"] = []string{"other"}; c["preferred_username"] = "sub-carol" })
	code, body, _ := e.do(t, "PATCH", "/v1/directory/channels/secret/members", mal, `{"add":["x"]}`, "If-Match", h.Get("ETag"))
	if code != 403 && code != 404 {
		t.Fatalf("patch by a look-alike: HTTP %d: %s", code, body)
	}
	code, body, _ = e.do(t, "GET", "/v1/directory/channels/secret", mal, "")
	want(t, "get by a look-alike", code, 404, body)
	code, body, _ = e.do(t, "GET", "/v1/directory/channels", mal, "")
	want(t, "list", code, 200, body)
	if strings.TrimSpace(body) != "[]" {
		t.Fatalf("the look-alike lists %s", body)
	}
}

func TestTheAdminGroupMatchIsExact(t *testing.T) {
	e := setup(t, Config{Burst: 100})
	g := testrealm.Group
	for _, groups := range [][]string{{"DIR-" + g}, {g + "-x"}, {"x-" + g}, {strings.ToUpper(g)}, {"//" + g}, {"/" + g + "/"}, {"x/" + g}} {
		tok := e.realm.Token("bob", func(c jwt.MapClaims) { c["groups"] = groups })
		code, body, _ := e.do(t, "PUT", "/v1/directory/channels/x", tok, `{}`, "If-None-Match", "*")
		want(t, "groups "+strings.Join(groups, ","), code, 403, body)
	}
}

func TestAMemberReadsAPrivateChannel(t *testing.T) {
	e := setup(t, Config{Burst: 100})
	_, _, h := e.do(t, "PUT", "/v1/directory/channels/secret", e.admin, `{"owner":"sub-carol","public":false}`, "If-None-Match", "*")
	code, body, _ := e.do(t, "PATCH", "/v1/directory/channels/secret/members", e.carol, `{"add":["sub-bob"]}`, "If-Match", h.Get("ETag"))
	want(t, "add", code, 200, body)
	code, body, _ = e.do(t, "GET", "/v1/directory/channels/secret", e.bob, "")
	want(t, "member reads", code, 200, body)
}

func TestAnIDTokenIsRefusedEverywhere(t *testing.T) {
	e := setup(t, Config{Burst: 100})
	id := e.realm.Token("alice", func(c jwt.MapClaims) { c["typ"] = "ID" })
	for _, r := range [][2]string{
		{"GET", "/v1/directory/channels"}, {"GET", "/v1/directory/channels/x"}, {"PUT", "/v1/directory/channels/x"},
		{"PATCH", "/v1/directory/channels/x/members"}, {"POST", "/v1/directory/import"},
		{"GET", "/v1/directory/pending"}, {"POST", "/v1/directory/pending/x/confirm"},
	} {
		code, body, _ := e.do(t, r[0], r[1], id, `{}`, "If-None-Match", "*", "If-Match", `"1"`)
		want(t, r[0]+" "+r[1], code, 401, body)
	}
}

// RT-6.40 step 9, issue #59: the account index's admin API.
func TestAccountEndpoints(t *testing.T) {
	var logs bytes.Buffer
	e := setup(t, Config{Burst: 100, Log: slog.New(slog.NewTextHandler(&logs, nil))})
	for _, sub := range []string{"sub-a", "sub-b"} {
		if _, err := e.store.Login(ctx(t), sub, "x", ""); err != nil {
			t.Fatal(err)
		}
	}
	const path = "/v1/directory/accounts/mattermost/alice"
	code, body, _ := e.do(t, "GET", path, e.bob, "")
	want(t, "not an admin", code, 403, body)
	code, body, _ = e.do(t, "PUT", path, e.bob, `{"sub":"sub-a"}`, "If-None-Match", "*")
	want(t, "not an admin, put", code, 403, body)
	code, body, _ = e.do(t, "DELETE", path, e.bob, "", "If-Match", `"1"`)
	want(t, "not an admin, delete", code, 403, body)
	code, body, _ = e.do(t, "GET", path, e.admin, "")
	want(t, "missing", code, 404, body)
	code, body, _ = e.do(t, "GET", "/v1/directory/accounts/mattermost/Bad%20Name", e.admin, "")
	want(t, "bad name", code, 400, body)
	code, body, _ = e.do(t, "PUT", path, e.admin, `{"sub":"sub-a"}`)
	want(t, "no precondition", code, 428, body)
	code, body, _ = e.do(t, "PUT", path, e.admin, `{"sub":"sub-nobody"}`, "If-None-Match", "*")
	want(t, "unknown identity", code, 404, body)
	if !strings.Contains(body, "no identity") {
		t.Fatalf("unclear message: %s", body)
	}
	code, body, h := e.do(t, "PUT", path, e.admin, `{"sub":"sub-a"}`, "If-None-Match", "*")
	want(t, "create", code, 200, body)
	tag := h.Get("ETag")
	var v struct {
		Product, Name, Sub, MovedFrom string
		Revision                      uint64
	}
	_ = json.Unmarshal([]byte(body), &v)
	if tag == "" || v.Product != "mattermost" || v.Name != "alice" || v.Sub != "sub-a" || v.Revision == 0 || v.MovedFrom != "" {
		t.Fatalf("create answered %s (ETag %q)", body, tag)
	}
	code, body, _ = e.do(t, "PUT", path, e.admin, `{"sub":"sub-b"}`, "If-None-Match", "*")
	want(t, "create twice", code, 409, body)
	code, body, _ = e.do(t, "PUT", path, e.admin, `{"sub":"sub-b"}`, "If-Match", `"999999"`)
	want(t, "stale replace", code, 409, body)
	code, body, h = e.do(t, "GET", path, e.admin, "")
	want(t, "get", code, 200, body)
	if h.Get("ETag") != tag {
		t.Fatalf("ETag %q, want %q", h.Get("ETag"), tag)
	}
	code, body, h = e.do(t, "PUT", path, e.admin, `{"sub":"sub-b"}`, "If-Match", tag)
	want(t, "replace", code, 200, body)
	if !strings.Contains(body, `"moved_from":"sub-a"`) || !strings.Contains(body, `"sub":"sub-b"`) {
		t.Fatalf("replace answered %s", body)
	}
	if id, ok, _ := e.store.ByAccount(ctx(t), "mattermost", "alice"); !ok || id.Sub != "sub-b" {
		t.Fatalf("after replace: %+v", id)
	}
	for _, w := range []string{"account set", "name=alice", "old_sub=sub-a", "new_sub=sub-b"} {
		if !strings.Contains(logs.String(), w) {
			t.Errorf("log lacks %q: %s", w, logs.String())
		}
	}
	code, body, _ = e.do(t, "GET", path, e.admin, "")
	want(t, "get moved", code, 200, body)
	if !strings.Contains(body, `"moved_from":"sub-a"`) {
		t.Fatalf("get lacks moved_from: %s", body)
	}
	code, body, _ = e.do(t, "DELETE", path, e.admin, "")
	want(t, "delete without precondition", code, 428, body)
	code, body, _ = e.do(t, "DELETE", path, e.admin, "", "If-Match", tag)
	want(t, "delete over an old revision", code, 409, body)
	code, body, h = e.do(t, "GET", path, e.admin, "")
	want(t, "get again", code, 200, body)
	code, body, _ = e.do(t, "DELETE", path, e.admin, "", "If-Match", h.Get("ETag"))
	want(t, "delete", code, 204, body)
	if !strings.Contains(logs.String(), "account removed") {
		t.Errorf("no log of the delete: %s", logs.String())
	}
	code, body, _ = e.do(t, "GET", path, e.admin, "")
	want(t, "gone", code, 404, body)
}

// RT-6.40 step 9: an admin's PUT whose records do not all follow the key is
// logged as incomplete, with both subs.
func TestAnIncompleteAccountPutIsLogged(t *testing.T) {
	var logs bytes.Buffer
	e := setup(t, Config{Burst: 100, Log: slog.New(slog.NewTextHandler(&logs, nil))})
	for _, sub := range []string{"sub-a", "sub-b"} {
		if _, err := e.store.Login(ctx(t), sub, "x", ""); err != nil {
			t.Fatal(err)
		}
	}
	const path = "/v1/directory/accounts/mattermost/alice"
	code, body, h := e.do(t, "PUT", path, e.admin, `{"sub":"sub-a"}`, "If-None-Match", "*")
	want(t, "create", code, 200, body)
	t.Cleanup(directory.SetAccountStepForTest(func(at string) error {
		if at == "key moved" {
			return errors.New("stopped")
		}
		return nil
	}))
	code, body, _ = e.do(t, "PUT", path, e.admin, `{"sub":"sub-b"}`, "If-Match", h.Get("ETag"))
	if code < 500 {
		t.Fatalf("the stopped PUT answered %d: %s", code, body)
	}
	for _, w := range []string{"account set, incomplete", "old_sub=sub-a", "new_sub=sub-b"} {
		if !strings.Contains(logs.String(), w) {
			t.Errorf("log lacks %q: %s", w, logs.String())
		}
	}
}
