package admin

import (
	"errors"
	"net/http"
	"regexp"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/directory"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

// accountProduct is the one chat the account index knows today.
const accountProduct = "mattermost"

// accountNameRE is a bus username: what the index may hold as a name.
var accountNameRE = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,63}$`)

// accountView is what the account endpoints answer.
type accountView struct {
	Product   string `json:"product"`
	Name      string `json:"name"`
	Sub       string `json:"sub"`
	Revision  uint64 `json:"revision"`
	MovedFrom string `json:"moved_from,omitempty"`
}

func (h *handler) accountName(w http.ResponseWriter, r *http.Request) (string, bool) {
	name := r.PathValue("name")
	if !accountNameRE.MatchString(name) {
		fail(w, http.StatusBadRequest, "the name is no bus username (a-z, 0-9, . _ -; at most 64)")
		return "", false
	}
	return name, true
}

func (h *handler) getAccount(w http.ResponseWriter, r *http.Request, _ token.Identity) {
	name, ok := h.accountName(w, r)
	if !ok {
		return
	}
	sub, rev, from, found, err := h.store.Account(r.Context(), accountProduct, name)
	switch {
	case err != nil:
		h.storeError(w, err)
	case !found:
		fail(w, http.StatusNotFound, "no such account entry")
	default:
		w.Header().Set("ETag", etag(rev))
		reply(w, http.StatusOK, accountView{Product: accountProduct, Name: name, Sub: sub, Revision: rev, MovedFrom: from})
	}
}

type accountBody struct {
	Sub string `json:"sub"`
}

func (h *handler) putAccount(w http.ResponseWriter, r *http.Request, id token.Identity) {
	name, ok := h.accountName(w, r)
	if !ok {
		return
	}
	create := r.Header.Get("If-None-Match") == "*"
	ifMatch := r.Header.Get("If-Match")
	if !create && ifMatch == "" {
		fail(w, http.StatusPreconditionRequired, "If-None-Match: * (create) or If-Match: \"<revision>\" (replace) required")
		return
	}
	var want uint64
	if !create {
		var valid bool
		if want, valid = parseRev(ifMatch); !valid {
			fail(w, http.StatusBadRequest, "If-Match must be a revision")
			return
		}
	}
	var b accountBody
	if !h.decode(w, r, &b) {
		return
	}
	ctx := r.Context()
	mv, err := h.store.PutAccount(ctx, b.Sub, accountProduct, name, want)
	from := mv.From
	if mv.Released != "" {
		h.cfg.Log.Warn("directory write", "event", "unfinished account move finished", "sub", id.Subject,
			"user", id.Owner, "product", accountProduct, "name", name, "released", directory.Short(mv.Released))
	}
	if err != nil && from != "" { // the key moved, the records did not all follow
		h.cfg.Log.Warn("directory write", "event", "account set, incomplete", "sub", id.Subject, "user", id.Owner,
			"product", accountProduct, "name", name, "old_sub", directory.Short(from), "new_sub", directory.Short(b.Sub),
			"error", err.Error())
	}
	if errors.Is(err, directory.ErrNoIdentity) {
		fail(w, http.StatusNotFound, "no identity with that sub: it must have logged in once")
		return
	}
	if err != nil {
		h.storeError(w, err)
		return
	}
	old := from
	if old == "" && !create {
		old = b.Sub
	}
	h.cfg.Log.Info("directory write", "event", "account set", "sub", id.Subject, "user", id.Owner,
		"product", accountProduct, "name", name, "old_sub", directory.Short(old), "new_sub", directory.Short(b.Sub))
	_, rev, _, found, err := h.store.Account(ctx, accountProduct, name)
	if err != nil || !found {
		h.storeError(w, errors.Join(err, directory.ErrNotFound))
		return
	}
	w.Header().Set("ETag", etag(rev))
	reply(w, http.StatusOK, accountView{Product: accountProduct, Name: name, Sub: b.Sub, Revision: rev, MovedFrom: from})
}

func (h *handler) dropAccount(w http.ResponseWriter, r *http.Request, id token.Identity) {
	name, ok := h.accountName(w, r)
	if !ok {
		return
	}
	ifMatch := r.Header.Get("If-Match")
	if ifMatch == "" {
		fail(w, http.StatusPreconditionRequired, "If-Match: \"<revision>\" of the account entry required")
		return
	}
	rev, valid := parseRev(ifMatch)
	if !valid {
		fail(w, http.StatusBadRequest, "If-Match must be a revision")
		return
	}
	sub, released, err := h.store.DropAccount(r.Context(), accountProduct, name, rev)
	if released != "" { // an unfinished move's old holder lost the name too
		h.cfg.Log.Warn("directory write", "event", "unfinished account move finished", "sub", id.Subject,
			"user", id.Owner, "product", accountProduct, "name", name, "released", directory.Short(released))
	}
	if err != nil {
		h.storeError(w, err)
		return
	}
	h.cfg.Log.Info("directory write", "event", "account removed", "sub", id.Subject, "user", id.Owner,
		"product", accountProduct, "name", name, "old_sub", directory.Short(sub), "new_sub", "")
	w.WriteHeader(http.StatusNoContent)
}
