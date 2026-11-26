package consumers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
)

// safe returns what the server keeps of the safe configuration of a stream.
func safe(name string) map[string]any {
	g := map[string]any{
		"retention": "limits", "discard": "old", "no_ack": true, "deny_delete": true, "deny_purge": true,
		"allow_direct": false, "allow_rollup_hdrs": false, "mirror_direct": false,
		"max_bytes": float64(1 << 30), "max_msg_size": float64(65536), "max_age": float64(MaxHistory), "duplicate_window": float64(DuplicateWindow),
		"subjects": []any{"bus.>"},
	}
	if name == PresenceStream {
		g["max_age"], g["max_msgs_per_subject"], g["subjects"] = float64(PresenceMaxAge), float64(1), []any{"presence.>"}
	}
	if name == StatsStream {
		g["max_age"], g["max_msgs_per_subject"], g["subjects"] = float64(StatsMaxAge), float64(500), []any{"stats.>"}
		g["max_bytes"], g["max_msg_size"], g["storage"] = float64(64<<20), float64(8192), "file"
	}
	return g
}

func with(name string, edit func(map[string]any)) map[string]any {
	g := maps.Clone(safe(name))
	edit(g)
	return g
}

func TestCheckStream(t *testing.T) {
	subject := map[string]string{BusStream: "bus.>", PresenceStream: "presence.>", StatsStream: "stats.>"}
	for name, c := range map[string]struct {
		stream string
		got    map[string]any
		unsafe bool
	}{
		"BUS safe":                  {BusStream, safe(BusStream), false},
		"PRESENCE safe":             {PresenceStream, safe(PresenceStream), false},
		"BUS shorter max_age":       {BusStream, with(BusStream, func(g map[string]any) { g["max_age"] = float64(time.Hour) }), false},
		"empty transform is no set": {BusStream, with(BusStream, func(g map[string]any) { g["subject_transform"] = []any{} }), false},
		"BUS extra subject":         {BusStream, with(BusStream, func(g map[string]any) { g["subjects"] = []any{"bus.>", "deliver.>"} }), true},
		"BUS other subject":         {BusStream, with(BusStream, func(g map[string]any) { g["subjects"] = []any{"deliver.>"} }), true},
		"BUS no subjects":           {BusStream, with(BusStream, func(g map[string]any) { delete(g, "subjects") }), true},
		"BUS narrower subject":      {BusStream, with(BusStream, func(g map[string]any) { g["subjects"] = []any{"bus.ch.>"} }), true},
		"PRESENCE extra subject":    {PresenceStream, with(PresenceStream, func(g map[string]any) { g["subjects"] = []any{"presence.>", "bus.>"} }), true},
		"BUS subject transform": {BusStream, with(BusStream, func(g map[string]any) {
			g["subject_transform"] = []any{map[string]any{"src": "bus.>", "dest": "deliver.>"}}
		}), true},
		"BUS duplicate window 2 min":    {BusStream, with(BusStream, func(g map[string]any) { g["duplicate_window"] = float64(2 * time.Minute) }), true},
		"PRESENCE duplicate window 1 s": {PresenceStream, with(PresenceStream, func(g map[string]any) { g["duplicate_window"] = float64(time.Second) }), true},
		"BUS retention interest":        {BusStream, with(BusStream, func(g map[string]any) { g["retention"] = "interest" }), true},
		"BUS retention workqueue":       {BusStream, with(BusStream, func(g map[string]any) { g["retention"] = "workqueue" }), true},
		"BUS no retention":              {BusStream, with(BusStream, func(g map[string]any) { delete(g, "retention") }), true},
		"BUS max_age unlimited":         {BusStream, with(BusStream, func(g map[string]any) { g["max_age"] = float64(0) }), true},
		"BUS max_age 15 days":           {BusStream, with(BusStream, func(g map[string]any) { g["max_age"] = float64(15 * 24 * time.Hour) }), true},
		"PRESENCE max_age unlimited":    {PresenceStream, with(PresenceStream, func(g map[string]any) { g["max_age"] = float64(0) }), true},
		"PRESENCE max_age 25 hours":     {PresenceStream, with(PresenceStream, func(g map[string]any) { g["max_age"] = float64(25 * time.Hour) }), true},
		"BUS acknowledges":              {BusStream, with(BusStream, func(g map[string]any) { g["no_ack"] = false }), true},
		"BUS allows deletes":            {BusStream, with(BusStream, func(g map[string]any) { g["deny_delete"] = false }), true},
		"BUS allows purges":             {BusStream, with(BusStream, func(g map[string]any) { g["deny_purge"] = false }), true},
		"BUS allows direct":             {BusStream, with(BusStream, func(g map[string]any) { g["allow_direct"] = true }), true},
		"BUS mirror direct":             {BusStream, with(BusStream, func(g map[string]any) { g["mirror_direct"] = true }), true},
		"BUS is a mirror":               {BusStream, with(BusStream, func(g map[string]any) { g["mirror"] = map[string]any{"name": "X"} }), true},
		"BUS has sources":               {BusStream, with(BusStream, func(g map[string]any) { g["sources"] = []any{map[string]any{"name": "X"}} }), true},
		"BUS republishes":               {BusStream, with(BusStream, func(g map[string]any) { g["republish"] = map[string]any{"src": ">", "dest": "x.>"} }), true},
		"BUS no size limit":             {BusStream, with(BusStream, func(g map[string]any) { g["max_bytes"] = float64(-1) }), true},
		"BUS no message size limit":     {BusStream, with(BusStream, func(g map[string]any) { g["max_msg_size"] = float64(-1) }), true},
		"BUS discards new":              {BusStream, with(BusStream, func(g map[string]any) { g["discard"] = "new" }), true},
		"PRESENCE keeps many":           {PresenceStream, with(PresenceStream, func(g map[string]any) { g["max_msgs_per_subject"] = float64(5) }), true},
		"PRESENCE keeps all":            {PresenceStream, with(PresenceStream, func(g map[string]any) { delete(g, "max_msgs_per_subject") }), true},
		// The STATS lines (RT-6.43): 30 days, at most 500 per agent's subject.
		"STATS safe":              {StatsStream, safe(StatsStream), false},
		"STATS keeps fewer":       {StatsStream, with(StatsStream, func(g map[string]any) { g["max_msgs_per_subject"] = float64(100) }), false},
		"STATS keeps 501":         {StatsStream, with(StatsStream, func(g map[string]any) { g["max_msgs_per_subject"] = float64(501) }), true},
		"STATS keeps all":         {StatsStream, with(StatsStream, func(g map[string]any) { delete(g, "max_msgs_per_subject") }), true},
		"STATS max_age 31 days":   {StatsStream, with(StatsStream, func(g map[string]any) { g["max_age"] = float64(31 * 24 * time.Hour) }), true},
		"STATS max_age unlimited": {StatsStream, with(StatsStream, func(g map[string]any) { g["max_age"] = float64(0) }), true},
		"STATS other subject":     {StatsStream, with(StatsStream, func(g map[string]any) { g["subjects"] = []any{"bus.>"} }), true},
		"STATS discards new":      {StatsStream, with(StatsStream, func(g map[string]any) { g["discard"] = "new" }), true},
		"STATS acknowledges":      {StatsStream, with(StatsStream, func(g map[string]any) { g["no_ack"] = false }), true},
		"STATS allows direct":     {StatsStream, with(StatsStream, func(g map[string]any) { g["allow_direct"] = true }), true},
		"STATS no size limit":     {StatsStream, with(StatsStream, func(g map[string]any) { g["max_bytes"] = float64(-1) }), true},
		"STATS larger messages":   {StatsStream, with(StatsStream, func(g map[string]any) { g["max_msg_size"] = float64(16384) }), true},
		"STATS smaller messages":  {StatsStream, with(StatsStream, func(g map[string]any) { g["max_msg_size"] = float64(4096) }), true},
		"STATS no message size":   {StatsStream, with(StatsStream, func(g map[string]any) { delete(g, "max_msg_size") }), true},
		"STATS in memory":         {StatsStream, with(StatsStream, func(g map[string]any) { g["storage"] = "memory" }), true},
		"STATS no storage":        {StatsStream, with(StatsStream, func(g map[string]any) { delete(g, "storage") }), true},
	} {
		t.Run(name, func(t *testing.T) {
			err := checkStream(c.stream, subject[c.stream], c.got)
			if c.unsafe != errors.Is(err, ErrUnsafeStream) || (!c.unsafe && err != nil) {
				t.Errorf("unsafe %v, got %v", c.unsafe, err)
			}
		})
	}
}

