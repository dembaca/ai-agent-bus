// Package agentbots gives each NATS agent a Mattermost bot of its own, so the
// bridge posts that agent's copies under it (RT-6.44, issue #151 step 1).
//
// The bots are made by an admin bot (system_admin) whose token only this
// package uses, through the registrar's Mattermost adapter; the bot that
// posts everything else stays without admin rights. A bot is named the
// bridge's prefix plus the agent's NATS name, owned by the agent's presence
// owner and marked as the bridge's in its description. Its token is minted at
// its first use after each start and kept in memory only; the bot's earlier
// tokens of the bridge are revoked then. Whatever fails, the caller gets nil
// and posts with the one bridge bot, as before.
package agentbots

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mirror"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mm"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/presence"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/registrar/core"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/registrar/mattermost"
)

// Marker starts the description of every bot the bridge creates; a bot
// without it is not the bridge's and is never posted with.
const Marker = "bus bridge copies of "

// TokenDescription marks the access tokens the bridge mints, so a restart
// revokes its own earlier tokens and no other.
const TokenDescription = "bus bridge (RT-6.44)"

// DefaultBackoff is how long a name posts with the bridge bot after its own
// bot failed, before the bridge tries again.
const DefaultBackoff = 5 * time.Minute

// Presence is the presence snapshot: the owner of an agent's name.
type Presence interface {
	Get(name string) (presence.Info, bool)
}

// Bots finds or creates the agents' bots. It is safe for concurrent use; one
// name is set up by one caller at a time.
type Bots struct {
	Admin    *mattermost.Adapter // with the admin token, TokenDescription and the team
	Presence Presence
	Prefix   string // BRIDGE_BOT_PREFIX
	URL      string // Mattermost, for the bots' own clients
	Log      *slog.Logger
	Backoff  time.Duration // DefaultBackoff when 0
	Now      func() time.Time

	adminID string
	mu      sync.Mutex
	by      map[string]*entry
}

type entry struct {
	mu     sync.Mutex
	agent  core.Agent
	client *mm.Client // nil until minted
	joined map[string]bool
	failed time.Time
}

// Check is the start check (RT-6.44): the admin token is a bot's with
// system_admin, the posting token's account has no system_admin.
func (b *Bots) Check(ctx context.Context, postingToken string) error {
	admin, err := b.Admin.Account(ctx, b.Admin.Cfg.Token)
	if err != nil {
		return fmt.Errorf("BRIDGE_MM_ADMIN_TOKEN_FILE: the admin account: %w", err)
	}
	if !admin.IsBot || !hasRole(admin.Roles, "system_admin") {
		return errors.New("BRIDGE_MM_ADMIN_TOKEN_FILE: the admin account must be a bot with the system_admin role")
	}
	poster, err := b.Admin.Account(ctx, postingToken)
	if err != nil {
		return fmt.Errorf("BRIDGE_MM_TOKEN_FILE: the bridge bot: %w", err)
	}
	if hasRole(poster.Roles, "system_admin") {
		return errors.New("BRIDGE_MM_TOKEN_FILE: the bridge bot that posts must not have the system_admin role")
	}
	if admin.ID == poster.ID {
		return errors.New("BRIDGE_MM_ADMIN_TOKEN_FILE: the admin bot must be another account than the bridge bot")
	}
	b.adminID = admin.ID
	return nil
}

func hasRole(roles, role string) bool { return slices.Contains(strings.Fields(roles), role) }

// username is the Mattermost username's rule; a name that would break it
// gets no bot of its own.
var username = regexp.MustCompile(`^[a-z][a-z0-9._-]{2,63}$`)

// Bot returns a poster for name's own bot, joined to home, or nil: the
// caller then posts with the bridge bot (RT-6.44).
func (b *Bots) Bot(ctx context.Context, name, home string) mirror.Sink {
	b.mu.Lock()
	if b.by == nil {
		b.by = map[string]*entry{}
	}
	e := b.by[name]
	if e == nil {
		e = &entry{joined: map[string]bool{}}
		b.by[name] = e
	}
	b.mu.Unlock()

	e.mu.Lock()
	defer e.mu.Unlock()
	if e.client != nil && e.joined[home] {
		return e.client
	}
	if !e.failed.IsZero() && b.now().Sub(e.failed) < b.backoff() {
		return nil
	}
	if e.client == nil {
		info, ok := b.Presence.Get(name)
		handle := ownerHandle(info.Owner)
		if !ok || handle == "" {
			return nil // no owner known yet: no backoff, the presence may come any moment
		}
		a, err := b.setUp(ctx, name, handle)
		if err != nil {
			return b.fail(e, name, err)
		}
		tok, err := b.mint(ctx, a)
		if err != nil {
			return b.fail(e, name, err)
		}
		e.agent, e.client = a, mm.New(b.URL, tok).As(a.ID)
	}
	if err := b.Admin.JoinChannels(ctx, e.agent, []string{home}); err != nil {
		return b.fail(e, name, fmt.Errorf("join %s: %w", home, err))
	}
	e.joined[home] = true
	e.failed = time.Time{}
	return e.client
}

