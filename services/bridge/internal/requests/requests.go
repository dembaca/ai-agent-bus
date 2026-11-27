// Package requests is the bridge's side of the request scaffold (ADR-014
// phase 5): an agent asks for what is not a channel message (a history read,
// file parts, the header line) on <req>.<kind>.<agent>, and the bridge answers
// on deliver.<agent>.req.<request id>. Nothing is kept: a lost message fails
// the request, and this phase's answer says only that the piece is not built.
package requests

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"regexp"
	"strings"

	"github.com/nats-io/nats.go"
)

// kindOK says whether kind is one of the scaffold's: the history read, file
// parts and the header line (ADR-014 phase 5).
func kindOK(kind string) bool {
	return kind == "history" || kind == "file" || kind == "header"
}

// agentRE is an agent name as the bus writes it: the subject's last token.
var agentRE = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)

// requestRE is a request id: 16 characters of [a-z0-9].
var requestRE = regexp.MustCompile(`^[a-z0-9]{16}$`)

// Request is the body an agent publishes on <req>.<kind>.<agent>.
type Request struct {
	V       int             `json:"v"`
	Request string          `json:"request"`
	Kind    string          `json:"kind"`
	Payload json.RawMessage `json:"payload"`
}

// Answer is the body the bridge publishes on deliver.<agent>.req.<request id>.
type Answer struct {
	V       int    `json:"v"`
	Request string `json:"request"`
	Kind    string `json:"kind"`
	Error   string `json:"error"`
}

// notBuilt is this phase's answer: the scaffold answers, the pieces do not.
const notBuilt = "not built"

// Handler answers requests on <Prefix>.>: it validates each message strictly
// and answers the three kinds with "not built". Prefix is one subject token,
// the request subjects' prefix (CB_BRIDGE_REQ_PREFIX, default req).
type Handler struct {
	Log    *slog.Logger
	Prefix string
}

// Run subscribes <Prefix>.> and answers until ctx ends. A message that does
// not validate is logged and left unanswered; the caller's request then fails.
func (h *Handler) Run(ctx context.Context, nc *nats.Conn) error {
	if h.Prefix == "" {
		return errors.New("requests: no prefix")
	}
	log := h.Log
	if log == nil {
		log = slog.Default()
	}
	subject := h.Prefix + ".>"
	ch := make(chan *nats.Msg, 64)
	sub, err := nc.ChanSubscribe(subject, ch)
	if err != nil {
		return fmt.Errorf("requests: subscribe %s: %w", subject, err)
	}
	defer func() { _ = sub.Unsubscribe() }()
	for {
		select {
		case <-ctx.Done():
			return nil
		case m := <-ch:
			if m == nil { // a closed channel is no message
				continue
			}
			h.one(m, nc, log)
		}
	}
}

// one validates one message and answers it, or logs why it is left unanswered.
func (h *Handler) one(m *nats.Msg, nc *nats.Conn, log *slog.Logger) {
	agent, kind, req, err := h.decode(m)
	if err != nil {
		log.Warn("requests: refused", "subject", m.Subject, "error", err)
		return
	}
	body, err := json.Marshal(Answer{V: 1, Request: req.Request, Kind: kind, Error: notBuilt})
	if err != nil {
		log.Warn("requests: answer", "subject", m.Subject, "error", err)
		return
	}
	if err := nc.Publish("deliver."+agent+".req."+req.Request, body); err != nil {
		log.Warn("requests: answer", "subject", m.Subject, "error", err)
	}
}

// decode checks the subject and the body: exactly the prefix, the kind and
// the agent; a body of v 1 whose kind is the subject's and whose request id
// has the one shape. The agent is the subject's last token, never a body field.
func (h *Handler) decode(m *nats.Msg) (agent, kind string, req Request, err error) {
	tokens := strings.Split(m.Subject, ".")
	if len(tokens) != 3 || tokens[0] != h.Prefix {
		return "", "", req, fmt.Errorf("subject is no %s.<kind>.<agent>", h.Prefix)
	}
	kind, agent = tokens[1], tokens[2]
	if !kindOK(kind) {
		return "", "", req, fmt.Errorf("kind %q is not history, file or header", kind)
	}
	if !agentRE.MatchString(agent) {
		return "", "", req, fmt.Errorf("agent %q is no agent name", agent)
	}
	if err := json.Unmarshal(m.Data, &req); err != nil {
		return "", "", req, fmt.Errorf("body: %w", err)
	}
	switch {
	case req.V != 1:
		return "", "", req, fmt.Errorf("body v %d is not 1", req.V)
	case !requestRE.MatchString(req.Request):
		return "", "", req, fmt.Errorf("request %q is no 16 characters of a-z0-9", req.Request)
	case req.Kind != kind:
		return "", "", req, fmt.Errorf("body kind %q is not the subject's %q", req.Kind, kind)
	}
	return agent, kind, req, nil
}