// The request the service sends is accepted by its own check.
func TestTheCreationRequestsPassTheCheck(t *testing.T) {
	for stream, c := range map[string]struct{ json, subject string }{
		BusStream:      {BusStreamJSON("bus", 0), "bus.>"},
		PresenceStream: {PresenceStreamJSON("presence", 0), "presence.>"},
		StatsStream:    {StatsStreamJSON("stats", 0), "stats.>"},
	} {
		var got map[string]any
		if err := json.Unmarshal([]byte(c.json), &got); err != nil {
			t.Fatal(err)
		}
		if err := checkStream(stream, c.subject, got); err != nil {
			t.Errorf("%s: %v", stream, err)
		}
	}
}

func cfg(edit func(*nats.ConsumerConfig)) nats.ConsumerConfig {
	c := nats.ConsumerConfig{
		Durable: "a_ch_home", DeliverSubject: "deliver.a.ch.home", AckPolicy: nats.AckNonePolicy,
		FilterSubject: "bus.ch.home.*", InactiveThreshold: time.Hour,
		Metadata: map[string]string{"agent": "a", "owner": "o"},
	}
	if edit != nil {
		edit(&c)
	}
	return c
}

func TestSame(t *testing.T) {
	multi := func(f ...string) func(*nats.ConsumerConfig) {
		return func(c *nats.ConsumerConfig) { c.FilterSubject, c.FilterSubjects = "", f }
	}
	last := func(c *nats.ConsumerConfig) { c.DeliverPolicy = nats.DeliverLastPerSubjectPolicy }
	for name, c := range map[string]struct {
		have, want nats.ConsumerConfig
		same       bool
	}{
		"equal":                         {cfg(nil), cfg(nil), true},
		"server metadata is ignored":    {cfg(func(c *nats.ConsumerConfig) { c.Metadata["_nats.ver"] = "2.15" }), cfg(nil), true},
		"one filter as a list":          {cfg(multi("bus.ch.home.*")), cfg(nil), true},
		"filters in another order":      {cfg(multi("a.*", "b.*")), cfg(multi("b.*", "a.*")), true},
		"other filter, same count":      {cfg(nil), cfg(func(c *nats.ConsumerConfig) { c.FilterSubject = "bus.ch.other.*" }), false},
		"other filters, same count":     {cfg(multi("a.*", "b.*")), cfg(multi("a.*", "c.*")), false},
		"more filters":                  {cfg(multi("a.*", "b.*")), cfg(multi("a.*", "b.*", "c.*")), false},
		"a filter twice, same count":    {cfg(multi("a.*", "a.*")), cfg(multi("a.*", "b.*")), false},
		"other deliver subject":         {cfg(nil), cfg(func(c *nats.ConsumerConfig) { c.DeliverSubject = "deliver.b.ch.home" }), false},
		"explicit ack":                  {cfg(func(c *nats.ConsumerConfig) { c.AckPolicy = nats.AckExplicitPolicy }), cfg(nil), false},
		"flow control":                  {cfg(func(c *nats.ConsumerConfig) { c.FlowControl = true }), cfg(nil), false},
		"heartbeat":                     {cfg(func(c *nats.ConsumerConfig) { c.Heartbeat = time.Second }), cfg(nil), false},
		"deliver group":                 {cfg(func(c *nats.ConsumerConfig) { c.DeliverGroup = "g" }), cfg(nil), false},
		"other inactivity":              {cfg(func(c *nats.ConsumerConfig) { c.InactiveThreshold = time.Minute }), cfg(nil), false},
		"other agent":                   {cfg(func(c *nats.ConsumerConfig) { c.Metadata["agent"] = "b" }), cfg(nil), false},
		"other owner":                   {cfg(func(c *nats.ConsumerConfig) { c.Metadata["owner"] = "p" }), cfg(nil), false},
		"other channels recorded":       {cfg(func(c *nats.ConsumerConfig) { c.Metadata["channels"] = "a" }), cfg(func(c *nats.ConsumerConfig) { c.Metadata["channels"] = "b" }), false},
		"channels not yet recorded":     {cfg(nil), cfg(func(c *nats.ConsumerConfig) { c.Metadata["channels"] = "" }), true},
		"presence wants last per subj.": {cfg(nil), cfg(last), false},
		"presence has last per subject": {cfg(last), cfg(last), true},
		"start position of a channel":   {cfg(func(c *nats.ConsumerConfig) { c.DeliverPolicy = nats.DeliverAllPolicy }), cfg(nil), true},
	} {
		t.Run(name, func(t *testing.T) {
			if got := same(c.have, c.want); got != c.same {
				t.Errorf("same = %v", got)
			}
		})
	}
}

