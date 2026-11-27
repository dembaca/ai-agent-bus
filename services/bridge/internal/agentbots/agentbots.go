// Package agentbots gives each NATS agent a Mattermost bot of its own, so the
// bridge posts that agent's copies under it (RT-6.44, issue #151 step 1).
//
// The bots are made by an admin bot (system_admin) whose token only this
// package uses, through the registrar's Mattermost adapter; the bot that
// posts everything else stays without admin rights. A bot is named the
// bridge's prefix plus the agent's NATS name, owned by the Mattermost account
// that the login service's account record binds to the agent's presence
// owner, and marked as the bridge's in its description. Its token is minted
// at its first use after each start and kept in memory only; the bot's
// earlier tokens of the bridge are revoked then. Whatever fails, Bot gets nil
// and the caller posts with the one bridge bot, as before; Client, which a read
// in the agent's name asks for, answers the error instead and never falls back.
// The sweep (sweep.go) disables the bots of agents idle longer than the keep
// time.
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
	"sync/atomic"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mirror"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mm"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/presence"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/registrar/core"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/registrar/mattermost"
)

// Marker starts the description of every bot the bridge creates: the whole
// description is Marker plus the agent's name. A bot without it is not the
// bridge's for that agent and is never posted with.
const Marker = "bus bridge copies of "

// SweptSuffix follows Marker plus the agent's name in the description of a
// bot the sweep disabled (RT-6.44 step 8): only such a bot is enabled again
// when its agent comes back. A disabled bot with the plain mark was
// disabled by an admin and stays off.
const SweptSuffix = " (swept)"

// errAdminDisabled is a disabled bot with the plain mark: an admin's, not
// the sweep's.
var errAdminDisabled = errors.New("the bot is disabled by an admin")

// TokenDescription marks the access tokens the bridge mints, so a restart
// revokes its own earlier tokens and no other.
const TokenDescription = "bus bridge (RT-6.44)"

// DefaultBackoff is how long a name posts with the bridge bot after its own
// bot failed, before the bridge tries again.
const DefaultBackoff = 5 * time.Minute

// postingRoles are the only roles the bridge bot that posts may hold: a
// user's and the bot roles that let it post. Anything else (system_admin,
// system_manager, system_user_manager, system_read_only_admin, a custom
// role) refuses the start.
var postingRoles = []string{"system_user", "system_post_all", "system_post_all_public"}

// Presence is the presence snapshot: the owner of an agent's name, and the
// newest presence seen at all (the sweep's check that the feed is alive).
type Presence interface {
	Get(name string) (presence.Info, bool)
	Newest() time.Time
}

// Owners is the login service's account records read the other way round
// (inbound.Accounts): the one Mattermost account bound to a person's anchor,
// "" when none or more than one is, or before the records are read.
type Owners interface {
	Owner(anchor string) string
}

// Bots finds or creates the agents' bots. It is safe for concurrent use; one
// name is set up by one caller at a time.
type Bots struct {
	Admin    *mattermost.Adapter // with the admin token, TokenDescription and the team
	Presence Presence
	Owners   Owners // the account records; nil binds no owner, so every agent falls back
	Prefix   string // CB_BRIDGE_MM_BOT_PREFIX
	URL      string // Mattermost, for the bots' own clients
	Log      *slog.Logger
	Backoff  time.Duration // DefaultBackoff when 0
	Now      func() time.Time

	adminID string
	mu      sync.Mutex
	by      map[string]*entry
}

type entry struct {
	mu      sync.Mutex
	agent   core.Agent
	owner   string     // the owner's user id the bot was checked against
	client  *mm.Client // nil until minted
	joined  map[string]bool
	failed  time.Time
	noOwner bool // logged once that no record names the owner
	off     bool // logged once that an admin disabled the bot
	// posted is the time of the agent's latest message (Bot), Unix
	// nanoseconds: a sign for the sweep beside the presence (RT-6.44 step 8)
	posted atomic.Int64
}

// Check is the start check (RT-6.44): the admin token is a bot's with
// system_admin, the posting token's account holds no role beyond
// postingRoles.
func (b *Bots) Check(ctx context.Context, postingToken string) error {
	admin, err := b.Admin.Account(ctx, b.Admin.Cfg.Token)
	if err != nil {
		return fmt.Errorf("CB_BRIDGE_MM_ADMIN_TOKEN_FILE: the admin account: %w", err)
	}
	if !admin.IsBot || !hasRole(admin.Roles, "system_admin") {
		return errors.New("CB_BRIDGE_MM_ADMIN_TOKEN_FILE: the admin account must be a bot with the system_admin role")
	}
	poster, err := b.Admin.Account(ctx, postingToken)
	if err != nil {
		return fmt.Errorf("CB_BRIDGE_MM_TOKEN_FILE: the bridge bot: %w", err)
	}
	for _, r := range strings.Fields(poster.Roles) {
		if !slices.Contains(postingRoles, r) {
			return fmt.Errorf("CB_BRIDGE_MM_TOKEN_FILE: the bridge bot that posts must hold no role beyond %s; it holds %q",
				strings.Join(postingRoles, ", "), r)
		}
	}
	if admin.ID == poster.ID {
		return errors.New("CB_BRIDGE_MM_ADMIN_TOKEN_FILE: the admin bot must be another account than the bridge bot")
	}
	b.adminID = admin.ID
	return nil
}

