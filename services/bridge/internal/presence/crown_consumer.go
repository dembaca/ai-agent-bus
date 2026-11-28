package presence

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strconv"
	"strings"
	"time"

	"github.com/nats-io/nats.go"
)

// ClaimsConsumer is the bridge's own consumer of the crown claims (RT-6.38
// step 1), made by the bridge at every start, for the crown it shows and the
// header line it writes (ADR-014 phase 5, plan B piece 9). The operator's
// bridge_presence consumer carries only presence.*.*, and a member claims
// again only after 23 h: a restart must replay the claims.
const ClaimsConsumer = "bridge_crown"

// ClaimsFilter is the consumer's filter. The bridge's create right names the
// consumer and the filter ($JS.API.CONSUMER.CREATE.PRESENCE.bridge_crown.
// presence.crown.*.*): it can make or change no other consumer.
const ClaimsFilter = "presence.crown.*.*"

const (
	claimsHeartbeat = 5 * time.Second
	claimsInactive  = 30 * time.Second
)

var errClaimsStale = errors.New("crown claims: a consumer of an earlier start")

// ClaimsReader reads the crown claims into Claims through a consumer it makes
// itself at every start: the last claim per subject, so every claim the
// stream still holds is replayed and a restart loses none; ack none, an idle
// heartbeat, gone 30 s after nobody listens. It holds no delete right: a
// consumer of that name still there (a restart within the threshold) makes
// the create fail, and the reader waits for it to go.
type ClaimsReader struct {
	Log      *slog.Logger
	Deliver  string // deliver.bridge.crown: the stem of the delivery subject
	Stream   string // "PRESENCE"
	Into     *Claims
	Ready    chan struct{} // closed once the first consumer is made (tests wait on it)
	CaughtUp chan struct{} // closed once the first replay is read (tests wait on it)
	// Heartbeat and Inactive set the idle heartbeat (5 s) and the inactive
	// threshold (30 s); the tests shorten them.
	Heartbeat, Inactive time.Duration
}

func (r *ClaimsReader) beats() (heartbeat, inactive time.Duration) {
	heartbeat, inactive = r.Heartbeat, r.Inactive
	if heartbeat == 0 {
		heartbeat = claimsHeartbeat
	}
	if inactive == 0 {
		inactive = claimsInactive
	}
	return heartbeat, inactive
}

// claimsRetryMax bounds the wait after failures in a row: a create right not
// granted yet (an image before the lab's configuration) logs one error a
// minute, not one a second.
const claimsRetryMax = time.Minute

// Run makes consumers and reads them until ctx ends.
func (r *ClaimsReader) Run(ctx context.Context, nc *nats.Conn) error {
	backoff := time.Second
	for ctx.Err() == nil {
		err := r.consume(ctx, nc)
		if err == nil || ctx.Err() != nil {
			backoff = time.Second
			continue
		}
		wait := backoff
		backoff = min(2*backoff, claimsRetryMax)
		if errors.Is(err, errClaimsStale) {
			heartbeat, inactive := r.beats()
			wait = inactive + heartbeat
			r.Log.Warn("crown claims: the consumer of an earlier start is still there; waiting for it to go",
				"wait", wait.String(), "error", err)
		} else {
			r.Log.Error("crown claims: the claims cannot be read; no crown shown, no header line written; trying again",
				"wait", wait.String(), "error", err)
		}
		select {
		case <-ctx.Done():
		case <-time.After(wait):
		}
	}
	return nil
}

