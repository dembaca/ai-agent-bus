package httpserver

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"time"
)

// Version is the reported service version. The build sets it with
// -ldflags "-X …/internal/httpserver.Version=…"; the skeleton reports
// "dev" until then.
var Version = "dev"

// New returns the service's HTTP server.
//
// It carries the health endpoint and nothing else; the auth-callout and
// device-flow handlers arrive with the pull requests that implement them.
func New(cfg Config) *http.Server {
	logger := cfg.Logger
	if logger == nil {
		logger = slog.Default()
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", healthz)

	return &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
		ErrorLog:          slog.NewLogLogger(logger.Handler(), slog.LevelWarn),
	}
}

// Config is what the server needs; it is smaller than config.Config on
// purpose, so this package does not depend on how the values are read.
type Config struct {
	ListenAddr string
	Logger     *slog.Logger
}

type healthResponse struct {
	Status  string `json:"status"`
	Service string `json:"service"`
	Version string `json:"version"`
}

// healthz is a liveness probe: the process is up and serving. It says nothing
// about Keycloak or NATS, which a readiness probe will cover once they exist.
func healthz(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(healthResponse{
		Status:  "ok",
		Service: "authd",
		Version: Version,
	})
}

// Run serves until ctx is cancelled, then stops gracefully and returns. A
// stop that outlives ShutdownTimeout is cut off and reported as an error.
func Run(ctx context.Context, srv *http.Server, shutdownTimeout time.Duration, logger *slog.Logger) error {
	errCh := make(chan error, 1)

	ln, err := net.Listen("tcp", srv.Addr)
	if err != nil {
		return err
	}

	go func() {
		logger.Info("listening", "addr", ln.Addr().String())
		errCh <- srv.Serve(ln)
	}()

	select {
	case err := <-errCh:
		// Serve returned before a stop was asked for: a real failure, not
		// the normal close path.
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	case <-ctx.Done():
	}

	logger.Info("stopping", "timeout", shutdownTimeout.String())
	shutdownCtx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
	defer cancel()

	if err := srv.Shutdown(shutdownCtx); err != nil {
		_ = srv.Close()
		return err
	}
	return nil
}
