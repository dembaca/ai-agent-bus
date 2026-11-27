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
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
)

// AccountsConsumer is the bridge's own consumer of the login service's account
// records (RT-6.42 step 3), made by the bridge at every start.
const AccountsConsumer = "bridge_accounts"

// AccountsFilter is the consumer's filter. The bridge's create right names the
// consumer and the filter ($JS.API.CONSUMER.CREATE.PRESENCE.bridge_accounts.
// presence.account.mattermost.*.v1): it can make or change no other consumer.
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
// service's records on `presence.account.mattermost.<user id>.v1`, and keeps
// each record itself for the history read (ADR-014 phase 5, piece 5). An
// account without a record that decodes, or with `anchor` null, is unbound:
// Anchor answers "", and the bridge carries it under `-` (points 3, 4 and 10).
// The record's `exp` and `approver` are the reader's to judge: a stale record
// still names the person, it only approves nothing (point 8).
//
// At every start the bridge makes its consumer bridge_accounts with a fresh
// delivery subject (deliver.bridge.accounts.<nonce>): deliver last per
// subject, ack none, an idle heartbeat, gone 30 s after nobody listens. It
// holds no delete right. A consumer of that name that is still there (a
// restart within the threshold) refuses the new config, or would hand out
// nothing again: the bridge then stays unsubscribed for the threshold, so the
// old one goes, and creates again. The map is caught up at the
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
	records  map[string]Account // every account's newest record; Anchor "" is unbound
	staging  map[string]Account // a new consumer's records until it caught up
	caughtUp bool
}

// Anchor is the anchor bound to a Mattermost user id, "" when unbound.
func (a *Accounts) Anchor(userID string) string {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.records[userID].Anchor
}

// Record is the login service's newest record of a Mattermost account, false
// when the records are not read yet, when there is none that decodes, or when
// it binds no anchor (unbound). The record's `exp` and `approver` are the
// reader's to judge: the history read answers it beside each post, and the
// session applies RT-6.42's freshness rule as it does to a live person's event.
func (a *Accounts) Record(userID string) (Account, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	rec, ok := a.records[userID]
	if !a.caughtUp || !ok || rec.Anchor == "" {
		return Account{}, false
	}
	return rec, true
}

// Owner is the Mattermost user id whose record binds anchor, the other way
// round from Anchor (RT-6.44: an agent bot's owner). "" until the records are
// read, for an anchor no record binds, and for one that more than one record
// binds: never a guess.
func (a *Accounts) Owner(anchor string) string {
	if anchor == "" {
		return ""
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.caughtUp {
		return ""
	}
	owner := ""
	for uid, rec := range a.records {
		if rec.Anchor != anchor {
			continue
		}
		if owner != "" {
			return ""
		}
		owner = uid
	}
	return owner
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
		err := a.consume(ctx, nc)
		if err == nil || ctx.Err() != nil {
			continue
		}
		wait := time.Second
		if errors.Is(err, errStale) {
			// unsubscribed now: the old consumer goes after its threshold
			heartbeat, inactive := a.beats()
			wait = inactive + heartbeat
			a.Log.Warn("accounts: the consumer of an earlier start is still there; waiting for it to go",
				"wait", wait.String(), "error", err)
		} else {
			a.Log.Error("accounts: the account records cannot be read; trying again", "error", err)
		}
		select {
		case <-ctx.Done():
		case <-time.After(wait):
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
	name := AccountsConsumer
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
	// A new consumer reads every record afresh into a map of its own, which
	// replaces the old one once caught up: an account unbound while NATS was
	// away must not keep its old anchor. Until then no home carries.
	a.mu.Lock()
	a.staging, a.caughtUp = map[string]Account{}, false
	a.mu.Unlock()
	if err := a.create(ctx, nc, name, deliver); err != nil {
		return err // errStale: Run waits the threshold, unsubscribed
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
	// Ack none: a delivery lost on the way shows only as a gap in the
	// consumer's sequence, in a delivery's or in the heartbeat's
	// Nats-Last-Consumer; a gap starts over with a new consumer.
	var last uint64
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
				if seen, err := strconv.ParseUint(m.Header.Get("Nats-Last-Consumer"), 10, 64); err == nil && seen != last {
					a.Log.Warn("accounts: a record was lost on the way; making a new consumer",
						"consumer", name, "delivered", seen, "received", last)
					return nil
				}
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
			md, err := m.Metadata()
			if err != nil {
				a.Log.Warn("accounts: not a delivery, dropped", "subject", m.Subject)
				continue
			}
			if md.Sequence.Consumer != last+1 {
				a.Log.Warn("accounts: a record was lost on the way; making a new consumer",
					"consumer", name, "delivered", md.Sequence.Consumer, "received", last)
				return nil
			}
			last = md.Sequence.Consumer
			a.one(m.Subject, m.Data)
			if md.NumPending == 0 {
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
		Config *struct {
			DeliverSubject string `json:"deliver_subject"`
		} `json:"config"`
		Delivered struct {
			Consumer uint64 `json:"consumer_seq"`
		} `json:"delivered"`
		Error *struct {
			Code    int    `json:"code"`
			ErrCode int    `json:"err_code"`
			Desc    string `json:"description"`
		} `json:"error"`
	}
	if err := json.Unmarshal(m.Data, &resp); err != nil {
		return fmt.Errorf("accounts: create %s: %w", name, err)
	}
	switch {
	case resp.Error != nil && resp.Error.ErrCode == errConsumerExists:
		return fmt.Errorf("%w: %s", errStale, resp.Error.Desc)
	case resp.Error != nil:
		return fmt.Errorf("accounts: create %s: %d %s", name, resp.Error.Code, resp.Error.Desc)
	case resp.Delivered.Consumer > 0 || resp.Config == nil || resp.Config.DeliverSubject != deliver:
		// the create returned a consumer that already handed out records
		return fmt.Errorf("%w: it delivered %d before", errStale, resp.Delivered.Consumer)
	}
	return nil
}

// errConsumerExists is JetStream's "consumer already exists" (another config).
const errConsumerExists = 10148

// errStale: a consumer of the bridge's name from an earlier start is there.
var errStale = errors.New("accounts: a consumer of an earlier start")

// caught commits the new consumer's records over the old ones, once it caught
// up.
func (a *Accounts) caught() {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.staging != nil {
		a.records, a.staging = a.staging, nil
		a.Log.Info("accounts: the account records are read", "accounts", len(a.records))
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
	a.set(uid, rec, err)
}

// set records an account's record; one that does not decode is unbound
// (step 10). a.mu is held.
func (a *Accounts) set(uid string, rec Account, err error) {
	if err != nil {
		rec = Account{}
		a.Log.Warn("accounts: a record that does not decode; unbound", "account", uid, "error", err)
	}
	if a.staging != nil {
		a.staging[uid] = rec
		return
	}
	if a.records == nil {
		a.records = map[string]Account{}
	}
	a.records[uid] = rec
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
