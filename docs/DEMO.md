# Demo Script — Redshift MCP Server

**Total estimated time:** 12–15 minutes  
**Audience:** Technical judges / reviewers  
**Goal:** Demonstrate a production-grade MCP server on AWS — Auth0 authentication, tiered access control, rate limiting, TTL caching with stale fallback, sqlglot SQL safety, and structured audit logging — all working end-to-end on real infrastructure.

---

## Architecture in One Sentence

> An MCP server running inside an AWS Lambda container, secured by Auth0 JWT authentication, with DynamoDB-backed rate limiting, an in-process TTL cache, and a sqlglot safety layer — exposed via a Lambda Function URL over HTTPS, no API Gateway needed.

---

## Pre-Demo Setup Checklist

Before recording, confirm everything is ready:

- [ ] Lambda deployed: `curl -s https://YOUR-LAMBDA-URL/healthz` → `{"status":"ok"}`
- [ ] Three Auth0 test users configured (`app_metadata.tier`):
  | User | Tier | Can use |
  |------|------|---------|
  | `free_user` | `free` | `list_schemas`, `list_tables`, `describe_table` |
  | `premium_user` | `premium` | All free tools + `sample_table`, `get_table_size`, `profile_column` |
  | `analyst_user` | `analyst` | All tools including `run_select_query` |
- [ ] MCP URL entered in Claude / Cursor: `https://YOUR-LAMBDA-URL/mcp`
- [ ] CloudWatch Logs Insights tab open in AWS Console
- [ ] Terminal ready for curl commands
- [ ] Bearer tokens pre-fetched for `free_user` and `analyst_user` (for rate-limit demo)

---

## Technical Overview — What to Say at the Start (60 seconds)

Show `assets/architecture.png` and walk through it:

> "What you're looking at is a fully deployed, production-ready MCP server. The MCP client — Claude or Cursor — connects to a Lambda Function URL over HTTPS. Every request carries a signed JWT from Auth0.
>
> Inside Lambda, the request goes through a 7-step pipeline:
> **JWT verification → tier gate → TTL cache check → rate limit check → sqlglot SQL safety → tool execution → async audit log.**
>
> Rate-limit counters live in DynamoDB — shared across all Lambda instances, so limits hold even after a cold start. Redshift credentials are pulled from AWS Secrets Manager on cold start only and cached in memory. Every tool call is logged to CloudWatch as a structured JSON line.
>
> There is no API Gateway, no always-on EC2, no Redis. Just Lambda, DynamoDB, Secrets Manager, CloudWatch Logs — and Redshift as the data warehouse."

---

## Test 1 — Server is Live (Health Check)

**Time:** ~30 seconds  
**Proves:** Lambda is deployed, publicly reachable, and the ASGI stack initialises cleanly.

**Command:**
```bash
curl -s https://YOUR-LAMBDA-URL/healthz | jq
```

**Expected:**
```json
{"status": "ok"}
```

**Say this:**
> "The `/healthz` endpoint confirms Lambda is warm and the server process initialised correctly. The Lambda Function URL has `authorization_type = NONE` — HTTPS is handled by AWS, and our own JWT validation happens inside the application layer, giving us full control over the auth logic."

**Technical note for judges:**
- Lambda is deployed as a **container image** (not a zip), giving us a deterministic dependency environment.
- The function URL uses `invoke_mode = BUFFERED` — the full request body is available before the handler runs, which is required for the MCP streamable-HTTP transport.
- CORS is configured with `allow_origins = ["*"]` so any MCP client can connect.

---

## Test 2 — Happy Path: Full End-to-End Tool Call

**Time:** ~2 minutes  
**Proves:** OAuth 2.1 PKCE flow works, JWT tier claim is read, tools execute against real Redshift.

**Steps:**
1. Open Claude / Cursor → MCP / Connectors settings
2. Enter `https://YOUR-LAMBDA-URL/mcp` → click Connect
3. Auth0 login page appears — log in as `analyst_user`
4. In chat: *"List all schemas in the database"*
5. Then: *"Show me the tables in the [schema] schema"*

**Expected:** Real schema and table names returned from Redshift.

