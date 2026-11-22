package httpserver

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func quietLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// testClient talks to the server one connection per request.
//
// It matters for the shutdown test: the default transport keeps idle
// connections pooled, and an idle keep-alive connection is an open connection
// that http.Server.Shutdown waits for. A pooled spare would make the stop look
// like it hangs, when the server is behaving correctly.
func testClient() *http.Client {
	return &http.Client{Transport: &http.Transport{DisableKeepAlives: true}}
}

// waitForServer polls until the address answers, so the tests do not race the
// goroutine that serves.
func waitForServer(t *testing.T, client *http.Client, addr string) {
	t.Helper()
	for i := 0; i < 200; i++ {
		resp, err := client.Get("http://" + addr + "/healthz")
		if err == nil {
			_, _ = io.ReadAll(resp.Body)
			_ = resp.Body.Close()
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("server at %s never came up", addr)
}

func TestHealthz(t *testing.T) {
	srv := New(Config{ListenAddr: ":0", Logger: quietLogger()})
	rec := httptest.NewRecorder()

	srv.Handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if got := rec.Header().Get("Content-Type"); got != "application/json" {
		t.Errorf("Content-Type = %q", got)
	}
	if got := rec.Header().Get("Cache-Control"); got != "no-store" {
		t.Errorf("Cache-Control = %q, want no-store", got)
	}

	var body healthResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body is not JSON: %v", err)
	}
	if body.Status != "ok" || body.Service != "authd" {
		t.Errorf("body = %+v", body)
	}
	if body.Version == "" {
		t.Error("version is empty")
	}
}

func TestHealthzAnswersHead(t *testing.T) {
	// The GET pattern matches HEAD too. That the body is stripped is the
	// server's job, not the handler's, so it is checked through a real
	// listener in TestRunServesAndStops.
	srv := New(Config{ListenAddr: ":0", Logger: quietLogger()})
	rec := httptest.NewRecorder()

	srv.Handler.ServeHTTP(rec, httptest.NewRequest(http.MethodHead, "/healthz", nil))

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 for HEAD", rec.Code)
	}
}

func TestHealthzRejectsOtherMethods(t *testing.T) {
	srv := New(Config{ListenAddr: ":0", Logger: quietLogger()})

	for _, method := range []string{http.MethodPost, http.MethodPut, http.MethodDelete, http.MethodPatch} {
		t.Run(method, func(t *testing.T) {
			rec := httptest.NewRecorder()
			srv.Handler.ServeHTTP(rec, httptest.NewRequest(method, "/healthz", nil))
			if rec.Code != http.StatusMethodNotAllowed {
				t.Errorf("status = %d, want 405", rec.Code)
			}
		})
	}
}

func TestUnknownPathIsNotFound(t *testing.T) {
	srv := New(Config{ListenAddr: ":0", Logger: quietLogger()})

	for _, path := range []string{"/", "/health", "/healthz/", "/readyz", "/api/v1/nats"} {
		t.Run(path, func(t *testing.T) {
			rec := httptest.NewRecorder()
			srv.Handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
			if rec.Code != http.StatusNotFound {
				t.Errorf("status = %d, want 404", rec.Code)
			}
		})
	}
}

func TestNewToleratesNoLogger(t *testing.T) {
	// A nil logger must not panic: the server is also built in tests and,
	// later, in handlers that take no logger.
	srv := New(Config{ListenAddr: ":0"})
	if srv.ErrorLog == nil {
		t.Error("ErrorLog is nil, the server would log to the standard logger")
	}
}

// freeAddr reserves a port, releases it and returns the address. Run listens
// itself, and the test needs to know where.
func freeAddr(t *testing.T) string {
	t.Helper()
	probe, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("reserve port: %v", err)
	}
	addr := probe.Addr().String()
	if err := probe.Close(); err != nil {
		t.Fatalf("release port: %v", err)
	}
	return addr
}

func TestRunServesAndStops(t *testing.T) {
	addr := freeAddr(t)
	srv := New(Config{ListenAddr: addr, Logger: quietLogger()})
	client := testClient()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- Run(ctx, srv, 5*time.Second, quietLogger()) }()

	waitForServer(t, client, addr)

	resp, err := client.Get("http://" + addr + "/healthz")
	if err != nil {
		t.Fatalf("GET /healthz: %v", err)
	}
	_, _ = io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("status = %d, want 200", resp.StatusCode)
	}

	// A HEAD through a real server must come back without a body.
	head, err := client.Head("http://" + addr + "/healthz")
	if err != nil {
		t.Fatalf("HEAD /healthz: %v", err)
	}
	body, readErr := io.ReadAll(head.Body)
	_ = head.Body.Close()
	if readErr != nil {
		t.Errorf("reading HEAD body: %v", readErr)
	}
	if len(body) != 0 {
		t.Errorf("HEAD returned a body of %d bytes", len(body))
	}
	if head.StatusCode != http.StatusOK {
		t.Errorf("HEAD status = %d, want 200", head.StatusCode)
	}

	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Errorf("Run returned %v, want nil on a clean stop", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("Run did not return after the context was cancelled")
	}
}

func TestRunReportsBindFailure(t *testing.T) {
	// Hold the port so Run cannot bind it.
	taken, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer func() { _ = taken.Close() }()

	srv := New(Config{ListenAddr: taken.Addr().String(), Logger: quietLogger()})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	if err := Run(ctx, srv, time.Second, quietLogger()); err == nil {
		t.Fatal("Run accepted an address that is already in use")
	}
}

func TestRunStopsOnCancelledContextBeforeServing(t *testing.T) {
	// A stop requested up front must still produce a clean return, not a hang.
	addr := freeAddr(t)
	srv := New(Config{ListenAddr: addr, Logger: quietLogger()})

	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	done := make(chan error, 1)
	go func() { done <- Run(ctx, srv, time.Second, quietLogger()) }()

	select {
	case err := <-done:
		if err != nil {
			t.Errorf("Run returned %v, want nil", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("Run hung on an already cancelled context")
	}
}
