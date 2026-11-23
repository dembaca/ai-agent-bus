package dircli

import (
	"bytes"
	"encoding/json"
	"encoding/pem"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"
)

const secretToken = "tok-SECRET-0123456789"

type rec struct {
	Method, Path, Auth, IfMatch, IfNoneMatch, Body string
}

type rig struct {
	t      *testing.T
	kc     *httptest.Server
	api    *httptest.Server
	mu     sync.Mutex
	calls  []rec
	tokens []string // token endpoint answers, in order; the last repeats
	dev    map[string]any
	pend   string
	sleeps []time.Duration
	clock  time.Time
	home   string
}

func newRig(t *testing.T) *rig {
	t.Helper()
	r := &rig{t: t, clock: time.Unix(1_700_000_000, 0), home: t.TempDir()}
	t.Setenv("HOME", r.home)
	t.Chdir(r.home)
	r.tokens = []string{"ok"}
	r.pend = `[{"name":"alice","channels":["dev"],"confirmed":false,"revision":7}]`
	var kcURL string
	kmux := http.NewServeMux()
	kmux.HandleFunc("/.well-known/openid-configuration", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]string{"issuer": kcURL,
			"device_authorization_endpoint": kcURL + "/device", "token_endpoint": kcURL + "/token"})
	})
	kmux.HandleFunc("/device", func(w http.ResponseWriter, req *http.Request) {
		_ = req.ParseForm()
		if req.Form.Get("client_id") != "authd-cli" || req.Form.Get("scope") != "openid" {
			http.Error(w, "{}", http.StatusBadRequest)
			return
		}
		d := map[string]any{"device_code": "dc", "user_code": "ABCD-EFGH", "verification_uri": kcURL + "/activate",
			"verification_uri_complete": kcURL + "/activate?c=ABCD-EFGH", "expires_in": 600, "interval": 1}
		for k, v := range r.dev {
			d[k] = v
		}
		_ = json.NewEncoder(w).Encode(d)
	})
	kmux.HandleFunc("/token", func(w http.ResponseWriter, req *http.Request) {
		_ = req.ParseForm()
		if req.Form.Get("device_code") != "dc" || req.Form.Get("grant_type") != "urn:ietf:params:oauth:grant-type:device_code" {
			http.Error(w, "{}", http.StatusBadRequest)
			return
		}
		r.mu.Lock()
		a := r.tokens[0]
		if len(r.tokens) > 1 {
			r.tokens = r.tokens[1:]
		}
		r.mu.Unlock()
		if a == "ok" {
			_ = json.NewEncoder(w).Encode(map[string]string{"access_token": secretToken, "token_type": "Bearer"})
			return
		}
		w.WriteHeader(http.StatusBadRequest)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": a})
	})
	r.kc = httptest.NewServer(kmux)
	kcURL = r.kc.URL
	t.Cleanup(r.kc.Close)
	r.api = httptest.NewServer(http.HandlerFunc(r.serveAPI))
	t.Cleanup(r.api.Close)
	return r
}

func (r *rig) serveAPI(w http.ResponseWriter, req *http.Request) {
	b, _ := io.ReadAll(req.Body)
	r.mu.Lock()
	r.calls = append(r.calls, rec{req.Method, req.URL.EscapedPath(), req.Header.Get("Authorization"),
		req.Header.Get("If-Match"), req.Header.Get("If-None-Match"), string(b)})
	r.mu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	p := req.URL.EscapedPath()
	chJSON := `{"name":"dev team","owner":"u1","public":false,"members":["m1","m2"],"bindings":{"mattermost":"dev"}}`
	switch {
	case p == "/v1/directory/channels":
		_, _ = io.WriteString(w, `[`+chJSON+`]`)
	case strings.HasPrefix(p, "/v1/directory/channels/conflict"):
		w.Header().Set("ETag", `"3"`)
		if req.Method == "GET" {
			_, _ = io.WriteString(w, chJSON)
			return
		}
		w.WriteHeader(http.StatusConflict)
		_, _ = io.WriteString(w, `{"error":"changed meanwhile; read again"}`)
	case strings.HasPrefix(p, "/v1/directory/channels/missing"):
		w.WriteHeader(http.StatusNotFound)
		_, _ = io.WriteString(w, `{"error":"no such channel"}`)
	case strings.HasPrefix(p, "/v1/directory/channels/"):
		w.Header().Set("ETag", `"5"`)
		_, _ = io.WriteString(w, chJSON)
	case p == "/v1/directory/import":
		_, _ = io.WriteString(w, `{"channels_created":["a","b"],"channels_updated":[],"channels_skipped":[{"name":"x","reason":"bad name"}],`+
			`"identities":3,"members_added":4,"pending_added":1,"accounts_skipped":[{"username":"bob","reason":"password account"}],"next":"set AUTH_DIRECTORY_IMPORTED=true and restart"}`)
	case p == "/v1/directory/pending":
		_, _ = io.WriteString(w, r.pend)
	case strings.HasSuffix(p, "/confirm"):
		_, _ = io.WriteString(w, `{"confirmed":"alice"}`)
	default:
		w.WriteHeader(http.StatusNotFound)
		_, _ = io.WriteString(w, `{"error":"nope"}`)
	}
}

