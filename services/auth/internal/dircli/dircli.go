// Package dircli (RT-6.40) is the command line `authd dir …` for the channel
// directory's admin API. It logs in with Keycloak's device flow (RFC 8628) and
// keeps the access token in memory for this one invocation: never on disk,
// never printed, never in argv or the environment.
package dircli

import (
	"bytes"
	"context"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

// Exit codes.
const (
	exitOK      = 0
	exitFailure = 1
	exitUsage   = 2
)

const usageText = `usage: authd dir [settings] <command>

commands:
  ls                                  list the channels
  show <channel>                      show a channel and its revision
  create <channel> [--owner <sub>] [--public]
  owner <channel> <sub|->             set the owner ("-" clears it)
  public <channel> on|off             make a channel public or private
  members add <channel> <sub>...      add members
  members rm <channel> <sub>...       remove members
  import                              copy the Mattermost channels once
  pending                             list the pending name claims
  confirm <name>                      confirm a pending name claim

settings (flag, then environment):
  --api <url>         AUTHD_DIR_API             admin API base URL (https; http only on a loopback IP)
  --issuer <url>      AUTH_KEYCLOAK_ISSUER      Keycloak issuer
  --client-id <id>    AUTH_KEYCLOAK_CLIENT_ID   Keycloak client for the device flow
  --ca-file <file>    AUTHD_DIR_CA_FILE         extra root CA for the API (optional)
  --json                                        print the API's JSON instead of text
  -h, --help                                    this text

exit codes: 0 ok, 1 failure, 2 usage error
`

// deps are the parts a test replaces: the clock and the wait.
type deps struct {
	now   func() time.Time
	sleep func(time.Duration)
}

// Main runs the command line and returns the exit code.
func Main(args []string, env func(string) string, stdout, stderr io.Writer) int {
	return run(args, env, stdout, stderr, deps{now: time.Now, sleep: time.Sleep})
}

type settings struct {
	api, issuer, clientID, caFile string
	json, public                  bool
	owner                         string
	ownerSet                      bool
}

type cli struct {
	s      settings
	out    io.Writer
	err    io.Writer
	d      deps
	api    *http.Client
	token  string
	apiURL string
}

type usageError string

func (u usageError) Error() string { return string(u) }

func run(args []string, env func(string) string, stdout, stderr io.Writer, d deps) int {
	if len(args) == 0 {
		fmt.Fprint(stderr, usageText)
		return exitUsage
	}
	s, pos, help, err := parseArgs(args, env)
	if help {
		fmt.Fprint(stdout, usageText)
		return exitOK
	}
	c := &cli{s: s, out: stdout, err: stderr, d: d}
	if err == nil {
		err = c.check(pos)
	}
	if err == nil {
		err = c.setup()
	}
	if err == nil {
		err = c.login()
	}
	if err == nil {
		err = c.command(pos)
	}
	if err == nil {
		return exitOK
	}
	var ue usageError
	if errors.As(err, &ue) {
		fmt.Fprintf(stderr, "authd dir: %s\n(authd dir -h shows the usage)\n", err)
		return exitUsage
	}
	fmt.Fprintf(stderr, "authd dir: %s\n", err)
	return exitFailure
}

func parseArgs(args []string, env func(string) string) (s settings, pos []string, help bool, err error) {
	s = settings{api: env("AUTHD_DIR_API"), issuer: env("AUTH_KEYCLOAK_ISSUER"),
		clientID: env("AUTH_KEYCLOAK_CLIENT_ID"), caFile: env("AUTHD_DIR_CA_FILE")}
	strs := map[string]*string{"api": &s.api, "issuer": &s.issuer, "client-id": &s.clientID,
		"ca-file": &s.caFile, "owner": &s.owner}
	bools := map[string]*bool{"json": &s.json, "public": &s.public}
	for i := 0; i < len(args); i++ {
		a := args[i]
		switch {
		case a == "-h" || a == "--help":
			return s, nil, true, nil
		case a == "--":
			pos = append(pos, args[i+1:]...)
			return s, pos, false, nil
		case strings.HasPrefix(a, "--"):
			name, val, hasVal := strings.Cut(a[2:], "=")
			if p, ok := bools[name]; ok {
				if hasVal {
					return s, nil, false, usageError("flag --" + name + " takes no value")
				}
				*p = true
				continue
			}
			p, ok := strs[name]
			if !ok {
				return s, nil, false, usageError("unknown flag --" + name)
			}
			if !hasVal {
				if i+1 >= len(args) {
					return s, nil, false, usageError("flag --" + name + " needs a value")
				}
				i++
				val = args[i]
			}
			*p = val
			if name == "owner" {
				s.ownerSet = true
			}
		default:
			pos = append(pos, a)
		}
	}
	return s, pos, false, nil
}

// check validates the command and its arguments before anything is sent.
func (c *cli) check(pos []string) error {
	if len(pos) == 0 {
		return usageError("no command")
	}
	if (c.s.ownerSet || c.s.public) && pos[0] != "create" {
		return usageError("--owner and --public belong to create")
	}
	need := func(n int, exact bool) error {
		if len(pos)-1 < n || (exact && len(pos)-1 != n) {
			return usageError("wrong number of arguments for " + pos[0])
		}
		return nil
	}
	switch pos[0] {
	case "ls", "import", "pending":
		return need(0, true)
	case "show", "confirm", "create":
		return need(1, true)
	case "owner":
		return need(2, true)
	case "public":
		if err := need(2, true); err != nil {
			return err
		}
		if pos[2] != "on" && pos[2] != "off" {
			return usageError("public wants on or off")
		}
	case "members":
		if len(pos) < 2 || (pos[1] != "add" && pos[1] != "rm") {
			return usageError("members wants add or rm")
		}
		if len(pos) < 4 {
			return usageError("members " + pos[1] + " wants a channel and at least one sub")
		}
	default:
		return usageError("unknown command " + pos[0])
	}
	return nil
}

func loopbackHTTP(u *url.URL) bool {
	ip := net.ParseIP(u.Hostname())
	return u.Scheme == "http" && ip != nil && ip.IsLoopback()
}

func parseBase(raw, what string) (*url.URL, error) {
	if raw == "" {
		return nil, usageError(what + " is not set")
	}
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return nil, usageError(what + " is not a usable URL")
	}
	if u.Scheme != "https" && !loopbackHTTP(u) {
		return nil, usageError(what + " must be https (http only on a loopback IP address)")
	}
	return u, nil
}

