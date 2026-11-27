// relinked - manual-drive LinkedIn name harvester (single-binary Go port).
//
// The tool does no navigating. It opens a real, visible Chrome, hands it to
// you, and then only READS whatever Google or Bing results page you have on
// screen - harvesting the public LinkedIn employee names it finds. You do all
// the driving: open Google or Bing, type your own searches, page through the
// results, solve any consent/robot wall by hand. Because every query, click
// and scroll is a genuine human action, there is no automated-navigation
// fingerprint for the engines to flag. relinked just watches: a few times a
// second it reads the page already loaded in the browser (a local DevTools
// read - no request is sent to the engine) and, whenever the page changes,
// harvests the names off it automatically.
//
// Unlike the Python version this is a self-contained binary: it drives Chrome
// directly over the DevTools protocol (chromedp), so there is no Python, no
// Selenium and no chromedriver to install - only a Chrome/Chromium on the box.
//
// Intended for authorised security testing / OSINT only.
//
// Build:  go build -o relinked .
// Run:    ./relinked -t "Target Company"
//         ./relinked -t "Target Company" -f '{first}.{last}@target.com'
//         ./relinked -t "Target Company" --csv -o names.txt
package main

import (
	"context"
	"encoding/csv"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"time"

	"github.com/chromedp/chromedp"
)

// A tiny JS probe: current URL plus the number of result headings on the
// page. It changes whenever you run a new search, turn a page (the URL's
// start=/first= param moves) or scroll in more results (the heading count
// grows). Reading this short string each tick is far cheaper than serialising
// the whole DOM, and it is a more reliable "did the page change" signal than
// the page's byte length (which jitters with ads and can collide between
// genuinely different pages). It is an expression (not a statement) because
// chromedp's Evaluate runs it through Runtime.evaluate.
const probeJS = `location.href + '|' + document.querySelectorAll('h2,h3').length`

type config struct {
	target      string
	format      string
	output      string
	csv         bool
	csvOut      string
	safe        bool
	userDataDir string
	chromePath  string
	noSandbox   bool
	interval    time.Duration
	verbose     bool
}

func parseFlags() config {
	var c config
	var intervalSec float64
	home, _ := os.UserHomeDir()
	defDir := filepath.Join(home, ".relinked-chrome")

	// Register short and long names against the same variables so both -t and
	// --target work, matching the Python CLI.
	str := func(p *string, def, usage, short, long string) {
		flag.StringVar(p, short, def, usage)
		if long != "" {
			flag.StringVar(p, long, def, "(same as -"+short+")")
		}
	}
	boolf := func(p *bool, usage, short, long string) {
		flag.BoolVar(p, short, false, usage)
		if long != "" {
			flag.BoolVar(p, long, false, "(same as -"+short+")")
		}
	}

	str(&c.target, "", "company name as it appears on LinkedIn", "t", "target")
	str(&c.format, "", "email/username format, e.g. '{first}.{last}@corp.com'", "f", "format")
	str(&c.output, "names.txt", "output file", "o", "output")
	str(&c.csvOut, "", "write the header-less CSV to this path (implies --csv)", "csv-out", "")
	str(&c.userDataDir, defDir, "persistent Chrome profile dir (empty to disable)", "user-data-dir", "")
	str(&c.chromePath, "", "path to a Chrome/Chromium binary (default: auto-detect)", "chrome", "")
	boolf(&c.csv, "also write a header-less CSV of one full name per row (<output>.csv)", "csv", "")
	boolf(&c.safe, "only keep titles containing the company name", "s", "safe")
	boolf(&c.verbose, "print captured names with their source titles", "v", "verbose")
	boolf(&c.noSandbox, "launch Chrome with --no-sandbox (auto-enabled when running as root, e.g. root Kali)", "no-sandbox", "")
	flag.Float64Var(&intervalSec, "interval", 1.0,
		"seconds between page checks while watching (a tiny DOM read; sends no requests)")

	flag.Usage = func() {
		fmt.Fprintln(os.Stderr,
			"relinked - manual-drive LinkedIn name harvester.\n\n"+
				"You open a real browser it hands you and search Google/Bing by hand;\n"+
				"relinked only reads the results page on screen and harvests the names.\n\n"+
				"Usage:\n"+
				"  relinked -t \"Target Company\" [-f '{first}.{last}@target.com'] [--csv] [options]\n\n"+
				"Options (short and --long forms both work):")
		flag.PrintDefaults()
	}

	flag.Parse()
	c.interval = time.Duration(intervalSec * float64(time.Second))
	if c.csvOut != "" {
		c.csv = true
	}
	if c.interval < 200*time.Millisecond {
		c.interval = 200 * time.Millisecond
	}
	return c
}

