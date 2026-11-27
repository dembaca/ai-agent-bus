// Package dircli (RT-6.40) is the command line `authd dir …` for the channel
// directory's admin API. It logs in with Keycloak's device flow (RFC 8628) and
// keeps the access token in memory for this one invocation: never on disk,
// never printed, never in argv or the environment.
package dircli

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/envname"
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
  planb <channel> <n,...|->           switch plan B pieces 1 to 13 on, the others off ("-": all off)
  members add <channel> <sub>...      add members
  members rm <channel> <sub>...       remove members
  import                              copy the Mattermost channels once
  stats-import [--all]                copy the stats channel's STATS lines into the STATS
                                      stream (repeatable); --all reads the whole channel
  pending                             list the pending name claims
  confirm <name>                      confirm a pending name claim
  drop <name>                         delete a pending name claim
  account show <name>                 show which sub holds a Mattermost account name
  account set <name> <sub> [--rev N]  point a name at a sub (no --rev: create; --rev N: replace revision N)
  account rm <name> --rev N           remove a name's entry (revision N from show)

settings (flag, then environment):
  --api <url>         CB_AUTHD_DIR_API             admin API base URL (https; http only on a loopback IP)
  --issuer <url>      CB_AUTH_KEYCLOAK_ISSUER      Keycloak issuer
  --client-id <id>    CB_AUTH_KEYCLOAK_CLIENT_ID   Keycloak client for the device flow
  --ca-file <file>    CB_AUTHD_DIR_CA_FILE         extra root CA for the API (optional)
  --json                                           print the API's JSON instead of text
  -h, --help                                       this text
  (the old names without CB_ are still read, with a warning, until the next release)

exit codes: 0 ok, 1 failure, 2 usage error
`

// warnTo writes a deprecation warning of envname as one line to w.
func warnTo(w io.Writer) envname.Warn {
	return func(msg string, args ...any) {
		var kv strings.Builder
		for i := 0; i+1 < len(args); i += 2 {
			fmt.Fprintf(&kv, " %v=%v", args[i], args[i+1])
		}
		fmt.Fprintf(w, "authd dir: warning: %s:%s\n", msg, kv.String())
	}
}

// deps are the parts a test replaces: the clock and the wait.
type deps struct {
	now   func() time.Time
	sleep func(time.Duration)
}

// Main runs the command line and returns the exit code.
// lookup is os.LookupEnv or a stand-in; names are read by the reading rule of
// docs/env-names.md (envname).
func Main(args []string, lookup envname.Lookup, stdout, stderr io.Writer) int {
	return run(args, lookup, stdout, stderr, deps{now: time.Now, sleep: time.Sleep})
}

type settings struct {
	api, issuer, clientID, caFile string
	json, public, all             bool
	owner                         string
	ownerSet                      bool
	revText                       string
	revSet                        bool
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

// envNames are the names the directory commands read.
var envNames = []string{"CB_AUTHD_DIR_API", "CB_AUTH_KEYCLOAK_ISSUER", "CB_AUTH_KEYCLOAK_CLIENT_ID", "CB_AUTHD_DIR_CA_FILE"}

func run(args []string, lookup envname.Lookup, stdout, stderr io.Writer, d deps) int {
	if len(args) == 0 {
		fmt.Fprint(stderr, usageText)
		return exitUsage
	}
	env := envname.New(lookup, warnTo(stderr))
	envErr := env.Check(envNames...)
	// After a refused Check, Value panics: parse the flags without the
	// environment, so -h still works, and report envErr below.
	get := env.Value
	if envErr != nil {
		get = func(string) string { return "" }
	}
	s, pos, help, err := parseArgs(args, get)
	if help {
		fmt.Fprint(stdout, usageText)
		return exitOK
	}
	if err == nil && envErr != nil {
		err = usageError(envErr.Error())
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
	s = settings{api: env("CB_AUTHD_DIR_API"), issuer: env("CB_AUTH_KEYCLOAK_ISSUER"),
		clientID: env("CB_AUTH_KEYCLOAK_CLIENT_ID"), caFile: env("CB_AUTHD_DIR_CA_FILE")}
	strs := map[string]*string{"api": &s.api, "issuer": &s.issuer, "client-id": &s.clientID,
		"ca-file": &s.caFile, "owner": &s.owner, "rev": &s.revText}
	bools := map[string]*bool{"json": &s.json, "public": &s.public, "all": &s.all}
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
			switch name {
			case "owner":
				s.ownerSet = true
			case "rev":
				s.revSet = true
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
	isAccount := pos[0] == "account" && len(pos) > 1 && (pos[1] == "set" || pos[1] == "rm")
	if c.s.revSet && !isAccount {
		return usageError("--rev belongs to account set and account rm")
	}
	if c.s.all && pos[0] != "stats-import" {
		return usageError("--all belongs to stats-import")
	}
	need := func(n int, exact bool) error {
		if len(pos)-1 < n || (exact && len(pos)-1 != n) {
			return usageError("wrong number of arguments for " + pos[0])
		}
		return nil
	}
	switch pos[0] {
	case "ls", "import", "stats-import", "pending":
		return need(0, true)
	case "show", "confirm", "drop", "create":
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
	case "planb":
		if err := need(2, true); err != nil {
			return err
		}
		if _, err := planBArg(pos[2]); err != nil {
			return err
		}
	case "members":
		if len(pos) < 2 || (pos[1] != "add" && pos[1] != "rm") {
			return usageError("members wants add or rm")
		}
		if len(pos) < 4 {
			return usageError("members " + pos[1] + " wants a channel and at least one sub")
		}
	case "account":
		return c.checkAccount(pos)
	default:
		return usageError("unknown command " + pos[0])
	}
	return nil
}

// accountNameRE is a bus username, as the API wants it.
var accountNameRE = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,63}$`)