**Say this:**
> "When the client adds our MCP URL, it calls the OAuth metadata endpoint — FastMCP exposes `/.well-known/oauth-authorization-server` automatically. The client then redirects to Auth0 for PKCE authentication. After login, Auth0 issues a JWT with a custom claim `https://redshift-mcp/tier` set from the user's `app_metadata`. Our server verifies the JWT signature using Auth0's JWKS endpoint — the public keys are cached in memory for 1 hour to avoid hitting Auth0 on every request."

**Technical note for judges:**
- JWT verification uses `PyJWKClient` with RS256 — the algorithm is pinned, HS256 is never accepted.
- The tier claim key is configurable via `AUTH0_TIER_CLAIM` environment variable.
- `list_schemas` has a **5-minute TTL cache** — the first call hits Redshift, subsequent calls within 5 minutes are served from memory.

---

## Test 3 — Tier Gating: Free User Gets HTTP 403

**Time:** ~2 minutes  
**Proves:** Access control is enforced at the HTTP layer (403 status), not just with an error message.

**Steps:**
1. Disconnect → reconnect as `free_user`
2. Ask: *"List all schemas"* — succeeds ✅ (Free tier tool)
3. Ask: *"Sample 5 rows from table X in schema Y"* — blocked ❌

**Expected:**
```
Error: This tool is not enabled for your subscription tier.
HTTP 403 Forbidden
```

**Say this:**
> "The Free tier can only call catalog tools: `list_schemas`, `list_tables`, `describe_table`. When they try `sample_table`, they get an HTTP 403 — not just a JSON-RPC error body with a 200 status. We added custom ASGI middleware that intercepts JSON-RPC error code `-32030` and rewrites the HTTP status to 403 before the response leaves Lambda. This is important because well-behaved HTTP clients use status codes, not just response bodies.
>
> Also worth noting: `list_tools` is filtered by tier too — Premium and Analyst tools are completely invisible to Free users. They can't even see the tool exists."

**CloudWatch query to show the blocked call:**
```
fields @timestamp, user_id, tier, tool, ok, error
| filter event = "mcp_tool_call" and ok = false
| sort @timestamp desc
| limit 5
```
> Shows: `"error": "insufficient_tier"`, `"tier": "free"`, `"tool": "sample_table"`

**Technical note for judges:**
- `TOOL_MIN_TIER` map in `tiers.py` defines the minimum tier per tool — a single source of truth.
- The check happens in `call_tool` BEFORE any DB connection is made — no wasted resources.
- Audit event is still emitted for blocked calls, enabling security monitoring.

---

## Test 4 — Tier Upgrade: Same Tool Works for Premium

**Time:** ~1 minute  
**Proves:** Tier is purely JWT-driven — no server-side config change needed to upgrade a user.

**Steps:**
1. Disconnect → reconnect as `premium_user`
2. Ask: *"Sample 5 rows from table X in schema Y"* — now works ✅
3. Ask: *"What is the size of table X?"* — works ✅
4. Ask: *"Profile the column Y in table X"* — works ✅

**Expected:** Sample rows, size/row count, and column statistics returned.

**Say this:**
> "Same server, same code, same URL. No deployment, no config change. The only thing that changed is the tier claim in the JWT. The server reads the claim on every request — there is no session, no server-side user state. This is stateless tier enforcement via signed tokens."

**Technical note for judges:**
- Premium unlocks: `sample_table` (60-sec TTL cache), `get_table_size` (120-sec TTL), `profile_column` (300-sec TTL).
- The TTL is tuned per tool: catalog metadata caches longer, data samples expire faster.

---

## Test 5 — Analyst SQL: Raw Query Through Safety Layer

**Time:** ~1.5 minutes  
**Proves:** Analyst tier can run raw SQL, but sqlglot blocks dangerous operations at the AST level.

**Steps:**
1. Reconnect as `analyst_user`
2. Ask: *"Run this query: `SELECT count(*), MAX(created_at) FROM schema.orders`"*
3. Then try: *"Run: DROP TABLE schema.orders"* — should be blocked

**Expected:**
- First query: row count and max date returned. Response includes the rewritten SQL with injected LIMIT.
- DROP TABLE: blocked with `UnsafeQueryError`

