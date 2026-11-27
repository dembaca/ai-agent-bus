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

// MembersConsumer is the bridge's own consumer of the memberships (RT-6.37,
// RT-6.42 step 11), made by the bridge at every start. The operator's
// bridge_presence consumer carries only presence.*.*: a membership written
// while the bridge was down, or before a restart, must still reach the
// snapshot that the history read checks.
const MembersConsumer = "bridge_members"

// MembersFilter is the consumer's filter. The bridge's create right names the
// consumer and the filter ($JS.API.CONSUMER.CREATE.PRESENCE.bridge_members.
// presence.member.*.*): it can make or change no other consumer.
const MembersFilter = "presence.member.*.*"

const (
	membersHeartbeat = 5 * time.Second
	membersInactive  = 30 * time.Second
)

var errMembersStale = errors.New("memberships: a consumer of an earlier start")

// MembersReader reads the memberships into a snapshot through a consumer it
// makes itself at every start: last per subject, so every channel and name the
// stream still holds is replayed once and a restart loses none; ack none, an
// idle heartbeat, gone 30 s after nobody listens. It holds no delete right: a
// consumer of that name still there (a restart within the threshold) makes the
// create fail, and the reader waits for it to go.
//
// The stream's own time stamps each record and the consumer's creation seeds
// the snapshot's clock, so a membership replayed after a start ages as it
// should: a read needs one younger than MemberKeep.
type MembersReader struct {
	Log     *slog.Logger
	Deliver string // deliver.bridge.members: the stem of the delivery subject
	Stream  string // "PRESENCE"
	Into    *Snapshot
	Ready   chan struct{} // closed once the first consumer is made (tests wait on it)
	// CaughtUp is closed once the first replay is read (tests wait on it).
	CaughtUp chan struct{}
	// Heartbeat and Inactive set the idle heartbeat (5 s) and the inactive
	// threshold (30 s); the tests shorten them.
	Heartbeat, Inactive time.Duration
}

func (r *MembersReader) beats() (heartbeat, inactive time.Duration) {
	heartbeat, inactive = r.Heartbeat, r.Inactive
	if heartbeat == 0 {
		heartbeat = membersHeartbeat
	}
	if inactive == 0 {
		inactive = membersInactive
	}
	return heartbeat, inactive
}

// Run makes consumers and reads them until ctx ends.
func (r *MembersReader) Run(ctx context.Context, nc *nats.Conn) error {
	for ctx.Err() == nil {
		err := r.consume(ctx, nc)
		if err == nil || ctx.Err() != nil {
			continue
		}
		wait := time.Second
		if errors.Is(err, errMembersStale) {
			heartbeat, inactive := r.beats()
			wait = inactive + heartbeat
			r.Log.Warn("memberships: the consumer of an earlier start is still there; waiting for it to go",
				"wait", wait.String(), "error", err)
		} else {
			r.Log.Error("memberships: the records cannot be read; trying again", "error", err)
		}
		select {
		case <-ctx.Done():
		case <-time.After(wait):
		}
	}
	return nil
}

// consume makes one consumer and reads it until it goes silent or ctx ends.
func (r *MembersReader) consume(ctx context.Context, nc *nats.Conn) error {
	nonce := make([]byte, 8)
	if _, err := rand.Read(nonce); err != nil {
		return err
	}
	deliver := r.Deliver + "." + hex.EncodeToString(nonce)
	ch := make(chan *nats.Msg, 256)
	sub, err := nc.ChanSubscribe(deliver, ch)
	if err != nil {
		return fmt.Errorf("memberships: subscribe %s: %w", deliver, err)
	}
	defer func() { _ = sub.Unsubscribe() }()
	if err := nc.Flush(); err != nil {
		return fmt.Errorf("memberships: subscribe %s: %w", deliver, err)
	}
	if err := r.create(ctx, nc, deliver); err != nil {
		return err // errMembersStale: Run waits the threshold, unsubscribed
	}
	if r.Ready != nil {
		select {
		case <-r.Ready:
		default:
			close(r.Ready)
		}
	}
	prefix := "$JS.ACK." + r.Stream + "." + MembersConsumer + "."
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
			r.Log.Warn("memberships: the consumer went silent; making a new one")
			return nil
		case m := <-ch:
			silence.Reset(3 * heartbeat)
			if m.Header.Get("Status") == "100" && strings.Contains(m.Header.Get("Description"), "Idle") {
				if seen, err := strconv.ParseUint(m.Header.Get("Nats-Last-Consumer"), 10, 64); err == nil && seen != last {
					r.Log.Warn("memberships: a record was lost on the way; making a new consumer")
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
				r.Log.Warn("memberships: a record was lost on the way; making a new consumer")
				return nil
			}
			last = md.Sequence.Consumer
			r.one(m, md.Timestamp, md.Sequence.Stream)
			if md.NumPending == 0 {
				caught()
			}
		}
	}
}

// one records one membership: its subject names the channel and the agent, its
// body must be a membership v1. A record that does not decode names none.
func (r *MembersReader) one(m *nats.Msg, at time.Time, seq uint64) {
	channel, name, ok := Membership(m.Subject)
	if !ok {
		r.Log.Warn("memberships: unexpected subject", "subject", m.Subject)
		return
	}
	if err := DecodeMember(m.Data); err != nil {
		r.Log.Warn("memberships: a record that does not decode", "subject", m.Subject, "error", err)
		return
	}
	r.Into.ObservedMember(channel, name, at, seq)
}

// create makes the consumer through the create API with the filter in the
// subject, the form the bridge's right names.
func (r *MembersReader) create(ctx context.Context, nc *nats.Conn, deliver string) error {
	heartbeat, inactive := r.beats()
	body, err := json.Marshal(map[string]any{"stream_name": r.Stream, "action": "create", "config": map[string]any{
		"name": MembersConsumer, "filter_subject": MembersFilter, "deliver_subject": deliver,
		"deliver_policy": "last_per_subject", "ack_policy": "none",
		"idle_heartbeat": int64(heartbeat), "inactive_threshold": int64(inactive),
	}})
	if err != nil {
		return err
	}
	rctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	m, err := nc.RequestWithContext(rctx, "$JS.API.CONSUMER.CREATE."+r.Stream+"."+MembersConsumer+"."+MembersFilter, body)
	if err != nil {
		return fmt.Errorf("memberships: create %s: %w", MembersConsumer, err)
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
		return fmt.Errorf("memberships: create %s: %w", MembersConsumer, err)
	}
	switch {
	case resp.Error != nil && resp.Error.ErrCode == errConsumerExists:
		return fmt.Errorf("%w: %s", errMembersStale, resp.Error.Desc)
	case resp.Error != nil:
		return fmt.Errorf("memberships: create %s: %d %s", MembersConsumer, resp.Error.Code, resp.Error.Desc)
	case resp.Delivered.Consumer > 0 || resp.Config == nil || resp.Config.DeliverSubject != deliver:
		return fmt.Errorf("%w: it delivered %d before", errMembersStale, resp.Delivered.Consumer)
	}
	// The server's own time, before the first record is read: an old membership
	// replayed after a start must not look fresh.
	r.Into.Seed(resp.Created)
	return nil
}
