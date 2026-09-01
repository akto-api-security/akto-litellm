# Akto hook behaviour: Claude Code vs LiteLLM

Akto ships two very different connectors for agentic traffic. They are not
alternatives to each other — they observe different layers and can see things the
other structurally cannot.

| | **Claude Code hooks** | **LiteLLM connector** |
|---|---|---|
| Akto product | Atlas (employee endpoints) | Argus (homegrown AI) |
| Where it runs | on each developer's machine | centrally, at the gateway |
| Integration | Claude Code's native hook system (`~/.claude/settings.json`) | one line: `litellm_settings.callbacks` |
| Install unit | 9 files per machine (wrappers + Python + shared modules) | 1 file, one place |
| Identity anchor | device (`DEVICE_ID`) | virtual key / user / team, plus client account id |
| Covers non-Claude clients | no | yes — any LiteLLM caller |
| Covers non-LLM tool calls | yes (`Bash`, `Read`, `Edit`, …) | no |

---

## 1. Interception points

### Claude Code — 4 hook points, around the agent loop

| Hook | Fires | Can block? |
|---|---|---|
| `UserPromptSubmit` | before the prompt reaches the API | yes — `{"decision":"block"}` |
| `PreToolUse` | before a tool executes **locally** | yes — `permissionDecision: "deny"` |
| `PostToolUse` | after a tool returns | yes — `{"decision":"block"}` |
| `Stop` | when the model finishes generating | yes — `{"decision":"block"}` |

### LiteLLM — 4 callbacks, around the model call

| Callback | Fires | Can block? |
|---|---|---|
| `async_pre_call_hook` | before the provider call | yes — `HTTPException(403)` |
| `async_moderation_hook` | parallel moderation slot | yes |
| `async_should_run_agentic_loop` | after the model requests tool calls, before the client runs them | no — ingest only |
| `async_log_success_event` | after the response | no — ingest only |

**The structural difference:** Claude Code hooks sit around *tool execution on the
developer's machine*. The LiteLLM connector sits around *the model call*. A tool
call that Claude Code executes locally never touches LiteLLM, so the gateway sees
the model's *request* to call a tool but never its result.

---

## 2. Control

| Capability | Claude Code | LiteLLM |
|---|---|---|
| Block a prompt before the model sees it | ✅ | ✅ (`403`) |
| Block a **local** tool call before it executes | ✅ `PreToolUse` | ❌ tools run client-side |
| Block on the tool's **result** | ✅ `PostToolUse` | ❌ never sees results |
| Block the final response | ✅ `Stop` | ❌ post-call is ingest-only |
| Rewrite tool arguments | ✅ `updatedInput` | ❌ |
| Redact / rewrite the request body | ❌ | ✅ `ModifiedPayload` |
| Three-state verdict (block / warn / alert) | ✅ | ❌ binary |
| User-bypassable soft block | ✅ `warn` — resubmit identical input to proceed | ❌ |
| Enforce for every client centrally | ❌ per-machine install | ✅ |
| Rate limits, budgets, key rotation | ❌ | ✅ LiteLLM virtual keys |

Notable asymmetries:

- **Only Claude Code can stop a destructive local action.** A `Bash` command that
  deletes files is blocked at `PreToolUse`; the gateway has no visibility into it.
- **Only LiteLLM can redact.** Akto can return a rewritten request body and the
  connector splices it back in, so PII can be masked rather than rejected. Claude
  Code hooks can only deny (or rewrite *tool arguments*).
- **Claude Code's `warn` behaviour** blocks once with a reason and lets the user
  resubmit the identical input to proceed, tracked by a SHA-256 fingerprint in
  `~/.claude/akto/logs/akto_*_warn_pending.json`. LiteLLM has no equivalent.
- **Both fail open.** Timeout, non-200, unset URL, or any unexpected exception
  results in *allow* on both sides. `SYNC_MODE=true` does **not** mean fail-closed.

---

## 3. Telemetry

### Only LiteLLM has

- **Model actually served** (including which Bedrock inference profile)
- **Token usage and cost**, latency, retries, `applied_guardrails`
- **Full request and response payloads** as the provider saw them
- **Key/org identity**: `user_api_key_alias`, `user_id`, `team_alias`, `org_alias`,
  `project_alias`, budgets and spend
- **Arbitrary caller metadata** via `x-litellm-spend-logs-metadata` (JSON, flattened
  to individual tags) and `x-litellm-tags`
- Traffic from **every** client behind the gateway, not just Claude Code

### Only Claude Code hooks have