func (c *cli) setup() error {
	u, err := parseBase(c.s.api, "the API URL (--api, AUTHD_DIR_API)")
	if err != nil {
		return err
	}
	c.apiURL = strings.TrimRight(u.String(), "/")
	if _, err := parseBase(c.s.issuer, "the issuer (--issuer, AUTH_KEYCLOAK_ISSUER)"); err != nil {
		return err
	}
	if c.s.clientID == "" {
		return usageError("the client id is not set (--client-id, AUTH_KEYCLOAK_CLIENT_ID)")
	}
	tr := http.DefaultTransport.(*http.Transport).Clone()
	if c.s.caFile != "" {
		pem, err := os.ReadFile(c.s.caFile)
		if err != nil {
			return errors.New("the CA file is not readable")
		}
		pool, err := x509.SystemCertPool()
		if err != nil || pool == nil {
			pool = x509.NewCertPool()
		}
		if !pool.AppendCertsFromPEM(pem) {
			return usageError("the CA file holds no certificate")
		}
		tr.TLSClientConfig.RootCAs = pool
	}
	c.api = newClient(tr)
	return nil
}

func newClient(tr http.RoundTripper) *http.Client {
	return &http.Client{Timeout: 30 * time.Second, Transport: tr,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
}

// --- device flow ---

const (
	maxDeviceLife = 15 * time.Minute
	maxBackoff    = 60 * time.Second
)

func (c *cli) login() error {
	kc := newClient(http.DefaultTransport)
	issuer := strings.TrimRight(c.s.issuer, "/")
	iu, _ := url.Parse(issuer)
	sameSite := func(raw, what string) error {
		u, err := url.Parse(raw)
		if err != nil || u.Scheme != iu.Scheme || u.Hostname() != iu.Hostname() {
			return fmt.Errorf("%s is not on the issuer's host", what)
		}
		return nil
	}
	var doc struct {
		Issuer string `json:"issuer"`
		Device string `json:"device_authorization_endpoint"`
		Token  string `json:"token_endpoint"`
	}
	st, body, err := doJSON(kc, http.MethodGet, issuer+"/.well-known/openid-configuration", nil, nil)
	if err != nil || st != http.StatusOK || json.Unmarshal(body, &doc) != nil {
		return errors.New("Keycloak's discovery document was not read")
	}
	if strings.TrimRight(doc.Issuer, "/") != issuer {
		return errors.New("Keycloak's discovery document names another issuer")
	}
	for what, v := range map[string]string{"device_authorization_endpoint": doc.Device, "token_endpoint": doc.Token} {
		if v == "" {
			return fmt.Errorf("Keycloak's discovery document has no %s", what)
		}
		if err := sameSite(v, what); err != nil {
			return err
		}
	}
	st, body, err = c.postForm(kc, doc.Device, url.Values{"client_id": {c.s.clientID}, "scope": {"openid"}})
	var dev struct {
		DeviceCode string `json:"device_code"`
		UserCode   string `json:"user_code"`
		URI        string `json:"verification_uri"`
		Complete   string `json:"verification_uri_complete"`
		ExpiresIn  int    `json:"expires_in"`
		Interval   int    `json:"interval"`
	}
	if err != nil {
		return err
	}
	if st != http.StatusOK || json.Unmarshal(body, &dev) != nil || dev.DeviceCode == "" {
		return fmt.Errorf("the device authorization was refused (%s)", errCode(body, st))
	}
	if err := sameSite(dev.URI, "the verification URI"); err != nil {
		return err
	}
	show := dev.URI
	if dev.Complete != "" {
		if err := sameSite(dev.Complete, "the verification URI"); err != nil {
			return err
		}
		show = dev.Complete
	}
	fmt.Fprintf(c.err, "Open %s and confirm code %s\n", clean(show), clean(dev.UserCode))

	life := time.Duration(dev.ExpiresIn) * time.Second
	if life <= 0 {
		life = 10 * time.Minute
	}
	life = min(life, maxDeviceLife)
	deadline := c.d.now().Add(life)
	interval := max(time.Duration(dev.Interval)*time.Second, time.Second)
	if dev.Interval <= 0 {
		interval = 5 * time.Second
	}
	delay := interval
	for {
		c.d.sleep(delay)
		if c.d.now().After(deadline) {
			return errors.New("the login expired; run the command again")
		}
		st, body, err := c.postForm(kc, doc.Token, url.Values{
			"grant_type":  {"urn:ietf:params:oauth:grant-type:device_code"},
			"device_code": {dev.DeviceCode}, "client_id": {c.s.clientID}})
		if err != nil || st >= 500 {
			delay = min(delay*2, maxBackoff)
			continue
		}
		delay = interval
		if st == http.StatusOK {
			var tok struct {
				AccessToken string `json:"access_token"`
			}
			if json.Unmarshal(body, &tok) != nil || tok.AccessToken == "" {
				return errors.New("Keycloak's answer holds no access token")
			}
			c.token = tok.AccessToken
			return nil
		}
		switch code := errCode(body, st); code {
		case "authorization_pending":
		case "slow_down":
			interval += 5 * time.Second
			delay = interval
		case "access_denied":
			return errors.New("the login was declined")
		case "expired_token":
			return errors.New("the login expired; run the command again")
		default:
			return fmt.Errorf("the token request failed (%s)", code)
		}
	}
}

// errCode is the OAuth error code of a response, else its HTTP status.
func errCode(body []byte, status int) string {
	var e struct {
		Error string `json:"error"`
	}
	if json.Unmarshal(body, &e) == nil && e.Error != "" && len(e.Error) <= 64 {
		return clean(e.Error)
	}
	return "HTTP " + strconv.Itoa(status)
}

func (c *cli) postForm(cl *http.Client, u string, v url.Values) (int, []byte, error) {
	return doJSON(cl, http.MethodPost, u, strings.NewReader(v.Encode()),
		map[string]string{"Content-Type": "application/x-www-form-urlencoded"})
}

// doJSON sends one request and reads at most 4 MiB of the answer.
func doJSON(cl *http.Client, method, u string, body io.Reader, h map[string]string) (int, []byte, error) {
	st, _, b, err := doReq(cl, method, u, body, h)
	return st, b, err
}

func doReq(cl *http.Client, method, u string, body io.Reader, h map[string]string) (int, http.Header, []byte, error) {
	req, err := http.NewRequestWithContext(context.Background(), method, u, body)
	if err != nil {
		return 0, nil, nil, err
	}
	for k, v := range h {
		req.Header.Set(k, v)
	}
	resp, err := cl.Do(req)
	if err != nil {
		var ue *url.Error
		if errors.As(err, &ue) {
			err = ue.Err // the URL could carry nothing secret, but is noise
		}
		return 0, nil, nil, fmt.Errorf("request failed: %w", err)
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	return resp.StatusCode, resp.Header, b, err
}

// --- API ---

type reply struct {
	status int
	etag   string
	body   []byte
}

func (c *cli) call(method, path string, hdr map[string]string, payload any) (reply, error) {
	var rd io.Reader
	h := map[string]string{"Authorization": "Bearer " + c.token, "Accept": "application/json"}
	if payload != nil {
		b, err := json.Marshal(payload)
		if err != nil {
			return reply{}, err
		}
		rd = bytes.NewReader(b)
		h["Content-Type"] = "application/json"
	}
	for k, v := range hdr {
		h[k] = v
	}
	st, hd, b, err := doReq(c.api, method, c.apiURL+path, rd, h)
	if err != nil {
		return reply{}, err
	}
	r := reply{status: st, etag: hd.Get("ETag"), body: b}
	switch {
	case st == http.StatusConflict:
		return r, errors.New("changed meanwhile; run again")
	case st >= 300:
		var e struct {
			Error string `json:"error"`
		}
		msg := "no error text"
		if json.Unmarshal(b, &e) == nil && e.Error != "" {
			msg = clean(e.Error)
		}
		return r, fmt.Errorf("%s (HTTP %d)", msg, st)
	}
	return r, nil
}

type channel struct {
	Name     string            `json:"name"`
	Owner    string            `json:"owner"`
	Public   bool              `json:"public"`
	Members  []string          `json:"members"`
	Bindings map[string]string `json:"bindings"`
}

type putBody struct {
	Owner    string            `json:"owner"`
	Public   bool              `json:"public"`
	Bindings map[string]string `json:"bindings"`
}

func esc(s string) string { return url.PathEscape(s) }

// clean drops control characters from text a server sent before it reaches
// the terminal, so an answer cannot move the cursor, clear the screen or
// fake a line of its own; at most 512 bytes.
func clean(s string) string {
	s = strings.Map(func(r rune) rune {
		if r < 0x20 || r == 0x7f || (r >= 0x80 && r < 0xa0) {
			return -1
		}
		return r
	}, s)
	if len(s) > 512 {
		s = s[:512]
	}
	return s
}

func visibility(public bool) string {
	if public {
		return "public"
	}
	return "private"
}

func (c *cli) raw(b []byte) {
	b = bytes.TrimRight(b, "\n")
	fmt.Fprintf(c.out, "%s\n", b)
}

func (c *cli) command(pos []string) error {
	switch pos[0] {
	case "ls":
		r, err := c.call("GET", "/v1/directory/channels", nil, nil)
		if err != nil {
			return err
		}
		if c.s.json {
			c.raw(r.body)
			return nil
		}
		var chs []channel
		if err := json.Unmarshal(r.body, &chs); err != nil {
			return errors.New("the API's answer is not a channel list")
		}
		for _, ch := range chs {
			fmt.Fprintf(c.out, "%s\t%s\t%s\t%d members\n", ch.Name, visibility(ch.Public), ownerText(ch.Owner), len(ch.Members))
		}
	case "show":
		r, err := c.call("GET", "/v1/directory/channels/"+esc(pos[1]), nil, nil)
		if err != nil {
			return err
		}
		return c.printChannel(r)
	case "create":
		b := putBody{Owner: c.s.owner, Public: c.s.public}
		r, err := c.call("PUT", "/v1/directory/channels/"+esc(pos[1]), map[string]string{"If-None-Match": "*"}, b)
		if err != nil {
			return err
		}
		return c.printChannel(r)
	case "owner", "public":
		path := "/v1/directory/channels/" + esc(pos[1])
		cur, ch, err := c.read(path)
		if err != nil {
			return err
		}
		b := putBody{Owner: ch.Owner, Public: ch.Public, Bindings: ch.Bindings}
		if pos[0] == "owner" {
			b.Owner = pos[2]
			if pos[2] == "-" {
				b.Owner = ""
			}
		} else {
			b.Public = pos[2] == "on"
		}
		r, err := c.call("PUT", path, map[string]string{"If-Match": cur.etag}, b)
		if err != nil {
			return err
		}
		return c.printChannel(r)
	case "members":
		path := "/v1/directory/channels/" + esc(pos[2])
		cur, _, err := c.read(path)
		if err != nil {
			return err
		}
		b := map[string][]string{"add": {}, "remove": {}}
		if pos[1] == "add" {
			b["add"] = pos[3:]
		} else {
			b["remove"] = pos[3:]
		}
		r, err := c.call("PATCH", path+"/members", map[string]string{"If-Match": cur.etag}, b)
		if err != nil {
			return err
		}
		return c.printChannel(r)
	case "import":
		r, err := c.call("POST", "/v1/directory/import", nil, nil)
		if err != nil {
			return err
		}
		return c.printImport(r)
	case "pending":
		ps, r, err := c.pending()
		if err != nil {
			return err
		}
		if c.s.json {
			c.raw(r.body)
			return nil
		}
		for _, p := range ps {
			fmt.Fprintf(c.out, "%s\t%s\tconfirmed=%t\trevision %d\n", p.Name, strings.Join(p.Channels, ","), p.Confirmed, p.Revision)
		}
	case "confirm":
		ps, _, err := c.pending()
		if err != nil {
			return err
		}
		for _, p := range ps {
			if p.Name != pos[1] {
				continue
			}
			r, err := c.call("POST", "/v1/directory/pending/"+esc(p.Name)+"/confirm",
				map[string]string{"If-Match": `"` + strconv.FormatUint(p.Revision, 10) + `"`}, nil)
			if err != nil {
				return err
			}
			if c.s.json {
				c.raw(r.body)
			} else {
				fmt.Fprintf(c.out, "confirmed %s\n", p.Name)
			}
			return nil
		}
		return fmt.Errorf("%q is not in the pending list", pos[1])
	}
	return nil
}

func ownerText(o string) string {
	if o == "" {
		return "-"
	}
	return o
}

// read gets a channel for its ETag.
func (c *cli) read(path string) (reply, channel, error) {
	r, err := c.call("GET", path, nil, nil)
	if err != nil {
		return r, channel{}, err
	}
	var ch channel
	if r.etag == "" || json.Unmarshal(r.body, &ch) != nil {
		return r, ch, errors.New("the API's answer has no channel or no revision")
	}
	return r, ch, nil
}

type pendingEntry struct {
	Name      string   `json:"name"`
	Channels  []string `json:"channels"`
	Confirmed bool     `json:"confirmed"`
	Revision  uint64   `json:"revision"`
}

func (c *cli) pending() ([]pendingEntry, reply, error) {
	r, err := c.call("GET", "/v1/directory/pending", nil, nil)
	if err != nil {
		return nil, r, err
	}
	var ps []pendingEntry
	if err := json.Unmarshal(r.body, &ps); err != nil {
		return nil, r, errors.New("the API's answer is not a pending list")
	}
	return ps, r, nil
}

func (c *cli) printChannel(r reply) error {
	if c.s.json {
		c.raw(r.body)
		return nil
	}
	var ch channel
	if err := json.Unmarshal(r.body, &ch); err != nil {
		return errors.New("the API's answer is not a channel")
	}
	rev := strings.Trim(r.etag, `"`)
	if rev == "" {
		rev = "unknown"
	}
	fmt.Fprintf(c.out, "%s\t%s\towner %s\t%d members: %s\trevision %s\n", ch.Name, visibility(ch.Public),
		ownerText(ch.Owner), len(ch.Members), strings.Join(ch.Members, ","), rev)
	return nil
}

func (c *cli) printImport(r reply) error {
	if c.s.json {
		c.raw(r.body)
		return nil
	}
	var rep struct {
		Created         []string                            `json:"channels_created"`
		Updated         []string                            `json:"channels_updated"`
		SkippedChannels []struct{ Name, Reason string }     `json:"channels_skipped"`
		Identities      int                                 `json:"identities"`
		MembersAdded    int                                 `json:"members_added"`
		PendingAdded    int                                 `json:"pending_added"`
		SkippedAccounts []struct{ Username, Reason string } `json:"accounts_skipped"`
		Next            string                              `json:"next"`
	}
	if err := json.Unmarshal(r.body, &rep); err != nil {
		return errors.New("the API's answer is not an import report")
	}
	fmt.Fprintf(c.out, "channels created %d, updated %d, skipped %d\n", len(rep.Created), len(rep.Updated), len(rep.SkippedChannels))
	fmt.Fprintf(c.out, "identities %d, members added %d, pending added %d, accounts skipped %d\n",
		rep.Identities, rep.MembersAdded, rep.PendingAdded, len(rep.SkippedAccounts))
	for _, s := range rep.SkippedChannels {
		fmt.Fprintf(c.out, "skipped channel %s: %s\n", s.Name, s.Reason)
	}
	for _, s := range rep.SkippedAccounts {
		fmt.Fprintf(c.out, "skipped account %s: %s\n", s.Username, s.Reason)
	}
	if rep.Next != "" {
		fmt.Fprintf(c.out, "next: %s\n", rep.Next)
	}
	return nil
}
