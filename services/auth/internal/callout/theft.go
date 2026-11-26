package callout

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"strings"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/theft"
)

// The theft check (decision 10, step 3 of #154): a session alternating between
// networks, or a host key doing so, is a copy. The session is refused for a
// while — a retry included — and the host key is reported only, since a copied
// key is the owner's to rotate and refusing it would lock the owner out. Both
// are published for the owner (`<presence>.notice.<anchor>.<verdict>.v1`), a
// subject only the login service may publish; the owner's live MCP servers turn a copied
// notice into a DM to their human. The guard needs no credential, so no
// Keycloak admin right is involved; the stolen token stays valid at Keycloak
// until it runs out or the owner ends the session (RT-6.34, the owner's
// decision).
var (
	// ErrCopiedUse refuses an issue for a session seen alternating between
	// networks.
	ErrCopiedUse = errors.New("login: this session was used from alternating networks; " +
		"refused (the session may be copied)")
)

// NoticeLifetime is how long a notice is delivered by a server that starts
// after it: long enough for a restart, short enough that no one is told of a
// long-gone use.
const NoticeLifetime = 24 * time.Hour

// Watcher is the theft detector (theft.Guard): per session and host key,
// whether the issue comes from the same network, a new one, or returns to an
// earlier one. A session may be refused, a host key only reported.
type Watcher interface {
	Session(sid, network string) theft.Verdict
	HostKey(nkey, network string) theft.Verdict
}

// Noticer publishes the owner's notice. Best effort: a failed publish is
// logged, never a reason to refuse or allow differently.
type Noticer interface {
	Notice(ctx context.Context, subject string, body []byte) error
}

// NATSNoticer publishes on the service's JetStream connection, off the JetStream
// API: the PRESENCE stream takes it as an ordinary message on a subject only
// this user may publish (nats-server.example.conf).
type NATSNoticer struct {
	NC *nats.Conn
}

// Notice implements Noticer.
func (n NATSNoticer) Notice(_ context.Context, subject string, body []byte) error {
	if err := n.NC.Publish(subject, body); err != nil {
		return err
	}
	return n.NC.Flush()
}

// Notice is the record published for the owner (RT-6.34). It names what was
// watched and its id, the network prefix, the agent ("" when the token named
// none) and its lifetime in server time; nothing in it decides anything, the
// subject does.
type Notice struct {
	V       int    `json:"v"`
	Kind    string `json:"kind"`    // session or host_key
	Verdict string `json:"verdict"` // moved (a report) or copied (a suspect copy)
	SID     string `json:"sid"`     // the Keycloak session id, or the host key
	Network string `json:"network"` // the prefix the issue came from
	Agent   string `json:"agent,omitempty"`
	Iat     int64  `json:"iat"`
	Exp     int64  `json:"exp"`
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

// SetNoticer sets the notice publisher. nil (the default) publishes none.
func (s *Service) SetNoticer(n Noticer) {
	s.cmu.Lock()
	defer s.cmu.Unlock()
	s.noticer = n
}

func (s *Service) noticeHook() Noticer {
	s.cmu.RLock()
	defer s.cmu.RUnlock()
	return s.noticer
}

// noticeSubject is where the owner's notice is published (RT-6.34): the owner
// as one subject token and the verdict, on the login service's presence prefix.
// One subject per verdict, so a move never erases a waiting copy.
func (s *Service) noticeSubject(anchor, verdict string) string {
	return s.cfg.Subjects.Presence + ".notice." + ownerToken(anchor) + "." + verdict + ".v1"
}

// watch is what the guard said about one issue.
type watch struct {
	host    theft.Verdict
	session theft.Verdict
	network string
}

// watchTheft notes the issue of a host key and its session from the client's
// network. The returned verdicts are what the callout acts on: a copied session
// refuses the issue, a copied host key is reported, and both are noticed.
func (s *Service) watchTheft(nkey, sid, addr string) watch {
	wt := s.watchHook()
	if wt == nil {
		return watch{network: source(addr)}
	}
	w := watch{network: source(addr), host: theft.Same, session: theft.Same}
	if nkey != "" {
		w.host = wt.HostKey(nkey, w.network)
	}
	if sid != "" {
		w.session = wt.Session(sid, w.network)
	}
	return w
}

// publishNotices tells the owner's servers what the guard saw, best effort: a
// moved record is a report, a copied one is the copy — the session refused, the
// host key to rotate.
func (s *Service) publishNotices(ctx context.Context, w watch, anchor, agent, nkey, sid string, now time.Time) {
	n := s.noticeHook()
	if n == nil {
		return
	}
	for _, one := range []struct {
		kind, id string
		v        theft.Verdict
	}{
		{"host_key", nkey, w.host},
		{"session", sid, w.session},
	} {
		if one.id == "" || (one.v != theft.Moved && one.v != theft.Copied) {
			continue
		}
		verdict := one.v.String()
		// One notice per id and verdict in a block's time: a refused session's
		// every retry says "copied" again, and the owner must not get a DM per
		// retry (RT-6.34). A copy after a move is its own notice.
		if !s.noticeDue(one.kind+"|"+one.id+"|"+verdict, now) {
			continue
		}
		body, err := json.Marshal(Notice{V: 1, Kind: one.kind, Verdict: verdict, SID: one.id,
			Network: w.network, Agent: agent, Iat: now.Unix(), Exp: now.Add(NoticeLifetime).Unix()})
		if err != nil {
			continue
		}
		c, cancel := context.WithTimeout(ctx, 2*time.Second)
		err = n.Notice(c, s.noticeSubject(anchor, verdict), body)
		cancel()
		if err != nil {
			s.log.Warn("auth callout", "event", "notice not published", "kind", one.kind, "err", err)
			continue
		}
		s.noticePublished(one.kind+"|"+one.id+"|"+verdict, now)
	}
}

// NoticeCooldown is how long one id and verdict goes unnoticed after a record:
// the guard's block, so a refused session's retries tell the owner once.
const NoticeCooldown = 30 * time.Minute

// noticeDue says whether the key was not published within NoticeCooldown.
func (s *Service) noticeDue(key string, now time.Time) bool {
	s.nmu.Lock()
	defer s.nmu.Unlock()
	at, ok := s.notices[key]
	return !ok || now.Sub(at) >= NoticeCooldown
}

func (s *Service) noticePublished(key string, now time.Time) {
	s.nmu.Lock()
	defer s.nmu.Unlock()
	if s.notices == nil {
		s.notices = map[string]time.Time{}
	}
	s.notices[key] = now
	// Keep the map small: drop what is older than a day.
	for k, at := range s.notices {
		if now.Sub(at) > 24*time.Hour {
			delete(s.notices, k)
		}
	}
}

// source is the network an issue comes from, as a prefix (IPv4 /24, IPv6 /64):
// two addresses of one network are one network, so returning IPv6 privacy
// addresses and one host's several paths are not mistaken for a copy. Two users
// on one prefix — a shared NAT or proxy, or a thief in the same /24 or /64 — are
// invisible (the limit).
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
