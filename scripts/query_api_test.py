#!/usr/bin/env python3
"""Offline lifecycle-only CLI, authorization gate, org, and output tests."""
import contextlib
import io
import os
import unittest
from unittest import mock

import cloud_config
import query
import query_api

CREDS = {"base_url": "https://api.databend.cn", "org": "acme", "warehouse": "analytics", "api_key": "synthetic-lifecycle-key"}


class QueryAPITest(unittest.TestCase):
    def invoke(self, argv, response=None, failure=None, config_error=None, add_org=True):
        if add_org and "--org" not in argv:
            argv = [*argv, "--org", "acme"]
        request = mock.Mock(return_value=response if response is not None else {"id": "q", "state": "Succeeded"}, side_effect=failure)
        resolver = mock.Mock(return_value=CREDS, side_effect=config_error)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(cloud_config, "resolve", resolver), mock.patch.object(query, "request", request), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = query_api.main(argv)
            except SystemExit as error:
                code = error.code
        self.assertNotIn(CREDS["api_key"], out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue(), request, resolver

    def test_status_uses_confirmed_site_and_never_starts_sql(self):
        code, output, _, request, resolver = self.invoke(["status", "q", "--org", "acme"])
        self.assertEqual(code, 0)
        self.assertIn('"state": "Succeeded"', output)
        args = request.call_args.args
        self.assertEqual(args[0], "https://api.databend.cn/v1/query/q")
        self.assertEqual(args[1]["X-DatabendCloud-Token"], CREDS["api_key"])
        self.assertEqual(args[1]["X-DatabendCloud-Org"], "acme")
        self.assertEqual(args[1]["X-DatabendCloud-Warehouse"], "analytics")
        self.assertIsNone(args[2])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(resolver.call_args.args[0].org, "acme")

    def test_page_is_one_explicit_page_without_following_foreign_uri(self):
        code, _, _, request, _ = self.invoke(["page", "q", "--page", "3", "--org", "acme"],
                                             response={"data": [[1]], "next_uri": "https://foreign.example/v1/query/q/page/4"})
        self.assertEqual(code, 0)
        self.assertTrue(request.call_args.args[0].endswith("/q/page/3"))
        self.assertEqual(request.call_count, 1)

    def test_page_options_are_validated_before_reading_credentials(self):
        for argv in (["page", "q"], ["page", "q", "--page", "-1"], ["status", "q", "--page", "0"]):
            code, _, _, request, resolver = self.invoke(argv)
            self.assertEqual(code, 2)
            request.assert_not_called()
            resolver.assert_not_called()

    def test_kill_is_post_with_confirmation(self):
        code, _, _, request, _ = self.invoke(["kill", "q", "--confirm", "--org", "acme"])
        self.assertEqual(code, 0)
        self.assertTrue(request.call_args.args[0].endswith("/q/kill"))
        self.assertEqual(request.call_args.args[2], {})
        self.assertEqual(request.call_count, 1)

    def test_final_is_get_with_confirmation(self):
        code, _, _, request, _ = self.invoke(["final", "q", "--confirm"])
        self.assertEqual(code, 0)
        self.assertTrue(request.call_args.args[0].endswith("/q/final"))
        self.assertIsNone(request.call_args.args[2])
        self.assertEqual(request.call_count, 1)

    def test_side_effects_without_confirmation_do_nothing(self):
        for operation in ("kill", "final"):
            code, _, error, request, resolver = self.invoke([operation, "q"])
            self.assertEqual(code, 2)
            self.assertIn("require --confirm", error)
            request.assert_not_called()
            resolver.assert_not_called()

    def test_path_injection_is_rejected_before_config(self):
        for value in ("../other", "q/kill", "https://foreign.example", "q?secret=1", "q\nheader", "x" * 129):
            code, _, _, request, resolver = self.invoke(["status", value])
            self.assertEqual(code, 2)
            request.assert_not_called()
            resolver.assert_not_called()

    def test_no_arbitrary_api_or_sql_operation(self):
        for operation in ("sql", "warehouses", "/api/v1/my/orgs"):
            code, _, _, request, _ = self.invoke([operation, "q"])
            self.assertEqual(code, 2)
            request.assert_not_called()

    def test_no_context_option_exists(self):
        code, _, _, request, resolver = self.invoke(["status", "q", "--org", "acme", "--context", "alias"], add_org=False)
        self.assertEqual(code, 2)
        request.assert_not_called()
        resolver.assert_not_called()

    def test_missing_org_and_target_is_a_configuration_error(self):
        code, _, error, request, _ = self.invoke(["status", "q"], add_org=False,
                                                 config_error=cloud_config.ConfigError("Organization is required; pass --org"))
        self.assertEqual(code, 2)
        self.assertIn("--org", error)
        request.assert_not_called()

    def test_target_option_is_forwarded_to_resolver(self):
        code, _, _, _, resolver = self.invoke(["status", "q", "--target", "logs"], add_org=False)
        self.assertEqual(code, 0)
        self.assertEqual(resolver.call_args.args[0].target, "logs")

    def test_missing_key_stops_before_network(self):
        code, _, error, request, _ = self.invoke(["status", "q"], config_error=cloud_config.ConfigError("set-key first"))
        self.assertEqual(code, 2)
        self.assertIn("set-key", error)
        request.assert_not_called()

    def test_transport_error_is_not_retried(self):
        code, _, error, request, _ = self.invoke(["kill", "q", "--confirm"], failure=query.QueryError("outcome unknown"))
        self.assertEqual(code, 1)
        self.assertIn("outcome unknown", error)
        self.assertEqual(request.call_count, 1)

    def test_cloud_error_is_reported(self):
        code, _, error, _, _ = self.invoke(["status", "q"], response={"error": {"kind": "Denied", "message": "no"}})
        self.assertEqual(code, 1)
        self.assertIn("Denied", error)

    def test_token_in_response_or_error_is_redacted(self):
        for response in ({"note": CREDS["api_key"]}, {"error": {"message": CREDS["api_key"]}}):
            _, output, error, _, _ = self.invoke(["status", "q"], response=response)
            self.assertIn("[REDACTED]", output + error)

    def test_response_rows_are_bounded_and_reported(self):
        code, output, error, _, _ = self.invoke(["page", "q", "--page", "0", "--max-rows", "1"], response={"data": [[1], [2]]})
        self.assertEqual(code, 0)
        self.assertIn('"data": [[1]]', output)
        self.assertNotIn("[2]", output)
        self.assertIn("not a complete export", error)

    def test_route_hint_option_is_sent_on_every_lifecycle_operation(self):
        for argv in (["status", "q"], ["page", "q", "--page", "1"], ["kill", "q", "--confirm"], ["final", "q", "--confirm"]):
            with self.subTest(argv=argv):
                code, _, _, request, _ = self.invoke([*argv, "--route-hint", "clextra01-a03c"])
                self.assertEqual(code, 0)
                self.assertEqual(request.call_args.args[1][query.ROUTE_HINT_HEADER], "clextra01-a03c")

    def test_no_route_hint_option_sends_no_header(self):
        code, _, _, request, _ = self.invoke(["status", "q"])
        self.assertEqual(code, 0)
        self.assertNotIn(query.ROUTE_HINT_HEADER, request.call_args.args[1])

    def test_unsafe_route_hint_is_rejected_before_config(self):
        for value in ("cl a", "cl\nX-Other: 1", "cl/x", "x" * 257, ""):
            with self.subTest(value=value):
                code, _, _, request, resolver = self.invoke(["status", "q", "--route-hint", value])
                self.assertEqual(code, 2)
                request.assert_not_called()
                resolver.assert_not_called()

    def test_returned_route_hint_goes_to_stderr_not_stdout(self):
        response = query.QueryResponse({"id": "q", "state": "Running"}, route_hint="clb-2")
        code, output, error, _, _ = self.invoke(["status", "q", "--route-hint", "cla-1"], response=response)
        self.assertEqual(code, 0)
        self.assertIn("route_hint=clb-2", error)
        self.assertNotIn("clb-2", output)

    def test_independent_account_file_and_timeout_are_forwarded(self):
        code, _, _, request, resolver = self.invoke(["status", "q", "--credentials-file", "/private/work.json", "--timeout", "7"])
        self.assertEqual(code, 0)
        self.assertEqual(resolver.call_args.args[0].credentials_file, "/private/work.json")
        self.assertEqual(request.call_args.args[3], 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
