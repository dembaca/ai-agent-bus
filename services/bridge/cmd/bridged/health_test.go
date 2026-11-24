package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// The probe counts homes, not flags: one home's recovery never hides another
// home's retries.
func TestTheProbeCountsEveryHomeApart(t *testing.T) {
	h := &health{}
	probe := func() int {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/healthz", nil))
		return w.Code
	}
	if got := probe(); got != http.StatusOK {
		t.Fatalf("a fresh bridge: %d", got)
	}
	h.stuckOn(true) // the first home starts retrying
	if got := probe(); got != http.StatusServiceUnavailable {
		t.Fatalf("a home retries: %d", got)
	}
	h.stuckOn(true)  // and a second one
	h.stuckOn(false) // the first goes through again
	if got := probe(); got != http.StatusServiceUnavailable {
		t.Fatalf("the second home still retries: %d", got)
	}
	h.stuckOn(false)
	if got := probe(); got != http.StatusOK {
		t.Fatalf("both went through: %d", got)
	}
	h.failed.Store(true)
	if got := probe(); got != http.StatusServiceUnavailable {
		t.Fatalf("a home stopped: %d", got)
	}
}