// checkAccount validates `account show|set|rm` before anything is sent.
func (c *cli) checkAccount(pos []string) error {
	if len(pos) < 2 || (pos[1] != "show" && pos[1] != "set" && pos[1] != "rm") {
		return usageError("account wants show, set or rm")
	}
	want := map[string]int{"show": 1, "set": 2, "rm": 1}[pos[1]]
	if len(pos)-2 != want {
		return usageError("wrong number of arguments for account " + pos[1])
	}
	if !accountNameRE.MatchString(pos[2]) {
		return usageError("the account name is no bus username (a-z, 0-9, . _ -; at most 64)")
	}
	if pos[1] == "set" && pos[3] == "" {
		return usageError("the sub is empty")
	}
	if pos[1] == "show" && c.s.revSet {
		return usageError("--rev belongs to account set and account rm")
	}
	if c.s.revSet {
		if n, err := strconv.ParseUint(c.s.revText, 10, 64); err != nil || n == 0 {
			return usageError("--rev wants a revision number above 0")
		}
	}
	if pos[1] == "rm" && !c.s.revSet {
		return usageError("account rm needs --rev N (the revision account show printed)")
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
	u, err := parseBase(c.s.api, "the API URL (--api, CB_AUTHD_DIR_API)")
	if err != nil {
		return err
	}
	c.apiURL = strings.TrimRight(u.String(), "/")
	if _, err := parseBase(c.s.issuer, "the issuer (--issuer, CB_AUTH_KEYCLOAK_ISSUER)"); err != nil {
		return err
	}
	if c.s.clientID == "" {
		return usageError("the client id is not set (--client-id, CB_AUTH_KEYCLOAK_CLIENT_ID)")
	}
	tr, err := apiTransport(http.DefaultTransport.(*http.Transport), c.s.caFile)
	if err != nil {
		return err
	}
	c.api = newClient(tr)
	return nil
}

// apiTransport is a copy of base that also trusts the CA file, if any.
func apiTransport(base *http.Transport, caFile string) (*http.Transport, error) {
	tr := base.Clone()
	if caFile == "" {
		return tr, nil
	}
	pem, err := os.ReadFile(caFile)
	if err != nil {
		return nil, errors.New("the CA file is not readable")
	}
	pool, err := x509.SystemCertPool()
	if err != nil || pool == nil {
		pool = x509.NewCertPool()
	}
	if !pool.AppendCertsFromPEM(pem) {
		return nil, usageError("the CA file holds no certificate")
	}
	if tr.TLSClientConfig == nil {
		tr.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12}
	}
	tr.TLSClientConfig.RootCAs = pool
	return tr, nil
}

