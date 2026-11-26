package callout

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"time"

	"github.com/nats-io/jwt/v2"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/consumers"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

// The operator console's path (RT-6.43, the addition of 2026-11-25 to
// ADR-015, points 1 to 6): the console's server connects to NATS with its own
// key pair for one operator's login and the operator's Keycloak access token,
// bare, as the connect token (no JSON hello). A token whose `azp` is the
// console's client takes this path; every other connect token the agents'
// path, which refuses a console token, as this one refuses the bus's.
//
// The credential: named <AUTH_PERSON_PREFIX><anchor> (the anchor the
// directory holds for the token's `sub`, "." written as "~"), 15 minutes,
// no publish at all, subscribe only on deliver.person.<login>.>, where
// <login> is the login's whole public key, lower-cased. No JetStream API, no
// inbox: what the person reads comes from two push consumers the login
// service makes for that login (consumers.EnsurePerson), so whoever takes
// over the console's server can read what the person reads and do nothing
// else (RISK-12). The console never derives the person's name: at each issue
// the service writes the login's own record (PersonRecord) on
// consumers.PersonRecordSubject, which only that login's consumer delivers.

// DefaultPersonPrefix starts a person's NATS name.
const DefaultPersonPrefix = "person-"

// DefaultMaxLogins: logins one person may hold at once (browsers, tabs of
// separate sessions); each holds two consumers until 15 minutes after it ends.
const DefaultMaxLogins = 8

// maxPersonSubs: the two deliver subjects, with room for a reconnect.
const maxPersonSubs = 8

// Console is the console's path in the configuration.
type Console struct {
	// ClientID is the console's Keycloak client (AUTH_CONSOLE_CLIENT_ID): a
	// token's `azp` must be it and its `aud` must name it. Empty: the path
	// is off and no person's credential is issued.
	ClientID string
	// OperatorGroup must be in the token's group claim at every issue
	// (AUTH_OPERATOR_GROUP), compared exactly as the group mapper writes it.
	OperatorGroup string
	// PersonPrefix starts a person's name (AUTH_PERSON_PREFIX).
	PersonPrefix string
	// MaxLogins bounds the logins one person holds consumers for at once
	// (AUTH_PERSON_MAX_LOGINS); a new login above it is refused.
	MaxLogins int
	// Metrics turns the console's unattended metrics page on
	// (AUTH_CONSOLE_METRICS, default off): the service's two consumers are
	// made at the start and kept (consumers.EnsureMetrics, #158). Nothing
	// else: the account that reads them is a static nkey of the NATS
	// configuration, subscribe-only.
	Metrics bool
}

func (c Console) withDefaults() Console {
	if c.PersonPrefix == "" {
		c.PersonPrefix = DefaultPersonPrefix
	}
	if c.MaxLogins == 0 {
		c.MaxLogins = DefaultMaxLogins
	}
	return c
}

// checkConsole: with the path on, an operators' group, and a person prefix
// that no agent's name can share: it differs from the name prefix, neither
// starts the other, and it ends in "-", so no name it makes is another
// reader's deliver token (the bridge's "bridge").
func (c Config) checkConsole() error {
	con := c.Console
	if con.ClientID == "" {
		return nil
	}
	switch {
	case !strings.HasPrefix(con.OperatorGroup, "/") || len(strings.TrimSpace(con.OperatorGroup)) < 2:
		return errors.New("callout: the operators' group must be set with the console's client, as a full path (\"/group\", the group mapper's full path on)")
	case con.MaxLogins < 1 || con.MaxLogins > 64:
		return fmt.Errorf("callout: at most %d logins per person: from 1 to 64", con.MaxLogins)
	case !plain(con.PersonPrefix) || !strings.HasSuffix(con.PersonPrefix, "-"):
		return fmt.Errorf("callout: person prefix %q: letters, digits, - and _, ending in -", con.PersonPrefix)
	case strings.HasPrefix(con.PersonPrefix, c.NamePrefix) || strings.HasPrefix(c.NamePrefix, con.PersonPrefix):
		return fmt.Errorf("callout: person prefix %q and name prefix %q: neither may start the other", con.PersonPrefix, c.NamePrefix)
	}
	return nil
}

// PersonVerifier checks a console token against the realm
// (token.Verifier.VerifyRealm, configured for the console's client).
type PersonVerifier interface {
	VerifyRealm(ctx context.Context, raw string) (token.Identity, error)
}

// Persons is the directory's part of the path (directory.Persons).
type Persons interface {
	// Anchor is the anchor held for `sub`; false: none, no credential.
	Anchor(ctx context.Context, sub string) (string, bool, error)
	// Visible are the channels the person may see.
	Visible(ctx context.Context, sub string) ([]string, error)
}

// PersonConsumers makes a login's consumers (consumers.Manager). The
// service's Consumers must offer it for the path to issue anything.
type PersonConsumers interface {
	EnsurePerson(ctx context.Context, person, login string, channels []string, maxLogins int) error
}

type personHooks struct {
	verifier PersonVerifier
	dir      Persons
}

