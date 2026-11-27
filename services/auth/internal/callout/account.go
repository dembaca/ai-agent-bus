package callout

// The account record (RT-6.42 step 3, ADR-016 step 2 points 7 and 8): for each
// Mattermost account the service has proven to be a Keycloak identity's, the
// anchor behind it and whether the token placed the identity in the approvers'
// group, on a subject only this service may publish on. The central bridge
// maps a Mattermost account to the anchor it publishes a person's post under
// from it; the hosts take "approver" from it. Both decode it strictly; an
// agent has no right to write it (perms.go gives an agent no five-token
// presence subject).

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"sync"
	"time"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/directory"
)

// chatIDRE is a Mattermost user id: 26 characters, no dot or wildcard, so it
// is one safe subject token.
var chatIDRE = regexp.MustCompile(`^[a-z0-9]{26}$`)

// TokenGuard keeps, per identity, the issue time of the newest token whose
// record was written, and whether it had the approvers' group (directory.
// Anchors has it); at an equal issue time the token without the group wins.
// The service checks for it by type assertion on what SetAnchors is given;
// without it no account record is written, since a record could then be
// replaced by one from an older token.
type TokenGuard interface {
	NewestToken(ctx context.Context, sub string, iat int64, approver bool) (bool, error)
}

// RecordPublisher is an Attester (NATSAttester) that takes a stored message
// other than its own bytes as proof of the write, for a subject several
// logins write.
type RecordPublisher interface {
	PublishRecord(ctx context.Context, subject string, body []byte, accept func(stored []byte) bool) error
}

// AccountRecord is the body published for a Mattermost account (format v1).
type AccountRecord struct {
	V        int    `json:"v"`
	Account  string `json:"account"`  // Mattermost's user id
	Anchor   string `json:"anchor"`   // the identity's anchor
	Name     string `json:"name"`     // the Mattermost username, to show
	Approver bool   `json:"approver"` // the token lists the approvers' group
	TokenIat int64  `json:"token_iat"`
	Iat      int64  `json:"iat"`
	Exp      int64  `json:"exp"` // the expiry of the NATS credential issued from the token
}

// accountSubject is where the record of a Mattermost account is published.
func (s *Service) accountSubject(account string) string {
	return s.cfg.Subjects.Presence + ".account.mattermost." + account + ".v1"
}

// accountRecord publishes the record of the chat account behind a verified
// login, unless the token is older than the newest the identity logged in
// with: then the stored record stays. Only an account proven to be the
// token's (ChatID set, with the proof authService and authData) gets one;
// an error refuses the login, as the attestation's does. skipped says why no
// record was written, "" when it was. With the record go the proof for the
// bridge (ProofRecord) and the binding, kept durably in the directory
// (BindingStore), so the binding is proven once and not at every post (the
// owner's decision of 2026-11-27, issue 266). The binding is written first,
// then the proof, then the record: a write that fails later leaves no record
// on NATS that the binding's hourly check (SweepBindings) does not know of.
// approver nil keeps the flag of the account's stored record (a token that
// says nothing about the approvers' group must not lower it; none stored:
// false).
//
// The guard and the write are one step per identity (a lock per `sub`): else
// an older token could pass the guard, be overtaken by a newer one and write
// after it, leaving the older approver flag. The lock lives in this process:
// the login service runs as exactly one instance (the deployment view and the
// README, as the consumers' own per-agent lock needs); a second instance would
// need a compare-and-set spanning instances, on the guard's key-value revision
// or JetStream's expected last sequence. Not built. A stored record that is a newer
// one for the same account counts as written, never as a refusal.
func (s *Service) accountRecord(ctx context.Context, a Attester, guard TokenGuard, sub, chatID, name string, approverOf *bool, anchor string, tokenIat time.Time, iat, exp int64, authService, authData string) (skipped string, err error) {
	switch {
	case chatID == "" || authService == "" || authData == "":
		return "no proven chat account", nil
	case guard == nil:
		return "no directory, so no guard", nil
	case !chatIDRE.MatchString(chatID):
		return "the chat's user id is not a 26 character id", nil
	case tokenIat.IsZero():
		return "the token has no iat", nil
	}
	defer s.lockSub(sub)()
	bs, _ := guard.(BindingStore)
	approver := false
	switch {
	case approverOf != nil:
		approver = *approverOf
	case bs != nil:
		approver = storedApprover(ctx, bs, chatID, sub, anchor)
	}
	fresh, err := guard.NewestToken(ctx, sub, tokenIat.Unix(), approver)
	if err != nil {
		return "", err
	}
	if !fresh {
		return "a newer token wrote it", nil
	}
	rec := AccountRecord{V: 1, Account: chatID, Anchor: anchor, Name: name, Approver: approver,
		TokenIat: tokenIat.Unix(), Iat: iat, Exp: exp}
	body, err := json.Marshal(rec)
	if err != nil {
		return "", err
	}
	if bs != nil {
		if err := bs.PutBinding(ctx, directory.Binding{Account: chatID, Sub: sub, Anchor: anchor,
			AuthService: authService, AuthData: authData, Record: body, At: iat}); err != nil {
			return "", fmt.Errorf("binding: %w", err)
		}
	}
	if err := s.publishProof(ctx, a, ProofRecord{V: 1, Account: chatID, AuthService: authService, AuthData: authData, Iat: iat}); err != nil {
		return "", fmt.Errorf("proof: %w", err)
	}
	return "", s.publishAccount(ctx, a, chatID, body, rec.TokenIat)
}