func newClient(tr http.RoundTripper) *http.Client {
	return &http.Client{Timeout: 30 * time.Second, Transport: tr,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
}

// --- device flow ---

const (
	maxDeviceLife = 15 * time.Minute
	maxInterval   = 30 * time.Second // also the backoff
	maxBackoff    = maxInterval
)

// effectivePort is the URL's port, the scheme's default when it has none.
func effectivePort(u *url.URL) string {
	if p := u.Port(); p != "" {
		return p
	}
	switch u.Scheme {
	case "https":
		return "443"
	case "http":
		return "80"
	}
	return ""
}

// sameSite refuses a URL that is not on the issuer's own site: scheme, host
// and port (a default port counts as the implicit one) must match, and the
// URL carries no user info.
func sameSite(issuer *url.URL, raw, what string) error {
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" {
		return fmt.Errorf("%s is not a usable URL", what)
	}
	if u.User != nil {
		return fmt.Errorf("%s carries user info", what)
	}
	if u.Scheme != issuer.Scheme || !strings.EqualFold(u.Hostname(), issuer.Hostname()) ||
		effectivePort(u) != effectivePort(issuer) {
		return fmt.Errorf("%s is not on the issuer's host", what)
	}
	return nil
}

func (c *cli) login() error {
	kc := newClient(http.DefaultTransport)
	issuer := strings.TrimRight(c.s.issuer, "/")
	iu, _ := url.Parse(issuer)
	sameSite := func(raw, what string) error { return sameSite(iu, raw, what) }
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
	interval := time.Duration(dev.Interval) * time.Second
	if dev.Interval <= 0 {
		interval = 5 * time.Second
	}
	interval = min(max(interval, time.Second), maxInterval)
	delay := interval
	for {
		left := deadline.Sub(c.d.now())
		if left <= 0 {
			return errors.New("the login expired; run the command again")
		}
		c.d.sleep(min(delay, left))
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
			interval = min(interval+5*time.Second, maxInterval)
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
	PlanB    []int             `json:"planb"`
}

type putBody struct {
	Owner    string            `json:"owner"`
	Public   bool              `json:"public"`
	Bindings map[string]string `json:"bindings"`
	// PlanB: nil leaves the key out, so the service keeps the pieces; a
	// pointer to an empty list switches every piece off (RT-6.39 step 5).
	PlanB *[]int `json:"planb,omitempty"`
}

// planBArg reads `planb`'s list: "-" for none, else pieces 1 to 13,
// comma-separated, each once (the service checks it again).
func planBArg(s string) ([]int, error) {
	bad := usageError("planb wants pieces 1 to 13, comma-separated, each once, or -")
	if s == "-" {
		return []int{}, nil
	}
	var out []int
	for _, f := range strings.Split(s, ",") {
		n, err := strconv.Atoi(strings.TrimSpace(f))
		if err != nil || n < 1 || n > 13 || slices.Contains(out, n) {
			return nil, bad
		}
		out = append(out, n)
	}
	slices.Sort(out)
	return out, nil
}

func esc(s string) string { return url.PathEscape(s) }

// clean is the one filter for text a server sent: it drops control and
// format characters (so also bidi controls) and the line and paragraph
// separators, which could move the cursor, clear the screen, fake a line or
// reorder the text on a terminal; at most 512 bytes. --json stays raw.
func clean(s string) string {
	s = strings.Map(func(r rune) rune {
		if unicode.Is(unicode.Cc, r) || unicode.Is(unicode.Cf, r) || r == 0x2028 || r == 0x2029 {
			return -1
		}
		return r
	}, s)
	if len(s) > 512 {
		s = s[:512]
		for !utf8.ValidString(s) {
			s = s[:len(s)-1]
		}
	}
	return s
}

func cleanAll(ss []string) []string {
	out := make([]string, len(ss))
	for i, s := range ss {
		out[i] = clean(s)
	}
	return out
}

func visibility(public bool) string {
	if public {
		return "public"
	}
	return "private"
}

// raw writes the server's bytes as they came, if they are JSON.
func (c *cli) raw(b []byte) error {
	if !json.Valid(b) {
		return errors.New("the server's answer is not JSON")
	}
	b = bytes.TrimRight(b, "\n")
	fmt.Fprintf(c.out, "%s\n", b)
	return nil
}

func (c *cli) command(pos []string) error {
	switch pos[0] {
	case "ls":
		r, err := c.call("GET", "/v1/directory/channels", nil, nil)
		if err != nil {
			return err
		}
		if c.s.json {
			return c.raw(r.body)
		}
		var chs []channel
		if err := json.Unmarshal(r.body, &chs); err != nil {
			return errors.New("the API's answer is not a channel list")
		}
		for _, ch := range chs {
			fmt.Fprintf(c.out, "%s\t%s\t%s\t%d members\n", clean(ch.Name), visibility(ch.Public), ownerText(ch.Owner), len(ch.Members))
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
	case "planb":
		pieces, err := planBArg(pos[2])
		if err != nil {
			return err
		}
		path := "/v1/directory/channels/" + esc(pos[1])
		cur, ch, err := c.read(path)
		if err != nil {
			return err
		}
		b := putBody{Owner: ch.Owner, Public: ch.Public, Bindings: ch.Bindings, PlanB: &pieces}
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
	case "stats-import":
		path := "/v1/stats/import"
		if c.s.all { // RT-6.45: the whole channel, past the last complete run's mark
			path += "?all=true"
		}
		r, err := c.call("POST", path, nil, nil)
		if err != nil {
			return err
		}
		return c.printStatsImport(r)
	case "pending":
		ps, r, err := c.pending()
		if err != nil {
			return err
		}
		if c.s.json {
			return c.raw(r.body)
		}
		for _, p := range ps {
			fmt.Fprintf(c.out, "%s\t%s\tconfirmed=%t\trevision %d\n", clean(p.Name), strings.Join(cleanAll(p.Channels), ","), p.Confirmed, p.Revision)
			if len(p.WaitingFull) > 0 {
				fmt.Fprintf(c.out, "\twaiting on full channel(s): %s\n", strings.Join(cleanAll(p.WaitingFull), ","))
			}
		}
	case "account":
		return c.account(pos)
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
				return c.raw(r.body)
			}
			fmt.Fprintf(c.out, "confirmed %s\n", clean(p.Name))
			return nil
		}
		return fmt.Errorf("%q is not in the pending list", pos[1])
	case "drop":
		ps, _, err := c.pending()
		if err != nil {
			return err
		}
		for _, p := range ps {
			if p.Name != pos[1] {
				continue
			}
			r, err := c.call("DELETE", "/v1/directory/pending/"+esc(p.Name),
				map[string]string{"If-Match": `"` + strconv.FormatUint(p.Revision, 10) + `"`}, nil)
			if err != nil {
				return err
			}
			if c.s.json {
				return c.raw(r.body)
			}
			fmt.Fprintf(c.out, "dropped %s\n", clean(p.Name))
			return nil
		}
		return fmt.Errorf("%q is not in the pending list", pos[1])
	}
	return nil
}

