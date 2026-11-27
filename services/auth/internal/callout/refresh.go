package callout

// The approvers' refresh (RT-6.42 step 3, #266, the owner's decision of
// 2026-11-27): on a schedule, without a login, the service writes the account
// record with the approver flag for each member of the approvers' group whose
// Mattermost account is bound to their Keycloak identity, so a person who
// only uses Mattermost stays an approver while plan B piece 4 judges by that
// record. The flag comes from Keycloak's group membership (ApproverGroup),
// never from Mattermost; the binding is the one a login checks (an OpenID
// account whose auth_data is the `sub`, token.Verifier.BoundAccount); the
// anchor is the one the directory holds, never made here. Same subject,
// format and guard as a login's record (accountRecord), so the refresh needs
// no right the service's JetStream user does not already have.

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

// MaxApprovers bounds the members one run reads and writes records for. A
// group with more is cut, logged, and those left out get no record: theirs
// runs out.
const MaxApprovers = 256

// ApproverGrace is how long a refreshed record outlives its period: room for
// a run's own length, so a member stays an approver from one run to the
// next. It also bounds a run.
const ApproverGrace = time.Minute

// Approver is an enabled member of the approvers' group, as Keycloak lists it.
type Approver struct {
	Sub      string // the Keycloak user id: the `sub` of the person's tokens
	Username string // the Keycloak username: the chat username, as preferred_username is
}

// ApproverGroup lists the enabled members of the approvers' group, at most
// max of them; cut says there are more (keycloak.Group, through app). An
// error is an unknown answer, never an empty group.
type ApproverGroup interface {
	Members(ctx context.Context, max int) (members []Approver, cut bool, err error)
}

// ChatBinding is the chat account bound to `sub` under the chat name
// username, as a login checks it (token.Verifier.BoundAccount): its chat id,
// "" for an account that does not log in over OpenID Connect.
type ChatBinding interface {
	BoundAccount(ctx context.Context, username, sub string) (string, error)
}

// AnchorReader is the anchor the directory holds for `sub`, false when it
// holds none; it writes nothing (directory.Persons).
type AnchorReader interface {
	Anchor(ctx context.Context, sub string) (string, bool, error)
}

// ApproverRefresh is what the refresh reads and how often.
type ApproverRefresh struct {
	Group   ApproverGroup
	Chat    ChatBinding
	Anchors AnchorReader
	// Period is the time between two runs; a record is valid Period plus
	// ApproverGrace from its run (app checks 1 to 14 minutes, so that stays
	// within the 15 minutes the MCP server lets a record's flag live).
	Period time.Duration
}

// approverRef is the account an approver's record was written for.
type approverRef struct {
	account, name, anchor string
}

// refreshRun counts one run, for the log and the tests.
type refreshRun struct {
	members  int  // enabled members Keycloak listed (after the cap)
	cut      bool // Keycloak has more than MaxApprovers
	written  int  // records with the flag
	revoked  int  // records without it, for members who left the group
	skipped  int  // members with no bound account, no anchor, or a newer record
	failed   int  // members whose lookup or write failed: retried next run
	err      error
	recovers int // a panic caught in the run (never a crash)
}

