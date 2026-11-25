#!/usr/bin/env python3
"""relinked.py - LinkedIn employee-name enumeration via search-engine scraping.

A spiritual successor to CrossLinked (now abandoned). Collects public
employee names for a target organisation by scraping search-engine results
for indexed LinkedIn profiles - no API keys, no credentials, no direct
requests to LinkedIn.

Intended for authorised security testing / OSINT only.

    python3 relinked.py -t "Target Company"
    python3 relinked.py -t "Target Company" -f '{first}.{last}@target.com' -o emails.txt
    python3 relinked.py -t "Target Company" --engines duckduckgo,bing --depth 5 -j 2

    # Google through a visible browser so you can clear any CAPTCHA yourself:
    python3 relinked.py -t "Target Company" --engines google --browser

Dependencies: requests, beautifulsoup4   (and selenium for --browser)
    pip3 install requests beautifulsoup4 selenium

------------------------------------------------------------------------
TROUBLESHOOTING - when it fails

"No names recovered"
    - Check the company name matches LinkedIn exactly, in quotes:
      -t "Acme Corp Ltd", not -t acme.com. The domain won't work.
    - Raise --depth (e.g. --depth 10) to pull more result pages.
    - Try engines one at a time to see which is actually returning
      results: --engines duckduckgo  then  --engines bing.
    - Drop --safe; it discards any title without the company name and
      can be too aggressive for orgs whose name varies in listings.

"rate-limited / CAPTCHA" or results suddenly dry up
    - You're being throttled. Raise jitter: -j 5 (or higher).
    - Route through a proxy / VPN: --proxy http://127.0.0.1:8080
      then rerun. Rotate egress IPs between runs if you can.
    - Give it a rest - search engines lift soft blocks after a while.
    - For Google specifically, use --browser and solve the check
      by hand (see below).

--browser issues
    - "selenium not installed":  pip3 install selenium
    - "chromedriver" errors / version mismatch: install a chromedriver
      matching your installed Chrome (chromedriver --version must line
      up with chrome://version). Recent Chrome ships Selenium Manager,
      so simply upgrading selenium often fixes driver resolution.
    - No window appears: you're probably on a headless/SSH host. Run it
      somewhere with a display, or use X-forwarding.
    - "no interactive console; cannot solve check": run it directly in
      a terminal, not piped or from a scheduler - it needs stdin to
      wait for your Enter.

Names look wrong (titles, extra words, mojibake)
    - Some listings bury the name behind job titles or "View X's
      profile". The parser keeps the first and last token of the name
      chunk; eyeball names.txt and prune before generating emails.
    - Re-run with -v to see each name next to the title it came from.

Still stuck
    - Metadata in public documents often reveals the real username
      convention - cross-check with a tool like PyMeta before trusting
      a guessed email format.
------------------------------------------------------------------------
"""
from __future__ import annotations

import argparse
import os
import random
import re
import sys
import time
from html import unescape
from urllib.parse import quote_plus

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("[-] Missing deps. Run: pip3 install requests beautifulsoup4")


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
]

# Trailing site markers to strip from result titles, various locales/dashes.
TITLE_TAIL = re.compile(
    r"\s*[\|\-–—]\s*LinkedIn.*$", re.IGNORECASE
)
NAME_SPLIT = re.compile(r"\s+[\-–—\|]\s+")      # " - ", " – ", " | "
VIEW_PREFIX = re.compile(r"^view\s+", re.IGNORECASE)
POSSESSIVE = re.compile(r"[’']s\s+profile.*$", re.IGNORECASE)


