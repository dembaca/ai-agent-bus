package release

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/registrar/core"
)

const testKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGP3bXHhZ0mM0b8o0mZ0sh6o6rGm0yQp0i0a0b0c0d0e"

func testAttestor(t *testing.T) (*core.Attestor, ed25519.PublicKey) {
	t.Helper()
	seed := make([]byte, ed25519.SeedSize)
	for i := range seed {
		seed[i] = byte(i + 1)
	}
	a, err := core.NewAttestor(seed, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	return a, ed25519.NewKeyFromSeed(seed).Public().(ed25519.PublicKey)
}

// RT-6.32, #151 step 4: the registrar's route and record, signed with the
// bridge's key; a host's verifier reads it unchanged.
func TestTheRecordIsTheRegistrarS(t *testing.T) {
	a, pub := testAttestor(t)
	setAt := time.Date(2026, 11, 26, 9, 0, 0, 0, time.UTC)
	h, err := New(testKey+"\n", "latest", a, setAt)
	if err != nil {
		t.Fatal(err)
	}
	now := setAt.Add(time.Hour)
	h.Now = func() time.Time { return now }
	w := httptest.NewRecorder()
	h.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/register/v1/release", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	if w.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("a host may cache it: %q", w.Header().Get("Cache-Control"))
	}
	var out struct{ Record string }
	if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil || out.Record == "" {
		t.Fatalf("body %q: %v", w.Body.String(), err)
	}
	// The hosts' own verifier: signature, header's typ, and expiry.
	claims, err := core.VerifyRelease(out.Record, pub, now)
	if err != nil {
		t.Fatalf("the record does not verify: %v", err)
	}
	if len(claims.Signers) != 1 || claims.Signers[0] != testKey {
		t.Fatalf("signers %v", claims.Signers)
	}
	if claims.Version != "latest" || claims.SetAtMs != setAt.UnixMilli() {
		t.Fatalf("claims %+v", claims)
	}
	if claims.Exp-claims.Iat != 3600 || claims.Iat != now.Unix() {
		t.Fatalf("times %+v", claims)
	}
	if !strings.Contains(out.Record, ".") {
		t.Fatal("not a compact JWS")
	}
}

func TestNoReleaseSetIsTheRegistrarS404(t *testing.T) {
	a, _ := testAttestor(t)
	h := &Handler{Attest: a}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/register/v1/release", nil))
	if w.Code != http.StatusNotFound || strings.TrimSpace(w.Body.String()) != `{"error":"no release set"}` {
		t.Fatalf("%d %q", w.Code, w.Body.String())
	}
}

// #194 review: a read asks for the record; anything else is refused.
func TestOnlyAReadGetsTheRecord(t *testing.T) {
	a, _ := testAttestor(t)
	h, err := New(testKey+"\n", "latest", a, time.Date(2026, 11, 26, 9, 0, 0, 0, time.UTC))
	if err != nil {
		t.Fatal(err)
	}
	for _, method := range []string{http.MethodPost, http.MethodPut, http.MethodDelete} {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest(method, "/register/v1/release", nil))
		if w.Code != http.StatusMethodNotAllowed || w.Header().Get("Allow") != "GET, HEAD" {
			t.Fatalf("%s: %d %q", method, w.Code, w.Header().Get("Allow"))
		}
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, httptest.NewRequest(http.MethodHead, "/register/v1/release", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("HEAD: %d", w.Code)
	}
}

func TestTheSettingsAreValidated(t *testing.T) {
	a, _ := testAttestor(t)
	setAt := time.Date(2026, 11, 26, 9, 0, 0, 0, time.UTC)
	for _, tc := range []struct {
		signers string
		version string
		ok      bool
	}{
		{testKey + "\n", "", true},                   // keys only: no target
		{testKey + "\n", "v0.103.0", true},           // both
		{"", "v0.103.0", false},                      // a target without keys
		{testKey + "\n", "0.103", false},             // not v<x.y.z>
		{testKey + "\n", "v01.2.3", false},           // leading zero
		{"ssh-rsa AAAAB3NzaC1yc2E\n", "", false},     // not an ed25519 key
		{testKey + "\n" + testKey + "\n", "", false}, // listed twice
		{"", "", false},                              // nothing set
	} {
		if _, err := New(tc.signers, tc.version, a, setAt); (err == nil) != tc.ok {
			t.Errorf("%q/%q: err %v, want ok=%v", tc.signers, tc.version, err, tc.ok)
		}
	}
	if _, err := New(testKey+"\n", "v0.103.0", a, time.Time{}); err == nil {
		t.Error("a zero set_at was accepted")
	}
}

// The fixture the hosts' Python verifier checks is also pinned here: a change to
// what this code signs fails on both sides, so neither drifts.
func TestTheFixtureIsThisCodeS(t *testing.T) {
	seed := make([]byte, ed25519.SeedSize)
	for i := range seed {
		seed[i] = byte(i + 1)
	}
	a, err := core.NewAttestor(seed, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	pub := ed25519.NewKeyFromSeed(seed).Public().(ed25519.PublicKey)
	typ := []byte("ssh-ed25519")
	blob := append([]byte{0, 0, 0, byte(len(typ))}, typ...)
	blob = append(blob, 0, 0, 0, byte(len(pub)))
	blob = append(blob, pub...)
	signer := "ssh-ed25519 " + base64.StdEncoding.EncodeToString(blob)
	want, err := a.SignReleaseExact(core.ReleaseClaims{
		Signers: []string{signer}, Version: core.ReleaseLatest,
		SetAtMs: 1_800_000_000_000, Iat: 1_800_000_000, Exp: 1_800_003_600,
	})
	if err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile("../../../../mcp/tests/testdata/release_record.txt")
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.TrimSpace(string(b)); got != want {
		t.Fatalf("mcp/tests/testdata/release_record.txt is not what this code signs; regenerate it from the same seed and times")
	}
}
