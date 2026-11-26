// Package commands serves the bridge's slash commands and their signed records
// (issue #151 step 3): the registrar's mode, mandate and debug commands, on
// listeners of their own, signed with the bridge's own Ed25519 key and kept in
// the bridge's NATS bucket instead of the registrar's files.
//
// The HTTP behavior is the registrar's: the same routes, the same bodies, the
// same per-IP buckets (30 command posts and 120 reads a minute), the same 64 KiB
// body limit, and 413 for a body over it.
package commands

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/registrar/core"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/registrar/mattermost"
)

const (
	// maxBody is the largest command POST the registrar accepts; a slash
	// command's form is far smaller.
	maxBody = 64 << 10
	// commandRatePerMin is the general bucket of the command posts.
	commandRatePerMin = 30
	// readRatePerMin is each read route's own bucket, so agents polling their
	// channel's record never starve a command.
	readRatePerMin = 120
	// pruneEvery is how often ended debug settings are dropped while running.
	pruneEvery = time.Hour
)

// Config is what the commands need: the Mattermost admin token their lookups
// use, the signing key, the records' persister and each command's own
// verification tokens.
type Config struct {
	MMURL      string
	MMTeam     string
	AdminToken string
	// AllowHTTP lets the admin token go over plain http to a host that is not
	// loopback; the bridge's own CB_BRIDGE_MM_ALLOW_HTTP, already validated.
	AllowHTTP bool
	Attestor  *core.Attestor
	// Persister keeps the three stores (keys modes, mandates, debug).
	Persister core.Persister
	// The command tokens; an empty list leaves that command out.
	ModeTokens    []string
	MandateTokens []string
	DebugTokens   []string
	// TrustedProxyHops is how many proxies append to X-Forwarded-For.
	TrustedProxyHops int
	Log              *slog.Logger
	// Now is time.Now in production; the tests fix it.
	Now func() time.Time
}

// Server is the commands: the read records on the main listener and the
// command posts on their own one.
type Server struct {
	svc *core.Service
	log *slog.Logger
	// trustedProxyHops is how many proxies in front of the bridge append to
	// X-Forwarded-For; 0 ignores the header, nothing is trusted unconfigured.
	trustedProxyHops int
	// One adapter per command, verifying that command's own tokens; nil when
	// its token list is empty.
	modePort    core.ProviderPort
	mandatePort core.ProviderPort
	debugPort   core.ProviderPort
	rate        *limiter
	modeRate    *limiter
	mandateRate *limiter
	debugRate   *limiter
}

// New builds the commands. It reads the three stores through cfg.Persister and
// refuses a missing key or persister: without either no record can be served.
func New(cfg Config) (*Server, error) {
	switch {
	case cfg.Attestor == nil:
		return nil, errors.New("commands: no signing key")
	case cfg.Persister == nil:
		return nil, errors.New("commands: no records store")
	}
	log := cfg.Log
	if log == nil {
		log = slog.Default()
	}
	now := cfg.Now
	if now == nil {
		now = time.Now
	}
	mmcfg := mattermost.Config{URL: cfg.MMURL, AllowHTTP: cfg.AllowHTTP, Token: cfg.AdminToken, Team: cfg.MMTeam}
	port, err := mattermost.New(mmcfg, log)
	if err != nil {
		return nil, err
	}
	modes, err := core.OpenModeStoreWith(cfg.Persister, "modes")
	if err != nil {
		return nil, err
	}
	mandates, err := core.OpenMandateStoreWith(cfg.Persister, "mandates")
	if err != nil {
		return nil, err
	}
	debug, err := core.OpenDebugStoreWith(cfg.Persister, "debug")
	if err != nil {
		return nil, err
	}
	s := &Server{
		log: log,
		svc: &core.Service{Provider: port, Log: log, Attestor: cfg.Attestor,
			Modes: modes, Mandates: mandates, Debug: debug, Now: now},
		trustedProxyHops: cfg.TrustedProxyHops,
		rate:             newLimiter(commandRatePerMin),
		modeRate:         newLimiter(readRatePerMin),
		mandateRate:      newLimiter(readRatePerMin),
		debugRate:        newLimiter(readRatePerMin),
	}
	for _, l := range []*limiter{s.rate, s.modeRate, s.mandateRate, s.debugRate} {
		l.now = now
	}
	if s.modePort, err = commandPort(mmcfg, cfg.ModeTokens, log); err != nil {
		return nil, err
	}
	if s.mandatePort, err = commandPort(mmcfg, cfg.MandateTokens, log); err != nil {
		return nil, err
	}
	if s.debugPort, err = commandPort(mmcfg, cfg.DebugTokens, log); err != nil {
		return nil, err
	}
	return s, nil
}

