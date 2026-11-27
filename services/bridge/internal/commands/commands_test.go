package commands

import (
	"crypto/ed25519"
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/registrar/core"
)

const (
	// modeChan is a team channel the human below is a member of; modeDM a
	// direct message; homeChan the channel a mandate names.
	modeChan = "c0000000000000000000000000"
	modeDM   = "d0000000000000000000000000"
	homeChan = "c1111111111111111111111111"

	human     = "u0000000000000000000000000" // dembaca, an active human in modeChan and homeChan
	otherUser = "v0000000000000000000000000" // an active human who is in neither
	botUser   = "b0000000000000000000000000" // a bot account
	guestUser = "g0000000000000000000000000" // an active guest

	adminPAT = "admin-pat"
	teamID   = "team-id"
)

// testNow is the fixed clock: 2027-01-15T08:00:00Z, so both the records and the
// cross-language fixtures below are exact.
var testNow = time.Unix(1800000000, 0)

// memPersister stands in for the bridge's records bucket.
type memPersister struct {
	data map[string][]byte
}

func (m *memPersister) Load(name string) ([]byte, error) { return m.data[name], nil }

func (m *memPersister) Save(name string, data []byte) error {
	m.data[name] = data
	return nil
}

// mgmt is one response of the fake Mattermost API.
type mgmt struct {
	status int
	body   string
}

