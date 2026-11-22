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
	cfg, err := config.Load(getenv)
	if err != nil {
		// No logger yet: the configuration may be what is wrong.
		slog.Error("configuration", "err", err)
		return exitConfigError
	}

	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level: cfg.LogLevel,
	}))
	logger.Info("starting", "service", "authd", "version", httpserver.Version)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	srv := httpserver.New(httpserver.Config{
		ListenAddr: cfg.ListenAddr,
		Logger:     logger,
	})

	if err := httpserver.Run(ctx, srv, cfg.ShutdownTimeout, logger); err != nil {
		logger.Error("server", "err", err)
		return exitFailure
	}

	logger.Info("stopped")
	return exitOK
}
