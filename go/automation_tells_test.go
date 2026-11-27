//go:build smoke

// Checks what automation tells the chromedp-driven browser exposes to page
// JavaScript - the things a site's anti-bot script could read. Mirrors the
// flags startBrowser() uses (no JS webdriver override - the flag alone yields
// the realistic value). Gated behind -tags smoke (needs Chrome):
//
//	go test -tags smoke -run TestAutomationTells -v
package main

import (
	"context"
	"os"
	"strings"
	"testing"

	"github.com/chromedp/chromedp"
)

func TestAutomationTells(t *testing.T) {
	chromeBin := "/usr/bin/google-chrome"
	if _, err := os.Stat(chromeBin); err != nil {
		t.Skip("no Chrome available")
	}
	// Same tell-suppression the real tool uses: the flag, and (deliberately)
	// no JS webdriver override.
	opts := append(chromedp.DefaultExecAllocatorOptions[:],
		chromedp.ExecPath(chromeBin),
		chromedp.Flag("no-sandbox", true),
		chromedp.Flag("disable-blink-features", "AutomationControlled"),
	)
	allocCtx, cancelAlloc := chromedp.NewExecAllocator(context.Background(), opts...)
	defer cancelAlloc()
	ctx, cancel := chromedp.NewContext(allocCtx)
	defer cancel()

	var tells string
	err := chromedp.Run(ctx,
		chromedp.Navigate("about:blank"),
		chromedp.Evaluate(`JSON.stringify({
			webdriver: String(navigator.webdriver),
			present: ('webdriver' in navigator),
			cdc: Object.getOwnPropertyNames(window).filter(n => n.toLowerCase().includes('cdc_')),
			docCdc: Object.getOwnPropertyNames(document).filter(n => n.toLowerCase().includes('cdc_'))
		})`, &tells),
	)
	if err != nil {
		t.Fatalf("probe failed (Chrome launchable?): %v", err)
	}
	t.Logf("automation tells seen by the page: %s", tells)
	if tells == "" {
		t.Fatal("no result")
	}
	// Realistic, like a browser you opened yourself: webdriver present and
	// false (NOT true, NOT undefined), and no chromedriver cdc_ variables.
	if !strings.Contains(tells, `"webdriver":"false"`) {
		t.Errorf("navigator.webdriver is not the realistic false: %s", tells)
	}
	if !strings.Contains(tells, `"cdc":[]`) || !strings.Contains(tells, `"docCdc":[]`) {
		t.Errorf("cdc_ automation variables present: %s", tells)
	}
}
