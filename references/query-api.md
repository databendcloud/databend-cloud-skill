# Databend Cloud Query API

This is the verified personal-token query surface. It is separate from the tenant SQL gateway and authenticated console APIs. See [API capabilities](api-capabilities.md) for the full authentication boundary and public catalogs. This reference describes the Cloud REST endpoints, not a requirement to use the Python helper.

## Base URL and authentication

API Base URL: `https://api.databend.cn` for China; `https://api.databend.com` for international. Select the site during setup and keep the saved credential's origin. The helper's low-level fallback is international; do not depend on it for China. Both use the paths and headers below. Never retry an authentication failure against the other site.

Every request, including pagination, status, finalization, and cancellation, carries:

```http
Content-Type: application/json
X-DATABENDCLOUD-TOKEN: <personal-api-token>
X-DATABENDCLOUD-ORG: <organization-id>
X-DATABENDCLOUD-WAREHOUSE: <warehouse-name>
```

These are the existing header names. Do not replace them with Bearer authentication without a documented server change. Header names are case-insensitive.

Keep the key outside model context and process arguments. A custom implementation should load it privately from the configured credential source and attach it in code. Do not paste a real authenticated HTTP request into chat.

## Endpoints

| Method | Path | Purpose and side effects |
|--------|------|--------------------------|
| `POST` | `/v1/query` | Start SQL. Can mutate data/schema/permissions according to the account's role. Not safe to blindly retry. |
| `GET` | `/v1/query/{queryID}` | Inspect status. Does not start another SQL execution. |
| `GET` | `/v1/query/{queryID}/page/{pageNum}` | Fetch a result page. Follow server-issued page URIs. |
| `GET` | `/v1/query/{queryID}/final` | Finalize the result lifecycle and release result resources. Do not treat as a substitute for cancel. |
| `POST` | `/v1/query/{queryID}/kill` | Cancel that execution. Requires authorization. |

Use the same account, organization, and warehouse throughout a query lifecycle. Do not enumerate other query IDs or use another organization's headers to recover a failed request.

## Route hint

A warehouse can have more than one cluster, and a query runs on one of them. Each response through the gateway can carry an `X-DATABEND-ROUTE-HINT` response header that names that cluster. Send it back as a request header, with the value from the latest response, on every later request for the same query: status, pages, final, and kill. Without it, the gateway can route the request to another cluster, which does not have the query and returns `Query ID ... not found`. The helpers send no hint on a new `POST /v1/query`, because each invocation is an independent query. A custom client that deliberately continues a `session` should send the latest hint on its next `POST /v1/query`, so that the query runs on the cluster that holds the session state.

```http
GET /v1/query/<query-id>/page/1
X-DATABENDCLOUD-TOKEN: <personal-api-token>
X-DATABENDCLOUD-ORG: <organization-id>
X-DATABENDCLOUD-WAREHOUSE: <warehouse-name>
X-DATABEND-ROUTE-HINT: <latest-route-hint>
```

The hint is HTTP metadata, not part of the JSON body. `query.py` carries it automatically and prints `route_hint=<value>` on stderr when it is first seen or changes. `query_api.py` sends `--route-hint <value>` and prints the hint of its response on stderr. Hints contain only letters, digits, dots, underscores, and hyphens; the helpers ignore or reject other values. Responses can also carry `X-DATABEND-SESSION-ID`; it is used by the browser console and the helpers ignore it.

## Start SQL

The minimum request body is:

```json
{"sql": "SELECT 1"}
```

A representative response looks like this. The exact column type and query ID depend on the server; additional fields can be present:

```json
{
  "id": "query-id",
  "schema": [{"name": "1", "type": "UInt8"}],
  "data": [["1"]],
  "state": "Succeeded",
  "error": null,
  "next_uri": "/v1/query/query-id/final"
}
```

A response can contain no rows while the query is still executing. Schema can arrive on a later page. Do not infer success or completion from the first response's row count.

The reviewed request DTO also accepts these optional fields:

| Field | Type | Use |
|-------|------|-----|
| `query_id` | String | Caller-supplied correlation ID, forwarded as a query-ID header. It does not establish idempotency or authorize blind retries. |
| `session` | Object | Explicit session/settings/transaction state supported by the deployed server. Carry forward returned state only when deliberately managing a session. |
| `string_fields` | Boolean | Request string-formatted result fields where supported. |

Do not assume other native SQL-gateway options pass through this Cloud DTO; unknown fields can be discarded. `query.py` exposes the minimum SQL body, not all these options. For a custom client, a known ID can help inspect an uncertain execution outcome, but it is not an exactly-once guarantee.

[Warehouse management SQL](warehouse-management.md) can also be submitted here with the required admin role. It has a different local result lifecycle described below.

Each helper invocation starts an independent query. Use fully qualified names such as `analytics.events`; do not expect an earlier invocation's `USE` statement to persist.

## Fetch results and finalize