// commandPort is one command's adapter, verifying only its own tokens; nil
// when the list is empty, which leaves the command out.
func commandPort(cfg mattermost.Config, tokens []string, log *slog.Logger) (core.ProviderPort, error) {
	if len(tokens) == 0 {
		return nil, nil
	}
	cfg.CommandTokens = tokens
	return mattermost.New(cfg, log)
}

// Reads adds the public record routes to the bridge's main listener: one record
// per channel, signed fresh for each request.
func (s *Server) Reads(mux *http.ServeMux) {
	mux.HandleFunc("GET /register/v1/modes/{channel_id}", s.handleMode)
	mux.HandleFunc("GET /register/v1/mandates/{channel_id}", s.handleMandate)
	mux.HandleFunc("GET /register/v1/debug/{channel_id}", s.handleDebug)
}

// Commands is the command listener's handler: a command only while its token
// list is set. No read route is here.
func (s *Server) Commands() http.Handler {
	mux := http.NewServeMux()
	if s.modePort != nil {
		mux.HandleFunc("POST /register/v1/mode", s.command(s.modePort, s.svc.ModeCommand))
	}
	if s.mandatePort != nil {
		mux.HandleFunc("POST /register/v1/mandate", s.command(s.mandatePort, s.svc.MandateCommand))
	}
	if s.debugPort != nil {
		mux.HandleFunc("POST /register/v1/debug", s.command(s.debugPort, s.svc.DebugCommand))
	}
	return mux
}

// TLSConfig is the command listener's TLS: the bridge's certificate and key,
// and at least TLS 1.2.
func TLSConfig(certFile, keyFile string) (*tls.Config, error) {
	cert, err := tls.LoadX509KeyPair(certFile, keyFile)
	if err != nil {
		return nil, fmt.Errorf("commands: %w", err)
	}
	return &tls.Config{Certificates: []tls.Certificate{cert}, MinVersion: tls.VersionTLS12}, nil
}

// PruneDebug drops ended debug settings now and then hourly until ctx ends. The
// command store is the only one that grows by itself: an ended setting is
// unpublished but would stay stored.
func (s *Server) PruneDebug(ctx context.Context) {
	s.svc.PruneDebug()
	go func() {
		t := time.NewTicker(pruneEvery)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				s.svc.PruneDebug()
			}
		}
	}()
}

// command is one command post: the registrar's 64 KiB limit, its bucket, its
// adapter and its private answer.
func (s *Server) command(port core.ProviderPort, run func(context.Context, core.Invocation) string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		body := &limitedBody{Reader: http.MaxBytesReader(w, r.Body, maxBody)}
		r.Body = body
		// A form that announces its size is refused before a rate hit is spent;
		// the reader catches one that does not (chunked).
		if r.ContentLength > maxBody {
			http.Error(w, "request body too large", http.StatusRequestEntityTooLarge)
			return
		}
		if !s.rate.allow(s.clientIP(r)) {
			http.Error(w, "rate limit", http.StatusTooManyRequests)
			return
		}
		inv, err := port.VerifyInvocation(r)
		if err != nil {
			if body.tooBig {
				// The Mattermost adapter reports ParseForm's error as a bad
				// request; a body over the limit is a 413.
				http.Error(w, "request body too large", http.StatusRequestEntityTooLarge)
				return
			}
			if errors.Is(err, core.ErrForbidden) {
				port.ReplyPrivate(w, inv, strings.TrimPrefix(err.Error(), core.ErrForbidden.Error()+": "))
				return
			}
			s.fail(w, err)
			return
		}
		port.ReplyPrivate(w, inv, run(r.Context(), inv))
	}
}

// limitedBody remembers that the 64 KiB reader refused, so the handler can
// answer 413 where the adapter's form error would be a 400.
type limitedBody struct {
	io.Reader
	tooBig bool
}

func (b *limitedBody) Read(p []byte) (int, error) {
	n, err := b.Reader.Read(p)
	var large *http.MaxBytesError
	if errors.As(err, &large) {
		b.tooBig = true
	}
	return n, err
}

// Close closes what the limit wraps; the server closes the body after the
// handler either way.
func (b *limitedBody) Close() error {
	if c, ok := b.Reader.(io.Closer); ok {
		return c.Close()
	}
	return nil
}

// fail maps a core error kind to a status; the message after the kind is meant
// for the caller.
func (s *Server) fail(w http.ResponseWriter, err error) {
	status, msg := http.StatusInternalServerError, "internal error"
	switch {
	case errors.Is(err, core.ErrBadRequest):
		status, msg = http.StatusBadRequest, err.Error()
	case errors.Is(err, core.ErrUnauthorized):
		status, msg = http.StatusUnauthorized, err.Error()
	case errors.Is(err, core.ErrForbidden):
		status, msg = http.StatusForbidden, err.Error()
	case errors.Is(err, core.ErrConflict):
		status, msg = http.StatusConflict, err.Error()
	case errors.Is(err, core.ErrUpstream):
		// "provider error: create bot: <detail>" -> "create bot failed"; the
		// detail goes to the log only.
		status = http.StatusBadGateway
		msg = strings.SplitN(strings.TrimPrefix(err.Error(), core.ErrUpstream.Error()+": "), ":", 2)[0] + " failed"
	}
	if status >= 500 {
		s.log.Error("request failed", "err", err)
	}
	http.Error(w, msg, status)
}