type accountEntry struct {
	Product   string `json:"product"`
	Name      string `json:"name"`
	Sub       string `json:"sub"`
	Revision  uint64 `json:"revision"`
	MovedFrom string `json:"moved_from"`
}

// account runs account show, set and rm (the arguments are checked).
func (c *cli) account(pos []string) error {
	path := "/v1/directory/accounts/mattermost/" + esc(pos[2])
	rev := `"` + c.s.revText + `"`
	var r reply
	var err error
	switch pos[1] {
	case "show":
		r, err = c.call("GET", path, nil, nil)
	case "set":
		hdr := map[string]string{"If-None-Match": "*"}
		if c.s.revSet {
			hdr = map[string]string{"If-Match": rev}
		}
		r, err = c.call("PUT", path, hdr, map[string]string{"sub": pos[3]})
	case "rm":
		r, err = c.call("DELETE", path, map[string]string{"If-Match": rev}, nil)
		if err != nil {
			return err
		}
		if c.s.json && len(r.body) > 0 {
			return c.raw(r.body)
		}
		fmt.Fprintf(c.out, "removed %s\n", clean(pos[2]))
		return nil
	}
	if err != nil {
		return err
	}
	if c.s.json {
		return c.raw(r.body)
	}
	var a accountEntry
	if err := json.Unmarshal(r.body, &a); err != nil {
		return errors.New("the API's answer is not an account entry")
	}
	fmt.Fprintf(c.out, "%s\t%s\tsub %s\trevision %d\n", clean(a.Product), clean(a.Name), clean(a.Sub), a.Revision)
	if a.MovedFrom != "" {
		fmt.Fprintf(c.out, "\tmoved from %s\n", clean(a.MovedFrom))
	}
	return nil
}

