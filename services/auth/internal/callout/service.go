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
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/nats-io/jwt/v2"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nkeys"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

// Lifetime of an issued user JWT, counted from the check (ADR-014, point 7):
// every issue checks the Keycloak token again, and without the host's nkey
// seed nothing issued can be used by anyone else. A Keycloak session that
// ends therefore takes effect within this time.
const Lifetime = 15 * time.Minute

// Verifier is the token check (token.Verifier).
type Verifier interface {
	Verify(ctx context.Context, raw string) (token.Identity, error)
}

// Channels says whether a human is a member of a channel
// (mattermost.Client).
type Channels interface {
	Member(ctx context.Context, channel, username string) (bool, error)
}

// Hello is what a host sends as `auth_token` in its CONNECT, as JSON: the
// Keycloak access token and the channels it asks for. The channels are a
// wish; only those the human is a member of are granted.
type Hello struct {
	Token  string   `json:"token"`
	Home   string   `json:"home,omitempty"`
	Guests []string `json:"guests,omitempty"`
	Lobby  bool     `json:"lobby,omitempty"`
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
}

// New returns a service. issuer is the account's signing key pair (its
// public key is the auth_callout issuer in the server's configuration); xkey
// may be nil when the server sends requests unencrypted; brake may be nil.
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
	return &Service{cfg: cfg, issuer: issuer, xkey: xkey, verifier: v, channels: ch, brake: brake, log: log, now: time.Now}, nil
}

// Subject is where the NATS server sends auth callout requests.
const Subject = "$SYS.REQ.USER.AUTH"

// Serve subscribes on nc (a connection as one of the server's auth_users)
// until ctx ends.
func (s *Service) Serve(ctx context.Context, nc *nats.Conn) error {
	sub, err := nc.QueueSubscribe(Subject, "authd", func(m *nats.Msg) { s.handle(ctx, m) })
	if err != nil {
		return err
	}
	<-ctx.Done()
	return sub.Drain()
}

func (s *Service) handle(ctx context.Context, m *nats.Msg) {
	data, serverXKey, err := s.open(m)
	if err != nil {
		s.log.Warn("auth callout: unreadable request", "reason", err.Error())
		return // without a readable request there is nobody to answer
	}
	req, err := jwt.DecodeAuthorizationRequestClaims(string(data))
	if err != nil {
		s.log.Warn("auth callout: request not signed by a server", "reason", err.Error())
		return
	}
	ctx, cancel := context.WithTimeout(ctx, s.cfg.Timeout)
	defer cancel()
	userJWT, who, err := s.authorize(ctx, req)
	resp := jwt.NewAuthorizationResponseClaims(req.UserNkey)
	resp.Audience = req.Server.ID
	if err != nil {
		resp.Error = err.Error()
		s.log.Info("auth callout", append([]any{"event", "refused"}, append(who, "reason", err.Error())...)...)
	} else {
		resp.Jwt = userJWT
		s.log.Info("auth callout", append([]any{"event", "issued"}, who...)...)
	}
	out, err := resp.Encode(s.issuer)
	if err != nil {
		s.log.Error("auth callout: cannot sign the response", "reason", err.Error())
		return
	}
	if serverXKey != "" {
		sealed, err := s.xkey.Seal([]byte(out), serverXKey)
		if err != nil {
			s.log.Error("auth callout: cannot encrypt the response", "reason", err.Error())
			return
		}
		_ = m.Respond(sealed)
		return
	}
	_ = m.Respond([]byte(out))
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

// authorize returns the user JWT for req, and log attributes without secrets.
func (s *Service) authorize(ctx context.Context, req *jwt.AuthorizationRequestClaims) (string, []any, error) {
	opts := req.ConnectOptions
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
	agent := s.cfg.NamePrefix + name(id.Owner, host)
	who = append(who, "owner", id.Owner, "agent", agent)
	if err := s.brake.Check(id.Owner); err != nil {
		return "", who, err
	}
	channels := s.grant(ctx, id.Owner, hello)
	who = append(who, "channels", channels)

	uc := jwt.NewUserClaims(req.UserNkey)
	uc.Name = agent
	uc.Audience = s.cfg.Account
	uc.IssuedAt = s.now().Unix()
	uc.Expires = s.now().Add(Lifetime).Unix()
	uc.Permissions = s.cfg.Subjects.permissions(agent, host, channels)
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
	return userJWT, who, err
}

// grant keeps of the asked-for channels those the human is a member of, and
// the lobby; every refused wish is logged with its reason.
func (s *Service) grant(ctx context.Context, owner string, h Hello) []string {
	var granted []string
	seen := map[string]bool{}
	ask := append([]string{h.Home}, h.Guests...)
	for _, ch := range ask {
		if ch == "" || seen[ch] {
			continue
		}
		seen[ch] = true
		if err := checkToken(ch); err != nil {
			s.log.Info("auth callout: channel refused", "owner", owner, "channel", fmt.Sprintf("%.64q", ch), "reason", err.Error())
			continue
		}
		ok, err := s.channels.Member(ctx, ch, owner)
		switch {
		case err != nil:
			s.log.Warn("auth callout: channel refused", "owner", owner, "channel", ch, "reason", "membership lookup failed")
		case !ok:
			s.log.Info("auth callout: channel refused", "owner", owner, "channel", ch, "reason", "not a member")
		default:
			granted = append(granted, ch)
		}
	}
	if h.Lobby && s.cfg.Lobby != "" && !seen[s.cfg.Lobby] {
		granted = append(granted, s.cfg.Lobby)
	}
	return granted
}

// hostKey checks that the host holds the user nkey it names: its signature
// over the server's nonce. The server sends the nonce only to a client that
// signed it (always_enable_nonce in the server's configuration).
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

// name is the agent's name: stable for one host process (its nkey) of one
// owner, and practically never the name of another owner's agent (48 bits).
func name(owner, hostNkey string) string {
	h := sha256.Sum256([]byte(owner + "\x00" + hostNkey))
	return hex.EncodeToString(h[:6])
}
