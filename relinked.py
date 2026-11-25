#!/usr/bin/env python3
"""relinked.py - manual-drive LinkedIn name harvester.

A spiritual successor to CrossLinked (now abandoned), rebuilt around a
simple idea: the TOOL does no navigating. It opens a real, visible browser,
hands it to you, and then only READS whatever Google or Bing results page
you have on screen - harvesting the public LinkedIn employee names it finds.

You do all the driving yourself: open Google or Bing, type your own
searches, page through the results, solve any consent/robot wall by hand.
Because every query, click and scroll is a genuine human action, there is
no automated-navigation fingerprint for the engines to flag. When a results
page looks worth keeping, you switch back to this terminal and press Enter;
relinked scrapes the names off the current page and adds them to the pile.

No API keys, no credentials, no direct requests to LinkedIn, and - unlike
the old version - no automated query submission or "Next" clicking.

Intended for authorised security testing / OSINT only.

    python3 relinked.py -t "Target Company"
    python3 relinked.py -t "Target Company" -f '{first}.{last}@target.com'
    python3 relinked.py -t "Target Company" --csv -o names.txt

Dependencies: selenium, beautifulsoup4
    pip3 install selenium beautifulsoup4

------------------------------------------------------------------------
HOW TO USE

  1. Run it. A Chrome window opens (and stays open on its own profile, so
     cookies/consent survive between runs).
  2. In that window, go to Google or Bing and search however you like, e.g.
         site:linkedin.com/in "Target Company"
     Vary the phrasing, add regional subdomains (au.linkedin.com, ...),
     page through results - all by hand. Solve any CAPTCHA/consent wall
     yourself; relinked never touches it.
  3. When a results page is showing names you want, come back to this
     terminal and press Enter. relinked reads the CURRENT page and reports
     how many new names it harvested.
  4. Repeat for as many pages/searches as you like.
  5. Type  q  then Enter to finish. Names (and optional CSV / email list)
     are written out.

TROUBLESHOOTING

  "selenium not installed":  pip3 install selenium
  No window appears: you're on a headless/SSH host. Run it on a machine
    with a display, or use X-forwarding.
  chromedriver errors: install a chromedriver matching your Chrome, or just
    upgrade selenium (recent versions resolve the driver automatically).
  "captured 0 names": make sure a Google or Bing *results* page is the
    active tab when you press Enter - not the homepage, not LinkedIn itself.
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


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

# Trailing site markers to strip from result titles, various locales/dashes.
TITLE_TAIL = re.compile(r"\s*[\|\-–—]\s*LinkedIn.*$", re.IGNORECASE)
NAME_SPLIT = re.compile(r"\s+[\-–—\|]\s+")      # " - ", " – ", " | "
VIEW_PREFIX = re.compile(r"^view\s+", re.IGNORECASE)
POSSESSIVE = re.compile(r"[’']s\s+profile.*$", re.IGNORECASE)


class Harvester:
    """Holds the browser and the accumulated names. Does no navigation -
    only reads the page you have on screen when you ask it to."""

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
        import random
        opts = Options()
        opts.add_argument("--window-size=1200,900")
        # Mild anti-automation hygiene so the window behaves like an ordinary
        # browser. You still do every bit of the navigating by hand.
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_argument(f"--user-agent={random.choice(USER_AGENTS)}")
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
        # Land somewhere useful; you take it from here.
        try:
            self._driver.get("https://www.google.com/")
        except Exception:
            pass
        return self._driver

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass

    # -- capture the current page -----------------------------------------
    def capture(self) -> int:
        """Read whatever results page is on screen and harvest names from it.
        Auto-detects Google vs Bing by URL; falls back to trying both.
        Returns the number of NEW names added."""
        if self._driver is None:
            return 0
        try:
            url = self._driver.current_url or ""
            html = self._driver.page_source or ""
        except Exception as exc:
            print(f"    [!] couldn't read the page ({exc}); is the window "
                  "still open?", file=sys.stderr)
            return 0
        soup = BeautifulSoup(html, "html.parser")
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


def interactive_loop(h: Harvester) -> None:
    """Hand the browser to the user; capture the current page each time they
    press Enter; stop on q/quit/EOF."""
    print("\n[*] Browser is open. Drive it yourself:")
    print("      - go to Google or Bing and run your own searches, e.g.")
    print('          site:linkedin.com/in \"%s\"' % h.company)
    print("      - page through the results, solve any wall by hand")
    print("    Then come back here and:")
    print("      [Enter]  capture names from the page currently on screen")
    print("      q+Enter  finish and write results\n")
    while True:
        try:
            cmd = input("[relinked] Enter = capture, q = quit > ").strip().lower()
        except EOFError:
            print()
            break
        if cmd in ("q", "quit", "exit"):
            break
        added = h.capture()
        if added:
            print(f"    [+] captured {added} new name(s); "
                  f"{len(h.found)} total")
        else:
            print(f"    [i] no new names on this page; {len(h.found)} total "
                  "(is a Google/Bing results page the active tab?)")


def write_results(h: Harvester, args) -> None:
    names = sorted(h.found)
    if not names:
        print("[-] No names captured. Nothing written.", file=sys.stderr)
        return

    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write("\n".join(names) + "\n")
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
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print captured names with their source titles")
    args = ap.parse_args()

    print(f"[*] relinked - harvesting names for '{args.target}'")
    h = Harvester(args.target, args.safe,
                  user_data_dir=(args.user_data_dir or None))
    try:
        h.open_browser()
        interactive_loop(h)
    finally:
        h.close()

    write_results(h, args)


if __name__ == "__main__":
    main()
