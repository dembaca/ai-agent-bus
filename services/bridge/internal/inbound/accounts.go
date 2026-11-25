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
	"time"

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
// The operator's consumer delivers each record once and the bridge acks it,
// so after a restart it delivers only what changed since. An account the
// bridge has not heard of since it started is looked up once, the last
// record on its subject, through the stream's message-get API (the bridge's
// right `$JS.API.STREAM.MSG.GET.PRESENCE`); its answer is wrapped JSON, so a
// reply routed elsewhere is never a record (step 10).
type Accounts struct {
	Log     *slog.Logger
	Deliver string        // deliver.bridge.accounts
	Stream  string        // "PRESENCE", for the ack reply's prefix and the lookup
	Ready   chan struct{} // closed once the subscription is active (tests wait on it)
	// Timeout bounds one lookup; 5 s when zero.
	Timeout time.Duration

	mu      sync.Mutex
	nc      *nats.Conn
	anchors map[string]string // known accounts, "" for unbound
}

// Anchor is the anchor bound to a Mattermost user id, "" when unbound. An
// account not heard of since the start is looked up; a lookup that fails is
// an error, never "unbound".
func (a *Accounts) Anchor(ctx context.Context, userID string) (string, error) {
	a.mu.Lock()
	anchor, known := a.anchors[userID]
	nc := a.nc
	a.mu.Unlock()
	if known {
		return anchor, nil
	}
	if nc == nil {
		return "", errors.New("accounts: not reading the records yet")
	}
	subject := "presence.account.mattermost." + userID + ".v1"
	data, found, err := a.last(ctx, nc, subject)
	if err != nil {
		return "", err
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if anchor, known := a.anchors[userID]; known {
		return anchor, nil // the consumer was faster, and is newer
	}
	if !found {
		a.set(userID, "", nil)
		return "", nil
	}
	a.take(subject, data)
	return a.anchors[userID], nil
}

// last reads the last message on subject through the message-get API.
func (a *Accounts) last(ctx context.Context, nc *nats.Conn, subject string) ([]byte, bool, error) {
	timeout := a.Timeout
	if timeout == 0 {
		timeout = 5 * time.Second
	}
	rctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	req, _ := json.Marshal(map[string]string{"last_by_subj": subject})
	m, err := nc.RequestWithContext(rctx, "$JS.API.STREAM.MSG.GET."+a.Stream, req)
	if err != nil {
		return nil, false, fmt.Errorf("accounts: the lookup of %s: %w", subject, err)
	}
	var resp struct {
		Message *struct {
			Subject string `json:"subject"`
			Data    []byte `json:"data"`
		} `json:"message"`
		Error *struct {
			Code int    `json:"code"`
			Desc string `json:"description"`
		} `json:"error"`
	}
	if err := json.Unmarshal(m.Data, &resp); err != nil {
		return nil, false, fmt.Errorf("accounts: the lookup of %s: %w", subject, err)
	}
	switch {
	case resp.Error != nil && resp.Error.Code == 404:
		return nil, false, nil
	case resp.Error != nil:
		return nil, false, fmt.Errorf("accounts: the lookup of %s: %d %s", subject, resp.Error.Code, resp.Error.Desc)
	case resp.Message == nil || resp.Message.Subject != subject:
		return nil, false, fmt.Errorf("accounts: the lookup of %s answered no message of it", subject)
	}
	return resp.Message.Data, true, nil
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
	a.mu.Lock()
	a.nc = nc
	a.mu.Unlock()
	if a.Ready != nil {
		close(a.Ready)
	}
	for {
		select {
		case <-ctx.Done():
			return nil
		case m := <-ch:
			// Only a delivery of the operator's consumer counts (no heartbeat,
			// no flow control); what may be stored on a record's subject at all
			// is the login service's alone, and one checks the subject.
			if m.Header.Get("Status") != "" ||
				!strings.HasPrefix(m.Reply, "$JS.ACK."+a.Stream+"."+AccountsConsumer+".") {
				a.Log.Warn("accounts: not a delivery, dropped", "subject", m.Subject)
				continue
			}
			a.one(m.Subject, m.Data)
			if err := m.Ack(); err != nil {
				a.Log.Warn("accounts: ack refused", "error", err)
			}
		}
	}
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
