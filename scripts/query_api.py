#!/usr/bin/env python3
"""Call only the personal-token query lifecycle routes, without submitting SQL."""
import argparse
import json
import os
import sys

import cloud_config
import query


def query_id(value):
    if not query.QUERY_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("query ID must use letters, digits, underscores, or hyphens (maximum 128 characters)")
    return value


def page_number(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("page number must be nonnegative")
    return number


def route_hint(value):
    if not query.valid_route_hint(value):
        raise argparse.ArgumentTypeError("route hint must use letters, digits, dots, underscores, or hyphens (maximum 256 characters)")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect or manage one known Cloud query; no SQL submission")
    parser.add_argument("operation", choices=("status", "page", "final", "kill"))
    parser.add_argument("query_id", type=query_id)
    parser.add_argument("--page", type=page_number, help="Page number from a server-issued next_uri")
    parser.add_argument("--route-hint", type=route_hint,
                        help="Latest X-DATABEND-ROUTE-HINT value of this query; routes the request to the cluster that holds it")
    parser.add_argument("--confirm", action="store_true", help="Confirm authorized cancellation or result release")
    parser.add_argument("--timeout", type=query.positive_int, default=30)
    parser.add_argument("--max-rows", type=query.positive_int, default=1000)
    cloud_config.add_options(parser)
    args = parser.parse_args(argv)
    if args.operation == "page" and args.page is None:
        parser.error("page requires --page <number> from the query's next_uri")
    if args.operation != "page" and args.page is not None:
        parser.error("--page is only valid for page")
    if args.operation in ("kill", "final") and not args.confirm:
        parser.error("kill/final require --confirm after user authorization; final releases stored results")
    try:
        creds = cloud_config.resolve(args, os.environ)
    except cloud_config.ConfigError as error:
        print(error, file=sys.stderr)
        return 2
    root = creds["base_url"]
    path = f"/v1/query/{args.query_id}"
    if args.operation == "page":
        path += f"/page/{args.page}"
    elif args.operation != "status":
        path += f"/{args.operation}"
    headers = {"Content-Type": "application/json", "X-DatabendCloud-Token": creds["api_key"],
               "X-DatabendCloud-Org": creds["org"], "X-DatabendCloud-Warehouse": creds["warehouse"]}
    headers = query.with_route_hint(headers, args.route_hint)
    selected = f"org={creds['org']} warehouse={creds['warehouse']} base_url={root} query_id={args.query_id}"
    print(query.redact(selected, creds), file=sys.stderr)
    try:
        response = query.request(root + path, headers, {} if args.operation == "kill" else None, args.timeout)
        returned_hint = getattr(response, "route_hint", None)
        if returned_hint:
            # Metadata for the next lifecycle call; stdout stays the JSON body only.
            print(f"route_hint={query.redact(returned_hint, creds)}", file=sys.stderr)
        query.raise_for_error(response)
        data = response.get("data")
        if isinstance(data, list) and len(data) > args.max_rows:
            response = {**response, "data": data[:args.max_rows]}
            print(f"WARN: response data truncated to {args.max_rows} rows; this is not a complete export.", file=sys.stderr)
        print(query.redact(json.dumps(response, ensure_ascii=False), creds))
        return 0
    except (query.QueryError, KeyboardInterrupt) as error:
        message = str(error) or "Request interrupted; verify status before repeating a side effect"
        print("ERROR: " + query.redact(message, creds), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
