#!/usr/bin/env python3
"""Bounded SQL query helper. The REST API is documented in references/query-api.md."""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import cloud_config

ConfigError = cloud_config.ConfigError
USER_AGENT = "databend-cloud-skill/2"
QUERY_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
# The gateway names the warehouse cluster that holds a query. Later requests for
# that query must send it back, or they can reach another cluster and miss it.
ROUTE_HINT_HEADER = "X-DATABEND-ROUTE-HINT"
ROUTE_HINT = re.compile(r"^[A-Za-z0-9._-]{1,256}$")


class QueryError(Exception):
    pass


class QueryResponse(dict):
    """Keep trusted HTTP metadata (execution source, route hint) out of the JSON payload."""
    def __init__(self, payload, gateway_local=False, route_hint=None):
        super().__init__(payload)
        self.gateway_local = gateway_local
        self.route_hint = route_hint


def valid_route_hint(value):
    """Return the hint if it is a safe header value, else None."""
    return value if isinstance(value, str) and ROUTE_HINT.fullmatch(value) else None


def with_route_hint(headers, route_hint):
    return {**headers, ROUTE_HINT_HEADER: route_hint} if route_hint else headers


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the personal token to a redirect destination.
        return None


def request(url, headers, payload=None, timeout=60, method=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={**headers, "User-Agent": USER_AGENT},
                                 method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            response_headers = getattr(response, "headers", {})
            gateway_local = response_headers.get("X-Databend-Query-Source") == "gateway"
            route_hint = valid_route_hint(response_headers.get(ROUTE_HINT_HEADER))
    except urllib.error.HTTPError as error:
        with error:
            if 300 <= error.code < 400:
                raise QueryError(f"HTTP {error.code}: redirect refused; verify the configured API origin") from None
            body = error.read(MAX_RESPONSE_BYTES + 1)
            try:
                decoded = json.loads(body) if len(body) <= MAX_RESPONSE_BYTES else {}
                raise_for_error(decoded)
            except (ValueError, UnicodeError):
                pass
            raise QueryError(f"HTTP {error.code}: request failed; check credentials, organization, warehouse, and SQL") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise QueryError("API request failed or timed out. The query may have started; do not blindly resubmit it") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise QueryError("API response exceeds 16 MiB; reduce the query output before retrying")
    try:
        decoded = json.loads(body or b"{}")
    except (ValueError, UnicodeError):
        raise QueryError("API returned a non-JSON response; verify the API origin") from None
    if not isinstance(decoded, dict):
        raise QueryError("API returned an unexpected response shape")
    return QueryResponse(decoded, gateway_local=gateway_local, route_hint=route_hint)


def raise_for_error(payload):
    if not isinstance(payload, dict):
        raise QueryError("API returned an unexpected response shape")
    error = payload.get("error")
    if error:
        if isinstance(error, dict):
            kind = error.get("kind") or f"code={error.get('code')}"
            raise QueryError(f"{kind}: {error.get('message', '')}"[:2000])
        raise QueryError(str(error)[:2000])
    if payload.get("success") is False or payload.get("code") not in (None, 0, "0", "OK"):
        raise QueryError(f"API error: {payload.get('message') or payload.get('code') or 'request rejected'}"[:2000])


def query_uri(root, uri, query_id):
    """Accept only this query's page/final paths on the configured origin."""
    if not isinstance(uri, str) or not query_id or not QUERY_ID.fullmatch(query_id):
        raise QueryError("Invalid query continuation URI")
    resolved = urllib.parse.urlsplit(urllib.parse.urljoin(root + "/", uri))
    origin = urllib.parse.urlsplit(root)
    prefix = f"/v1/query/{query_id}/"
    suffix = resolved.path[len(prefix):] if resolved.path.startswith(prefix) else ""
    if ((resolved.scheme, resolved.netloc) != (origin.scheme, origin.netloc)
            or resolved.username is not None or resolved.query or resolved.fragment
            or not (suffix == "final" or re.fullmatch(r"page/[0-9]+", suffix))):
        raise QueryError("Refused continuation outside the selected query and API origin")
    return urllib.parse.urlunsplit(resolved)


def redact(message, creds):
    token = creds.get("api_key", "")
    if token:
        for value in (token, urllib.parse.quote(token, safe="")):
            message = message.replace(value, "[REDACTED]")
    return message


def run_once(sql, creds, timeout, max_rows=1000):
    root = creds["base_url"]
    headers = {"Content-Type": "application/json", "X-DatabendCloud-Token": creds["api_key"],
               "X-DatabendCloud-Org": creds["org"], "X-DatabendCloud-Warehouse": creds["warehouse"]}
    deadline = time.monotonic() + timeout
    query_id, schema, rows = "", [], []
    completed = False
    gateway_local = False
    route_hint = None

    def remember_route_hint(response):
        # Carry the latest hint so pages, kill, and final reach the cluster that holds the query.
        nonlocal route_hint
        hint = getattr(response, "route_hint", None)
        if hint and hint != route_hint:
            route_hint = hint
            print(f"route_hint={redact(route_hint, creds)}", file=sys.stderr)

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise QueryError(f"Query deadline exceeded; query_id={query_id or 'unknown'}. Verify status before resubmitting")
        return seconds

    try:
        page = request(f"{root}/v1/query", headers, {"sql": sql}, remaining())
        gateway_local = getattr(page, "gateway_local", False)
        candidate = page.get("id", "") if isinstance(page, dict) else ""
        if isinstance(candidate, str) and QUERY_ID.fullmatch(candidate):
            query_id = candidate
            print(f"query_id={redact(query_id, creds)}", file=sys.stderr)
        if not gateway_local:
            remember_route_hint(page)
        raise_for_error(page)
        if not query_id:
            raise QueryError("Query response has no valid query ID; outcome is unknown, do not blindly resubmit")
        while True:
            raise_for_error(page)
            if page.get("state") == "Failed":
                raise QueryError(f"Query failed; query_id={query_id}")
            if page.get("schema"):
                if not isinstance(page["schema"], list) or not all(isinstance(column, dict) for column in page["schema"]):
                    raise QueryError("Invalid query schema")
                schema = page["schema"]
            data = page.get("data", [])
            if not isinstance(data, list) or not all(isinstance(row, list) for row in data):
                raise QueryError("Invalid query data")
            capacity = max_rows - len(rows)
            rows.extend(data[:capacity])
            following = page.get("next_uri")
            if gateway_local and following:
                raise QueryError("Unexpected continuation for a gateway-local management result")
            if not following:
                if page.get("state") in ("Running", "Starting"):
                    raise QueryError(f"Incomplete query response has no continuation; query_id={query_id}")
                completed = True
                break
            next_url = query_uri(root, following, query_id)
            if next_url.endswith("/final"):
                completed = True
                if len(data) > capacity:
                    print(f"WARN: output truncated to {max_rows} rows.", file=sys.stderr)
                break
            if len(rows) >= max_rows:
                print(f"WARN: output capped at {max_rows} rows; remaining results were not fetched.", file=sys.stderr)
                break
            if not data:
                time.sleep(min(0.2, remaining()))
            page = request(next_url, with_route_hint(headers, route_hint), None, remaining())
            remember_route_hint(page)
        if completed and len(data) > capacity and not following:
            print(f"WARN: output truncated to {max_rows} rows.", file=sys.stderr)
        return schema, rows
    finally:
        if query_id and not gateway_local:
            # Gateway-local SQL has no query-node result lifecycle. Do not wake compute for cleanup.
            # Bounded best-effort cleanup; do not hide the original failure or re-submit SQL.
            cleanup_headers = with_route_hint(headers, route_hint)
            if not completed:
                try:
                    reply = request(f"{root}/v1/query/{query_id}/kill", cleanup_headers, {}, 5)
                    raise_for_error(reply)
                except QueryError as error:
                    print(redact(f"WARN: cancellation could not be confirmed; query_id={query_id}: {error}", creds), file=sys.stderr)
            try:
                reply = request(f"{root}/v1/query/{query_id}/final", cleanup_headers, None, 5)
                raise_for_error(reply)
            except QueryError as error:
                print(redact(f"WARN: finalization could not be confirmed; query_id={query_id}: {error}", creds), file=sys.stderr)


def run(sql, creds, timeout, max_rows=1000):
    # A query can have side effects. No automatic SQL re-submission, even on provisioning errors.
    return run_once(sql, creds, timeout, max_rows)


def render_table(schema, rows, limit=60):
    names = [column.get("name", "?") for column in schema]
    if not rows:
        print("(0 rows)")
        return
    if any("time" in column.get("name", "").lower() or "timestamp" in str(column.get("type", "")).lower()
           for column in schema):
        print("(time columns are UTC)")
    text = [["NULL" if value is None else str(value).replace("\n", "\\n").replace("\r", "\\r") for value in row] for row in rows]
    widths = [min(len(name), limit) for name in names]
    for row in text:
        for index, value in enumerate(row[:len(widths)]):
            widths[index] = max(widths[index], min(len(value), limit))
    print(" | ".join(name[:limit].ljust(widths[index]) for index, name in enumerate(names)))
    print("-+-".join("-" * width for width in widths))
    for row in text:
        print(" | ".join((value[:limit - 3] + "..." if len(value) > limit else value).ljust(widths[index])
                         for index, value in enumerate(row[:len(widths)])))
    print(f"({len(rows)} rows)")


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run SQL through the Databend Cloud API")
    parser.add_argument("sql")
    cloud_config.add_options(parser)
    parser.add_argument("--timeout", type=positive_int, default=600, help="Query deadline in seconds, plus up to 10s cleanup")
    parser.add_argument("--max-rows", type=positive_int, default=1000)
    parser.add_argument("--format", choices=["table", "json"], default="table")
    args = parser.parse_args(argv)
    try:
        creds = cloud_config.resolve(args, os.environ)
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 2
    label = f"target={creds['target']} " if creds.get("target") else ""
    print(f"{label}org={creds['org']} warehouse={creds['warehouse']} base_url={creds['base_url']}", file=sys.stderr)
    try:
        schema, rows = run(args.sql, creds, args.timeout, args.max_rows)
    except (QueryError, KeyboardInterrupt) as error:
        print(f"ERROR: {redact(str(error) or 'Query interrupted; verify status before resubmitting', creds)}", file=sys.stderr)
        return 1
    if args.format == "json":
        print(json.dumps(schema, ensure_ascii=False))
        for row in rows:
            print(json.dumps(row, ensure_ascii=False))
    else:
        render_table(schema, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
