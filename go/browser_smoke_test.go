//go:build smoke

// End-to-end smoke test of the real chromedp pipeline (probe -> readPage ->
// Capture) against a headless browser and a local SERP fixture. It needs a
// Chrome/Chromium, so it is gated behind the `smoke` build tag and run
// explicitly:  go test -tags smoke -run TestBrowserPipeline -v
//
// It cannot exercise the visible, human-driven part (that needs a display and
// a person), but it proves the DevTools reads and the harvest wiring work on
// a live page.
package main

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/chromedp/chromedp"
)

const googleFixture = `<!doctype html><html><head><title>q - Google Search</title></head>
<body>
  <div class="g"><div><span>LinkedIn</span></div><h3>Carol White - VP at Acme Corp</h3></div>
  <div class="g"><div><span>example.com</span></div><h3>Acme Corp Official Website</h3></div>
</body></html>`

func TestBrowserPipeline(t *testing.T) {
	chromeBin := "/usr/bin/google-chrome"
	if _, err := os.Stat(chromeBin); err != nil {
		if _, err := os.Stat("/opt/pw-browsers/chromium"); err == nil {
			chromeBin = "/opt/pw-browsers/chromium"
		} else {
			t.Skip("no Chrome/Chromium available")
		}
	}

	// Write the fixture to a temp file and load it over file://.
	dir := t.TempDir()
	fpath := filepath.Join(dir, "serp.html")
	if err := os.WriteFile(fpath, []byte(googleFixture), 0o644); err != nil {
		t.Fatal(err)
	}

	opts := append(chromedp.DefaultExecAllocatorOptions[:],
		chromedp.ExecPath(chromeBin),
		chromedp.Flag("no-sandbox", true),          // required running as root in CI
		chromedp.Flag("disable-dev-shm-usage", true),
	)
	allocCtx, cancelAlloc := chromedp.NewExecAllocator(context.Background(), opts...)
	defer cancelAlloc()
	ctx, cancel := chromedp.NewContext(allocCtx)
	defer cancel()

	// Navigate the fixture, then use the SAME probe()/readPage() the watch
	// loop uses.
	runCtx, runCancel := context.WithTimeout(ctx, 30*time.Second)
	defer runCancel()
	if err := chromedp.Run(runCtx, chromedp.Navigate("file://"+fpath)); err != nil {
		t.Fatalf("navigate (is Chrome launchable here?): %v", err)
	}

	sig, err := probe(ctx)
	if err != nil {
		t.Fatalf("probe: %v", err)
	}
	if sig == "" {
		t.Fatal("probe returned empty signature")
	}
	t.Logf("probe signature: %s", sig)

	url, html, err := readPage(ctx)
	if err != nil {
		t.Fatalf("readPage: %v", err)
	}

	h := NewHarvester("Acme Corp", false)
	added := h.Capture(url, html)
	if added != 1 {
		t.Fatalf("Capture added %d names, want 1; url=%s", added, url)
	}
	if names := h.Names(); len(names) != 1 || names[0] != "Carol White" {
		t.Fatalf("names = %v, want [Carol White]", names)
	}
	t.Logf("harvested %v from live headless page", h.Names())
}
