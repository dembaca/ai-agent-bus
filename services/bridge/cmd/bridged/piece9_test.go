package main

import (
	"context"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/agentbots"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/config"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mm"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/presence"
)

func TestPiece9NeedsTheAgentsOwnBots(t *testing.T) {
	p := newPiece9(config.Config{Homes: []string{"home"}}, slog.New(slog.NewTextHandler(io.Discard, nil)),
		mm.New("http://127.0.0.1:1", "x"), nil, presence.New(), &presence.Switches{})
	if p != nil || len(p.handlers()) != 0 || p.shownStore() != nil {
		t.Fatal("piece 9 runs without the agents' own bots")
	}
	p.run(context.Background(), &sync.WaitGroup{}, nil) // nothing to run
}

// Without the agents' own bots (how the lab runs the bridge) the request
// wiring in run adds the permission, history and file pieces to what piece 9
// returns; that map must accept them (plan B request scaffold, ADR-014 phase 5;
// seen as "assignment to entry in nil map" on 0.105.0).
func TestPiece9HandlersAcceptOtherPiecesWithoutBots(t *testing.T) {
	h := (*piece9)(nil).handlers()
	if h == nil {
		t.Fatal("handlers is nil without the agents' own bots")
	}
	h["permission"] = nil
	h["history"] = nil
	h["file"] = nil
	if len(h) != 3 {
		t.Fatalf("handlers = %v", h)
	}
}

// While no served home has piece 9 on, run makes no consumer (a nil
// connection would panic) and logs nothing; it ends with its context.
func TestTheClaimsReaderWaitsForPiece9(t *testing.T) {
	sw := &presence.Switches{}
	p := newPiece9(config.Config{Homes: []string{"home", "other"}, Deliver: "deliver.bridge"},
		slog.New(slog.NewTextHandler(io.Discard, nil)), mm.New("http://127.0.0.1:1", "x"),
		&agentbots.Bots{}, presence.New(), sw)
	if p == nil || p.handlers()["header"] == nil || p.shownStore() == nil {
		t.Fatal("piece 9 is not wired with the agents' own bots")
	}
	if p.on() {
		t.Fatal("on without a record")
	}
	sw.Observed("other", []int{2, 4}, time.Now())
	if p.on() {
		t.Fatal("on without piece 9")
	}
	ctx, cancel := context.WithCancel(context.Background())
	var wg sync.WaitGroup
	p.run(ctx, &wg, nil)
	time.Sleep(50 * time.Millisecond)
	cancel()
	done := make(chan struct{})
	go func() { wg.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(15 * time.Second):
		t.Fatal("run did not end with its context")
	}
	sw.Observed("other", []int{9}, time.Now())
	if !p.on() {
		t.Fatal("piece 9 on in a served home reads off")
	}
}