func main() {
	c := parseFlags()
	if strings.TrimSpace(c.target) == "" {
		fmt.Fprintln(os.Stderr, "[-] -t/--target is required (company name as it appears on LinkedIn)")
		flag.Usage()
		os.Exit(2)
	}

	fmt.Printf("[*] relinked - harvesting names for '%s'\n", c.target)
	h := NewHarvester(c.target, c.safe)

	ctx, cancel := startBrowser(c)
	defer cancel()

	watch(ctx, h, c)
	writeResults(h, c)
}

// startBrowser launches a visible Chrome under our control and returns a
// chromedp context plus a cancel that tears the browser down.
func startBrowser(c config) (context.Context, context.CancelFunc) {
	opts := append(chromedp.DefaultExecAllocatorOptions[:],
		chromedp.Flag("headless", false), // visible on purpose - you drive it
		// With this flag and no --enable-automation (chromedp doesn't pass it),
		// navigator.webdriver reads false - exactly like a browser you opened
		// yourself. We intentionally do NOT override it in JS: forcing it to
		// undefined is itself a tell, since a real browser returns false.
		chromedp.Flag("disable-blink-features", "AutomationControlled"),
		chromedp.WindowSize(1200, 900),
	)
	if c.userDataDir != "" {
		opts = append(opts, chromedp.UserDataDir(c.userDataDir))
	}
	if c.chromePath != "" {
		opts = append(opts, chromedp.ExecPath(c.chromePath))
	}
	// Chrome refuses to run as root unless sandboxing is disabled. Kali is
	// frequently used as root, so auto-add --no-sandbox there (and --disable-
	// dev-shm-usage, which also avoids crashes in containers / WSL with a tiny
	// /dev/shm). os.Geteuid returns -1 on non-Unix, so this never fires there.
	if c.noSandbox || os.Geteuid() == 0 {
		if os.Geteuid() == 0 && !c.noSandbox {
			fmt.Println("[i] running as root - launching Chrome with --no-sandbox")
		}
		opts = append(opts,
			chromedp.Flag("no-sandbox", true),
			chromedp.Flag("disable-dev-shm-usage", true),
		)
	}

	allocCtx, cancelAlloc := chromedp.NewExecAllocator(context.Background(), opts...)
	ctx, cancelCtx := chromedp.NewContext(allocCtx)

	// Start the browser and land on a blank tab - YOU make even the first real
	// page load, so nothing automated ever hits a search engine.
	err := chromedp.Run(ctx, chromedp.Navigate("about:blank"))
	if err != nil {
		cancelCtx()
		cancelAlloc()
		fmt.Fprintf(os.Stderr, "[-] couldn't launch Chrome: %v\n"+
			"    Make sure a Chrome/Chromium is installed and on a machine with a\n"+
			"    display. On Kali it isn't installed by default: sudo apt install chromium\n"+
			"    (Firefox won't work - this needs Chrome/Chromium). Or pass --chrome <path>.\n", err)
		os.Exit(1)
	}
	return ctx, func() { cancelCtx(); cancelAlloc() }
}