**Say this:**
> "The `run_select_query` tool accepts arbitrary SQL, but every query goes through a sqlglot safety layer before execution. sqlglot parses the SQL into an AST — it blocks DML and DDL (INSERT, UPDATE, DELETE, DROP, CREATE, TRUNCATE), dangerous functions, and subquery CTEs that try to sneak in mutations. It also automatically injects `LIMIT 1000` if no LIMIT is present, and validates that all referenced schemas are in the configured allow-list. The LLM gets powerful SQL execution but the warehouse is protected."

**CloudWatch query to verify the safe execution:**
```
fields @timestamp, tool, ok, duration_ms, cache_hit
| filter event = "mcp_tool_call" and tool = "run_select_query"
| sort @timestamp desc
| limit 5
```

**Technical note for judges:**
- sqlglot operates at the AST level — it catches obfuscated mutations (e.g. `WITH x AS (DELETE ...) SELECT ...`) that regex-based filters miss.
- Queries use parameterized execution with `statement_timeout` set per query to prevent long-running warehouse locks.
- `run_select_query` has a 60-second TTL cache — identical queries within 60 seconds return from memory.

---

## Test 6 — Caching: Second Call Returns Instantly

**Time:** ~2 minutes  
**Proves:** In-process TTL cache eliminates repeated warehouse queries. Audit logs confirm cache hits.

**Steps:**
1. As `analyst_user`, ask: *"List all schemas"* — note response time (~200ms)
2. Immediately ask the exact same question again — near-instant response
3. Open CloudWatch Logs Insights and run:

```
fields @timestamp, tool, cache_hit, stale_fallback, duration_ms, user_id
| filter event = "mcp_tool_call" and tool = "list_schemas"
| sort @timestamp desc
| limit 5
```

**Expected log rows:**
```
First call:   cache_hit=false,  duration_ms=~180,  ok=true
Second call:  cache_hit=true,   duration_ms=~0,    ok=true
```

**Say this:**
> "The cache uses a SHA-256 key over the tool name and serialised arguments — so the same tool with different arguments gets different cache entries. `list_schemas` has a 5-minute TTL. On the second call, the response comes from memory in under a millisecond and Redshift is never touched.
>
> The audit log is the proof: `cache_hit: true`, `duration_ms` is essentially zero. Every field you'd need for billing, debugging, or abuse monitoring is right there — user ID, tier, tool name, ISO timestamp, cache hit status, whether it fell back to stale data, and duration."

**TTL reference table:**
| Tool | Cache TTL |
|------|-----------|
| `list_schemas` | 5 min |
| `describe_table` | 5 min |
| `profile_column` | 5 min |
| `list_tables` | 2 min |
| `get_table_size` | 2 min |
| `sample_table` | 1 min |
| `run_select_query` | 1 min |

**Technical note for judges:**
- The cache is in-process (Lambda memory) — no Redis, no Elasticache, no extra cost.
- Cache entries are kept even after TTL expiry as "stale" backups for Test 8.
- Cache key uses `json.dumps(args, sort_keys=True)` for canonical argument serialisation.

---

## Test 7 — Rate Limiting: HTTP 429 + Retry-After

**Time:** ~2 minutes  
**Proves:** Per-user rate limits enforced via DynamoDB. Exceeding the limit returns 429 with a proper Retry-After header.

**Steps:**
1. Get a bearer token for `free_user` from Auth0
2. Run this loop from a terminal:

```bash
FREE_TOKEN="YOUR_FREE_USER_JWT"
URL="https://YOUR-LAMBDA-URL/mcp"
BODY='{"jsonrpc":"2.0","method":"tools/call","params":{"name":"list_schemas","arguments":{}},"id":1}'

for i in $(seq 1 32); do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "$URL" \
    -H "Authorization: Bearer $FREE_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$BODY")
  echo "Call $i: HTTP $STATUS"
done
```

3. On call 31, show the full response including headers:
```bash
curl -v -X POST "$URL" \
  -H "Authorization: Bearer $FREE_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$BODY" 2>&1 | grep -E "HTTP|Retry-After|rate"
```

**Expected:**
```
Call 1–30:  HTTP 200
Call 31:    HTTP 429
Retry-After: <seconds until next UTC hour>
```

