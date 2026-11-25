# relinked

Manual-drive LinkedIn employee-name harvester for **authorised security
testing / OSINT only**.

A spiritual successor to the abandoned CrossLinked, rebuilt around one idea:
**the tool does no navigating.** It opens a real, visible browser and hands it
to you. You open Google or Bing, type your own searches, page through the
results and solve any consent/robot wall by hand. Because every query, click
and scroll is a genuine human action, there's no automated-navigation
fingerprint for the engines to flag. relinked just *watches* — a few times a
second it reads the page already loaded in the browser (a local DOM read, no
request is sent to the engine) and, whenever the page changes, harvests the
public LinkedIn names off it automatically.

No API keys, no credentials, no direct requests to LinkedIn.

There are two implementations with identical harvesting behaviour; pick by how
you want to run it.

| | `relinked.py` (Python) | `go/` (Go, single binary) |
|---|---|---|
| Install | `pip3 install selenium beautifulsoup4` (+ `lxml` optional, faster) and a chromedriver | none — one self-contained executable |
| Drives Chrome via | Selenium / chromedriver | DevTools protocol (chromedp) directly |
| Best when | you already have a Python/Selenium setup, or want to tweak the script | you want to hand someone a binary with nothing to install |

Both need a Chrome/Chromium on the machine and a display (the window is visible
on purpose — you drive it).

### Running on Kali

The Go binary is statically linked, so it drops onto Kali and runs with nothing
else installed — but it still drives a browser, so note:

- **Install Chromium.** Kali ships Firefox ESR by default, and this tool can
  only drive Chrome/Chromium: `sudo apt install chromium`. (Or point
  `--chrome` at any Chrome/Chromium binary you have.)
- **Use a desktop session.** The window is visible and you drive it, so a GUI
  Kali is required — a headless/SSH/WSL Kali has no display and won't work.
- **Root is handled.** Chrome won't run as root without `--no-sandbox`; relinked
  adds it automatically when it detects it's running as root (common on Kali).
  Pass `--no-sandbox` yourself to force it otherwise.

## Go binary

**Download:** prebuilt binaries for Linux, Windows and macOS (amd64/arm64) are
attached to each [GitHub Release](../../releases) — grab the one for your
platform, `chmod +x`, run. Nothing else to install but a Chromium and a
display. Releases are produced by CI (`.github/workflows/release.yml`) when a
version tag is pushed, e.g. `git tag v0.1.0 && git push origin v0.1.0`.

**Or build it yourself:**

```sh
cd go
go build -o relinked .          # or: CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o relinked .
./relinked -t "Target Company"
```

Cross-compile a binary for another OS (no C dependencies, so this just works):

```sh
GOOS=windows GOARCH=amd64 go build -o relinked.exe .
GOOS=darwin  GOARCH=arm64 go build -o relinked-mac .
```

Common options (short and `--long` forms both work):

```sh
./relinked -t "Target Company" -f '{first}.{last}@target.com'   # also write emails
./relinked -t "Target Company" --csv -o names.txt               # also write a CSV for cleanup
./relinked -t "Target Company" -s                               # safe: keep only titles with the company name
./relinked -t "Target Company" --chrome /path/to/chrome         # pick a specific Chrome binary
./relinked -h
```

## Python script

```sh
pip3 install selenium beautifulsoup4 lxml
python3 relinked.py -t "Target Company"
python3 relinked.py -t "Target Company" -f '{first}.{last}@target.com' --csv
```

## How a session goes (either tool)

1. Run it; a Chrome window opens on a blank tab (its own persistent profile, so
   cookies/consent survive between runs).
2. In that window, search Google or Bing however you like, e.g.
   `site:linkedin.com/in "Target Company"`. Vary the phrasing, try regional
   subdomains (`au.linkedin.com`, …), page through results — all by hand.
3. Names are captured automatically as pages load, printed as a running total,
   and written to the output file live (so you can `tail -f names.txt`, and
   nothing is lost if the browser or terminal dies).
4. Press **Ctrl+C** (or close the browser) to finish. The CSV / email list, if
   requested, are written then.

## Output

- `names.txt` — one `First Last` per line, de-duplicated and sorted.
- `--csv` → a header-less CSV with the full, uncollapsed name chunk per row
  (middle names / post-nominals intact) for hand-cleanup before generating
  emails.
- `-f/--format` → `<output>_emails.txt`, applying a `{first}`/`{last}`/`{f}`/`{l}`
  template.

Names come from result titles, so eyeball them before use — some listings bury
the name behind job titles or "View X's profile"; the parser keeps the first
and last token of the name chunk.

## Developing

The Go harvesting logic is browser-free and unit-tested:

```sh
cd go
go test ./...                               # fast, no browser
go test -tags smoke -run TestBrowserPipeline # end-to-end against a headless Chrome
```
