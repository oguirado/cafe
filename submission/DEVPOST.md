# Café — Windows capture capsules and human-confirmed MCP organizer

## Inspiration and purpose

Streamers and creators collaborating with guests on Discord need a reliable way to isolate call pop-out windows, preview them in one place, record separate clean feeds to disk, and let AI assistants help organize sessions without surrendering control. Café is a Windows desktop application (Electron shell, Rust authority core, and Python MCP/OBS/Twitch connectors) that turns popped-out Discord windows and desktop windows into live capsules, records each feed with Windows Graphics Capture and FFmpeg/NVENC, and exposes a human-confirmed MCP surface for AI assistants.

## What is verified

The following capabilities were measured on a real Windows laptop with an NVIDIA GTX 1060:

- **Single Discord pop-out capture and NVENC recording [MEASURED]:** A real Discord pop-out window was captured through Windows Graphics Capture and recorded with FFmpeg/NVENC to a valid MKV file for 30 s (1810 frames) with a clean stop.
- **Multi-window concurrent recording [MEASURED]:** Two real windows recorded simultaneously, and four windows recorded simultaneously (one real Discord pop-out plus three test windows) for around 30 s each, all producing valid MKV files; still windows continue recording normally.
- **Live capsule tile preview [MEASURED]:** Live preview of the Discord pop-out renders inside its capsule tile in the packaged Windows application.
- **End-to-end Claude Code CLI over MCP [MEASURED]:** Connected over stdio MCP, Claude Code CLI listed the windows, proposed a recording plan with a subfolder name, a confirmation panel appeared inside Café, and after one operator click on **Confirm**, Café created the capsule, anchored only the Discord pop-out, and recorded a 45 s valid MKV into the subfolder.
- **Multi-client MCP resilience [MEASURED]:** A second MCP client works against the running session and survives restarts of the MCP process.

## Known limits

- **Covered Discord pop-outs go black [MEASURED]:** When another window fully covers a Discord pop-out, Discord stops painting the occluded window, so its tile preview and recording go black. Keep pop-outs not fully covered on screen.
- **Not yet verified:**
  - Capturing and recording more than one real Discord pop-out window at the same time.
  - Live OBS WebSocket connection and scene/source creation against a running OBS instance.
  - Live OpenAI Codex CLI client execution (Codex speaks the same stdio MCP protocol; registration uses `command = resources\python\python.exe` and `args = -I -B resources\connectors\cafe_mcp.py`).

## What is available in the base and packaged application

The [project overview](../README.md), [acceptance runbook](../docs/acceptance/README.md), and [MCP organizer documentation](../docs/bridge/CODEX_ORGANIZER.md) describe the full Windows runtime, Rust authority core in [`core/src/runtime_main.rs`](../core/src/runtime_main.rs), direct control adapter in [`connectors/cafe/mcp_control.py`](../connectors/cafe/mcp_control.py), and organizer adapter in [`connectors/cafe/mcp_organizer.py`](../connectors/cafe/mcp_organizer.py).

In the packaged Windows build, register Café's MCP server in Claude Code or Codex with:

- `command`: `resources\python\python.exe`
- `args`: `-I -B resources\connectors\cafe_mcp.py`

The [MCP server](../connectors/cafe/mcp_server.py) exposes the six bounded chat read tools:

| Tool | Typed input and bound |
| --- | --- |
| `list_channels_and_days` | Empty object. |
| `recent_messages` | Required channel; optional limit 1–200 and UTC since. |
| `search_messages` | Required channel and literal query of 1–512 characters; optional limit 1–200. |
| `top_chatters` | Required channel and window_minutes 1–1440. |
| `message_rate` | Required channel and bucket_seconds 1–3600. |
| `session_summary` | Required channel and calendar-valid day. |

