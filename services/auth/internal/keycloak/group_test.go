package keycloak

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
)

type user struct {
	ID       string `json:"id,omitempty"`
	Username string `json:"username,omitempty"`
	Enabled  bool   `json:"enabled"`
}

// stand is a Keycloak stand-in; adminCalls counts requests to the admin API.
type stand struct {
	*httptest.Server
	adminCalls atomic.Int32
	users      []user
	groupJSON  string // answer of group-by-path; "" is the default group
	membersMax atomic.Int32
	membersFn  func(w http.ResponseWriter, r *http.Request)
	groupPaths chan string
}

func newStand(t *testing.T, users []user) *stand {
	t.Helper()
	s := &stand{users: users, groupPaths: make(chan string, 8)}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /realms/test/protocol/openid-connect/token", func(w http.ResponseWriter, r *http.Request) {
		if r.ParseForm() != nil || r.PostForm.Get("grant_type") != "client_credentials" ||
			r.PostForm.Get("client_id") != "bus-approver-refresh" || r.PostForm.Get("client_secret") != "s3cret" {
			http.Error(w, "no", http.StatusUnauthorized)
			return
		}
		fmt.Fprint(w, `{"access_token":"tok","token_type":"Bearer"}`)
	})
	admin := func(h http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			s.adminCalls.Add(1)
			if r.Header.Get("Authorization") != "Bearer tok" {
				http.Error(w, "no", http.StatusUnauthorized)
				return
			}
			h(w, r)
		}
	}
	mux.HandleFunc("GET /admin/realms/test/group-by-path/{path...}", admin(func(w http.ResponseWriter, r *http.Request) {
		select {
		case s.groupPaths <- r.PathValue("path"):
		default:
		}
		if s.groupJSON != "" {
			fmt.Fprint(w, s.groupJSON)
			return
		}
		if r.PathValue("path") != "bus-approvers" {
			http.NotFound(w, r)
			return
		}
		fmt.Fprint(w, `{"id":"g1","name":"bus-approvers","path":"/bus-approvers"}`)
	}))
	mux.HandleFunc("GET /admin/realms/test/groups/g1/members", admin(func(w http.ResponseWriter, r *http.Request) {
		if s.membersFn != nil {
			s.membersFn(w, r)
			return
		}
		q := r.URL.Query()
		max, err := strconv.Atoi(q.Get("max"))
		if err != nil || q.Get("first") != "0" || q.Get("briefRepresentation") != "true" {
			http.Error(w, "bad query", http.StatusBadRequest)
			return
		}
		s.membersMax.Store(int32(max))
		us := s.users
		if len(us) > max {
			us = us[:max]
		}
		out := "["
		for i, u := range us {
			if i > 0 {
				out += ","
			}
			out += fmt.Sprintf(`{"id":%q,"username":%q,"enabled":%v}`, u.ID, u.Username, u.Enabled)
		}
		fmt.Fprint(w, out+"]")
	}))
	s.Server = httptest.NewServer(mux)
	t.Cleanup(s.Close)
	return s
}

func (s *stand) group(t *testing.T, secret, group string) *Group {
	t.Helper()
	g, err := New(s.URL+"/realms/test/protocol/openid-connect/certs", "bus-approver-refresh", secret, group, nil)
	if err != nil {
		t.Fatal(err)
	}
	return g
}

func noSecrets(t *testing.T, err error) {
	t.Helper()
	if err == nil {
		t.Fatal("no error")
	}
	for _, bad := range []string{"s3cret", "tok"} {
		if strings.Contains(err.Error(), bad) {
			t.Errorf("the error carries %q: %v", bad, err)
		}
	}
}