func (r *ClaimsReader) consume(ctx context.Context, nc *nats.Conn) error {
	nonce := make([]byte, 8)
	if _, err := rand.Read(nonce); err != nil {
		return err
	}
	deliver := r.Deliver + "." + hex.EncodeToString(nonce)
	ch := make(chan *nats.Msg, 256)
	sub, err := nc.ChanSubscribe(deliver, ch)
	if err != nil {
		return fmt.Errorf("crown claims: subscribe %s: %w", deliver, err)
	}
	defer func() { _ = sub.Unsubscribe() }()
	if err := nc.Flush(); err != nil {
		return fmt.Errorf("crown claims: subscribe %s: %w", deliver, err)
	}
	r.Into.Begin()
	if err := r.create(ctx, nc, deliver); err != nil {
		return err
	}
	if r.Ready != nil {
		select {
		case <-r.Ready:
		default:
			close(r.Ready)
		}
	}
	prefix := "$JS.ACK." + r.Stream + "." + ClaimsConsumer + "."
	heartbeat, _ := r.beats()
	silence := time.NewTimer(3 * heartbeat)
	defer silence.Stop()
	var last uint64
	caught := func() {
		r.Into.Commit()
		if r.CaughtUp != nil {
			select {
			case <-r.CaughtUp:
			default:
				close(r.CaughtUp)
			}
		}
	}
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-silence.C:
			r.Log.Warn("crown claims: the consumer went silent; making a new one")
			return nil
		case m := <-ch:
			silence.Reset(3 * heartbeat)
			if m.Header.Get("Status") == "100" && strings.Contains(m.Header.Get("Description"), "Idle") {
				if seen, err := strconv.ParseUint(m.Header.Get("Nats-Last-Consumer"), 10, 64); err == nil && seen != last {
					r.Log.Warn("crown claims: a claim was lost on the way; making a new consumer")
					return nil
				}
				caught()
				continue
			}
			if m.Header.Get("Status") != "" || !strings.HasPrefix(m.Reply, prefix) {
				continue
			}
			md, err := m.Metadata()
			if err != nil {
				continue
			}
			if md.Sequence.Consumer != last+1 {
				r.Log.Warn("crown claims: a claim was lost on the way; making a new consumer")
				return nil
			}
			last = md.Sequence.Consumer
			r.one(m, md.Sequence.Stream, md.Timestamp)
			if md.NumPending == 0 {
				caught()
			}
		}
	}
}

func (r *ClaimsReader) one(m *nats.Msg, seq uint64, at time.Time) {
	channel, name, ok := ClaimSubject(m.Subject)
	if !ok {
		return
	}
	if err := DecodeClaim(m.Data); err != nil {
		// as the MCP server reads it: a claim that does not decode counts for nothing
		r.Log.Warn("crown claims: a claim that does not decode, ignored", "subject", m.Subject, "error", err)
		return
	}
	r.Into.Observed(channel, name, seq, at)
}

// create makes the consumer through the create API with the filter in the
// subject, the form the bridge's right names.
func (r *ClaimsReader) create(ctx context.Context, nc *nats.Conn, deliver string) error {
	heartbeat, inactive := r.beats()
	body, err := json.Marshal(map[string]any{"stream_name": r.Stream, "action": "create", "config": map[string]any{
		"name": ClaimsConsumer, "filter_subject": ClaimsFilter, "deliver_subject": deliver,
		"deliver_policy": "last_per_subject", "ack_policy": "none",
		"idle_heartbeat": int64(heartbeat), "inactive_threshold": int64(inactive),
	}})
	if err != nil {
		return err
	}
	rctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	m, err := nc.RequestWithContext(rctx, "$JS.API.CONSUMER.CREATE."+r.Stream+"."+ClaimsConsumer+"."+ClaimsFilter, body)
	if err != nil {
		return fmt.Errorf("crown claims: create %s: %w", ClaimsConsumer, err)
	}
	var resp struct {
		Created time.Time `json:"created"` // the server's clock
		Config  *struct {
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
		return fmt.Errorf("crown claims: create %s: %w", ClaimsConsumer, err)
	}
	switch {
	case resp.Error != nil && resp.Error.ErrCode == errConsumerExists:
		return fmt.Errorf("%w: %s", errClaimsStale, resp.Error.Desc)
	case resp.Error != nil:
		return fmt.Errorf("crown claims: create %s: %d %s", ClaimsConsumer, resp.Error.Code, resp.Error.Desc)
	case resp.Delivered.Consumer > 0 || resp.Config == nil || resp.Config.DeliverSubject != deliver:
		return fmt.Errorf("%w: it delivered %d before", errClaimsStale, resp.Delivered.Consumer)
	}
	// The server's own time, before the first claim is read: an old claim
	// replayed after a start must not look fresh.
	if !resp.Created.IsZero() {
		r.Into.Seed(resp.Created)
	}
	return nil
}
