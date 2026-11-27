package release

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// The wiring's unset case (issue #151 step 4): with no signers file and no
// version the route is still served, with the registrar's exact 404, which a
// host's release check treats as normal; the bridge must not refuse to start
// for it.
func TestTheUnsetHandlerServesTheRegistrar404(t *testing.T) {
	a, _ := testAttestor(t)
	h := &Handler{Attest: a, SetAt: time.Now()}
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/register/v1/release", nil))
	if rr.Code != http.StatusNotFound || strings.TrimSpace(rr.Body.String()) != `{"error":"no release set"}` {
		t.Fatalf("%d %q", rr.Code, rr.Body.String())
	}
	if ct := rr.Header().Get("Content-Type"); ct != "application/json" {
		t.Fatalf("content type %q", ct)
	}
}
