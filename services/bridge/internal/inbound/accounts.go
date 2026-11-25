package inbound

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"regexp"
	"strings"
	"sync"

	"github.com/nats-io/nats.go"
)

// AccountsConsumer is the operator's consumer of the login service's account
// records (RT-6.42 step 3): last per subject, explicit ack, delivered to
// deliver.bridge.accounts. The bridge only subscribes and acks.
const AccountsConsumer = "bridge_accounts"

// Accounts maps a Mattermost account to the person's anchor from the login
// service's records on `presence.account.mattermost.<user id>.v1`. An account
// without a record that decodes, or with `anchor` null, is unbound: Anchor
// answers "", and the bridge carries it under `-` (points 3, 4 and 10). The
// record's `exp` and `approver` are the MCP server's to judge: a stale record
// still names the person, it only approves nothing (point 8).
//
// The bridge reads the records only through the operator's consumer and holds
// no right to make, reset or read past it. A durable consumer does not hand
// out again what was acked, so the operator makes bridge_accounts afresh
// before every start of the bridge (deliver last per subject, an idle
// heartbeat): it then delivers the last record of every account. The map is
// caught up at the first delivery that says nothing is pending, or at the
// first idle heartbeat (sent only when nothing is); until then no home
// signals, so no session reads people from a bridge that would carry a bound
// person under `-`. A consumer left over from before the restart delivers
// nothing old: the bound people go out under `-`, which fails closed.
type Accounts struct {
	Log     *slog.Logger
	Deliver string        // deliver.bridge.accounts
	Stream  string        // "PRESENCE", for the ack reply's prefix
	Ready   chan struct{} // closed once the subscription is active (tests wait on it)

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

// Run consumes until ctx ends.
func (a *Accounts) Run(ctx context.Context, nc *nats.Conn) error {
	ch := make(chan *nats.Msg, 256)
	sub, err := nc.ChanSubscribe(a.Deliver, ch)
	if err != nil {
		return fmt.Errorf("accounts: subscribe %s: %w", a.Deliver, err)
	}
	defer func() { _ = sub.Unsubscribe() }()
	if err := nc.Flush(); err != nil {
		return fmt.Errorf("accounts: subscribe %s: %w", a.Deliver, err)
	}
	if a.Ready != nil {
		close(a.Ready)
	}
	for {
		select {
		case <-ctx.Done():
			return nil
		case m := <-ch:
			if m.Header.Get("Status") == "100" && strings.Contains(m.Header.Get("Description"), "Idle") {
				// the server's idle heartbeat: nothing is pending
				a.caught()
				continue
			}
			// Only a delivery of the operator's consumer counts (no flow
			// control, no other status); what may be stored on a record's
			// subject at all is the login service's alone, and one checks the
			// subject.
			if m.Header.Get("Status") != "" ||
				!strings.HasPrefix(m.Reply, "$JS.ACK."+a.Stream+"."+AccountsConsumer+".") {
				a.Log.Warn("accounts: not a delivery, dropped", "subject", m.Subject)
				continue
			}
			a.one(m.Subject, m.Data)
			if md, err := m.Metadata(); err == nil && md.NumPending == 0 {
				a.caught()
			}
			if err := m.Ack(); err != nil {
				a.Log.Warn("accounts: ack refused", "error", err)
			}
		}
	}
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
