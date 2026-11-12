from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from rich.console import Console
from rich.markup import escape
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Column, Table
from rich.text import Text


USER_AGENT = "LinkChecker/1.0"
DEFAULT_MAX_PAGES = 10_000
DEFAULT_TIMEOUT = 10.0
DEFAULT_DELAY = 0.25
DEFAULT_WORKERS = 8
ROBOTS_LIMIT = 512 * 1024
HTML_LIMIT = 2 * 1024 * 1024
REPORT_COLUMNS = ("issue", "status_code", "link_type", "url", "found_on", "final_url")
REQUEST_HEADERS = {"User-Agent": USER_AGENT}


@dataclass(frozen=True)
class CheckResult:
    status_code: int | None
    final_url: str | None
    error: str | None = None
    skipped_reason: str | None = None


@dataclass(frozen=True)
class Finding:
    issue: str
    status_code: int | None
    link_type: str
    url: str
    found_on: str
    final_url: str | None


@dataclass
class CrawlResult:
    pages: list[str] = field(default_factory=list)
    link_sources: dict[str, set[str]] = field(default_factory=dict)
    pages_visited: int = 0
    robots_skipped: int = 0
    request_errors: int = 0
    limit_reached: bool = False


def normalize_url(raw_url: str, base_url: str | None = None) -> str | None:
    candidate = raw_url.strip()
    if not candidate or candidate.startswith("#"):
        return None

    try:
        absolute_url = urljoin(base_url, candidate) if base_url else candidate
        parts = urlsplit(absolute_url)
        scheme = parts.scheme.lower()
        host = parts.hostname
        if (
            scheme not in {"http", "https"}
            or not host
            or parts.username is not None
            or parts.password is not None
        ):
            return None
        port = parts.port
        host = host.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None

    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]"
    else:
        netloc = host

    default_port = 80 if scheme == "http" else 443
    if port is not None and port != default_port:
        netloc = f"{netloc}:{port}"

    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def normalize_seed_url(raw_url: str) -> str:
    candidate = raw_url.strip()
    if not candidate:
        raise ValueError("Enter a website URL.")
    if candidate.startswith("//"):
        candidate = f"https:{candidate}"
    elif not urlsplit(candidate).scheme:
        candidate = f"https://{candidate}"

    normalized = normalize_url(candidate)
    if normalized is None:
        raise ValueError("Enter a valid HTTP or HTTPS URL without embedded credentials.")
    return normalized


def _host_alias(host: str) -> str:
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def is_internal_url(url: str, site_host: str) -> bool:
    try:
        host = urlsplit(url).hostname
        seed = urlsplit(site_host if "://" in site_host else f"//{site_host}").hostname
        if seed is None:
            seed = site_host.strip("[]")
    except ValueError:
        return False
    return bool(host and seed and _host_alias(host) == _host_alias(seed))


