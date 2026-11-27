package callout

import (
	"context"
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
	"github.com/dembaca/ai-agent-bus/services/auth/internal/testrealm"
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

// exampleKeys are the keys and the certificate exampleConfig fills in.
type exampleKeys struct {
	issuer, xkey, svcUser, jsUser, bridgeUser, metricsUser                      nkeys.KeyPair
	issuerPub, xkeyPub, svcPub, jsPub, bridgePub, metricsPub, certFile, keyFile string
}

// exampleConfig fills the example's placeholders with keys made here and
// parses it as the server does.
func exampleConfig(t *testing.T) (*server.Options, exampleKeys) {
	t.Helper()
	raw, err := os.ReadFile(exampleConf)
	if err != nil {
		t.Fatal(err)
	}
	text := string(raw)
	var k exampleKeys
	k.issuer, _ = nkeys.CreateAccount()
	k.issuerPub, _ = k.issuer.PublicKey()
	k.xkey, _ = nkeys.CreateCurveKeys()
	k.xkeyPub, _ = k.xkey.PublicKey()
	k.svcUser, _ = nkeys.CreateUser()
	k.svcPub, _ = k.svcUser.PublicKey()
	k.jsUser, _ = nkeys.CreateUser()
	k.jsPub, _ = k.jsUser.PublicKey()
	k.bridgeUser, _ = nkeys.CreateUser()
	k.bridgePub, _ = k.bridgeUser.PublicKey()
	k.metricsUser, _ = nkeys.CreateUser()
	k.metricsPub, _ = k.metricsUser.PublicKey()
	k.certFile, k.keyFile = selfSigned(t)
	issuerPub, xkeyPub, svcPub, jsPub, bridgePub, metricsPub := k.issuerPub, k.xkeyPub, k.svcPub, k.jsPub, k.bridgePub, k.metricsPub
	certFile, keyFile := k.certFile, k.keyFile
	for placeholder, value := range map[string]string{
		"<ISSUER_ACCOUNT_PUBLIC_KEY>":   issuerPub,
		"<XKEY_PUBLIC_KEY>":             xkeyPub,
		"<SERVICE_USER_PUBLIC_NKEY>":    svcPub,
		"<JS_USER_PUBLIC_NKEY>":         jsPub,
		"<BRIDGE_USER_PUBLIC_NKEY>":     bridgePub,
		"<CONSOLE_METRICS_PUBLIC_NKEY>": metricsPub,
		"<HOME>":                        "home",
		"<PATH_TO_SERVER_CERT>":         certFile,
		"<PATH_TO_SERVER_KEY>":          keyFile,
		"<PATH_TO_CLIENT_CA>":           certFile,
		"<JETSTREAM_STORE_DIR>":         t.TempDir(),
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
	return opts, k
}

// restricts says whether a publish or subscribe rule limits its user: an
// allow list without ">", or a deny of every subject (">"). Without one the
// user may publish or subscribe to any subject of its account (#172); an
// allow list holding ">" allows everything, and a deny list of single
// subjects still allows all the rest. An empty publish allow list counts (the
// server then allows no subject, allow_responses aside), an empty subscribe
// allow list does not (the server then builds none).
func restricts(p *server.SubjectPermission, publish bool) bool {
	if p == nil {
		return false
	}
	if !slices.Contains(p.Allow, ">") && (len(p.Allow) > 0 || (publish && p.Allow != nil)) {
		return true
	}
	return slices.Contains(p.Deny, ">")
}

func TestRestricts(t *testing.T) {
	for _, c := range []struct {
		name    string
		p       *server.SubjectPermission
		publish bool
		want    bool
	}{
		{"no rule", nil, true, false},
		{"an allow list", &server.SubjectPermission{Allow: []string{"a.>"}}, true, true},
		{"an allow list with >", &server.SubjectPermission{Allow: []string{"a.>", ">"}}, true, false},
		{"an allow list with > on subscribe", &server.SubjectPermission{Allow: []string{">"}}, false, false},
		{"> allowed but denied", &server.SubjectPermission{Allow: []string{">"}, Deny: []string{">"}}, true, true},
		{"an empty publish allow list", &server.SubjectPermission{Allow: []string{}}, true, true},
		{"an empty subscribe allow list", &server.SubjectPermission{Allow: []string{}}, false, false},
		{"a deny of everything", &server.SubjectPermission{Deny: []string{">"}}, true, true},
		{"a deny of single subjects", &server.SubjectPermission{Deny: []string{"a.b"}}, false, false},
	} {
		if got := restricts(c.p, c.publish); got != c.want {
			t.Errorf("%s (publish %v): %v, want %v", c.name, c.publish, got, c.want)
		}
	}
}

// Every user of the example names what it may publish and what it may
// subscribe to (#172): a user without both may do the other on any subject of
// its account. It covers the nkey users and the users with a password of
// every account (opts.Nkeys, opts.Users) and the leaf node users
// (opts.LeafNode.Users). The single user or token of an authorization,
// leafnodes, websocket or mqtt block has no rules of its own to check, so
// the example may set none. A user added later fails here, not only the
// users the test below pins one by one.
func TestEveryExampleUserHasPublishAndSubscribeRules(t *testing.T) {
	opts, _ := exampleConfig(t)
	if len(opts.Nkeys)+len(opts.Users) == 0 {
		t.Fatal("the example has no users")
	}
	for block, set := range map[string]bool{
		"authorization": opts.Username != "" || opts.Authorization != "",
		"leafnodes":     opts.LeafNode.Username != "" || opts.LeafNode.Nkey != "",
		"websocket":     opts.Websocket.Username != "" || opts.Websocket.Token != "",
		"mqtt":          opts.MQTT.Username != "" || opts.MQTT.Token != "",
	} {
		if set {
			t.Errorf("the %s block sets a single user or token, which has no publish and subscribe rules", block)
		}
	}
	check := func(who string, acc *server.Account, p *server.Permissions) {
		where := "no account"
		if acc != nil {
			where = acc.Name
		}
		if p == nil {
			t.Errorf("%s (%s) has no permissions: it may publish and subscribe to anything", who, where)
			return
		}
		if !restricts(p.Publish, true) {
			t.Errorf("%s (%s) has no publish rule: it may publish anything (%+v)", who, where, p.Publish)
		}
		if !restricts(p.Subscribe, false) {
			t.Errorf("%s (%s) has no subscribe rule: it may subscribe to anything (%+v)", who, where, p.Subscribe)
		}
	}
	for _, u := range opts.Nkeys {
		check("nkey user "+u.Nkey, u.Account, u.Permissions)
	}
	for _, u := range opts.Users {
		check("user "+u.Username, u.Account, u.Permissions)
	}
	for _, u := range opts.LeafNode.Users {
		check("leaf node user "+u.Username, u.Account, u.Permissions)
	}
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
	opts, k := exampleConfig(t)
	issuerPub, xkeyPub, svcPub, jsPub, bridgePub, metricsPub := k.issuerPub, k.xkeyPub, k.svcPub, k.jsPub, k.bridgePub, k.metricsPub
	jsUser, certFile := k.jsUser, k.certFile

	co := opts.AuthCallout
	if co == nil {
		t.Fatal("no auth_callout")
	}
	if co.Issuer != issuerPub || co.Account != "AUTH" || co.XKey != xkeyPub {
		t.Errorf("auth_callout: %+v", co)
	}
	if want := []string{svcPub, jsPub, bridgePub, metricsPub}; !slices.Equal(co.AuthUsers, want) {
		t.Errorf("auth_users %v, want the login service's two users, the bridge's and the metrics service's", co.AuthUsers)
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
	if len(opts.Nkeys) != 4 {
		t.Fatalf("%d nkey users, want the login service's two, the bridge's and the metrics service's", len(opts.Nkeys))
	}
	for _, u := range opts.Nkeys {
		switch u.Nkey {
		case svcPub:
			if u.Account == nil || u.Account.Name != "AUTH" {
				t.Errorf("the callout's user is not in AUTH: %+v", u.Account)
			}
			// The requests, and one answer to each on the reply subject the
			// server set (allow_responses); nothing else (#172).
			p := u.Permissions
			if p == nil || p.Publish == nil || p.Subscribe == nil || p.Response == nil {
				t.Fatalf("the callout's user lacks rights: %+v", p)
			}
			if len(p.Publish.Allow) != 0 || !slices.Equal(p.Publish.Deny, []string{">"}) {
				t.Errorf("the callout's user publishes %v (deny %v), want nothing but answers",
					p.Publish.Allow, p.Publish.Deny)
			}
			if !slices.Equal(p.Subscribe.Allow, []string{Subject}) || len(p.Subscribe.Deny) != 0 {
				t.Errorf("the callout's user subscribes to %v (deny %v), want %s", p.Subscribe.Allow, p.Subscribe.Deny, Subject)
			}
			if want := (server.ResponsePermission{MaxMsgs: 1, Expires: 2 * time.Minute}); *p.Response != want {
				t.Errorf("the callout's user answers %+v, want %+v", *p.Response, want)
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
				"bus.hu.home.*", "presence.bridge.home.v1", "presence.bot.*.v1",
				"$JS.API.CONSUMER.CREATE.PRESENCE.bridge_accounts.presence.account.mattermost.*.v1",
				// plan B's switches per channel, its own consumer (RT-6.39 step 5)
				"$JS.API.CONSUMER.CREATE.PRESENCE.bridge_planb.presence.planb.*.v1",
				// the memberships the history read checks, its own consumer
				// (RT-6.37, RT-6.42 step 11)
				"$JS.API.CONSUMER.CREATE.PRESENCE.bridge_members.presence.member.*.*",
				"$KV.BUS_BRIDGE_SEEN.>",
				"$JS.API.STREAM.INFO.KV_BUS_BRIDGE_SEEN", "$JS.API.STREAM.MSG.GET.KV_BUS_BRIDGE_SEEN",
				// the bridge's own slash command records (issue #151 step 3),
				// which only it writes: no stream create, no direct get.
				"$JS.API.STREAM.INFO.KV_BUS_BRIDGE_RECORDS",
				"$JS.API.STREAM.MSG.GET.KV_BUS_BRIDGE_RECORDS", "$KV.BUS_BRIDGE_RECORDS.>",
				// the request scaffold (ADR-014 phase 5): the answer goes to
				// deliver.<agent>.req.<request id>, and nowhere else on deliver
				"deliver.*.req.*"} {
				if !strings.Contains(pub, want) {
					t.Errorf("the bridge's rights lack %s: %+v", want, u.Permissions.Publish.Allow)
				}
			}
			// The request answer's subject, exactly once (ADR-014 phase 5).
			if n := strings.Count(pub, "deliver.*.req.*"); n != 1 {
				t.Errorf("the bridge's request answer right appears %d times: %+v", n, u.Permissions.Publish.Allow)
			}
			// The records bucket's three rights, each exactly once: read, write,
			// and no stream create or direct get (issue #151 step 3).
			for _, want := range []string{"$JS.API.STREAM.INFO.KV_BUS_BRIDGE_RECORDS",
				"$JS.API.STREAM.MSG.GET.KV_BUS_BRIDGE_RECORDS", "$KV.BUS_BRIDGE_RECORDS.>"} {
				if n := strings.Count(pub, want); n != 1 {
					t.Errorf("the bridge's records right %s appears %d times: %+v", want, n, u.Permissions.Publish.Allow)
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
					subject != "$JS.API.CONSUMER.CREATE.PRESENCE.bridge_accounts.presence.account.mattermost.*.v1" &&
					subject != "$JS.API.CONSUMER.CREATE.PRESENCE.bridge_planb.presence.planb.*.v1" &&
					subject != "$JS.API.CONSUMER.CREATE.PRESENCE.bridge_members.presence.member.*.*" {
					t.Errorf("the bridge may create consumers by %s", subject)
				}
			}
			sub := strings.Join(u.Permissions.Subscribe.Allow, " ")
			if !strings.Contains(sub, "_INBOX_bridge.>") || !strings.Contains(sub, "deliver.bridge.>") ||
				!strings.Contains(sub, "req.>") ||
				strings.Contains(sub, "_INBOX.>") ||
				strings.Contains(sub, "bus.>") || strings.Contains(sub, "presence.>") {
				t.Errorf("the bridge subscribes to %q", sub)
			}
		case metricsPub:
			if u.Account == nil || u.Account.Name != "BUS" {
				t.Errorf("the metrics user is not in BUS: %+v", u.Account)
			}
			// The subscribe-only narrowing, pinned (a NATS permissions
			// block without publish means allow-all publish): the seed's
			// holder may publish nothing, subscribe exactly the two
			// deliver subjects (#158, the probe in #168's review).
			if u.Permissions == nil || len(u.Permissions.Publish.Allow) != 0 {
				t.Error("the metrics user may publish")
			}
			deny := false
			for _, d := range u.Permissions.Publish.Deny {
				deny = deny || d == ">"
			}
			if !deny {
				t.Error("the metrics user's deny-all publish is missing")
			}
			if want := []string{"deliver.metrics.presence", "deliver.metrics.stats"}; len(u.Permissions.Subscribe.Allow) != 2 || u.Permissions.Subscribe.Allow[0] != want[0] || u.Permissions.Subscribe.Allow[1] != want[1] {
				t.Errorf("the metrics user's subscribe subjects: %v", u.Permissions.Subscribe.Allow)
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
			pub = append(pub, "$JS.API.STREAM.CREATE."+consumers.StatsStream, "$JS.API.STREAM.INFO."+consumers.StatsStream,
				// the STATS history (RT-6.45): the in-place update and the import
				"$JS.API.STREAM.UPDATE."+consumers.StatsStream, consumers.DefaultStatsPrefix+"."+consumers.ImportToken+".*")
			// the operators' consumers on STATS (RT-6.43)
			pub = append(pub, "$JS.API.CONSUMER.*."+consumers.StatsStream, "$JS.API.CONSUMER.*."+consumers.StatsStream+".>",
				"presence.person.*.v1")
			kv := "KV_" + directory.DefaultBucket
			pub = append(pub, "$JS.API.INFO", "$JS.API.STREAM.CREATE."+kv, "$JS.API.STREAM.INFO."+kv,
				"$JS.API.DIRECT.GET."+kv+".>", "$KV."+directory.DefaultBucket+".>",
				// the directory's key listings: an ordered consumer, made and removed
				"$JS.API.CONSUMER.CREATE."+kv+".>", "$JS.API.CONSUMER.DELETE."+kv+".*",
				"$JS.API.STREAM.MSG.GET.PRESENCE", "presence.attest.*.v1", "presence.notice.*.*.v1",
				"presence.account.mattermost.*.v1",
				// plan B's switches per channel (RT-6.39 step 5)
				"presence.planb.*.v1",
				// each agent's channel record (RT-6.39 step 6)
				"presence.directory.*.v1")
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
	// The STATS stream (RT-6.43) is made with the example's rights, and found
	// again at the next start.
	if err := mgr.EnsureStreams(ctx5(t)); err != nil {
		t.Fatalf("EnsureStreams on existing streams: %v", err)
	}
	if _, err := nc.Request("$JS.API.STREAM.INFO."+consumers.StatsStream, nil, 2*time.Second); err != nil {
		t.Fatalf("STATS: %v", err)
	}
	// The STATS history (RT-6.45): the update, an imported line and the
	// import's reading, with the example's rights.
	stats := mgr.Stats()
	if err := stats.Update(ctx5(t)); err != nil {
		t.Fatalf("STATS update: %v", err)
	}
	if err := stats.Put(ctx5(t), "bot-x", []byte(`{"v":"0.85.0"}`), nil); err != nil {
		t.Fatalf("STATS import: %v", err)
	}
	if err := stats.ReadAll(ctx5(t), func() {}, func(string, []byte, nats.Header) error { return nil }); err != nil {
		t.Fatalf("STATS read: %v", err)
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

	// The callout with the example's rights for its user (#172): the login
	// service gets the request and answers it, and an agent logs in.
	realm, chat := testrealm.New(t), testrealm.NewChat("alice")
	chat.Channels["alice"] = []string{"home"}
	brake := filepath.Join(t.TempDir(), "brake")
	if err := os.WriteFile(brake, []byte("# nobody stopped\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	svc, err := New(Config{Account: "BUS", Lobby: "lobby"}.WithDefaults(), k.issuer, k.xkey, realm.Verifier(chat), chat,
		NewBrake(brake, nil), slogTo(logs))
	if err != nil {
		t.Fatal(err)
	}
	refused := make(chan error, 8)
	svcNC, err := nats.Connect(srv.ClientURL(), nats.RootCAs(certFile), nats.TLSHandshakeFirst(),
		nats.Nkey(svcPub, k.svcUser.Sign),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, err error) { refused <- err }))
	if err != nil {
		t.Fatalf("the callout's user cannot connect: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { _ = svc.Serve(ctx, svcNC); close(done) }()
	t.Cleanup(func() { cancel(); <-done; svcNC.Close() })
	if err := svcNC.Flush(); err != nil {
		t.Fatal(err)
	}
	host := newHost()
	hostPub, _ := host.PublicKey()
	hello, _ := jsonMarshal(Hello{Token: realm.Token("alice", nil), Home: "home"})
	agentDenied := make(chan error, 4)
	agentNC, err := nats.Connect(srv.ClientURL(), nats.RootCAs(certFile), nats.TLSHandshakeFirst(),
		nats.Nkey(hostPub, host.Sign), nats.Token(string(hello)), nats.MaxReconnects(0),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, err error) { agentDenied <- err }))
	if err != nil {
		t.Fatalf("an agent cannot log in through the callout: %v (log: %s)", err, logs.String())
	}
	// The bridge's slash command records are the bridge's: an agent's credential
	// holds no KV right (issue #151 step 3).
	if err := agentNC.Publish("$KV.BUS_BRIDGE_RECORDS.modes", []byte("{}")); err != nil {
		t.Fatal(err)
	}
	_ = agentNC.Flush()
	select {
	case err := <-agentDenied:
		if want := `Permissions Violation for Publish to "$KV.BUS_BRIDGE_RECORDS.modes"`; !strings.Contains(err.Error(), want) {
			t.Errorf("agent publish: %v, want a permissions violation", err)
		}
	case <-time.After(2 * time.Second):
		t.Error("an agent may write the bridge's records")
	}
	agentNC.Close()
	if !strings.Contains(logs.String(), `"event":"issued"`) {
		t.Errorf("no credential issued: %s", logs.String())
	}
	select {
	case err := <-refused:
		t.Fatalf("the callout's answer was refused: %v", err)
	default:
	}
	// Anything but an answer is refused, a server inbox that no request named
	// included. (The request subject is no case: the server refuses it to
	// every auth user whatever the configuration says.)
	for _, subject := range []string{"bus.ch.home.someone", "$SYS._INBOX.abc.def"} {
		if err := svcNC.Publish(subject, nil); err != nil {
			t.Fatal(err)
		}
		select {
		case err := <-refused:
			if want := `Permissions Violation for Publish to "` + subject + `"`; !strings.Contains(err.Error(), want) {
				t.Errorf("publish %s: %v, want a permissions violation", subject, err)
			}
		case <-time.After(2 * time.Second):
			t.Errorf("the callout's user may publish %s", subject)
		}
	}
}
