package app

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"fmt"
	"io"
	"log/slog"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nkeys"
)

// tlsServer starts a NATS server on 127.0.0.1 with a self-signed certificate
// and the given handshake_first setting; it returns its tls:// URL and the
// certificate file a client trusts.
func tlsServer(t *testing.T, first bool, fallback time.Duration, user nkeys.KeyPair) (string, string) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "test"},
		NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(time.Hour),
		KeyUsage:    x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		IPAddresses: []net.IP{net.ParseIP("127.0.0.1")}, IsCA: true, BasicConstraintsValid: true,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	certFile := filepath.Join(t.TempDir(), "cert.pem")
	if err := os.WriteFile(certFile, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0o600); err != nil {
		t.Fatal(err)
	}
	pub, _ := user.PublicKey()
	opts := &server.Options{
		Host: "127.0.0.1", Port: -1, NoLog: true, NoSigs: true,
		TLSConfig: &tls.Config{Certificates: []tls.Certificate{{Certificate: [][]byte{der}, PrivateKey: key}},
			MinVersion: tls.VersionTLS12},
		TLSTimeout: 2, TLSHandshakeFirst: first, TLSHandshakeFirstFallback: fallback,
		Nkeys: []*server.NkeyUser{{Nkey: pub}},
	}
	srv, err := server.NewServer(opts)
	if err != nil {
		t.Fatal(err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(5 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(srv.Shutdown)
	return fmt.Sprintf("tls://127.0.0.1:%d", srv.Addr().(*net.TCPAddr).Port), certFile
}

// The login service speaks TLS first to a tls:// server (RT-6.35), as the
// agents' hosts do: it connects to a server with handshake_first and no
// fallback, which a client in the classic order cannot reach.
func TestTheLoginServiceConnectsTLSFirst(t *testing.T) {
	user, _ := nkeys.CreateUser()
	quiet := slog.New(slog.NewTextHandler(io.Discard, nil))
	connect := func(url, ca string) (*nats.Conn, error) {
		pub, _ := user.PublicKey()
		return nats.Connect(url, nats.RootCAs(ca), nats.Nkey(pub, user.Sign),
			nats.Timeout(2*time.Second), nats.MaxReconnects(0))
	}

	url, ca := tlsServer(t, true, 0, user)
	nc, err := nats.Connect(url, connectOptions(Settings{NATSCAFile: ca}, url, "authd", user, quiet)...)
	if err != nil {
		t.Fatalf("the login service cannot reach a handshake_first server: %v", err)
	}
	nc.Close()
	if nc, err := connect(url, ca); err == nil {
		nc.Close()
		t.Fatal("a classic client reached a handshake_first server without fallback: the test proves nothing")
	}

	// The transition: handshake_first with a fallback ("300ms") lets both in.
	url, ca = tlsServer(t, true, 300*time.Millisecond, user)
	if nc, err := nats.Connect(url, connectOptions(Settings{NATSCAFile: ca}, url, "authd", user, quiet)...); err != nil {
		t.Errorf("the login service, during the transition: %v", err)
	} else {
		nc.Close()
	}
	if nc, err := connect(url, ca); err != nil {
		t.Errorf("a classic client, during the transition: %v", err)
	} else {
		nc.Close()
	}
}

func TestTLSFirstOnlyForAnAllTLSList(t *testing.T) {
	for urls, want := range map[string]bool{
		"tls://nats.example:4222":                    true,
		"tls://a.example:4222, tls://b.example:4222": true,
		"nats://127.0.0.1:4222":                      false,
		"tls://a.example:4222,nats://127.0.0.1:4222": false,
		"wss://nats.example:443":                     false,
	} {
		if got := tlsFirst(urls); got != want {
			t.Errorf("tlsFirst(%q) = %v, want %v", urls, got, want)
		}
	}
}