class Scraper:
    def __init__(self, timeout: int, jitter: float, proxy: str | None,
                 safe: bool, company: str, browser: bool = False,
                 user_data_dir: str | None = None,
                 regions: list[str] | None = None):
        self.timeout = timeout
        self.jitter = jitter
        self.safe = safe
        self.company = company.lower()
        self.browser = browser
        self.user_data_dir = user_data_dir
        self.regions = regions or []
        self.session = requests.Session()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        # name -> title (first title seen)
        self.found: dict[str, str] = {}
        # name -> full uncollapsed name chunk (for manual CSV cleanup)
        self.chunks: dict[str, str] = {}
        self._driver = None  # lazily created selenium driver

    def _get(self, url: str) -> str | None:
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
        }
        try:
            r = self.session.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            print(f"    [!] request error: {exc}", file=sys.stderr)
            return None
        if r.status_code == 429 or "captcha" in r.text.lower():
            print("    [!] rate-limited / CAPTCHA - back off or add a proxy",
                  file=sys.stderr)
            return None
        if r.status_code != 200:
            print(f"    [!] HTTP {r.status_code}", file=sys.stderr)
            return None
        return r.text

    def _sleep(self) -> None:
        if self.jitter:
            time.sleep(random.uniform(0, self.jitter))

    # -- title -> name -----------------------------------------------------
    def _handle_title(self, title: str, known_linkedin: bool = False) -> None:
        title = unescape(title).strip()
        # When the caller already matched a linkedin.com/in URL, the title
        # itself need not contain the word "linkedin" (profile titles are
        # often just "Name - Role at Company").
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
            # Keep the full, uncollapsed name chunk (middle names, post-nominals
            # and all) for manual cleanup in the CSV.
            self.chunks[name] = name_part

    @staticmethod
    def _name_chunk(title: str) -> str:
        """The name portion of a result title, before the job-title separator,
        with site markers and 'View ... profile' wrappers stripped. Unlike the
        stored name it is NOT collapsed to first+last, so e.g.
        'Snezana Bakalovski MAICD - Chief People Officer' -> 'Snezana Bakalovski MAICD'."""
        clean = TITLE_TAIL.sub("", title).strip()
        clean = VIEW_PREFIX.sub("", clean)
        clean = POSSESSIVE.sub("", clean)
        name_part = NAME_SPLIT.split(clean)[0].strip()
        # keep letters, spaces, hyphens, apostrophes only
        return re.sub(r"[^A-Za-zÀ-ɏ '\-]", "", name_part).strip()

    # -- engines -----------------------------------------------------------
    def duckduckgo(self, depth: int) -> None:
        q = quote_plus(f'site:linkedin.com/in "{self.company}"')
        for page in range(depth):
            s = page * 30  # ddg html paginates by ~30
            url = f"https://html.duckduckgo.com/html/?q={q}&s={s}"
            html = self._get(url)
            if not html:
                break
            soup = BeautifulSoup(html, "html.parser")
            hits = soup.select("a.result__a")
            if not hits:
                break
            for a in hits:
                self._handle_title(a.get_text())
            print(f"    [ddg] page {page + 1}: {len(self.found)} names so far")
            self._sleep()

    def bing(self, depth: int) -> None:
        if self.browser:
            return self._bing_browser(depth)
        # Plain HTTP: a cold, cookieless session gets bot-flagged - Bing serves
        # a frozen page 1 (its Next link stays at first=11) or unrelated junk.
        # Priming the homepage helps page 1, but real pagination needs the
        # cookied browser session (--browser / _bing_browser).
        self._get("https://www.bing.com/")
        self._sleep()
        q = quote_plus(f'site:linkedin.com/in "{self.company}"')
        for page in range(depth):
            first = page * 10 + 1
            url = f"https://www.bing.com/search?q={q}&first={first}"
            html = self._get(url)
            if not html:
                break
            before = len(self.found)
            self._harvest_bing(BeautifulSoup(html, "html.parser"))
            print(f"    [bing] page {page + 1}: {len(self.found)} names so far")
            if len(self.found) == before:
                break  # HTTP pagination doesn't advance; stop on no-new
            self._sleep()

    def _bing_browser(self, depth: int) -> None:
        # Drive Bing through the persistent, cookied Chrome profile and click
        # its real Next link, like a person. A cookieless automated session
        # gets bot-flagged (frozen page 1 / junk); an established profile
        # paginates normally.
        from selenium.webdriver.common.by import By
        driver = self._get_driver()
        q = quote_plus(f'site:linkedin.com/in "{self.company}"')
        driver.get(f"https://www.bing.com/search?q={q}")
        self._sleep()
        try:
            self._bing_pages(driver, By, depth)
        except Exception as exc:
            import traceback
            print(f"    [!] Bing browser error: {exc}", file=sys.stderr)
            traceback.print_exc()

    def _bing_pages(self, driver, By, depth: int) -> None:
        for page in range(depth):
            soup = BeautifulSoup(driver.page_source, "html.parser")
            before = len(self.found)
            self._harvest_bing(soup)
            print(f"    [bing] page {page + 1}: {len(self.found)} names so far "
                  f"(url first={self._first_param(driver.current_url)})")
            if page > 0 and len(self.found) == before:
                break  # a real page that adds nothing new -> end of results
            if page == depth - 1:
                break
            # Scroll to the bottom first - Bing lazy-renders the pager, and a
            # Next link that isn't in view clicks unreliably.
            driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight);")
            self._sleep()
            try:
                nxt = driver.find_element(By.CSS_SELECTOR, "a.sb_pagN")
            except Exception:
                break  # no Next link -> last page
            next_href = nxt.get_attribute("href")
            print(f"    [bing] -> Next link first={self._first_param(next_href or '')}",
                  file=sys.stderr)
            # Signature of the current page so we can wait for it to actually
            # change after navigating (a short sleep can read the stale page).
            sig = self._bing_sig(driver)
            clicked = False
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", nxt)
                self._sleep()
                nxt.click()
                clicked = True
            except Exception as exc:
                print(f"    [!] click failed ({exc}); trying link directly.",
                      file=sys.stderr)
            advanced = self._wait_change(driver, sig) if clicked else False
            if not advanced and next_href:
                # Click didn't advance - navigate the Next link's URL directly.
                print("    [bing] click didn't advance; opening Next URL direct",
                      file=sys.stderr)
                driver.get(next_href)
                advanced = self._wait_change(driver, sig)
            if not advanced:
                print("    [i] Bing served the same page again (pagination "
                      "capped for this session) - stopping.", file=sys.stderr)
                break

    @staticmethod
    def _first_param(url: str) -> str:
        from urllib.parse import urlparse, parse_qs
        return (parse_qs(urlparse(url).query).get("first") or ["1"])[0]

    @staticmethod
    def _bing_sig(driver) -> str:
        """First result title + current first= param - changes when the page
        genuinely advances."""
        soup = BeautifulSoup(driver.page_source, "html.parser")
        h2 = soup.select_one("li.b_algo h2")
        return (Scraper._first_param(driver.current_url) + "|" +
                (h2.get_text()[:60] if h2 else ""))

    def _wait_change(self, driver, old_sig: str, timeout: float = 10.0) -> bool:
        """Poll until the results page differs from old_sig. False = unchanged
        within timeout (Bing re-served the same page)."""
        waited = 0.0
        while waited < timeout:
            time.sleep(0.5)
            waited += 0.5
            if self._bing_sig(driver) != old_sig:
                return True
        return False

    def _harvest_bing(self, soup: BeautifulSoup) -> int:
        """Keep only linkedin.com /in profiles (identified by each result's
        visible <cite> domain, since Bing cloaks the href) and harvest the
        <h2> title. Returns names added."""
        before = len(self.found)
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
        return len(self.found) - before

    def google(self, depth: int) -> None:
        if self.browser:
            return self._google_browser(depth)
        q = quote_plus(f'site:linkedin.com/in "{self.company}"')
        for page in range(depth):
            start = page * 10
            url = f"https://www.google.com/search?q={q}&start={start}"
            html = self._get(url)
            if not html:
                break
            soup = BeautifulSoup(html, "html.parser")
            hits = soup.select("h3")
            if not hits:
                break
            for h in hits:
                self._handle_title(h.get_text())
            print(f"    [google] page {page + 1}: {len(self.found)} names so far")
            self._sleep()
        print("    [i] plain Google scraping is heavily rate-limited; "
              "add --browser to solve checks by hand.", file=sys.stderr)

    # -- selenium-backed Google: you clear any CAPTCHA/consent yourself ----
    def _get_driver(self):
        if self._driver is not None:
            return self._driver
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
        except ImportError:
            sys.exit("[-] --browser needs selenium: pip3 install selenium "
                     "(and a matching chromedriver on PATH)")
        opts = Options()
        # Visible on purpose - a headless window gives you nothing to click.
        opts.add_argument("--window-size=1200,900")
        # Look like an ordinary browser, not automation. Search engines (Bing
        # especially) detect Selenium via navigator.webdriver and the
        # AutomationControlled flag and freeze on page 1 / serve junk; these
        # flags plus the CDP patch below remove those tells.
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_argument(f"--user-agent={random.choice(USER_AGENTS)}")
        # Persist cookies/consent so you clear Google's wall ONCE, not per run.
        if self.user_data_dir:
            opts.add_argument(f"--user-data-dir={self.user_data_dir}")
        self._driver = webdriver.Chrome(options=opts)
        try:
            self._driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator, 'webdriver', "
                           "{get: () => undefined});"})
        except Exception:
            pass  # non-Chrome driver / unsupported - best effort only
        return self._driver

    @staticmethod
    def _needs_human(page_source: str, current_url: str) -> bool:
        # Detect the wall by URL - the reliable signal. Google's block page
        # lives under /sorry/, and the EU consent interstitial redirects to
        # consent.google.com. Scanning the page HTML for "recaptcha" /
        # "consent.google" gives false positives: those strings are embedded
        # in normal results pages too, so it looked unsolved after you solved.
        url = current_url.lower()
        if "/sorry/" in url or "consent.google" in url:
            return True
        # Interstitial occasionally served inline on the search domain.
        lowered = page_source.lower()
        inline = ("before you continue to google",
                  "our systems have detected unusual traffic")
        return any(m in lowered for m in inline)

    def _clear_wall(self, driver, reload_url: str | None = None) -> bool:
        """Block until a human clears any consent/robot wall. False = give up."""
        waits = 0
        while self._needs_human(driver.page_source, driver.current_url):
            waits += 1
            if waits > 5:
                print("    [!] still blocked after several tries - "
                      "stopping Google.", file=sys.stderr)
                return False
            try:
                input("    [>] Google wants a human - solve the check in "
                      "the browser window, then press Enter here... ")
            except EOFError:
                print("    [!] no interactive console; cannot solve check.",
                      file=sys.stderr)
                return False
            # Reload so the cookies you just earned take effect.
            driver.get(reload_url or driver.current_url)
            self._sleep()
        return True

    def _google_queries(self) -> list[str]:
        """Query variants to run. A single site:linkedin.com/in query exhausts
        at ~one page because Google only indexes a slice of profiles for that
        exact phrasing; varying the phrasing and the regional LinkedIn
        subdomain surfaces different people, so we run several and aggregate."""
        c = self.company
        qs = [f'site:linkedin.com/in "{c}"',
              f'site:linkedin.com/in "at {c}"']
        for r in self.regions:
            qs.append(f'site:{r}.linkedin.com/in "{c}"')
        return qs

    def _google_browser(self, depth: int) -> None:
        driver = self._get_driver()
        for query in self._google_queries():
            print(f"    [google] query: {query}")
            self._google_one_query(driver, query, depth)

    def _google_one_query(self, driver, query: str, depth: int) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys

        # Type the query into Google's search box rather than loading a
        # ?q=... URL directly: a direct URL load lands on Google's
        # operator-relaxed results path (site: effectively ignored), whereas
        # submitting through the box is the normal flow and honours operators.
        driver.get("https://www.google.com/")
        self._sleep()
        if not self._clear_wall(driver):
            return
        try:
            box = driver.find_element(
                By.CSS_SELECTOR, "textarea[name=q], input[name=q]")
            box.clear()
            box.send_keys(query)
            box.send_keys(Keys.RETURN)
            self._sleep()
        except Exception as exc:
            print(f"    [!] couldn't use the search box ({exc}); "
                  "falling back to URL query.", file=sys.stderr)
            driver.get("https://www.google.com/search?q=" + quote_plus(query))
            self._sleep()
        if not self._clear_wall(driver):
            return

        # Walk pages by clicking Google's own "Next" link, the way a person
        # would. Google still tends to quietly drop the site: operator once it
        # runs low on exact matches, so we don't rely on it: _harvest_linkedin
        # enforces site: ourselves (only results labelled LinkedIn are kept),
        # and once a page has no LinkedIn results left we move to the next
        # query - the indexed profiles for this one are exhausted.
        for page in range(depth):
            soup = BeautifulSoup(driver.page_source, "html.parser")
            kept = self._harvest_linkedin(soup)
            if kept == 0:
                if not (page == 0 and not soup.select("h3")):
                    print("    [i] no more LinkedIn results for this query "
                          "(exhausted / relaxed).")
                break
            print(f"    [google]   page {page + 1}: {len(self.found)} names so far")
            if page == depth - 1:
                break
            if not self._click_next(driver):
                break  # no further pages

    def _click_next(self, driver) -> bool:
        """Click Google's Next-page link like a human. False = no next page."""
        from selenium.webdriver.common.by import By
        nxt = None
        for by, sel in ((By.ID, "pnnext"),
                        (By.CSS_SELECTOR, 'a[aria-label="Next page"]'),
                        (By.CSS_SELECTOR, "#pnnext, a#pnnext")):
            try:
                nxt = driver.find_element(by, sel)
                break
            except Exception:
                continue
        if nxt is None:
            return False
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", nxt)
            self._sleep()
            nxt.click()
            self._sleep()
        except Exception as exc:
            print(f"    [!] couldn't click Next ({exc}); stopping pagination.",
                  file=sys.stderr)
            return False
        # A fresh page may re-trigger the wall.
        return self._clear_wall(driver)

    # Harvest names from result titles. We can't filter by the result URL:
    # Google now cloaks every hit behind an opaque /goto?url=<token> link with
    # no readable destination, so there's no linkedin.com/in/ string to match.
    # Google also drops the site: operator once it runs low on exact matches,
    # so we can't trust the query alone either. Instead we enforce site:
    # ourselves: each organic <h3> result carries a visible source label
    # ("LinkedIn . Name . N followers") in its block, so we keep a title only
    # when its own result block is labelled LinkedIn and skip relaxed filler.
    # Returns the number of LinkedIn results seen on the page.
    def _harvest_linkedin(self, soup: BeautifulSoup) -> int:
        kept = skipped = 0
        for h3 in soup.select("h3"):
            if self._result_is_linkedin(h3):
                self._handle_title(h3.get_text(), known_linkedin=True)
                kept += 1
            else:
                skipped += 1
        if skipped:
            print(f"    [i] skipped {skipped} non-LinkedIn result(s) on this "
                  "page (site: relaxed)", file=sys.stderr)
        return kept

    @staticmethod
    def _result_is_linkedin(h3) -> bool:
        """True if this result's own block is labelled LinkedIn. Climb
        ancestors while the container still wraps only this one <h3>, looking
        for a 'linkedin' source label. Stop as soon as a container holds
        another <h3> - that's a multi-result block, so a label there would
        belong to a neighbouring result, not this one."""
        node = h3
        for _ in range(8):
            node = node.parent
            if node is None:
                break
            if len(node.select("h3")) > 1:
                break  # climbed into a block containing other results
            if "linkedin" in node.get_text(" ", strip=True).lower():
                return True
        return False

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass


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