func hasRole(roles, role string) bool { return slices.Contains(strings.Fields(roles), role) }

// username is the Mattermost username's rule; a name that would break it
// gets no bot of its own.
var username = regexp.MustCompile(`^[a-z][a-z0-9._-]{2,63}$`)

func (b *Bots) entry(name string) *entry {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.by == nil {
		b.by = map[string]*entry{}
	}
	e := b.by[name]
	if e == nil {
		e = &entry{joined: map[string]bool{}}
		b.by[name] = e
	}
	return e
}

// owner is the Mattermost user id of name's owner: the presence owner's
// anchor through the login service's account records, never a username.
func (b *Bots) owner(name string) string {
	info, ok := b.Presence.Get(name)
	if !ok || b.Owners == nil {
		return ""
	}
	return b.Owners.Owner(ownerAnchor(info.Owner))
}

// NotInChannel is a read's refusal: the agent's bot is not a member of the
// channel named, and a read never joins it there (ADR-014 phase 5, piece 5).
type NotInChannel struct{ Channel string }

func (e *NotInChannel) Error() string { return "the agent's bot is not in " + e.Channel }

// Bot returns a poster for name's own bot, joined to home, or nil: the
// caller then posts with the bridge bot (RT-6.44).
func (b *Bots) Bot(ctx context.Context, name, home string) mirror.Sink {
	c, err := b.client(ctx, name, home, true)
	if err != nil {
		return nil
	}
	return c
}

// Client returns name's own bot as the client of its token, and the error why
// it cannot be had: no account record binds its owner, an admin disabled it, a
// mint failed, a failure before is still within the backoff, or the bot is not
// a member of channel. It never joins the bot to channel and never falls back
// to the bridge bot: a read in the agent's name has no other token, and joining
// is the posting path's (Bot, mirror.Sink) alone (ADR-014 phase 5, RT-6.44).
func (b *Bots) Client(ctx context.Context, name, channel string) (*mm.Client, error) {
	return b.client(ctx, name, channel, false)
}

// client is Bot and Client: join true joins the bot to channel and caches it;
// false checks the membership and never joins.
func (b *Bots) client(ctx context.Context, name, channel string, join bool) (*mm.Client, error) {
	e := b.entry(name)
	e.posted.Store(b.now().UnixNano())
	e.mu.Lock()
	defer e.mu.Unlock()
	owner := b.owner(name)
	if owner == "" {
		// no presence, or no record binds its owner (yet): no backoff, the
		// presence or the record may come any moment
		if !e.noOwner {
			e.noOwner = true
			b.Log.Info("agentbots: no account record binds the agent's owner; the bridge bot posts for it", "agent", name)
		}
		return nil, errors.New("no account record binds the agent's owner")
	}
	e.noOwner = false
	if e.client != nil && e.owner != owner {
		b.Log.Warn("agentbots: the owner's account record changed; the bot is checked again", "agent", name)
		e.client, e.joined = nil, map[string]bool{}
	}
	if e.client != nil && join && e.joined[channel] {
		return e.client, nil
	}
	if !e.failed.IsZero() && b.now().Sub(e.failed) < b.backoff() {
		return nil, fmt.Errorf("its bot failed before; trying again in %s", b.backoff().String())
	}
	if e.client == nil {
		a, err := b.setUp(ctx, name, owner)
		if errors.Is(err, errAdminDisabled) {
			// logged once per name; the backoff spares Mattermost a call per message
			e.failed = b.now()
			if !e.off {
				e.off = true
				b.Log.Warn("agentbots: bot disabled by an admin, posting as the bridge bot", "agent", name)
			}
			return nil, err
		}
		if err != nil {
			return nil, b.fail(e, name, err)
		}
		e.off = false
		tok, err := b.mint(ctx, a)
		if err != nil {
			return nil, b.fail(e, name, err)
		}
		e.agent, e.owner, e.client = a, owner, mm.New(b.URL, tok).As(a.ID)
	}
	if join {
		if err := b.Admin.JoinChannels(ctx, e.agent, []string{channel}); err != nil {
			return nil, b.fail(e, name, fmt.Errorf("join %s: %w", channel, err))
		}
		e.joined[channel] = true
	} else {
		member, err := b.Admin.MemberOf(ctx, e.agent, channel)
		switch {
		case err != nil:
			return nil, b.fail(e, name, fmt.Errorf("membership of %s: %w", channel, err))
		case !member:
			// no backoff: the bot may be added to the channel at any time
			return nil, &NotInChannel{Channel: channel}
		}
	}
	e.failed = time.Time{}
	return e.client, nil
}