func (r *rig) deps() deps {
	return deps{
		now:   func() time.Time { return r.clock },
		sleep: func(d time.Duration) { r.sleeps = append(r.sleeps, d); r.clock = r.clock.Add(d) },
	}
}

func (r *rig) env(extra map[string]string) func(string) string {
	m := map[string]string{"AUTHD_DIR_API": r.api.URL, "AUTH_KEYCLOAK_ISSUER": r.kc.URL, "AUTH_KEYCLOAK_CLIENT_ID": "authd-cli"}
	for k, v := range extra {
		m[k] = v
	}
	return func(k string) string { return m[k] }
}

// do runs the command line and checks the secrets stay out of the output and
// the disk.
func (r *rig) do(args ...string) (int, string, string) {
	r.t.Helper()
	var out, errb bytes.Buffer
	code := run(args, r.env(nil), &out, &errb, r.deps())
	for _, s := range []string{out.String(), errb.String()} {
		if strings.Contains(s, secretToken) {
			r.t.Fatalf("the token reached the output: %q", s)
		}
	}
	if es, _ := os.ReadDir(r.home); len(es) != 0 {
		r.t.Fatalf("files were created: %v", es)
	}
	return code, out.String(), errb.String()
}

func (r *rig) last() rec {
	r.t.Helper()
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.calls) == 0 {
		r.t.Fatal("no API call")
	}
	return r.calls[len(r.calls)-1]
}

func TestDeviceFlowPollsWithInterval(t *testing.T) {
	r := newRig(t)
	r.tokens = []string{"authorization_pending", "authorization_pending", "slow_down", "ok"}
	code, out, errs := r.do("ls")
	if code != 0 {
		t.Fatalf("exit %d: %s", code, errs)
	}
	want := []time.Duration{time.Second, time.Second, time.Second, 6 * time.Second}
	if !reflect.DeepEqual(r.sleeps, want) {
		t.Fatalf("sleeps %v, want %v", r.sleeps, want)
	}
	if !strings.Contains(errs, "Open "+r.kc.URL+"/activate?c=ABCD-EFGH and confirm code ABCD-EFGH") {
		t.Fatalf("stderr: %q", errs)
	}
	if !strings.Contains(out, "dev team\tprivate\tu1\t2 members") {
		t.Fatalf("stdout: %q", out)
	}
	if got := r.last(); got.Auth != "Bearer "+secretToken || got.Method != "GET" || got.Path != "/v1/directory/channels" {
		t.Fatalf("call: %+v", got)
	}
}

func TestDeviceFlowEnds(t *testing.T) {
	for _, tc := range []struct{ answer, msg string }{
		{"access_denied", "declined"}, {"expired_token", "expired"}} {
		r := newRig(t)
		r.tokens = []string{tc.answer}
		code, _, errs := r.do("ls")
		if code != 1 || !strings.Contains(errs, tc.msg) {
			t.Fatalf("%s: exit %d, %q", tc.answer, code, errs)
		}
		if len(r.calls) != 0 {
			t.Fatal("the API was called without a token")
		}
	}
}

