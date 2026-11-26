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

    # Use Firefox instead of Chrome (e.g. the firefox-esr already on Kali),
    # reusing a copy of your existing profile so its cookies come along:
    python3 relinked.py -t "Target Company" --firefox \
        --firefox-profile ~/.mozilla/firefox/xxxx.default-release

Dependencies: selenium, beautifulsoup4   (lxml optional, for faster parsing)
    pip3 install selenium beautifulsoup4 lxml
    Chrome needs a chromedriver; Firefox (--firefox) needs geckodriver - recent
    selenium fetches both automatically.

------------------------------------------------------------------------
HOW TO USE

  1. Run it. A browser window opens - Chrome by default, or Firefox with
     --firefox (and stays open on its own profile, so cookies/consent survive
     between runs; or pass --firefox-profile to bring an existing profile's
     cookies).
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
  Names look wrong (titles, extra words): the parser keeps the whole name
    chunk (before the first " - " and before any comma), so odd listings can
    still carry extra words; eyeball the output (or the --csv) and prune
    before generating emails. Re-run with -v to see source titles.
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


# Splitting a result title into a name (see _name_chunk).
NAME_SPLIT = re.compile(r"\s+[\-–—|]\s+")       # " - ", " – ", " | "
VIEW_PREFIX = re.compile(r"^view\s+", re.IGNORECASE)
POSSESSIVE = re.compile(r"[’']s\s+profile.*$", re.IGNORECASE)
BRACKETS = re.compile(r"[\(\[][^)\]]*[\)\]]")   # "(He/Him)", "[Open to work]"


