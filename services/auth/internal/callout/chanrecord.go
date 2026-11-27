package callout

// The agent's channel record (ADR-014 phase 5 piece 1, RT-6.39 step 6): with
// the credential the login service writes, for the agent, the channels it
// granted and the chat's own id of each, from the channel directory. The
// session takes its channel ids from it instead of asking Mattermost. The
// record is the credential's side of the grant: it lists exactly the channels
// of the user JWT's permissions, and only the login service may publish it
// (an agent's publish right on the presence stream is its one 3-token subject).

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
)

// ChatProduct is the chat the ids are of.
const ChatProduct = "mattermost"

// ChatIDs gives the chat's own ids of channels (directory.Channels). A
// Channels that does not offer it makes no record.
type ChatIDs interface {
	ChatIDs(ctx context.Context, product string, names []string) (map[string]string, error)
}

// ChannelRecord is `<presence>.directory.<agent>.v1`: one entry per granted
// channel, with its Mattermost id when the directory has one.
type ChannelRecord struct {
	V     int    `json:"v"`
	Agent string `json:"agent"`
	// Channels: granted channel name -> Mattermost id; "" when the directory
	// holds none for it (the session then has no id for that channel).
	Channels map[string]string `json:"channels"`
	IAT      int64             `json:"iat"`
	Exp      int64             `json:"exp"`
}

func (s *Service) chanRecordSubject(agent string) string {
	return s.cfg.Subjects.Presence + ".directory." + agent + ".v1"
}

// channelRecord publishes the record for the granted channels; a service whose
// Channels has no ids makes none. An error refuses the login, like the
// attestation's: a credential without its record would look whole.
func (s *Service) channelRecord(ctx context.Context, a Attester, agent string, granted []string, iat, exp int64) error {
	ci, ok := s.channels.(ChatIDs)
	if !ok {
		return nil
	}
	names := append([]string(nil), granted...)
	sort.Strings(names)
	ids, err := ci.ChatIDs(ctx, ChatProduct, names)
	if err != nil {
		return err
	}
	rec := ChannelRecord{V: 1, Agent: agent, Channels: make(map[string]string, len(names)), IAT: iat, Exp: exp}
	for _, n := range names {
		rec.Channels[n] = ids[n]
	}
	body, err := json.Marshal(rec)
	if err != nil {
		return err
	}
	if err := a.Publish(ctx, s.chanRecordSubject(agent), body); err != nil {
		return fmt.Errorf("channel record: %w", err)
	}
	return nil
}
