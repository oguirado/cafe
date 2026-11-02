# Café — a human-confirmed demonstration guide

## Inspiration and purpose

Streamers need to keep a guest's call, separate feeds, recording, scene setup and
chat context understandable while staying in control. Café's intended workflow
groups host-selected call windows into capsules and makes typed chat reads
available to assistants. This submission supplies a static, inspectable guide to
that workflow. Its seven live checkpoints remain pending.

## What is available in the fixed base

This account describes commit `f1e874050e305645a90e64267e50f8625daae9bc`.
The [project status](https://github.com/oguirado/cafe/blob/f1e874050e305645a90e64267e50f8625daae9bc/README.md)
describes a Windows application with an Electron shell, Rust authority and Python
connectors, with development coverage and production acceptance still pending.
The [acceptance record](https://github.com/oguirado/cafe/blob/f1e874050e305645a90e64267e50f8625daae9bc/docs/acceptance/README.md)
distinguishes portable fixtures, hosted synthetic windows and actual native
observations. It records no live Discord acceptance receipt.

The existing [MCP server](https://github.com/oguirado/cafe/blob/f1e874050e305645a90e64267e50f8625daae9bc/connectors/cafe/mcp_server.py)
defines exactly these six chat tools:

| Tool | Typed input and existing bound |
| --- | --- |
| `list_channels_and_days` | Empty object. |
| `recent_messages` | Required channel; optional limit 1–200 and UTC since. |
| `search_messages` | Required channel and literal query of 1–512 characters; optional limit 1–200. |
| `top_chatters` | Required channel and window_minutes 1–1440. |
| `message_rate` | Required channel and bucket_seconds 1–3600. |
| `session_summary` | Required channel and calendar-valid day. |

The server uses newline-delimited JSON-RPC over stdio, protocol version
`2025-11-25`, an initialize request followed by notifications/initialized, and
then tools/list and tools/call. Notifications receive no response. Existing
request and response caps are 65,536 bytes; an event line is bounded at 131,072
bytes, a scan at 33,554,432 bytes and a file traversal at 4,096 entries.
JSON-RPC errors remain -32700 for parse errors, -32600 for invalid requests,
-32601 for unknown methods and -32602 for invalid parameters. A data failure or
oversized tool result uses the closed `data_unavailable_or_result_too_large`
tool error. This proposal changes none of those rules or limits.

The [control implementation](https://github.com/oguirado/cafe/blob/f1e874050e305645a90e64267e50f8625daae9bc/connectors/cafe/mcp_control.py)
defines `select_capsule`, `start_recording`, `stop_recording` and `send_to_obs`.
These are existing conditional control names, not four new guide tools. The
default server does not advertise them without a supplied control plane.
The [runtime projection](https://github.com/oguirado/cafe/blob/f1e874050e305645a90e64267e50f8625daae9bc/core/src/runtime_main.rs)
sets `mcp_control_available` to false. Recording branches and connector fixtures
exist, but the recorder, native confirmation and live runtime data are not
connected through MCP in this base. Injecting a provider in a test does not
establish that product connection.

## What this submission adds

The proposed `demo_guide` tool and `cafe://demo/guide` resource return static
definitions. They are **not registered in the unchanged base server**.
`tool_definitions()` and `resource_definitions()` describe this proposed surface;
`demo_guide(arguments)` and `read_resource(uri)` return its flat data object.
The schema is `cafe.demo_guide.v1`, the control mode is `human_confirmed`, and
every step has `live_status: "pending"`. A successful full read contains seven
ordered steps. An optional integer step selects one definition. Invalid tool
arguments return empty steps and `invalid_arguments`; an unrecognized resource
returns empty steps and `unknown_resource`. Neither error executes anything.

The guide performs no disk reads, directory scans, JSONL parsing, network access,
process launch or control effects. A future, separately reviewed server hook
would register its metadata, dispatch its functions and apply the existing
outer MCP framing and response cap. The guide's own payload remains flat.
Resource registration and that dispatcher hook are outside this submission.

## Human control and data boundaries

Any recording or OBS action requested by an AI is only a proposal that runs after
the operator clicks **Confirm inside Café**. **Control remains disabled without
a native provider.** The required confirmation boundary is not a claim that the
native bridge is already connected. A queued proposal is not a completed effect.
Confirmation material stays outside model, renderer, file and log outputs.
The renderer only renders and submits intents over closed typed IPC; chat text
is untrusted data and never executable instructions.

The [chat event schema](https://github.com/oguirado/cafe/blob/f1e874050e305645a90e64267e50f8625daae9bc/docs/schemas/chat_event.v1.json)
is a closed object tagged `cafe.chat_event.v1`. Its received_at object holds a
canonical decimal-string monotonic_ns and a calendar-valid UTC timestamp.
UTC uses `YYYY-MM-DDTHH:MM:SS[.fraction]Z`, with one through nine fractional digits
when present. Decimal u64 clocks match `0|[1-9][0-9]{0,19}` and cannot exceed
18446744073709551615. Monotonic readings compare only within the same session.
Unknown optional values remain null; unknown data is never replaced with a
fabricated total. Extra fields and wrong types are rejected, including booleans
where integers are required. Any future explicit time-range contract must keep
the half-open interval `[from,to)`. The guide accepts no timestamps or ranges.

Demonstration identifiers are synthetic: `demo_channel` is a synthetic channel
label, and `<CODEX_SESSION_ID>` is an unfilled session placeholder. No private
captures, account details, user paths or credentials belong in this submission.

## S01 — Prepare a quiet call [PENDING-LIVE:S01]

**Live status: pending.** Invite a guest to a Discord call and silence music.
Expected: a quiet, clean call ready to isolate. Required operator-visible check:
hear the guest clearly and confirm that background music is silent. This is a
future acceptance observation, not a fixture result.

## S02 — Select the popped-out window [PENDING-LIVE:S02]

**Live status: pending.** After S01, pop out the call window and choose it through
the host selector. Expected: the host holds the window token; a title or
executable name is not its identity. Required check: see the intended selection
in the host selector without publishing raw handles or window lists.

## S03 — Establish the capsule [PENDING-LIVE:S03]

**Live status: pending.** After S02, establish the capsule identifier and
generation. Label actual feeds game and camera only where those feeds exist.
Expected: stable capsule identity with the intended feed assignments. Required
check: see the capsule tile, its identifier and generation, and the real feed
labels. Missing feeds remain missing.

## S04 — Record, stop and verify the file [PENDING-LIVE:S04]

**Live status: pending.** After S03 and only when the native prerequisites are
available, confirm start recording, then confirm stop recording, and inspect
the resulting file in the operator-chosen folder. Expected: operator-confirmed
recording produces a verifiable file. Required check: see the file entry and
open the file to verify the intended content. A queued request, fixture artifact
or guide response is not acceptance of recording or file publication.

## S05 — Reuse one OBS target [PENDING-LIVE:S05]

**Live status: pending.** After S04 and native configuration, send the same
capsule to OBS twice through operator-confirmed actions. Expected: one Window
Capture target is reused without a duplicate source. Required check: inspect
the OBS source list after both attempts and verify the intended pixels.
Configuration readback alone does not establish capture or broadcast acceptance.

## S06 — Observe a typed chat row [PENDING-LIVE:S06]

**Live status: pending.** After S05, observe a live Twitch message and its durable
JSONL row in an authorized demonstration. Expected: a typed row valid against
the existing chat event schema. Required check: see the received message and
the corresponding schema-validated JSONL line with private details redacted.
This static guide does not parse, aggregate, store or fabricate a chat row.

## S07 — Inspect bounded assistant reads [PENDING-LIVE:S07]

**Live status: pending.** After S06, complete MCP initialization, inspect
tools/list, and perform typed reads such as `recent_messages` with the synthetic
channel label and a limit of 5. Expected: the existing tools return bounded typed
results from authorized data. Required check: see the tool list, typed arguments,
result and scan metadata, or the honest closed error. An unavailable data source
must not be described as successful live runtime integration.

## What remains to demonstrate

All seven checkpoints are unresolved. The narration and guide define the
operator-visible evidence required; neither is a live receipt. Contract tests
can establish shapes, rejections, static content and absence of effects. They
cannot accept native capture, recording, file publication, OBS pixels,
broadcasting or MCP control. Future work must connect the reviewed native
providers and record actual redacted Windows acceptance before making those
claims. Test execution and CI outcomes are reported separately from this pitch.
