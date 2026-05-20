# Architecture — Redshift MCP

AWS architecture for the **redshift-mcp** deployment — a read-only Model Context Protocol (MCP) server for Amazon Redshift.

This document covers design decisions, AWS component responsibilities, runtime flows, trade-offs, and cost notes. Deployment commands and Terraform variables live in [README.md](../README.md); client setup (Claude Desktop, Cursor, Auth0) is in the same README.

## Architecture Diagram

![Redshift MCP AWS architecture](../assets/architecture.png)

Diagram source:

| File | Use |
|------|-----|
| [assets/architecture.mermaid](../assets/architecture.mermaid) | Mermaid source — paste into [mermaid.live](https://mermaid.live) or draw.io **Insert → Mermaid** |

```mermaid
flowchart LR
  subgraph external["External"]
    C["Claude / Cursor\nMCP Client"]
    A0["Auth0\nOIDC · JWT Issuer\nTier via app_metadata"]
  end

  subgraph aws["AWS Account"]
    URL["Lambda Function URL\nHTTPS · /mcp · /healthz\nBUFFERED invoke mode"]

    subgraph lambda["Lambda Container · Python 3.12 · Mangum + FastMCP"]
      LOGIC["① JWT Verify → ② Tier Gate → ③ Rate Limit\n→ ④ TTL Cache → ⑤ sqlglot Safety\n→ ⑥ Tool Handlers → ⑦ Audit Log"]
    end

    ECR["ECR\nContainer Registry"]
    DDB[("DynamoDB\nRate Counters\npk=user · sk=UTC hour")]
    SM["Secrets Manager\nDB Password ARN"]
    CW["CloudWatch Logs\nAudit + Platform\n14-day retention"]
    RS[("Amazon Redshift\nRead-Only · port 5439")]
  end

  C -->|"HTTPS · Bearer JWT"| URL
  URL --> lambda
  C <-->|"OAuth PKCE\nAuth Code + Access Token"| A0
  lambda -.->|"JWKS · RS256 verify"| A0
  lambda -->|"UpdateItem\nhourly counter"| DDB
  lambda -.->|"GetSecretValue\n(password auth)"| SM
  lambda -->|"Audit JSON lines"| CW
  lambda -->|"Validated SELECT\nparameterized SQL"| RS
  ECR -.->|"container image"| lambda
  URL -->|"HTTP 403 Tier Forbidden\nHTTP 429 Rate Limited + Retry-After"| C
```

## AWS Components

| Component | Responsibility |
|-----------|----------------|
| **Lambda Function URL** | Public HTTPS entry for `/mcp` (streamable HTTP MCP) and `/healthz`. `authorization_type = NONE`; Auth0 bearer validation runs in-app. `invoke_mode = BUFFERED` for JSON MCP responses. |
| **Lambda (container image)** | Runs FastMCP (`streamable-http`), Mangum ASGI adapter, Auth0 JWT verification, tier gating, per-user hourly rate limits, in-process tool result cache, sqlglot SQL safety layer, structured audit logging, and all Redshift tool handlers. |
| **ECR** | Stores the Lambda container image (`public.ecr.aws/lambda/python:3.12` base, `redshift_mcp.lambda_handler.handler` entry point). |
| **DynamoDB `rate_limits`** | **Rate limits only** (not tool cache): `pk = user_id`, `sk = UTC hour bucket`, atomic `UpdateItem` with `ConditionExpression`. Regional service outside the VPC. When Lambda uses `vpc_config`, add a **DynamoDB gateway VPC endpoint** (or NAT) so counters remain reachable. Falls back to in-process limiter locally. |
| **Tool result cache** | **In-process memory** inside the Lambda container (`ToolResultCache` in `tool_cache.py`). Per warm instance — not shared across concurrent invocations. Stale entries are served on upstream errors. |
| **Secrets Manager** | Holds the Redshift database password when not using IAM database authentication (`REDSHIFT_PASSWORD_SECRET_ARN`; read at cold start). |
| **CloudWatch Logs** | Lambda platform logs plus JSON **tool-call audit** lines on stdout (`redshift_mcp_audit_sink`). SQL detail also goes to `redshift_mcp.log` (`/tmp` on Lambda). |
| **Amazon Redshift** | Target warehouse: catalog introspection and validated `SELECT` queries only. All writes are blocked at the application layer. |
| **VPC (optional)** | When Redshift is VPC-only: Lambda subnets + security group; Redshift SG must allow inbound from the Lambda SG on port 5439. |
| **Auth0** | OAuth 2.1 / OIDC for remote MCP clients. API **identifier** must equal `MCP_PUBLIC_URL` / `AUTH0_AUDIENCE`. Tier from custom claim `https://redshift-mcp/tier` (via Post-Login Action → `user.app_metadata.tier`). |

All Terraform-managed AWS resources inherit tags from `local.common_tags`:

```
Name    = ${project_name}-stack  (per-resource Name overrides applied)
Creator = <var.creator_tag>
Purpose = <var.purpose_tag>
```

## Runtime Flows

### Remote MCP Client Tool Call (Claude / Cursor)

```mermaid
sequenceDiagram
  autonumber
  participant C as Claude / Cursor
  participant URL as Lambda Function URL
  participant FN as Lambda FastMCP
  participant A0 as Auth0
  participant DDB as DynamoDB
  participant RS as Redshift

  C->>URL: Discover OAuth metadata (protected resource)
  URL->>FN: Mangum invoke
  FN-->>C: authorization_servers, resource URL
  C->>A0: OAuth authorization code + PKCE (third-party app)
  A0-->>C: access token (tier in custom claim)
  C->>URL: POST /mcp  Authorization: Bearer token
  URL->>FN: Mangum invoke
  FN->>A0: JWKS fetch and cache (RS256)
  FN->>FN: Validate JWT and map tier to scopes
  FN->>FN: Filter tools by tier
  FN->>DDB: Increment hourly counter
  alt rate limit exceeded
    FN-->>C: HTTP 429 + Retry-After header
  end
  alt tier insufficient
    FN-->>C: HTTP 403 Tier Forbidden
  end
  FN->>FN: Read in-process tool cache
  alt cache miss
    FN->>RS: Parameterized catalog SQL or validated SELECT (sqlglot)
    FN->>FN: Store fresh + stale cache entry
  end
  FN-->>C: MCP tool result (JSON response mode)
  FN->>FN: Emit audit JSON to CloudWatch via stdout
```

### Lambda Internal Pipeline — 7 Steps per Tool Call

Every tool call that passes the Function URL travels through seven sequential gates inside the Lambda container:

```mermaid
flowchart TD
    A["📥 Incoming Request\nPOST /mcp\nAuthorization: Bearer JWT"] --> S1

    S1["① JWT Verify\nauth0_jwt.py\n─────────────────\n• Extract kid from JWT header\n• Fetch Auth0 JWKS public key (cached 1 hr)\n• Verify RS256 signature, expiry, audience\n• Extract sub → unique user ID\n• Extract tier claim → free / premium / analyst"]
    S1 -->|"❌ Invalid token"| E1["HTTP 401 Unauthorized\nRequest stops"]
    S1 -->|"✅ sub + tier known"| S2

    S2["② Tier Gate\ntiers.py\n─────────────────\n• Look up TOOL_MIN_TIER for this tool\n• Compare TIER_RANK[user_tier] ≥ TIER_RANK[required]\n• free=0  premium=1  analyst=2"]
    S2 -->|"❌ Tier too low"| E2["HTTP 403 Tier Forbidden\nRequest stops"]
    S2 -->|"✅ Tier sufficient"| S3

    S3["③ Rate Limit\nratelimit.py\n─────────────────\n• key = (sub, UTC-hour-bucket e.g. 2026-05-20T07)\n• DynamoDB UpdateItem: call_count += 1\n• ConditionExpression: call_count < tier_limit\n• free=30  premium=150  analyst=500 per hour"]
    S3 -->|"❌ Limit exceeded"| E3["HTTP 429 + Retry-After\n(seconds to next UTC hour)\nRequest stops"]
    S3 -->|"✅ Under limit"| S4

    S4["④ TTL Cache\ntool_cache.py\n─────────────────\n• cache_key = SHA-256(tool_name + sorted args)\n• Check in-process fresh store\n• TTLs: catalog=5 min  tables=2 min  data=1 min"]
    S4 -->|"✅ Cache HIT"| R["Return cached result\naudit: cache_hit=true"]
    S4 -->|"❌ Cache MISS"| S5

    S5["⑤ sqlglot Safety\nsafety.py  ← run_select_query only\n─────────────────\n• Parse SQL into AST (Redshift dialect)\n• Allow only SELECT / UNION at root\n• Walk entire AST: block INSERT/UPDATE/DELETE/DROP/etc\n• Block dangerous functions: pg_terminate_backend etc\n• Inject or clamp LIMIT ≤ MAX_ROWS_RETURNED"]
    S5 -->|"❌ Unsafe SQL"| E5["HTTP 400 Unsafe Query\nRequest stops"]
    S5 -->|"✅ Safe SQL"| S6

    S6["⑥ Redshift\ndb.py\n─────────────────\n• Execute parameterised SQL\n• On success: store in fresh + stale cache\n• On error: check stale cache\n  → serve stale if available (audit: stale_fallback=true)\n  → error if no stale entry"]
    S6 --> S7

    S7["⑦ Audit Log\naudit.py\n─────────────────\n• Non-blocking background queue (daemon thread)\n• JSON line to stdout → CloudWatch Logs\n• Fields: ts ISO-8601  user_id  tier  tool\n  cache_hit  stale_fallback  duration_ms  ok  error"]
    S7 --> R2["✅ MCP tool result returned to client"]

    style E1 fill:#fee2e2,stroke:#dc2626,color:#7f1d1d
    style E2 fill:#fee2e2,stroke:#dc2626,color:#7f1d1d
    style E3 fill:#fef3c7,stroke:#d97706,color:#78350f
    style E5 fill:#fee2e2,stroke:#dc2626,color:#7f1d1d
    style R  fill:#dcfce7,stroke:#16a34a,color:#14532d
    style R2 fill:#dcfce7,stroke:#16a34a,color:#14532d
    style S1 fill:#eff6ff,stroke:#3b82f6,color:#1e3a5f
    style S2 fill:#eff6ff,stroke:#3b82f6,color:#1e3a5f
    style S3 fill:#eff6ff,stroke:#3b82f6,color:#1e3a5f
    style S4 fill:#eff6ff,stroke:#3b82f6,color:#1e3a5f
    style S5 fill:#eff6ff,stroke:#3b82f6,color:#1e3a5f
    style S6 fill:#eff6ff,stroke:#3b82f6,color:#1e3a5f
    style S7 fill:#f5f3ff,stroke:#7c3aed,color:#2e1065
```

| Step | File | Key identifier used |
|------|------|---------------------|
| ① JWT Verify | `auth0_jwt.py` | `kid` → JWKS key; `sub` → user identity; tier claim → access level |
| ② Tier Gate | `tiers.py` | `TOOL_MIN_TIER[tool_name]` vs `TIER_RANK[user_tier]` |
| ③ Rate Limit | `ratelimit.py` | DynamoDB `pk=sub, sk=UTC-hour`; limit from `tier_hourly_limit(tier)` |
| ④ TTL Cache | `tool_cache.py` | `SHA-256(tool_name + sorted_args)`; separate fresh + stale stores |
| ⑤ sqlglot Safety | `safety.py` | AST node types; `BLOCKED_DESCENDANT_TYPES`; `BLOCKED_FUNCTION_NAMES` |
| ⑥ Redshift | `db.py` | Parameterised SQL only; stale fallback on upstream errors |
| ⑦ Audit Log | `audit.py` | Background daemon thread; JSON → stdout → CloudWatch |

### Local MCP (stdio)

```mermaid
sequenceDiagram
  autonumber
  participant C as Claude Desktop / Cursor
  participant P as redshift_mcp process
  participant RS as Redshift

  C->>P: stdio MCP — no HTTP or JWT
  Note over P: local dev tier defaults to analyst (MCP_LOCAL_DEV_TIER)
  P->>P: sqlglot validate_query
  P->>RS: read-only parameterized queries
  P-->>C: tool result
  P->>P: SQL audit to redshift_mcp.log in project cwd
```

Local mode does **not** use Auth0, DynamoDB rate limits, or the function URL. Recommended for developers with direct network access to Redshift.

## Application Boundary (Lambda / HTTP)

| Route | Purpose |
|-------|---------|
| `/healthz` | Liveness probe — `{"status":"ok"}` |
| `/mcp` | Streamable HTTP MCP (JSON responses when `MCP_JSON_RESPONSE=true`) |
| `/.well-known/oauth-protected-resource` | OAuth protected-resource metadata (when Auth0 is configured) |

Clients never receive Redshift credentials or Secrets Manager ARNs — only the public MCP URL is needed.

### Tool Tiers and Limits

| Tier | Tools Available | Hourly Limit (per JWT `sub`, UTC hour) |
|------|-----------------|----------------------------------------|
| **free** | `list_schemas`, `list_tables`, `describe_table` | 30 calls |
| **premium** | + `sample_table`, `get_table_size`, `profile_column` | 150 calls |
| **analyst** | + `run_select_query` (raw SQL after `validate_query`) | 500 calls |

Rate-limit denials return JSON-RPC `-32029`, mapped to **HTTP 429** with `Retry-After` by `RateLimitHttp429Middleware`. Tier denials return JSON-RPC `-32030`, mapped to **HTTP 403**.

### Safety and Data Access

| Layer | Behavior |
|-------|----------|
| **sqlglot** (`safety.py`, dialect `redshift`) | Single-statement `SELECT`/`UNION` only; blocks DML/DDL, dangerous functions, schema violations. |
| **Identifier validation** | Catalog tools validate and quote schema/table/column names; no string concatenation from user input. |
| **Row / time caps** | `MAX_ROWS_RETURNED` (default 10 000), `QUERY_TIMEOUT_SECONDS` (default 60); optional `ALLOWED_SCHEMAS` / `BLOCKED_SCHEMAS`. |
| **Database principal** | Process holds full connection credentials; use a dedicated **read-only** Redshift user in production. |

## Deployment Modes

| Mode | Hosting | Auth | Rate Limit | Cache |
|------|---------|------|------------|-------|
| **Local stdio** | Developer machine (`uv run`) | None (`MCP_LOCAL_DEV_TIER`) | In-process | In-process |
| **Local HTTP** | Docker or `streamable-http` on laptop | Optional Auth0 | In-process or DynamoDB | In-process |
| **AWS (Terraform)** | Lambda container + function URL | Auth0 (required for remote) | DynamoDB (Terraform-provisioned table) | In-process per warm container |

## Trade-offs

| Choice | Why it was chosen | Cost |
|--------|-------------------|------|
| **Lambda Function URL** instead of API Gateway | Auth and tier checks live in FastMCP; fewer billable parts; same Mangum HTTP v2 event shape. | No WAF/usage plans at the edge; CORS on function URLs is limited (no `OPTIONS` in `allow_methods`). |
| **Container Lambda** instead of ZIP | Heavy deps (`sqlglot`, DB drivers) fit a container image; matches AWS Lambda Python 3.12 base. | Cold start latency + ECR storage; image rebuild/push on every release. |
| **In-process tool cache** instead of shared cache | Catalog/sample results are per-container; stale fallback on Redshift errors avoids extra infra. | Cache not shared across Lambda instances; cold containers miss warm cache. |
| **DynamoDB for rate limits only** | Shared counters across concurrent invocations; on-demand billing; atomic `UpdateItem`. | Slightly higher latency than memory; fails **open** on DynamoDB errors. |
| **Auth0** instead of self-hosted IdP | Managed OAuth 2.1 for Claude/Cursor third-party MCP flows; tier via `app_metadata`. | SaaS dependency; tenant must enable Resource Parameter Compatibility Profile. |
| **Buffered Function URL + JSON MCP** | Compatible with Mangum and Lambda synchronous invoke; avoids SSE/streaming payload limits. | Not true streaming MCP; large payloads must fit Lambda response size limits. |
| **Read-only MCP only** | Scope: exploration and validated analytics, no writes. | `validate_query` is the sole guardrail for ad-hoc SQL — treat bypass bugs as security incidents. |

## Cost Envelope

All prices are **us-east-1** on-demand rates (May 2026). No reserved capacity or savings plans assumed.

### Pricing reference

| Service | Rate | Free tier |
|---------|------|-----------|
| Lambda requests | $0.20 / 1 M requests | 1 M req / month (permanent) |
| Lambda duration (x86, 1 GB) | $0.0000166667 / GB-s | 400 K GB-s / month (permanent) |
| Lambda Function URL | No additional charge | — |
| DynamoDB on-demand — write | $1.25 / 1 M WRU | 25 WCU provisioned free (permanent) |
| DynamoDB on-demand — read | $0.25 / 1 M RRU | 25 RCU provisioned free (permanent) |
| DynamoDB storage | $0.25 / GB-month | 25 GB / month free |
| CloudWatch Logs ingestion | $0.50 / GB | 5 GB / month free |
| CloudWatch Logs storage | $0.03 / GB-month | 5 GB / month free |
| Secrets Manager | $0.40 / secret / month + $0.05 / 10 K API calls | — |
| ECR private storage | $0.10 / GB-month | 500 MB / month free (12 months, new accounts) — your 247 MB image is fully covered |

> Assumptions per invocation: **1 GB Lambda memory**, **~1 s average duration** (catalog tools ~300 ms, SQL queries ~2–3 s; blended ~1 s), **1 DynamoDB write** (rate counter) + **1 DynamoDB read** (stale fallback check), **~4 KB CloudWatch log line** per call.

---

### Monthly cost by traffic tier

| Line item | **Demo / Hackathon** ≤ 3 K calls/mo | **Small team** ~15 K calls/mo | **Moderate production** ~300 K calls/mo |
|-----------|--------------------------------------|-------------------------------|------------------------------------------|
| Lambda requests | **$0.00** (free tier) | **$0.00** (free tier) | **$0.00** (free tier — 1 M limit) |
| Lambda duration (1 GB × ~1 s) | **$0.00** (free tier — 3 K GB-s) | **$0.00** (free tier — 15 K GB-s) | **$0.00** (free tier — 300 K GB-s) |
| DynamoDB WRU (3 K / 15 K / 300 K) | **< $0.01** | **$0.02** | **$0.38** |
| DynamoDB RRU | **< $0.01** | **< $0.01** | **$0.08** |
| CloudWatch Logs (~12 KB/call) | **$0.00** (free tier — ~36 MB) | **$0.00** (free tier — ~180 MB) | **$0.00** (free tier — ~3.6 GB within 5 GB) |
| Secrets Manager (1 secret) | **$0.40** | **$0.40** | **$0.40** |
| ECR private storage (~247 MB image) | **$0.025** (free if acct < 12 mo) | **$0.025** | **$0.025** |
| **Total (excl. Redshift)** | **≈ $0.43 / mo** | **≈ $0.45 / mo** | **≈ $0.88 / mo** |

> **Redshift** is the dominant cost and is **not provisioned by this Terraform**.  A `dc2.large` single-node cluster costs ~$0.25/hr ≈ **$182/month** (always-on). Redshift Serverless scales to zero when idle and charges $0.36/RPU-hour; for burst-only demo usage this is effectively **$0** between tests.

### Free-tier note

The Lambda invocation + duration free tier (1 M req + 400 K GB-s) is permanent for all AWS accounts. At 1 GB memory / 1 s average, the Lambda compute for this server stays **entirely within the free tier up to ~400 K calls per month** — meaning the effective cost of the MCP layer for any demo or moderate team is essentially just the $0.40/month Secrets Manager secret plus a few cents of ECR storage.