def extract_links(html: str, page_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    base_url = page_url
    base_tag = soup.find("base", href=True)
    if base_tag:
        base_url = normalize_url(str(base_tag["href"]), page_url) or page_url

    links: list[str] = []
    for tag in soup.find_all(["a", "area"], href=True):
        href = str(tag.get("href", "")).strip()
        if not href or href.startswith("#"):
            continue
        normalized = normalize_url(href, base_url)
        if normalized:
            links.append(normalized)
    return links


def _read_limited(response: requests.Response, limit: int) -> bytes:
    body = bytearray()
    for chunk in response.iter_content(chunk_size=16_384):
        if not chunk:
            continue
        remaining = limit - len(body)
        body.extend(chunk[:remaining])
        if len(chunk) > remaining or len(body) >= limit:
            break
    return bytes(body)


class RobotsPolicy:
    def __init__(self, timeout: float, default_delay: float):
        self.timeout = timeout
        self.default_delay = default_delay
        self._parsers: dict[str, RobotFileParser] = {}
        self._cache_lock = threading.Lock()
        self._origin_locks: dict[str, threading.Lock] = {}

    def _parser_for(self, url: str) -> RobotFileParser:
        parts = urlsplit(url)
        origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        with self._cache_lock:
            origin_lock = self._origin_locks.setdefault(origin, threading.Lock())
        with origin_lock:
            with self._cache_lock:
                cached_parser = self._parsers.get(origin)
            if cached_parser:
                return cached_parser

            robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
            parser = RobotFileParser(robots_url)
            try:
                response = requests.get(
                    robots_url,
                    headers=REQUEST_HEADERS,
                    timeout=self.timeout,
                    allow_redirects=True,
                    stream=True,
                )
                with response:
                    status_code = response.status_code
                    if 200 <= status_code < 300:
                        text = _read_limited(response, ROBOTS_LIMIT).decode(
                            response.encoding or "utf-8",
                            errors="replace",
                        )
                        parser.parse(text.splitlines())
                    elif 400 <= status_code < 500 and status_code != 429:
                        parser.parse(["User-agent: *", "Allow: /"])
                    else:
                        parser.parse(["User-agent: *", "Disallow: /"])
            except requests.RequestException:
                parser.parse(["User-agent: *", "Disallow: /"])

            with self._cache_lock:
                self._parsers[origin] = parser
            return parser

    def can_fetch(self, url: str) -> bool:
        return self._parser_for(url).can_fetch(USER_AGENT, url)

    def delay_for(self, url: str) -> float:
        parser = self._parser_for(url)
        delay = parser.crawl_delay(USER_AGENT) or 0.0
        request_rate = parser.request_rate(USER_AGENT)
        if request_rate and request_rate.requests > 0:
            delay = max(delay, request_rate.seconds / request_rate.requests)
        return max(self.default_delay, delay)


def _pause(delay: float) -> None:
    if delay > 0:
        time.sleep(delay)


def _origin_for(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _interleave_origins(urls: list[str]) -> list[str]:
    by_origin: dict[str, deque[str]] = {}
    origins: deque[str] = deque()
    for url in urls:
        origin = _origin_for(url)
        if origin not in by_origin:
            by_origin[origin] = deque()
            origins.append(origin)
        by_origin[origin].append(url)

    ordered: list[str] = []
    while origins:
        origin = origins.popleft()
        origin_urls = by_origin[origin]
        ordered.append(origin_urls.popleft())
        if origin_urls:
            origins.append(origin)
    return ordered


def format_elapsed(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds_part = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds_part:02d}s"
    if minutes:
        return f"{minutes}m {seconds_part:02d}s"
    return f"{seconds_part}s"


def shorten_url(url: str, max_length: int = 48) -> str:
    if max_length < 7:
        raise ValueError("max_length must be at least 7.")
    if len(url) <= max_length:
        return url

    kept_length = max_length - 3
    prefix_length = kept_length // 2
    suffix_length = kept_length - prefix_length
    return f"{url[:prefix_length]}...{url[-suffix_length:]}"


class UrlRateTracker:
    def __init__(self, window_seconds: float = 5.0, started_at: float | None = None):
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive.")
        self.window_seconds = window_seconds
        self.started_at = time.perf_counter() if started_at is None else started_at
        self._events: deque[float] = deque()
        self._count = 0

    def record(self, now: float | None = None) -> tuple[float, float]:
        timestamp = time.perf_counter() if now is None else now
        self._count += 1
        self._events.append(timestamp)
        self._discard_old_events(timestamp)
        return self.rates(timestamp)

    def rates(self, now: float | None = None) -> tuple[float, float]:
        timestamp = time.perf_counter() if now is None else now
        self._discard_old_events(timestamp)
        elapsed = max(timestamp - self.started_at, 0.001)
        current_window = max(min(self.window_seconds, elapsed), 0.001)
        current_rate = len(self._events) / current_window
        average_rate = self._count / elapsed
        return current_rate, average_rate

    def _discard_old_events(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._events and self._events[0] < cutoff:
            self._events.popleft()


def default_report_path(
    start_url: str,
    generated_at: datetime | None = None,
) -> Path:
    host = urlsplit(normalize_seed_url(start_url)).hostname or "site"
    safe_host = re.sub(r"[^a-zA-Z0-9._-]+", "_", host).strip("._-") or "site"
    timestamp = generated_at or datetime.now().astimezone()
    base_path = Path(
        f"link_check_report_{safe_host}_{timestamp.strftime('%Y-%m-%d_%H%M')}.csv"
    )
    if not base_path.exists():
        return base_path

    suffix = 2
    while True:
        candidate = base_path.with_name(f"{base_path.stem}_{suffix}{base_path.suffix}")
        if not candidate.exists():
            return candidate
        suffix += 1


class RequestPacer:
    def __init__(self):
        self._lock = threading.Lock()
        self._origin_locks: dict[str, threading.Lock] = {}
        self._last_request: dict[str, float] = {}

    def wait(
        self,
        url: str,
        delay: float,
        cancel_event: threading.Event | None = None,
    ) -> None:
        origin = _origin_for(url)
        with self._lock:
            origin_lock = self._origin_locks.setdefault(origin, threading.Lock())
        with origin_lock:
            if cancel_event and cancel_event.is_set():
                raise InterruptedError("Link checking was canceled.")
            now = time.perf_counter()
            elapsed = now - self._last_request.get(origin, now)
            remaining = delay - elapsed
            if remaining > 0:
                if cancel_event:
                    if cancel_event.wait(remaining):
                        raise InterruptedError("Link checking was canceled.")
                else:
                    time.sleep(remaining)
            if cancel_event and cancel_event.is_set():
                raise InterruptedError("Link checking was canceled.")
            self._last_request[origin] = time.perf_counter()


def crawl_site(
    start_url: str,
    max_pages: int = DEFAULT_MAX_PAGES,
    timeout: float = DEFAULT_TIMEOUT,
    delay: float = DEFAULT_DELAY,
    on_progress: Callable[[int, int, str], None] | None = None,
    workers: int = DEFAULT_WORKERS,
    robots_policy: RobotsPolicy | None = None,
    on_page_complete: Callable[[str], None] | None = None,
) -> CrawlResult:
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1.")
    if timeout <= 0 or delay < 0:
        raise ValueError("timeout must be positive and delay cannot be negative.")
    if workers < 1:
        raise ValueError("workers must be at least 1.")

    start_url = normalize_seed_url(start_url)
    site_host = urlsplit(start_url).hostname or ""
    robots = robots_policy or RobotsPolicy(timeout, delay)
    pacer = RequestPacer()
    cancel_event = threading.Event()
    queue = deque([start_url])
    queued = {start_url}
    visited: set[str] = set()
    parsed_pages: set[str] = set()
    result = CrawlResult()

    def fetch_page(page_url: str) -> tuple[str | None, list[str], bool, bool]:
        if not robots.can_fetch(page_url):
            return None, [], True, False

        pacer.wait(page_url, robots.delay_for(page_url), cancel_event)
        try:
            response = requests.get(
                page_url,
                headers=REQUEST_HEADERS,
                timeout=timeout,
                allow_redirects=True,
                stream=True,
            )
            with response:
                final_url = normalize_url(response.url) or page_url
                if response.status_code >= 400 or not is_internal_url(final_url, site_host):
                    return None, [], False, False

                content_type = response.headers.get("Content-Type", "").lower()
                if content_type and "html" not in content_type:
                    return None, [], False, False
                body = _read_limited(response, HTML_LIMIT)
                encoding = response.encoding or "utf-8"
        except requests.RequestException:
            return None, [], False, True

        if not content_type and not body.lstrip().startswith(b"<"):
            return None, [], False, False
        return final_url, extract_links(body.decode(encoding, errors="replace"), final_url), False, False

    executor = ThreadPoolExecutor(max_workers=workers)
    pending = {}

    def submit_next() -> None:
        while queue and len(visited) < max_pages and len(pending) < workers * 2:
            page_url = queue.popleft()
            queued.discard(page_url)
            if page_url in visited:
                continue
            visited.add(page_url)
            if on_progress:
                on_progress(len(visited), len(result.link_sources), page_url)
            pending[executor.submit(fetch_page, page_url)] = page_url

    try:
        submit_next()
        while pending:
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                page_url = pending.pop(future)
                final_url, links, robots_skipped, request_error = future.result()
                if on_page_complete:
                    on_page_complete(page_url)
                result.robots_skipped += robots_skipped
                result.request_errors += request_error
                if not final_url or final_url in parsed_pages:
                    continue

                parsed_pages.add(final_url)
                result.pages.append(final_url)
                for link_url in links:
                    result.link_sources.setdefault(link_url, set()).add(final_url)
                    if (
                        is_internal_url(link_url, site_host)
                        and link_url not in visited
                        and link_url not in queued
                    ):
                        queue.append(link_url)
                        queued.add(link_url)
            submit_next()
    except BaseException:
        cancel_event.set()
        for future in pending:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)

    result.pages_visited = len(visited)
    result.limit_reached = len(visited) >= max_pages and any(url not in visited for url in queue)
    return result


def check_url(url: str, timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    try:
        response = requests.get(
            url,
            headers=REQUEST_HEADERS,
            timeout=timeout,
            allow_redirects=True,
            stream=True,
        )
        with response:
            return CheckResult(
                status_code=response.status_code,
                final_url=normalize_url(response.url) or response.url,
            )
    except requests.RequestException as error:
        return CheckResult(status_code=None, final_url=None, error=str(error))


def check_links(
    link_sources: Mapping[str, set[str]],
    timeout: float = DEFAULT_TIMEOUT,
    delay: float = DEFAULT_DELAY,
    on_result: Callable[[int, str, CheckResult], None] | None = None,
    workers: int = DEFAULT_WORKERS,
) -> dict[str, CheckResult]:
    if workers < 1:
        raise ValueError("workers must be at least 1.")

    robots = RobotsPolicy(timeout, delay)
    pacer = RequestPacer()
    cancel_event = threading.Event()
    results: dict[str, CheckResult] = {}

    def check_one(url: str) -> CheckResult:
        if not robots.can_fetch(url):
            return CheckResult(
                status_code=None,
                final_url=None,
                skipped_reason="robots.txt",
            )
        pacer.wait(url, robots.delay_for(url), cancel_event)
        return check_url(url, timeout)

    urls = _interleave_origins(sorted(link_sources))
    executor = ThreadPoolExecutor(max_workers=workers)
    pending = {}
    next_url = 0

    def submit_next() -> None:
        nonlocal next_url
        if next_url < len(urls):
            url = urls[next_url]
            next_url += 1
            pending[executor.submit(check_one, url)] = url

    try:
        for _ in range(min(workers * 2, len(urls))):
            submit_next()

        index = 0
        while pending:
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                url = pending.pop(future)
                result = future.result()
                results[url] = result
                index += 1
                if on_result:
                    on_result(index, url, result)
                submit_next()
    except BaseException:
        cancel_event.set()
        for future in pending:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
    return results


def build_findings(
    link_sources: Mapping[str, set[str]],
    results: Mapping[str, CheckResult],
) -> list[Finding]:
    findings: list[Finding] = []
    for url in sorted(link_sources):
        result = results.get(url, CheckResult(status_code=None, final_url=None))
        source_pages = link_sources[url]
        source_hosts = (urlsplit(page).hostname or "" for page in source_pages)
        link_type = (
            "internal"
            if any(is_internal_url(url, source_host) for source_host in source_hosts if source_host)
            else "external"
        )
        issues: list[str] = []
        if result.status_code == 403:
            issues.append("403 Forbidden")
        if result.status_code == 404:
            issues.append("404 Not Found")
        if urlsplit(url).scheme.lower() == "http" or (
            result.final_url and urlsplit(result.final_url).scheme.lower() == "http"
        ):
            issues.append("HTTP (not HTTPS)")
        if not issues:
            continue

        issue = "; ".join(issues)
        for source_page in sorted(link_sources[url]):
            findings.append(
                Finding(
                    issue=issue,
                    status_code=result.status_code,
                    link_type=link_type,
                    url=url,
                    found_on=source_page,
                    final_url=result.final_url,
                )
            )
    return findings


def build_findings_table(
    findings: list[Finding],
    link_sources: Mapping[str, set[str]],
) -> Table:
    table = Table(title="Findings", expand=True, show_lines=True)
    table.add_column("Issue", no_wrap=True)
    table.add_column("Status", justify="right", no_wrap=True)
    table.add_column("Type", no_wrap=True)
    table.add_column("URL", overflow="fold")
    table.add_column("Found on", justify="right", no_wrap=True)

    shown_urls: set[str] = set()
    for finding in findings:
        if finding.url in shown_urls:
            continue
        shown_urls.add(finding.url)
        if finding.status_code == 404:
            issue_style = "bold red"
        elif finding.status_code == 403:
            issue_style = "bold yellow"
        else:
            issue_style = "bold cyan"
        table.add_row(
            Text(finding.issue, style=issue_style),
            str(finding.status_code or "-"),
            finding.link_type,
            Text(finding.url, overflow="fold"),
            f"{len(link_sources.get(finding.url, set()))} page(s)",
        )
    return table


def write_csv(findings: list[Finding], path: str | Path) -> None:
    report_path = Path(path).expanduser()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            newline="",
            encoding="utf-8",
            dir=report_path.parent,
            prefix=f".{report_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as report_file:
            temporary_path = Path(report_file.name)
            writer = csv.DictWriter(report_file, fieldnames=REPORT_COLUMNS)
            writer.writeheader()
            for finding in findings:
                writer.writerow(
                    {
                        "issue": finding.issue,
                        "status_code": finding.status_code or "",
                        "link_type": finding.link_type,
                        "url": finding.url,
                        "found_on": finding.found_on,
                        "final_url": finding.final_url or "",
                    }
                )
        os.replace(temporary_path, report_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def run(
    start_url: str,
    csv_path: str | Path | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    timeout: float = DEFAULT_TIMEOUT,
    delay: float = DEFAULT_DELAY,
    workers: int = DEFAULT_WORKERS,
) -> int:
    started_at = datetime.now().astimezone()
    started_clock = time.perf_counter()
    start_url = normalize_seed_url(start_url)
    site_host = urlsplit(start_url).hostname or ""
    report_path = (
        Path(csv_path).expanduser()
        if csv_path is not None
        else default_report_path(start_url, started_at)
    )
    rate_tracker = UrlRateTracker(started_at=started_clock)

    console = Console(stderr=True, force_terminal=sys.stderr.isatty())
    console.print(f"Site: {escape(start_url)}")
    console.print(
        f"Page crawl scope: {escape(site_host)} (www alias included; other subdomains excluded)"
    )
    console.print("External links will be checked, but their pages will not be crawled.")
    console.print(f"Worker limit: {workers} concurrent page/link requests")
    console.print(
        f"Request pacing: at least {delay:g}s between starts per origin; robots.txt may require more."
    )
    console.print(f"Started at: {started_at.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    progress = Progress(
        SpinnerColumn(),
        TextColumn("elapsed"),
        TimeElapsedColumn(),
        TextColumn(
            "{task.description}",
            table_column=Column(ratio=1, no_wrap=True, overflow="ellipsis"),
        ),
        console=console,
        transient=True,
        disable=not sys.stderr.isatty(),
    )
    progress.start()
    try:
        task_id = progress.add_task("Checking robots.txt...", total=None)
        site_robots = RobotsPolicy(timeout, delay)
        seed_host_delay = site_robots.delay_for(start_url)
        console.print(f"Seed-host effective request spacing: {seed_host_delay:g}s minimum")
        progress.update(task_id, description="Discovering pages...")

        completed_pages = 0

        def update_discovery_progress(pages: int, links: int, url: str) -> None:
            current_rate, average_rate = rate_tracker.rates()
            progress.update(
                task_id,
                description=(
                    f"Mapped {pages} pages, {links} links | {current_rate:.1f} URL/s now | "
                    f"{average_rate:.1f} avg | {shorten_url(url)}"
                ),
            )

        def record_page_completion(_url: str) -> None:
            nonlocal completed_pages
            completed_pages += 1
            rate_tracker.record()
            current_rate, average_rate = rate_tracker.rates()
            progress.update(
                task_id,
                description=(
                    f"Fetched {completed_pages} pages | {current_rate:.1f} URL/s now | "
                    f"{average_rate:.1f} avg"
                ),
            )

        crawl = crawl_site(
            start_url,
            max_pages=max_pages,
            timeout=timeout,
            delay=delay,
            workers=workers,
            robots_policy=site_robots,
            on_progress=update_discovery_progress,
            on_page_complete=record_page_completion,
        )

        internal_count = sum(is_internal_url(url, site_host) for url in crawl.link_sources)
        external_count = len(crawl.link_sources) - internal_count
        insecure_count = sum(urlsplit(url).scheme.lower() == "http" for url in crawl.link_sources)
        console.print("Discovery complete")
        console.print(f"  Pages analyzed: {len(crawl.pages)}")
        console.print(f"  Link occurrences: {sum(len(sources) for sources in crawl.link_sources.values())}")
        console.print(f"  Unique URLs to check: {len(crawl.link_sources)}")
        console.print(f"  Internal URLs: {internal_count}; external URLs: {external_count}")
        console.print(f"  Plain HTTP URLs in discovered links: {insecure_count}")
        if crawl.limit_reached:
            console.print(
                f"  Page limit reached ({max_pages}); increase --max-pages for a broader crawl."
            )
        if crawl.robots_skipped:
            console.print(f"  Pages skipped by robots.txt: {crawl.robots_skipped}")
        if crawl.request_errors:
            console.print(f"  Page requests with network errors: {crawl.request_errors}")

        total = len(crawl.link_sources)
        console.print(
            f"Checking {total} unique URLs with {workers} workers; "
            "only 403, 404, and non-HTTPS findings will be printed."
        )
        issue_counts = {"403": 0, "404": 0, "http": 0}

        def show_progress(index: int, url: str, result: CheckResult) -> None:
            if result.status_code == 403:
                issue_counts["403"] += 1
            if result.status_code == 404:
                issue_counts["404"] += 1
            current_rate, average_rate = rate_tracker.record()
            is_http = urlsplit(url).scheme.lower() == "http" or (
                result.final_url and urlsplit(result.final_url).scheme.lower() == "http"
            )
            if is_http:
                issue_counts["http"] += 1

            issues = []
            if result.status_code == 403:
                issues.append("403 FORBIDDEN")
            if result.status_code == 404:
                issues.append("404")
            if is_http:
                issues.append("NOT HTTPS")
            if issues:
                page_count = len(crawl.link_sources[url])
                link_type = "internal" if is_internal_url(url, site_host) else "external"
                console.print(
                    f"[{'; '.join(issues)}] ({link_type}) {url} (found on {page_count} page(s))",
                    markup=False,
                )

            progress.update(
                task_id,
                description=(
                    f"Checked {index}/{total} | {current_rate:.1f} URL/s now | "
                    f"{average_rate:.1f} avg"
                ),
            )

        progress.update(task_id, description=f"Checking 0/{total} URLs...")
        results = check_links(
            crawl.link_sources,
            timeout=timeout,
            delay=delay,
            on_result=show_progress,
            workers=workers,
        )

        progress.update(task_id, description="Writing CSV report...")
        findings = build_findings(crawl.link_sources, results)
        if findings:
            write_csv(findings, report_path)
            console.print(build_findings_table(findings, crawl.link_sources))
        else:
            console.print("No issues found; no CSV report was created.")
    finally:
        progress.stop()

    broken_count = sum(result.status_code == 404 for result in results.values())
    forbidden_count = sum(result.status_code == 403 for result in results.values())
    internal_broken_count = sum(
        result.status_code == 404 and is_internal_url(url, site_host)
        for url, result in results.items()
    )
    external_broken_count = broken_count - internal_broken_count
    internal_forbidden_count = sum(
        result.status_code == 403 and is_internal_url(url, site_host)
        for url, result in results.items()
    )
    external_forbidden_count = forbidden_count - internal_forbidden_count
    http_count = sum(
        1
        for url, result in results.items()
        if urlsplit(url).scheme.lower() == "http"
        or (
            result.final_url is not None
            and urlsplit(result.final_url).scheme.lower() == "http"
        )
    )
    internal_http_count = sum(
        1
        for url, result in results.items()
        if is_internal_url(url, site_host)
        and (
            urlsplit(url).scheme.lower() == "http"
            or (
                result.final_url is not None
                and urlsplit(result.final_url).scheme.lower() == "http"
            )
        )
    )
    external_http_count = http_count - internal_http_count
    error_count = sum(result.error is not None for result in results.values())
    skipped_count = sum(result.skipped_reason is not None for result in results.values())

    finished_at = datetime.now().astimezone()
    elapsed = time.perf_counter() - started_clock
    current_rate, average_rate = rate_tracker.rates()
    console.print("Finished")
    console.print(
        f"  404 URLs: {broken_count} ({internal_broken_count} internal, "
        f"{external_broken_count} external)"
    )
    console.print(
        f"  403 URLs: {forbidden_count} ({internal_forbidden_count} internal, "
        f"{external_forbidden_count} external)"
    )
    console.print(
        f"  Non-HTTPS URLs: {http_count} ({internal_http_count} internal, "
        f"{external_http_count} external)"
    )
    console.print(f"  Network errors: {error_count}; URLs skipped by robots.txt: {skipped_count}")
    if findings:
        console.print(f"  CSV report: {report_path}", markup=False)
    console.print(f"Finished at: {finished_at.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    console.print(f"Total elapsed: {format_elapsed(elapsed)}")
    console.print(f"Current URL rate: {current_rate:.2f} URLs/s")
    console.print(f"Average URL rate: {average_rate:.2f} URLs/s")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find 404 and non-HTTPS links on a website.")
    parser.add_argument("url", nargs="?", help="site URL; prompted for when omitted")
    parser.add_argument("--csv", help="CSV output path; defaults to a unique site/date/time filename")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args(argv)

    if args.max_pages < 1 or args.timeout <= 0 or args.delay < 0 or args.workers < 1:
        parser.error(
            "--max-pages, --timeout, and --workers must be positive; --delay cannot be negative"
        )

    try:
        raw_url = args.url or input("Enter the site URL: ")
        start_url = normalize_seed_url(raw_url)
        return run(start_url, args.csv, args.max_pages, args.timeout, args.delay, args.workers)
    except KeyboardInterrupt:
        print("\nCanceled. No partial report was saved; any report already on disk is intact.")
        return 130
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())