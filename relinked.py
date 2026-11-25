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
                 user_data_dir: str | None = None):
        self.timeout = timeout
        self.jitter = jitter
        self.safe = safe
        self.company = company.lower()
        self.browser = browser
        self.user_data_dir = user_data_dir
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
        q = quote_plus(f'site:linkedin.com/in "{self.company}"')
        for page in range(depth):
            first = page * 10 + 1
            url = f"https://www.bing.com/search?q={q}&first={first}"
            html = self._get(url)
            if not html:
                break
            soup = BeautifulSoup(html, "html.parser")
            hits = soup.select("li.b_algo h2 a") or soup.select("h2 a")
            if not hits:
                break
            for a in hits:
                self._handle_title(a.get_text())
            print(f"    [bing] page {page + 1}: {len(self.found)} names so far")
            self._sleep()

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
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_argument(f"--user-agent={random.choice(USER_AGENTS)}")
        # Persist cookies/consent so you clear Google's wall ONCE, not per run.
        if self.user_data_dir:
            opts.add_argument(f"--user-data-dir={self.user_data_dir}")
        self._driver = webdriver.Chrome(options=opts)
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

    def _google_browser(self, depth: int) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys

        driver = self._get_driver()
        query = f'site:linkedin.com/in "{self.company}"'

        # Type the query into Google's search box rather than loading a
        # ?q=... URL directly: a direct URL load sometimes lands on Google's
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

        # Page 1 is whatever the box submission landed on; later pages reuse
        # that exact results URL (operators intact) with an added &start=.
        base_url = driver.current_url
        for page in range(depth):
            if page > 0:
                page_url = self._with_start(base_url, page * 10)
                driver.get(page_url)
                self._sleep()
                if not self._clear_wall(driver, reload_url=page_url):
                    return
            soup = BeautifulSoup(driver.page_source, "html.parser")
            n = self._harvest_linkedin(soup)
            if n == 0 and not soup.select("h3"):
                break
            print(f"    [google] page {page + 1}: {len(self.found)} names so far")

    @staticmethod
    def _with_start(url: str, start: int) -> str:
        """Return `url` with its start= pagination param set to `start`."""
        from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
        parts = urlparse(url)
        qs = parse_qs(parts.query, keep_blank_values=True)
        qs["start"] = [str(start)]
        return urlunparse(parts._replace(query=urlencode(qs, doseq=True)))

    # Harvest names from result titles. We can't filter by the result URL:
    # Google now cloaks every hit behind an opaque /goto?url=<token> link with
    # no readable destination, so there's no linkedin.com/in/ string to match.
    # Instead we trust the site:linkedin.com/in operator in the query to
    # constrain results, and pull every organic <h3> title - Google reserves
    # <h3> for organic result headings, so these are the profile titles.
    # known_linkedin=True because profile titles often omit the word
    # "LinkedIn" (e.g. "Jane Doe - Director at Acme Corp").
    def _harvest_linkedin(self, soup: BeautifulSoup) -> int:
        before = len(self.found)
        for h in soup.select("h3"):
            self._handle_title(h.get_text(), known_linkedin=True)
        return len(self.found) - before

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
    ap.add_argument("--engines", default="duckduckgo,bing",
                    help="comma list: duckduckgo,bing,google (default ddg,bing)")
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
    ap.add_argument("--user-data-dir",
                    default=os.path.expanduser("~/.relinked-chrome"),
                    help="persistent Chrome profile for --browser so Google's "
                         "consent/robot wall is remembered across runs "
                         "(default: ~/.relinked-chrome; pass '' to disable)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print recovered names with their source titles")
    args = ap.parse_args()

    print(f"[*] relinked - enumerating '{args.target}'")
    sc = Scraper(args.timeout, args.jitter, args.proxy, args.safe, args.target,
                 browser=args.browser,
                 user_data_dir=(args.user_data_dir or None))

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
