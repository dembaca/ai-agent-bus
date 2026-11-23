package admin

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"regexp"
	"slices"
	"sort"
	"time"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/directory"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/mattermost"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

// ErrAlreadyImported: the one import has run.
var ErrAlreadyImported = errors.New("admin: the import has already run")

// errTooBig: the union of a channel's members would pass MaxMembers.
var errTooBig = errors.New("admin: the channel would exceed the member limit")

var tooMany = fmt.Sprintf("more than %d members", directory.MaxMembers)

// beforeUnion runs right before an existing channel's members are written;
// tests use it to change the channel meanwhile. Nothing in production.
var beforeUnion = func(string) {}

// ErrAuthInvisible: the Mattermost token cannot see how accounts log in, so
// no account can be told apart as OpenID or password.
var ErrAuthInvisible = errors.New("admin: the Mattermost token cannot see how accounts log in; a system admin's token is needed")

var channelNameRE = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)

// Source is what the import reads from Mattermost (mattermost.Client).
type Source interface {
	TeamChannels(ctx context.Context) ([]mattermost.Channel, error)
	ChannelMemberIDs(ctx context.Context, channelID string) ([]string, error)
	Users(ctx context.Context, ids []string) ([]mattermost.User, error)
}

// SkippedChannel is a channel the import left out.
type SkippedChannel struct {
	Name   string `json:"name"`
	Reason string `json:"reason"`
}

// SkippedAccount is an account that became no member.
type SkippedAccount struct {
	Username string `json:"username"`
	Reason   string `json:"reason"`
}

// Report is what the import did.
type Report struct {
	ChannelsCreated []string         `json:"channels_created"`
	ChannelsUpdated []string         `json:"channels_updated"`
	ChannelsSkipped []SkippedChannel `json:"channels_skipped"`
	Identities      int              `json:"identities"`
	MembersAdded    int              `json:"members_added"`
	PendingAdded    int              `json:"pending_added"`
	AccountsSkipped []SkippedAccount `json:"accounts_skipped"`
	Next            string           `json:"next"`
}

// Import copies the team's channels and their human members from Mattermost
// into the directory, once. OpenID accounts become members by their `sub`;
// password accounts become pending claims (binding "open") or are skipped;
// they are never members. The mark comes last: an interrupted import has no
// mark and may be run again, which adds only what is missing.
func Import(ctx context.Context, store *directory.Store, source Source, binding string, by token.Identity) (Report, error) {
	return importAt(ctx, store, source, binding, by, time.Now())
}

