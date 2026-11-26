package mattermost

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"reflect"
	"strconv"
	"sync"
	"testing"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/registrar/core"
)

// The calls of the bridge's sweep (RT-6.44 step 8).

func TestListBotsReadsEveryPage(t *testing.T) {
	var pages []string
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v4/bots", func(w http.ResponseWriter, r *http.Request) {
		pages = append(pages, r.URL.Query().Get("page")+"/"+r.URL.Query().Get("per_page"))
		page, _ := strconv.Atoi(r.URL.Query().Get("page"))
		n := botPage
		if page == 2 {
			n = 3 // the short last page
		}
		bots := make([]Bot, n)
		for i := range bots {
			bots[i] = Bot{UserID: fmt.Sprintf("p%d-%d", page, i)}
		}
		_ = json.NewEncoder(w).Encode(bots)
	})
	bots, err := adapter(t, mux).ListBots(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(bots) != 2*botPage+3 || bots[2*botPage].UserID != "p2-0" {
		t.Fatalf("%d bots", len(bots))
	}
	want := []string{"0/200", "1/200", "2/200"}
	if !reflect.DeepEqual(pages, want) {
		t.Fatalf("pages %v, want %v", pages, want)
	}
}

func TestListBotsEmptyLastPage(t *testing.T) {
	calls := 0
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v4/bots", func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.Query().Get("page") == "0" {
			_ = json.NewEncoder(w).Encode(make([]Bot, botPage))
			return
		}
		_, _ = w.Write([]byte("[]"))
	})
	bots, err := adapter(t, mux).ListBots(context.Background())
	if err != nil || len(bots) != botPage || calls != 2 {
		t.Fatalf("%d bots, %d calls, %v", len(bots), calls, err)
	}
}

// A server that never ends its list cannot loop the sweep: the page bound
// gives an error, no bots.
func TestListBotsPageBound(t *testing.T) {
	calls := 0
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v4/bots", func(w http.ResponseWriter, _ *http.Request) {
		calls++
		_ = json.NewEncoder(w).Encode(make([]Bot, botPage))
	})
	bots, err := adapter(t, mux).ListBots(context.Background())
	if err == nil || bots != nil || calls != maxBotPages {
		t.Fatalf("%d bots, %d calls, %v", len(bots), calls, err)
	}
}

func TestListBotsError(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v4/bots", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
	})
	if bots, err := adapter(t, mux).ListBots(context.Background()); err == nil || bots != nil {
		t.Fatalf("%v %v", bots, err)
	}
}

// RevokeAll reads every page first, then revokes every active token,
// whatever its description; inactive ones are left.
func TestRevokeAllAcrossPages(t *testing.T) {
	var mu sync.Mutex
	var revoked []string
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v4/users/bot1/tokens", func(w http.ResponseWriter, r *http.Request) {
		page, _ := strconv.Atoi(r.URL.Query().Get("page"))
		n := tokenPage
		if page == 1 {
			n = 2
		}
		toks := make([]AccessToken, n)
		for i := range toks {
			toks[i] = AccessToken{ID: fmt.Sprintf("t%d-%d", page, i), Description: "theirs", IsActive: i%2 == 0}
		}
		if page == 0 {
			toks[0].Description = "bus bridge (RT-6.44)"
		}
		_ = json.NewEncoder(w).Encode(toks)
	})
	mux.HandleFunc("POST /api/v4/users/tokens/revoke", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]string
		_ = json.NewDecoder(r.Body).Decode(&body)
		mu.Lock()
		revoked = append(revoked, body["token_id"])
		mu.Unlock()
		_, _ = w.Write([]byte("{}"))
	})
	a := adapter(t, mux)
	a.Cfg.TokenDescription = "bus bridge (RT-6.44)"
	if err := a.RevokeAll(context.Background(), core.Agent{ID: "bot1"}); err != nil {
		t.Fatal(err)
	}
	// page 0: 100 active of 200; page 1: 1 active of 2
	if len(revoked) != tokenPage/2+1 || revoked[0] != "t0-0" || revoked[len(revoked)-1] != "t1-0" {
		t.Fatalf("%d revoked: %v ... %v", len(revoked), revoked[0], revoked[len(revoked)-1])
	}
	// RevokeIssued, by contrast, takes only the adapter's own
	revoked = nil
	if err := a.RevokeIssued(context.Background(), core.Agent{ID: "bot1"}); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(revoked, []string{"t0-0"}) {
		t.Fatalf("RevokeIssued revoked %v", revoked)
	}
}

func TestBotOfAnyAsksForDisabledBots(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v4/bots/{id}", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("include_deleted") != "true" {
			t.Errorf("no include_deleted: %s", r.URL.RawQuery)
		}
		if r.PathValue("id") != "off" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		_ = json.NewEncoder(w).Encode(Bot{UserID: "off", Description: "d", DeleteAt: 17})
	})
	a := adapter(t, mux)
	b, err := a.BotOfAny(context.Background(), "off")
	if err != nil || b == nil || b.DeleteAt != 17 {
		t.Fatalf("%+v %v", b, err)
	}
	if b, err := a.BotOfAny(context.Background(), "person"); err != nil || b != nil {
		t.Fatalf("a person's account: %+v %v", b, err)
	}
}

func TestEnableDisableAndDescribeBot(t *testing.T) {
	var mu sync.Mutex
	var calls []string
	mux := http.NewServeMux()
	for _, p := range []string{"POST /api/v4/bots/{id}/enable", "POST /api/v4/bots/{id}/disable", "PUT /api/v4/bots/{id}"} {
		mux.HandleFunc(p, func(w http.ResponseWriter, r *http.Request) {
			var body map[string]string
			_ = json.NewDecoder(r.Body).Decode(&body)
			mu.Lock()
			calls = append(calls, r.Method+" "+r.URL.Path+" "+body["description"])
			mu.Unlock()
			if r.PathValue("id") == "gone" {
				w.WriteHeader(http.StatusNotFound)
				return
			}
			_, _ = w.Write([]byte("{}"))
		})
	}
	a := adapter(t, mux)
	ctx := context.Background()
	if err := a.DisableBot(ctx, "b1"); err != nil {
		t.Fatal(err)
	}
	if err := a.EnableBot(ctx, "b1"); err != nil {
		t.Fatal(err)
	}
	if err := a.UpdateBotDescription(ctx, "b1", "new"); err != nil {
		t.Fatal(err)
	}
	if a.DisableBot(ctx, "gone") == nil || a.EnableBot(ctx, "gone") == nil || a.UpdateBotDescription(ctx, "gone", "x") == nil {
		t.Fatal("a 404 is no error")
	}
	want := []string{"POST /api/v4/bots/b1/disable ", "POST /api/v4/bots/b1/enable ", "PUT /api/v4/bots/b1 new"}
	if !reflect.DeepEqual(calls[:3], want) {
		t.Fatalf("calls %q", calls)
	}
}