// watch hands the browser to the user and polls it. Each tick it reads a cheap
// change-probe; when the page has changed it pulls the full HTML and harvests.
// Stops on Ctrl+C or when the browser window is gone.
func watch(ctx context.Context, h *Harvester, c config) {
	fmt.Println("\n[*] Browser is open - just drive it. relinked is watching.")
	fmt.Println("      - go to Google or Bing and run your own searches, e.g.")
	fmt.Printf("          site:linkedin.com/in \"%s\"\n", c.target)
	fmt.Println("      - page through the results, solve any wall by hand")
	fmt.Println("    Names are captured automatically as pages load and written")
	fmt.Printf("    to %s live. Stop with Ctrl+C, or just close the browser.\n\n", c.output)

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt)
	defer signal.Stop(sigCh)

	ticker := time.NewTicker(c.interval)
	defer ticker.Stop()

	var lastSig string
	misses := 0
	for {
		select {
		case <-sigCh:
			fmt.Println("\n    [i] stopped - wrapping up.")
			return
		case <-ctx.Done():
			fmt.Println("    [i] browser is gone - wrapping up.")
			return
		case <-ticker.C:
		}

		sig, err := probe(ctx)
		if err != nil {
			// A lone mid-navigation blip is fine; a streak (or a dead context)
			// means the browser is really gone.
			misses++
			if ctx.Err() != nil || misses >= 5 {
				fmt.Println("    [i] browser is gone - wrapping up.")
				return
			}
			continue
		}
		misses = 0
		if sig == lastSig {
			continue
		}
		lastSig = sig

		url, html, err := readPage(ctx)
		if err != nil {
			continue
		}
		if added := h.Capture(url, html); added > 0 {
			h.FlushNames(c.output) // crash-safe running output
			fmt.Printf("    [+] %d new name(s) -> %d total\n", added, len(h.found))
		}
	}
}

// probe reads the cheap change fingerprint from the live DOM.
func probe(ctx context.Context) (string, error) {
	cctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	var sig string
	err := chromedp.Run(cctx, chromedp.Evaluate(probeJS, &sig))
	return sig, err
}

// readPage pulls the current URL and full page HTML straight from the DOM.
// Only called once the probe says the page changed, so the expensive HTML
// pull happens per real page, not per tick. Still a local read.
func readPage(ctx context.Context) (url, html string, err error) {
	cctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	err = chromedp.Run(cctx,
		chromedp.Location(&url),
		chromedp.OuterHTML("html", &html, chromedp.ByQuery),
	)
	return url, html, err
}

func writeResults(h *Harvester, c config) {
	names := h.Names()
	if len(names) == 0 {
		fmt.Fprintln(os.Stderr, "[-] No names captured. Nothing written.")
		return
	}

	h.FlushNames(c.output) // same writer the watch loop used
	fmt.Printf("\n[+] %d unique names -> %s\n", len(names), c.output)

	if c.verbose {
		for _, n := range names {
			fmt.Printf("    %s  ::  %s\n", n, h.found[n])
		}
	}

	if c.csv {
		cfile := c.csvOut
		if cfile == "" {
			cfile = strings.TrimSuffix(c.output, filepath.Ext(c.output)) + ".csv"
		}
		if err := writeCSV(cfile, h, names); err != nil {
			fmt.Fprintf(os.Stderr, "    [!] couldn't write %s: %v\n", cfile, err)
		} else {
			fmt.Printf("[+] %d rows -> %s  (no header; one full name chunk per "+
				"row - clean it up by hand)\n", len(names), cfile)
		}
	}

	if c.format != "" {
		emails, seen := []string{}, map[string]bool{}
		for _, n := range names {
			toks := strings.Fields(n)
			addr := fmtName(toks[0], toks[len(toks)-1], c.format)
			if addr != "" && !seen[addr] {
				seen[addr] = true
				emails = append(emails, addr)
			}
		}
		efile := strings.TrimSuffix(c.output, filepath.Ext(c.output)) + "_emails.txt"
		body := strings.Join(emails, "\n") + "\n"
		if err := os.WriteFile(efile, []byte(body), 0o644); err != nil {
			fmt.Fprintf(os.Stderr, "    [!] couldn't write %s: %v\n", efile, err)
		} else {
			fmt.Printf("[+] %d addresses -> %s\n", len(emails), efile)
		}
	}
}

// writeCSV writes a header-less CSV of one full name chunk per row (middle
// names / post-nominals intact) for manual cleanup before generating emails.
func writeCSV(path string, h *Harvester, names []string) error {
	fh, err := os.Create(path)
	if err != nil {
		return err
	}
	defer fh.Close()
	w := csv.NewWriter(fh)
	for _, n := range names {
		chunk := h.chunks[n]
		if chunk == "" {
			chunk = n
		}
		if err := w.Write([]string{chunk}); err != nil {
			return err
		}
	}
	w.Flush()
	return w.Error()
}