// The members are listed as Member{ID, Username}; a disabled user and entries
// without an id or a name are left out (RT-6.42 step 3, #266).
func TestMembersAreListed(t *testing.T) {
	s := newStand(t, []user{
		{"u1", "alice", true}, {"u2", "bob", false}, {"", "ghost", true}, {"u4", "", true}, {"u5", "eve", true},
	})
	got, cut, err := s.group(t, "s3cret", "bus-approvers").Members(context.Background(), 10)
	if err != nil || cut {
		t.Fatalf("%v %v", cut, err)
	}
	want := []Member{{"u1", "alice"}, {"u5", "eve"}}
	if len(got) != 2 || got[0] != want[0] || got[1] != want[1] {
		t.Fatalf("%+v", got)
	}
}

// The request asks for max+1; more members than max are cut to max with
// cut=true, exactly max is not cut (RT-6.42 step 3, #266).
func TestMembersCutAtMax(t *testing.T) {
	var us []user
	for i := 0; i < 5; i++ {
		us = append(us, user{fmt.Sprintf("u%d", i), fmt.Sprintf("n%d", i), true})
	}
	s := newStand(t, us)
	g := s.group(t, "s3cret", "bus-approvers")
	got, cut, err := g.Members(context.Background(), 3)
	if err != nil || !cut || len(got) != 3 || s.membersMax.Load() != 4 {
		t.Fatalf("%+v %v %v asked max=%d", got, cut, err, s.membersMax.Load())
	}
	got, cut, err = g.Members(context.Background(), 5)
	if err != nil || cut || len(got) != 5 || s.membersMax.Load() != 6 {
		t.Fatalf("%+v %v %v asked max=%d", got, cut, err, s.membersMax.Load())
	}
}

// A wrong secret is an error and no admin request follows (RT-6.42 step 3, #266).
func TestWrongSecretMakesNoAdminRequest(t *testing.T) {
	s := newStand(t, []user{{"u1", "alice", true}})
	got, _, err := s.group(t, "wrong-secret", "bus-approvers").Members(context.Background(), 10)
	if err == nil || got != nil || s.adminCalls.Load() != 0 {
		t.Fatalf("%v %v admin calls %d", got, err, s.adminCalls.Load())
	}
	if strings.Contains(err.Error(), "wrong-secret") {
		t.Errorf("the secret is in the error: %v", err)
	}
}

// Keycloak down is an error, never an empty list (RT-6.42 step 3, #266).
func TestKeycloakDownIsAnError(t *testing.T) {
	s := newStand(t, nil)
	g := s.group(t, "s3cret", "bus-approvers")
	s.Close()
	got, _, err := g.Members(context.Background(), 10)
	if err == nil || got != nil {
		t.Fatalf("%v %v", got, err)
	}
	noSecrets(t, err)
}

// An unknown group, or an answer for another path, is ErrGroup; a leading
// slash makes no difference (RT-6.42 step 3, #266).
func TestGroupNotFound(t *testing.T) {
	s := newStand(t, []user{{"u1", "alice", true}})
	_, _, err := s.group(t, "s3cret", "other").Members(context.Background(), 10)
	if !errors.Is(err, ErrGroup) {
		t.Fatalf("404: %v", err)
	}
	s.groupJSON = `{"id":"g1","name":"x","path":"/elsewhere/bus-approvers"}`
	_, _, err = s.group(t, "s3cret", "bus-approvers").Members(context.Background(), 10)
	if !errors.Is(err, ErrGroup) {
		t.Fatalf("other path: %v", err)
	}
	s.groupJSON = ""
	for _, name := range []string{"bus-approvers", "/bus-approvers"} {
		got, _, err := s.group(t, "s3cret", name).Members(context.Background(), 10)
		if err != nil || len(got) != 1 {
			t.Fatalf("%q: %+v %v", name, got, err)
		}
	}
}

// A nested group path is requested segment by segment (RT-6.42 step 3, #266).
func TestNestedGroupPath(t *testing.T) {
	s := newStand(t, nil)
	s.groupJSON = `{"id":"g1","path":"/top/sub"}`
	s.membersFn = func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, `[]`) }
	if _, _, err := s.group(t, "s3cret", "/top/sub").Members(context.Background(), 10); err != nil {
		t.Fatal(err)
	}
	if p := <-s.groupPaths; p != "top/sub" {
		t.Fatalf("requested %q", p)
	}
}

