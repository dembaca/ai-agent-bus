package mattermost

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
)

const adminToken = "s3cret-admin-token"

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// meServer answers GET /api/v4/users/me with a user when the bearer is the
// admin token, and counts the requests.
func meServer(t *testing.T, hits *atomic.Int32) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v4/users/me", func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		if r.Header.Get("Authorization") != "Bearer "+adminToken {
			http.Error(w, "no", http.StatusUnauthorized)
			return
		}
		_ = json.NewEncoder(w).Encode(User{ID: "admin-id", Username: "admin"})
	})
	ts := httptest.NewServer(mux)
	t.Cleanup(ts.Close)
	return ts
}

// #135: an http URL to a host that is not loopback is refused, at New and
// at first use of an adapter built without New, and the token never shows.
func TestPlainHTTPToRemoteHostRefused(t *testing.T) {
	cfg := Config{URL: "http://user:pw@chat.example", Token: adminToken, Team: "team-a"}
	_, err := New(cfg, quiet())
	if err == nil {
		t.Fatal("New: http to chat.example accepted")
	}
	m := &Adapter{Cfg: cfg, Client: &http.Client{Transport: failTransport{t}}, Log: quiet()}
	_, err2 := m.AgentForCredential(context.Background(), adminToken)
	if err2 == nil {
		t.Fatal("do: http to chat.example accepted")
	}
	for _, e := range []error{err, err2} {
		if s := e.Error(); strings.Contains(s, adminToken) || strings.Contains(s, "pw") ||
			!strings.Contains(s, "not loopback") {
			t.Errorf("error %q: wants the reason, never the token or password", s)
		}
	}
	if _, err := New(Config{URL: "ftp://chat.example", Token: adminToken}, quiet()); err == nil {
		t.Error("New: ftp accepted")
	}
}

type failTransport struct{ t *testing.T }

func (f failTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	f.t.Errorf("request sent to %s", r.URL.Redacted())
	return nil, errors.New("no network in tests")
}

func TestPlainHTTPToLoopbackAllowed(t *testing.T) {
	var hits atomic.Int32
	ts := meServer(t, &hits) // http://127.0.0.1:<port>
	m, err := New(Config{URL: ts.URL, Token: adminToken}, quiet())
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if _, err := m.AgentForCredential(context.Background(), adminToken); err != nil {
		t.Fatalf("call: %v", err)
	}
	if hits.Load() != 1 {
		t.Fatalf("hits: got %d want 1", hits.Load())
	}
	u, _ := url.Parse(ts.URL)
	if _, err := New(Config{URL: "http://localhost:" + u.Port(), Token: adminToken}, quiet()); err != nil {
		t.Fatalf("New localhost: %v", err)
	}
}

// rewrite sends every request to the test server, whatever host it names.
type rewrite struct{ to *url.URL }

func (r rewrite) RoundTrip(req *http.Request) (*http.Response, error) {
	req = req.Clone(req.Context())
	req.URL.Scheme, req.URL.Host = r.to.Scheme, r.to.Host
	return http.DefaultTransport.RoundTrip(req)
}

func TestPlainHTTPAllowedWithFlag(t *testing.T) {
	var hits atomic.Int32
	ts := meServer(t, &hits)
	to, _ := url.Parse(ts.URL)
	m, err := New(Config{URL: "http://chat.example", AllowHTTP: true, Token: adminToken}, quiet())
	if err != nil {
		t.Fatalf("New with AllowHTTP: %v", err)
	}
	m.Client = &http.Client{Transport: rewrite{to}}
	if _, err := m.AgentForCredential(context.Background(), adminToken); err != nil {
		t.Fatalf("call: %v", err)
	}
	if hits.Load() != 1 {
		t.Fatalf("hits: got %d want 1", hits.Load())
	}
}

// #135: a 302 to another server is not followed, even with a caller's
// client that would follow it: the other server never sees the token, and
// the call fails with the 302.
func TestRedirectNotFollowed(t *testing.T) {
	var other atomic.Int32
	second := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		other.Add(1)
		if r.Header.Get("Authorization") != "" {
			t.Errorf("second server got an Authorization header")
		}
		_ = json.NewEncoder(w).Encode(User{ID: "admin-id"})
	}))
	t.Cleanup(second.Close)
	first := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, second.URL+r.URL.Path, http.StatusFound)
	}))
	t.Cleanup(first.Close)
	m := &Adapter{Cfg: Config{URL: first.URL, Token: adminToken}, Client: first.Client(), Log: quiet()}
	_, err := m.AgentForCredential(context.Background(), adminToken)
	var he *httpError
	if !errors.As(err, &he) || he.status != http.StatusFound {
		t.Fatalf("err: got %v, want the 302 as an error", err)
	}
	if other.Load() != 0 {
		t.Fatalf("second server hit %d times", other.Load())
	}
}
