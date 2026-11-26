// Package release serves the release record (RT-6.32, #151 step 4): the
// registrar's route `GET /register/v1/release`, the same compact JWS, signed
// with the bridge's own key (step 3) — the registrar's key is never copied.
//
// The hosts read it with their pinned keys and their own Python
// (`mattermost_ai_agent_bus.selfupdate`, `attest`), so the record's shape is a
// contract: header `{"alg":"EdDSA","typ":"mm-agent-release+jwt","kid":…}`,
// claims `signers` (1 to 16 distinct ssh-ed25519 keys, canonical),
// `version` (`v<x.y.z>`, or `latest`, optional: keys only means no target),
// `set_at_ms`, `iat`, `exp` (an hour later).
package release

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/registrar/core"
)

// Handler answers the release record. Signers and Version come from the
// bridge's settings; Attest is the bridge's own key (step 3).
type Handler struct {
	Signers []string // canonical "<type> <base64>", 1 to 16
	Version string   // "", "latest" or "v<x.y.z>"
	Attest  *core.Attestor
	SetAt   time.Time // the bridge's start: it orders and keys nothing
	Now     func() time.Time
}

// New validates the settings and returns the handler. The signers text is in
// authorized_keys form, one key per line (the registrar's parser and limits).
func New(signersText, version string, attest *core.Attestor, setAt time.Time) (*Handler, error) {
	signers, err := core.ParseReleaseSigners(signersText, false)
	if err != nil {
		return nil, err
	}
	if len(signers) == 0 {
		return nil, errors.New("release signers: at least one ssh-ed25519 key")
	}
	if version != "" && !core.ValidReleaseVersion(version) {
		return nil, errors.New("release version: v<x.y.z> (no leading zeros), latest, or empty")
	}
	if setAt.IsZero() {
		return nil, errors.New("release set_at: the bridge's start time")
	}
	return &Handler{Signers: signers, Version: version, Attest: attest, SetAt: setAt, Now: time.Now}, nil
}

// SignersText reads a release signers file.
func SignersText(path string) (string, error) {
	b, err := os.ReadFile(path)
	return string(b), err
}

func (h *Handler) now() time.Time {
	if h.Now != nil {
		return h.Now()
	}
	return time.Now()
}

// ServeHTTP writes the record, or the registrar's exact 404.
func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// The hosts verify the record, never cache it; only a read asks for it.
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Type", "application/json")
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		w.Header().Set("Allow", "GET, HEAD")
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	if h.Attest == nil {
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = io.WriteString(w, `{"error":"no signing key"}`)
		return
	}
	if h.Version == "" && len(h.Signers) == 0 {
		w.WriteHeader(http.StatusNotFound)
		_, _ = io.WriteString(w, `{"error":"no release set"}`)
		return
	}
	rec, err := h.Attest.SignRelease(core.ReleaseClaims{
		Signers: h.Signers, Version: h.Version, SetAtMs: h.SetAt.UnixMilli(),
	}, h.now())
	if err != nil {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = io.WriteString(w, `{"error":"record"}`)
		return
	}
	out, err := json.Marshal(map[string]string{"record": rec})
	if err != nil {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = io.WriteString(w, `{"error":"record"}`)
		return
	}
	_, _ = w.Write(out)
}
