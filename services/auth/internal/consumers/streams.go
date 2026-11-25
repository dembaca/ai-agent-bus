// Package consumers is the login service's consumer management (ADR-014,
// point 16): it creates the three streams of the bus with the settings the
// permission model was proven with, makes the push consumers an agent reads
// from, and removes them again at sign-off, when a channel is dropped from
// the credential and when the emergency brake holds the agent's owner.
//
// The connection it works on is the only holder of JetStream API rights, in
// the tenant account. Nothing here logs a token.
package consumers

import (
	"errors"
	"fmt"
	"strings"
	"time"
)

// Names of the three streams.
const (
	BusStream      = "BUS"
	PresenceStream = "PRESENCE"
	// StatsStream keeps the agents' STATS lines on <stats>.<agent>, apart
	// from the chat's BUS stream (RT-6.43): no channel consumer delivers them.
	StatsStream = "STATS"
)

// DefaultStatsPrefix is the STATS lines' subject prefix when none is set
// (AUTH_STATS_PREFIX).
const DefaultStatsPrefix = "stats"

// Limits and defaults of the streams.
const (
	// MaxHistory is how far back a channel's messages are kept, and the
	// furthest back a consumer may start.
	MaxHistory = 14 * 24 * time.Hour
	// PresenceMaxAge is how long an orphaned presence subject stays.
	PresenceMaxAge = 24 * time.Hour

	// StatsMaxAge is how long a STATS line stays: the 7-day reports with room.
	StatsMaxAge = 30 * 24 * time.Hour
	// StatsMaxMsgsPerSubject is how many lines one agent's subject keeps: a
	// name that floods pushes out only its own oldest lines.
	StatsMaxMsgsPerSubject = 500

	DefaultBusMaxBytes      int64 = 1 << 30
	DefaultPresenceMaxBytes int64 = 16 << 20
	DefaultStatsMaxBytes    int64 = 64 << 20
)

const (
	busMaxMsgSize      = 65536
	presenceMaxMsgSize = 4096
	statsMaxMsgSize    = 8192
)

// BusStreamJSON is the creation request of the BUS stream for the subject
// prefix: the configuration native_sender_test.go proves. maxBytes <= 0 takes
// DefaultBusMaxBytes.
func BusStreamJSON(prefix string, maxBytes int64) string {
	if maxBytes <= 0 {
		maxBytes = DefaultBusMaxBytes
	}
	return streamJSON(BusStream, prefix+".>", fmt.Sprintf(`"max_age":%d,"max_bytes":%d,"max_msg_size":%d`,
		int64(MaxHistory), maxBytes, busMaxMsgSize))
}

// PresenceStreamJSON is the creation request of the PRESENCE stream: one
// message per subject. maxBytes <= 0 takes DefaultPresenceMaxBytes.
func PresenceStreamJSON(prefix string, maxBytes int64) string {
	if maxBytes <= 0 {
		maxBytes = DefaultPresenceMaxBytes
	}
	return streamJSON(PresenceStream, prefix+".>", fmt.Sprintf(`"max_age":%d,"max_msgs_per_subject":1,"max_bytes":%d,"max_msg_size":%d`,
		int64(PresenceMaxAge), maxBytes, presenceMaxMsgSize))
}

// StatsStreamJSON is the creation request of the STATS stream (RT-6.43): 30
// days, at most 500 lines per agent's subject, 8 KiB per line, the oldest
// discarded. maxBytes <= 0 takes DefaultStatsMaxBytes.
func StatsStreamJSON(prefix string, maxBytes int64) string {
	if maxBytes <= 0 {
		maxBytes = DefaultStatsMaxBytes
	}
	return streamJSON(StatsStream, prefix+".>", fmt.Sprintf(`"max_age":%d,"max_msgs_per_subject":%d,"max_bytes":%d,"max_msg_size":%d`,
		int64(StatsMaxAge), StatsMaxMsgsPerSubject, maxBytes, statsMaxMsgSize))
}