For ordinary query-node executions:

1. Check HTTP status and error fields. Record the query ID as soon as it is available.
2. Collect schema and rows, keeping output bounded.
3. If `next_uri` points to a page, fetch it with the same headers plus the latest [route hint](#route-hint). Continue even after an empty page. A repeated URI can require a brief wait.
4. If `next_uri` points to final, or no continuation remains, finish collection and finalize the known query.
5. If interrupted, timed out, or capped while unfinished, attempt cancellation and then finalization, both with the latest route hint. Report cleanup failures and the query ID.

Do not follow arbitrary continuation URLs. The helper permits only the selected query's `/page/<number>` and `/final` paths on the configured HTTPS origin. It refuses redirects, foreign origins, other query IDs, query strings, and fragments, rather than forwarding the personal token.

Use an overall deadline and bound each request by the remaining time. The helper defaults to `600` seconds and permits up to two additional five-second best-effort cleanup requests. It bounds individual responses at 16 MiB and collected rows at `1000` by default. Truncation is reported on stderr; it is not a complete export. Use SQL filters and limits first.

## Gateway-local management results

The gateway executes management SQL locally and returns `X-Databend-Query-Source: gateway`. The response already contains its completed data/schema, including successful zero-row changes. The helper preserves this HTTP metadata separately from JSON and does not call query-node page/kill/final endpoints for that execution. Do not infer local execution from SQL text or a JSON field. See [Warehouse management](warehouse-management.md).

## Status, pages, release, and cancellation

The lifecycle-only helper never submits SQL. It loads the same private credentials as `query.py` and requires the actual Organization ID on each request:

```bash
python3 scripts/query_api.py status <query-id> --org <Organization-ID> --base-url <confirmed-api-origin> --route-hint <route-hint>
python3 scripts/query_api.py page <query-id> --page 0 --org <Organization-ID> --base-url <confirmed-api-origin> --route-hint <route-hint> --max-rows 100
```

Pass the latest `route_hint=` value that `query.py` or a previous `query_api.py` call printed on stderr. Without it, a query on a multi-cluster warehouse can be reported as not found even though it still exists. If no hint was printed, omit the option.

Use the page number from the server-issued `next_uri`, not guessed pages. The helper performs exactly one request, prints one JSON response object (not schema-first JSON Lines), and never follows a returned URL or auto-finalizes a manually fetched page. Responses are capped at 16 MiB and `data` rows at `1000` by default; truncation is reported on stderr.

Final releases stored results and can make unread pages unavailable. Kill interrupts execution. After explicit user authorization:

```bash
python3 scripts/query_api.py kill <query-id> --org <Organization-ID> --base-url <confirmed-api-origin> --route-hint <route-hint> --confirm
python3 scripts/query_api.py final <query-id> --org <Organization-ID> --base-url <confirmed-api-origin> --route-hint <route-hint> --confirm
```

`--confirm` is a client-side consent gate, not a substitute for server authorization. Do not use this helper to poll or finalize a completed gateway-local management statement. Query status is not an archival history endpoint; use authorized history SQL for old executions. A not-found status does not prove the original query never ran; first check that the request carried the query's latest route hint.

To inspect an already known query, make an authenticated `GET /v1/query/{queryID}` using the original confirmed API origin, org, warehouse, and latest route hint. Report the returned state and error rather than assuming the query failed because a client lost contact.

For an authorized cancellation, make `POST /v1/query/{queryID}/kill` (an empty JSON object is sufficient), then attempt `GET /v1/query/{queryID}/final`. Check both responses. Cancelling another user's or a previously running query needs explicit user authorization; the helper's automatic cleanup only affects the query it just started.

## Errors and retries

The engine can return an `error` object with `code` and/or `kind` and a `message`. Cloud failures can use a different envelope with `code`/`message` or `success: false`. An HTTP failure is not success even if it contains JSON or no recognizable error field. See [Errors](errors.md).

The helper never automatically re-submits SQL, including on provisioning errors. A timeout or broken connection can leave the query executing. Use its query ID to inspect status before deciding what to do. A missing query ID means the execution outcome may be unknown.

Status/page transport recovery must remain bounded and must not restart the SQL. Do not claim an automatic retry is exactly-once without a documented server guarantee.

## Helper output

```bash
python3 scripts/query.py --org <Organization-ID> --base-url <confirmed-api-origin> --format json --max-rows 100 "SELECT * FROM analytics.events LIMIT 100"
```

`json` is schema-first JSON Lines: line 1 is the schema array; every following line is a row array. It is not a single JSON object. Diagnostics, selected origin/org/warehouse, query ID, route hint, and truncation/cleanup warnings go to stderr. `table` truncates long cells for display, so use JSON for exact values. Timestamps are UTC.

The helper is optional. A direct API client must implement the same credential safety, explicit origin/org/warehouse selection, route-hint pass-through, lifecycle, and bounds.