func TestTheLocksMapDoesNotGrow(t *testing.T) {
	m := &Manager{locks: map[string]*agentLock{}}
	var wg sync.WaitGroup
	for i := range 500 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			// Two goroutines per agent contend for one lock.
			unlock := m.lock(fmt.Sprintf("agent%d", i/2))
			time.Sleep(time.Millisecond)
			unlock()
		}()
	}
	wg.Wait()
	m.lmu.Lock()
	n := len(m.locks)
	m.lmu.Unlock()
	if n != 0 {
		t.Errorf("%d entries left after all were released", n)
	}
	// The lock still excludes: the second holder waits for the first.
	unlock := m.lock("x")
	got := make(chan struct{})
	go func() { u := m.lock("x"); close(got); u() }()
	select {
	case <-got:
		t.Fatal("two holders at once")
	case <-time.After(50 * time.Millisecond):
	}
	unlock()
	<-got
}

// The STATS stream's creation request says what the spec says (RT-6.43): its
// own subjects, file storage, 30 days, 64 MiB, 8 KiB per line, 500 per
// subject, the oldest discarded.
func TestTheStatsStreamDefinition(t *testing.T) {
	var got map[string]any
	if err := json.Unmarshal([]byte(StatsStreamJSON("st", 0)), &got); err != nil {
		t.Fatal(err)
	}
	for k, want := range map[string]any{
		"name": "STATS", "storage": "file", "retention": "limits", "discard": "old", "no_ack": true,
		"max_age": float64(30 * 24 * time.Hour), "max_bytes": float64(64 << 20), "max_msg_size": float64(8192),
		"max_msgs_per_subject": float64(500), "num_replicas": float64(1),
	} {
		if got[k] != want {
			t.Errorf("%s = %v, want %v", k, got[k], want)
		}
	}
	if subs, _ := got["subjects"].([]any); len(subs) != 1 || subs[0] != "st.>" {
		t.Errorf("subjects %v", got["subjects"])
	}
	if err := json.Unmarshal([]byte(StatsStreamJSON("st", 1<<20)), &got); err != nil || got["max_bytes"] != float64(1<<20) {
		t.Errorf("max_bytes %v, %v", got["max_bytes"], err)
	}
}