When connected to the running Windows session broker, the MCP server also exposes the six human-confirmed organizer tools (`organizer_status`, `list_discord_windows`, `propose_recording_plan`, `plan_result`, `propose_stop_all`, and `propose_send_to_obs`). The server uses newline-delimited JSON-RPC over stdio (`2025-11-25`), bounded 65,536-byte frames, and closed error codes.

## What this submission adds

Alongside the live Windows capture pipeline, native confirmation UI, and MCP organizer bridge, [`connectors/cafe/mcp_operator_runbook.py`](../connectors/cafe/mcp_operator_runbook.py) provides a static, inspectable `cafe.demo_guide.v1` operator checklist (`demo_guide` and `cafe://demo/guide`) with `control_mode: "human_confirmed"` for offline contract verification.

## Human control and data boundaries

The security model is strict: **the AI proposes, only the operator's click runs it, and the AI never sees secrets.**

Any recording, stop, or OBS action requested by an AI over MCP creates only a 60-second pending proposal. Nothing executes until the operator clicks **Confirm inside Café**. Window titles and chat messages are treated strictly as untrusted data, never executable instructions. Broker rendezvous secrets, native confirmation tokens, OBS credentials, and full host filesystem paths are never exposed to the model or accepted as MCP tool arguments.

The [chat event schema](../docs/schemas/chat_event.v1.json) is a closed object tagged `cafe.chat_event.v1` with canonical decimal monotonic clocks and UTC timestamps (`YYYY-MM-DDTHH:MM:SS[.fraction]Z`). Placeholder identifiers such as `<CODEX_SESSION_ID>` remain explicit placeholders in documentation.

## S01 — Prepare a Discord call pop-out

Invite a guest to a Discord call, pop out the call window, and keep the pop-out window not fully covered by other windows so Discord continues painting frames. Verified on Windows with a real Discord pop-out.

## S02 — Discover or select the popped-out window

Select the popped-out window via the One-Click **Add window** picker in Café or let the MCP assistant call `list_discord_windows` to obtain opaque window tokens. Only windows classified as `Popout` are eligible for automatic Discord anchoring; `Unknown` windows are never anchored automatically. Verified on Windows with Claude Code CLI over MCP.

## S03 — Establish the capsule and live preview

Anchor the pop-out into a capsule tile on the Café wall. The capsule renders a live preview of the pop-out inside its tile in the packaged application and tracks capsule identity and generation. Verified on Windows.

## S04 — Confirm the plan, record NVENC MKV, and verify the file

Ask the assistant to propose a recording plan in subfolder `Demo 01` (or click **REC** directly on any tile). When the plan panel appears in Café, click **Confirm**. Café creates the subfolder under the operator-chosen root and records via Windows Graphics Capture and FFmpeg/NVENC into a valid MKV file (measured at 30 s / 1810 frames for a single pop-out, 45 s via Claude Code MCP plan confirmation, and ~30 s each across four concurrent windows). Stop recording and open the MKV file to verify playback.

## S05 — Reuse one OBS target (optional)

When a live OBS connection is configured, sending a capsule to OBS reuses a single Window Capture target without creating duplicate sources. Live OBS connection remains not yet verified and is marked optional in the live demonstration.

## S06 — Observe a typed chat row

Receive Twitch chat messages and store durable JSONL rows validated against `cafe.chat_event.v1`, preserving canonical clocks and null for unknown optional fields.

## S07 — Inspect bounded assistant reads and multi-client MCP

Connect an MCP client (or a second MCP client across MCP process restarts) to list tools, run bounded typed chat queries, and propose organizer plans that execute only after the operator's Confirm click inside Café.

## What remains to demonstrate

While single Discord pop-out WGC/NVENC recording, live tile previews, four-window concurrent recording (one Discord pop-out plus three test windows), Claude Code CLI MCP plan confirmation, and second-client MCP restarts are verified on Windows, verifying multiple simultaneous real Discord pop-out windows, live OBS WebSocket control, and a live Codex CLI session remain next steps.