// Reasons a person's connection is refused.
var (
	ErrPersonPath  = errors.New("the console's path is not set up")
	ErrNotOperator = errors.New("not in the operators' group")
	ErrUnbound     = errors.New("no anchor in the directory for this identity")
)

// SetPersons wires the console's path: the console's token check and the
// directory. Without it a console token is refused.
func (s *Service) SetPersons(v PersonVerifier, d Persons) {
	s.cmu.Lock()
	defer s.cmu.Unlock()
	s.persons = personHooks{verifier: v, dir: d}
}

func (s *Service) personHooks() personHooks {
	s.cmu.RLock()
	defer s.cmu.RUnlock()
	return s.persons
}

// personToken says whether the connect token takes the console's path: the
// path is on, the token is no JSON hello, and its unchecked `azp` is the
// console's client. Everything is checked after that.
func (s *Service) personToken(connect string) (string, bool) {
	if s.cfg.Console.ClientID == "" || strings.HasPrefix(strings.TrimSpace(connect), "{") {
		return "", false
	}
	return connect, token.Client(connect) == s.cfg.Console.ClientID
}

// loginOf is a login's token in names and subjects: its whole public key,
// lower-cased.
func loginOf(key string) string { return strings.ToLower(key) }

// personPermissions: no publish, subscribe only on the login's deliver
// subjects.
func personPermissions(login string) jwt.Permissions {
	var p jwt.Permissions
	p.Pub.Deny.Add(">")
	p.Sub.Allow.Add(DeliverPrefix + "." + consumers.PersonDeliverToken + "." + login + ".>")
	return p
}

// PersonRecord is the login's own record: who the credential is for, as the
// console shows it and compares an ask's `human` with.
type PersonRecord struct {
	V int `json:"v"`
	// Name is the person's NATS name, <AUTH_PERSON_PREFIX><anchor>.
	Name string `json:"name"`
	// Anchor is the anchor in its subject form ("." as "~").
	Anchor string `json:"anchor"`
	IAT    int64  `json:"iat"`
	Exp    int64  `json:"exp"`
}

// authorizePerson issues an operator's credential.
func (s *Service) authorizePerson(ctx context.Context, req *jwt.AuthorizationRequestClaims, raw string, now time.Time) (string, []any, error) {
	opts := req.ConnectOptions
	key, err := hostKey(opts.Nkey, opts.SignedNonce, req.ClientInformation.Nonce)
	if err != nil {
		return "", nil, err
	}
	who := []any{"path", "person", "login_nkey", key}
	if err := s.brake.Check(""); err != nil {
		return "", who, err
	}
	h := s.personHooks()
	if h.verifier == nil || h.dir == nil {
		return "", who, ErrPersonPath
	}
	id, err := h.verifier.VerifyRealm(ctx, raw)
	if err != nil {
		return "", who, err
	}
	who = append(who, "sub", id.Subject, "jti", id.ID)
	if !slices.Contains(id.Groups, s.cfg.Console.OperatorGroup) {
		return "", who, ErrNotOperator
	}
	anchor, ok, err := h.dir.Anchor(ctx, id.Subject)
	if err != nil {
		return "", who, fmt.Errorf("%w: anchor: %v", token.ErrDirectory, err)
	}
	tok := ownerToken(anchor)
	if !ok || tok == "-" {
		return "", who, ErrUnbound
	}
	person, login := s.cfg.Console.PersonPrefix+tok, loginOf(key)
	who = append(who, "person", person, "login", login)
	channels, err := h.dir.Visible(ctx, id.Subject)
	if err != nil {
		return "", who, fmt.Errorf("%w: channels: %v", token.ErrDirectory, err)
	}
	who = append(who, "channels", channels)

	uc := jwt.NewUserClaims(req.UserNkey)
	uc.Name = person
	uc.Audience = s.cfg.Account
	uc.IssuedAt = now.Unix()
	uc.Expires = now.Add(Lifetime).Unix()
	uc.Permissions = personPermissions(login)
	uc.Limits.Subs = maxPersonSubs
	uc.Limits.Payload = maxPayload
	uc.AllowedConnectionTypes.Add(jwt.ConnectionTypeStandard)
	uc.Tags.Add("person")
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
		pc, ok := c.(PersonConsumers)
		if !ok {
			return "", who, ErrPersonPath
		}
		if err := pc.EnsurePerson(ctx, person, login, channels, s.cfg.Console.MaxLogins); err != nil {
			return "", who, fmt.Errorf("consumers could not be made: %w", err)
		}
	}
	if at, _ := s.attestHooks(); at != nil {
		body, _ := json.Marshal(PersonRecord{V: 1, Name: person, Anchor: tok, IAT: uc.IssuedAt, Exp: uc.Expires})
		if err := at.Publish(ctx, consumers.PersonRecordSubject(s.cfg.Subjects.Presence, login), body); err != nil {
			return "", who, fmt.Errorf("the login's record: %w", err)
		}
	}
	return userJWT, who, nil
}
