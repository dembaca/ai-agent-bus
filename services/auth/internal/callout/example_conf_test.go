package callout

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
	"github.com/nats-io/nkeys"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/consumers"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/directory"
)

const exampleConf = "../../nats-server.example.conf"

// selfSigned writes a certificate for 127.0.0.1 and its key, for the example
// configuration's tls block.
func selfSigned(t *testing.T) (certFile, keyFile string) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "example"},
		NotBefore:             time.Now().Add(-time.Minute),
		NotAfter:              time.Now().Add(time.Hour),
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		IPAddresses:           []net.IP{net.ParseIP("127.0.0.1")},
		IsCA:                  true,
		BasicConstraintsValid: true,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	certFile, keyFile = filepath.Join(dir, "cert.pem"), filepath.Join(dir, "key.pem")
	if err := os.WriteFile(certFile, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(keyFile, pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER}), 0o600); err != nil {
		t.Fatal(err)
	}
	return certFile, keyFile
}

// The example configuration in services/auth carries no key, and with its
// placeholders filled a server starts from it: the callout is set as the
// README says, and the JetStream user has exactly the documented minimal
// rights and can make the streams with them.
func TestTheExampleServerConfiguration(t *testing.T) {
	raw, err := os.ReadFile(exampleConf)
	if err != nil {
		t.Fatal(err)
	}
	text := string(raw)
	// No nkey (public or seed) and nothing that looks like a password.
	if m := regexp.MustCompile(`\b[A-Z2-7]{56,}\b`).FindString(text); m != "" {
		t.Fatalf("the example holds a key: %s", m)
	}
	if regexp.MustCompile(`(?i)\b(password|token|seed)\s*[:=]`).MatchString(text) {
		t.Fatal("the example sets a password, token or seed")
	}

	issuer, _ := nkeys.CreateAccount()
	issuerPub, _ := issuer.PublicKey()
	xkey, _ := nkeys.CreateCurveKeys()
	xkeyPub, _ := xkey.PublicKey()
	svcUser, _ := nkeys.CreateUser()
	svcPub, _ := svcUser.PublicKey()
	jsUser, _ := nkeys.CreateUser()
	jsPub, _ := jsUser.PublicKey()
	bridgeUser, _ := nkeys.CreateUser()
	bridgePub, _ := bridgeUser.PublicKey()
	certFile, keyFile := selfSigned(t)
	for placeholder, value := range map[string]string{
		"<ISSUER_ACCOUNT_PUBLIC_KEY>": issuerPub,
		"<XKEY_PUBLIC_KEY>":           xkeyPub,
		"<SERVICE_USER_PUBLIC_NKEY>":  svcPub,
		"<JS_USER_PUBLIC_NKEY>":       jsPub,
		"<BRIDGE_USER_PUBLIC_NKEY>":   bridgePub,
		"<HOME>":                      "home",
		"<PATH_TO_SERVER_CERT>":       certFile,
		"<PATH_TO_SERVER_KEY>":        keyFile,
		"<PATH_TO_CLIENT_CA>":         certFile,
		"<JETSTREAM_STORE_DIR>":       t.TempDir(),
	} {
		if !strings.Contains(text, placeholder) {
			t.Errorf("the example has no %s", placeholder)
		}
		text = strings.ReplaceAll(text, placeholder, value)
	}
	if m := regexp.MustCompile(`<[A-Z_]+>`).FindString(text); m != "" {
		t.Fatalf("a placeholder this test does not know: %s", m)
	}
	conf := filepath.Join(t.TempDir(), "nats.conf")
	if err := os.WriteFile(conf, []byte(text), 0o600); err != nil {
		t.Fatal(err)
	}
	opts, err := server.ProcessConfigFile(conf)
	if err != nil {
		t.Fatalf("the example does not parse: %v", err)
	}

	co := opts.AuthCallout
	if co == nil {
		t.Fatal("no auth_callout")
	}
	if co.Issuer != issuerPub || co.Account != "AUTH" || co.XKey != xkeyPub {
		t.Errorf("auth_callout: %+v", co)
	}
	if want := []string{svcPub, jsPub, bridgePub}; !slices.Equal(co.AuthUsers, want) {
		t.Errorf("auth_users %v, want the login service's two users and the bridge's", co.AuthUsers)
	}
	if opts.TLSConfig == nil {
		t.Error("no tls")
	}
	if !opts.TLSHandshakeFirst || opts.TLSHandshakeFirstFallback != 0 {
		t.Errorf("tls: handshake_first %v (fallback %v), want true without fallback",
			opts.TLSHandshakeFirst, opts.TLSHandshakeFirstFallback)
	}
	if len(opts.Users) != 0 {
		t.Errorf("%d users with a password", len(opts.Users))
	}
	if len(opts.Nkeys) != 3 {
		t.Fatalf("%d nkey users, want the login service's two and the bridge's", len(opts.Nkeys))
	}
	for _, u := range opts.Nkeys {
		switch u.Nkey {
		case svcPub:
			if u.Account == nil || u.Account.Name != "AUTH" {
				t.Errorf("the callout's user is not in AUTH: %+v", u.Account)
			}
		case bridgePub:
			if u.Account == nil || u.Account.Name != "BUS" {
				t.Errorf("the bridge's user is not in BUS: %+v", u.Account)
			}
			// Read-only on the bus, its own consumers and its bucket, which the
			// operator makes: it may create no stream (a source would read what
			// it must not), and no consumer but its own.
			pub := strings.Join(u.Permissions.Publish.Allow, " ")
			for _, want := range []string{"$JS.ACK.BUS.bridge_home.>",
				"$JS.ACK.PRESENCE.bridge_presence.>", "$KV.BUS_BRIDGE.>",
				"$JS.API.STREAM.MSG.GET.KV_BUS_BRIDGE",
				// people onto NATS (RT-6.42)
				"bus.hu.home.*", "presence.bridge.home.v1",
				"$JS.API.CONSUMER.CREATE.PRESENCE.bridge_accounts.presence.account.mattermost.*.v1",
				"$KV.BUS_BRIDGE_SEEN.>",
				"$JS.API.STREAM.INFO.KV_BUS_BRIDGE_SEEN", "$JS.API.STREAM.MSG.GET.KV_BUS_BRIDGE_SEEN"} {
				if !strings.Contains(pub, want) {
					t.Errorf("the bridge's rights lack %s: %+v", want, u.Permissions.Publish.Allow)
				}
			}
			// no direct get (its answer carries a key's value onto a reply
			// subject the server does not check), no stream create, and no
			// stream info (its subjects filter lists who writes DMs to whom)
			for _, unwanted := range []string{"bus.>", "$JS.API.STREAM.CREATE",
				"DIRECT.GET", "$JS.API.STREAM.INFO.BUS", "$JS.API.STREAM.INFO.PRESENCE",
				"$JS.API.CONSUMER.DELETE", "$JS.API.CONSUMER.MSG.NEXT",
				"$JS.API.STREAM.MSG.GET.PRESENCE"} {
				if strings.Contains(pub, unwanted) {
					t.Errorf("the bridge may %s: %+v", unwanted, u.Permissions.Publish.Allow)
				}
			}
			// one consumer create, its filter fixed in the subject (RT-6.42)
			for _, subject := range u.Permissions.Publish.Allow {
				if strings.HasPrefix(subject, "$JS.API.CONSUMER.CREATE") &&
					subject != "$JS.API.CONSUMER.CREATE.PRESENCE.bridge_accounts.presence.account.mattermost.*.v1" {
					t.Errorf("the bridge may create consumers by %s", subject)
				}
			}
			sub := strings.Join(u.Permissions.Subscribe.Allow, " ")
			if !strings.Contains(sub, "_INBOX_bridge.>") || !strings.Contains(sub, "deliver.bridge.>") ||
				strings.Contains(sub, "_INBOX.>") ||
				strings.Contains(sub, "bus.>") || strings.Contains(sub, "presence.>") {
				t.Errorf("the bridge subscribes to %q", sub)
			}
		case jsPub:
			if u.Account == nil || u.Account.Name != "BUS" {
				t.Errorf("the JetStream user is not in BUS: %+v", u.Account)
			}
			// The rights services/auth/README.md documents, and the ones
			// TestTheDocumentedMinimalRightsSuffice runs the manager with.
			var pub []string
			for _, s := range []string{consumers.BusStream, consumers.PresenceStream} {
				pub = append(pub, "$JS.API.STREAM.CREATE."+s, "$JS.API.STREAM.INFO."+s,
					"$JS.API.CONSUMER.*."+s, "$JS.API.CONSUMER.*."+s+".>")
			}
			kv := "KV_" + directory.DefaultBucket
			pub = append(pub, "$JS.API.INFO", "$JS.API.STREAM.CREATE."+kv, "$JS.API.STREAM.INFO."+kv,
				"$JS.API.DIRECT.GET."+kv+".>", "$KV."+directory.DefaultBucket+".>", "$JS.API.STREAM.MSG.GET.PRESENCE", "presence.attest.*.v1", "presence.account.mattermost.*.v1")
			p := u.Permissions
			if p == nil || p.Publish == nil || p.Subscribe == nil {
				t.Fatalf("the JetStream user has no permissions: %+v", p)
			}
			if !slices.Equal(p.Publish.Allow, pub) || len(p.Publish.Deny) != 0 {
				t.Errorf("publish %v (deny %v), want %v", p.Publish.Allow, p.Publish.Deny, pub)
			}
			if !slices.Equal(p.Subscribe.Allow, []string{"_INBOX.>"}) || len(p.Subscribe.Deny) != 0 {
				t.Errorf("subscribe %v (deny %v)", p.Subscribe.Allow, p.Subscribe.Deny)
			}
		default:
			t.Errorf("an unexpected user %s", u.Nkey)
		}
	}

	// A server takes it: the callout's account and users exist, JetStream is
	// on in the tenant's account only, and the JetStream user makes both
	// streams over TLS.
	opts.Host, opts.Port = "127.0.0.1", -1
	opts.NoLog, opts.NoSigs = true, true
	srv, err := server.NewServer(opts)
	if err != nil {
		t.Fatalf("the server refuses the example: %v", err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(5 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(srv.Shutdown)
	for name, want := range map[string]bool{"BUS": true, "AUTH": false} {
		acc, err := srv.LookupAccount(name)
		if err != nil {
			t.Fatalf("account %s: %v", name, err)
		}
		if acc.JetStreamEnabled() != want {
			t.Errorf("JetStream in %s: %v, want %v", name, acc.JetStreamEnabled(), want)
		}
	}
	nc, err := nats.Connect(srv.ClientURL(), nats.RootCAs(certFile), nats.TLSHandshakeFirst(),
		nats.Nkey(jsPub, func(nonce []byte) ([]byte, error) { return jsUser.Sign(nonce) }))
	if err != nil {
		t.Fatalf("the JetStream user cannot connect: %v", err)
	}
	t.Cleanup(nc.Close)
	logs := &syncBuffer{}
	mgr, err := consumers.New(nc, consumers.Config{Bus: "bus", Presence: "presence", Log: slogTo(logs)})
	if err != nil {
		t.Fatal(err)
	}
	if err := mgr.EnsureStreams(ctx5(t)); err != nil {
		t.Fatalf("EnsureStreams: %v", err)
	}
	// The channel directory (ADR-016): the same user makes its bucket, opens
	// it again, writes and reads it with exactly these rights.
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	for range 2 { // made, then found
		dir, err := directory.Open(ctx5(t), js, "", false)
		if err != nil {
			t.Fatalf("directory: %v", err)
		}
		if id, err := (directory.Anchors{Store: dir}).Anchor(ctx5(t), "0b6f3c1e-sub", "alice"); err != nil || id != "alice" {
			t.Fatalf("anchor %q, %v", id, err)
		}
	}
}