class Harvester:
    """Holds the browser and the accumulated names. Does no navigation -
    it only reads whatever page you have on screen and harvests names."""

    def __init__(self, company: str, safe: bool, *,
                 browser: str = "chrome",
                 user_data_dir: str | None = None,
                 firefox_profile: str | None = None,
                 firefox_binary: str | None = None):
        self.company = company.lower()
        self.safe = safe
        self.browser = browser               # "chrome" or "firefox"
        self.user_data_dir = user_data_dir    # persistent profile for the engine
        self.firefox_profile = firefox_profile  # existing profile to reuse (copy)
        self.firefox_binary = firefox_binary
        # name -> title (first title seen)
        self.found: dict[str, str] = {}
        # name -> name again (kept so the CSV writer has a per-name value)
        self.chunks: dict[str, str] = {}
        self._driver = None
        self._tmp_profile: str | None = None  # throwaway Firefox profile copy

    # -- browser -----------------------------------------------------------
    def open_browser(self):
        try:
            import selenium  # noqa: F401
        except ImportError:
            sys.exit("[-] relinked needs selenium: pip3 install selenium")
        if self.browser == "firefox":
            self._open_firefox()
        else:
            self._open_chrome()
        # Open a blank tab and stop there - YOU do every bit of the
        # navigating, including the very first page load, so nothing automated
        # ever hits a search engine.
        try:
            self._driver.get("about:blank")
        except Exception:
            pass
        return self._driver

    def _open_chrome(self) -> None:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
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
        try:
            self._driver = webdriver.Chrome(options=opts)
        except Exception as exc:
            sys.exit(f"[-] couldn't launch Chrome: {exc}\n"
                     "    Install Chrome/Chromium, or use --firefox.")
        try:
            self._driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator, 'webdriver', "
                           "{get: () => undefined});"})
        except Exception:
            pass  # best effort only

    def _open_firefox(self) -> None:
        from selenium import webdriver
        from selenium.webdriver.firefox.options import Options
        opts = Options()
        opts.add_argument("--width=1200")
        opts.add_argument("--height=900")
        if self.firefox_binary:
            opts.binary_location = self.firefox_binary
        # Best-effort: dampen the obvious automation tell. Modern Firefox may
        # ignore this, but it matters little here - you do all the navigating
        # and relinked only reads the page.
        try:
            opts.set_preference("dom.webdriver.enabled", False)
            opts.set_preference("useAutomationExtension", False)
        except Exception:
            pass
        profile = self._firefox_profile_dir()
        if profile:
            opts.add_argument("-profile")
            opts.add_argument(profile)
        try:
            self._driver = webdriver.Firefox(options=opts)
        except Exception as exc:
            sys.exit(f"[-] couldn't launch Firefox: {exc}\n"
                     "    Install Firefox (on Kali it's firefox-esr; pass its\n"
                     "    path with --firefox-binary) and geckodriver. Recent\n"
                     "    selenium fetches geckodriver automatically.")

    def _firefox_profile_dir(self) -> str | None:
        """Decide which profile Firefox uses. With --firefox-profile we COPY
        your existing profile to a throwaway dir and drive the copy, so its
        cookies/logins come along but your real profile is never touched or
        lock-contended. Without it, a persistent relinked profile so a
        consent/robot wall is solved once, not per run."""
        if self.firefox_profile:
            import shutil
            import tempfile
            src = os.path.expanduser(self.firefox_profile)
            if not os.path.isdir(src):
                sys.exit(f"[-] --firefox-profile is not a directory: {src}\n"
                         "    Find your profile path at about:profiles.")
            parent = tempfile.mkdtemp(prefix="relinked-ff-")
            dst = os.path.join(parent, "profile")
            # Skip caches and lock files so the copy is small and not seen as
            # "in use" (Firefox locks a live profile).
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
                "cache2", "startupCache", "*.lock", "lock", ".parentlock",
                "parent.lock"))
            for lk in (".parentlock", "parent.lock", "lock"):
                try:
                    os.remove(os.path.join(dst, lk))
                except OSError:
                    pass
            self._tmp_profile = parent
            print("    [i] driving a copy of your Firefox profile (its cookies "
                  "come along; your real profile is untouched)")
            return dst
        if self.user_data_dir:
            os.makedirs(self.user_data_dir, exist_ok=True)
            return self.user_data_dir
        return None

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
        if self._tmp_profile:  # throwaway Firefox profile copy
            import shutil
            shutil.rmtree(self._tmp_profile, ignore_errors=True)

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
        low = title.lower()
        if not known_linkedin and "linkedin" not in low:
            return
        if self.safe and self.company not in low:
            return
        name = self._name_chunk(title)
        if len(name.split()) < 2:   # need at least a first and a last name
            return
        if name not in self.found:
            self.found[name] = title
            self.chunks[name] = name

    @staticmethod
    def _name_chunk(title: str) -> str:
        """Reduce a result title to a person's full name, the way
        GatherContacts does: cut the '| LinkedIn' suffix, take the part before
        the first ' - ' job-title separator, drop post-nominals after a comma,
        and KEEP THE WHOLE NAME (first, any middle names, last) rather than
        collapsing to first+last.

            'Mary Jane Watson - UX Designer, MBA | LinkedIn' -> 'Mary Jane Watson'
            'John Smith, CPA - Finance at Acme'              -> 'John Smith'
        """
        s = title.split("|", 1)[0]          # drop "| LinkedIn" and anything after
        s = NAME_SPLIT.split(s)[0]          # name is before the first " - "/" – "
        s = s.split(",", 1)[0]              # drop ", CPA" / ", PhD" credentials
        s = VIEW_PREFIX.sub("", s.strip())  # "View <name>'s profile" wrappers
        s = POSSESSIVE.sub("", s)
        s = BRACKETS.sub(" ", s)            # "(He/Him)", "[Open to work]", etc.
        # Keep letters of any script, spaces, apostrophes and hyphens; turn the
        # rest (emoji, digits, symbols) into spaces. Unicode-aware via isalpha,
        # so non-Latin names survive - the old Latin-only filter dropped them.
        s = "".join(c if (c.isalpha() or c in " '-") else " " for c in s)
        return " ".join(s.split())          # collapse whitespace and trim

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
        print(f"[+] {len(names)} rows -> {cfile}  (no header; one name per "
              "row - clean it up by hand)")

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
                    help="also write a header-less CSV of one name per row "
                         "(for manual cleanup); bare --csv writes "
                         "<output basename>.csv")
    ap.add_argument("-s", "--safe", action="store_true",
                    help="only keep titles containing the company name")
    ap.add_argument("--user-data-dir",
                    default=os.path.expanduser("~/.relinked-chrome"),
                    help="persistent Chrome profile so a consent/robot wall "
                         "is remembered across runs "
                         "(default: ~/.relinked-chrome; pass '' to disable)")
    ap.add_argument("--firefox", action="store_true",
                    help="drive Firefox (via geckodriver) instead of Chrome - "
                         "e.g. to use the Firefox already on Kali")
    ap.add_argument("--firefox-profile", metavar="DIR",
                    help="reuse a COPY of this existing Firefox profile so its "
                         "cookies/logins come along (find the path at "
                         "about:profiles; close Firefox first so it's not a "
                         "copy of a live profile). Implies --firefox")
    ap.add_argument("--firefox-binary", metavar="PATH",
                    help="path to the Firefox binary, e.g. /usr/bin/firefox-esr "
                         "(default: auto-detect). Implies --firefox")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="seconds between page checks while watching "
                         "(default 1.0; each check is a tiny DOM read, it "
                         "sends no requests to the engine)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print captured names with their source titles")
    args = ap.parse_args()

    use_firefox = bool(args.firefox or args.firefox_profile or args.firefox_binary)
    print(f"[*] relinked - harvesting names for '{args.target}' "
          f"({'Firefox' if use_firefox else 'Chrome'})")
    if use_firefox:
        # Firefox gets its own persistent profile dir so it never clashes with
        # the Chrome one; --firefox-profile overrides it with a reused copy.
        persistent = os.path.expanduser("~/.relinked-firefox")
    else:
        persistent = (args.user_data_dir or None)
    h = Harvester(args.target, args.safe,
                  browser=("firefox" if use_firefox else "chrome"),
                  user_data_dir=persistent,
                  firefox_profile=args.firefox_profile,
                  firefox_binary=args.firefox_binary)
    try:
        h.open_browser()
        watch_loop(h, args.interval, args.output)
    finally:
        h.close()

    write_results(h, args)


if __name__ == "__main__":
    main()