// RefreshApprovers runs the refresh at once and then every r.Period until
// ctx ends. A run that fails is logged and the next one tries again; nothing
// here stops the service.
func (s *Service) RefreshApprovers(ctx context.Context, r ApproverRefresh) {
	if r.Group == nil || r.Chat == nil || r.Anchors == nil || r.Period <= 0 {
		s.log.Error("approver refresh", "event", "not started", "reason", "incomplete configuration")
		return
	}
	t := time.NewTicker(r.Period)
	defer t.Stop()
	var last map[string]approverRef
	for {
		var run refreshRun
		last, run = s.refreshOnce(ctx, r, last)
		s.logRun(run)
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

func (s *Service) logRun(run refreshRun) {
	attrs := []any{"members", run.members, "written", run.written, "revoked", run.revoked,
		"skipped", run.skipped, "failed", run.failed}
	switch {
	case run.err != nil:
		s.log.Warn("approver refresh", append([]any{"event", "run failed", "reason", run.err.Error()}, attrs...)...)
	case run.cut:
		s.log.Warn("approver refresh", append([]any{"event", "group cut", "max", MaxApprovers}, attrs...)...)
	case run.failed > 0:
		s.log.Warn("approver refresh", append([]any{"event", "run done with failures"}, attrs...)...)
	default:
		s.log.Info("approver refresh", append([]any{"event", "run done"}, attrs...)...)
	}
}

// refreshOnce is one run, bounded by ApproverGrace and safe from a panic:
// last is the run before's approvers, the result this run's.
func (s *Service) refreshOnce(ctx context.Context, r ApproverRefresh, last map[string]approverRef) (next map[string]approverRef, run refreshRun) {
	defer func() {
		if p := recover(); p != nil {
			next, run = last, refreshRun{err: fmt.Errorf("panic: %v", p), recovers: 1}
		}
	}()
	ctx, cancel := context.WithTimeout(ctx, ApproverGrace)
	defer cancel()
	return s.refreshApprovers(ctx, r, last)
}

// refreshApprovers writes, for each member of the group with a bound account
// and an anchor, the record with the flag, valid r.Period plus ApproverGrace;
// and for each approver of the run before who is no longer a member, while
// the same account is still theirs, the record without it. A member whose
// account is no longer bound gets nothing: their record runs out. When
// Keycloak cannot be read nothing is written and last is kept, so who left
// in between loses the flag at the next run that reads the group.
func (s *Service) refreshApprovers(ctx context.Context, r ApproverRefresh, last map[string]approverRef) (map[string]approverRef, refreshRun) {
	var run refreshRun
	at, _ := s.attestHooks()
	guard, _ := s.anchorHook().(TokenGuard)
	if at == nil || guard == nil {
		run.err = errors.New("no record publisher or no directory")
		return last, run
	}
	members, cut, err := r.Group.Members(ctx, MaxApprovers)
	if err != nil {
		run.err = err
		return last, run
	}
	run.members, run.cut = len(members), cut
	now := s.now()
	iat, exp := now.Unix(), now.Add(r.Period+ApproverGrace).Unix()
	// The token guard keys by second: the run's own time stands for the
	// token's issue time, so a login's token issued before this read never
	// replaces it, and one issued after it does.
	asOf := time.Unix(iat, 0)
	next := map[string]approverRef{}
	member := map[string]bool{}
	write := func(sub string, ref approverRef, approver bool) bool {
		skipped, err := s.accountRecord(ctx, at, guard, sub, ref.account, ref.name, approver, ref.anchor, asOf, iat, exp)
		switch {
		case err != nil:
			run.failed++
			s.log.Warn("approver refresh", "event", "record not written", "sub", sub, "reason", err.Error())
			return false
		case skipped != "":
			run.skipped++
			s.log.Info("approver refresh", "event", "account record kept", "sub", sub, "reason", skipped)
			return false
		}
		return true
	}
	for _, m := range members {
		member[m.Sub] = true
		ref, why, err := s.approverAccount(ctx, r, m.Sub, m.Username)
		switch {
		case err != nil:
			run.failed++
			s.log.Warn("approver refresh", "event", "lookup failed", "sub", m.Sub, "reason", err.Error())
			continue
		case why != "":
			run.skipped++
			s.log.Debug("approver refresh", "event", "no record", "sub", m.Sub, "reason", why)
			continue
		}
		if write(m.Sub, ref, true) {
			run.written++
			next[m.Sub] = ref
		}
	}
	if cut { // who is beyond the cap may still be a member: nobody is revoked for it
		for sub := range last {
			member[sub] = true
		}
	}
	for sub, old := range last {
		if member[sub] {
			continue
		}
		// Left the group (or was disabled): the record without the flag, but
		// only for the account that is still bound to the same identity, so
		// a record never binds an account another identity holds now.
		ref, why, err := s.approverAccount(ctx, r, sub, old.name)
		switch {
		case err != nil:
			run.failed++
			s.log.Warn("approver refresh", "event", "lookup failed", "sub", sub, "reason", err.Error())
		case why != "" || ref.account != old.account:
			run.skipped++
			s.log.Debug("approver refresh", "event", "no record", "sub", sub, "reason", "no longer bound: the record runs out")
		default:
			if write(sub, ref, false) {
				run.revoked++
			}
		}
	}
	return next, run
}

// approverAccount is the bound chat account and the anchor of `sub` under the
// chat name username; why says why there is none, err that a lookup failed.
func (s *Service) approverAccount(ctx context.Context, r ApproverRefresh, sub, username string) (approverRef, string, error) {
	chatID, err := r.Chat.BoundAccount(ctx, username, sub)
	switch {
	case errors.Is(err, token.ErrDirectory):
		return approverRef{}, "", err
	case err != nil:
		return approverRef{}, err.Error(), nil
	case chatID == "":
		return approverRef{}, "the chat account does not log in over OpenID Connect", nil
	case !chatIDRE.MatchString(chatID):
		return approverRef{}, "the chat's user id is not a 26 character id", nil
	}
	anchor, ok, err := r.Anchors.Anchor(ctx, sub)
	switch {
	case err != nil:
		return approverRef{}, "", err
	case !ok:
		return approverRef{}, "the directory holds no anchor for the identity", nil
	}
	return approverRef{account: chatID, name: username, anchor: anchor}, "", nil
}