func importAt(ctx context.Context, store *directory.Store, source Source, binding string, _ token.Identity, now time.Time) (Report, error) {
	rep := Report{ChannelsCreated: []string{}, ChannelsUpdated: []string{}, ChannelsSkipped: []SkippedChannel{},
		AccountsSkipped: []SkippedAccount{}, Next: "set AUTH_DIRECTORY_IMPORTED=true and restart"}
	done, err := store.Imported(ctx)
	if err != nil {
		return Report{}, err
	}
	if done {
		return Report{}, ErrAlreadyImported
	}
	chs, err := source.TeamChannels(ctx)
	if err != nil {
		return Report{}, fmt.Errorf("channels: %w", err)
	}
	sort.Slice(chs, func(i, j int) bool { return chs[i].Name < chs[j].Name })

	users := map[string]mattermost.User{}
	fetch := func(ids []string) error {
		var need []string
		for _, id := range ids {
			if _, ok := users[id]; !ok && !slices.Contains(need, id) {
				need = append(need, id)
			}
		}
		if len(need) == 0 {
			return nil
		}
		got, err := source.Users(ctx, need)
		if err != nil {
			return err
		}
		for _, u := range got {
			users[u.ID] = u
		}
		return nil
	}
	subs := map[string]bool{}
	skippedAcc := map[SkippedAccount]bool{}
	skipAccount := func(u mattermost.User, reason string) {
		a := SkippedAccount{Username: u.Username, Reason: reason}
		if !skippedAcc[a] {
			skippedAcc[a] = true
			rep.AccountsSkipped = append(rep.AccountsSkipped, a)
		}
	}
	isOpenID := func(u mattermost.User) bool { return u.AuthService == token.OpenIDService && u.AuthData != "" }
	human := func(u mattermost.User) string {
		switch {
		case u.IsBot:
			return "bot"
		case u.Deactivated:
			return "deactivated"
		case u.Guest:
			return "guest"
		}
		return ""
	}

	for _, ch := range chs {
		if ch.Type != "O" && ch.Type != "P" { // never a direct or group message
			rep.ChannelsSkipped = append(rep.ChannelsSkipped, SkippedChannel{Name: ch.Name, Reason: "not a team channel"})
			continue
		}
		if !channelNameRE.MatchString(ch.Name) {
			rep.ChannelsSkipped = append(rep.ChannelsSkipped, SkippedChannel{Name: ch.Name, Reason: "not a valid channel name on the bus"})
			continue
		}
		ids, err := source.ChannelMemberIDs(ctx, ch.ID)
		if errors.Is(err, mattermost.ErrTooMany) {
			rep.ChannelsSkipped = append(rep.ChannelsSkipped, SkippedChannel{Name: ch.Name, Reason: tooMany})
			continue
		}
		if err != nil {
			return rep, fmt.Errorf("channel %s: members: %w", ch.Name, err)
		}
		if err := fetch(ids); err != nil {
			return rep, fmt.Errorf("channel %s: users: %w", ch.Name, err)
		}
		var humans []mattermost.User
		openIDs := map[string]bool{}
		for _, id := range ids {
			u, ok := users[id]
			if !ok {
				continue
			}
			if why := human(u); why != "" {
				skipAccount(u, why)
				continue
			}
			if !u.AuthKnown { // before anything more is written
				return rep, ErrAuthInvisible
			}
			humans = append(humans, u)
			if isOpenID(u) {
				openIDs[u.AuthData] = true
			}
		}
		// The size after the import, members already in the directory
		// included, decided before anything of this channel is written.
		cur, rev, ok, err := store.Channel(ctx, ch.Name)
		if err != nil {
			return rep, err
		}
		after := maps.Clone(openIDs)
		for _, m := range cur.Members {
			after[m] = true
		}
		if len(after) > directory.MaxMembers {
			rep.ChannelsSkipped = append(rep.ChannelsSkipped, SkippedChannel{Name: ch.Name, Reason: tooMany})
			continue
		}
		var members, claims []string // claims: written only once the channel is
		for _, u := range humans {
			switch {
			case isOpenID(u):
				if _, err := store.Login(ctx, u.AuthData, u.Username, u.Username); err != nil {
					return rep, fmt.Errorf("identity of %s: %w", u.Username, err)
				}
				subs[u.AuthData] = true
				members = append(members, u.AuthData)
			case binding == "open":
				claims = append(claims, u.Username)
			default:
				skipAccount(u, "password account, name binding openid-only")
			}
		}
		slices.Sort(members)
		members = slices.Compact(members)
		// The claims of password accounts are written after the channel: a
		// channel left out (too big on a retry) gets none.
		writeClaims := func() error {
			for _, name := range claims {
				if err := store.AddPending(ctx, name, ch.Name); err != nil {
					return fmt.Errorf("pending %s: %w", name, err)
				}
				rep.PendingAdded++
			}
			return nil
		}

		if !ok {
			// the creator owns it only while still a member, human and an
			// OpenID account with a visible sub
			owner := ""
			if u, found := users[ch.CreatorID]; found && slices.Contains(ids, ch.CreatorID) &&
				human(u) == "" && u.AuthKnown && isOpenID(u) {
				owner = u.AuthData
			}
			c := directory.Channel{Name: ch.Name, Owner: owner, Public: ch.Type == "O", Members: members,
				Bindings: map[string]string{"mattermost": ch.Name}}
			_, err := store.PutChannel(ctx, c, 0)
			if err == nil {
				rep.ChannelsCreated = append(rep.ChannelsCreated, ch.Name)
				rep.MembersAdded += len(members)
				if err := writeClaims(); err != nil {
					return rep, err
				}
				continue
			}
			if !errors.Is(err, directory.ErrConflict) {
				return rep, fmt.Errorf("channel %s: %w", ch.Name, err)
			}
			if cur, rev, ok, err = store.Channel(ctx, ch.Name); err != nil || !ok {
				return rep, fmt.Errorf("channel %s: changed meanwhile: %w", ch.Name, errors.Join(err, directory.ErrConflict))
			}
		}
		beforeUnion(ch.Name)
		added, err := unionMembers(ctx, store, cur, rev, members)
		if errors.Is(err, errTooBig) {
			rep.ChannelsSkipped = append(rep.ChannelsSkipped, SkippedChannel{Name: ch.Name, Reason: tooMany})
			continue
		}
		if err != nil {
			return rep, fmt.Errorf("channel %s: %w", ch.Name, err)
		}
		if added > 0 {
			rep.ChannelsUpdated = append(rep.ChannelsUpdated, ch.Name)
			rep.MembersAdded += added
		}
		if err := writeClaims(); err != nil {
			return rep, err
		}
	}
	rep.Identities = len(subs)
	if err := store.MarkImported(ctx, now); err != nil {
		if errors.Is(err, directory.ErrConflict) {
			return Report{}, ErrAlreadyImported
		}
		return rep, err
	}
	return rep, nil
}

// unionMembers adds the missing members to an existing channel, keeping
// everything else; it returns how many were new.
func unionMembers(ctx context.Context, store *directory.Store, cur directory.Channel, rev uint64, members []string) (int, error) {
	for range 5 {
		var add []string
		for _, m := range members {
			if !slices.Contains(cur.Members, m) {
				add = append(add, m)
			}
		}
		if len(add) == 0 {
			return 0, nil
		}
		if len(cur.Members)+len(add) > directory.MaxMembers {
			return 0, errTooBig
		}
		next := cur
		next.Members = append(slices.Clone(cur.Members), add...)
		slices.Sort(next.Members)
		_, err := store.PutChannel(ctx, next, rev)
		if err == nil {
			return len(add), nil
		}
		if !errors.Is(err, directory.ErrConflict) {
			return 0, err
		}
		var ok bool
		if cur, rev, ok, err = store.Channel(ctx, cur.Name); err != nil || !ok {
			return 0, errors.Join(err, directory.ErrConflict)
		}
	}
	return 0, directory.ErrConflict
}
