package callout

import (
	"errors"
	"net"
	"strings"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/theft"
)

// The theft check (decision 10, step 3 of #154): one Keycloak session or one
// host key used from two networks at the same time is refused; a network
// change one after another is only reported. The guard needs no credential, so
// no Keycloak admin right is involved; what the refusal costs the owner is
// named in RT-6.34 (the stolen token stays valid at Keycloak until it runs out
// or the owner ends the session, which the owner decides).
var (
	// ErrConcurrentUse refuses an issue for a session or host key seen from two
	// networks at once.
	ErrConcurrentUse = errors.New("login: this session or host key was used from two networks at once; " +
		"refused (the owner's session may be stolen)")
)

// Watcher is the theft detector (theft.Guard): per id, whether the issue comes
// from the same network, a new one, or two at once.
type Watcher interface {
	Observe(id, network string) theft.Verdict
}

// SetWatcher sets the theft detector. nil (the default) watches nothing.
func (s *Service) SetWatcher(w Watcher) {
	s.cmu.Lock()
	defer s.cmu.Unlock()
	s.watcher = w
}

func (s *Service) watchHook() Watcher {
	s.cmu.RLock()
	defer s.cmu.RUnlock()
	return s.watcher
}

// watchTheft notes the issue of a host key and its session from the client's
// address and refuses it when either is in use from two networks at once.
func (s *Service) watchTheft(nkey, sid, addr string) error {
	wt := s.watchHook()
	if wt == nil {
		return nil
	}
	network := source(addr)
	for _, id := range []string{"nkey:" + nkey, "sid:" + sid} {
		if strings.HasSuffix(id, ":") {
			continue // no session id in the token: the host key alone is watched
		}
		if wt.Observe(id, network) == theft.Concurrent {
			return ErrConcurrentUse
		}
	}
	return nil
}

// source is the client's address without its port: the network an issue comes
// from, as the NATS server reports it (IPv6 in brackets). An address without a
// port is taken as it is.
func source(host string) string {
	host = strings.TrimSpace(host)
	if h, _, err := net.SplitHostPort(host); err == nil {
		return strings.Trim(h, "[]")
	}
	return strings.Trim(host, "[]")
}
