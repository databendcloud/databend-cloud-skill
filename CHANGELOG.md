# Changelog

Versions here identify the skill, not a Databend Cloud server release. Dates are UTC.

## Unreleased — standalone repository

- Derived from `evotai/evot-skills`, `skills/databend-cloud`, at `ad14e1fde0570e12354d92f61960e076f3186714`, under Apache-2.0. Retain the existing 1.1.0 version history; this is not a published release.
- Add a standalone README, license copy, ignore rules, and a minimal offline-test workflow. Keep agent-guided installation and private API key setup; no new installer or runtime dependencies.
- Remove evot-specific metadata from `SKILL.md` and adapt `scripts/layout_test.py` to portable dependencies and a standalone directory layout.
- Add API token IP allowlist guidance to `references/authentication.md` and `references/errors.md`. No runtime scripts or credential formats change.
- Pass the `X-DATABEND-ROUTE-HINT` response header back on later requests for the same query. On a multi-cluster warehouse, a page, status, kill, or final request without it can reach another cluster and fail with `Query ID ... not found`. `query.py` carries the latest hint automatically and prints `route_hint=` on stderr; `query_api.py` adds `--route-hint` and prints the hint of its response on stderr. Unsafe hint values are ignored or rejected. Documented in `references/query-api.md`.

## 1.1.0 — 2026-10-28

- Add a local named-target registry (`cloud_config.py set-target / list-targets / remove-target`, `~/.config/databend-cloud/targets.json`). A target stores origin, Organization ID, warehouse, and a routing note; it never stores a key.
- `query.py`, `query_api.py`, and `cloud_config.py show` accept `--target <name>` as an alternative to `--org/--base-url`. Explicit flags that disagree with the target are errors; `--warehouse` may override per call.
- Agents must run `list-targets` and match the subject before asking for an Organization ID, and should offer `set-target` after a user supplies one. There is still no default or active target; a missing `--org/--target` error lists saved names.

## 1.0.1 — 2026-10-23

- Update token creation guidance to the standalone **Account API Tokens** sidebar entry.
- Use **账户 API 令牌 → 访问令牌 → 生成新令牌** for the China console.

## 1.0.0 — 2026-10-19

First explicitly versioned skill revision. Local changes are not a published repository release until committed and released.

- Use the actual Organization ID via required `--org` on every query/API request. No alias registry, saved org configuration, or compatibility commands.
- Keep account credentials separate by API origin; both Cloud sites can coexist. Warehouse defaults to `default` unless explicitly selected.
- Preserve masked terminal input, safe same-origin key updates, bounded query lifecycle helpers, personal-token API references, and SQL documentation discovery.
- Use one short entrypoint; load incident workflows, log schema, memory analysis, recipes, and exports from references only when needed.
- Keep tests offline and fixtures synthetic; do not include production account or organization identifiers.
