# Akto × LiteLLM

Reference setup for putting **Akto AI Guardrails** in front of LLM traffic with
**LiteLLM** as the gateway, including coding-agent traffic (Claude Code) routed
through to **AWS Bedrock**.

- Claude Code → LiteLLM → Bedrock → Anthropic
- Akto hook attached to LiteLLM for guardrails + traffic ingestion
- Layered enforcement: **Bedrock** owns toxicity/PII, **Akto** owns prompt injection
- User/agent identity forwarded to Akto as queryable tags

---

## Architecture

```mermaid
flowchart LR
    CC["Claude Code"]
    LL["LiteLLM Gateway<br/>port 4000"]
    HK["Akto hook<br/>custom_hooks.py"]
    AK["Akto<br/>Guardrails + Ingestion"]
    BR["AWS Bedrock<br/>ap-south-1"]
    AN["Anthropic models"]

    CC -->|"HTTPS, x-litellm-api-key"| LL
    LL --> HK
    HK -->|"verdict: allow or 403"| AK
    HK -->|"mirrored request + response"| AK
    LL -->|"SigV4"| BR
    BR --> AN
```

Claude Code sends its own identity in the Anthropic `metadata.user_id` field, so
the hook can attribute traffic without any client-side configuration.

The hook attaches to LiteLLM through three callbacks:

| Callback | When | Role |
|---|---|---|
| `async_pre_call_hook` | before the provider call | guardrail verdict; returns `403` on a block |
| `async_should_run_agentic_loop` | after the model requests tool calls | ingests MCP / tool calls |
| `async_log_success_event` | after the response | ingests the full request + response |

Per request the gateway makes **two** guardrail-relevant calls:

| Call | Query | Purpose |
|---|---|---|
| verdict | `?guardrails=true` | allow/block decision, **before** the model is called |
| ingestion | `?ingest_data=true` | full request + response recorded in Akto |

