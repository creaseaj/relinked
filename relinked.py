#!/usr/bin/env python3
"""relinked.py - manual-drive LinkedIn name harvester.

A spiritual successor to CrossLinked (now abandoned), rebuilt around a
simple idea: the TOOL does no navigating. It opens a real, visible browser,
hands it to you, and then only READS whatever Google or Bing results page
you have on screen - harvesting the public LinkedIn employee names it finds.

You do all the driving yourself: open Google or Bing, type your own
searches, page through the results, solve any consent/robot wall by hand.
Because every query, click and scroll is a genuine human action, there is
no automated-navigation fingerprint for the engines to flag. relinked just
watches: a few times a second it reads the page already loaded in the
browser (a local DOM read - no request is sent to the engine) and, whenever
the page changes, harvests the LinkedIn names off it automatically. You
never have to touch the terminal while you work.

No API keys, no credentials, no direct requests to LinkedIn, and - unlike
the old version - no automated query submission or "Next" clicking.

Intended for authorised security testing / OSINT only.

    python3 relinked.py -t "Target Company"
    python3 relinked.py -t "Target Company" -f '{first}.{last}@target.com'
    python3 relinked.py -t "Target Company" --csv -o names.txt

Dependencies: selenium, beautifulsoup4   (lxml optional, for faster parsing)
    pip3 install selenium beautifulsoup4 lxml

------------------------------------------------------------------------
HOW TO USE

  1. Run it. A Chrome window opens (and stays open on its own profile, so
     cookies/consent survive between runs).
  2. In that window, go to Google or Bing and search however you like, e.g.
         site:linkedin.com/in "Target Company"
     Vary the phrasing, add regional subdomains (au.linkedin.com, ...),
     page through results - all by hand. Solve any CAPTCHA/consent wall
     yourself; relinked never touches it.
  3. That's it. As each results page loads, relinked harvests the names
     automatically, prints a running total, and writes them to the output
     file live (so you can `tail -f names.txt`, and nothing is lost if the
     browser or terminal dies). Keep searching and paging as long as you like.
  4. When you're done, press Ctrl+C in this terminal (or just close the
     browser window). The CSV / email list (if requested) are written then.

TROUBLESHOOTING

  "selenium not installed":  pip3 install selenium
  No window appears: you're on a headless/SSH host. Run it on a machine
    with a display, or use X-forwarding.
  chromedriver errors: install a chromedriver matching your Chrome, or just
    upgrade selenium (recent versions resolve the driver automatically).
  "no names showing up": make sure you're on a Google or Bing *results*
    page - not the homepage, and not LinkedIn itself. Only result pages from
    those two engines are harvested.
  Names look wrong (titles, extra words): the parser keeps the first and
    last token of the name chunk; eyeball the output (or the --csv) and
    prune before generating emails. Re-run with -v to see source titles.
------------------------------------------------------------------------
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from html import unescape
from urllib.parse import urlparse

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("[-] Missing deps. Run: pip3 install selenium beautifulsoup4")

# Parse result pages with lxml when it's installed (several times faster on
# the large HTML a search results page produces) and fall back to the stdlib
# parser otherwise, so lxml stays an optional speed-up, not a requirement.
try:
    import lxml  # noqa: F401
    _PARSER = "lxml"
except ImportError:
    _PARSER = "html.parser"


# Trailing site markers to strip from result titles, various locales/dashes.
TITLE_TAIL = re.compile(r"\s*[\|\-–—]\s*LinkedIn.*$", re.IGNORECASE)
NAME_SPLIT = re.compile(r"\s+[\-–—\|]\s+")      # " - ", " – ", " | "
VIEW_PREFIX = re.compile(r"^view\s+", re.IGNORECASE)
POSSESSIVE = re.compile(r"[’']s\s+profile.*$", re.IGNORECASE)


class Harvester:
    """Holds the browser and the accumulated names. Does no navigation -
    it only reads whatever page you have on screen and harvests names."""

    def __init__(self, company: str, safe: bool,
                 user_data_dir: str | None = None):
        self.company = company.lower()
        self.safe = safe
        self.user_data_dir = user_data_dir
        # name -> title (first title seen)
        self.found: dict[str, str] = {}
        # name -> full uncollapsed name chunk (for manual CSV cleanup)
        self.chunks: dict[str, str] = {}
        self._driver = None

    # -- browser -----------------------------------------------------------
    def open_browser(self):
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
        except ImportError:
            sys.exit("[-] relinked needs selenium: pip3 install selenium "
                     "(and a matching chromedriver on PATH)")
        opts = Options()
        opts.add_argument("--window-size=1200,900")
        # Hide the signs that this Chrome is Selenium-driven (the automation
        # infobar, navigator.webdriver, the AutomationControlled flag) so it
        # behaves like an ordinary browser you opened yourself. We deliberately
        # do NOT override the User-Agent: a real, current Chrome already
        # presents a UA that is consistent with its TLS/client-hint
        # fingerprint, whereas a spoofed UA disagrees with them and is itself a
        # bot tell.
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        # Persistent profile: clear a consent/robot wall ONCE, not per run.
        if self.user_data_dir:
            opts.add_argument(f"--user-data-dir={self.user_data_dir}")
        self._driver = webdriver.Chrome(options=opts)
        try:
            self._driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator, 'webdriver', "
                           "{get: () => undefined});"})
        except Exception:
            pass  # best effort only
        # Open a blank tab and stop there - YOU do every bit of the
        # navigating, including the very first page load, so nothing automated
        # ever hits a search engine.
        try:
            self._driver.get("about:blank")
        except Exception:
            pass
        return self._driver

    def window_closed(self) -> bool:
        """True once the browser is gone: the user closed the last window (no
        handles left) or the driver session has died (enumerating handles
        raises). Only consulted after a probe has already failed, so an
        exception here means the session really is unusable."""
        if self._driver is None:
            return True
        try:
            return not self._driver.window_handles
        except Exception:
            return True

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass

    # -- watching the current page ----------------------------------------
    # A tiny JS probe: current URL plus the number of result headings on the
    # page. It changes whenever you run a new search, turn a page (the URL's
    # start=/first= param moves) or scroll in more results (the heading count
    # grows). Returning this ~short string each tick is far cheaper than
    # serialising the whole DOM, and it is a more reliable "did the page
    # change" signal than the page's byte length (which jitters with ads and
    # can collide between genuinely different pages).
    _PROBE_JS = ("return location.href + '|' + "
                 "document.querySelectorAll('h2,h3').length;")

    def change_probe(self) -> str | None:
        """Cheap change fingerprint read from the live DOM, or None if the
        page can't be read (window closed / mid-navigation). Sends no request
        to the engine."""
        if self._driver is None:
            return None
        try:
            return self._driver.execute_script(self._PROBE_JS)
        except Exception:
            return None

    def read_page(self) -> tuple[str, str] | None:
        """Read the current URL + full page HTML straight from the DOM. Only
        called once the probe says the page changed, so the expensive
        page_source pull happens per real page, not per tick. Still a local
        read - no request reaches the engine."""
        if self._driver is None:
            return None
        try:
            return (self._driver.current_url or "",
                    self._driver.page_source or "")
        except Exception:
            return None

    def capture(self, url: str, html: str) -> int:
        """Harvest names from an already-read results page. Auto-detects
        Google vs Bing by URL; falls back to trying both. Returns the number
        of NEW names added."""
        soup = BeautifulSoup(html, _PARSER)
        host = urlparse(url).netloc.lower()
        before = len(self.found)
        if "bing." in host:
            self._harvest_bing(soup)
        elif "google." in host:
            self._harvest_google(soup)
        else:
            # Unknown page - try both harvesters, keep whatever sticks.
            self._harvest_google(soup)
            self._harvest_bing(soup)
        return len(self.found) - before

    def flush_names(self, path: str) -> None:
        """Write the names gathered so far to `path`, atomically (write a temp
        file then rename) so a crash or kill mid-session always leaves a
        complete file. Called after every capture that adds names, so output
        is never lost and you can tail it live."""
        names = sorted(self.found)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write("\n".join(names) + ("\n" if names else ""))
            os.replace(tmp, path)
        except OSError as exc:
            print(f"    [!] couldn't write {path}: {exc}", file=sys.stderr)

    # -- title -> name -----------------------------------------------------
    def _handle_title(self, title: str, known_linkedin: bool = False) -> None:
        title = unescape(title).strip()
        if not known_linkedin and "linkedin" not in title.lower():
            return
        if self.safe and self.company not in title.lower():
            return
        name_part = self._name_chunk(title)
        tokens = name_part.split()
        if len(tokens) < 2:
            return
        name = f"{tokens[0]} {tokens[-1]}"
        if name not in self.found:
            self.found[name] = title
            self.chunks[name] = name_part

    @staticmethod
    def _name_chunk(title: str) -> str:
        """The name portion of a result title, before the job-title separator,
        with site markers and 'View ... profile' wrappers stripped. NOT
        collapsed to first+last, so 'Snezana Bakalovski MAICD - Chief People
        Officer' -> 'Snezana Bakalovski MAICD'."""
        clean = TITLE_TAIL.sub("", title).strip()
        clean = VIEW_PREFIX.sub("", clean)
        clean = POSSESSIVE.sub("", clean)
        name_part = NAME_SPLIT.split(clean)[0].strip()
        return re.sub(r"[^A-Za-zÀ-ɏ '\-]", "", name_part).strip()

    # -- per-engine harvesting --------------------------------------------
    def _harvest_bing(self, soup: BeautifulSoup) -> None:
        """Keep only linkedin.com /in profiles (identified by each result's
        visible <cite> domain, since Bing cloaks the href) and harvest the
        <h2> title."""
        for li in soup.select("li.b_algo"):
            cite = li.find("cite")
            low = cite.get_text().lower() if cite else ""
            if "linkedin.com" not in low:
                continue
            if "company" in low or "jobs" in low:  # skip non-profile pages
                continue
            h2 = li.find("h2")
            if h2:
                self._handle_title(h2.get_text(), known_linkedin=True)

    def _harvest_google(self, soup: BeautifulSoup) -> None:
        """Google cloaks result hrefs behind opaque /goto?url= tokens, so we
        can't filter by URL. Instead each organic <h3> carries a visible
        source label ('LinkedIn . Name . N followers') in its block; keep a
        title only when its own result block is labelled LinkedIn."""
        for h3 in soup.select("h3"):
            if self._result_is_linkedin(h3):
                self._handle_title(h3.get_text(), known_linkedin=True)

    @staticmethod
    def _result_is_linkedin(h3) -> bool:
        """True if this result's own block is labelled LinkedIn. Climb
        ancestors while the container still wraps only this one <h3>, looking
        for a 'linkedin' source label. Stop as soon as a container holds
        another <h3> - that's a multi-result block, so a label there would
        belong to a neighbouring result."""
        node = h3
        for _ in range(8):
            node = node.parent
            if node is None:
                break
            if len(node.select("h3")) > 1:
                break
            if "linkedin" in node.get_text(" ", strip=True).lower():
                return True
        return False


