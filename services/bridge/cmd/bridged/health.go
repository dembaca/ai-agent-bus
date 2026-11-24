package main

import (
	"net/http"
	"sync/atomic"
)

// health is the probe's state: a home that has been retrying one message for
// too long is counted, and another home that recovers cannot clear it — the
// probe stays 503 while any home is stuck.
type health struct {
	failed atomic.Bool
	stuck  atomic.Int32
}

// stuckOn is a reader's Stuck callback. A reader reports the transition, so
// the count follows and each home counts for itself.
func (h *health) stuckOn(on bool) {
	if on {
		h.stuck.Add(1)
	} else {
		h.stuck.Add(-1)
	}
}

func (h *health) ServeHTTP(w http.ResponseWriter, _ *http.Request) {
	if h.failed.Load() || h.stuck.Load() > 0 {
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte("a home fails or retries; see the log\n"))
		return
	}
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("ok\n"))
}
