#!/usr/bin/env python3
"""Offline query/CLI tests. Only invented orgs, IDs, and synthetic credentials."""
import contextlib
import http.client
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import cloud_config
import query

CREDS = {"org": "acme", "api_key": "synthetic-test-key", "base_url": "https://api.databend.com", "warehouse": "default"}
SCHEMA = [{"name": "n", "type": "Int"}]


class RequestTest(unittest.TestCase):
    def send(self, body=b"{}", error=None, payload=None, response_headers=None):
        opener = mock.Mock()
        opener.open.side_effect = error
        stream = io.BytesIO(body)
        stream.headers = response_headers or {}
        opener.open.return_value = stream
        with mock.patch.object(query.urllib.request, "build_opener", return_value=opener) as factory:
            result = query.request("https://api.databend.com/v1/query", {}, payload, 5)
        self.assertIsInstance(factory.call_args.args[0], query.NoRedirect)
        return result, opener.open.call_args.args[0]

    def test_post_and_user_agent(self):
        result, request = self.send(b'{"id":"q"}', payload={"sql": "SELECT 1"})
        self.assertEqual(result["id"], "q")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("User-agent"), query.USER_AGENT)

    def test_gateway_source_is_retained_as_http_metadata_not_json(self):
        result, _ = self.send(b'{"id":"q","data":[]}', response_headers={"X-Databend-Query-Source": "gateway"})
        self.assertTrue(result.gateway_local)
        self.assertNotIn("gateway_local", result)
        ordinary, _ = self.send(b'{"id":"q","gateway_local":true}')
        self.assertFalse(ordinary.gateway_local)

    def test_route_hint_is_retained_as_http_metadata_not_json(self):
        headers = http.client.HTTPMessage()
        headers["X-Databend-Route-Hint"] = "clextra01-a03c"
        result, _ = self.send(b'{"id":"q"}', response_headers=headers)
        self.assertEqual(result.route_hint, "clextra01-a03c")
        self.assertNotIn("route_hint", result)
        self.assertIsNone(self.send(b'{"id":"q","route_hint":"clx-1"}')[0].route_hint)

    def test_unsafe_route_hint_is_ignored(self):
        for value in ("cl a", "cl/x", "cl;x", "x" * 257, ""):
            with self.subTest(value=value):
                headers = http.client.HTTPMessage()
                headers["X-Databend-Route-Hint"] = value
                self.assertIsNone(self.send(b"{}", response_headers=headers)[0].route_hint)

    def test_route_hint_is_sent_as_request_header(self):
        opener = mock.Mock()
        stream = io.BytesIO(b"{}")
        stream.headers = {}
        opener.open.return_value = stream
        with mock.patch.object(query.urllib.request, "build_opener", return_value=opener):
            query.request("https://api.databend.com/v1/query/q/page/1", query.with_route_hint({}, "clx-1"), None, 5)
        self.assertEqual(opener.open.call_args.args[0].get_header("X-databend-route-hint"), "clx-1")
        self.assertEqual(query.with_route_hint({"A": "1"}, None), {"A": "1"})

    def test_get_and_empty_post(self):
        self.assertEqual(self.send()[1].get_method(), "GET")
        self.assertEqual(self.send(payload={})[1].get_method(), "POST")

    def test_redirect_handler_never_forwards(self):
        self.assertIsNone(query.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example"))

    def test_redirect_error_is_not_success(self):
        error = urllib.error.HTTPError("https://api.databend.com", 302, "redirect", {}, io.BytesIO(b"{}"))
        with self.assertRaisesRegex(query.QueryError, "redirect refused"):
            self.send(error=error)

    def test_http_json_error(self):
        error = urllib.error.HTTPError("https://api.databend.com", 400, "bad", {},
                                       io.BytesIO(b'{"error":{"kind":"BadSQL","message":"bad"}}'))
        with self.assertRaisesRegex(query.QueryError, "BadSQL"):
            self.send(error=error)

    def test_http_json_without_error_is_not_success(self):
        error = urllib.error.HTTPError("https://api.databend.com", 403, "bad", {}, io.BytesIO(b"{}"))
        with self.assertRaisesRegex(query.QueryError, "HTTP 403"):
            self.send(error=error)

    def test_html_error_is_not_echoed(self):
        error = urllib.error.HTTPError("https://api.databend.com", 403, "bad", {}, io.BytesIO(b"private-proxy-page"))
        with self.assertRaises(query.QueryError) as raised:
            self.send(error=error)
        self.assertNotIn("private-proxy-page", str(raised.exception))

    def test_network_timeout_and_bad_response(self):
        for error in (urllib.error.URLError("route"), TimeoutError(), OSError("socket")):
            with self.subTest(error=error), self.assertRaisesRegex(query.QueryError, "do not blindly resubmit"):
                self.send(error=error)
        for body in (b"not-json", b"[]"):
            with self.assertRaises(query.QueryError):
                self.send(body)

    def test_empty_cleanup_response_is_accepted(self):
        self.assertEqual(self.send(b"")[0], {})

    def test_oversized_response_is_bounded(self):
        with mock.patch.object(query, "MAX_RESPONSE_BYTES", 8):
            with self.assertRaisesRegex(query.QueryError, "exceeds"):
                self.send(b"0123456789")

    def test_cloud_and_engine_errors(self):
        for payload in ({"error": "bad"}, {"error": {"code": 1008, "message": "bad"}},
                        {"code": "AUTH_FAILED", "message": "no"}, {"success": False}):
            with self.assertRaises(query.QueryError):
                query.raise_for_error(payload)
        query.raise_for_error({"error": None, "data": []})


class PaginationTest(unittest.TestCase):
    def execute(self, pages, creds=None, **kwargs):
        calls = []
        iterator = iter(pages)

        def fake(url, headers, payload=None, timeout=60):
            calls.append((url, headers, payload, timeout))
            if url.endswith(("/final", "/kill")):
                return {}
            item = next(iterator)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(query, "request", fake), mock.patch.object(query.time, "sleep"), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            result = query.run_once("SELECT 1", creds or CREDS, 60, **kwargs)
        return result, calls, err.getvalue()

    def test_schema_can_arrive_after_empty_first_page(self):
        result, calls, _ = self.execute([
            {"id": "q", "data": [], "next_uri": "/v1/query/q/page/0"},
            {"schema": SCHEMA, "data": [[1]], "next_uri": "/v1/query/q/page/1"},
            {"data": [[2]], "next_uri": "/v1/query/q/final"},
        ])
        self.assertEqual(result, (SCHEMA, [[1], [2]]))
        self.assertTrue(calls[-1][0].endswith("/final"))
        self.assertFalse(any(call[0].endswith("/kill") for call in calls))

    def test_china_query_pages_and_cleanup_stay_on_china(self):
        result, calls, _ = self.execute([
            {"id": "q", "data": [], "next_uri": "/v1/query/q/page/0"},
            {"schema": SCHEMA, "data": [[1]], "next_uri": "/v1/query/q/final"},
        ], creds={**CREDS, "base_url": "https://api.databend.cn"})
        self.assertEqual(result[1], [[1]])
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call[0].startswith("https://api.databend.cn/v1/query") for call in calls))

    def test_gateway_management_result_does_not_poll_or_finalize_compute(self):
        result, calls, _ = self.execute([query.QueryResponse({
            "id": "management-query", "schema": SCHEMA, "data": [[1]], "state": "Succeeded"
        }, gateway_local=True)])
        self.assertEqual(result, (SCHEMA, [[1]]))
        self.assertEqual(len(calls), 1)

    def test_zero_row_gateway_change_is_success_without_engine_cleanup(self):
        result, calls, _ = self.execute([query.QueryResponse({
            "id": "management-query", "schema": [], "data": [], "state": "Succeeded"
        }, gateway_local=True)])
        self.assertEqual(result, ([], []))
        self.assertEqual(len(calls), 1)

    def test_gateway_errors_do_not_issue_engine_kill_or_final(self):
        response = query.QueryResponse({"id": "management-query", "error": {"kind": "Denied"}}, gateway_local=True)
        request = mock.Mock(return_value=response)
        with mock.patch.object(query, "request", request), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(query.QueryError, "Denied"):
                query.run_once("SHOW WAREHOUSES", CREDS, 60)
        self.assertEqual(request.call_count, 1)

    def test_gateway_output_cap_is_reported_without_cancellation(self):
        result, calls, error = self.execute([query.QueryResponse({
            "id": "management-query", "schema": SCHEMA, "data": [[1], [2]], "state": "Succeeded"
        }, gateway_local=True)], max_rows=1)
        self.assertEqual(result[1], [[1]])
        self.assertEqual(len(calls), 1)
        self.assertIn("truncated", error)

    def test_gateway_continuation_is_rejected_without_engine_requests(self):
        response = query.QueryResponse({"id": "management-query", "next_uri": "/v1/query/management-query/page/0"}, gateway_local=True)
        request = mock.Mock(return_value=response)
        with mock.patch.object(query, "request", request), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(query.QueryError, "Unexpected continuation"):
                query.run_once("SHOW WAREHOUSES", CREDS, 60)
        self.assertEqual(request.call_count, 1)

    def test_empty_page_repeats_are_polled(self):
        result, _, _ = self.execute([
            {"id": "q", "data": [], "next_uri": "/v1/query/q/page/0"},
            {"data": [], "next_uri": "/v1/query/q/page/0"},
            {"schema": SCHEMA, "data": [[1]], "next_uri": "/v1/query/q/final"},
        ])
        self.assertEqual(result[1], [[1]])

    def test_single_page_and_headers(self):
        result, calls, _ = self.execute([{"id": "q", "schema": SCHEMA, "data": [[1]], "next_uri": ""}])
        self.assertEqual(result[1], [[1]])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1]["X-DatabendCloud-Token"], CREDS["api_key"])
        self.assertEqual(calls[0][1]["X-DatabendCloud-Org"], "acme")
        self.assertEqual(calls[0][1]["X-DatabendCloud-Warehouse"], "default")

    def test_cap_cancels_and_finalizes_without_fetching_more(self):
        result, calls, err = self.execute([
            {"id": "q", "schema": SCHEMA, "data": [[1], [2]], "next_uri": "/v1/query/q/page/0"}
        ], max_rows=1)
        self.assertEqual(result[1], [[1]])
        self.assertIn("capped", err)
        self.assertEqual([call[0].rsplit("/", 1)[-1] for call in calls], ["query", "kill", "final"])
        self.assertEqual(calls[1][2], {})

    def test_final_page_truncation_is_reported(self):
        for uri in ("", "/v1/query/q/final"):
            _, calls, err = self.execute([
                {"id": "q", "schema": SCHEMA, "data": [[1], [2]], "next_uri": uri}
            ], max_rows=1)
            self.assertIn("truncated", err)
            self.assertFalse(any(call[0].endswith("/kill") for call in calls))

    def test_foreign_query_or_origin_is_rejected(self):
        for uri in ("https://evil.example/v1/query/q/page/0", "//evil.example/v1/query/q/page/0",
                    "/v1/query/other/page/0", "/api/v1/my/orgs", "/v1/query/q/page/0?secret=x",
                    "/v1/query/q/page/0#fragment"):
            calls = []

            def fake(url, headers, payload=None, timeout=60):
                calls.append(url)
                return {"id": "q", "next_uri": uri} if url.endswith("/query") else {}

            with self.subTest(uri=uri), mock.patch.object(query, "request", fake), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(query.QueryError):
                query.run_once("SELECT 1", CREDS, 60)
            self.assertTrue(all(url.startswith(CREDS["base_url"] + "/v1/query") for url in calls))
            self.assertEqual(len(calls), 3)

    def test_absolute_same_origin_page_allowed(self):
        result, _, _ = self.execute([
            {"id": "q", "next_uri": "https://api.databend.com/v1/query/q/page/0"},
            {"schema": SCHEMA, "data": [[3]], "next_uri": ""},
        ])
        self.assertEqual(result[1], [[3]])

    def test_page_error_still_cleans_up(self):
        calls = []

        def fake(url, headers, payload=None, timeout=60):
            calls.append(url)
            if url.endswith("/query"):
                return {"id": "q", "next_uri": "/v1/query/q/page/0"}
            if "/page/" in url:
                return {"error": {"kind": "Aborted"}}
            return {}

        with mock.patch.object(query, "request", fake), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(query.QueryError, "Aborted"):
                query.run_once("SELECT 1", CREDS, 60)
        self.assertTrue(calls[-2].endswith("/kill"))
        self.assertTrue(calls[-1].endswith("/final"))

    def test_no_automatic_resubmission(self):
        runner = mock.Mock(side_effect=query.QueryError("ProvisionWarehouseTimeout"))
        with mock.patch.object(query, "run_once", runner), self.assertRaises(query.QueryError):
            query.run("INSERT INTO t VALUES (1)", CREDS, 60)
        self.assertEqual(runner.call_count, 1)

    def test_unknown_query_id_is_not_used_for_cleanup(self):
        for value in (None, "", "../other", "bad/id"):
            fake = mock.Mock(return_value={"id": value, "data": []})
            with mock.patch.object(query, "request", fake), self.assertRaises(query.QueryError):
                query.run_once("SELECT 1", CREDS, 60)
            self.assertEqual(fake.call_count, 1)

    def test_deadline_cleans_up(self):
        clock = {"time": 0}
        calls = []

        def fake(url, headers, payload=None, timeout=60):
            calls.append(url)
            return {"id": "q", "data": [], "next_uri": "/v1/query/q/page/0"} if not url.endswith(("/kill", "/final")) else {}

        def sleep(seconds):
            clock["time"] += seconds

        with mock.patch.object(query, "request", fake), mock.patch.object(query.time, "sleep", sleep), \
                mock.patch.object(query.time, "monotonic", lambda: clock["time"]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(query.QueryError, "deadline exceeded"):
            query.run_once("SELECT 1", CREDS, 1)
        self.assertTrue(calls[-1].endswith("/final"))

    def test_malformed_data_and_failed_state_raise(self):
        for page in ({"id": "q", "data": ["not-a-row"]},
                     {"id": "q", "schema": "not-a-schema"},
                     {"id": "q", "state": "Failed"}):
            with self.subTest(page=page), self.assertRaises(query.QueryError):
                self.execute([page])

    def test_cleanup_failure_is_visible_and_does_not_hide_result(self):
        def fake(url, headers, payload=None, timeout=60):
            if url.endswith("/final"):
                raise query.QueryError(CREDS["api_key"])
            return {"id": "q", "schema": SCHEMA, "data": [[1]]}

        with mock.patch.object(query, "request", fake), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(query.run_once("SELECT 1", CREDS, 60)[1], [[1]])
        self.assertIn("finalization could not be confirmed", err.getvalue())
        self.assertNotIn(CREDS["api_key"], err.getvalue())


class RouteHintTest(unittest.TestCase):
    """A multi-cluster warehouse keeps a query on one cluster; follow-up calls must name it."""

    def execute(self, responses, **kwargs):
        calls = []
        iterator = iter(responses)

        def respond(url, headers, payload=None, timeout=60):
            calls.append((url, dict(headers)))
            item = next(iterator)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(query, "request", respond), mock.patch.object(query.time, "sleep"), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                result = query.run_once("SELECT 1", CREDS, 60, **kwargs)
            except query.QueryError as error:
                result = error
        return result, calls, err.getvalue()

    @staticmethod
    def hint(call):
        return call[1].get(query.ROUTE_HINT_HEADER)

    def test_first_hint_is_sent_on_pages_and_final_but_not_on_start(self):
        result, calls, err = self.execute([
            query.QueryResponse({"id": "q", "data": [], "next_uri": "/v1/query/q/page/0"}, route_hint="cla-1"),
            query.QueryResponse({"schema": SCHEMA, "data": [[1]], "next_uri": "/v1/query/q/page/1"}),
            query.QueryResponse({"data": [[2]], "next_uri": "/v1/query/q/final"}),
            {},
        ])
        self.assertEqual(result, (SCHEMA, [[1], [2]]))
        self.assertEqual([call[0].rsplit("/v1/query", 1)[1] for call in calls], ["", "/q/page/0", "/q/page/1", "/q/final"])
        self.assertEqual([self.hint(call) for call in calls], [None, "cla-1", "cla-1", "cla-1"])
        self.assertIn("route_hint=cla-1", err)

    def test_latest_hint_wins(self):
        _, calls, _ = self.execute([
            query.QueryResponse({"id": "q", "data": [], "next_uri": "/v1/query/q/page/0"}, route_hint="cla-1"),
            query.QueryResponse({"schema": SCHEMA, "data": [[1]], "next_uri": "/v1/query/q/page/1"}, route_hint="clb-2"),
            query.QueryResponse({"data": [], "next_uri": ""}),
            {},
        ])
        self.assertEqual([self.hint(call) for call in calls], [None, "cla-1", "clb-2", "clb-2"])

    def test_cleanup_after_cap_and_after_error_carries_hint(self):
        _, calls, _ = self.execute([
            query.QueryResponse({"id": "q", "schema": SCHEMA, "data": [[1], [2]], "next_uri": "/v1/query/q/page/0"},
                                route_hint="cla-1"),
            {}, {},
        ], max_rows=1)
        self.assertEqual([call[0].rsplit("/", 1)[-1] for call in calls], ["query", "kill", "final"])
        self.assertEqual([self.hint(call) for call in calls[1:]], ["cla-1", "cla-1"])

        result, calls, _ = self.execute([
            query.QueryResponse({"id": "q", "data": [], "next_uri": "/v1/query/q/page/0"}, route_hint="cla-1"),
            query.QueryError("HTTP 404: request failed"),
            {}, {},
        ])
        self.assertIsInstance(result, query.QueryError)
        self.assertEqual([call[0].rsplit("/", 1)[-1] for call in calls], ["query", "0", "kill", "final"])
        self.assertEqual([self.hint(call) for call in calls[1:]], ["cla-1", "cla-1", "cla-1"])

    def test_no_hint_means_no_header(self):
        _, calls, err = self.execute([
            {"id": "q", "data": [], "next_uri": "/v1/query/q/page/0"},
            {"schema": SCHEMA, "data": [[1]], "next_uri": "/v1/query/q/final"},
            {},
        ])
        self.assertTrue(all(self.hint(call) is None for call in calls))
        self.assertNotIn("route_hint=", err)

    def test_gateway_local_hint_is_not_used(self):
        _, calls, err = self.execute([query.QueryResponse(
            {"id": "management-query", "schema": SCHEMA, "data": [[1]], "state": "Succeeded"},
            gateway_local=True, route_hint="cla-1")])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("route_hint=", err)


class MainTest(unittest.TestCase):
    def invoke(self, argv, env=None, runner=None):
        with tempfile.TemporaryDirectory() as directory:
            values = {"XDG_CONFIG_HOME": directory, **(env or {})}
            out, err = io.StringIO(), io.StringIO()
            runner = runner or mock.Mock(return_value=(SCHEMA, [[1]]))
            with mock.patch.dict(os.environ, values, clear=True), mock.patch.object(query, "run", runner), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = query.main(argv)
                except SystemExit as error:
                    code = error.code
            return code, out.getvalue(), err.getvalue(), runner

    def test_api_key_and_org(self):
        code, out, err, runner = self.invoke(["--org", "acme", "SELECT 1"], {"DATABEND_CLOUD_API_KEY": CREDS["api_key"]})
        self.assertEqual(code, 0)
        self.assertIn("(1 rows)", out)
        self.assertNotIn(CREDS["api_key"], err)
        self.assertEqual(runner.call_args.args[1]["org"], "acme")

    def test_missing_config_exits_2(self):
        code, _, err, runner = self.invoke(["--org", "acme", "SELECT 1"])
        self.assertEqual(code, 2)
        self.assertIn("set-key", err)
        runner.assert_not_called()

    def test_jsonl_schema_and_exact_values(self):
        code, out, _, _ = self.invoke(["--format", "json", "--org", "acme", "SELECT 1"],
                                     {"DATABEND_CLOUD_API_KEY": CREDS["api_key"]})
        self.assertEqual(code, 0)
        self.assertEqual([json.loads(line) for line in out.splitlines()], [SCHEMA, [1]])

    def test_errors_redact_token(self):
        runner = mock.Mock(side_effect=query.QueryError("rejected " + CREDS["api_key"]))
        code, _, err, _ = self.invoke(["--org", "acme", "SELECT 1"],
                                     {"DATABEND_CLOUD_API_KEY": CREDS["api_key"]}, runner)
        self.assertEqual(code, 1)
        self.assertIn("[REDACTED]", err)
        self.assertNotIn(CREDS["api_key"], err)

    def test_host_dsns_do_not_satisfy_missing_api_key(self):
        for name in ("BENDCLOUD_DSN", "BENDCLOUD_DSN_TEST_ORG"):
            for argv in (["--org", "test-org", "SELECT 1"], ["--org", "test-org-two", "SELECT 1"]):
                with self.subTest(variable=name, argv=argv):
                    code, _, err, runner = self.invoke(argv, {
                        name: "bendcloud://test-org:synthetic-old-key@api.example.invalid/default"})
                    self.assertEqual(code, 2)
                    self.assertIn("set-key", err)
                    self.assertNotIn("synthetic-old-key", err)
                    runner.assert_not_called()

    def test_host_dsns_cannot_supply_organization(self):
        code, _, err, runner = self.invoke(["SELECT 1"], {
            "DATABEND_CLOUD_API_KEY": CREDS["api_key"],
            "BENDCLOUD_DSN": "bendcloud://wrong:fake@api.databend.com/default"})
        self.assertEqual(code, 2)
        self.assertIn("--org", err)
        runner.assert_not_called()

    def test_target_is_not_read_from_host_dsn_variables(self):
        code, _, err, runner = self.invoke(["--target", "test-alias", "SELECT 1"], {
            "BENDCLOUD_DSN_TEST_ALIAS": "bendcloud://test-org:synthetic-unused-key@api.example.invalid/default"})
        self.assertEqual(code, 2)
        self.assertIn("Unknown target", err)
        self.assertNotIn("synthetic-unused-key", err)
        runner.assert_not_called()

    def test_saved_target_supplies_org_origin_and_warehouse(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"XDG_CONFIG_HOME": directory, "DATABEND_CLOUD_API_KEY": CREDS["api_key"]}
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(cloud_config.main(["set-target", "logs", "--org", "org-synthetic",
                                                    "--base-url", "https://api.databend.com", "--warehouse", "analytics"]), 0)
            code, _, err, runner = self.invoke(["--target", "logs", "SELECT 1"], env)
            self.assertEqual(code, 0)
            selected = runner.call_args.args[1]
            self.assertEqual((selected["org"], selected["warehouse"], selected["target"]), ("org-synthetic", "analytics", "logs"))
            self.assertIn("target=logs org=org-synthetic", err)

    def test_api_key_model_ignores_unrelated_host_dsn(self):
        code, _, err, runner = self.invoke(["--org", "acme", "SELECT 1"], {
            "DATABEND_CLOUD_API_KEY": CREDS["api_key"],
            "BENDCLOUD_DSN": "bendcloud://wrong:fake@wrong.example/wrong"})
        self.assertEqual(code, 0)
        selected = runner.call_args.args[1]
        self.assertEqual(selected["org"], "acme")
        self.assertEqual(selected["warehouse"], "default")
        self.assertEqual(selected["base_url"], "https://api.databend.com")
        self.assertNotIn("wrong", err)

    def test_org_required_and_context_is_not_a_cli_option(self):
        for argv in (["SELECT 1"], ["--org", "acme", "--context", "alias", "SELECT 1"]):
            code, _, _, runner = self.invoke(argv)
            self.assertEqual(code, 2)
            runner.assert_not_called()

    def test_table_truncates_and_marks_utc(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            query.render_table([{"name": "event_time", "type": "Timestamp"}], [["x" * 100], [None]])
        self.assertIn("UTC", output.getvalue())
        self.assertIn("...", output.getvalue())
        self.assertIn("NULL", output.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