func ownerText(o string) string {
	if o == "" {
		return "-"
	}
	return clean(o)
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

	WaitingFull []string `json:"waiting_full"`
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
		return c.raw(r.body)
	}
	var ch channel
	if err := json.Unmarshal(r.body, &ch); err != nil {
		return errors.New("the API's answer is not a channel")
	}
	rev := clean(strings.Trim(r.etag, `"`))
	if rev == "" {
		rev = "unknown"
	}
	planb := ""
	if len(ch.PlanB) > 0 {
		ns := make([]string, len(ch.PlanB))
		for i, n := range ch.PlanB {
			ns[i] = strconv.Itoa(n)
		}
		planb = "\tplan B " + strings.Join(ns, ",")
	}
	fmt.Fprintf(c.out, "%s\t%s\towner %s\t%d members: %s%s\trevision %s\n", clean(ch.Name), visibility(ch.Public),
		ownerText(ch.Owner), len(ch.Members), strings.Join(cleanAll(ch.Members), ","), planb, rev)
	return nil
}

func (c *cli) printImport(r reply) error {
	if c.s.json {
		return c.raw(r.body)
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
		fmt.Fprintf(c.out, "skipped channel %s: %s\n", clean(s.Name), clean(s.Reason))
	}
	for _, s := range rep.SkippedAccounts {
		fmt.Fprintf(c.out, "skipped account %s: %s\n", clean(s.Username), clean(s.Reason))
	}
	if rep.Next != "" {
		fmt.Fprintf(c.out, "next: %s\n", clean(rep.Next))
	}
	return nil
}

// printStatsImport prints the STATS import's report (RT-6.45).
func (c *cli) printStatsImport(r reply) error {
	if c.s.json {
		return c.raw(r.body)
	}
	var rep struct {
		Read            int    `json:"read"`
		Published       int    `json:"published"`
		WithoutSid      int    `json:"published_without_sid"`
		Confirmed       int    `json:"confirmed"`
		Truncated       string `json:"truncated"`
		NotStats        int    `json:"not_stats"`
		SkippedInStats  int    `json:"skipped_in_stats"`
		SkippedImported int    `json:"skipped_imported"`
		KnownBefore     string `json:"known_before"`
		SkippedInvalid  int    `json:"skipped_invalid"`
		SkippedTooBig   int    `json:"skipped_too_big"`
		SkippedName     int    `json:"skipped_name"`
		SkippedRecent   int    `json:"skipped_recent"`
		SkippedOverCap  int    `json:"skipped_over_cap"`
		SkippedRoom     int    `json:"skipped_room"`
		All             bool   `json:"all"`
		Oldest          string `json:"oldest"`
		Newest          string `json:"newest"`
	}
	if err := json.Unmarshal(r.body, &rep); err != nil {
		return errors.New("the API's answer is not a stats import report")
	}
	fmt.Fprintf(c.out, "posts read %d, lines published %d (%d of them from before 0.91.0, without sid; "+
		"%d found in STATS after the run), posts without a STATS line %d\n",
		rep.Read, rep.Published, rep.WithoutSid, rep.Confirmed, rep.NotStats)
	fmt.Fprintf(c.out, "skipped: already in STATS from an agent %d, already imported %d, not a valid line %d, "+
		"too big %d, no agent name %d, too recent %d, over the cap per name %d, no room left when publishing %d\n",
		rep.SkippedInStats, rep.SkippedImported, rep.SkippedInvalid, rep.SkippedTooBig, rep.SkippedName,
		rep.SkippedRecent, rep.SkippedOverCap, rep.SkippedRoom)
	if rep.Oldest != "" {
		fmt.Fprintf(c.out, "published lines from %s to %s\n", clean(rep.Oldest), clean(rep.Newest))
	}
	if rep.All {
		fmt.Fprintln(c.out, "the whole channel was read (--all)")
	}
	if rep.KnownBefore != "" {
		fmt.Fprintf(c.out, "posts up to %s were gone through by an earlier complete run of the service "+
			"and not read again (--all reads them)\n", clean(rep.KnownBefore))
	}
	again := "run it again"
	if rep.All {
		again = "run it again with --all"
	}
	switch rep.Truncated {
	case "":
	case "room":
		fmt.Fprintln(c.out, "older lines were not published: STATS has no more room "+
			"(it would drop its oldest lines); make room, then "+again)
	default:
		fmt.Fprintf(c.out, "older lines were not published (the run's bound); %s to take them\n", again)
	}
	return nil
}
