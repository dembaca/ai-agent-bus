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

// PlanBConsumer is the bridge's own consumer of the plan B records
// (RT-6.39 step 5), made by the bridge at every start.
const PlanBConsumer = "bridge_planb"

// PlanBFilter is the consumer's filter. The bridge's create right names the
// consumer and the filter ($JS.API.CONSUMER.CREATE.PRESENCE.bridge_planb.
// presence.planb.*.v1): it can make or change no other consumer.
const PlanBFilter = "presence.planb.*.v1"

const (
	planBHeartbeat = 5 * time.Second
	planBInactive  = 30 * time.Second
	// JetStream's "consumer already exists" (another config).
	errConsumerExists = 10148
)

var errPlanBStale = errors.New("plan B: a consumer of an earlier start")

// PlanBReader reads every plan B record into Switches through a consumer it
// makes itself at every start: deliver all, so every record the stream still
// holds is replayed and a restart loses no switch; ack none, an idle
// heartbeat, gone 30 s after nobody listens. It holds no delete right: a
// consumer of that name still there (a restart within the threshold) makes
// the create fail, and the reader waits for it to go.
type PlanBReader struct {
	Log      *slog.Logger
	Deliver  string // deliver.bridge.planb: the stem of the delivery subject
	Stream   string // "PRESENCE"
	Into     *Switches
	Ready    chan struct{} // closed once the first consumer is made (tests wait on it)
	CaughtUp chan struct{} // closed once the first replay is read (tests wait on it)
	// Heartbeat and Inactive set the idle heartbeat (5 s) and the inactive
	// threshold (30 s); the tests shorten them.
	Heartbeat, Inactive time.Duration
}

func (r *PlanBReader) beats() (heartbeat, inactive time.Duration) {
	heartbeat, inactive = r.Heartbeat, r.Inactive
	if heartbeat == 0 {
		heartbeat = planBHeartbeat
	}
	if inactive == 0 {
		inactive = planBInactive
	}
	return heartbeat, inactive
}

// Run makes consumers and reads them until ctx ends.
func (r *PlanBReader) Run(ctx context.Context, nc *nats.Conn) error {
	for ctx.Err() == nil {
		err := r.consume(ctx, nc)
		if err == nil || ctx.Err() != nil {
			continue
		}
		wait := time.Second
		if errors.Is(err, errPlanBStale) {
			heartbeat, inactive := r.beats()
			wait = inactive + heartbeat
			r.Log.Warn("plan B: the consumer of an earlier start is still there; waiting for it to go",
				"wait", wait.String(), "error", err)
		} else {
			r.Log.Error("plan B: the records cannot be read; trying again", "error", err)
		}
		select {
		case <-ctx.Done():
		case <-time.After(wait):
		}
	}
	return nil
}

func (r *PlanBReader) consume(ctx context.Context, nc *nats.Conn) error {
	nonce := make([]byte, 8)
	if _, err := rand.Read(nonce); err != nil {
		return err
	}
	deliver := r.Deliver + "." + hex.EncodeToString(nonce)
	ch := make(chan *nats.Msg, 256)
	sub, err := nc.ChanSubscribe(deliver, ch)
	if err != nil {
		return fmt.Errorf("plan B: subscribe %s: %w", deliver, err)
	}
	defer func() { _ = sub.Unsubscribe() }()
	if err := nc.Flush(); err != nil {
		return fmt.Errorf("plan B: subscribe %s: %w", deliver, err)
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
	prefix := "$JS.ACK." + r.Stream + "." + PlanBConsumer + "."
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
			r.Log.Warn("plan B: the consumer went silent; making a new one")
			return nil
		case m := <-ch:
			silence.Reset(3 * heartbeat)
			if m.Header.Get("Status") == "100" && strings.Contains(m.Header.Get("Description"), "Idle") {
				if seen, err := strconv.ParseUint(m.Header.Get("Nats-Last-Consumer"), 10, 64); err == nil && seen != last {
					r.Log.Warn("plan B: a record was lost on the way; making a new consumer")
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
				r.Log.Warn("plan B: a record was lost on the way; making a new consumer")
				return nil
			}
			last = md.Sequence.Consumer
			r.one(m, md.Timestamp)
			if md.NumPending == 0 {
				caught()
			}
		}
	}
}

func (r *PlanBReader) one(m *nats.Msg, at time.Time) {
	channel, ok := PlanBSubject(m.Subject)
	if !ok {
		return
	}
	pieces, err := DecodePlanB(m.Data, channel)
	if err != nil {
		// What does not decode switches nothing on: the old path.
		r.Log.Warn("plan B: a record that does not decode, its pieces off", "subject", m.Subject, "error", err)
	}
	r.Into.Observed(channel, pieces, at)
}

// create makes the consumer through the create API with the filter in the
// subject, the form the bridge's right names.
func (r *PlanBReader) create(ctx context.Context, nc *nats.Conn, deliver string) error {
	heartbeat, inactive := r.beats()
	body, err := json.Marshal(map[string]any{"stream_name": r.Stream, "action": "create", "config": map[string]any{
		"name": PlanBConsumer, "filter_subject": PlanBFilter, "deliver_subject": deliver,
		"deliver_policy": "all", "ack_policy": "none",
		"idle_heartbeat": int64(heartbeat), "inactive_threshold": int64(inactive),
	}})
	if err != nil {
		return err
	}
	rctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	m, err := nc.RequestWithContext(rctx, "$JS.API.CONSUMER.CREATE."+r.Stream+"."+PlanBConsumer+"."+PlanBFilter, body)
	if err != nil {
		return fmt.Errorf("plan B: create %s: %w", PlanBConsumer, err)
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
		return fmt.Errorf("plan B: create %s: %w", PlanBConsumer, err)
	}
	switch {
	case resp.Error != nil && resp.Error.ErrCode == errConsumerExists:
		return fmt.Errorf("%w: %s", errPlanBStale, resp.Error.Desc)
	case resp.Error != nil:
		return fmt.Errorf("plan B: create %s: %d %s", PlanBConsumer, resp.Error.Code, resp.Error.Desc)
	case resp.Delivered.Consumer > 0 || resp.Config == nil || resp.Config.DeliverSubject != deliver:
		return fmt.Errorf("%w: it delivered %d before", errPlanBStale, resp.Delivered.Consumer)
	}
	return nil
}
