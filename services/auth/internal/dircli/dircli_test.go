package dircli

import (
	"bytes"
	"crypto/tls"
	"encoding/json"
	"encoding/pem"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"
	"unicode/utf8"
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

	disc          map[string]string // overrides in the discovery document
	body          map[string]string // exact API path -> JSON answer
	etag          string
	tokenRedirect string
	apiRedirect   string
	tokenFails    int
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
		d := map[string]string{"issuer": kcURL,
			"device_authorization_endpoint": kcURL + "/device", "token_endpoint": kcURL + "/token"}
		for k, v := range r.disc {
			d[k] = v
		}
		_ = json.NewEncoder(w).Encode(d)
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
		if r.tokenRedirect != "" {
			http.Redirect(w, req, r.tokenRedirect, http.StatusFound)
			return
		}
		r.mu.Lock()
		fail := r.tokenFails > 0
		if fail {
			r.tokenFails--
		}
		r.mu.Unlock()
		if fail {
			w.WriteHeader(http.StatusBadGateway)
			return
		}
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
	if r.apiRedirect != "" {
		http.Redirect(w, req, r.apiRedirect, http.StatusFound)
		return
	}
	if b, ok := r.body[p]; ok {
		if r.etag != "" {
			w.Header().Set("ETag", r.etag)
		}
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, b)
		return
	}
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
	case req.Method == "DELETE" && strings.HasPrefix(p, "/v1/directory/pending/"):
		_, _ = io.WriteString(w, `{"dropped":"alice"}`)
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

// --- review fixes ---

func mustURL(t *testing.T, raw string) *url.URL {
	t.Helper()
	u, err := url.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	return u
}

func TestSameSite(t *testing.T) {
	https := mustURL(t, "https://login.example.com")
	httpU := mustURL(t, "http://127.0.0.1:8080")
	for _, tc := range []struct {
		issuer *url.URL
		raw    string
		ok     bool
	}{
		{https, "https://login.example.com/activate", true},
		{https, "https://login.example.com:443/activate", true},
		{https, "https://LOGIN.example.com/activate", true},
		{mustURL(t, "http://login.example.com:80"), "http://login.example.com/x", true},
		{https, "https://login.example.com:8443/activate", false},
		{https, "http://login.example.com/activate", false},
		{https, "http://login.example.com:443/activate", false},
		{https, "https://login.evil.example@login.example.com/activate", false},
		{https, "https://user:pw@login.example.com/activate", false},
		{https, "https://other.example.com/activate", false},
		{httpU, "http://127.0.0.1:1/activate", false},
		{httpU, "https://127.0.0.1:8080/activate", false},
		{httpU, "http://127.0.0.1:8080/activate", true},
		{httpU, "/activate", false},
	} {
		err := sameSite(tc.issuer, tc.raw, "x")
		if (err == nil) != tc.ok {
			t.Errorf("%s vs %s: err %v, want ok=%v", tc.issuer, tc.raw, err, tc.ok)
		}
	}
}

func TestLoginRefusesOtherSites(t *testing.T) {
	r := newRig(t)
	host := strings.TrimPrefix(r.kc.URL, "http://")
	for name, tc := range map[string]struct {
		disc map[string]string
		dev  map[string]any
	}{
		"verification uri other port":    {dev: map[string]any{"verification_uri": "http://127.0.0.1:1/activate", "verification_uri_complete": ""}},
		"verification uri complete port": {dev: map[string]any{"verification_uri_complete": "http://127.0.0.1:1/activate?c=1"}},
		"verification uri userinfo":      {dev: map[string]any{"verification_uri": "http://login.evil.example@" + host + "/activate"}},
		"complete userinfo":              {dev: map[string]any{"verification_uri_complete": "http://login.evil.example@" + host + "/a"}},
		"verification uri scheme":        {dev: map[string]any{"verification_uri": "https://" + host + "/activate"}},
		"token endpoint other port":      {disc: map[string]string{"token_endpoint": "http://127.0.0.1:1/token"}},
		"token endpoint scheme":          {disc: map[string]string{"token_endpoint": "https://" + host + "/token"}},
		"token endpoint userinfo":        {disc: map[string]string{"token_endpoint": "http://x@" + host + "/token"}},
		"device endpoint other port":     {disc: map[string]string{"device_authorization_endpoint": "http://127.0.0.1:1/device"}},
		"device endpoint other host":     {disc: map[string]string{"device_authorization_endpoint": "http://localhost:" + strings.Split(host, ":")[1] + "/device"}},
		"issuer differs":                 {disc: map[string]string{"issuer": "http://127.0.0.1:1"}},
	} {
		t.Run(name, func(t *testing.T) {
			r.disc, r.dev, r.sleeps = tc.disc, tc.dev, nil
			code, _, errs := r.do("ls")
			if code != 1 {
				t.Fatalf("exit %d: %q", code, errs)
			}
			if len(r.sleeps) != 0 || len(r.calls) != 0 {
				t.Fatalf("went on: sleeps %v calls %v", r.sleeps, r.calls)
			}
		})
	}
}

