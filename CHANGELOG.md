# Changelog

Notable changes to Link Checker are documented here.

## [0.1.0] - 2026-11-12

### Added

- Crawl same-host pages and check discovered internal and external HTTP(S) links.
- Report 403, 404, non-HTTPS links, and source pages in terminal output and CSV.
- Respect robots.txt, display crawl/check progress and throughput, and check links concurrently.
- Add `--version` to display the application version.