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
	"regexp"
	"sync"
	"time"
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
// token's (ChatID set) gets one; an error refuses the login, as the
// attestation's does. skipped says why no record was written, "" when it was.
//
// The guard and the write are one step per identity (a lock per `sub`): else
// an older token could pass the guard, be overtaken by a newer one and write
// after it, leaving the older approver flag. The lock lives in this process:
// the login service runs as exactly one instance (the deployment view and the
// README, as the consumers' own per-agent lock needs); a second instance would
// need a compare-and-set spanning instances, on the guard's key-value revision
// or JetStream's expected last sequence. Not built. A stored record that is a newer
// one for the same account counts as written, never as a refusal.
func (s *Service) accountRecord(ctx context.Context, a Attester, guard TokenGuard, sub, chatID, name string, approver bool, anchor string, tokenIat time.Time, iat, exp int64) (skipped string, err error) {
	switch {
	case chatID == "":
		return "no proven chat account", nil
	case guard == nil:
		return "no directory, so no guard", nil
	case !chatIDRE.MatchString(chatID):
		return "the chat's user id is not a 26 character id", nil
	case tokenIat.IsZero():
		return "the token has no iat", nil
	}
	defer s.lockSub(sub)()
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
	subject := s.accountSubject(chatID)
	if rp, ok := a.(RecordPublisher); ok {
		return "", rp.PublishRecord(ctx, subject, body, func(stored []byte) bool {
			var got AccountRecord
			return json.Unmarshal(stored, &got) == nil && got.V == 1 && got.Account == chatID && got.TokenIat > rec.TokenIat
		})
	}
	return "", a.Publish(ctx, subject, body)
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