func TestTokenEndpointNeverPostedWhenRefused(t *testing.T) {
	r := newRig(t)
	var hits int
	other := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { hits++ }))
	defer other.Close()
	r.disc = map[string]string{"token_endpoint": other.URL + "/token"}
	if code, _, _ := r.do("ls"); code != 1 || hits != 0 {
		t.Fatalf("exit %d, hits %d", code, hits)
	}
}

const dirt = "ev\x1b[2Jil\u202e\u2028\u2029\u200f\u2066x\u0085"

func noDirt(t *testing.T, what, s string) {
	t.Helper()
	for _, bad := range []string{"\x1b", "\u202e", "\u2028", "\u2029", "\u200f", "\u2066", "\u0085"} {
		if strings.Contains(s, bad) {
			t.Fatalf("%s: %q reaches the terminal in %q", what, bad, s)
		}
	}
}

func jsonOf(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func TestServerTextIsCleaned(t *testing.T) {
	ch := map[string]any{"name": dirt, "owner": dirt, "public": true, "members": []string{dirt, "m2"}}
	imp := map[string]any{"channels_created": []string{dirt}, "channels_updated": []string{},
		"channels_skipped": []map[string]string{{"name": dirt, "reason": dirt}},
		"accounts_skipped": []map[string]string{{"username": dirt, "reason": dirt}}, "next": dirt}
	pend := []map[string]any{{"name": dirt, "channels": []string{dirt}, "confirmed": false, "revision": 1}}
	r := newRig(t)
	r.etag = "\"5\u202e\""
	r.body = map[string]string{
		"/v1/directory/channels":           "[" + jsonOf(t, ch) + "]",
		"/v1/directory/channels/dev":       jsonOf(t, ch),
		"/v1/directory/import":             jsonOf(t, imp),
		"/v1/directory/pending":            jsonOf(t, pend),
		"/v1/directory/pending/ev%1B%5B2J": "{}",
	}
	for _, args := range [][]string{{"ls"}, {"show", "dev"}, {"import"}, {"pending"}} {
		code, out, errs := r.do(args...)
		if code != 0 {
			t.Fatalf("%v: exit %d %s", args, code, errs)
		}
		noDirt(t, args[0], out)
		if !strings.Contains(out, "ev[2Jilx") && args[0] != "import" || args[0] == "import" && !strings.Contains(out, "next: ev[2Jilx") {
			t.Fatalf("%v: cleaned text missing: %q", args, out)
		}
	}
	_, out, _ := r.do("show", "dev")
	if !strings.Contains(out, "revision 5\n") {
		t.Fatalf("revision not cleaned: %q", out)
	}
	_, out, _ = r.do("--json", "pending")
	if !strings.Contains(out, `\u001b`) && !strings.Contains(out, "\\u001b") {
		t.Fatalf("--json should stay raw: %q", out)
	}
}

func TestCleanDropsCategories(t *testing.T) {
	in := "a\u200eb\u202ac\u202ed\u2066e\u2069f\u2028g\u2029h\ti\x7fj\u00adk\u200bl"
	if got := clean(in); got != "abcdefghijkl" {
		t.Fatalf("clean: %q", got)
	}
	long := clean(strings.Repeat("\u00e4", 400))
	if len(long) > 512 || !utf8.ValidString(long) {
		t.Fatalf("clean long: %d bytes, valid %v", len(long), utf8.ValidString(long))
	}
}

func TestUserCodeAndAPIErrorCleaned(t *testing.T) {
	r := newRig(t)
	r.dev = map[string]any{"user_code": "AB\x1b[2JCD\u202e"}
	code, _, errs := r.do("ls")
	if code != 0 {
		t.Fatalf("exit %d %s", code, errs)
	}
	noDirt(t, "stderr", errs)
	if !strings.Contains(errs, "confirm code AB[2JCD\n") {
		t.Fatalf("stderr: %q", errs)
	}
	// API error text
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		_, _ = io.WriteString(w, jsonOf(t, map[string]string{"error": dirt}))
	}))
	defer api.Close()
	var out, eb bytes.Buffer
	code = run([]string{"--api", api.URL, "ls"}, r.env(nil), &out, &eb, r.deps())
	if code != 1 || !strings.Contains(eb.String(), "(HTTP 403)") {
		t.Fatalf("exit %d: %q", code, eb.String())
	}
	noDirt(t, "api error", eb.String())
	// OAuth error code
	r2 := newRig(t)
	r2.tokens = []string{"bad\x1b[2J_code"}
	_, _, errs = r2.do("ls")
	noDirt(t, "oauth error", errs)
}

