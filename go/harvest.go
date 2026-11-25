// harvest.go - turning Google/Bing result pages into employee names.
//
// This file is deliberately browser-free: it takes a results page as a
// string of HTML and extracts names, so it can be unit-tested without
// launching Chrome (see harvest_test.go). main.go feeds it the live page.
package main

import (
	"fmt"
	"os"
	"regexp"
	"sort"
	"strings"
	"unicode"

	"github.com/PuerkitoBio/goquery"
)

// Splitting a result title into a name (see nameChunk), mirroring the Python
// version's regexes.
var (
	nameSplit  = regexp.MustCompile(`\s+[\-–—|]\s+`) // " - ", " – ", " | "
	viewPrefix = regexp.MustCompile(`(?i)^view\s+`)
	possessive = regexp.MustCompile(`(?i)[’']s\s+profile.*$`)
	brackets   = regexp.MustCompile(`[\(\[][^)\]]*[\)\]]`) // "(He/Him)", "[Open to work]"
	nonAlpha   = regexp.MustCompile(`[^a-z]`)
)

// Harvester holds the accumulated names. It does no navigation - it only
// turns pages you already have on screen into names.
type Harvester struct {
	company string            // lower-cased target, for --safe filtering
	safe    bool              // keep only titles containing the company name
	found   map[string]string // "First Last" -> first title it was seen in
	chunks  map[string]string // "First Last" -> full uncollapsed name chunk
}

func NewHarvester(company string, safe bool) *Harvester {
	return &Harvester{
		company: strings.ToLower(company),
		safe:    safe,
		found:   map[string]string{},
		chunks:  map[string]string{},
	}
}

// Capture harvests names from an already-read results page. It auto-detects
// Google vs Bing from the URL host and falls back to trying both. Returns the
// number of NEW names added.
func (h *Harvester) Capture(url, html string) int {
	doc, err := goquery.NewDocumentFromReader(strings.NewReader(html))
	if err != nil {
		return 0
	}
	host := hostOf(url)
	before := len(h.found)
	switch {
	case strings.Contains(host, "bing."):
		h.harvestBing(doc)
	case strings.Contains(host, "google."):
		h.harvestGoogle(doc)
	default:
		// Unknown page - try both harvesters, keep whatever sticks.
		h.harvestGoogle(doc)
		h.harvestBing(doc)
	}
	return len(h.found) - before
}

// Names returns the collected names, sorted.
func (h *Harvester) Names() []string {
	names := make([]string, 0, len(h.found))
	for n := range h.found {
		names = append(names, n)
	}
	sort.Strings(names)
	return names
}

// FlushNames writes the names gathered so far to path, atomically (temp file
// then rename) so a crash or kill mid-session always leaves a complete file.
// Called after every capture that adds names, so output is never lost and you
// can tail it live.
func (h *Harvester) FlushNames(path string) {
	names := h.Names()
	body := ""
	if len(names) > 0 {
		body = strings.Join(names, "\n") + "\n"
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, []byte(body), 0o644); err != nil {
		fmt.Fprintf(os.Stderr, "    [!] couldn't write %s: %v\n", path, err)
		return
	}
	if err := os.Rename(tmp, path); err != nil {
		fmt.Fprintf(os.Stderr, "    [!] couldn't write %s: %v\n", path, err)
	}
}

// -- title -> name ---------------------------------------------------------

func (h *Harvester) handleTitle(title string, knownLinkedIn bool) {
	title = strings.TrimSpace(title)
	low := strings.ToLower(title)
	// When the caller already matched a LinkedIn result the title itself need
	// not contain the word (profile titles are often "Name - Role at Co").
	if !knownLinkedIn && !strings.Contains(low, "linkedin") {
		return
	}
	if h.safe && !strings.Contains(low, h.company) {
		return
	}
	name := nameChunk(title)
	if len(strings.Fields(name)) < 2 { // need at least a first and a last name
		return
	}
	if _, seen := h.found[name]; !seen {
		h.found[name] = title
		h.chunks[name] = name
	}
}