// The STATS prefix is checked like the other two (RT-6.43).
func TestTheStatsPrefixMustDiffer(t *testing.T) {
	for _, stats := range []string{"bus", "presence", DeliverPrefix, "a.b"} {
		if _, err := New(nil, Config{Bus: "bus", Presence: "presence", Stats: stats}); err == nil {
			t.Errorf("stats prefix %q accepted", stats)
		}
	}
}

// match is NATS's subject match of a filter (with * and >) against a subject.
func match(filter, subject string) bool {
	f, s := strings.Split(filter, "."), strings.Split(subject, ".")
	for i, tok := range f {
		switch {
		case tok == ">":
			return len(s) > i
		case i >= len(s):
			return false
		case tok != "*" && tok != s[i]:
			return false
		}
	}
	return len(f) == len(s)
}

// An agent's consumers never deliver an ask record (RT-6.43), its own or
// another agent's in a channel it shares: the console reads them, agents do not.
func TestNoAgentConsumerDeliversAnAskRecord(t *testing.T) {
	m := &Manager{cfg: Config{Bus: "bus", Presence: "presence", Stats: "stats"}}
	for _, w := range m.desired("agent-a", "alice", []string{"home", "ga"}) {
		for _, f := range w.filters {
			for _, s := range []string{"presence.ask.home.agent-b", "presence.ask.ga.agent-a", "stats.agent-b"} {
				if match(f, s) {
					t.Errorf("consumer %s (filter %s) delivers %s", w.name, f, s)
				}
			}
		}
	}
	if !match("presence.*.*", "presence.alice.agent-b") || match("presence.*.*", "presence.ask.home.agent-b") {
		t.Fatal("matcher broken")
	}
}

