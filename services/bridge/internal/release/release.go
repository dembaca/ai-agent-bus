// Package release serves the release record (RT-6.32, #151 step 4): the
// registrar's route `GET /register/v1/release`, the same compact JWS, signed
// with the bridge's own key (step 3) — the registrar's key is never copied.
//
// The hosts read it with their pinned keys and their own Python
// (`mattermost_ai_agent_bus.selfupdate`), so the record's shape is a contract:
// header `{"alg":"EdDSA","typ":"mm-agent-release+jwt","kid":…}`, claims
// `signers` (1 to 16 distinct ssh-ed25519 keys, `<type> <base64>`, canonical),
// `version` (`v<x.y.z>`, or `latest`, optional: keys only means no target),
// `set_at_ms`, `iat`, `exp` (an hour later).
package release

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"regexp"
	"strings"
	"time"
)

// Signer signs the record with the bridge's key (step 3). EdDSA over the
// compact JWS' signing input; only the signature goes back.
type Signer interface {
	Sign(data []byte) ([]byte, error)
	KeyID() string
}

// MaxSigners bounds the offered keys, as the registrar's record does.
const MaxSigners = 16

var (
	versionRE  = regexp.MustCompile(`^v\d+\.\d+\.\d+$`)
	signerRE   = regexp.MustCompile(`^(ssh-ed25519|sk-ssh-ed25519@openssh\.com) [A-Za-z0-9+/]+={0,2}$`)
	errNoKey   = errors.New("no signing key")
	errNoSet   = errors.New("no release set")
	errSigners = errors.New("release signers: 1 to 16 ssh-ed25519 keys, one per line")
)

// Handler answers the release record. Signers and Version come from the
// bridge's settings; the zero Version with no Signers answers 404, as the
// registrar does when nothing is set.
type Handler struct {
	Signers []string // "<type> <base64>", 1 to 16, distinct
	Version string   // "", "latest" or "v<x.y.z>"
	Signer  Signer
	SetAt   time.Time // the bridge's start: it orders and keys nothing
	Now     func() time.Time
}

// New validates the settings and returns the handler.
func New(signers []string, version string, signer Signer, setAt time.Time) (*Handler, error) {
	seen := map[string]bool{}
	for _, s := range signers {
		s = strings.TrimSpace(s)
		if !signerRE.MatchString(s) {
			return nil, errSigners
		}
		if seen[s] {
			return nil, errSigners
		}
		seen[s] = true
	}
	if len(seen) > MaxSigners {
		return nil, errSigners
	}
	if version != "" && version != "latest" && !versionRE.MatchString(version) {
		return nil, fmt.Errorf("release version %q: v<x.y.z>, latest or empty", version)
	}
	return &Handler{Signers: signers, Version: version, Signer: signer, SetAt: setAt, Now: time.Now}, nil
}

// SignersFromFile reads one key per line, empty lines skipped: the release
// signers file.
func SignersFromFile(path string) ([]string, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var out []string
	for _, line := range strings.Split(string(b), "\n") {
		if line = strings.TrimSpace(line); line != "" {
			out = append(out, line)
		}
	}
	return out, nil
}

func (h *Handler) now() time.Time {
	if h.Now != nil {
		return h.Now()
	}
	return time.Now()
}

// ServeHTTP writes the record, or the registrar's 404.
func (h *Handler) ServeHTTP(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	if h.Signer == nil {
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = io.WriteString(w, `{"error":"no signing key"}`)
		return
	}
	if h.Version == "" && len(h.Signers) == 0 {
		w.WriteHeader(http.StatusNotFound)
		_, _ = io.WriteString(w, `{"error":"no release set"}`)
		return
	}
	now := h.now()
	header, _ := json.Marshal(map[string]string{
		"alg": "EdDSA", "typ": "mm-agent-release+jwt", "kid": h.Signer.KeyID(),
	})
	claims := map[string]any{
		"signers":   h.Signers,
		"set_at_ms": h.SetAt.UnixMilli(),
		"iat":       now.Unix(),
		"exp":       now.Add(time.Hour).Unix(),
	}
	if h.Version != "" {
		claims["version"] = h.Version
	}
	payload, err := json.Marshal(claims)
	if err != nil {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = io.WriteString(w, `{"error":"record"}`)
		return
	}
	b64 := base64.RawURLEncoding
	input := b64.EncodeToString(header) + "." + b64.EncodeToString(payload)
	sig, err := h.Signer.Sign([]byte(input))
	if err != nil {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = io.WriteString(w, `{"error":"signature"}`)
		return
	}
	out, err := json.Marshal(map[string]string{"record": input + "." + b64.EncodeToString(sig)})
	if err != nil {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = io.WriteString(w, `{"error":"record"}`)
		return
	}
	_, _ = w.Write(out)
}

// FileSigner is an Ed25519 key from a file (64-byte private key or its 32-byte
// seed): the bridge's own signing key once step 3 sets it.
type FileSigner struct {
	key ed25519.PrivateKey
	kid string
}

// NewFileSigner reads a raw ed25519 private key or seed.
func NewFileSigner(path string) (*FileSigner, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	b = bytesTrimSpace(b)
	var key ed25519.PrivateKey
	switch len(b) {
	case ed25519.SeedSize:
		key = ed25519.NewKeyFromSeed(b)
	case ed25519.PrivateKeySize:
		key = ed25519.PrivateKey(b)
	default:
		return nil, errNoKey
	}
	pub := key.Public().(ed25519.PublicKey)
	return &FileSigner{key: key, kid: base64.RawURLEncoding.EncodeToString(pub[:8])}, nil
}

// Sign implements Signer.
func (f *FileSigner) Sign(data []byte) ([]byte, error) {
	return ed25519.Sign(f.key, data), nil
}

// KeyID implements Signer.
func (f *FileSigner) KeyID() string { return f.kid }

func bytesTrimSpace(b []byte) []byte {
	return []byte(strings.TrimSpace(string(b)))
}