// handleMode publishes a channel's ask mode as a signed record, signed fresh
// for each request.
func (s *Server) handleMode(w http.ResponseWriter, r *http.Request) {
	s.record(w, r, s.modeRate, s.svc.ModeRecord, core.ErrNoMode, "no mode set", "sign mode record")
}

// handleMandate publishes a channel's mandate as a signed record, signed fresh
// for each request.
func (s *Server) handleMandate(w http.ResponseWriter, r *http.Request) {
	s.record(w, r, s.mandateRate, s.svc.MandateRecord, core.ErrNoMandate, "no mandate set", "sign mandate record")
}

// handleDebug publishes a channel's debug setting as a signed record, signed
// fresh for each request; both no setting and an ended one are 404.
func (s *Server) handleDebug(w http.ResponseWriter, r *http.Request) {
	s.record(w, r, s.debugRate, s.svc.DebugRecord, core.ErrNoDebug, "no debug set", "sign debug record")
}

// record is one read route: its own bucket, its store's record, the registrar's
// exact bodies.
func (s *Server) record(w http.ResponseWriter, r *http.Request, rate *limiter,
	sign func(string) (string, error), none error, noneMsg, logMsg string) {
	if !rate.allow(s.clientIP(r)) {
		http.Error(w, "rate limit", http.StatusTooManyRequests)
		return
	}
	rec, err := sign(r.PathValue("channel_id"))
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	switch {
	case errors.Is(err, core.ErrBadRequest):
		w.WriteHeader(http.StatusBadRequest)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "invalid channel id"})
	case errors.Is(err, none):
		w.WriteHeader(http.StatusNotFound)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": noneMsg})
	case err != nil:
		s.log.Error(logMsg, "err", err)
		w.WriteHeader(http.StatusInternalServerError)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "internal error"})
	default:
		_ = json.NewEncoder(w).Encode(map[string]string{"record": rec})
	}
}

// clientIP is the address the rate buckets count. Each trusted proxy appends the
// peer it saw to X-Forwarded-For, so only entries from the right are the proxies'
// own; the left ones are whatever the client sent. With too few entries, or none,
// or with TrustedProxyHops 0, the connection's own address counts.
func (s *Server) clientIP(r *http.Request) string {
	if n := s.trustedProxyHops; n > 0 {
		var entries []string
		for _, h := range r.Header.Values("X-Forwarded-For") {
			entries = append(entries, strings.Split(h, ",")...)
		}
		if len(entries) >= n {
			// Only an IP is a bucket key: junk (a port, text) falls back to the
			// peer.
			if ip := net.ParseIP(strings.TrimSpace(entries[len(entries)-n])); ip != nil {
				return ip.String()
			}
		}
	}
	if host, _, err := net.SplitHostPort(r.RemoteAddr); err == nil && host != "" {
		return host
	}
	return r.RemoteAddr
}

// limiter is a per-IP sliding window of one minute.
type limiter struct {
	perMin int
	// now is time.Now; a test fixes it.
	now  func() time.Time
	mu   sync.Mutex
	hits map[string][]time.Time
	// swept is when keys without a hit in the last minute were last dropped.
	swept time.Time
}

func newLimiter(perMin int) *limiter {
	return &limiter{perMin: perMin, now: time.Now, hits: map[string][]time.Time{}}
}

func (l *limiter) allow(ip string) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := l.now()
	cut := now.Add(-time.Minute)
	// A key that stays quiet for a minute holds nothing; without this sweep the
	// map would keep every address ever seen.
	if now.Sub(l.swept) >= time.Minute {
		// A new map, because a Go map keeps its table size after deletes: a
		// flood of distinct addresses would leave the memory at its peak.
		fresh := make(map[string][]time.Time, len(l.hits))
		for k, ts := range l.hits {
			if len(ts) > 0 && ts[len(ts)-1].After(cut) {
				fresh[k] = ts
			}
		}
		l.hits = fresh
		l.swept = now
	}
	var kept []time.Time
	for _, t := range l.hits[ip] {
		if t.After(cut) {
			kept = append(kept, t)
		}
	}
	if len(kept) >= l.perMin {
		l.hits[ip] = kept
		return false
	}
	l.hits[ip] = append(kept, now)
	return true
}
