// Package callout answers the NATS server's auth callout (ADR-014, points 5
// to 7): it checks the Keycloak token a host presents, that the host holds
// the nkey it names, which of the asked-for channels the human may use, and
// issues a user JWT for the agent, named by this service and valid 15 min.
//
// Nothing here logs a token, a seed or a password.
package callout

import (
	"context"
	"crypto/sha256"
	"encoding/base32"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/nats-io/jwt/v2"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nkeys"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/directory"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/theft"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

// Lifetime of an issued user JWT, counted from the check (ADR-014, point 7):
// every issue checks the Keycloak token again, and without the host's nkey
// seed nothing issued can be used by anyone else. A Keycloak session that
// ends therefore takes effect within this time.
const Lifetime = 15 * time.Minute

// Bounds of one request and of the work in flight.
const (
	MaxGuests  = 16 // guest channels asked for beyond this are refused
	MaxPending = 32 // requests answered at once
	// The limits in every user JWT.
	maxSubs    = 64
	maxPayload = 1 << 20
	// What is left of the server's request lifetime for sending the answer.
	answerMargin = 100 * time.Millisecond
)

// The audience of a server's auth request.
const requestAudience = "nats-authorization-request"

// Verifier is the token check (token.Verifier).
type Verifier interface {
	Verify(ctx context.Context, raw string) (token.Identity, error)
}

// Channels says of which of the given channels a human is a member
// (mattermost.Client). An error means the answer is unknown, not "no".
type Channels interface {
	Memberships(ctx context.Context, username string, channels []string) (map[string]bool, error)
}

// IdentityChannels is a Channels that can also answer for the Keycloak
// identity, by `sub` (directory.Channels, ADR-016 point 2). The service uses
// it when the Channels it was given offers it.
type IdentityChannels interface {
	MembershipsFor(ctx context.Context, username, sub string, channels []string) (map[string]bool, error)
}

// Anchors gives each identity its fixed anchor (directory.Anchors, RT-6.39, ADR-016
// decisions 7 and 9): what an agent's name hashes and its presence subject
// names as owner, so neither changes when the name in Keycloak does.
type Anchors interface {
	Anchor(ctx context.Context, sub, display string) (string, error)
}

// AccountIndex is an optional part of what SetAnchors is given
// (directory.Anchors has it): it keeps the chat account index current from a
// login. The service checks for it by type assertion and calls it only for a
// login whose chat account is proven to be the token's (token.Identity.
// ChatOpenID), never for a password account; an error refuses the login.
type AccountIndex interface {
	Account(ctx context.Context, sub, name string) error
}

// Hello is what a host sends as `auth_token` in its CONNECT, as JSON: the
// Keycloak access token and the channels it asks for. The channels are a
// wish; only those the human is a member of are granted.
type Hello struct {
	Token  string   `json:"token"`
	Home   string   `json:"home,omitempty"`
	Guests []string `json:"guests,omitempty"`
	Lobby  bool     `json:"lobby,omitempty"`
	// Since asks, per channel, for history: the stream sequence the host
	// saw last. It counts only for a granted channel; the key "@dm" is the
	// direct-message consumer (consumers.SinceDM), and "@presence" asks for the
	// presence consumer to be made anew (consumers.SincePresence: a new session
	// that has not read the presence stream yet).
	Since map[string]uint64 `json:"since,omitempty"`
	// Trusted are the host's MM_TRUSTED_HUMANS names; the attestation
	// vouches for them (attest.go).
	Trusted []string `json:"trusted,omitempty"`
	// TrustedCut says the host itself dropped invalid names or truncated
	// its list before sending; the attestation's cut is then true.
	TrustedCut bool `json:"trusted_cut,omitempty"`
}

// Consumers makes the push consumers an agent reads from (consumers.Manager).
// The service calls it after it built a credential and before it answers:
// an error refuses the login, since a credential without its consumers would
// look whole. It also removes the consumers of channels no longer in the
// credential.
type Consumers interface {
	Reconcile(ctx context.Context, agent, owner, anchor string, channels []string, since map[string]uint64) error
}

// Reasons a connection is refused, besides those of the token check. The
// text goes back to the NATS server, which logs it.
var (
	ErrRequest = errors.New("malformed auth request")
	ErrNoNkey  = errors.New("the host's nkey is missing or did not sign the nonce")
)

// Service answers auth callout requests.
type Service struct {
	cfg      Config
	issuer   nkeys.KeyPair // signs responses and user JWTs: the account's key
	xkey     nkeys.KeyPair // decrypts requests, encrypts responses; nil: plain
	verifier Verifier
	channels Channels
	brake    *Brake
	log      *slog.Logger
	now      func() time.Time
	pending  chan struct{}

	subMu    sync.Mutex          // guards subLocks
	subLocks map[string]*subLock // one lock per identity while its account record is written

	cmu        sync.RWMutex
	consumers  Consumers            // nil: none are made
	anchors    Anchors              // nil: the anchor is the owner's name, as before ADR-016
	attester   Attester             // nil: no attestation is published
	trust      Trust                // nil: a trusted name is its own anchor
	trustIndex TrustIndex           // nil: no trust index is written (RT-6.43 step 3)
	watcher    Watcher              // nil: no theft detection (#154 step 3)
	noticer    Noticer              // nil: no owner's notice is published (#154 step 3)
	nmu        sync.Mutex           // guards notices
	notices    map[string]time.Time // notice key -> when it was published
	persons    personHooks          // the console's path (person.go); unset: none issued
}

// SetAnchors sets where anchors come from. nil (the default) takes the
// owner's preferred_username, which is what the directory gives every
// identity known before the import: names and presence tokens are the same.
func (s *Service) SetAnchors(a Anchors) {
	s.cmu.Lock()
	defer s.cmu.Unlock()
	s.anchors = a
}

func (s *Service) anchorHook() Anchors {
	s.cmu.RLock()
	defer s.cmu.RUnlock()
	return s.anchors
}

// SetConsumers sets the consumer management. nil (the default) makes none and
// is for unit tests of this package only: the service's wiring (app.RunCallout)
// always sets it, since a credential without consumer management is not
// reconciled or braked.
func (s *Service) SetConsumers(c Consumers) {
	s.cmu.Lock()
	defer s.cmu.Unlock()
	s.consumers = c
}

func (s *Service) consumerHook() Consumers {
	s.cmu.RLock()
	defer s.cmu.RUnlock()
	return s.consumers
}

// New returns a service. issuer is the account's signing key pair (its
// public key is the auth_callout issuer in the server's configuration); xkey
// may be nil only when the server sends requests unencrypted, which the
// caller allows explicitly; brake may be nil.
func New(cfg Config, issuer, xkey nkeys.KeyPair, v Verifier, ch Channels, brake *Brake, log *slog.Logger) (*Service, error) {
	if err := cfg.check(); err != nil {
		return nil, err
	}
	if pub, err := issuer.PublicKey(); err != nil || !nkeys.IsValidPublicAccountKey(pub) {
		return nil, errors.New("callout: the issuer must be an account key pair")
	}
	if xkey != nil {
		if pub, err := xkey.PublicKey(); err != nil || !nkeys.IsValidPublicCurveKey(pub) {
			return nil, errors.New("callout: the xkey must be a curve key pair")
		}
	}
	if v == nil || ch == nil || log == nil {
		return nil, errors.New("callout: verifier, channels and logger must be set")
	}
	return &Service{cfg: cfg, issuer: issuer, xkey: xkey, verifier: v, channels: ch, brake: brake,
		log: log, now: time.Now, pending: make(chan struct{}, MaxPending)}, nil
}

// Subject is where the NATS server sends auth callout requests.
const Subject = "$SYS.REQ.USER.AUTH"

// Serve subscribes on nc (a connection as one of the server's auth_users)
// until ctx ends. Requests are answered concurrently, at most MaxPending at
// a time; one that finds no free slot within its timeout is dropped and the
// server times it out.
func (s *Service) Serve(ctx context.Context, nc *nats.Conn) error {
	sub, err := nc.QueueSubscribe(Subject, "authd", func(m *nats.Msg) {
		go func() {
			select {
			case s.pending <- struct{}{}:
			case <-time.After(s.cfg.Timeout):
				s.log.Warn("auth callout", "event", "dropped", "reason", "too many requests at once")
				return
			case <-ctx.Done():
				return
			}
			defer func() { <-s.pending }()
			s.handle(ctx, m)
		}()
	})
	if err != nil {
		return err
	}
	<-ctx.Done()
	return sub.Drain()
}

func (s *Service) handle(ctx context.Context, m *nats.Msg) {
	now := s.now()
	data, serverXKey, err := s.open(m)
	if err != nil {
		s.log.Warn("auth callout", "event", "unanswered", "reason", err.Error())
		return // without a readable request there is nobody to answer
	}
	req, err := s.request(data, now)
	if err != nil {
		s.log.Warn("auth callout", "event", "unanswered", "reason", err.Error())
		return
	}
	who := []any{"server_id", req.Server.ID, "client_host", req.ClientInformation.Host, "user_nkey", req.UserNkey}

	ctx, cancel := context.WithDeadline(ctx, deadline(now, s.cfg.Timeout, req.Expires))
	defer cancel()
	userJWT, more, err := s.authorize(ctx, req, now)
	who = append(who, more...)

	resp := jwt.NewAuthorizationResponseClaims(req.UserNkey)
	resp.Audience = req.Server.ID
	resp.IssuedAt = now.Unix()
	if err != nil {
		resp.Error = err.Error()
		s.log.Info("auth callout", append([]any{"event", "refused"}, append(who, "reason", err.Error())...)...)
	} else {
		resp.Jwt = userJWT
		s.log.Info("auth callout", append([]any{"event", "issued"}, who...)...)
	}
	out, err := resp.Encode(s.issuer)
	if err != nil {
		s.log.Error("auth callout", "event", "unanswered", "reason", "cannot sign the response: "+err.Error())
		return
	}
	if serverXKey != "" {
		sealed, err := s.xkey.Seal([]byte(out), serverXKey)
		if err != nil {
			s.log.Error("auth callout", "event", "unanswered", "reason", "cannot encrypt the response: "+err.Error())
			return
		}
		_ = m.Respond(sealed)
		return
	}
	_ = m.Respond([]byte(out))
}

// deadline is the end of one request's work: the timeout, but never past the
// server's own request expiry, less the time to send the answer.
func deadline(now time.Time, timeout time.Duration, expires int64) time.Time {
	d := now.Add(timeout)
	if exp := time.Unix(expires, 0).Add(-answerMargin); exp.Before(d) {
		d = exp
	}
	return d
}

// open decrypts a request the server encrypted to this service's xkey. With
// an xkey configured, a plain request is refused: it would mean the server is
// configured otherwise than this service.
func (s *Service) open(m *nats.Msg) ([]byte, string, error) {
	serverXKey := m.Header.Get("Nats-Server-Xkey")
	switch {
	case s.xkey == nil && serverXKey == "":
		return m.Data, "", nil
	case s.xkey == nil:
		return nil, "", errors.New("encrypted request, but no xkey configured")
	case serverXKey == "":
		return nil, "", errors.New("plain request, but an xkey is configured")
	}
	data, err := s.xkey.Open(m.Data, serverXKey)
	return data, serverXKey, err
}

// request decodes a server's auth request and checks that a server signed it
// for itself, for the auth callout, and that it has not expired.
func (s *Service) request(data []byte, now time.Time) (*jwt.AuthorizationRequestClaims, error) {
	req, err := jwt.DecodeAuthorizationRequestClaims(string(data))
	switch {
	case err != nil:
		return nil, fmt.Errorf("request not signed: %w", err)
	case !nkeys.IsValidPublicServerKey(req.Issuer) || req.Issuer != req.Server.ID:
		return nil, errors.New("request not issued by the server it names")
	case req.Audience != requestAudience:
		return nil, errors.New("request for another audience")
	case req.Expires == 0 || now.Unix() >= req.Expires:
		return nil, errors.New("request expired or without expiry")
	}
	return req, nil
}

// authorize returns the user JWT for req, and log attributes without secrets.
func (s *Service) authorize(ctx context.Context, req *jwt.AuthorizationRequestClaims, now time.Time) (string, []any, error) {
	opts := req.ConnectOptions
	if raw, ok := s.personToken(opts.Token); ok {
		return s.authorizePerson(ctx, req, raw, now)
	}
	var hello Hello
	if err := json.Unmarshal([]byte(opts.Token), &hello); err != nil || hello.Token == "" {
		return "", nil, ErrRequest
	}
	host, err := hostKey(opts.Nkey, opts.SignedNonce, req.ClientInformation.Nonce)
	if err != nil {
		return "", nil, err
	}
	who := []any{"host_nkey", host}
	if err := s.brake.Check(""); err != nil {
		return "", who, err
	}
	id, err := s.verifier.Verify(ctx, hello.Token)
	if err != nil {
		return "", who, err
	}
	// The brake holds names (the owner's username), before anything is written.
	if err := s.brake.Check(id.Owner); err != nil {
		return "", append(who, "owner", id.Owner, "sub", id.Subject), err
	}
	anchor := id.Owner
	ah := s.anchorHook()
	if ah != nil {
		if anchor, err = ah.Anchor(ctx, id.Subject, id.Owner); err != nil {
			return "", append(who, "owner", id.Owner, "sub", id.Subject),
				fmt.Errorf("%w: anchor: %v", token.ErrDirectory, err)
		}
	}
	agent := s.cfg.NamePrefix + name(anchor, host)
	who = append(who, "owner", id.Owner, "sub", id.Subject, "jti", id.ID, "agent", agent)
	if anchor != id.Owner {
		who = append(who, "anchor", anchor)
	}
	// The theft check needs the anchor (the notice's subject) and the agent
	// (its name): a copied session is refused, a copied host key reported, both
	// noticed for the owner (#154 step 3).
	w := s.watchTheft(host, id.SID, req.ClientInformation.Host)
	s.publishNotices(ctx, w, anchor, agent, host, id.SID, now)
	if w.session == theft.Copied {
		return "", append(who, "session", id.SID), ErrCopiedUse
	}
	// Only a credential that is issued writes the chat account's record
	// (RT-6.42): a refused one leaves the directory as it is.
	if ai, ok := ah.(AccountIndex); ok && id.ChatOpenID {
		if err := ai.Account(ctx, id.Subject, id.Owner); err != nil {
			return "", append(who, "owner", id.Owner, "sub", id.Subject),
				fmt.Errorf("%w: account index: %v", token.ErrDirectory, err)
		}
	}
	channels, err := s.grant(ctx, id.Owner, id.Subject, hello)
	if err != nil {
		return "", who, err
	}
	who = append(who, "channels", channels)

	uc := jwt.NewUserClaims(req.UserNkey)
	uc.Name = agent
	uc.Audience = s.cfg.Account
	uc.IssuedAt = now.Unix()
	uc.Expires = now.Add(Lifetime).Unix()
	uc.Permissions = s.cfg.Subjects.permissions(agent, anchor, hello.Home, channels)
	uc.Limits.Subs = maxSubs
	uc.Limits.Payload = maxPayload
	uc.AllowedConnectionTypes.Add(jwt.ConnectionTypeStandard)
	uc.Tags.Add("owner:"+id.Owner, "host-nkey:"+host)
	if id.Approver {
		uc.Tags.Add("approver")
	}
	vr := jwt.CreateValidationResults()
	uc.Validate(vr)
	if errs := vr.Errors(); len(errs) > 0 {
		return "", who, fmt.Errorf("user JWT invalid: %w", errs[0])
	}
	userJWT, err := uc.Encode(s.issuer)
	if err != nil {
		return "", who, err
	}
	if c := s.consumerHook(); c != nil {
		if err := c.Reconcile(ctx, agent, id.Owner, anchor, channels, hello.Since); err != nil {
			return "", who, fmt.Errorf("consumers could not be made: %w", err)
		}
	}
	if at, tr := s.attestHooks(); at != nil {
		att, err := s.attest(ctx, at, tr, agent, directory.Trusted{Anchor: anchor, Name: id.Owner},
			hello.Trusted, hello.TrustedCut, uc.IssuedAt, uc.Expires)
		if err != nil {
			return "", who, fmt.Errorf("attestation: %w", err)
		}
		// The trust index an operator's credential reads (RT-6.43 step 3).
		if ix := s.trustIndexHook(); ix != nil {
			if err := s.indexTrust(ctx, ix, att, channels); err != nil {
				return "", who, fmt.Errorf("%w: trust index: %v", token.ErrDirectory, err)
			}
		}
		// The record of the chat account behind the login (RT-6.42 step 3).
		guard, _ := s.anchorHook().(TokenGuard)
		skipped, err := s.accountRecord(ctx, at, guard, id.Subject, id.ChatID, id.Owner, id.Approver, anchor, id.IssuedAt, uc.IssuedAt, uc.Expires)
		if err != nil {
			return "", who, fmt.Errorf("account record: %w", err)
		}
		if skipped != "" && id.ChatID != "" {
			s.log.Info("auth callout", "event", "account record kept", "sub", id.Subject, "reason", skipped)
		}
	}
	return userJWT, who, nil
}

// grant keeps of the asked-for channels those the human is a member of, and
// the lobby when asked for, by the flag or by its name. A refused wish is
// logged with its reason and the others are granted; a lookup that fails
// refuses the whole request, since a partial credential would look whole.
func (s *Service) grant(ctx context.Context, owner, sub string, h Hello) ([]string, error) {
	refuse := func(ch, why string) {
		s.log.Info("auth callout", "event", "channel refused", "owner", owner,
			"channel", fmt.Sprintf("%.64q", ch), "reason", why)
	}
	lobby := h.Lobby && s.cfg.Lobby != ""
	guests := h.Guests
	if len(guests) > MaxGuests {
		for _, ch := range guests[MaxGuests:] {
			refuse(ch, "more than 16 guest channels")
		}
		guests = guests[:MaxGuests]
	}
	var ask []string
	seen := map[string]bool{}
	for _, ch := range append([]string{h.Home}, guests...) {
		switch {
		case ch == "" || seen[ch]:
			continue
		case s.cfg.Lobby != "" && ch == s.cfg.Lobby:
			lobby = true
			continue
		}
		seen[ch] = true
		if err := checkToken(ch); err != nil {
			refuse(ch, err.Error())
			continue
		}
		ask = append(ask, ch)
	}
	var granted []string
	if len(ask) > 0 {
		var member map[string]bool
		var err error
		if ic, ok := s.channels.(IdentityChannels); ok {
			member, err = ic.MembershipsFor(ctx, owner, sub, ask)
		} else {
			member, err = s.channels.Memberships(ctx, owner, ask)
		}
		if err != nil {
			return nil, fmt.Errorf("%w: membership: %v", token.ErrDirectory, err)
		}
		for _, ch := range ask {
			if member[ch] {
				granted = append(granted, ch)
			} else {
				refuse(ch, "not a member")
			}
		}
	}
	if lobby {
		granted = append(granted, s.cfg.Lobby)
	}
	return granted, nil
}

// hostKey checks that the host holds the user nkey it names: its signature
// over the server's nonce. The server sends a nonce when it has an nkey user
// configured (the service's own).
func hostKey(pub, sig, nonce string) (string, error) {
	if !nkeys.IsValidPublicUserKey(pub) || sig == "" || nonce == "" {
		return "", ErrNoNkey
	}
	kp, err := nkeys.FromPublicKey(pub)
	if err != nil {
		return "", ErrNoNkey
	}
	raw, err := base64.RawURLEncoding.DecodeString(sig)
	if err != nil {
		if raw, err = base64.StdEncoding.DecodeString(sig); err != nil {
			return "", ErrNoNkey
		}
	}
	if kp.Verify([]byte(nonce), raw) != nil {
		return "", ErrNoNkey
	}
	return pub, nil
}

var lowerBase32 = base32.NewEncoding("abcdefghijklmnopqrstuvwxyz234567").WithPadding(base32.NoPadding)

// name is the agent's name: 128 bits of SHA-256 over owner and host nkey,
// 26 characters of base32 in lower case. Stable for one host process (its
// nkey) of one owner. 128 bits make finding a name for a given owner and key
// (a preimage) take 2^128 tries, but two names of an attacker's own choosing
// can collide in about 2^64, so the name never proves an owner: the owner is
// pinned in the presence subject (perms.go).
func name(owner, hostNkey string) string {
	h := sha256.Sum256([]byte(owner + "\x00" + hostNkey))
	return lowerBase32.EncodeToString(h[:16])
}