// Forget drops name's client after Mattermost failed it: the bridge bot
// posts for name until the backoff is over, then the bot is found and its
// token minted again.
func (b *Bots) Forget(name string) {
	b.mu.Lock()
	e := b.by[name]
	b.mu.Unlock()
	if e == nil {
		return
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	e.client, e.joined, e.failed = nil, map[string]bool{}, b.now()
}

func (b *Bots) fail(e *entry, name string, err error) mirror.Sink {
	e.failed = b.now()
	b.Log.Warn("agentbots: the bridge bot posts for this agent", "agent", name, "error", err)
	return nil
}

// setUp finds the bot, or creates it and binds its owner.
func (b *Bots) setUp(ctx context.Context, name, handle string) (core.Agent, error) {
	user := b.Prefix + name
	if !username.MatchString(user) {
		return core.Agent{}, fmt.Errorf("%q is no Mattermost username", user)
	}
	owner, err := b.Admin.ResolveHuman(ctx, handle)
	if err != nil {
		return core.Agent{}, fmt.Errorf("owner: %w", err)
	}
	if owner == nil {
		return core.Agent{}, errors.New("the presence owner is no active person in Mattermost")
	}
	found, err := b.Admin.FindAgent(ctx, user)
	if err != nil {
		return core.Agent{}, err
	}
	if found == nil {
		a, err := b.Admin.CreateAgent(ctx, user, name, Marker+name)
		if err != nil {
			return core.Agent{}, fmt.Errorf("create: %w", err)
		}
		if err := b.Admin.BindOwner(ctx, a, *owner); err != nil {
			return core.Agent{}, fmt.Errorf("bind the owner: %w", err)
		}
		b.Log.Info("agentbots: created", "agent", name, "bot", a.Username)
		return a, nil
	}
	bot, err := b.Admin.BotOf(ctx, found.ID)
	if err != nil {
		return core.Agent{}, err
	}
	switch {
	case bot == nil:
		return core.Agent{}, fmt.Errorf("%s is no active bot", user)
	case !strings.HasPrefix(bot.Description, Marker):
		return core.Agent{}, fmt.Errorf("%s is not the bridge's bot", user)
	case bot.OwnerID == b.adminID && b.adminID != "":
		// created by the bridge, but its owner was never bound
		if err := b.Admin.BindOwner(ctx, *found, *owner); err != nil {
			return core.Agent{}, fmt.Errorf("bind the owner: %w", err)
		}
	case bot.OwnerID != owner.ID:
		return core.Agent{}, fmt.Errorf("%s has another owner than the agent", user)
	}
	return *found, nil
}

// mint revokes the bot's earlier bridge tokens and mints a new one.
func (b *Bots) mint(ctx context.Context, a core.Agent) (string, error) {
	if err := b.Admin.RevokeIssued(ctx, a); err != nil {
		return "", fmt.Errorf("revoke the earlier tokens: %w", err)
	}
	tok, err := b.Admin.IssueCredential(ctx, a)
	if err != nil {
		return "", fmt.Errorf("mint: %w", err)
	}
	return tok, nil
}

// ownerHandle is the presence subject's owner token as a username: "." is
// written "~" there, and "-" names none (RT-6.37 step 1).
func ownerHandle(token string) string {
	if token == "" || token == "-" {
		return ""
	}
	return strings.ReplaceAll(token, "~", ".")
}

func (b *Bots) now() time.Time {
	if b.Now != nil {
		return b.Now()
	}
	return time.Now()
}

func (b *Bots) backoff() time.Duration {
	if b.Backoff > 0 {
		return b.Backoff
	}
	return DefaultBackoff
}
