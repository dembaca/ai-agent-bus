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

// Config is what the server needs besides its listener.
type Config struct {
	Logger *slog.Logger
}

// New returns the service's HTTP server.
//
// It carries the health endpoint and nothing else; the auth-callout and
// device-flow handlers arrive with the pull requests that implement them.
// The address is not part of the server: Run serves on the listener it is
// given, so there is one place that decides where the service listens.
func New(cfg Config) *http.Server {
	logger := cfg.Logger
	if logger == nil {
		logger = slog.Default()
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", healthz)

	return &http.Server{
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
		ErrorLog:          slog.NewLogLogger(logger.Handler(), slog.LevelWarn),
	}
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

// Bind opens the listener the service serves on. Splitting it from Run lets a
// caller report a taken port before it announces a start, and lets a test
// hand Run a listener it already holds, with no port race.
func Bind(addr string) (net.Listener, error) {
	return net.Listen("tcp", addr)
}

// Run serves on ln until ctx is cancelled, then stops gracefully and returns.
//
// A stop that outlives shutdownTimeout is cut off and returned as
// context.DeadlineExceeded. The listener is closed in every case, so a caller
// may Bind and Run without leaking a socket.
func Run(ctx context.Context, srv *http.Server, ln net.Listener, shutdownTimeout time.Duration, logger *slog.Logger) error {
	if logger == nil {
		logger = slog.Default()
	}

	errCh := make(chan error, 1)
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

	// Shutdown waits for in-flight requests and, when the deadline passes,
	// returns without closing them: the connection would stay open and the
	// caller would keep waiting. Close cuts them off, which is what the
	// deadline promises.
	if err := srv.Shutdown(shutdownCtx); err != nil {
		_ = srv.Close()
		return err
	}
	return nil
}