func TestRedirectsNotFollowed(t *testing.T) {
	var mu sync.Mutex
	var seen []string
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		mu.Lock()
		seen = append(seen, req.Method+" "+req.URL.Path+" auth="+req.Header.Get("Authorization"))
		mu.Unlock()
	}))
	defer target.Close()

	r := newRig(t)
	r.apiRedirect = target.URL + "/stolen"
	code, _, errs := r.do("ls")
	if code != 1 || !strings.Contains(errs, "HTTP 302") {
		t.Fatalf("api redirect: exit %d %q", code, errs)
	}
	r2 := newRig(t)
	r2.tokenRedirect = target.URL + "/token"
	if code, _, _ := r2.do("ls"); code != 1 {
		t.Fatalf("token redirect: exit %d", code)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(seen) != 0 {
		t.Fatalf("a redirect was followed: %v", seen)
	}
}

func TestCAFileWithNilTLSConfig(t *testing.T) {
	r := newRig(t)
	srv := httptest.NewTLSServer(http.HandlerFunc(r.serveAPI))
	defer srv.Close()
	caPath := filepath.Join(t.TempDir(), "ca.pem")
	if err := os.WriteFile(caPath, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw}), 0o600); err != nil {
		t.Fatal(err)
	}
	base := &http.Transport{TLSNextProto: map[string]func(string, *tls.Conn) http.RoundTripper{}} // no HTTP/2 setup; TLSClientConfig is nil, as with GODEBUG=http2client=0
	tr, err := apiTransport(base, caPath)
	if err != nil {
		t.Fatal(err)
	}
	if base.TLSClientConfig != nil {
		t.Fatal("the base transport was changed")
	}
	resp, err := newClient(tr).Get(srv.URL + "/v1/directory/channels")
	if err != nil {
		t.Fatalf("with CA: %v", err)
	}
	_ = resp.Body.Close()
	if _, err := apiTransport(base, filepath.Join(t.TempDir(), "none.pem")); err == nil {
		t.Fatal("a missing CA file was accepted")
	}
}

func TestPollIntervalCapped(t *testing.T) {
	r := newRig(t)
	r.dev = map[string]any{"interval": 3600}
	r.tokens = []string{"ok"}
	if code, _, errs := r.do("ls"); code != 0 {
		t.Fatalf("exit %d %s", code, errs)
	}
	if !reflect.DeepEqual(r.sleeps, []time.Duration{30 * time.Second}) {
		t.Fatalf("sleeps %v", r.sleeps)
	}
	// slow_down never lifts the interval over the cap
	r = newRig(t)
	r.dev = map[string]any{"interval": 28}
	r.tokens = []string{"slow_down", "slow_down", "ok"}
	if code, _, errs := r.do("ls"); code != 0 {
		t.Fatalf("exit %d %s", code, errs)
	}
	want := []time.Duration{28 * time.Second, 30 * time.Second, 30 * time.Second}
	if !reflect.DeepEqual(r.sleeps, want) {
		t.Fatalf("sleeps %v, want %v", r.sleeps, want)
	}
}

func TestPollNeverSleepsPastDeadline(t *testing.T) {
	r := newRig(t)
	r.dev = map[string]any{"interval": 3600, "expires_in": 20}
	r.tokens = []string{"authorization_pending"}
	code, _, errs := r.do("ls")
	if code != 1 || !strings.Contains(errs, "expired") {
		t.Fatalf("exit %d %q", code, errs)
	}
	var total time.Duration
	for _, d := range r.sleeps {
		total += d
	}
	if total > 20*time.Second || total == 0 {
		t.Fatalf("slept %v in %v", total, r.sleeps)
	}
}

