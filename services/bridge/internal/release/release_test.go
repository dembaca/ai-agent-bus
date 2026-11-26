package release

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func testSigner(t *testing.T) *FileSigner {
	t.Helper()
	seed := make([]byte, ed25519.SeedSize)
	for i := range seed {
		seed[i] = byte(i + 1)
	}
	key := ed25519.NewKeyFromSeed(seed)
	pub := key.Public().(ed25519.PublicKey)
	return &FileSigner{key: key, kid: base64.RawURLEncoding.EncodeToString(pub[:8])}
}

const testKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGP3bXHhZ0mM0b8o0mZ0sh6o6rGm0yQp0i0a0b0c0d0e"

// RT-6.32, #151 step 4: the registrar's route and record, signed with the
// bridge's key; a host's release.py reads it unchanged.
func TestTheRecordIsTheRegistrarS(t *testing.T) {
	signer := testSigner(t)
	setAt := time.Date(2026, 11, 26, 9, 0, 0, 0, time.UTC)
	h, err := New([]string{testKey}, "latest", signer, setAt)
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
	var out struct{ Record string }
	if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil || out.Record == "" {
		t.Fatalf("body %q: %v", w.Body.String(), err)
	}
	parts := strings.Split(out.Record, ".")
	if len(parts) != 3 {
		t.Fatalf("not a compact JWS: %q", out.Record)
	}
	b64 := base64.RawURLEncoding
	header, err := b64.DecodeString(parts[0])
	if err != nil {
		t.Fatal(err)
	}
	var hd map[string]string
	if err := json.Unmarshal(header, &hd); err != nil {
		t.Fatal(err)
	}
	if hd["alg"] != "EdDSA" || hd["typ"] != "mm-agent-release+jwt" || hd["kid"] != signer.KeyID() {
		t.Fatalf("header %v", hd)
	}
	payload, err := b64.DecodeString(parts[1])
	if err != nil {
		t.Fatal(err)
	}
	var claims struct {
		Signers []string `json:"signers"`
		Version string   `json:"version"`
		SetAtMS int64    `json:"set_at_ms"`
		Iat     int64    `json:"iat"`
		Exp     int64    `json:"exp"`
	}
	if err := json.Unmarshal(payload, &claims); err != nil {
		t.Fatal(err)
	}
	if len(claims.Signers) != 1 || claims.Signers[0] != testKey || claims.Version != "latest" {
		t.Fatalf("claims %+v", claims)
	}
	if claims.SetAtMS != setAt.UnixMilli() || claims.Exp-claims.Iat != 3600 || claims.Iat != now.Unix() {
		t.Fatalf("times %+v", claims)
	}
	sig, err := b64.DecodeString(parts[2])
	if err != nil {
		t.Fatal(err)
	}
	pub := signer.key.Public().(ed25519.PublicKey)
	if !ed25519.Verify(pub, []byte(parts[0]+"."+parts[1]), sig) {
		t.Fatal("the record's signature does not verify")
	}
}

func TestNoReleaseSetIsTheRegistrarS404(t *testing.T) {
	h, err := New(nil, "", testSigner(t), time.Now())
	if err != nil {
		t.Fatal(err)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/register/v1/release", nil))
	if w.Code != http.StatusNotFound || strings.TrimSpace(w.Body.String()) != `{"error":"no release set"}` {
		t.Fatalf("%d %q", w.Code, w.Body.String())
	}
}

func TestTheSettingsAreValidated(t *testing.T) {
	s := testSigner(t)
	for _, tc := range []struct {
		signers []string
		version string
		ok      bool
	}{
		{[]string{testKey}, "", true}, // keys only: no target
		{[]string{testKey}, "v0.103.0", true},
		{[]string{testKey}, "0.103", false},
		{[]string{"ssh-rsa AAAAB3NzaC1yc2E"}, "", false},
		{[]string{testKey, testKey}, "", false}, // distinct keys
		{[]string{testKey, "ssh-ed25519 not base64!"}, "", false},
		{nil, "", true}, // no release set
	} {
		if _, err := New(tc.signers, tc.version, s, time.Now()); (err == nil) != tc.ok {
			t.Errorf("%v/%q: err %v, want ok=%v", tc.signers, tc.version, err, tc.ok)
		}
	}
	// An empty signer list with a version is valid: a target without new keys.
	if _, err := New(nil, "v0.103.0", s, time.Now()); err != nil {
		t.Errorf("a version alone: %v", err)
	}
}
