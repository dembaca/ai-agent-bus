package permission

import (
	"context"
	"fmt"
	"regexp"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/inbound"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mm"
)

// Run polls the open prompts' DMs until ctx ends. One prompt's failure is
// logged (throttled) and retried at its next poll; it never stops the loop.
func (h *Handler) Run(ctx context.Context) {
	tick := h.Tick
	if tick == 0 {
		tick = time.Second
	}
	t := time.NewTicker(tick)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			h.Poll(ctx)
		}
	}
}

// interval is how long after a poll the next one is due: FastEvery in the
// prompt's first FastFor, then SlowEvery.
func interval(opened, now time.Time) time.Duration {
	if now.Sub(opened) < FastFor {
		return FastEvery
	}
	return SlowEvery
}

type due struct {
	k key
	p *prompt
}

// Poll polls each prompt that is due, once.
func (h *Handler) Poll(ctx context.Context) {
	now := h.now()
	h.mu.Lock()
	h.expireLocked()
	var list []due
	for k, p := range h.open {
		if p.pending || now.Before(p.next) {
			continue
		}
		p.next = now.Add(interval(p.opened, now))
		list = append(list, due{k, p})
	}
	h.mu.Unlock()
	for _, d := range list {
		if ctx.Err() != nil {
			return
		}
		if err := h.pollOne(ctx, d.k, d.p); err != nil && ctx.Err() == nil {
			h.warn("poll "+d.k.agent+"/"+d.k.id, "permission: a prompt's DM was not carried; retrying",
				"agent", d.k.agent, "error", err)
		}
	}
}

func (h *Handler) isSeen(p *prompt, reaction bool, k string) bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	if reaction {
		return p.seenReactions[k]
	}
	return p.seenPosts[k]
}

func (h *Handler) markSeen(p *prompt, reaction bool, k string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if reaction {
		p.seenReactions[k] = true
	} else {
		p.seenPosts[k] = true
	}
}

// answerRE is a top-level answer for the prompt id: "yes <id>" or "no <id>".
func answerRE(id string) *regexp.Regexp {
	return regexp.MustCompile(`(?i)^\s*(y|yes|n|no)\s+` + regexp.QuoteMeta(id) + `\s*$`)
}

func (h *Handler) pollOne(ctx context.Context, k key, p *prompt) error {
	posts, err := p.poster.PostsSince(ctx, p.dm, p.createAt)
	if err != nil {
		return err
	}
	re := answerRE(k.id)
	subject := h.BusPrefix + ".hu.dm." + k.agent + "." + inbound.SenderToken(p.anchor)
	var firstErr error
	reactions := func(post mm.Post) {
		for _, r := range post.Reactions {
			if r.UserID != p.account {
				continue
			}
			ek := r.UserID + ":" + r.EmojiName
			if h.isSeen(p, true, ek) {
				continue
			}
			if err := h.carryOne(ctx, k, p, subject, "reaction", p.post, func() ([]byte, error) {
				return inbound.EncodeReaction(inbound.ReactionBody{Emoji: r.EmojiName, Name: p.name, MM: p.post}, true)
			}, true, ek); err != nil && firstErr == nil {
				firstErr = err
			}
		}
	}
	promptSeen := false
	for _, post := range posts {
		switch {
		case post.ID == p.post:
			promptSeen = true
			reactions(post)
			continue
		case post.UserID != p.account || post.CreateAt <= p.createAt || post.DeleteAt != 0 || post.Type != "" ||
			isWebhook(post) || h.isSeen(p, false, post.ID):
			continue
		case post.RootID == p.post || (post.RootID == "" && re.MatchString(post.Message)):
		default:
			h.Log.Debug("permission: a post in the DM is no answer; left in Mattermost", "agent", k.agent)
			continue
		}
		if err := h.carryOne(ctx, k, p, subject, "post", post.ID, func() ([]byte, error) {
			return inbound.EncodePost(inbound.PostBody{Text: post.Message, Name: p.name, MM: post.ID,
				MMRoot: post.RootID, Files: post.FileCount})
		}, false, post.ID); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	if !promptSeen {
		// Mattermost moves a post's update_at when it gets a reaction, so the since read
		// holds the prompt then; should a server not, its reactions are read from the post.
		post, err := p.poster.PostByID(ctx, p.post)
		if err != nil {
			if firstErr == nil {
				firstErr = err
			}
		} else {
			reactions(post)
		}
	}
	return firstErr
}

func isWebhook(p mm.Post) bool {
	_, a := p.Props["from_webhook"]
	_, b := p.Props["override_username"]
	return a || b
}

// carryOne publishes one answer, once its account still binds the prompt's
// anchor, and marks it carried only after the publish succeeded.
func (h *Handler) carryOne(ctx context.Context, k key, p *prompt, subject, what, mmID string,
	encode func() ([]byte, error), reaction bool, seenKey string) error {
	anchor, err := h.anchorOf(ctx, p.home, p.account, true)
	if err != nil {
		return fmt.Errorf("the account was not read: %w", err)
	}
	if anchor != p.anchor {
		h.mu.Lock()
		first := !p.warnedAnchor
		p.warnedAnchor = true
		h.mu.Unlock()
		if first {
			h.Log.Warn("permission: the account no longer binds the prompt's person; not carried",
				"agent", k.agent)
		}
		return nil
	}
	data, err := encode()
	if err != nil {
		return err
	}
	if err := h.Publisher.Publish(subject, data); err != nil {
		return err
	}
	h.markSeen(p, reaction, seenKey)
	h.Log.Info("permission: an answer was carried", "agent", k.agent, "what", what,
		"post", mmID, "subject", subject)
	return nil
}
