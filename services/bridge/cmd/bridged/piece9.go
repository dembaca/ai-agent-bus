package main

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/agentbots"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/config"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/header"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mm"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/presence"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/requests"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/status"
)

// piece9 is plan B piece 9 (ADR-014 phase 5; RT-6.6 steps 1 and 7, RT-6.38
// step 3, RT-6.44 step 9): in a home where its switch is on, each agent's own
// bot shows the status, presence and crown its session no longer writes to
// Mattermost, and the crown holder's header line is written with its own bot.
// Only with the agents' own bots (CB_BRIDGE_MM_ADMIN_TOKEN_FILE): the bridge
// never shows an agent's status or writes its header under the bridge bot.
// The crown claims come through the bridge's own consumer, bridge_crown, made
// at every start.
type piece9 struct {
	homes    []string
	switches *presence.Switches
	claims   *presence.Claims
	shown    *presence.ShownStore
	mirror   *status.Mirror
	writer   *header.Writer
	log      *slog.Logger
	deliver  string
}

// newPiece9 is nil without the agents' own bots.
func newPiece9(cfg config.Config, log *slog.Logger, client *mm.Client, bots *agentbots.Bots,
	snapshot *presence.Snapshot, switches *presence.Switches) *piece9 {
	if bots == nil {
		return nil
	}
	p := &piece9{homes: cfg.Homes, switches: switches, claims: &presence.Claims{},
		shown: &presence.ShownStore{}, log: log, deliver: cfg.Deliver + ".crown"}
	own := ownBots{bots}
	p.mirror = &status.Mirror{Homes: cfg.Homes, Presence: snapshot, Shown: p.shown, Claims: p.claims,
		Switches: switches, Bots: statusBots{own}, Log: log}
	ids := map[string]func(context.Context) (string, error){}
	for _, home := range cfg.Homes {
		ids[home] = channelOnce(cfg.MMTeam, home, client)
	}
	p.writer = &header.Writer{Homes: cfg.Homes, Claims: p.claims, Presence: snapshot, Switches: switches,
		Bots: headerBots{own}, ChannelID: func(ctx context.Context, home string) (string, error) {
			id, ok := ids[home]
			if !ok {
				return "", fmt.Errorf("the bridge serves no home %q", home)
			}
			return id(ctx)
		}}
	return p
}

// handlers are the request kinds piece 9 answers: the header line; none
// without the agents' own bots ("not built"). The map is never nil: the
// caller adds the other pieces' kinds to it, and a nil map panics on that.
func (p *piece9) handlers() map[string]requests.Piece {
	if p == nil {
		return map[string]requests.Piece{}
	}
	return map[string]requests.Piece{"header": p.writer.Piece}
}

// shownStore is where the presence reader keeps each presence's status; nil
// reads none.
func (p *piece9) shownStore() *presence.ShownStore {
	if p == nil {
		return nil
	}
	return p.shown
}

// switchWait is how often run looks whether a served home has piece 9 on yet.
const switchWait = 5 * time.Second

// on says whether a home the bridge serves has piece 9 on.
func (p *piece9) on() bool {
	for _, home := range p.homes {
		if p.switches.On(status.Piece, home) {
			return true
		}
	}
	return false
}

// run reads the crown claims and keeps the bots' statuses until ctx ends. The
// claims' consumer is made only once a served home has piece 9 on, so a
// bridge whose NATS user lacks its right logs nothing while the piece is off.
func (p *piece9) run(ctx context.Context, wg *sync.WaitGroup, nc *nats.Conn) {
	if p == nil {
		return
	}
	cr := &presence.ClaimsReader{Log: p.log, Deliver: p.deliver, Stream: "PRESENCE", Into: p.claims}
	wg.Add(2)
	go func() {
		defer wg.Done()
		t := time.NewTicker(switchWait)
		defer t.Stop()
		for !p.on() {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
			}
		}
		p.log.Info("bridge: plan B piece 9 is on in a served home; reading the crown claims")
		if err := cr.Run(ctx, nc); err != nil && ctx.Err() == nil {
			p.log.Error("bridge: the crown claims stopped", "error", err)
		}
	}()
	go func() {
		defer wg.Done()
		p.mirror.Run(ctx, status.Interval)
	}()
}

// ownBots gives an agent's own bot, never the bridge bot.
type ownBots struct{ b *agentbots.Bots }

func (o ownBots) own(ctx context.Context, name, home string) *mm.Client {
	c, _ := o.b.Bot(ctx, name, home).(*mm.Client)
	return c
}

type statusBots struct{ ownBots }

func (s statusBots) Bot(ctx context.Context, name, home string) status.Bot {
	if c := s.own(ctx, name, home); c != nil {
		return c
	}
	return nil
}

type headerBots struct{ ownBots }

func (h headerBots) Bot(ctx context.Context, name, home string) header.Bot {
	if c := h.own(ctx, name, home); c != nil {
		return c
	}
	return nil
}