func streamJSON(name, subject, limits string) string {
	return fmt.Sprintf(`{"name":%q,"subjects":[%q],"retention":"limits","storage":"file","discard":"old",
%s,"num_replicas":1,"no_ack":true,"duplicate_window":%d,
"allow_direct":false,"mirror_direct":false,"deny_delete":true,"deny_purge":true,"allow_rollup_hdrs":false,
"allow_msg_ttl":false,"allow_msg_counter":false,"allow_atomic":false,"allow_msg_schedules":false,"allow_batched":false}`,
		name, subject, limits, DuplicateWindow.Nanoseconds())
}

// DuplicateWindow is the streams' message-id window: as short as works, since
// a guessed id suppresses another agent's message within it.
const DuplicateWindow = 100 * time.Millisecond

// ErrUnsafeStream: an existing stream differs from the safe configuration.
var ErrUnsafeStream = errors.New("stream configuration is unsafe")

// checkStream compares the configuration the server kept with the safety
// settings of the model. Any allow_* switch that is on, anything but
// no_ack, deny_delete, deny_purge, a size limit and discard old fails.
func checkStream(name, subject string, got map[string]any) error {
	var bad []string
	flag := func(k string) bool { v, _ := got[k].(bool); return v }
	num := func(k string) float64 { v, _ := got[k].(float64); return v }
	if !flag("no_ack") {
		bad = append(bad, "no_ack is off")
	}
	if !flag("deny_delete") {
		bad = append(bad, "deny_delete is off")
	}
	if !flag("deny_purge") {
		bad = append(bad, "deny_purge is off")
	}
	for k, v := range got {
		if (strings.HasPrefix(k, "allow_") || k == "mirror_direct") && v == true {
			bad = append(bad, k+" is on")
		}
	}
	for _, k := range []string{"mirror", "sources", "republish"} {
		if set(got[k]) {
			bad = append(bad, k+" is set")
		}
	}
	if num("max_bytes") <= 0 {
		bad = append(bad, "no max_bytes")
	}
	if num("max_msg_size") <= 0 {
		bad = append(bad, "no max_msg_size")
	}
	if got["retention"] != "limits" {
		bad = append(bad, "retention is not limits")
	}
	maxAge := time.Duration(num("max_age"))
	limit := MaxHistory
	switch name {
	case PresenceStream:
		limit = PresenceMaxAge
	case StatsStream:
		limit = StatsMaxAge
	}
	if maxAge <= 0 || maxAge > limit {
		bad = append(bad, fmt.Sprintf("max_age is not within 0 and %s", limit))
	}
	// The message-id window is the side channel's reach (ADR-014 point 16,
	// condition 1): no wider than the one this package creates.
	if dw := time.Duration(num("duplicate_window")); dw <= 0 || dw > DuplicateWindow {
		bad = append(bad, fmt.Sprintf("duplicate_window is not within 0 and %s", DuplicateWindow))
	}
	if got["discard"] != "old" {
		bad = append(bad, "discard is not old")
	}
	if name == PresenceStream && num("max_msgs_per_subject") != 1 {
		bad = append(bad, "max_msgs_per_subject is not 1")
	}
	if n := num("max_msgs_per_subject"); name == StatsStream && (n <= 0 || n > StatsMaxMsgsPerSubject) {
		bad = append(bad, fmt.Sprintf("max_msgs_per_subject is not within 1 and %d", StatsMaxMsgsPerSubject))
	}
	if set(got["subject_transform"]) {
		bad = append(bad, "subject_transform is set")
	}
	if subs, ok := got["subjects"].([]any); !ok || len(subs) != 1 || subs[0] != subject {
		bad = append(bad, "subjects are not exactly "+subject)
	}
	if len(bad) > 0 {
		return fmt.Errorf("%w: %s: %s", ErrUnsafeStream, name, strings.Join(bad, ", "))
	}
	return nil
}

// set says whether a JSON value is there and not empty.
func set(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case []any:
		return len(x) > 0
	case map[string]any:
		return len(x) > 0
	}
	return true
}