// A JetStream user without the STATS rights (an upgrade that skipped them)
// gets an error that names the two rights, not a bare timeout (RT-6.43).
func TestMissingStatsRightsAreNamed(t *testing.T) {
	var allow []string
	for _, s := range []string{BusStream, PresenceStream} {
		allow = append(allow, "$JS.API.STREAM.CREATE."+s, "$JS.API.STREAM.INFO."+s)
	}
	srv, err := server.NewServer(&server.Options{Host: "127.0.0.1", Port: -1, JetStream: true,
		StoreDir: t.TempDir(), NoLog: true, NoSigs: true,
		Users: []*server.User{{Username: "js", Password: "pwd", Permissions: &server.Permissions{
			Publish:   &server.SubjectPermission{Allow: allow},
			Subscribe: &server.SubjectPermission{Allow: []string{"_INBOX.>"}},
		}}}})
	if err != nil {
		t.Fatal(err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(5 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(srv.Shutdown)
	nc, err := nats.Connect(srv.ClientURL(), nats.UserInfo("js", "pwd"), nats.ErrorHandler(func(*nats.Conn, *nats.Subscription, error) {}))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	m, err := New(nc, Config{Bus: "bus", Presence: "presence"})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	err = m.EnsureStreams(ctx)
	if err == nil || !strings.Contains(err.Error(), "$JS.API.STREAM.INFO.STATS") || !strings.Contains(err.Error(), "$JS.API.STREAM.CREATE.STATS") {
		t.Fatalf("EnsureStreams: %v", err)
	}
	t.Log(err)
}

// RT-6.34: an agent's presence consumer delivers its own owner's notice and no
// other owner's.
func TestThePresenceConsumerDeliversItsOwnersNotice(t *testing.T) {
	m := &Manager{cfg: Config{Bus: "bus", Presence: "presence", Stats: "stats"}}
	var filters []string
	for _, w := range m.desired("agent-a", "alice", []string{"home"}) {
		if w.presence {
			filters = w.filters
		}
	}
	if len(filters) == 0 {
		t.Fatal("no presence consumer")
	}
	ok := false
	for _, f := range filters {
		if match(f, "presence.notice.alice.v1") {
			ok = true
		}
		if match(f, "presence.notice.bob.v1") {
			t.Errorf("filter %s delivers another owner's notice", f)
		}
	}
	if !ok {
		t.Fatalf("nothing delivers the owner's notice: %v", filters)
	}
	// The subject token form: a dotted anchor is written with "~".
	filters = nil
	for _, w := range m.desired("agent-a", "alice.example", []string{"home"}) {
		if w.presence {
			filters = w.filters
		}
	}
	ok = false
	for _, f := range filters {
		if match(f, "presence.notice.alice~example.v1") {
			ok = true
		}
	}
	if !ok {
		t.Fatalf("nothing delivers a dotted owner's notice: %v", filters)
	}
}
