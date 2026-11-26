package callout

import (
	"errors"
	"fmt"
	"net"
	"strings"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/theft"
)

// The theft check (decision 10, step 3 of #154): a session alternating between
// networks, or a host key doing so, is a copy. The session is refused for a
// while — a retry included — and the host key is reported only, since a copied
// key is the owner's to rotate and refusing it would lock the owner out. The
// guard needs no credential, so no Keycloak admin right is involved; the stolen
// token stays valid at Keycloak until it runs out or the owner ends the
// session (RT-6.34, the owner's decision).
var (
	// ErrCopiedUse refuses an issue for a session seen alternating between
	// networks.
	ErrCopiedUse = errors.New("login: this session was used from alternating networks; " +
		"refused (the session may be copied)")
)

// Watcher is the theft detector (theft.Guard): per session and host key,
// whether the issue comes from the same network, a new one, or alternates with
// another. A session may be refused, a host key only reported.
type Watcher interface {
	Session(sid, network string) theft.Verdict
	HostKey(nkey, network string) theft.Verdict
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
// network, reports the host key and refuses a session that alternates between
// networks.
func (s *Service) watchTheft(nkey, sid, addr string) error {
	wt := s.watchHook()
	if wt == nil {
		return nil
	}
	network := source(addr)
	if nkey != "" {
		// Reported, never refused: the verdict is in the log, and the owner is
		// told to rotate the key.
		wt.HostKey(nkey, network)
	}
	if sid != "" && wt.Session(sid, network) == theft.Copied {
		return ErrCopiedUse
	}
	return nil
}

// source is the network an issue comes from, as a prefix (IPv4 /24, IPv6 /64):
// two addresses of one network are one network, so dual-stack, returning IPv6
// privacy addresses and one host's several paths are not mistaken for a copy.
// Two users on one prefix — a shared NAT or proxy, or a thief in the same /24 or
// /64 — are invisible (the limit).
func source(host string) string {
	host = strings.TrimSpace(host)
	if h, _, err := net.SplitHostPort(host); err == nil {
		host = h
	}
	host = strings.Trim(host, "[]")
	ip := net.ParseIP(host)
	if ip == nil {
		return host
	}
	if v4 := ip.To4(); v4 != nil {
		return fmt.Sprintf("%d.%d.%d.0/24", v4[0], v4[1], v4[2])
	}
	return ip.Mask(net.CIDRMask(64, 128)).String() + "/64"
}