def main() -> None:
    ap = argparse.ArgumentParser(
        description="LinkedIn name enumeration via search-engine scraping.")
    ap.add_argument("-t", "--target", required=True,
                    help="company name as it appears on LinkedIn")
    ap.add_argument("-f", "--format",
                    help="email/username format, e.g. '{first}.{last}@corp.com'")
    ap.add_argument("-o", "--output", default="names.txt",
                    help="output file (default: names.txt)")
    ap.add_argument("--csv", nargs="?", const="<auto>", default=None,
                    metavar="PATH",
                    help="also write a header-less CSV of one full name per row "
                         "(uncollapsed, for manual cleanup); bare --csv writes "
                         "<output basename>.csv")
    ap.add_argument("--engines", default="google,bing",
                    help="comma list: duckduckgo,bing,google (default google,bing; "
                         "ddg is currently anti-bot-blocked)")
    ap.add_argument("--depth", type=int, default=5,
                    help="result pages per engine (default 5)")
    ap.add_argument("-j", "--jitter", type=float, default=1.5,
                    help="max random seconds between requests (default 1.5)")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--proxy", help="proxy, e.g. http://127.0.0.1:8080")
    ap.add_argument("-s", "--safe", action="store_true",
                    help="only keep titles containing the company name")
    ap.add_argument("--browser", action="store_true",
                    help="route Google through a visible Chrome so you can "
                         "solve CAPTCHAs/consent walls by hand (needs selenium)")
    ap.add_argument("--regions", default="",
                    help="comma list of regional LinkedIn subdomains to also "
                         "query, e.g. au,uk,nz,sg - each surfaces different "
                         "profiles (Google-browser engine only)")
    ap.add_argument("--user-data-dir",
                    default=os.path.expanduser("~/.relinked-chrome"),
                    help="persistent Chrome profile for --browser so Google's "
                         "consent/robot wall is remembered across runs "
                         "(default: ~/.relinked-chrome; pass '' to disable)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print recovered names with their source titles")
    args = ap.parse_args()

    print(f"[*] relinked - enumerating '{args.target}'")
    regions = [r.strip().lower() for r in args.regions.split(",") if r.strip()]
    sc = Scraper(args.timeout, args.jitter, args.proxy, args.safe, args.target,
                 browser=args.browser,
                 user_data_dir=(args.user_data_dir or None),
                 regions=regions)

    engine_map = {"duckduckgo": sc.duckduckgo, "bing": sc.bing, "google": sc.google}
    try:
        for name in [e.strip().lower() for e in args.engines.split(",")]:
            fn = engine_map.get(name)
            if not fn:
                print(f"[!] unknown engine: {name}", file=sys.stderr)
                continue
            print(f"[*] scraping {name} ...")
            fn(args.depth)
    finally:
        sc.close()

    names = sorted(sc.found)
    if not names:
        sys.exit("[-] No names recovered. Try different engines, more depth, "
                 "a proxy, or check the company name spelling.")

    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write("\n".join(names) + "\n")
    print(f"\n[+] {len(names)} unique names -> {args.output}")

    if args.verbose:
        for n in names:
            print(f"    {n}  ::  {sc.found[n]}")

    if args.csv is not None:
        import csv
        cfile = (args.csv if args.csv != "<auto>"
                 else args.output.rsplit(".", 1)[0] + ".csv")
        # No header, one full name chunk per row (middle names / post-nominals
        # intact) so you can hand-fix single-letter surnames, strip
        # professional titles, etc. before generating emails.
        with open(cfile, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            for n in names:
                w.writerow([sc.chunks.get(n, n)])
        print(f"[+] {len(names)} rows -> {cfile}  (no header; one name per row, "
              "full chunk - clean it up by hand)")

    if args.format:
        emails, seen = [], set()
        for n in names:
            toks = n.split()
            addr = fmt_name(toks[0], toks[-1], args.format)
            if addr and addr not in seen:
                seen.add(addr)
                emails.append(addr)
        efile = (args.output.rsplit(".", 1)[0] + "_emails.txt")
        with open(efile, "w", encoding="utf-8") as fh:
            fh.write("\n".join(emails) + "\n")
        print(f"[+] {len(emails)} addresses -> {efile}")


if __name__ == "__main__":
    main()
