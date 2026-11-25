package inbound

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
)

// AccountsConsumer is the name stem of the bridge's own consumer of the login
// service's account records (RT-6.42 step 3); each start makes one of its own,
// bridge_accounts_<nonce>.
const AccountsConsumer = "bridge_accounts"

// AccountsFilter is the consumer's filter, fixed in the bridge's create right
// ($JS.API.CONSUMER.CREATE.PRESENCE.*.presence.account.mattermost.*.v1).
const AccountsFilter = "presence.account.mattermost.*.v1"

// The bridge's own consumer: an idle heartbeat says nothing is pending, and a
// consumer nobody listens to is gone after the threshold, before the next.
const (
	accountsHeartbeat = 5 * time.Second
	accountsInactive  = 30 * time.Second
)

func (a *Accounts) beats() (heartbeat, inactive time.Duration) {
	heartbeat, inactive = a.Heartbeat, a.Inactive
	if heartbeat == 0 {
		heartbeat = accountsHeartbeat
	}
	if inactive == 0 {
		inactive = accountsInactive
	}
	return heartbeat, inactive
}

// Accounts maps a Mattermost account to the person's anchor from the login
// service's records on `presence.account.mattermost.<user id>.v1`. An account
// without a record that decodes, or with `anchor` null, is unbound: Anchor
// answers "", and the bridge carries it under `-` (points 3, 4 and 10). The
// record's `exp` and `approver` are the MCP server's to judge: a stale record
// still names the person, it only approves nothing (point 8).
//
// At every start the bridge makes a consumer of its own, under a fresh name
// and delivery subject (bridge_accounts_<nonce>, deliver.bridge.accounts.<nonce>):
// deliver last per subject, ack none, an idle heartbeat, gone 30 s after
// nobody listens. A fresh name, because a create with the config of a consumer
// that still exists returns that consumer, which hands out nothing it handed
// out before. Its right names the filter, so the bridge can make no consumer
// of anything else, and it holds no delete right. The map is caught up at the
// first delivery with nothing pending, or at the first idle heartbeat (sent
// only when nothing is); until then no home carries or signals. A consumer
// that goes silent (NATS was away past the threshold) is replaced by a new one.
type Accounts struct {
	Log     *slog.Logger
	Deliver string        // deliver.bridge.accounts: the stem of the delivery subject
	Stream  string        // "PRESENCE"
	Ready   chan struct{} // closed once the first consumer is made (tests wait on it)
	// Heartbeat and Inactive set the consumer's idle heartbeat (5 s) and its
	// inactive threshold (30 s); the tests shorten them.
	Heartbeat, Inactive time.Duration

	mu       sync.Mutex
	anchors  map[string]string // accounts with a record, "" for unbound
	caughtUp bool
}

// Anchor is the anchor bound to a Mattermost user id, "" when unbound.
func (a *Accounts) Anchor(userID string) string {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.anchors[userID]
}

// CaughtUp says the consumer has handed out every record it holds.
func (a *Accounts) CaughtUp() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.caughtUp
}

// Run makes consumers and reads them until ctx ends.
func (a *Accounts) Run(ctx context.Context, nc *nats.Conn) error {
	for ctx.Err() == nil {
		if err := a.consume(ctx, nc); err != nil && ctx.Err() == nil {
			a.Log.Error("accounts: the account records cannot be read; trying again", "error", err)
			select {
			case <-ctx.Done():
			case <-time.After(time.Second):
			}
		}
	}
	return nil
}