func TestDeviceFlowExpiresByClock(t *testing.T) {
	r := newRig(t)
	r.dev = map[string]any{"expires_in": 3}
	r.tokens = []string{"authorization_pending"}
	code, _, errs := r.do("ls")
	if code != 1 || !strings.Contains(errs, "expired") {
		t.Fatalf("exit %d, %q", code, errs)
	}
}

func TestVerificationURIOnOtherHostRefused(t *testing.T) {
	for _, key := range []string{"verification_uri", "verification_uri_complete"} {
		r := newRig(t)
		r.dev = map[string]any{key: "http://evil.example/activate"}
		code, _, errs := r.do("ls")
		if code != 1 || !strings.Contains(errs, "not on the issuer's host") {
			t.Fatalf("%s: exit %d, %q", key, code, errs)
		}
		if len(r.sleeps) != 0 {
			t.Fatal("polled after a refused link")
		}
	}
}

func TestCommands(t *testing.T) {
	cases := []struct {
		name   string
		args   []string
		method string
		path   string
		ifm    string
		ifnm   string
		body   string // "" = none
	}{
		{"show", []string{"show", "dev team"}, "GET", "/v1/directory/channels/dev%20team", "", "", ""},
		{"create", []string{"create", "a/b", "--owner", "u9", "--public"}, "PUT", "/v1/directory/channels/a%2Fb", "", "*", `{"owner":"u9","public":true,"bindings":null}`},
		{"owner", []string{"owner", "dev team", "u2"}, "PUT", "/v1/directory/channels/dev%20team", `"5"`, "", `{"owner":"u2","public":false,"bindings":{"mattermost":"dev"}}`},
		{"owner clear", []string{"owner", "dev team", "-"}, "PUT", "/v1/directory/channels/dev%20team", `"5"`, "", `{"owner":"","public":false,"bindings":{"mattermost":"dev"}}`},
		{"public", []string{"public", "dev team", "on"}, "PUT", "/v1/directory/channels/dev%20team", `"5"`, "", `{"owner":"u1","public":true,"bindings":{"mattermost":"dev"}}`},
		{"members add", []string{"members", "add", "dev team", "s1", "s2"}, "PATCH", "/v1/directory/channels/dev%20team/members", `"5"`, "", `{"add":["s1","s2"],"remove":[]}`},
		{"members rm", []string{"members", "rm", "dev team", "m1"}, "PATCH", "/v1/directory/channels/dev%20team/members", `"5"`, "", `{"add":[],"remove":["m1"]}`},
		{"import", []string{"import"}, "POST", "/v1/directory/import", "", "", ""},
		{"pending", []string{"pending"}, "GET", "/v1/directory/pending", "", "", ""},
		{"confirm", []string{"confirm", "alice"}, "POST", "/v1/directory/pending/alice/confirm", `"7"`, "", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := newRig(t)
			code, out, errs := r.do(tc.args...)
			if code != 0 {
				t.Fatalf("exit %d: %s", code, errs)
			}
			got := r.last()
			want := rec{tc.method, tc.path, "Bearer " + secretToken, tc.ifm, tc.ifnm, tc.body}
			if got != want {
				t.Fatalf("got  %+v\nwant %+v", got, want)
			}
			if tc.name == "import" {
				for _, s := range []string{"created 2", "skipped channel x: bad name", "skipped account bob: password account", "next: set AUTH_DIRECTORY_IMPORTED=true"} {
					if !strings.Contains(out, s) {
						t.Fatalf("report lacks %q: %q", s, out)
					}
				}
			}
			if tc.name == "pending" && !strings.Contains(out, "alice\tdev\tconfirmed=false\trevision 7") {
				t.Fatalf("pending: %q", out)
			}
			if tc.name == "show" && !strings.Contains(out, "revision 5") {
				t.Fatalf("show: %q", out)
			}
		})
	}
}

func TestJSONOutput(t *testing.T) {
	r := newRig(t)
	code, out, _ := r.do("--json", "pending")
	if code != 0 || strings.TrimSpace(out) != r.pend {
		t.Fatalf("exit %d: %q", code, out)
	}
}