// storedApprover is the approver flag of the account's stored record, kept
// only for the same person: the binding is the identity's (`sub`) and its
// record names the same anchor. False without one, for one that does not
// decode, and for a binding of another identity (an account linked anew,
// before the hourly check unbinds it), so a login never takes over another
// person's flag (fail closed).
func storedApprover(ctx context.Context, bs BindingStore, chatID, sub, anchor string) bool {
	b, _, ok, err := bs.Binding(ctx, chatID)
	if err != nil || !ok || b.Sub != sub || b.Anchor != anchor {
		return false
	}
	var rec AccountRecord
	return json.Unmarshal(b.Record, &rec) == nil && rec.Account == chatID && rec.Anchor == anchor && rec.Approver
}

// publishAccount publishes an account record; a stored record of the same
// account from a newer token counts as written.
func (s *Service) publishAccount(ctx context.Context, a Attester, chatID string, body []byte, tokenIat int64) error {
	subject := s.accountSubject(chatID)
	if rp, ok := a.(RecordPublisher); ok {
		return rp.PublishRecord(ctx, subject, body, func(stored []byte) bool {
			var got AccountRecord
			return json.Unmarshal(stored, &got) == nil && got.V == 1 && got.Account == chatID && got.TokenIat > tokenIat
		})
	}
	return a.Publish(ctx, subject, body)
}

// ProofRecord is the proof of a binding (format v1), published beside the
// account record on <presence>.account.mattermost.<user id>.auth.v1 for the
// bridge: how the account logged in when it was proven and the value its
// auth_data equalled (RT-6.42 step 3). The bridge carries a person's post
// under the record's anchor only while the account still logs in so. No
// agent's consumer delivers it (their filter names the five-token record
// only), so no agent reads a person's `sub` or LDAP id from it, and no agent
// may publish there (perms.go).
type ProofRecord struct {
	V           int    `json:"v"`
	Account     string `json:"account"`      // Mattermost's user id
	AuthService string `json:"auth_service"` // "openid" or "ldap"
	AuthData    string `json:"auth_data"`    // the value the account's auth_data equals
	Iat         int64  `json:"iat"`
}

// proofSubject is where the proof of a Mattermost account's binding is
// published.
func (s *Service) proofSubject(account string) string {
	return s.cfg.Subjects.Presence + ".account.mattermost." + account + ".auth.v1"
}

// publishProof publishes a proof; a stored proof of the same account that is
// newer counts as written.
func (s *Service) publishProof(ctx context.Context, a Attester, p ProofRecord) error {
	body, err := json.Marshal(p)
	if err != nil {
		return err
	}
	subject := s.proofSubject(p.Account)
	if rp, ok := a.(RecordPublisher); ok {
		return rp.PublishRecord(ctx, subject, body, func(stored []byte) bool {
			var got ProofRecord
			return json.Unmarshal(stored, &got) == nil && got.V == 1 && got.Account == p.Account && got.Iat > p.Iat
		})
	}
	return a.Publish(ctx, subject, body)
}

// lockSub takes the lock of one identity and returns its release. The map
// forgets an identity nobody holds or waits for.
func (s *Service) lockSub(sub string) func() {
	s.subMu.Lock()
	l := s.subLocks[sub]
	if l == nil {
		if s.subLocks == nil {
			s.subLocks = map[string]*subLock{}
		}
		l = &subLock{}
		s.subLocks[sub] = l
	}
	l.users++
	s.subMu.Unlock()
	l.mu.Lock()
	return func() {
		l.mu.Unlock()
		s.subMu.Lock()
		if l.users--; l.users == 0 {
			delete(s.subLocks, sub)
		}
		s.subMu.Unlock()
	}
}

type subLock struct {
	mu    sync.Mutex
	users int
}