// consume makes one consumer and reads it until it goes silent or ctx ends.
func (a *Accounts) consume(ctx context.Context, nc *nats.Conn) error {
	nonce := make([]byte, 8)
	if _, err := rand.Read(nonce); err != nil {
		return err
	}
	name := AccountsConsumer + "_" + hex.EncodeToString(nonce)
	deliver := a.Deliver + "." + hex.EncodeToString(nonce)
	ch := make(chan *nats.Msg, 256)
	// the subscription first: a push consumer delivers to whom listens
	sub, err := nc.ChanSubscribe(deliver, ch)
	if err != nil {
		return fmt.Errorf("accounts: subscribe %s: %w", deliver, err)
	}
	defer func() { _ = sub.Unsubscribe() }()
	if err := nc.Flush(); err != nil {
		return fmt.Errorf("accounts: subscribe %s: %w", deliver, err)
	}
	if err := a.create(ctx, nc, name, deliver); err != nil {
		return err
	}
	if a.Ready != nil {
		select {
		case <-a.Ready:
		default:
			close(a.Ready)
		}
	}
	prefix := "$JS.ACK." + a.Stream + "." + name + "."
	heartbeat, _ := a.beats()
	// no message for three heartbeats: the consumer is gone, a new one
	silence := time.NewTimer(3 * heartbeat)
	defer silence.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-silence.C:
			a.Log.Warn("accounts: the consumer went silent; making a new one", "consumer", name)
			return nil
		case m := <-ch:
			silence.Reset(3 * heartbeat)
			if m.Header.Get("Status") == "100" && strings.Contains(m.Header.Get("Description"), "Idle") {
				// the server's idle heartbeat: nothing is pending
				a.caught()
				continue
			}
			// Only a delivery of this consumer counts; what may be stored on a
			// record's subject at all is the login service's alone, and one
			// checks the subject.
			if m.Header.Get("Status") != "" || !strings.HasPrefix(m.Reply, prefix) {
				a.Log.Warn("accounts: not a delivery, dropped", "subject", m.Subject)
				continue
			}
			a.one(m.Subject, m.Data)
			if md, err := m.Metadata(); err == nil && md.NumPending == 0 {
				a.caught()
			}
		}
	}
}

// create makes the consumer through the create API with the filter in the
// subject, the form the bridge's right names.
func (a *Accounts) create(ctx context.Context, nc *nats.Conn, name, deliver string) error {
	heartbeat, inactive := a.beats()
	body, err := json.Marshal(map[string]any{"stream_name": a.Stream, "action": "create", "config": map[string]any{
		"name": name, "filter_subject": AccountsFilter, "deliver_subject": deliver,
		"deliver_policy": "last_per_subject", "ack_policy": "none",
		"idle_heartbeat": int64(heartbeat), "inactive_threshold": int64(inactive),
	}})
	if err != nil {
		return err
	}
	rctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	m, err := nc.RequestWithContext(rctx, "$JS.API.CONSUMER.CREATE."+a.Stream+"."+name+"."+AccountsFilter, body)
	if err != nil {
		return fmt.Errorf("accounts: create %s: %w", name, err)
	}
	var resp struct {
		Error *struct {
			Code int    `json:"code"`
			Desc string `json:"description"`
		} `json:"error"`
	}
	if err := json.Unmarshal(m.Data, &resp); err != nil {
		return fmt.Errorf("accounts: create %s: %w", name, err)
	}
	if resp.Error != nil {
		return fmt.Errorf("accounts: create %s: %d %s", name, resp.Error.Code, resp.Error.Desc)
	}
	return nil
}

func (a *Accounts) caught() {
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.caughtUp {
		a.Log.Info("accounts: the account records are read", "accounts", len(a.anchors))
	}
	a.caughtUp = true
}

func (a *Accounts) one(subject string, data []byte) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.take(subject, data)
}

// take records one record; a.mu is held.
func (a *Accounts) take(subject string, data []byte) {
	uid, ok := accountSubject(subject)
	if !ok {
		a.Log.Warn("accounts: unexpected subject", "subject", subject)
		return
	}
	rec, err := DecodeAccount(data)
	if err == nil && rec.Account != uid {
		err = fmt.Errorf("the record names account %q", rec.Account)
	}
	a.set(uid, rec.Anchor, err)
}

// set records an account's anchor; a record that does not decode is unbound
// (step 10). a.mu is held.
func (a *Accounts) set(uid, anchor string, err error) {
	if a.anchors == nil {
		a.anchors = map[string]string{}
	}
	if err != nil {
		anchor = ""
		a.Log.Warn("accounts: a record that does not decode; unbound", "account", uid, "error", err)
	}
	a.anchors[uid] = anchor
}

