// Package admin is the directory's HTTP API (RT-6.40): channels, their
// members, the one-time import from Mattermost and the pending name claims.
// It speaks JSON only, wants a Keycloak access token on every request and
// never logs one. Nothing in the directory makes anyone an admin: the group
// claim of the token does.
package admin

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.org/x/time/rate"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/directory"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

// Verifier checks an access token (token.Verifier).
type Verifier interface {
	Verify(ctx context.Context, raw string) (token.Identity, error)
}

// Config configures the handler.
type Config struct {
	AdminGroup  string // the token group that makes an admin; required
	NameBinding string // "open" or "openid-only" (default open)
	BodyMax     int64  // default 64 KiB
	RatePerSec  float64
	Burst       int
	Log         *slog.Logger
	Now         func() time.Time
}

const maxLimiters = 10000

type handler struct {
	cfg      Config
	verifier Verifier
	store    *directory.Store
	source   Source
	mux      *http.ServeMux

	mu       sync.Mutex
	limiters map[string]*rate.Limiter
}

// New returns the API's handler.
func New(cfg Config, verifier Verifier, store *directory.Store, source Source) (http.Handler, error) {
	if strings.Trim(cfg.AdminGroup, "/ ") == "" {
		return nil, errors.New("admin: the admin group must be set")
	}
	switch cfg.NameBinding {
	case "":
		cfg.NameBinding = "open"
	case "open", "openid-only":
	default:
		return nil, errors.New("admin: name binding must be open or openid-only")
	}
	if cfg.BodyMax <= 0 {
		cfg.BodyMax = 64 << 10
	}
	if cfg.RatePerSec <= 0 {
		cfg.RatePerSec = 2
	}
	if cfg.Burst <= 0 {
		cfg.Burst = 10
	}
	if cfg.Log == nil {
		cfg.Log = slog.New(slog.DiscardHandler)
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	if verifier == nil || store == nil || source == nil {
		return nil, errors.New("admin: verifier, store and source are required")
	}
	h := &handler{cfg: cfg, verifier: verifier, store: store, source: source, limiters: map[string]*rate.Limiter{}}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/directory/channels", h.auth(false, h.listChannels))
	mux.HandleFunc("GET /v1/directory/channels/{name}", h.auth(false, h.getChannel))
	mux.HandleFunc("PUT /v1/directory/channels/{name}", h.auth(true, h.putChannel))
	mux.HandleFunc("PATCH /v1/directory/channels/{name}/members", h.auth(false, h.patchMembers))
	mux.HandleFunc("POST /v1/directory/import", h.auth(true, h.runImport))
	mux.HandleFunc("GET /v1/directory/pending", h.auth(true, h.listPending))
	mux.HandleFunc("POST /v1/directory/pending/{name}/confirm", h.auth(true, h.confirmPending))
	h.mux = mux
	return h, nil
}

func (h *handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if !h.allow(r) {
		fail(w, http.StatusTooManyRequests, "too many requests")
		return
	}
	h.mux.ServeHTTP(w, r)
}

func (h *handler) allow(r *http.Request) bool {
	ip, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		ip = r.RemoteAddr
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	l, ok := h.limiters[ip]
	if !ok {
		if len(h.limiters) >= maxLimiters {
			h.limiters = map[string]*rate.Limiter{}
		}
		l = rate.NewLimiter(rate.Limit(h.cfg.RatePerSec), h.cfg.Burst)
		h.limiters[ip] = l
	}
	return l.Allow()
}

func fail(w http.ResponseWriter, code int, msg string) {
	reply(w, code, map[string]string{"error": msg})
}

func reply(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

type authed func(w http.ResponseWriter, r *http.Request, id token.Identity)

// auth checks the bearer token, and the admin group when admin is set.
func (h *handler) auth(admin bool, next authed) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		scheme, raw, _ := strings.Cut(r.Header.Get("Authorization"), " ")
		raw = strings.TrimSpace(raw)
		if !strings.EqualFold(scheme, "Bearer") || raw == "" {
			fail(w, http.StatusUnauthorized, "a bearer token is required")
			return
		}
		id, err := h.verifier.Verify(r.Context(), raw)
		switch {
		case errors.Is(err, token.ErrDirectory):
			fail(w, http.StatusServiceUnavailable, "the chat could not be asked; try again later")
			return
		case err != nil:
			fail(w, http.StatusUnauthorized, "token refused: "+err.Error())
			return
		}
		if admin && !h.isAdmin(id) {
			fail(w, http.StatusForbidden, "admin group required")
			return
		}
		next(w, r, id)
	}
}

