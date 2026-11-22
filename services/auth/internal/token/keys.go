package token

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/MicahParks/jwkset"
	"github.com/MicahParks/keyfunc/v3"
	"golang.org/x/time/rate"
)

// Bounds of a JWKS fetch.
const (
	jwksTimeout  = 10 * time.Second
	maxJWKSBytes = 1 << 20
)

// errJWKSTooLarge ends a JWKS answer beyond maxJWKSBytes.
var errJWKSTooLarge = errors.New("JWKS answer too large")

// newKeys returns the realm's signing keys: fetched every cfg.KeyRefresh, and
// when a token names an unknown key, at most once per cfg.UnknownKeyRefresh.
// A JWKS that cannot be fetched at the start is no error: the first token
// with a key id fetches it again.
func newKeys(ctx context.Context, cfg Config) (keyfunc.Keyfunc, error) {
	onError := func(_ context.Context, err error) {
		if cfg.KeyFetchError != nil {
			cfg.KeyFetchError(err)
		}
	}
	store, err := jwkset.NewStorageFromHTTP(cfg.JWKSURL, jwkset.HTTPClientStorageOptions{
		Client:                    jwksClient(),
		Ctx:                       ctx,
		HTTPTimeout:               jwksTimeout,
		NoErrorReturnFirstHTTPReq: true,
		RefreshErrorHandler:       onError,
		RefreshInterval:           cfg.KeyRefresh,
	})
	if err != nil {
		return nil, err
	}
	keys, err := jwkset.NewHTTPClient(jwkset.HTTPClientOptions{
		HTTPURLs: map[string]jwkset.Storage{cfg.JWKSURL: store},
		// This bounds the wait for a refresh slot and the fetch itself. A slot
		// further away than this is refused at once (rate.Limiter.Wait does
		// not sleep past a deadline); one at most this far away is waited
		// for. The caller bounds Verify with a deadline of its own.
		RateLimitWaitMax:  jwksTimeout,
		RefreshUnknownKID: rate.NewLimiter(rate.Every(cfg.UnknownKeyRefresh), 1),
	})
	if err != nil {
		return nil, err
	}
	return keyfunc.New(keyfunc.Options{
		Ctx:          ctx,
		Storage:      keys,
		UseWhitelist: []jwkset.USE{jwkset.UseSig},
	})
}

// jwksClient follows no redirect (not to another host, not from https to
// http) and reads at most maxJWKSBytes of an answer.
func jwksClient() *http.Client {
	return &http.Client{
		Timeout: jwksTimeout,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return errors.New("JWKS redirect refused")
		},
		Transport: limitedTransport{http.DefaultTransport},
	}
}

type limitedTransport struct{ next http.RoundTripper }

func (t limitedTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	resp, err := t.next.RoundTrip(r)
	if err != nil {
		return nil, err
	}
	resp.Body = &limitedBody{rc: resp.Body, left: maxJWKSBytes}
	return resp, nil
}

// limitedBody fails, rather than truncates, an answer beyond its limit.
type limitedBody struct {
	rc   io.ReadCloser
	left int64
}

func (b *limitedBody) Read(p []byte) (int, error) {
	if b.left <= 0 {
		var one [1]byte
		if n, _ := b.rc.Read(one[:]); n > 0 {
			return 0, fmt.Errorf("%w (over %d bytes)", errJWKSTooLarge, maxJWKSBytes)
		}
		return 0, io.EOF
	}
	if int64(len(p)) > b.left {
		p = p[:b.left]
	}
	n, err := b.rc.Read(p)
	b.left -= int64(n)
	return n, err
}

func (b *limitedBody) Close() error { return b.rc.Close() }
