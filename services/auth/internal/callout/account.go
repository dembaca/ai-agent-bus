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
	"time"
)

// chatIDRE is a Mattermost user id: 26 characters, no dot or wildcard, so it
// is one safe subject token.
var chatIDRE = regexp.MustCompile(`^[a-z0-9]{26}$`)

// TokenGuard keeps, per identity, the issue time of the newest token whose
// record was written (directory.Anchors has it). The service checks for it by
// type assertion on what SetAnchors is given; without it no account record is
// written, since a record could then be replaced by one from an older token.
type TokenGuard interface {
	NewestToken(ctx context.Context, sub string, iat int64) (bool, error)
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
// attestation's does.
func (s *Service) accountRecord(ctx context.Context, a Attester, guard TokenGuard, sub, chatID, name string, approver bool, anchor string, tokenIat time.Time, iat, exp int64) (written bool, err error) {
	if chatID == "" || guard == nil || !chatIDRE.MatchString(chatID) || tokenIat.IsZero() {
		return false, nil
	}
	fresh, err := guard.NewestToken(ctx, sub, tokenIat.Unix())
	if err != nil || !fresh {
		return false, err
	}
	body, err := json.Marshal(AccountRecord{V: 1, Account: chatID, Anchor: anchor, Name: name, Approver: approver,
		TokenIat: tokenIat.Unix(), Iat: iat, Exp: exp})
	if err != nil {
		return false, err
	}
	return true, a.Publish(ctx, s.accountSubject(chatID), body)
}