// Unreact removes the reaction emoji of name's own bot from a post (RT-6.44):
// with the bot's client while it has one, else, or when that fails, with
// the admin token as the bot, also within the backoff or without an owner
// now, so no removal waits or is lost. One that is gone already is no error.
func (b *Bots) Unreact(ctx context.Context, name, post, emoji string) error {
	e := b.entry(name)
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.client != nil {
		err := e.client.Unreact(ctx, post, emoji)
		var refused *mirror.Refused
		if err == nil || errors.As(err, &refused) {
			return nil
		}
		if mirror.Revoked(err) {
			e.client, e.joined, e.failed = nil, map[string]bool{}, b.now()
		}
		b.Log.Info("agentbots: the admin bot removes the bot's reaction", "agent", name, "error", err)
	}
	id := e.agent.ID
	if id == "" {
		a, err := b.ours(ctx, name)
		if err != nil {
			return err
		}
		if a == nil {
			b.Log.Warn("agentbots: no bot of the bridge's left for a reaction to remove", "agent", name)
			return nil
		}
		id = a.ID
	}
	return b.Admin.RemoveReaction(ctx, id, post, emoji)
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

func (b *Bots) fail(e *entry, name string, err error) error {
	e.failed = b.now()
	b.Log.Warn("agentbots: the agent's bot is not available", "agent", name, "error", err)
	return err
}

// ours is name's bot when it is an active bot with the bridge's mark for
// name, nil when there is none of that name; an error for anything else.
func (b *Bots) ours(ctx context.Context, name string) (*core.Agent, error) {
	user := b.Prefix + name
	found, err := b.Admin.FindAgent(ctx, user)
	if err != nil || found == nil {
		return nil, err
	}
	bot, err := b.Admin.BotOf(ctx, found.ID)
	switch {
	case err != nil:
		return nil, err
	case bot == nil:
		return nil, fmt.Errorf("%s is no active bot", user)
	case bot.Description != Marker+name:
		return nil, fmt.Errorf("%s is not the bridge's bot", user)
	}
	return found, nil
}

// setUp finds the bot, or creates it and binds its owner, the account
// ownerID; a bot owned by any other account is not used.
func (b *Bots) setUp(ctx context.Context, name, ownerID string) (core.Agent, error) {
	user := b.Prefix + name
	if !username.MatchString(user) {
		return core.Agent{}, fmt.Errorf("%q is no Mattermost username", user)
	}
	owner, err := b.Admin.HumanByID(ctx, ownerID)
	if err != nil {
		return core.Agent{}, fmt.Errorf("owner: %w", err)
	}
	if owner == nil {
		return core.Agent{}, errors.New("the owner's account is no active person in Mattermost")
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
	// disabled too: the sweep (sweep.go) disables the bots of idle agents
	// with the swept mark, and one that comes back gets its bot enabled again
	// below; a disabled bot with the plain mark was an admin's doing
	bot, err := b.Admin.BotOfAny(ctx, found.ID)
	if err != nil {
		return core.Agent{}, err
	}
	mark, swept := Marker+name, Marker+name+SweptSuffix
	switch {
	case bot == nil:
		return core.Agent{}, fmt.Errorf("%s is no bot", user)
	case bot.Description != mark && bot.Description != swept:
		return core.Agent{}, fmt.Errorf("%s is not the bridge's bot", user)
	case bot.DeleteAt != 0 && bot.Description == mark:
		return core.Agent{}, errAdminDisabled
	case bot.OwnerID == b.adminID && b.adminID != "":
		// created by the bridge, but its owner was never bound
		if err := b.Admin.BindOwner(ctx, *found, *owner); err != nil {
			return core.Agent{}, fmt.Errorf("bind the owner: %w", err)
		}
	case bot.OwnerID != owner.ID:
		return core.Agent{}, fmt.Errorf("%s has another owner than the agent's account record names", user)
	}
	if bot.DeleteAt != 0 {
		if err := b.Admin.EnableBot(ctx, found.ID); err != nil {
			return core.Agent{}, fmt.Errorf("enable: %w", err)
		}
		b.Log.Info("agentbots: enabled again", "agent", name, "bot", user)
	}
	// the swept mark goes back to the plain one; an enabled bot that still
	// has it is one whose sweep or return was cut off between two calls
	if bot.Description == swept {
		if err := b.Admin.UpdateBotDescription(ctx, found.ID, mark); err != nil {
			return core.Agent{}, fmt.Errorf("restore the mark: %w", err)
		}
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

// ownerAnchor is the presence subject's owner token as the person's anchor:
// "." is written "~" there, and "-" names none (RT-6.37 step 1).
func ownerAnchor(token string) string {
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