func (h *handler) isAdmin(id token.Identity) bool {
	want := strings.TrimPrefix(h.cfg.AdminGroup, "/")
	return slices.ContainsFunc(id.Groups, func(g string) bool { return strings.TrimPrefix(g, "/") == want })
}

// decode reads the JSON body strictly; it answers and returns false on error.
func (h *handler) decode(w http.ResponseWriter, r *http.Request, into any) bool {
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, h.cfg.BodyMax))
	dec.DisallowUnknownFields()
	err := dec.Decode(into)
	if err == nil {
		if _, err2 := dec.Token(); err2 != io.EOF {
			err = errors.New("trailing data")
		}
	}
	if err != nil {
		var tooBig *http.MaxBytesError
		if errors.As(err, &tooBig) {
			fail(w, http.StatusRequestEntityTooLarge, "body too large")
		} else {
			fail(w, http.StatusBadRequest, "body is not the expected JSON")
		}
		return false
	}
	return true
}

func etag(rev uint64) string { return `"` + strconv.FormatUint(rev, 10) + `"` }

// parseRev reads If-Match: "<rev>".
func parseRev(v string) (uint64, bool) {
	v = strings.Trim(strings.TrimSpace(v), `"`)
	n, err := strconv.ParseUint(v, 10, 64)
	return n, err == nil && n > 0
}

func (h *handler) logWrite(id token.Identity, event, channel string, change any) {
	h.cfg.Log.Info("directory write", "event", event, "sub", id.Subject, "user", id.Owner,
		"channel", channel, "change", change)
}

func (h *handler) storeError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, directory.ErrConflict):
		fail(w, http.StatusConflict, "changed meanwhile; read again")
	case errors.Is(err, directory.ErrBadKey):
		fail(w, http.StatusBadRequest, "not a valid name or sub")
	case errors.Is(err, directory.ErrNotFound):
		fail(w, http.StatusNotFound, "not found")
	default:
		h.cfg.Log.Error("directory error", "reason", err.Error())
		fail(w, http.StatusInternalServerError, "directory error")
	}
}

// sees: an admin sees every channel; anyone else a public channel and the
// channels they own or are a member of (ADR-016 point 4: no one reads the
// member list of a private channel they are not in).
func (h *handler) sees(id token.Identity, c directory.Channel) bool {
	return h.isAdmin(id) || c.Public || c.Owner == id.Subject || slices.Contains(c.Members, id.Subject)
}

func (h *handler) listChannels(w http.ResponseWriter, r *http.Request, id token.Identity) {
	chs, err := h.store.ListChannels(r.Context())
	if err != nil {
		h.storeError(w, err)
		return
	}
	chs = slices.DeleteFunc(chs, func(c directory.Channel) bool { return !h.sees(id, c) })
	reply(w, http.StatusOK, chs)
}

func (h *handler) getChannel(w http.ResponseWriter, r *http.Request, id token.Identity) {
	c, rev, ok, err := h.store.Channel(r.Context(), r.PathValue("name"))
	switch {
	case err != nil:
		h.storeError(w, err)
	case !ok || !h.sees(id, c): // a private channel one is not in looks missing
		fail(w, http.StatusNotFound, "no such channel")
	default:
		w.Header().Set("ETag", etag(rev))
		reply(w, http.StatusOK, c)
	}
}

type putBody struct {
	Owner    string            `json:"owner"`
	Public   bool              `json:"public"`
	Bindings map[string]string `json:"bindings"`
}