func TestConfirmUnknownName(t *testing.T) {
	r := newRig(t)
	code, _, errs := r.do("confirm", "zed")
	if code != 1 || !strings.Contains(errs, "not in the pending list") {
		t.Fatalf("exit %d: %q", code, errs)
	}
	if got := r.last(); got.Method != "GET" {
		t.Fatalf("a confirm was sent: %+v", got)
	}
}

func TestConflictAndErrors(t *testing.T) {
	r := newRig(t)
	code, _, errs := r.do("owner", "conflict", "u2")
	if code != 1 || !strings.Contains(errs, "changed meanwhile; run again") {
		t.Fatalf("exit %d: %q", code, errs)
	}
	code, _, errs = r.do("show", "missing")
	if code != 1 || !strings.Contains(errs, "no such channel (HTTP 404)") {
		t.Fatalf("exit %d: %q", code, errs)
	}
}

func TestUsageErrors(t *testing.T) {
	r := newRig(t)
	for _, args := range [][]string{{}, {"nope"}, {"show"}, {"public", "x", "maybe"}, {"members", "add", "x"}, {"ls", "--public"}, {"--bogus", "ls"}} {
		if code, _, _ := r.do(args...); code != 2 {
			t.Fatalf("%v: exit %d", args, code)
		}
	}
	if code, out, _ := r.do("-h"); code != 0 || !strings.Contains(out, "usage: authd dir") {
		t.Fatalf("-h: exit %d %q", code, out)
	}
	var out, errb bytes.Buffer
	if code := run([]string{"ls"}, func(string) string { return "" }, &out, &errb, r.deps()); code != 2 {
		t.Fatalf("no settings: exit %d", code)
	}
}

func TestFlagsOverrideEnv(t *testing.T) {
	r := newRig(t)
	var out, errb bytes.Buffer
	env := r.env(map[string]string{"AUTHD_DIR_API": "http://127.0.0.1:1"})
	code := run([]string{"--api", r.api.URL, "--issuer=" + r.kc.URL, "--client-id", "authd-cli", "ls"}, env, &out, &errb, r.deps())
	if code != 0 {
		t.Fatalf("exit %d: %s", code, errb.String())
	}
}

func TestPlainHTTPToNonLoopbackRefused(t *testing.T) {
	r := newRig(t)
	for _, api := range []string{"http://api.example.com", "http://localhost:8080", "ftp://127.0.0.1"} {
		var out, errb bytes.Buffer
		code := run([]string{"--api", api, "ls"}, r.env(nil), &out, &errb, r.deps())
		if code != 2 || !strings.Contains(errb.String(), "https") {
			t.Fatalf("%s: exit %d %q", api, code, errb.String())
		}
	}
	if len(r.sleeps) != 0 {
		t.Fatal("logged in before refusing")
	}
}

func TestHTTPSWithCAFile(t *testing.T) {
	r := newRig(t)
	tls := httptest.NewTLSServer(http.HandlerFunc(r.serveAPI))
	defer tls.Close()
	caPath := filepath.Join(t.TempDir(), "ca.pem")
	pemBytes := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: tls.Certificate().Raw})
	if err := os.WriteFile(caPath, pemBytes, 0o600); err != nil {
		t.Fatal(err)
	}
	var out, errb bytes.Buffer
	args := []string{"--api", tls.URL, "--ca-file", caPath, "ls"}
	if code := run(args, r.env(nil), &out, &errb, r.deps()); code != 0 {
		t.Fatalf("with CA: exit %d %s", code, errb.String())
	}
	out.Reset()
	errb.Reset()
	if code := run([]string{"--api", tls.URL, "ls"}, r.env(nil), &out, &errb, r.deps()); code != 1 {
		t.Fatalf("without CA: exit %d", code)
	}
}

// Text a server sends reaches the terminal without control characters.
func TestServerTextIsCleanedBeforePrinting(t *testing.T) {
	if got := clean("ok\x1b[2J\x1b]0;pwned\x07\r\nfake line\x7f\u0085"); got != "ok[2J]0;pwnedfake line" {
		t.Fatalf("%q", got)
	}
	if got := clean(strings.Repeat("a", 600)); len(got) != 512 {
		t.Fatal(len(got))
	}
}