# -- name formatting -------------------------------------------------------
def fmt_name(first: str, last: str, fmt: str) -> str:
    f = re.sub(r"[^a-z]", "", first.lower())
    l = re.sub(r"[^a-z]", "", last.lower())
    if not f or not l:
        return ""
    out = fmt
    for k, v in (("{first}", f), ("{last}", l), ("{f}", f[:1]), ("{l}", l[:1])):
        out = out.replace(k, v)
    return out


def watch_loop(h: Harvester, interval: float, output: str) -> None:
    """Hand the browser to the user and watch it. Every `interval` seconds we
    read the current page from the DOM (a local read - no request is sent to
    the engine) and, whenever the page has changed, harvest names from it
    automatically. Names are flushed to `output` as they arrive so nothing is
    lost if the session ends abruptly. You never touch this terminal until
    you're done."""
    import time
    print("\n[*] Browser is open - just drive it. relinked is watching.")
    print("      - go to Google or Bing and run your own searches, e.g.")
    print('          site:linkedin.com/in \"%s\"' % h.company)
    print("      - page through the results, solve any wall by hand")
    print("    Names are captured automatically as pages load.")
    print("    Stop when you're done: press Ctrl+C here, or just close the "
          "browser window.\n")
    last_sig = None
    misses = 0  # consecutive failed probes; a transient mid-nav read is fine
    try:
        while True:
            sig = h.change_probe()
            if sig is None:
                # Probe failed. If the browser is genuinely gone, stop; a lone
                # mid-navigation blip is not enough (require a short streak).
                misses += 1
                if h.window_closed() or misses >= 5:
                    print("    [i] browser is gone - wrapping up.")
                    break
                time.sleep(interval)
                continue
            misses = 0
            if sig != last_sig:
                last_sig = sig
                page = h.read_page()
                if page is not None:
                    added = h.capture(*page)
                    if added:
                        h.flush_names(output)  # crash-safe running output
                        print(f"    [+] {added} new name(s) -> "
                              f"{len(h.found)} total")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n    [i] stopped - wrapping up.")