The verdict call is deliberately narrowed (see [Guardrails with coding agents](#guardrails-with-coding-agents)); **ingestion always carries the complete payload**.

---

## Quick start

```bash
git clone https://github.com/akto-api-security/akto-litellm
cd akto-litellm

# 1. Configure
cp .env.example .env
$EDITOR .env

# 2. Run
docker compose up -d

# 3. Verify the hook loaded
docker compose logs litellm | grep GuardrailsHandler
# GuardrailsHandler initialized | sync_mode=True
```

Point a client at it:

```bash
export ANTHROPIC_BASE_URL="https://your-gateway.example.com"
export ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: Bearer $LITELLM_MASTER_KEY"
export ANTHROPIC_MODEL="bedrock-claude-sonnet-4-5"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="bedrock-claude-haiku-4-5"
unset ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY   # see note below
claude
```

> **Leave `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_API_KEY` unset.** `ANTHROPIC_CUSTOM_HEADERS`
> is what authenticates the gateway hop. Setting either variable makes Claude Code
> send an `Authorization` header that LiteLLM may try to use as its API key.

---

## Configuration

### `config.yaml`

| Block | Source |
|---|---|
| `litellm_settings.callbacks` | Akto docs — *Update LiteLLM Configuration* (verbatim) |
| `guardrails` (bedrock) | LiteLLM docs — *Bedrock Guardrails* |
| `general_settings.master_key` | LiteLLM docs — proxy config |
| `model_list` (bedrock) | LiteLLM docs — Bedrock provider |

The Akto connector needs exactly one line to activate:

```yaml
litellm_settings:
  callbacks: [custom_hooks.proxy_handler_instance]
```

### Bedrock models

`ap-south-1` has no regional Anthropic capacity for current models, so the
config uses **cross-region inference profiles**:

```yaml
- model_name: bedrock-claude-sonnet-4-5
  litellm_params:
    model: bedrock/global.anthropic.claude-sonnet-4-5-20250929-v1:0
    aws_region_name: os.environ/AWS_REGION_NAME
```

Confirm access before wiring a model in:

```bash
aws bedrock-runtime converse --region ap-south-1 \
  --model-id global.anthropic.claude-sonnet-4-5-20250929-v1:0 \
  --messages '[{"role":"user","content":[{"text":"ping"}]}]' \
  --inference-config '{"maxTokens":16}'
```

### Environment

See [`.env.example`](.env.example). Two defaults differ from the Akto docs and both are deliberate:

- **`TIMEOUT=30`** (docs say 5). Guardrail latency scales with payload size and crosses 5s around 200 KB. The `httpx` timeout raises an exception whose `str()` is empty, so the connector logged a blank line and **silently dropped the event**. 30s eliminates it.
- **`SYNC_MODE`** — `true` enforces, `false` observes. Note that `false` cannot be narrowed (that path validates and ingests in a single call), so its verdicts are unreliable for agent traffic. Use `true` for enforcement.

---

## Layered guardrails

Two independent layers, each owning a distinct class of risk so every block is
attributable to one owner:

| Risk | Owner | Response |
|---|---|---|
| Toxicity (hate/violence/sexual/insults/misconduct) | **AWS Bedrock** | `400 Violated guardrail policy` |
| Credit card | **AWS Bedrock** | `400` with the triggering assessment |
| SSN | **Akto** | `403 Blocked by Akto Guardrails` |
| Prompt injection | **Akto** | `403 Blocked by Akto Guardrails` |
| Email address | Bedrock, masked not blocked | `200`, value anonymised upstream |
| Benign | — | `200` |

Both layers detect PII; which one reports it depends on the entity, because the
Akto hook runs in `async_pre_call_hook` and reaches its verdict before LiteLLM's
Bedrock guardrail for entities Akto recognises. Measured, 3 trials each:

```
email + SSN        -> AKTO
SSN only           -> AKTO
credit card only   -> BEDROCK
email only         -> allowed (Bedrock ANONYMIZE masks the value)
```

The Bedrock guardrail sets **`PROMPT_ATTACK = NONE`** on purpose, so it never
competes with Akto on injection.

### Two Bedrock policy choices

`EMAIL = BLOCK` is *correct* enforcement, but coding agents inject the operator's
email into a `<system-reminder>` on **every** request, so it rejects 100% of
benign agent traffic. Two usable options:

| Guardrail | EMAIL | Effect |
|---|---|---|
| strict | `BLOCK` | strongest; agent traffic unusable — demo via a plain API client |
| agent-friendly | `ANONYMIZE` | Bedrock masks the email before the model sees it; SSN/card still `BLOCK` |

Switch by changing `guardrailIdentifier` / `guardrailVersion` in `config.yaml`.

### Session behaviour

Bedrock re-reads the whole conversation by default, so one PII message poisons
every later turn. Fixed with a documented flag:

```yaml
experimental_use_latest_role_message_only: true
```

This makes the Bedrock layer judge the same slice the Akto layer does — the
newest user turn — so the two stay symmetric.

---

## Passing user metadata

Identity is attached with **request headers**. LiteLLM puts them in its
`metadata` dict and the Akto hook turns them into queryable tags.

> **Status:** the header channel below is built and verified. The JWT layer from
> the design diagram — Claude Code fetching a token via `apiKeyHelper`, LiteLLM
> decoding it and caching the user details — is **not implemented yet**; the
> issuer has still to be decided. Until then the claim fields are supplied
> directly in the header, as shown.

```bash
export ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: Bearer $LITELLM_MASTER_KEY
x-litellm-end-user-id: umesh@akto.io
x-litellm-tags: team-platform,claude-code
x-litellm-spend-logs-metadata: {\"jwt_sub\":\"auth0|abc123\",\"seat\":\"eng-42\"}"
```

`x-litellm-spend-logs-metadata` takes arbitrary JSON and each field becomes its
own tag. That is where JWT claims will go once the decode step exists; the
`jwt_sub` value below is a placeholder supplied by hand to prove the path.
Header reference:

| Header | Becomes |
|---|---|
| `x-litellm-spend-logs-metadata` | one tag per JSON field |
| `x-litellm-end-user-id` | `end_user_id` |
| `x-litellm-tags` | `caller_tags` |

### What arrives in Akto

A single Claude Code request, **21 tags**:

```
jwt_sub             = auth0|abc123            seat         = eng-42
end_user_id         = umesh@akto.io           caller_tags  = team-platform,claude-code
client_account_uuid = 3623eb18-c6de-...       client_device_id  = ebb0e005ed1aaf...
client_session_id   = a11d32de-cdae-...       client_user_agent = claude-cli/2.1.252
model               = global.anthropic...     litellm_call_id   = a12d3e07-...
session_id / trace_id                         call_type = anthropic_messages
```

The `client_*` tags come for free — Claude Code populates the Anthropic
`metadata.user_id` field on every request, so account, device and session are
attributed with no configuration at all.

<details>
<summary>Alternative: attach identity to a LiteLLM virtual key instead</summary>

If you would rather hold identity server-side than send it per request, put it on
the key. Every field in `metadata` becomes a tag, and `key_alias` / `team_alias`
additionally drive the **collection name**, so each agent gets its own collection.

```bash
curl -X POST http://localhost:4000/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"key_alias":"umesh-laptop","user_id":"umesh@akto.io",
       "metadata":{"user_email":"umesh@akto.io","department":"engineering"}}'
```

Collection resolution order: `metadata.agent_name` → `user_api_key_metadata`
(`app_name`, `app_slug`) when `key_type: application` → `key_alias` →
`team_alias` → host of `LITELLM_URL`.

</details>

### Session & trace correlation

Session identity is also emitted as `x-akto-installer-*` request headers, the
same convention the other Akto connectors use, so traces index consistently:

```
x-akto-installer-akto_session_id  = <session id>
x-akto-installer-akto_message_id  = <litellm_call_id>
x-akto-installer-session_id / trace_id / litellm_call_id / user_agent
```

---

## Guardrails with coding agents

A coding agent's request is nothing like an app's. Measured on real Claude Code
traffic:

| | Size |
|---|---|
| full request | 86–120 KB |
| system prompt | ~18,000 chars |
| tool catalogue | ~130 KB |
| the user's actual prompt | **63 chars** |

Handing all of that to a guardrail buries the prompt. The same injection string
blocks reliably on its own and is allowed inside the full request. The connector
therefore narrows **the verdict call only**:

```
verdict payload : 120,139 B  ->  136–175 B
ingestion       : unchanged, full request
```

Two knobs control it — see `.env.example`:

- `VALIDATE_LAST_USER_MESSAGE_ONLY=true` — judge the newest user turn
- `STRIP_HARNESS_CONTEXT=true` — drop `<system-reminder>` scaffolding, which is
  harness context rather than user input

`QUARANTINE_BLOCKED_HISTORY=true` closes a related gap: a client that has a
message rejected keeps it locally and resends it as history, so blocked content
would still reach the model. The connector remembers what it blocked (per
session, in memory) and strips those turns before forwarding.

---

## Testing

```bash
./test-guardrails.sh 5      # 4-quadrant matrix, attributes each block by layer
```

```
case                   observed layer per run   | expectation
benign                 allowed allowed allowed  | allowed
toxic (hate)           BEDROCK BEDROCK BEDROCK  | BEDROCK
PII (credit card)      BEDROCK BEDROCK BEDROCK  | BEDROCK
PII (SSN)              AKTO    AKTO    AKTO     | AKTO
prompt injection       AKTO    AKTO    AKTO     | AKTO
```

Bedrock blocks are `HTTP 400 Violated guardrail policy` with the triggering
assessment in the body; Akto blocks are `HTTP 403 Blocked by Akto Guardrails`.

Watch the decisions live:

```bash
docker compose logs -f litellm | grep -E \
  "Narrowed guardrail view|Blocked by Akto|Violated guardrail|Stripped .* quarantined"
```

---

## Patched connector

`custom_hooks.py` is the upstream Akto connector **plus fixes** (+574/−20 lines,
8 hunks; only 20 upstream lines touched). Every change is behind an env flag and
defaults can be reverted to upstream behaviour. All of them affect **only the
copy sent for the guardrail verdict** — ingestion always records the original
request, so dashboard data is unchanged.

> **Fixes 1 and 2 are now upstream**, merged into `akto-api-security/akto` as
> PR #6290 (`normalize_verdict_payload`). They are documented here because this
> repo pinned them first; once you pull a connector build that includes #6290,
> the fork only needs fixes 3–5.

### 1. Content blocks were invisible to the guardrail — the big one  ✅ upstream

Akto's guardrail reads message content only as a **plain string**. Anthropic-style
content blocks (`content: [{"type":"text","text":"..."}]`) are not read at all.
Claude Code always sends blocks, so **prompt-injection blocking was silently
disabled for every Claude Code request** while the service reported `Allowed: true`.

Measured by replaying a real captured Claude Code verdict envelope, 4 trials each:

| Verdict payload | Result |
|---|---|
| content as list of blocks (as captured) | allowed 4/4 |
| same, flattened to a string | **blocked 4/4** |
| headers reduced to minimum | allowed (not the cause) |
| tags reduced to minimum | allowed (not the cause) |

Flag: `VERDICT_FLATTEN_CONTENT=true`

### 2. Call metadata suppressed detection  ✅ upstream

`stream` and `tools` in the mirrored body also suppress detection. Replaying a
real curl-derived envelope, 4 trials each: as captured → allowed 4/4; `tools`
removed → **blocked 4/4**. Neither key is content to judge.

Flag: `VERDICT_DROP_BODY_KEYS=stream,tools`

### 3. Verdict scoping — prevents false positives

A Claude Code request is 86–120 KB, of which the user's prompt may be 63 chars.
The verdict call is narrowed to the newest user turn and `<system-reminder>`
harness context stripped — 120,139 B → ~150 B.

This is **not** about detection failing on large payloads (fix #1 was the real
cause of that). It is about **false positives**: the agent's own system prompt
and scaffolding trip the policy. Measured with fix #1 in place, 4 benign Claude
Code prompts:

| | benign prompts falsely blocked |
|---|---|
| without verdict scoping | **1 of 4** |
| with verdict scoping | 0 of 4 |

Flags: `VALIDATE_LAST_USER_MESSAGE_ONLY=true`, `STRIP_HARNESS_CONTEXT=true`

### 4. Session survival after a block

When a request is rejected the client keeps the message and resends it as history
on the next turn. Without handling, one of two bad things happens: the blocked
content reaches the model as history, or the guardrail re-blocks it and every
later turn in the session dies. The connector remembers what it blocked (per
session, in memory) and strips those turns before forwarding, which avoids both.

Measured after a blocked prompt injection:

| | next benign turn | can the model quote the blocked text? |
|---|---|---|
| without quarantine | **403 — session dead** | n/a, everything is blocked |
| with quarantine | allowed | **no** — it quotes the following message |

Flag: `QUARANTINE_BLOCKED_HISTORY=true`

### 5. Identity and trace telemetry

`client_identity_tags()` surfaces the client's own identity (Claude Code account
uuid, device id, session id, user-agent), caller tags, and
`x-litellm-spend-logs-metadata` as Akto tags — 21 tags on a real request.
`session_trace_headers()` emits `x-akto-installer-akto_session_id` /
`akto_message_id`, matching the convention the other Akto connectors use.

### 6. `TIMEOUT` default

Raised 5 → 30. At 200 KB the guardrail call crossed 5s and the timeout was
swallowed as a fail-open, silently dropping the event.

### Verified after patching

```
Claude Code, injection x5      403 AKTO x5
Claude Code, benign x4         all allowed
hello then injection, x3       blocked 3/3
injection then benign          benign works; model cannot quote the blocked turn
curl path, injection x6        6/6 blocked
curl path, benign x8           0/8 falsely blocked
toxicity / PII                 Bedrock, 3/3
```

### Reverting to upstream behaviour

```bash
VERDICT_FLATTEN_CONTENT=false
VERDICT_DROP_BODY_KEYS=
VALIDATE_LAST_USER_MESSAGE_ONLY=false
STRIP_HARNESS_CONTEXT=false
QUARANTINE_BLOCKED_HISTORY=false
```

These fixes belong upstream in `akto-api-security/akto`; this repo carries them
so the reference setup works today.

## Known issues

| Issue | Status |
|---|---|
| JWT identity layer (`apiKeyHelper` fetch, LiteLLM decode, user-detail cache) not implemented; issuer undecided | **not started** |
| Occasional inconsistent verdicts were largely explained by fix #1 (content blocks silently ignored). Residual flakiness has been seen but not reproduced since | mostly resolved |
| `policy_name` / `rule_violated` are empty on every Akto block; `Reason` is always `"Blocked by Akto"` — nothing is diagnosable | **open, server-side** |
| Injection phrasing coverage not re-measured since the content-format fix; the earlier 1-of-8 figure was taken while blocks were being silently dropped and is not trustworthy | needs re-testing |
| `SYNC_MODE=false` verdicts unreliable — that path validates and ingests in one call, so it cannot be narrowed | open |
| Quarantine state is in-memory (500 sessions × 50 turns, cleared on restart) — a multi-replica deployment needs shared state | open |
| Akto malicious-session detection can block later benign turns in a session that contained a violation | by design; framing matters for demos |

---

## Repository layout

```
config.yaml            LiteLLM + Akto + Bedrock guardrail configuration
docker-compose.yml     gateway + postgres
.env.example           every setting, documented
up.sh                  idempotent bring-up
test-guardrails.sh     layered guardrail matrix, attributes each block by layer
claude-via-bedrock.sh  point Claude Code at the gateway
claude-as-agent.sh     same, via a virtual key with key_alias
docs/claude-code-vs-litellm.md   Akto hook comparison: telemetry & control
```

`custom_hooks.py` here is a **patched copy** of the upstream connector — see
[Patched connector](#patched-connector). Upstream lives at
[akto-api-security/akto](https://github.com/akto-api-security/akto/tree/master/apps/mcp-endpoint-shield/litellm).

## Further reading

- [Akto hooks: Claude Code vs LiteLLM](docs/claude-code-vs-litellm.md)
- [Akto LiteLLM connector docs](https://docs.akto.io)
- [LiteLLM proxy docs](https://docs.litellm.ai/docs/proxy/docker_quick_start)
