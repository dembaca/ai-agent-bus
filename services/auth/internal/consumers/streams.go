// Package consumers is the login service's consumer management (ADR-014,
// point 16): it creates the two streams of the bus with the settings the
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

// Names of the two streams.
const (
	BusStream      = "BUS"
	PresenceStream = "PRESENCE"
)

// Limits and defaults of the streams.
const (
	// MaxHistory is how far back a channel's messages are kept, and the
	// furthest back a consumer may start.
	MaxHistory = 14 * 24 * time.Hour
	// PresenceMaxAge is how long an orphaned presence subject stays.
	PresenceMaxAge = 24 * time.Hour

	DefaultBusMaxBytes      int64 = 1 << 30
	DefaultPresenceMaxBytes int64 = 16 << 20
)

const (
	busMaxMsgSize      = 65536
	presenceMaxMsgSize = 4096
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

func streamJSON(name, subject, limits string) string {
	return fmt.Sprintf(`{"name":%q,"subjects":[%q],"retention":"limits","storage":"file","discard":"old",
%s,"num_replicas":1,"no_ack":true,"duplicate_window":100000000,
"allow_direct":false,"mirror_direct":false,"deny_delete":true,"deny_purge":true,"allow_rollup_hdrs":false,
"allow_msg_ttl":false,"allow_msg_counter":false,"allow_atomic":false,"allow_msg_schedules":false,"allow_batched":false}`,
		name, subject, limits)
}

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
		if got[k] != nil {
			bad = append(bad, k+" is set")
		}
	}
	if num("max_bytes") <= 0 {
		bad = append(bad, "no max_bytes")
	}
	if num("max_msg_size") <= 0 {
		bad = append(bad, "no max_msg_size")
	}
	if num("max_age") <= 0 {
		bad = append(bad, "no max_age")
	}
	if got["discard"] != "old" {
		bad = append(bad, "discard is not old")
	}
	if name == PresenceStream && num("max_msgs_per_subject") != 1 {
		bad = append(bad, "max_msgs_per_subject is not 1")
	}
	has := false
	if subs, ok := got["subjects"].([]any); ok {
		for _, s := range subs {
			has = has || s == subject
		}
	}
	if !has {
		bad = append(bad, "subject "+subject+" is not bound")
	}
	if len(bad) > 0 {
		return fmt.Errorf("%w: %s: %s", ErrUnsafeStream, name, strings.Join(bad, ", "))
	}
	return nil
}
