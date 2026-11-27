package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// part is one of the bridge's parts for the probe's test: it is failing while
// down says so.
type part struct {
	err  string
	down bool
}

func (p part) Failure() (string, bool) { return p.err, p.down }

// The probe counts homes, not flags: one home's recovery never hides another
// home's retries. A part that cannot answer without stopping the bridge is
// named in the body, and never fails the probe.
func TestTheProbeCountsEveryHomeApart(t *testing.T) {
	h := &health{requests: part{err: "subscribe req.>: refused", down: true}}
	probe := func() (int, string) {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/healthz", nil))
		return w.Code, w.Body.String()
	}
	// requests failing alone: 200, the part named beside the ok
	code, body := probe()
	if code != http.StatusOK {
		t.Fatalf("requests cannot answer: %d", code)
	}
	if want := "ok\nrequests: subscribe req.>: refused\n"; body != want {
		t.Fatalf("the probe's body %q, want %q", body, want)
	}
	h.requests = part{down: false} // the rights were granted
	if code, body = probe(); code != http.StatusOK || body != "ok\n" {
		t.Fatalf("requests answer again: %d %q", code, body)
	}
	h.stuckOn(true) // the first home starts retrying
	if code, _ = probe(); code != http.StatusServiceUnavailable {
		t.Fatalf("a home retries: %d", code)
	}
	h.stuckOn(true)  // and a second one
	h.stuckOn(false) // the first goes through again
	if code, _ = probe(); code != http.StatusServiceUnavailable {
		t.Fatalf("the second home still retries: %d", code)
	}
	h.stuckOn(false)
	if code, _ = probe(); code != http.StatusOK {
		t.Fatalf("both went through: %d", code)
	}
	h.failed.Store(true)
	if code, body = probe(); code != http.StatusServiceUnavailable {
		t.Fatalf("a home stopped: %d", code)
	}
	if want := "a home fails or retries; see the log\n"; body != want {
		t.Fatalf("a home stopped: %q, want %q", body, want)
	}
}