func TestPollBackoffCapped(t *testing.T) {
	r := newRig(t)
	r.dev = map[string]any{"interval": 20}
	r.tokenFails = 3 // answers 502 three times: 20 s doubles to 40, capped at 30
	if code, _, errs := r.do("ls"); code != 0 {
		t.Fatalf("exit %d %s", code, errs)
	}
	want := []time.Duration{20 * time.Second, 30 * time.Second, 30 * time.Second, 30 * time.Second}
	if !reflect.DeepEqual(r.sleeps, want) {
		t.Fatalf("sleeps %v, want %v", r.sleeps, want)
	}
}

// The printed link is cleaned: a path with a bidi control and a C1 control
// passes url.Parse and the same-site check, and must not reach the terminal.
func TestVerificationURIIsCleanedWhenPrinted(t *testing.T) {
	r := newRig(t)
	r.dev = map[string]any{"verification_uri_complete": r.kc.URL + "/activate/a\u202eb\u0085c"}
	code, _, errs := r.do("ls")
	if code != 0 {
		t.Fatalf("exit %d: %q", code, errs)
	}
	if strings.ContainsAny(errs, "\u202e\u0085") || !strings.Contains(errs, "/activate/abc") {
		t.Fatalf("printed: %q", errs)
	}
}

func TestUserInfoInAPIOrIssuerRefused(t *testing.T) {
	for _, flag := range []string{"--api", "--issuer"} {
		r := newRig(t)
		u := "https://u:p@" + strings.TrimPrefix(r.api.URL, "http://")
		var out, errb bytes.Buffer
		code := run([]string{flag, u, "ls"}, r.env(nil), &out, &errb, r.deps())
		if code != 2 || !strings.Contains(errb.String(), "not a usable URL") {
			t.Fatalf("%s: exit %d, %q", flag, code, errb.String())
		}
		if len(r.calls) != 0 || len(r.sleeps) != 0 || strings.Contains(errb.String(), "u:p") {
			t.Fatalf("%s: contacted or echoed: %v %q", flag, r.calls, errb.String())
		}
	}
}

func TestJSONThatIsNotJSONIsAnError(t *testing.T) {
	r := newRig(t)
	r.body = map[string]string{"/v1/directory/channels": "<html>oops</html>"}
	code, out, errs := r.do("--json", "ls")
	if code != 1 || out != "" || !strings.Contains(errs, "the server's answer is not JSON") {
		t.Fatalf("exit %d, out %q, err %q", code, out, errs)
	}
}

func TestPendingShowsWhatItWaitsOn(t *testing.T) {
	r := newRig(t)
	r.pend = `[{"name":"alice","channels":["crowd"],"confirmed":true,"revision":7,"waiting_full":["cro\u202ewd"]}]`
	code, out, _ := r.do("pending")
	if code != 0 || !strings.Contains(out, "waiting on full channel(s): crowd") || strings.Contains(out, "\u202e") {
		t.Fatalf("exit %d: %q", code, out)
	}
}

func TestDrop(t *testing.T) {
	r := newRig(t)
	code, out, errs := r.do("drop", "alice")
	if code != 0 || !strings.Contains(out, "dropped alice") {
		t.Fatalf("exit %d: %q %q", code, out, errs)
	}
	if got := r.last(); got.Method != "DELETE" || got.Path != "/v1/directory/pending/alice" || got.IfMatch != `"7"` {
		t.Fatalf("%+v", got)
	}
	r = newRig(t)
	code, _, errs = r.do("drop", "zed")
	if code != 1 || !strings.Contains(errs, "not in the pending list") {
		t.Fatalf("exit %d: %q", code, errs)
	}
	if got := r.last(); got.Method != "GET" {
		t.Fatalf("a delete was sent: %+v", got)
	}
}

// The name printed after a drop is cleaned like every server text.
func TestDropPrintsTheNameClean(t *testing.T) {
	r := newRig(t)
	r.pend = `[{"name":"al\u001b[2Jice‮","channels":["dev"],"confirmed":false,"revision":7}]`
	code, out, errs := r.do("drop", "al\x1b[2Jice‮")
	if code != 0 {
		t.Fatalf("exit %d: %q %q", code, out, errs)
	}
	if strings.ContainsAny(out, "\x1b‮") || !strings.Contains(out, "dropped al[2Jice") {
		t.Fatalf("not clean: %q", out)
	}
}