// newFakeMM answers the calls the commands make: a user lookup, a channel and
// its members, and a team channel by name.
func newFakeMM(t *testing.T) *httptest.Server {
	t.Helper()
	users := map[string]mgmt{
		human:     {200, fmt.Sprintf(`{"id":%q,"username":"dembaca"}`, human)},
		otherUser: {200, fmt.Sprintf(`{"id":%q,"username":"kollegin"}`, otherUser)},
		botUser:   {200, fmt.Sprintf(`{"id":%q,"username":"helper","is_bot":true}`, botUser)},
		guestUser: {200, fmt.Sprintf(`{"id":%q,"username":"guest","roles":"system_guest"}`, guestUser)},
	}
	channels := map[string]string{modeChan: "O", homeChan: "O", modeDM: "D"}
	members := map[string]bool{modeChan + "/" + human: true, homeChan + "/" + human: true, modeDM + "/" + human: true,
		homeChan + "/" + otherUser: true}
	names := map[string]string{"home": homeChan}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v4/users/{id}", func(w http.ResponseWriter, r *http.Request) {
		u, ok := users[r.PathValue("id")]
		if !ok {
			http.NotFound(w, nil)
			return
		}
		w.WriteHeader(u.status)
		_, _ = w.Write([]byte(u.body))
	})
	mux.HandleFunc("GET /api/v4/channels/{id}", func(w http.ResponseWriter, r *http.Request) {
		typ, ok := channels[r.PathValue("id")]
		if !ok {
			http.NotFound(w, nil)
			return
		}
		_, _ = fmt.Fprintf(w, `{"id":%q,"type":%q}`, r.PathValue("id"), typ)
	})
	mux.HandleFunc("GET /api/v4/channels/{id}/members/{uid}", func(w http.ResponseWriter, r *http.Request) {
		if !members[r.PathValue("id")+"/"+r.PathValue("uid")] {
			http.NotFound(w, nil)
			return
		}
		_, _ = w.Write([]byte(`{}`))
	})
	mux.HandleFunc("GET /api/v4/teams/name/{team}", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"id":"team-id"}`))
	})
	mux.HandleFunc("GET /api/v4/teams/team-id/channels/name/{name}", func(w http.ResponseWriter, r *http.Request) {
		id, ok := names[r.PathValue("name")]
		if !ok {
			http.NotFound(w, nil)
			return
		}
		_, _ = fmt.Fprintf(w, `{"id":%q,"type":"O"}`, id)
	})
	ts := httptest.NewServer(mux)
	t.Cleanup(ts.Close)
	return ts
}

// newTestServer builds the commands against the fake Mattermost and a fresh
// in-memory store. now is the clock the server and its buckets use.
func newTestServer(t *testing.T, att *core.Attestor, now *time.Time,
	modeTokens, mandateTokens, debugTokens []string) *Server {
	t.Helper()
	if att == nil {
		var err error
		if att, err = core.NewAttestor(testSeed(), time.Hour); err != nil {
			t.Fatal(err)
		}
	}
	s, err := New(Config{
		MMURL: newFakeMM(t).URL, MMTeam: "ai-agents", AdminToken: adminPAT,
		Attestor: att, Persister: &memPersister{data: map[string][]byte{}},
		ModeTokens: modeTokens, MandateTokens: mandateTokens, DebugTokens: debugTokens,
		Now: func() time.Time { return *now },
	})
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func testSeed() []byte {
	s := make([]byte, ed25519.SeedSize)
	for i := range s {
		s[i] = byte(i)
	}
	return s
}

func testPub() ed25519.PublicKey {
	return ed25519.NewKeyFromSeed(testSeed()).Public().(ed25519.PublicKey)
}

// reads is the main listener's mux with the public record routes.
func reads(s *Server) http.Handler {
	mux := http.NewServeMux()
	s.Reads(mux)
	return mux
}

func slash(t *testing.T, h http.Handler, path, token, userID, channelID, text string) *httptest.ResponseRecorder {
	t.Helper()
	form := url.Values{"token": {token}, "user_id": {userID}, "channel_id": {channelID},
		"team_id": {teamID}, "command": {"/agent-mode"}, "text": {text}}
	req := httptest.NewRequest(http.MethodPost, path, strings.NewReader(form.Encode()))
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, req)
	return rr
}

func get(t *testing.T, h http.Handler, path string) *httptest.ResponseRecorder {
	t.Helper()
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest(http.MethodGet, path, nil))
	return rr
}

func ephemeral(t *testing.T, rr *httptest.ResponseRecorder) string {
	t.Helper()
	var reply struct {
		ResponseType string `json:"response_type"`
		Text         string `json:"text"`
	}
	if err := json.Unmarshal(rr.Body.Bytes(), &reply); err != nil || reply.ResponseType != "ephemeral" {
		t.Fatalf("not an ephemeral reply: %d %s", rr.Code, rr.Body.String())
	}
	return reply.Text
}

func recordOf(t *testing.T, rr *httptest.ResponseRecorder) string {
	t.Helper()
	var body struct {
		Record string `json:"record"`
	}
	if err := json.Unmarshal(rr.Body.Bytes(), &body); err != nil || rr.Code != http.StatusOK || body.Record == "" {
		t.Fatalf("get: %d %s", rr.Code, rr.Body.String())
	}
	if rr.Header().Get("Content-Type") != "application/json" || rr.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("headers: %v", rr.Header())
	}
	return body.Record
}

// Issue #151 step 3: the registrar's mode command and its signed record over
// the bridge's own listener.
func TestTheModeCommandAndItsRecordOverHTTP(t *testing.T) {
	now := testNow
	s := newTestServer(t, nil, &now, []string{"mode-a", "mode-b"}, nil, nil)
	cmd, read := s.Commands(), reads(s)

	for _, tok := range []string{"", "wrong", "mand-a"} {
		if rr := slash(t, cmd, "/register/v1/mode", tok, human, modeChan, "coordinator"); rr.Code != http.StatusUnauthorized {
			t.Fatalf("token %q: %d %s", tok, rr.Code, rr.Body.String())
		}
	}
	if txt := ephemeral(t, slash(t, cmd, "/register/v1/mode", "mode-a", botUser, modeChan, "coordinator")); !strings.Contains(txt, "no bots") {
		t.Fatalf("bot: %s", txt)
	}
	if txt := ephemeral(t, slash(t, cmd, "/register/v1/mode", "mode-a", guestUser, modeChan, "coordinator")); !strings.Contains(txt, "no guests") {
		t.Fatalf("guest: %s", txt)
	}
	if rr := get(t, read, "/register/v1/modes/"+modeChan); rr.Code != http.StatusNotFound ||
		strings.TrimSpace(rr.Body.String()) != `{"error":"no mode set"}` {
		t.Fatalf("unset: %d %s", rr.Code, rr.Body.String())
	}
	if txt := ephemeral(t, slash(t, cmd, "/register/v1/mode", "mode-a", human, modeChan, "")); !strings.Contains(txt, "not set") {
		t.Fatalf("status: %s", txt)
	}
	if txt := ephemeral(t, slash(t, cmd, "/register/v1/mode", "mode-b", human, modeDM, "coordinator")); !strings.Contains(txt, "direct or group") {
		t.Fatalf("dm: %s", txt)
	}
	if txt := ephemeral(t, slash(t, cmd, "/register/v1/mode", "mode-a", otherUser, modeChan, "coordinator")); !strings.Contains(txt, "Only members") {
		t.Fatalf("non-member: %s", txt)
	}
	if txt := ephemeral(t, slash(t, cmd, "/register/v1/mode", "mode-a", human, modeChan, "please")); !strings.Contains(txt, "/agent-mode coordinator") {
		t.Fatalf("help: %s", txt)
	}
	if rr := get(t, read, "/register/v1/modes/"+modeChan); rr.Code != http.StatusNotFound {
		t.Fatalf("refused calls stored a mode: %d", rr.Code)
	}

	if txt := ephemeral(t, slash(t, cmd, "/register/v1/mode", "mode-b", human, modeChan, "coordinator")); !strings.Contains(txt, "now `coordinator`") {
		t.Fatalf("set: %s", txt)
	}
	rec := recordOf(t, get(t, read, "/register/v1/modes/"+modeChan))
	c, err := core.VerifyMode(rec, testPub(), testNow)
	want := core.ModeClaims{ChannelID: modeChan, Mode: core.ModeCoordinator, SetBy: "dembaca",
		SetByID: human, SetAtMs: testNow.UnixMilli(), Iat: testNow.Unix(), Exp: testNow.Add(core.ModeTTL).Unix()}
	if err != nil || c != want {
		t.Fatalf("claims: %+v %v", c, err)
	}
	if _, err := core.VerifyAttestation(rec, testPub(), testNow); err == nil {
		t.Fatal("a mode record verifies as an attestation")
	}
	if _, err := core.VerifyMandate(rec, testPub(), testNow); err == nil {
		t.Fatal("a mode record verifies as a mandate")
	}
	if txt := ephemeral(t, slash(t, cmd, "/register/v1/mode", "mode-a", human, modeChan, "status")); !strings.Contains(txt, "`coordinator`, set by @dembaca") {
		t.Fatalf("status after set: %s", txt)
	}
	for _, id := range []string{"short", "C0000000000000000000000000", "c00000000000000000000000000", "c000000000000000000000000_"} {
		rr := get(t, read, "/register/v1/modes/"+id)
		if rr.Code != http.StatusBadRequest || strings.TrimSpace(rr.Body.String()) != `{"error":"invalid channel id"}` {
			t.Fatalf("%q: %d %s", id, rr.Code, rr.Body.String())
		}
	}
}

// The mandate command names another channel's coordinator; its record holds
// home, until and note, and off removes it.
func TestTheMandateCommandAndItsRecordOverHTTP(t *testing.T) {
	now := testNow
	s := newTestServer(t, nil, &now, nil, []string{"mand-a", "mand-b"}, nil)
	cmd, read := s.Commands(), reads(s)

	for _, tok := range []string{"", "wrong", "mode-a"} {
		if rr := slash(t, cmd, "/register/v1/mandate", tok, human, modeChan, "status"); rr.Code != http.StatusUnauthorized {
			t.Fatalf("token %q: %d", tok, rr.Code)
		}
	}
	if rr := get(t, read, "/register/v1/mandates/"+modeChan); rr.Code != http.StatusNotFound ||
		strings.TrimSpace(rr.Body.String()) != `{"error":"no mandate set"}` {
		t.Fatalf("unset: %d %s", rr.Code, rr.Body.String())
	}
	if txt := ephemeral(t, slash(t, cmd, "/register/v1/mandate", "mand-a", human, modeChan, "")); !strings.Contains(txt, "not set") {
		t.Fatalf("status: %s", txt)
	}
	if txt := ephemeral(t, slash(t, cmd, "/register/v1/mandate", "mand-b", human, modeDM, "set ~home")); !strings.Contains(txt, "direct or group") {
		t.Fatalf("dm: %s", txt)
	}
	if txt := ephemeral(t, slash(t, cmd, "/register/v1/mandate", "mand-a", otherUser, modeChan, "set ~home")); !strings.Contains(txt, "Only members") {
		t.Fatalf("non-member: %s", txt)
	}
	if txt := ephemeral(t, slash(t, cmd, "/register/v1/mandate", "mand-a", human, modeChan, "set ~nowhere")); !strings.Contains(txt, "~nowhere is not a channel") {
		t.Fatalf("unknown home: %s", txt)
	}
	if rr := get(t, read, "/register/v1/mandates/"+modeChan); rr.Code != http.StatusNotFound {
		t.Fatalf("refused calls stored a mandate: %d", rr.Code)
	}

	if txt := ephemeral(t, slash(t, cmd, "/register/v1/mandate", "mand-b", human, modeChan,
		"Set ~home UNTIL 2027-01-20 ship it")); !strings.Contains(txt, "Mandate set") {
		t.Fatalf("set: %s", txt)
	}
	rec := recordOf(t, get(t, read, "/register/v1/mandates/"+modeChan))
	c, err := core.VerifyMandate(rec, testPub(), testNow)
	want := core.MandateClaims{ChannelID: modeChan, Home: homeChan, SetBy: "dembaca", SetByID: human,
		SetAtMs: testNow.UnixMilli(), Until: "2027-01-20", Note: "ship it",
		Iat: testNow.Unix(), Exp: testNow.Add(core.MandateTTL).Unix()}
	if err != nil || c != want {
		t.Fatalf("claims: %+v %v", c, err)
	}
	if txt := ephemeral(t, slash(t, cmd, "/register/v1/mandate", "mand-a", human, modeChan, "off")); !strings.Contains(txt, "Mandate removed") {
		t.Fatalf("off: %s", txt)
	}
	if rr := get(t, read, "/register/v1/mandates/"+modeChan); rr.Code != http.StatusNotFound {
		t.Fatalf("after off: %d", rr.Code)
	}
	for _, id := range []string{"short", "C0000000000000000000000000"} {
		if rr := get(t, read, "/register/v1/mandates/"+id); rr.Code != http.StatusBadRequest {
			t.Fatalf("%q: %d", id, rr.Code)
		}
	}
}

// The debug command's record carries the categories in canonical order and the
// setting's own end; once it has ended, the read is a 404 like never set.
func TestTheDebugCommandAndItsRecordOverHTTP(t *testing.T) {
	now := testNow
	s := newTestServer(t, nil, &now, nil, nil, []string{"dbg-a", "dbg-b"})
	cmd, read := s.Commands(), reads(s)

	for _, tok := range []string{"", "wrong", "mand-a"} {
		if rr := slash(t, cmd, "/register/v1/debug", tok, human, modeChan, "full"); rr.Code != http.StatusUnauthorized {
			t.Fatalf("token %q: %d", tok, rr.Code)
		}
	}
	const none = `{"error":"no debug set"}`
	if rr := get(t, read, "/register/v1/debug/"+modeChan); rr.Code != http.StatusNotFound || strings.TrimSpace(rr.Body.String()) != none {
		t.Fatalf("unset: %d %s", rr.Code, rr.Body.String())
	}
	if txt := ephemeral(t, slash(t, cmd, "/register/v1/debug", "dbg-b", human, modeChan, "On Wake PROPS for 2h")); !strings.Contains(txt, "`wake` `props`") {
		t.Fatalf("on: %s", txt)
	}
	rec := recordOf(t, get(t, read, "/register/v1/debug/"+modeChan))
	c, err := core.VerifyDebug(rec, testPub(), testNow)
	want := core.DebugClaims{ChannelID: modeChan, Categories: []string{"wake", "props"}, SetBy: "dembaca",
		SetByID: human, SetAtMs: testNow.UnixMilli(), UntilMs: testNow.Add(2 * time.Hour).UnixMilli(),
		Iat: testNow.Unix(), Exp: testNow.Add(core.DebugTTL).Unix()}
	if err != nil || !reflect.DeepEqual(c, want) {
		t.Fatalf("claims: %+v %v", c, err)
	}
	now = now.Add(2 * time.Hour)
	if rr := get(t, read, "/register/v1/debug/"+modeChan); rr.Code != http.StatusNotFound || strings.TrimSpace(rr.Body.String()) != none {
		t.Fatalf("ended: %d %s", rr.Code, rr.Body.String())
	}
	if txt := ephemeral(t, slash(t, cmd, "/register/v1/debug", "dbg-a", human, modeChan, "full")); !strings.Contains(txt, "`wake` `identity` `sig` `asks` `calls` `props`") {
		t.Fatalf("full: %s", txt)
	}
	if rr := get(t, read, "/register/v1/debug/"+modeChan); rr.Code != http.StatusOK {
		t.Fatalf("after full: %d", rr.Code)
	}
	if txt := ephemeral(t, slash(t, cmd, "/register/v1/debug", "dbg-a", human, modeChan, "off")); !strings.Contains(txt, "off (was:") {
		t.Fatalf("off: %s", txt)
	}
	for _, id := range []string{"short", "C0000000000000000000000000"} {
		if rr := get(t, read, "/register/v1/debug/"+id); rr.Code != http.StatusBadRequest {
			t.Fatalf("%q: %d", id, rr.Code)
		}
	}
}

// Each command takes only its own tokens, and the two muxes carry their own
// routes: the command listener no read, the main listener no command.
func TestTheCommandRoutesAndMuxes(t *testing.T) {
	now := testNow
	s := newTestServer(t, nil, &now, []string{"mode-a"}, []string{"mand-a"}, []string{"dbg-a"})
	cmd, read := s.Commands(), reads(s)
	gone := func(rr *httptest.ResponseRecorder) bool {
		return rr.Code == http.StatusNotFound || rr.Code == http.StatusMethodNotAllowed
	}
	for _, tc := range []struct {
		path, token, want string
	}{
		{"/register/v1/mode", "mand-a", "mandate's token on the mode command"},
		{"/register/v1/mandate", "mode-a", "mode's token on the mandate command"},
		{"/register/v1/mandate", "dbg-a", "debug's token on the mandate command"},
		{"/register/v1/debug", "mode-a", "mode's token on the debug command"},
	} {
		if rr := slash(t, cmd, tc.path, tc.token, human, modeChan, "status"); rr.Code != http.StatusUnauthorized {
			t.Fatalf("%s: %d", tc.want, rr.Code)
		}
	}
	// A command the config left out has no route, and its record still reads.
	partial := newTestServer(t, nil, &now, []string{"mode-a"}, nil, nil)
	if !gone(slash(t, partial.Commands(), "/register/v1/mandate", "mand-a", human, modeChan, "status")) {
		t.Fatal("the mandate route exists without MANDATE_COMMAND_TOKEN")
	}
	if rr := get(t, reads(partial), "/register/v1/mandates/"+modeChan); rr.Code != http.StatusNotFound ||
		strings.TrimSpace(rr.Body.String()) != `{"error":"no mandate set"}` {
		t.Fatalf("mandate read without its command: %d %s", rr.Code, rr.Body.String())
	}
	// No read on the command listener, no command on the main one.
	if rr := get(t, cmd, "/register/v1/modes/"+modeChan); !gone(rr) {
		t.Fatalf("the command listener serves a read: %d", rr.Code)
	}
	if rr := slash(t, read, "/register/v1/mode", "mode-a", human, modeChan, "status"); !gone(rr) {
		t.Fatalf("the main listener serves a command: %d", rr.Code)
	}
}

// The registrar's buckets: the 31st command post in a minute is a 429, and each
// read has its own 120.
func TestTheThirtyFirstCommandAndThe121stRead(t *testing.T) {
	now := testNow
	s := newTestServer(t, nil, &now, []string{"mode-a"}, []string{"mand-a"}, []string{"dbg-a"})
	cmd, read := s.Commands(), reads(s)
	for i := 0; i < 30; i++ {
		if rr := slash(t, cmd, "/register/v1/mode", "mode-a", human, modeChan, "status"); rr.Code != http.StatusOK {
			t.Fatalf("command %d: %d %s", i+1, rr.Code, rr.Body.String())
		}
	}
	if rr := slash(t, cmd, "/register/v1/mode", "mode-a", human, modeChan, "status"); rr.Code != http.StatusTooManyRequests {
		t.Fatalf("the 31st command: %d", rr.Code)
	}
	for i := 0; i < 120; i++ {
		if rr := get(t, read, "/register/v1/modes/"+modeChan); rr.Code != http.StatusNotFound {
			t.Fatalf("mode read %d: %d", i+1, rr.Code)
		}
	}
	if rr := get(t, read, "/register/v1/modes/"+modeChan); rr.Code != http.StatusTooManyRequests {
		t.Fatalf("the 121st mode read: %d", rr.Code)
	}
	// The other read buckets did not move.
	if rr := get(t, read, "/register/v1/mandates/"+modeChan); rr.Code != http.StatusNotFound {
		t.Fatalf("the mandate bucket was drained: %d", rr.Code)
	}
	if rr := get(t, read, "/register/v1/debug/"+modeChan); rr.Code != http.StatusNotFound {
		t.Fatalf("the debug bucket was drained: %d", rr.Code)
	}
}

// A form over 64 KiB is refused with 413, without spending a rate hit.
func TestABodyOverTheLimitIs413(t *testing.T) {
	now := testNow
	s := newTestServer(t, nil, &now, []string{"mode-a"}, nil, nil)
	cmd := s.Commands()
	big := "text=" + strings.Repeat("x", maxBody)
	req := httptest.NewRequest(http.MethodPost, "/register/v1/mode", strings.NewReader(big))
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	rr := httptest.NewRecorder()
	cmd.ServeHTTP(rr, req)
	if rr.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("%d %s", rr.Code, rr.Body.String())
	}
	if len(s.userRate.hits) != 0 || len(s.channelRate.hits) != 0 {
		t.Fatal("the refused body spent a rate hit")
	}
}

// The read buckets count the peer, and read X-Forwarded-For only when that peer
// is a configured trusted proxy; the first address from the right that is not a
// proxy counts, a junk entry or a chain of only proxies falls back to the peer.
func TestTheReadClientIPUsesTrustedProxiesOnly(t *testing.T) {
	proxies, err := parseProxies([]string{"10.0.0.0/8", "2001:db8::1"})
	if err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		name string
		peer string
		xff  []string
		want string
	}{
		{"a trusted proxy names the client", "10.1.2.3:4711", []string{"6.6.6.6, 203.0.113.9"}, "203.0.113.9"},
		{"further trusted proxies are skipped", "10.1.2.3:4711", []string{"203.0.113.9, 10.9.9.9"}, "203.0.113.9"},
		{"an untrusted peer's header is ignored", "192.0.2.1:4711", []string{"203.0.113.9"}, "192.0.2.1"},
		{"a junk entry falls back to the peer", "10.1.2.3:4711", []string{"203.0.113.9, evil"}, "10.1.2.3"},
		{"a chain of only proxies falls back", "10.1.2.3:4711", []string{"10.4.4.4"}, "10.1.2.3"},
		{"no header", "10.1.2.3:4711", nil, "10.1.2.3"},
		{"an IPv6 proxy as configured", "[2001:db8::1]:4711", []string{"2001:db8::9"}, "2001:db8::9"},
	}
	for _, c := range cases {
		s := &Server{trustedProxies: proxies}
		r := httptest.NewRequest(http.MethodGet, "/", nil)
		r.RemoteAddr = c.peer
		for _, h := range c.xff {
			r.Header.Add("X-Forwarded-For", h)
		}
		if got := s.clientIP(r); got != c.want {
			t.Errorf("%s: got %q, want %q", c.name, got, c.want)
		}
	}
}

// The command buckets are one per Mattermost user and one per channel, filled
// after the token check: a busy person does not drain a channel, and a busy
// channel does not drain a person.
func TestTheCommandBucketsArePerUserAndPerChannel(t *testing.T) {
	now := testNow
	s := newTestServer(t, nil, &now, []string{"mode-a"}, nil, nil)
	cmd := s.Commands()
	for i := 0; i < 30; i++ {
		if rr := slash(t, cmd, "/register/v1/mode", "mode-a", human, homeChan, "status"); rr.Code != http.StatusOK {
			t.Fatalf("command %d: %d %s", i+1, rr.Code, rr.Body.String())
		}
	}
	if rr := slash(t, cmd, "/register/v1/mode", "mode-a", otherUser, homeChan, "status"); rr.Code != http.StatusTooManyRequests {
		t.Fatalf("a second person in the full channel: %d", rr.Code)
	}
	if rr := slash(t, cmd, "/register/v1/mode", "mode-a", human, modeChan, "status"); rr.Code != http.StatusTooManyRequests {
		t.Fatalf("the full person in another channel: %d", rr.Code)
	}
}

// A person at its own limit is refused in a fresh channel, while another person
// there is not: the person's bucket, not the channel's, is what filled.
func TestTheCommandBucketIsPerUserAcrossChannels(t *testing.T) {
	now := testNow
	s := newTestServer(t, nil, &now, []string{"mode-a"}, nil, nil)
	cmd := s.Commands()
	for i := 0; i < 30; i++ {
		ch := modeChan
		if i%2 == 1 {
			ch = modeDM
		}
		if rr := slash(t, cmd, "/register/v1/mode", "mode-a", human, ch, "status"); rr.Code != http.StatusOK {
			t.Fatalf("command %d: %d %s", i+1, rr.Code, rr.Body.String())
		}
	}
	if rr := slash(t, cmd, "/register/v1/mode", "mode-a", human, homeChan, "status"); rr.Code != http.StatusTooManyRequests {
		t.Fatalf("the full person in a fresh channel: %d", rr.Code)
	}
	if rr := slash(t, cmd, "/register/v1/mode", "mode-a", otherUser, homeChan, "status"); rr.Code != http.StatusOK {
		t.Fatalf("another person in a fresh channel: %d %s", rr.Code, rr.Body.String())
	}
}

// The limiter drops a key quiet for a minute, and sweeps at most once a minute.
func TestTheLimiterForgetsQuietKeys(t *testing.T) {
	l := newLimiter(2)
	old := time.Now().Add(-2 * time.Minute)
	l.hits["198.51.100.1"] = []time.Time{old}
	l.hits["198.51.100.2"] = []time.Time{old, time.Now()}
	l.swept = old
	if !l.allow("203.0.113.9") {
		t.Fatal("a new key must be allowed")
	}
	if _, ok := l.hits["198.51.100.1"]; ok {
		t.Error("a key quiet for a minute must be dropped")
	}
	if _, ok := l.hits["198.51.100.2"]; !ok {
		t.Error("a key with a recent hit must stay")
	}
	l.allow("203.0.113.9")
	if l.allow("203.0.113.9") {
		t.Error("third hit within the minute must be refused")
	}
	l2 := newLimiter(2)
	l2.swept = time.Now()
	l2.hits["198.51.100.1"] = []time.Time{time.Now().Add(-2 * time.Minute)}
	l2.allow("203.0.113.9")
	if _, ok := l2.hits["198.51.100.1"]; !ok {
		t.Error("a quiet key stays until the next sweep")
	}
}

// updateRecordFixtures rewrites the MCP checker's fixtures from this test, the
// one way they change; without it the committed files are the contract.
var updateRecordFixtures = flag.Bool("update-record-fixtures", false, "rewrite mcp/tests/testdata/bridge-*.jws")

// The exact bytes the bridge signs are the MCP checker's fixtures (issue #151
// step 3): a fixed seed and a fixed clock, so the three records are stable, and
// the Python test verifies these very files against the same public key.
func TestTheBridgeRecordsAreTheMCPFixtures(t *testing.T) {
	now := testNow
	s := newTestServer(t, nil, &now, []string{"mode-a"}, []string{"mand-a"}, []string{"dbg-a"})
	cmd, read := s.Commands(), reads(s)
	if txt := ephemeral(t, slash(t, cmd, "/register/v1/mode", "mode-a", human, modeChan, "coordinator")); !strings.Contains(txt, "now `coordinator`") {
		t.Fatalf("mode: %s", txt)
	}
	if txt := ephemeral(t, slash(t, cmd, "/register/v1/mandate", "mand-a", human, modeChan,
		"set ~home until 2027-01-20 ship it")); !strings.Contains(txt, "Mandate set") {
		t.Fatalf("mandate: %s", txt)
	}
	if txt := ephemeral(t, slash(t, cmd, "/register/v1/debug", "dbg-a", human, modeChan, "on wake props for 30m")); !strings.Contains(txt, "`wake` `props`") {
		t.Fatalf("debug: %s", txt)
	}
	dir := filepath.Join("..", "..", "..", "..", "mcp", "tests", "testdata")
	for _, tc := range []struct{ name, record string }{
		{"bridge-mode.jws", recordOf(t, get(t, read, "/register/v1/modes/"+modeChan))},
		{"bridge-mandate.jws", recordOf(t, get(t, read, "/register/v1/mandates/"+modeChan))},
		{"bridge-debug.jws", recordOf(t, get(t, read, "/register/v1/debug/"+modeChan))},
	} {
		path := filepath.Join(dir, tc.name)
		if *updateRecordFixtures {
			if err := os.MkdirAll(dir, 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(path, []byte(tc.record+"\n"), 0o644); err != nil {
				t.Fatal(err)
			}
			continue
		}
		want, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("%s: %v (run go test -run TestTheBridgeRecordsAreTheMCPFixtures -update-record-fixtures)", path, err)
		}
		if got := strings.TrimSpace(string(want)); got != tc.record {
			t.Fatalf("%s differs:\n got  %s\n want %s", path, tc.record, got)
		}
	}
}

// The release route gets a read bucket of its own, keyed like the record reads:
// 120 a minute, and behind a trusted proxy the client's address.
func TestTheReleaseRouteIsRateLimited(t *testing.T) {
	ok := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	h, err := ReadLimit(ok, nil)
	if err != nil {
		t.Fatal(err)
	}
	req := func(peer, xff string) int {
		r := httptest.NewRequest(http.MethodGet, "/register/v1/release", nil)
		r.RemoteAddr = peer
		if xff != "" {
			r.Header.Set("X-Forwarded-For", xff)
		}
		rr := httptest.NewRecorder()
		h.ServeHTTP(rr, r)
		return rr.Code
	}
	for i := 0; i < 120; i++ {
		if code := req("192.0.2.1:4711", ""); code != http.StatusOK {
			t.Fatalf("read %d: %d", i+1, code)
		}
	}
	if code := req("192.0.2.1:4711", ""); code != http.StatusTooManyRequests {
		t.Fatalf("the 121st read: %d", code)
	}

	gated, err := ReadLimit(ok, []string{"10.0.0.0/8"})
	if err != nil {
		t.Fatal(err)
	}
	r := httptest.NewRequest(http.MethodGet, "/register/v1/release", nil)
	r.RemoteAddr = "10.1.2.3:4711"
	for i := 0; i < 120; i++ {
		r.Header.Set("X-Forwarded-For", "203.0.113.9")
		rr := httptest.NewRecorder()
		gated.ServeHTTP(rr, r)
		if rr.Code != http.StatusOK {
			t.Fatalf("client read %d: %d", i+1, rr.Code)
		}
	}
	r.Header.Set("X-Forwarded-For", "203.0.113.10") // another client behind the same proxy
	rr := httptest.NewRecorder()
	gated.ServeHTTP(rr, r)
	if rr.Code != http.StatusOK {
		t.Fatalf("the proxy's next client: %d", rr.Code)
	}
}