- **Local tool results** — what `Bash`/`Read`/`Edit` actually returned
- **Non-MCP built-in tool calls** (opt-in via `AKTO_INGEST_NON_MCP_TOOLS=true`)
- **Per-device attribution** with a stable device label, and a heartbeat that
  registers the machine
- The **final assistant message** as rendered to the user
- Prompt/response visibility even when the user talks to Anthropic directly,
  with no gateway in the path

### Both have

- The user's prompt
- MCP tool calls, reported as JSON-RPC `tools/call` on `/mcp`, so MCP servers and
  tools land in the Akto inventory
- Session/trace correlation via `x-akto-installer-akto_session_id` /
  `akto_message_id` headers

### Client identity available to the LiteLLM connector

Claude Code sends its own identity in the Anthropic `metadata.user_id` field, so
the gateway gets it with no client configuration:

```
client_account_uuid   Claude Code account
client_device_id      machine
client_session_id     conversation
client_user_agent     claude-cli/<version> (…, agent-sdk/<version>)
```

---

## 4. Collection / inventory shape

| | Claude Code | LiteLLM |
|---|---|---|
| Default collection | `<DEVICE_ID>.ai-agent.claudecli` | host of `LITELLM_URL` |
| Per-agent split | one collection per device | `metadata.agent_name` → `app_name` → `key_alias` → `team_alias` |
| MCP servers | `<DEVICE_ID>.claudecli.<server>` — one per MCP server | `/mcp` path, tagged `mcp_server_name` |
| Non-MCP tools | `/tool/<name>` (opt-in) | not visible |

A practical consequence: **device names must not contain dots.** The dashboard
takes the first label of the reported host as the device name, so a dotted label
gets truncated — hence the `<computer-name>-<first 8 of machine id>` convention.

---

## 5. Payload size and guardrail effectiveness

Both connectors hand Akto whatever the client sent, and for coding agents that is
mostly scaffolding:

| | Claude Code request |
|---|---|
| full request | 86–120 KB |
| system prompt | ~18,000 chars |
| tool catalogue | ~130 KB |
| user's actual prompt | 63 chars |

Guardrail detection degrades badly at that size — the user's text is buried. The
LiteLLM setup in this repo narrows the **verdict** call to the newest user turn
and strips `<system-reminder>` scaffolding, taking the judged payload from
120,139 B down to ~150 B while ingestion keeps the whole request.

The Claude Code connector has the same exposure and no equivalent narrowing: its
`UserPromptSubmit` hook sends the prompt alone (small, fine), but `PreToolUse` /
`PostToolUse` send whole tool payloads.

---

## 6. Operational differences

| | Claude Code | LiteLLM |
|---|---|---|
| Rollout | per developer machine; needs a deployment script | one gateway |
| Upgrade | re-run install on every machine | replace one file, restart |
| Bypass | user can delete `~/.claude/settings.json` | user must be able to reach the provider directly |
| Blast radius of failure | one machine | all traffic |
| Requires the client to opt in | yes | no, if the gateway is the only egress |
| Extra network hop | none | one |

**Enforceability is the deciding factor.** Claude Code hooks live in a directory
the developer controls, so they are advisory on an unmanaged laptop. A gateway
that is the only route to the provider cannot be sidestepped — which is why the
two are usually deployed together: LiteLLM for enforcement and cost/identity
telemetry, Claude Code hooks for local tool visibility and control.

---

## 7. Choosing

| Goal | Use |
|---|---|
| Enforce policy no client can bypass | LiteLLM |
| Token/cost attribution per user or team | LiteLLM |
| Redact PII instead of rejecting the request | LiteLLM |
| Block a destructive local shell command | Claude Code |
| See what a tool actually returned | Claude Code |
| Inventory MCP servers a developer uses | either (Claude Code gives per-server collections) |
| Cover non-Claude clients and homegrown apps | LiteLLM |
| Discover shadow AI usage on employee laptops | Claude Code |
| Complete coverage | both |

---

## Appendix — shared ingestion contract

Both connectors POST the same envelope to the same endpoint:

```
POST {DATA_INGESTION_SERVICE_URL}/api/http-proxy
     ?guardrails=true          # verdict
     &ingest_data=true         # record
     &akto_connector=<name>    # litellm | claude_code_cli
```

Response both parse:

```json
{"data":{"guardrailsResult":{
  "Allowed": true,
  "Reason": "",
  "behaviour": "",
  "ModifiedPayload": ""
}}}
```

`behaviour` (`warn` / `alert`) is honoured only by the Claude Code connector.
`ModifiedPayload` is applied only by the LiteLLM connector. Both treat any
transport failure as *allow*.
