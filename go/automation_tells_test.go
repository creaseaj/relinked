//go:build smoke

// Checks what automation tells the chromedp-driven browser exposes to page
// JavaScript - the things a site's anti-bot script could read. Mirrors the
// flags/patch startBrowser() uses. Gated behind -tags smoke (needs Chrome):
//
//	go test -tags smoke -run TestAutomationTells -v
package main

import (
	"context"
	"os"
	"strings"
	"testing"

	"github.com/chromedp/cdproto/page"
	"github.com/chromedp/chromedp"
)

func TestAutomationTells(t *testing.T) {
	chromeBin := "/usr/bin/google-chrome"
	if _, err := os.Stat(chromeBin); err != nil {
		t.Skip("no Chrome available")
	}
	opts := append(chromedp.DefaultExecAllocatorOptions[:],
		chromedp.ExecPath(chromeBin),
		chromedp.Flag("no-sandbox", true),
		// Same tell-suppression the real tool uses:
		chromedp.Flag("disable-blink-features", "AutomationControlled"),
	)
	allocCtx, cancelAlloc := chromedp.NewExecAllocator(context.Background(), opts...)
	defer cancelAlloc()
	ctx, cancel := chromedp.NewContext(allocCtx)
	defer cancel()

	var tells string
	err := chromedp.Run(ctx,
		chromedp.ActionFunc(func(ctx context.Context) error {
			// The same webdriver override startBrowser installs.
			_, err := page.AddScriptToEvaluateOnNewDocument(webdriverPatch).Do(ctx)
			return err
		}),
		chromedp.Navigate("about:blank"),
		chromedp.Evaluate(`JSON.stringify({
			webdriver: String(navigator.webdriver),
			cdc: Object.getOwnPropertyNames(window).filter(n => n.toLowerCase().includes('cdc_')),
			docCdc: Object.getOwnPropertyNames(document).filter(n => n.toLowerCase().includes('cdc_'))
		})`, &tells),
	)
	if err != nil {
		t.Fatalf("probe failed (Chrome launchable?): %v", err)
	}
	t.Logf("automation tells seen by the page: %s", tells)

	// navigator.webdriver must not be true, and no chromedriver cdc_ vars.
	if tells == "" {
		t.Fatal("no result")
	}
	if strings.Contains(tells, `"webdriver":"true"`) {
		t.Errorf("navigator.webdriver is true: %s", tells)
	}
	if !strings.Contains(tells, `"cdc":[]`) || !strings.Contains(tells, `"docCdc":[]`) {
		t.Errorf("cdc_ automation variables present: %s", tells)
	}
}
