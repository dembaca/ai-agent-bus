// Command authd is the login service: agents log in with Keycloak's device
// flow and receive short-lived NATS credentials from it (ADR-014,
// docs/arc42/09-architecture-decisions.md).
//
// The skeleton starts, reads its configuration, serves a health probe and
// stops on SIGINT or SIGTERM. Token checks and credential issuing come with
// the following pull requests.
package main

import (
	"context"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/config"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/httpserver"
)

// Exit codes. 0 is a clean stop; 2 is a configuration the service refuses to
// start with; 1 is anything else.
const (
	exitOK          = 0
	exitFailure     = 1
	exitConfigError = 2
)

func main() {
	os.Exit(run(os.Getenv))
}

// run is main with the environment injected, so the configuration path is
// testable without touching the process environment.
func run(getenv func(string) string) int {
	// JSON from the first line on, a refused configuration included: one
	// format for everything this program writes.
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))

	cfg, err := config.Load(getenv)
	if err != nil {
		logger.Error("configuration", "err", err.Error())
		return exitConfigError
	}

	// The level is known only now; a fresh handler keeps the first line above
	// at its default level, which is the one an operator wants to see.
	logger = slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level: cfg.LogLevel,
	}))
	logger.Info("starting", "service", "authd", "version", httpserver.Version)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	// Release the handler as soon as the stop begins, not when it ends: a
	// second signal during a slow stop then ends the program the usual way
	// instead of being swallowed, which is what an operator expects of
	// "press again to force".
	go func() {
		<-ctx.Done()
		stop()
	}()

	// Bind before announcing readiness: a taken port is a start failure, not
	// a line saying the service is up.
	ln, err := httpserver.Bind(cfg.ListenAddr)
	if err != nil {
		stop()
		logger.Error("bind", "addr", cfg.ListenAddr, "err", err.Error())
		return exitFailure
	}

	srv := httpserver.New(httpserver.Config{Logger: logger})

	if err := httpserver.Run(ctx, srv, ln, cfg.ShutdownTimeout, logger); err != nil {
		logger.Error("server", "err", err.Error())
		return exitFailure
	}

	logger.Info("stopped")
	return exitOK
}
