package main

import (
	"net/http"
	"strings"
	"sync/atomic"
)

// failing is a part of the bridge that is not answering while it says why. Its
// failure is no reason to stop the bridge or to fail the probe: the parts that
// carry people's posts go on.
type failing interface {
	Failure() (string, bool)
}

// health is the probe's state: a home that has been retrying one message for
// too long is counted, and another home that recovers cannot clear it — the
// probe stays 503 while any home is stuck. A part that keeps running without
// answering (the request scaffold without its NATS rights) is named in the
// body, while the probe stays 200.
type health struct {
	failed   atomic.Bool
	stuck    atomic.Int32
	requests failing // nil while the request scaffold is off
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
	var b strings.Builder
	if h.failed.Load() || h.stuck.Load() > 0 {
		b.WriteString("a home fails or retries; see the log\n")
	} else {
		b.WriteString("ok\n")
	}
	// The request scaffold keeps answering nothing until the rights are
	// granted: named here, but never a reason for a 503.
	if h.requests != nil {
		if err, bad := h.requests.Failure(); bad {
			b.WriteString("requests: " + err + "\n")
		}
	}
	status := http.StatusOK
	if h.failed.Load() || h.stuck.Load() > 0 {
		status = http.StatusServiceUnavailable
	}
	w.WriteHeader(status)
	_, _ = w.Write([]byte(b.String()))
}