// accountSubject is the user id of presence.account.mattermost.<id>.v1.
func accountSubject(subject string) (string, bool) {
	parts := strings.Split(subject, ".")
	if len(parts) != 5 || parts[0] != "presence" || parts[1] != "account" ||
		parts[2] != "mattermost" || parts[4] != "v1" || !mmID.MatchString(parts[3]) {
		return "", false
	}
	return parts[3], true
}

// Account is a decoded account record; Anchor "" stands for null.
type Account struct {
	Account  string
	Anchor   string
	Name     string
	Approver bool
	TokenIat int64
	Iat      int64
	Exp      int64
}

// anchorRE is the login service's anchor (services/auth directory.anchorRE).
var anchorRE = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,63}$`)

// accountFields are the record's fields, all required, no other.
var accountFields = []string{"v", "account", "anchor", "name", "approver", "token_iat", "iat", "exp"}

// DecodeAccount decodes a record strictly (RT-6.42 step 3): format v1, exactly
// its fields, no unknown or duplicate key; an empty message is refused. A JSON
// answer that landed on the subject (step 10) is never one.
func DecodeAccount(data []byte) (Account, error) {
	fields, err := strictObject(data, accountFields, accountFields)
	if err != nil {
		return Account{}, err
	}
	var rec Account
	var v int
	var anchor *string
	for key, dst := range map[string]any{"v": &v, "account": &rec.Account, "anchor": &anchor,
		"name": &rec.Name, "approver": &rec.Approver, "token_iat": &rec.TokenIat,
		"iat": &rec.Iat, "exp": &rec.Exp} {
		if key != "anchor" && string(fields[key]) == "null" {
			return Account{}, fmt.Errorf("%s is null", key)
		}
		if err := json.Unmarshal(fields[key], dst); err != nil {
			return Account{}, fmt.Errorf("%s: %w", key, err)
		}
	}
	if v != 1 {
		return Account{}, fmt.Errorf("format %d, not 1", v)
	}
	if !mmID.MatchString(rec.Account) {
		return Account{}, fmt.Errorf("account %q is no Mattermost id", rec.Account)
	}
	if anchor != nil {
		if !anchorRE.MatchString(*anchor) {
			return Account{}, fmt.Errorf("anchor %q", *anchor)
		}
		rec.Anchor = *anchor
	}
	return rec, nil
}

// strictObject splits one JSON object into its raw fields: every key in
// allowed, none twice, each of required present, nothing after the object.
func strictObject(data []byte, allowed, required []string) (map[string]json.RawMessage, error) {
	if len(bytes.TrimSpace(data)) == 0 {
		return nil, errors.New("an empty message")
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	tok, err := dec.Token()
	if err != nil {
		return nil, err
	}
	if d, ok := tok.(json.Delim); !ok || d != '{' {
		return nil, errors.New("not an object")
	}
	ok := map[string]bool{}
	for _, k := range allowed {
		ok[k] = true
	}
	out := map[string]json.RawMessage{}
	for dec.More() {
		tok, err := dec.Token()
		if err != nil {
			return nil, err
		}
		key, _ := tok.(string)
		if !ok[key] {
			return nil, fmt.Errorf("unknown key %q", key)
		}
		if _, dup := out[key]; dup {
			return nil, fmt.Errorf("duplicate key %q", key)
		}
		var raw json.RawMessage
		if err := dec.Decode(&raw); err != nil {
			return nil, err
		}
		out[key] = raw
	}
	if _, err := dec.Token(); err != nil {
		return nil, err
	}
	if _, err := dec.Token(); !errors.Is(err, io.EOF) {
		return nil, errors.New("data after the object")
	}
	for _, k := range required {
		if _, has := out[k]; !has {
			return nil, fmt.Errorf("no %q", k)
		}
	}
	return out, nil
}