**Say this:**
> "Rate limits are stored in DynamoDB — not Lambda memory. This means they survive Lambda restarts, cold starts, and scale-out to multiple concurrent Lambda instances. The counter is a fixed UTC-hour window: `pk = user_id`, `sk = 2026-05-19T12` (the current UTC hour). Each call does an atomic `UpdateItem` with a `ConditionalExpression` — if the count would exceed the tier limit, the condition fails and we return 429.
>
> The ASGI middleware converts the JSON-RPC error code `-32029` to an HTTP 429 and adds a `Retry-After` header pointing to the exact second the next hour starts. Free users get 30 calls/hour, Premium 150, Analyst 500."

**Rate limit reference:**
| Tier | Calls / hour |
|------|-------------|
| Free | 30 |
| Premium | 150 |
| Analyst | 500 |

**Technical note for judges:**
- DynamoDB uses `PAY_PER_REQUEST` billing — cost scales with actual usage, no idle cost.
- On DynamoDB errors (network issues), the limiter **fails open** — the call proceeds. This prevents DynamoDB availability from taking down the MCP service.
- The middleware layer (not the tool handler) sets the HTTP status — so even if the MCP client doesn't parse JSON-RPC error codes, it sees the correct HTTP error.

---

## Test 8 — Stale Cache Fallback: Graceful Degradation

**Time:** ~1 minute (optional — use if time allows)  
**Proves:** If Redshift becomes unreachable, the server returns stale cached data instead of crashing.

**Steps:**
1. Call `list_schemas` successfully (populates both fresh and stale cache)
2. Temporarily break the Redshift connection (change `REDSHIFT_HOST` to `invalid-host` and redeploy, or revoke network access)
3. Call `list_schemas` again

**Expected:**
- Response: previous cached schema list (no error)
- CloudWatch log: `stale_fallback: true`, `ok: true`

**Say this:**
> "Every successful tool result is written into two stores: the fresh TTL cache and a permanent stale backup. When the tool call fails because Redshift is down, we check the stale store and serve the last known good result. The client gets data, the server stays healthy, and the audit log marks `stale_fallback: true` so ops teams can see the degradation without an outage alert."

**CloudWatch query:**
```
fields @timestamp, tool, cache_hit, stale_fallback, ok, user_id
| filter event = "mcp_tool_call" and stale_fallback = true
| sort @timestamp desc
```

---

## Key Talking Points — Technical Summary

| Topic | Key Technical Point |
|-------|---------------------|
| **Lambda container** | Deployed as a Docker image on `public.ecr.aws/lambda/python:3.12`. Handler: `redshift_mcp.lambda_handler.handler`. Provisioned via Terraform. |
| **Lambda Function URL** | `authorization_type = NONE`, `invoke_mode = BUFFERED`, CORS enabled — no API Gateway needed. |
| **Auth0 / JWT** | OAuth 2.1 PKCE. RS256-signed JWTs. Tier claim read from `app_metadata` via Auth0 Rules/Actions. JWKS keys cached 1 hour in Lambda memory. |
| **Tier gating** | `TOOL_MIN_TIER` dict maps each of the 7 tools to a minimum tier. Enforced in both `list_tools` (visibility) and `call_tool` (execution). HTTP 403 via ASGI middleware. |
| **Rate limiting** | DynamoDB `UpdateItem` with `ConditionalExpression`. Fixed UTC-hour window. Fails open on DynamoDB errors. HTTP 429 + `Retry-After` via ASGI middleware. |
| **TTL cache** | SHA-256 keyed, in-process. TTLs: 5 min (catalog), 2 min (table meta), 1 min (data). Fresh + stale dual store. No Redis required. |
| **sqlglot safety** | AST-level validation: SELECT/UNION only, blocks DML/DDL/dangerous functions, injects LIMIT, validates schema allow-list. Catches obfuscated mutations. |
| **Audit logging** | Non-blocking queue + daemon thread. JSON lines to stdout → CloudWatch. Fields: `ts` (ISO 8601), `user_id`, `tier`, `tool`, `cache_hit`, `stale_fallback`, `duration_ms`, `ok`, `error`. |
| **Secrets Manager** | Redshift password fetched once at cold start via `GetSecretValue`. Cached in Lambda environment for the lifetime of the container. |
| **IaC** | Full Terraform — ECR repo, Lambda function, Function URL, DynamoDB table, CloudWatch log group, IAM role + policies. `terraform apply` is the full deployment. |
| **Cost model** | Lambda: pay per invocation + duration. DynamoDB: pay per request. CloudWatch: pay per GB ingested. Zero idle cost. |