// nameChunk reduces a result title to a person's full name, the way
// GatherContacts does: cut the "| LinkedIn" suffix, take the part before the
// first " - " job-title separator, drop post-nominals after a comma, and KEEP
// THE WHOLE NAME (first, any middle names, last) rather than collapsing to
// first+last.
//
//	"Mary Jane Watson - UX Designer, MBA | LinkedIn" -> "Mary Jane Watson"
//	"John Smith, CPA - Finance at Acme"              -> "John Smith"
func nameChunk(title string) string {
	s := strings.SplitN(title, "|", 2)[0]  // drop "| LinkedIn" and anything after
	s = nameSplit.Split(s, -1)[0]           // name is before the first " - "/" – "
	s = strings.SplitN(s, ",", 2)[0]        // drop ", CPA" / ", PhD" credentials
	s = strings.TrimSpace(s)
	s = viewPrefix.ReplaceAllString(s, "")  // "View <name>'s profile" wrappers
	s = possessive.ReplaceAllString(s, "")
	s = brackets.ReplaceAllString(s, " ")   // "(He/Him)", "[Open to work]", etc.
	// Keep letters of any script, spaces, apostrophes and hyphens; turn the
	// rest (emoji, digits, symbols) into spaces. Unicode-aware, so non-Latin
	// names survive - the old Latin-only filter dropped them.
	s = strings.Map(func(r rune) rune {
		if unicode.IsLetter(r) || r == ' ' || r == '\'' || r == '-' {
			return r
		}
		return ' '
	}, s)
	return strings.Join(strings.Fields(s), " ") // collapse whitespace and trim
}

// -- per-engine harvesting -------------------------------------------------

// harvestBing keeps only linkedin.com /in profiles (identified by each
// result's visible <cite> domain, since Bing cloaks the href) and harvests
// the <h2> title.
func (h *Harvester) harvestBing(doc *goquery.Document) {
	doc.Find("li.b_algo").Each(func(_ int, li *goquery.Selection) {
		low := strings.ToLower(li.Find("cite").First().Text())
		if !strings.Contains(low, "linkedin.com") {
			return
		}
		if strings.Contains(low, "company") || strings.Contains(low, "jobs") {
			return // skip non-profile pages
		}
		if h2 := li.Find("h2").First(); h2.Length() > 0 {
			h.handleTitle(h2.Text(), true)
		}
	})
}

// harvestGoogle: Google cloaks result hrefs behind opaque /goto?url= tokens,
// so we can't filter by URL. Instead each organic <h3> carries a visible
// source label ("LinkedIn . Name . N followers") in its block; keep a title
// only when its own result block is labelled LinkedIn.
func (h *Harvester) harvestGoogle(doc *goquery.Document) {
	doc.Find("h3").Each(func(_ int, h3 *goquery.Selection) {
		if resultIsLinkedIn(h3) {
			h.handleTitle(h3.Text(), true)
		}
	})
}

// resultIsLinkedIn reports whether this result's own block is labelled
// LinkedIn. Climb ancestors while the container still wraps only this one
// <h3>, looking for a "linkedin" source label. Stop as soon as a container
// holds another <h3> - that's a multi-result block, so a label there would
// belong to a neighbouring result.
func resultIsLinkedIn(h3 *goquery.Selection) bool {
	node := h3
	for i := 0; i < 8; i++ {
		node = node.Parent()
		if node.Length() == 0 {
			break
		}
		if node.Find("h3").Length() > 1 {
			break // climbed into a block containing other results
		}
		if strings.Contains(strings.ToLower(node.Text()), "linkedin") {
			return true
		}
	}
	return false
}

// -- name formatting -------------------------------------------------------

// fmtName renders first/last into an email/username per a {first}/{last}/
// {f}/{l} template, e.g. "{first}.{last}@corp.com".
func fmtName(first, last, format string) string {
	f := nonAlpha.ReplaceAllString(strings.ToLower(first), "")
	l := nonAlpha.ReplaceAllString(strings.ToLower(last), "")
	if f == "" || l == "" {
		return ""
	}
	out := format
	out = strings.ReplaceAll(out, "{first}", f)
	out = strings.ReplaceAll(out, "{last}", l)
	out = strings.ReplaceAll(out, "{f}", f[:1])
	out = strings.ReplaceAll(out, "{l}", l[:1])
	return out
}

// hostOf extracts the lower-cased host from a URL without pulling in net/url
// for what is only a substring check.
func hostOf(url string) string {
	s := url
	if i := strings.Index(s, "://"); i >= 0 {
		s = s[i+3:]
	}
	if i := strings.IndexAny(s, "/?#"); i >= 0 {
		s = s[:i]
	}
	return strings.ToLower(s)
}