// A redirect is not followed: the target is never hit and it is an error
// (RT-6.42 step 3, #266).
func TestRedirectIsNotFollowed(t *testing.T) {
	var hit atomic.Bool
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hit.Store(true)
		fmt.Fprint(w, `[]`)
	}))
	defer target.Close()
	s := newStand(t, nil)
	s.membersFn = func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL+"/x", http.StatusFound)
	}
	_, _, err := s.group(t, "s3cret", "bus-approvers").Members(context.Background(), 10)
	if hit.Load() {
		t.Fatal("the redirect was followed")
	}
	noSecrets(t, err)
}

// An answer larger than 1 MiB is an error (RT-6.42 step 3, #266).
func TestOversizedAnswer(t *testing.T) {
	s := newStand(t, nil)
	s.membersFn = func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `[{"id":"u","username":"`+strings.Repeat("a", maxBody+10)+`","enabled":true}]`)
	}
	got, _, err := s.group(t, "s3cret", "bus-approvers").Members(context.Background(), 10)
	if err == nil || got != nil {
		t.Fatalf("%v %v", got, err)
	}
	noSecrets(t, err)
}

// An answer that is no 200 or no JSON gives an error without the secret or
// the token (RT-6.42 step 3, #266).
func TestErrorsCarryNoSecret(t *testing.T) {
	s := newStand(t, nil)
	s.membersFn = func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "Bearer tok s3cret", http.StatusInternalServerError)
	}
	_, _, err := s.group(t, "s3cret", "bus-approvers").Members(context.Background(), 10)
	noSecrets(t, err)
	s.membersFn = func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, `tok s3cret`) }
	_, _, err = s.group(t, "s3cret", "bus-approvers").Members(context.Background(), 10)
	noSecrets(t, err)
}

// New refuses what would send the secret astray or cannot name a group, and
// takes a path prefix (RT-6.42 step 3, #266).
func TestNew(t *testing.T) {
	const certs = "/realms/x/protocol/openid-connect/certs"
	for name, c := range map[string]struct{ url, id, secret, group string }{
		"no realm":      {"https://kc.example/certs", "c", "s", "g"},
		"wrong suffix":  {"https://kc.example/realms/x/protocol/openid-connect/token", "c", "s", "g"},
		"plain http":    {"http://kc.example" + certs, "c", "s", "g"},
		"no client id":  {"https://kc.example" + certs, "", "s", "g"},
		"no secret":     {"https://kc.example" + certs, "c", "", "g"},
		"empty part":    {"https://kc.example" + certs, "c", "s", "a//b"},
		"empty group":   {"https://kc.example" + certs, "c", "s", ""},
		"nested realm":  {"https://kc.example/realms/x/y/protocol/openid-connect/certs", "c", "s", "g"},
		"user info url": {"https://u:p@kc.example" + certs, "c", "s", "g"},
	} {
		if g, err := New(c.url, c.id, c.secret, c.group, nil); err == nil {
			t.Errorf("%s: accepted %+v", name, g)
		}
	}
	g, err := New("https://kc.example/auth"+certs, "c", "s", "g", nil)
	if err != nil {
		t.Fatal(err)
	}
	if g.tokenURL != "https://kc.example/auth/realms/x/protocol/openid-connect/token" || g.adminURL != "https://kc.example/auth/admin/realms/x" {
		t.Fatalf("%q %q", g.tokenURL, g.adminURL)
	}
}

// Members(ctx, 0) is an error (RT-6.42 step 3, #266).
func TestMembersNeedsAMax(t *testing.T) {
	s := newStand(t, nil)
	if _, _, err := s.group(t, "s3cret", "bus-approvers").Members(context.Background(), 0); err == nil {
		t.Fatal("no error")
	}
}
