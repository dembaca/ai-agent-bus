package admin

import (
	"context"
	"encoding/json"
	"io"
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
	realm := testrealm.New(t)
	v := realm.Verifier(testrealm.NewChat("alice", "bob", "carol"))
	store := newStore(t)
	cfg.AdminGroup = testrealm.Group
	h, err := New(cfg, v, store, &fakeMM{})
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
	want(t, "other", code, 403, body)
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
		want(t, "no owner, "+name, code, 403, body)
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
