import csv
import contextlib
import io
import tempfile
import threading
import time
import unittest
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from link_checker import (
    CheckResult,
    Finding,
    build_findings,
    check_links,
    crawl_site,
    default_report_path,
    extract_links,
    format_elapsed,
    is_internal_url,
    main,
    normalize_url,
    RequestPacer,
    shorten_url,
    UrlRateTracker,
    write_csv,
)
from unittest.mock import patch


class UrlResolutionTests(unittest.TestCase):
    def test_normalize_url_resolves_paths_and_removes_fragments(self):
        self.assertEqual(
            normalize_url("../news?id=7#latest", "https://www.example.com/guide/start"),
            "https://www.example.com/news?id=7",
        )

    def test_normalize_url_skips_non_http_references(self):
        self.assertIsNone(normalize_url("mailto:editor@example.com"))
        self.assertIsNone(normalize_url("javascript:void(0)"))
        self.assertIsNone(normalize_url("https://user:pass@example.com/private"))

    def test_extract_links_respects_base_and_ignores_non_web_links(self):
        html = """
        <base href="https://example.com/docs/">
        <a href="../article?edition=2#intro">Article</a>
        <a href="//cdn.example.net/file">External</a>
        <a href="#section">Same page</a>
        <a href="mailto:editor@example.com">Email</a>
        <area href="/map-target">Map</area>
        """

        self.assertEqual(
            extract_links(html, "https://example.com/docs/start"),
            [
                "https://example.com/article?edition=2",
                "https://cdn.example.net/file",
                "https://example.com/map-target",
            ],
        )

    def test_internal_scope_includes_www_alias_but_not_other_subdomains(self):
        self.assertTrue(is_internal_url("https://www.example.com/about", "example.com"))
        self.assertTrue(is_internal_url("http://example.com/about", "www.example.com"))
        self.assertFalse(is_internal_url("https://blog.example.com/about", "example.com"))

    def test_internal_scope_matches_ipv6_hosts(self):
        self.assertTrue(is_internal_url("http://[::1]:8080/about", "::1"))


class DurationFormatTests(unittest.TestCase):
    def test_format_elapsed_shows_seconds_minutes_and_hours(self):
        self.assertEqual(format_elapsed(5), "5s")
        self.assertEqual(format_elapsed(65), "1m 05s")
        self.assertEqual(format_elapsed(3661), "1h 01m 01s")


class RequestPacerTests(unittest.TestCase):
    def test_wait_stops_promptly_when_canceled(self):
        pacer = RequestPacer()
        cancel_event = threading.Event()
        timer = threading.Timer(0.02, cancel_event.set)
        timer.start()
        started = time.perf_counter()
        try:
            with self.assertRaises(InterruptedError):
                pacer.wait("https://example.com/", 5, cancel_event)
        finally:
            timer.cancel()

        self.assertLess(time.perf_counter() - started, 1)


class UrlRateTrackerTests(unittest.TestCase):
    def test_rates_report_rolling_current_and_whole_run_average(self):
        tracker = UrlRateTracker(window_seconds=5, started_at=0)
        tracker.record(now=1)
        current, average = tracker.record(now=2)

        self.assertEqual(current, 1.0)
        self.assertEqual(average, 1.0)

        current, average = tracker.record(now=7)
        self.assertAlmostEqual(current, 0.4)
        self.assertAlmostEqual(average, 3 / 7)


class ReportNamingTests(unittest.TestCase):
    def test_default_report_name_includes_domain_date_and_24_hour_time(self):
        generated_at = datetime(2026, 11, 5, 13, 7, tzinfo=timezone.utc)

        report_path = default_report_path("https://www.qstar.se/path", generated_at)

        self.assertEqual(
            report_path.name,
            "link_check_report_www.qstar.se_2026-11-05_1307.csv",
        )


class ProgressDisplayTests(unittest.TestCase):
    def test_shorten_url_caps_width_and_preserves_its_ends(self):
        url = "https://example.com/press/very-long-announcement-name/about.html"

        shortened = shorten_url(url, max_length=48)

        self.assertEqual(len(shortened), 48)
        self.assertTrue(shortened.startswith("https://example.com"))
        self.assertTrue(shortened.endswith("about.html"))
        self.assertIn("...", shortened)


class LocalSiteHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        self.server.request_counts[path] += 1

        if path.startswith("/slow/"):
            with self.server.activity_lock:
                self.server.active_slow_requests += 1
                self.server.max_active_slow_requests = max(
                    self.server.max_active_slow_requests,
                    self.server.active_slow_requests,
                )
            try:
                time.sleep(0.1)
            finally:
                with self.server.activity_lock:
                    self.server.active_slow_requests -= 1
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/missing")
            self.end_headers()
            return

        status, content_type, body = self.server.routes.get(
            path,
            (404, "text/plain", b"not found"),
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        pass


class LocalSiteServer(ThreadingHTTPServer):
    daemon_threads = True


class CrawlAndReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = LocalSiteServer(("127.0.0.1", 0), LocalSiteHandler)
        cls.port = cls.server.server_address[1]
        external_url = f"http://localhost:{cls.port}/external"
        cls.external_broken_url = f"http://localhost:{cls.port}/external-missing"
        root_html = (
            '<a href="/inside">inside</a>'
            '<a href="/missing">missing</a>'
            '<a href="/redirect">redirect</a>'
            '<a href="/blocked">blocked</a>'
            f'<a href="{external_url}">external</a>'
            f'<a href="{cls.external_broken_url}">external broken</a>'
            '<a href="mailto:editor@example.com">email</a>'
        ).encode()
        crawl_concurrency_html = b"".join(
            f'<a href="/slow/crawl-{index}">slow {index}</a>'.encode()
            for index in range(4)
        )
        cls.server.routes = {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nDisallow: /blocked\nAllow: /\n"),
            "/": (200, "text/html", root_html),
            "/crawl-concurrency": (200, "text/html", crawl_concurrency_html),
            "/blocked": (200, "text/html", b"should not be fetched"),
            "/inside": (200, "text/html", b'<a href="/missing">again</a>'),
            "/external": (200, "text/html", b'<a href="/external-child">child</a>'),
            "/external-child": (200, "text/html", b"child"),
            "/missing": (404, "text/plain", b"not found"),
        }
        cls.server.request_counts = Counter()
        cls.server.activity_lock = threading.Lock()
        cls.server.active_slow_requests = 0
        cls.server.max_active_slow_requests = 0
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.start_url = f"http://127.0.0.1:{cls.port}/"

    def setUp(self):
        self.original_routes = self.server.routes.copy()
        self.server.request_counts.clear()
        self.server.max_active_slow_requests = 0

    def tearDown(self):
        self.server.routes.clear()
        self.server.routes.update(self.original_routes)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join()

    def test_crawl_maps_internal_and_external_links_without_traversing_external_pages(self):
        crawl = crawl_site(self.start_url, max_pages=20, timeout=2, delay=0)
        external_url = f"http://localhost:{self.port}/external"

        self.assertIn("/inside", {urlsplit(page).path for page in crawl.pages})
        self.assertIn(external_url, crawl.link_sources)
        self.assertNotIn(f"http://localhost:{self.port}/external-child", crawl.link_sources)
        self.assertEqual(self.server.request_counts["/external"], 0)
        self.assertNotIn(f"http://127.0.0.1:{self.port}/blocked", crawl.pages)
        self.assertEqual(self.server.request_counts["/blocked"], 0)
        results = check_links(crawl.link_sources, timeout=2, delay=0)
        self.assertEqual(results[f"http://127.0.0.1:{self.port}/blocked"].skipped_reason, "robots.txt")
        self.assertEqual(results[self.external_broken_url].status_code, 404)
        external_finding = next(
            finding
            for finding in build_findings(crawl.link_sources, results)
            if finding.url == self.external_broken_url
        )
        self.assertIn("404 Not Found", external_finding.issue)
        self.assertEqual(external_finding.link_type, "external")

    def test_check_links_checks_urls_concurrently_within_worker_limit(self):
        source_page = self.start_url
        links = {
            f"http://127.0.0.1:{self.port}/slow/{index}": {source_page}
            for index in range(4)
        }

        results = check_links(links, timeout=2, delay=0, workers=4)

        self.assertEqual(len(results), 4)
        self.assertTrue(all(result.status_code == 200 for result in results.values()))
        self.assertGreaterEqual(self.server.max_active_slow_requests, 2)
        self.assertLessEqual(self.server.max_active_slow_requests, 4)

    def test_crawl_stops_at_the_configured_page_cap(self):
        crawl = crawl_site(self.start_url, max_pages=1, timeout=2, delay=0)

        self.assertEqual(len(crawl.pages), 1)
        self.assertTrue(crawl.limit_reached)

    def test_crawl_reports_page_and_link_progress(self):
        progress = []
        crawl_site(
            self.start_url,
            max_pages=20,
            timeout=2,
            delay=0,
            on_progress=lambda pages, links, url: progress.append((pages, links, url)),
        )

        self.assertTrue(progress)
        self.assertEqual(progress[0], (1, 0, self.start_url))
        self.assertTrue(any(link_count > 0 for _, link_count, _ in progress))

    def test_crawl_reports_each_completed_page_request(self):
        completed_pages = []

        crawl = crawl_site(
            self.start_url,
            max_pages=20,
            timeout=2,
            delay=0,
            on_page_complete=completed_pages.append,
        )

        self.assertEqual(len(completed_pages), crawl.pages_visited)

    def test_crawl_site_fetches_pages_concurrently_within_worker_limit(self):
        crawl_site(
            f"http://127.0.0.1:{self.port}/crawl-concurrency",
            max_pages=10,
            timeout=2,
            delay=0,
            workers=4,
        )

        self.assertGreaterEqual(self.server.max_active_slow_requests, 2)
        self.assertLessEqual(self.server.max_active_slow_requests, 4)

    def test_redirected_404_and_http_links_are_written_for_each_source_page(self):
        crawl = crawl_site(self.start_url, max_pages=20, timeout=2, delay=0)
        results = check_links(crawl.link_sources, timeout=2, delay=0)
        redirect_url = f"http://127.0.0.1:{self.port}/redirect"

        self.assertEqual(results[redirect_url].status_code, 404)
        self.assertEqual(results[redirect_url].final_url, f"http://127.0.0.1:{self.port}/missing")

        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.csv"
            findings = build_findings(crawl.link_sources, results)
            write_csv(findings, report_path)
            with report_path.open(newline="", encoding="utf-8") as report_file:
                rows = list(csv.DictReader(report_file))

        self.assertEqual(
            set(rows[0]),
            {"issue", "status_code", "link_type", "url", "found_on", "final_url"},
        )
        missing_rows = [row for row in rows if row["url"].endswith("/missing")]
        self.assertEqual(len(missing_rows), 2)
        self.assertTrue(all("404" in row["issue"] for row in missing_rows))
        self.assertTrue(all(row["status_code"] == "404" for row in missing_rows))
        external_missing_rows = [row for row in rows if row["url"] == self.external_broken_url]
        self.assertEqual(len(external_missing_rows), 1)
        self.assertIn("404 Not Found", external_missing_rows[0]["issue"])
        self.assertEqual(external_missing_rows[0]["link_type"], "external")

    def test_findings_include_http_success_and_https_404(self):
        http_url = "http://example.com/secure-redirect"
        https_url = "https://example.com/not-found"
        downgrade_url = "https://example.com/downgrade"
        source_page = "https://example.com/page"
        sources = {
            http_url: {source_page},
            https_url: {source_page},
            downgrade_url: {source_page},
        }
        results = {
            http_url: CheckResult(status_code=200, final_url="https://example.com/secure-redirect"),
            https_url: CheckResult(status_code=404, final_url=https_url),
            downgrade_url: CheckResult(status_code=200, final_url="http://example.com/downgrade"),
        }

        findings = build_findings(sources, results)

        self.assertEqual({finding.issue for finding in findings}, {"HTTP (not HTTPS)", "404 Not Found"})
        self.assertEqual(len(findings), 3)
        self.assertTrue(any(finding.url == downgrade_url for finding in findings))

    def test_403_is_classified_separately_from_404(self):
        url = "https://example.com/private"

        findings = build_findings(
            {url: {"https://example.com/"}},
            {url: CheckResult(status_code=403, final_url=url)},
        )

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].issue, "403 Forbidden")
        self.assertEqual(findings[0].status_code, 403)

    def test_cli_prints_discovery_summary_before_findings_and_writes_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "cli-report.csv"
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                exit_code = main(
                    [
                        self.start_url,
                        "--csv",
                        str(report_path),
                        "--max-pages",
                        "20",
                        "--timeout",
                        "2",
                        "--delay",
                        "0",
                        "--workers",
                        "4",
                    ]
                )

            output_text = output.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertLess(output_text.index("Discovery complete"), output_text.index("Checking"))
            self.assertIn("External links will be checked", output_text)
            self.assertIn("Worker limit: 4 concurrent page/link requests", output_text)
            self.assertIn("Checking 6 unique URLs with 4 workers", output_text)
            self.assertIn("only 403, 404, and non-HTTPS findings", output_text)
            self.assertIn("Seed-host effective request spacing:", output_text)
            self.assertIn("Started at:", output_text)
            self.assertIn("Finished at:", output_text)
            self.assertIn("Total elapsed:", output_text)
            self.assertIn("404 URLs: 3 (2 internal, 1 external)", output_text)
            self.assertIn("403 URLs: 0 (0 internal, 0 external)", output_text)
            self.assertIn("Non-HTTPS URLs: 6 (4 internal, 2 external)", output_text)
            self.assertIn("Plain HTTP URLs in discovered links:", output_text)
            self.assertIn("[404; NOT HTTPS]", output_text)
            self.assertNotIn("[200]", output_text)
            self.assertIn("Findings", output_text)
            self.assertIn("Issue", output_text)
            self.assertIn("Average URL rate:", output_text)
            self.assertIn("Current URL rate:", output_text)
            self.assertTrue(report_path.is_file())

    def test_cli_uses_generated_report_path_when_csv_option_is_omitted(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "link_check_report_example.com_2026-11-05_1307.csv"
            with patch("link_checker.default_report_path", return_value=report_path), patch(
                "link_checker.write_csv"
            ) as write_csv_mock:
                exit_code = main(
                    [
                        self.start_url,
                        "--max-pages",
                        "1",
                        "--timeout",
                        "2",
                        "--delay",
                        "0",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(write_csv_mock.call_args.args[1], report_path)

    def test_cli_does_not_create_csv_when_there_are_no_findings(self):
        clean_url = f"https://example.com/clean-{self.port}"
        clean_report = Path(tempfile.gettempdir()) / f"clean-report-{self.port}.csv"
        clean_report.write_text("previous results\n", encoding="utf-8")
        clean_html = b'<a href="/inside">inside</a>'
        self.server.routes[f"/clean-{self.port}"] = (200, "text/html", clean_html)
        self.server.routes["/robots.txt"] = (200, "text/plain", b"User-agent: *\nAllow: /\n")
        self.server.routes["/inside"] = (200, "text/html", b"<p>ok</p>")
        output = io.StringIO()

        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            exit_code = main(
                [
                    clean_url,
                    "--csv",
                    str(clean_report),
                    "--max-pages",
                    "10",
                    "--timeout",
                    "2",
                    "--delay",
                    "0",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(clean_report.read_text(encoding="utf-8"), "previous results\n")
        self.assertIn("No issues found; no CSV report was created.", output.getvalue())
        clean_report.unlink()

    def test_cli_handles_cancellation_without_replacing_existing_report(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.csv"
            report_path.write_text("previous report\n", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), patch(
                "link_checker.crawl_site",
                side_effect=KeyboardInterrupt,
            ):
                exit_code = main([self.start_url, "--csv", str(report_path)])
            self.assertEqual(report_path.read_text(encoding="utf-8"), "previous report\n")

        self.assertEqual(exit_code, 130)
        self.assertIn("Canceled", output.getvalue())

    def test_interrupted_csv_write_cleans_temporary_file_and_preserves_old_report(self):
        class InterruptedFindings(list):
            def __iter__(self):
                yield Finding(
                    issue="404 Not Found",
                    status_code=404,
                    link_type="external",
                    url="https://example.com/missing",
                    found_on="https://example.com/",
                    final_url="https://example.com/missing",
                )
                raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.csv"
            report_path.write_text("previous report\n", encoding="utf-8")

            with self.assertRaises(KeyboardInterrupt):
                write_csv(InterruptedFindings(), report_path)

            self.assertEqual(report_path.read_text(encoding="utf-8"), "previous report\n")
            self.assertEqual(list(Path(directory).glob(".report.csv.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()