def write_results(h: Harvester, args) -> None:
    names = sorted(h.found)
    if not names:
        print("[-] No names captured. Nothing written.", file=sys.stderr)
        return

    h.flush_names(args.output)  # same writer the watch loop used
    print(f"\n[+] {len(names)} unique names -> {args.output}")

    if args.verbose:
        for n in names:
            print(f"    {n}  ::  {h.found[n]}")

    if args.csv is not None:
        import csv
        cfile = (args.csv if args.csv != "<auto>"
                 else args.output.rsplit(".", 1)[0] + ".csv")
        with open(cfile, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            for n in names:
                w.writerow([h.chunks.get(n, n)])
        print(f"[+] {len(names)} rows -> {cfile}  (no header; one full name "
              "chunk per row - clean it up by hand)")

    if args.format:
        emails, seen = [], set()
        for n in names:
            toks = n.split()
            addr = fmt_name(toks[0], toks[-1], args.format)
            if addr and addr not in seen:
                seen.add(addr)
                emails.append(addr)
        efile = args.output.rsplit(".", 1)[0] + "_emails.txt"
        with open(efile, "w", encoding="utf-8") as fh:
            fh.write("\n".join(emails) + "\n")
        print(f"[+] {len(emails)} addresses -> {efile}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Manual-drive LinkedIn name harvester: you navigate a "
                    "real browser, relinked reads the Google/Bing results "
                    "page you have on screen.")
    ap.add_argument("-t", "--target", required=True,
                    help="company name as it appears on LinkedIn (used for "
                         "--safe filtering and as the suggested query)")
    ap.add_argument("-f", "--format",
                    help="email/username format, e.g. '{first}.{last}@corp.com'")
    ap.add_argument("-o", "--output", default="names.txt",
                    help="output file (default: names.txt)")
    ap.add_argument("--csv", nargs="?", const="<auto>", default=None,
                    metavar="PATH",
                    help="also write a header-less CSV of one full name per "
                         "row (uncollapsed, for manual cleanup); bare --csv "
                         "writes <output basename>.csv")
    ap.add_argument("-s", "--safe", action="store_true",
                    help="only keep titles containing the company name")
    ap.add_argument("--user-data-dir",
                    default=os.path.expanduser("~/.relinked-chrome"),
                    help="persistent Chrome profile so a consent/robot wall "
                         "is remembered across runs "
                         "(default: ~/.relinked-chrome; pass '' to disable)")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="seconds between page checks while watching "
                         "(default 1.0; each check is a tiny DOM read, it "
                         "sends no requests to the engine)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print captured names with their source titles")
    args = ap.parse_args()

    print(f"[*] relinked - harvesting names for '{args.target}'")
    h = Harvester(args.target, args.safe,
                  user_data_dir=(args.user_data_dir or None))
    try:
        h.open_browser()
        watch_loop(h, args.interval, args.output)
    finally:
        h.close()

    write_results(h, args)


if __name__ == "__main__":
    main()
