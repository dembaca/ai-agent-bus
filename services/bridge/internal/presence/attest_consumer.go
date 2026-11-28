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

// AttestConsumer is the bridge's own consumer of the login service's
// attestations of the agents (presence.attest.<agent>.v1; plan B piece 10,
// RT-6.13), made by the bridge at every start. The permission DMs check whom
// an agent may reach through them: an attestation written while the bridge was
// down must still be read.
const AttestConsumer = "bridge_attest"

// AttestFilter is the consumer's filter. The bridge's create right names the
// consumer and the filter ($JS.API.CONSUMER.CREATE.PRESENCE.bridge_attest.
// presence.attest.*.v1): it can make or change no other consumer.
const AttestFilter = "presence.attest.*.v1"

const (
	attestHeartbeat = 5 * time.Second
	attestInactive  = 30 * time.Second
)

var errAttestStale = errors.New("attestations: a consumer of an earlier start")

// AttestReader reads the attestations into Attestations through a consumer it
// makes itself at every start: last per subject, so every agent's newest
// attestation the stream still holds is replayed once; ack none, an idle
// heartbeat, gone 30 s after nobody listens. It holds no delete right: a
// consumer of that name still there (a restart within the threshold) makes the
// create fail, and the reader waits for it to go.
type AttestReader struct {
	Log     *slog.Logger
	Deliver string // deliver.bridge.attest: the stem of the delivery subject
	Stream  string // "PRESENCE"
	Into    *Attestations
	Ready   chan struct{} // closed once the first consumer is made (tests wait on it)
	// CaughtUp is closed once the first replay is read (tests wait on it).
	CaughtUp chan struct{}
	// Heartbeat and Inactive set the idle heartbeat (5 s) and the inactive
	// threshold (30 s); the tests shorten them.
	Heartbeat, Inactive time.Duration
}

func (r *AttestReader) beats() (heartbeat, inactive time.Duration) {
	heartbeat, inactive = r.Heartbeat, r.Inactive
	if heartbeat == 0 {
		heartbeat = attestHeartbeat
	}
	if inactive == 0 {
		inactive = attestInactive
	}
	return heartbeat, inactive
}

// Run makes consumers and reads them until ctx ends.
func (r *AttestReader) Run(ctx context.Context, nc *nats.Conn) error {
	for ctx.Err() == nil {
		err := r.consume(ctx, nc)
		if err == nil || ctx.Err() != nil {
			continue
		}
		wait := time.Second
		if errors.Is(err, errAttestStale) {
			heartbeat, inactive := r.beats()
			wait = inactive + heartbeat
			r.Log.Warn("attestations: the consumer of an earlier start is still there; waiting for it to go",
				"wait", wait.String(), "error", err)
		} else {
			r.Log.Error("attestations: the records cannot be read; trying again", "error", err)
		}
		select {
		case <-ctx.Done():
		case <-time.After(wait):
		}
	}
	return nil
}

// consume makes one consumer and reads it until it goes silent or ctx ends.
func (r *AttestReader) consume(ctx context.Context, nc *nats.Conn) error {
	nonce := make([]byte, 8)
	if _, err := rand.Read(nonce); err != nil {
		return err
	}
	deliver := r.Deliver + "." + hex.EncodeToString(nonce)
	ch := make(chan *nats.Msg, 256)
	sub, err := nc.ChanSubscribe(deliver, ch)
	if err != nil {
		return fmt.Errorf("attestations: subscribe %s: %w", deliver, err)
	}
	defer func() { _ = sub.Unsubscribe() }()
	if err := nc.Flush(); err != nil {
		return fmt.Errorf("attestations: subscribe %s: %w", deliver, err)
	}
	if err := r.create(ctx, nc, deliver); err != nil {
		return err // errAttestStale: Run waits the threshold, unsubscribed
	}
	if r.Ready != nil {
		select {
		case <-r.Ready:
		default:
			close(r.Ready)
		}
	}
	prefix := "$JS.ACK." + r.Stream + "." + AttestConsumer + "."
	heartbeat, _ := r.beats()
	silence := time.NewTimer(3 * heartbeat)
	defer silence.Stop()
	caught := func() {
		if r.CaughtUp != nil {
			select {
			case <-r.CaughtUp:
			default:
				close(r.CaughtUp)
			}
		}
	}
	var last uint64
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-silence.C:
			r.Log.Warn("attestations: the consumer went silent; making a new one")
			return nil
		case m := <-ch:
			silence.Reset(3 * heartbeat)
			if m.Header.Get("Status") == "100" && strings.Contains(m.Header.Get("Description"), "Idle") {
				if seen, err := strconv.ParseUint(m.Header.Get("Nats-Last-Consumer"), 10, 64); err == nil && seen != last {
					r.Log.Warn("attestations: a record was lost on the way; making a new consumer")
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
				r.Log.Warn("attestations: a record was lost on the way; making a new consumer")
				return nil
			}
			last = md.Sequence.Consumer
			r.one(m, md.Sequence.Stream)
			if md.NumPending == 0 {
				caught()
			}
		}
	}
}

// one records one attestation: its subject names the agent, its body must be
// an attestation v1 of that agent. A record that does not decode names none.
func (r *AttestReader) one(m *nats.Msg, seq uint64) {
	agent, ok := AttestAgent(m.Subject)
	if !ok {
		r.Log.Warn("attestations: unexpected subject", "subject", m.Subject)
		return
	}
	a, err := DecodeAttestation(agent, m.Data)
	if err != nil {
		r.Log.Warn("attestations: a record that does not decode", "subject", m.Subject, "error", err)
		return
	}
	r.Into.Observed(agent, a, seq)
}

// create makes the consumer through the create API with the filter in the
// subject, the form the bridge's right names.
func (r *AttestReader) create(ctx context.Context, nc *nats.Conn, deliver string) error {
	heartbeat, inactive := r.beats()
	body, err := json.Marshal(map[string]any{"stream_name": r.Stream, "action": "create", "config": map[string]any{
		"name": AttestConsumer, "filter_subject": AttestFilter, "deliver_subject": deliver,
		"deliver_policy": "last_per_subject", "ack_policy": "none",
		"idle_heartbeat": int64(heartbeat), "inactive_threshold": int64(inactive),
	}})
	if err != nil {
		return err
	}
	rctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	m, err := nc.RequestWithContext(rctx, "$JS.API.CONSUMER.CREATE."+r.Stream+"."+AttestConsumer+"."+AttestFilter, body)
	if err != nil {
		return fmt.Errorf("attestations: create %s: %w", AttestConsumer, err)
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
		return fmt.Errorf("attestations: create %s: %w", AttestConsumer, err)
	}
	switch {
	case resp.Error != nil && resp.Error.ErrCode == errConsumerExists:
		return fmt.Errorf("%w: %s", errAttestStale, resp.Error.Desc)
	case resp.Error != nil:
		return fmt.Errorf("attestations: create %s: %d %s", AttestConsumer, resp.Error.Code, resp.Error.Desc)
	case resp.Delivered.Consumer > 0 || resp.Config == nil || resp.Config.DeliverSubject != deliver:
		return fmt.Errorf("%w: it delivered %d before", errAttestStale, resp.Delivered.Consumer)
	}
	return nil
}
