# Link Checker

Checks for broken links and non-HTTPS URLs.

A terminal crawler that discovers linked pages on one site, checks each discovered HTTP(S) URL once, and reports 403, 404, and non-HTTPS findings to the terminal and a CSV file.

## Install

Requires Python 3.10 or later.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```sh
source .venv/bin/activate
python link_checker.py https://example.com --workers 8
```

If the URL is omitted, the program asks for it. During discovery and checking, an animated spinner keeps elapsed time visible before the shortened current URL and phase counts. It also shows a rolling current URL/s rate and a whole-run average. When complete, a findings table lists issue, status, internal/external type, URL, and number of source pages, followed by start/end time, duration, and totals.

The crawl follows links on the starting hostname, treating `www.example.com` and `example.com` as the same host. Other subdomains are external: their HTTP(S) links are checked, but their pages are not crawled. It discovers hyperlinks in `<a>` and `<area>` elements; it does not execute JavaScript or check fragment targets.

The default page cap is 10,000 rather than 500. Increase or lower it with `--max-pages`. The crawler honors `robots.txt`, uses the greater of the configured delay and any crawl delay/request rate in `robots.txt`, and blocks requests when `robots.txt` is unreachable or returns a server error.

## Findings and CSV

- `404 Not Found` is based on the final status after redirects. Other HTTP status codes are not reported as broken links.
- `403 Forbidden` is reported separately as access-restricted. It may mean a linked resource requires permission or blocks the checker; it does not necessarily mean the link is dead.
- A URL is flagged as `HTTP (not HTTPS)` if the link itself uses `http://` or if an HTTPS link redirects down to HTTP. An original HTTP URL remains flagged even when it redirects to HTTPS. A URL can also have both issues.
- `mailto:`, `tel:`, fragment-only, and other non-HTTP(S) references are ignored.
- Network errors are counted in the summary but are not labeled as 404s.
- The CSV contains `issue`, `status_code`, `link_type`, `url`, `found_on`, and `final_url`. `link_type` is `internal` or `external`. The CSV has one row per finding per source page; each unique destination is checked only once.

The final summary breaks 403, 404, and non-HTTPS counts down into internal and external URLs, making it easier to prioritize fixes on your own site versus links pointing elsewhere. The CSV's `link_type` column carries the same classification for each source-page row.

## Performance

Up to 8 page requests are fetched concurrently during discovery, and up to 8 destination URLs are checked concurrently afterward. Change the limit for both phases with `--workers`. Workers can overlap slow responses, but request starts to each hostname remain spaced by the greater of `--delay` and that host's `robots.txt` crawl delay/request rate. The startup output shows the seed host's effective minimum spacing; other hosts may differ. For example, a 0.25-second per-host delay allows at most 4 starts per second, regardless of worker count. If you control the site and its `robots.txt` permits it, `--delay 0` removes the tool's 0.25-second default; it does not override stricter robots rules. The current rate is a rolling five-second rate. The average counts completed discovery page requests plus checked links; robots.txt requests are excluded.

The report defaults to `link_check_report_<domain>_<YYYY-MM-DD>_<HHMM>.csv`, using 24-hour local time. If that filename already exists, a suffix is added instead of overwriting it. Choose an exact path with `--csv`. A CSV is created only when at least one issue is found; a clean run prints a message and leaves existing reports untouched.

```sh
python link_checker.py https://example.com \
  --csv reports/example-links.csv \
  --max-pages 20000 \
  --timeout 15 \
  --delay 0.5 \
  --workers 16
```

`--timeout` defaults to 10 seconds, `--delay` to 0.25 seconds, and `--workers` to 8. Page responses are limited to 2 MiB for link discovery. Total runtime is shown in seconds, minutes, or hours.

## Canceling a Run

Press `Ctrl+C` to stop. The program exits cleanly with code 130 and cancels queued page fetches and link checks. A request already in progress may take up to `--timeout` to return. Discovered URLs and check results are kept in memory only; page contents and crawl logs are not saved. A canceled run does not export partial findings. CSV output is written to a temporary file and replaces the report only after the write completes, so an existing report stays intact and interrupted writes do not leave temporary files.

## Tests

```sh
python -m unittest discover -s tests -v
```

Sitemap import is a useful next addition for discovering orphaned pages that are not linked from crawled pages. Sites that build navigation entirely in JavaScript may also need browser-based rendering.
