package httpserver

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

func quietLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// testClient talks to the server one connection per request.
//
// It matters for the shutdown tests: the default transport keeps idle
// connections pooled, and an idle keep-alive connection is an open connection
// that http.Server.Shutdown waits for. A pooled spare would make a stop look
// like it hangs, when the server is behaving correctly.
func testClient() *http.Client {
	return &http.Client{Transport: &http.Transport{DisableKeepAlives: true}}
}

// serve starts srv on a listener the test holds and returns the address and a
// stop function that cancels the context and waits for Run to return.
func serve(t *testing.T, timeout time.Duration) (addr string, stop func() error) {
	t.Helper()
	ln, err := Bind("127.0.0.1:0")
	if err != nil {
		t.Fatalf("Bind: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())

	ch := make(chan error, 1)
	go func() { ch <- Run(ctx, New(Config{Logger: quietLogger()}), ln, timeout, quietLogger()) }()

	var stopOnce sync.Once
	var result error
	stop = func() error {
		stopOnce.Do(func() {
			cancel()
			select {
			case result = <-ch:
			case <-time.After(10 * time.Second):
				result = errors.New("Run did not return after its context was cancelled")
			}
		})
		return result
	}
	t.Cleanup(func() { _ = stop() })
	return ln.Addr().String(), stop
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
	srv := New(Config{Logger: quietLogger()})
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
	srv := New(Config{Logger: quietLogger()})
	rec := httptest.NewRecorder()

	srv.Handler.ServeHTTP(rec, httptest.NewRequest(http.MethodHead, "/healthz", nil))

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 for HEAD", rec.Code)
	}
}

func TestHealthzRejectsOtherMethods(t *testing.T) {
	srv := New(Config{Logger: quietLogger()})

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
	srv := New(Config{Logger: quietLogger()})

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
	srv := New(Config{})
	if srv.ErrorLog == nil {
		t.Error("ErrorLog is nil, the server would log to the standard logger")
	}
}

func TestBindReportsTakenPort(t *testing.T) {
	taken, err := Bind("127.0.0.1:0")
	if err != nil {
		t.Fatalf("first bind: %v", err)
	}
	defer func() { _ = taken.Close() }()

	if _, err := Bind(taken.Addr().String()); err == nil {
		t.Fatal("Bind accepted an address that is already in use")
	}
}

func TestRunServesAndStops(t *testing.T) {
	addr, stop := serve(t, 5*time.Second)
	client := testClient()
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

	if err := stop(); err != nil {
		t.Errorf("Run returned %v, want nil on a clean stop", err)
	}
}

func TestRunStopsOnCancelledContextBeforeServing(t *testing.T) {
	// A stop requested up front must still produce a clean return, not a hang.
	ln, err := Bind("127.0.0.1:0")
	if err != nil {
		t.Fatalf("Bind: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	done := make(chan error, 1)
	go func() {
		done <- Run(ctx, New(Config{Logger: quietLogger()}), ln, time.Second, quietLogger())
	}()

	select {
	case err := <-done:
		if err != nil {
			t.Errorf("Run returned %v, want nil", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("Run hung on an already cancelled context")
	}
}

// TestRunCutsOffASlowHandler is finding 6: a request that outlives the
// shutdown timeout must be cut off and reported, not waited for forever.
func TestRunCutsOffASlowHandler(t *testing.T) {
	ln, err := Bind("127.0.0.1:0")
	if err != nil {
		t.Fatalf("Bind: %v", err)
	}

	release := make(chan struct{})
	t.Cleanup(func() { close(release) })

	srv := New(Config{Logger: quietLogger()})
	mux := http.NewServeMux()
	mux.HandleFunc("GET /slow", func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-release:
		case <-time.After(30 * time.Second):
		}
	})
	srv.Handler = mux

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- Run(ctx, srv, ln, 50*time.Millisecond, quietLogger()) }()

	client := testClient()
	go func() {
		resp, err := client.Get("http://" + ln.Addr().String() + "/slow")
		if err == nil {
			_, _ = io.ReadAll(resp.Body)
			_ = resp.Body.Close()
		}
	}()
	// Give the handler a moment to start, so Shutdown has something to wait for.
	time.Sleep(100 * time.Millisecond)

	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Errorf("Run returned %v, want context.DeadlineExceeded", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("Run never returned after the shutdown timeout")
	}
}

// TestRunClosesItsListenerOnReturn is finding 9: Run owns the listener and
// must close it, so a caller may Bind and Run without leaking a socket.
func TestRunClosesItsListenerOnReturn(t *testing.T) {
	ln, err := Bind("127.0.0.1:0")
	if err != nil {
		t.Fatalf("Bind: %v", err)
	}
	addr := ln.Addr().String()

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- Run(ctx, New(Config{Logger: quietLogger()}), ln, time.Second, quietLogger())
	}()

	client := testClient()
	waitForServer(t, client, addr)

	cancel()
	if err := <-done; err != nil {
		t.Fatalf("Run returned %v", err)
	}

	// The address must be free again, so a fresh Bind on it succeeds.
	fresh, err := Bind(addr)
	if err != nil {
		t.Fatalf("address still taken after Run returned: %v", err)
	}
	_ = fresh.Close()
}