func (h *handler) putChannel(w http.ResponseWriter, r *http.Request, id token.Identity) {
	name := r.PathValue("name")
	create := r.Header.Get("If-None-Match") == "*"
	ifMatch := r.Header.Get("If-Match")
	if !create && ifMatch == "" {
		fail(w, http.StatusPreconditionRequired, "If-None-Match: * (create) or If-Match: \"<revision>\" (update) required")
		return
	}
	var b putBody
	if !h.decode(w, r, &b) {
		return
	}
	ctx := r.Context()
	c := directory.Channel{Name: name, Owner: b.Owner, Public: b.Public, Bindings: b.Bindings}
	var rev uint64
	if !create {
		want, ok := parseRev(ifMatch)
		if !ok {
			fail(w, http.StatusBadRequest, "If-Match must be a revision")
			return
		}
		cur, have, found, err := h.store.Channel(ctx, name)
		switch {
		case err != nil:
			h.storeError(w, err)
			return
		case !found:
			fail(w, http.StatusNotFound, "no such channel")
			return
		case have != want:
			fail(w, http.StatusConflict, "stale revision; read again")
			return
		}
		c.Members = cur.Members
		rev = want
	}
	n, err := h.store.PutChannel(ctx, c, rev)
	if err != nil {
		h.storeError(w, err)
		return
	}
	event, code := "channel updated", http.StatusOK
	if create {
		event, code = "channel created", http.StatusCreated
	}
	h.logWrite(id, event, name, map[string]any{"owner": c.Owner, "public": c.Public, "bindings": c.Bindings})
	w.Header().Set("ETag", etag(n))
	reply(w, code, c)
}

type membersBody struct {
	Add    []string `json:"add"`
	Remove []string `json:"remove"`
}

func (h *handler) patchMembers(w http.ResponseWriter, r *http.Request, id token.Identity) {
	name := r.PathValue("name")
	ctx := r.Context()
	cur, have, ok, err := h.store.Channel(ctx, name)
	switch {
	case err != nil:
		h.storeError(w, err)
		return
	case !ok:
		fail(w, http.StatusNotFound, "no such channel")
		return
	case cur.Owner == "" || cur.Owner != id.Subject:
		fail(w, http.StatusForbidden, "only the channel's owner keeps its members")
		return
	}
	ifMatch := r.Header.Get("If-Match")
	if ifMatch == "" {
		fail(w, http.StatusPreconditionRequired, "If-Match required")
		return
	}
	want, valid := parseRev(ifMatch)
	if !valid {
		fail(w, http.StatusBadRequest, "If-Match must be a revision")
		return
	}
	var b membersBody
	if !h.decode(w, r, &b) {
		return
	}
	if want != have {
		fail(w, http.StatusConflict, "stale revision; read again")
		return
	}
	set := slices.Clone(cur.Members)
	set = slices.DeleteFunc(set, func(m string) bool { return slices.Contains(b.Remove, m) })
	set = append(set, b.Add...)
	slices.Sort(set)
	set = slices.Compact(set)
	if len(set) > directory.MaxMembers {
		fail(w, http.StatusBadRequest, "too many members")
		return
	}
	next := cur
	next.Members = set
	n, err := h.store.PutChannel(ctx, next, have)
	if err != nil {
		h.storeError(w, err)
		return
	}
	h.logWrite(id, "members changed", name, map[string]any{"add": b.Add, "remove": b.Remove})
	w.Header().Set("ETag", etag(n))
	reply(w, http.StatusOK, next)
}

func (h *handler) runImport(w http.ResponseWriter, r *http.Request, id token.Identity) {
	rep, err := importAt(r.Context(), h.store, h.source, h.cfg.NameBinding, id, h.cfg.Now())
	switch {
	case errors.Is(err, ErrAlreadyImported):
		fail(w, http.StatusConflict, "the import has already run")
		return
	case err != nil:
		h.cfg.Log.Error("import failed; it can be run again", "reason", err.Error(), "sub", id.Subject)
		fail(w, http.StatusBadGateway, "the import stopped before its end and left no mark; run it again")
		return
	}
	h.logWrite(id, "import", "", map[string]any{"created": rep.ChannelsCreated, "updated": rep.ChannelsUpdated,
		"identities": rep.Identities, "members_added": rep.MembersAdded, "pending_added": rep.PendingAdded})
	reply(w, http.StatusOK, rep)
}

func (h *handler) listPending(w http.ResponseWriter, r *http.Request, _ token.Identity) {
	ps, err := h.store.ListPending(r.Context())
	if err != nil {
		h.storeError(w, err)
		return
	}
	reply(w, http.StatusOK, ps)
}

func (h *handler) confirmPending(w http.ResponseWriter, r *http.Request, id token.Identity) {
	name := r.PathValue("name")
	if err := h.store.ConfirmPending(r.Context(), name, id.Subject); err != nil {
		h.storeError(w, err)
		return
	}
	h.logWrite(id, "pending confirmed", "", map[string]any{"name": name})
	reply(w, http.StatusOK, map[string]string{"confirmed": name})
}
