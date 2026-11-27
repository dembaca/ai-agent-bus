package callout

// The binding of a Mattermost account, proven once (RT-6.42 step 3, the
// owner's decision of 2026-11-27, issue 266): a login whose chat account is
// proven to be the token's identity (OpenID: auth_data is `sub`; LDAP:
// auth_data is the token's LDAP id) keeps the binding in the directory, and
// the login service publishes the account record and its proof again from
// there. So a person who logged in once stays bound without logging in
// again, while the chat still shows the account as proven; the bridge checks
// that at every post too.

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/directory"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

// BindingStore keeps the proven bindings durably (directory.Anchors has it).
// The service checks for it by type assertion on what SetAnchors is given;
// without it the records are written at the logins alone, as before.
type BindingStore interface {
	PutBinding(ctx context.Context, b directory.Binding) error
	Binding(ctx context.Context, account string) (directory.Binding, uint64, bool, error)
	BindingAccounts(ctx context.Context) ([]string, error)
	DropBinding(ctx context.Context, account string, rev uint64) error
}

// ChatAccounts reads a chat account by the chat's id (mattermost.Client):
// token.ErrNoSuchUser for one that is gone.
type ChatAccounts interface {
	UserByID(ctx context.Context, id string) (token.User, error)
}

// BindingSweepEvery is how often the bindings are checked against the chat
// and their records published again: well within PRESENCE's 24 hours, so a
// record never ages out while its binding holds, and soon after a change in
// the chat for the MCP servers, which cannot read auth_data themselves.
const (
	BindingSweepEvery   = time.Hour
	bindingSweepTimeout = 5 * time.Minute
)

// SweepBindings checks every stored binding at once and then every `every`
// (0: BindingSweepEvery) until ctx ends. A failed round only waits for the
// next; a record it could not publish ages out at the latest after 24 hours,
// and the account is unbound then (fail closed).
func (s *Service) SweepBindings(ctx context.Context, chat ChatAccounts, every time.Duration) {
	if every <= 0 {
		every = BindingSweepEvery
	}
	tick := time.NewTicker(every)
	defer tick.Stop()
	for {
		rctx, cancel := context.WithTimeout(ctx, bindingSweepTimeout)
		kept, dropped, err := s.sweepBindings(rctx, chat)
		cancel()
		switch {
		case err != nil && ctx.Err() == nil:
			s.log.Warn("bindings: the sweep failed", "reason", err.Error(), "kept", kept, "unbound", dropped)
		case err == nil && kept+dropped > 0:
			s.log.Info("bindings: swept", "kept", kept, "unbound", dropped)
		}
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
	}
}

// sweepBindings checks every stored binding against the chat once. A binding
// the chat still shows (the account there, active, no bot, no guest, logging
// in as it was proven, with the same auth_data) gets its account record, as
// last written, and its proof published again; one the chat no longer shows
// is unbound: an account record with `anchor` null, and the binding dropped.
// An account the chat could not be asked about stays as it is this round.
func (s *Service) sweepBindings(ctx context.Context, chat ChatAccounts) (kept, dropped int, err error) {
	bs, ok := s.anchorHook().(BindingStore)
	at, _ := s.attestHooks()
	if !ok || at == nil || chat == nil {
		return 0, 0, nil
	}
	accounts, err := bs.BindingAccounts(ctx)
	if err != nil {
		return 0, 0, err
	}
	var errs []error
	for _, account := range accounts {
		b, rev, ok, err := bs.Binding(ctx, account)
		if err != nil || !ok {
			errs = append(errs, err)
			continue
		}
		u, err := chat.UserByID(ctx, account)
		why := ""
		switch {
		case errors.Is(err, token.ErrNoSuchUser):
			why = "the account is gone"
		case err != nil:
			errs = append(errs, err)
			continue
		default:
			why = unbound(u, b)
		}
		if why == "" && !u.AuthKnown {
			errs = append(errs, errors.New("the chat did not say how the account logs in"))
			continue
		}
		done, err := s.sweepOne(ctx, at, bs, b, rev, why)
		if err != nil {
			errs = append(errs, err)
			continue
		}
		switch {
		case !done:
		case why == "":
			kept++
		default:
			dropped++
			s.log.Warn("bindings: an account unbound", "account", account, "sub", b.Sub, "reason", why)
		}
	}
	return kept, dropped, errors.Join(errs...)
}

// unbound says why the chat no longer shows the binding, "" while it does.
func unbound(u token.User, b directory.Binding) string {
	switch {
	case u.ID != b.Account:
		return "the chat answered for another account"
	case u.Bot:
		return "a bot"
	case u.Deactivated:
		return "deactivated"
	case u.Guest:
		return "a guest"
	case !u.AuthKnown:
		return ""
	case u.AuthService != b.AuthService || u.AuthData != b.AuthData:
		return "it logs in otherwise than when it was proven"
	}
	return ""
}

// sweepOne publishes one binding's records again, or unbinds it (why not
// ""), under the identity's lock, so a login of that identity is not
// overtaken; a binding written again since it was read waits for the next
// round (done false).
func (s *Service) sweepOne(ctx context.Context, at Attester, bs BindingStore, b directory.Binding, rev uint64, why string) (done bool, err error) {
	defer s.lockSub(b.Sub)()
	now, again, ok, err := bs.Binding(ctx, b.Account)
	if err != nil || !ok || again != rev {
		return false, err
	}
	var rec AccountRecord
	if err := json.Unmarshal(now.Record, &rec); err != nil || rec.Account != b.Account {
		why = "its stored record does not decode"
	}
	if why == "" {
		if err := s.publishAccount(ctx, at, b.Account, now.Record, rec.TokenIat); err != nil {
			return false, err
		}
		return true, s.publishProof(ctx, at, ProofRecord{V: 1, Account: b.Account, AuthService: now.AuthService,
			AuthData: now.AuthData, Iat: now.At})
	}
	t := s.now().Unix()
	body, err := json.Marshal(unboundRecord{V: 1, Account: b.Account, Name: rec.Name, TokenIat: rec.TokenIat, Iat: t, Exp: t})
	if err != nil {
		return false, err
	}
	if err := s.publishAccount(ctx, at, b.Account, body, rec.TokenIat); err != nil {
		return false, err
	}
	return true, bs.DropBinding(ctx, b.Account, rev)
}

// unboundRecord is an account record with `anchor` null (format v1): the
// account is bound to nobody.
type unboundRecord struct {
	V        int     `json:"v"`
	Account  string  `json:"account"`
	Anchor   *string `json:"anchor"`
	Name     string  `json:"name"`
	Approver bool    `json:"approver"`
	TokenIat int64   `json:"token_iat"`
	Iat      int64   `json:"iat"`
	Exp      int64   `json:"exp"`
}
