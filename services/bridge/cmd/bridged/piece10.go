package main

import (
	"context"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/agentbots"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mm"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/permission"
)

// permissionBots gives an agent's own bot to the permission DMs (plan B piece
// 10, RT-6.13), nil while it has none: the bridge bot posts then, with a line
// that names the agent. A nil *agentbots.Bots gives none.
type permissionBots struct{ b *agentbots.Bots }

func (p permissionBots) Bot(ctx context.Context, agent, home string) permission.Poster {
	if p.b == nil {
		return nil
	}
	if c := (ownBots{p.b}).own(ctx, agent, home); c != nil {
		return c
	}
	return nil
}

var _ permission.Poster = (*mm.Client)(nil)
