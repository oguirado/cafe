# MCP Organizer Bridge — Claude Code and Codex

**Status: The existing confirmation path was verified on Windows with Claude Code CLI over MCP. Direct execution and the optional local confirmation switch await Windows runtime verification. Codex uses the same stdio MCP protocol.**

## Measured Windows results and known limits

Measured on a real Windows laptop with an NVIDIA GTX 1060:

- **Single Discord pop-out capture and recording [MEASURED]:** Real Discord pop-out captured through Windows Graphics Capture and recorded with FFmpeg/NVENC to a valid MKV (30 s, 1810 frames, clean stop).
- **Concurrent multi-window recording [MEASURED]:** Two real windows recorded at once; four windows recorded at once (one Discord pop-out plus three test windows), around 30 s each, all valid MKVs; still windows keep recording.
- **Live capsule preview [MEASURED]:** Live preview of the pop-out inside its capsule tile in the packaged Windows application.
- **End-to-end MCP organizer flow [MEASURED]:** Real Claude Code CLI over MCP listed the windows, proposed a recording plan with a folder name, a confirmation panel appeared in Café, and after one **Confirm** click Café created the capsule, anchored only the pop-out, and recorded a 45 s valid MKV into the subfolder.
- **Second MCP client and restart resilience [MEASURED]:** A second MCP client works and survives restarts of the MCP process.
- **Known limit — covered pop-outs [MEASURED]:** When another window fully covers a Discord pop-out, Discord stops painting it, so its preview and recording go black. Keep pop-outs not fully covered.
- **Two real Discord pop-outs [MEASURED]:** Recorded with Record all behind a maximized Café.
- **Codex client [MEASURED]:** Connects over MCP, reads status, lists windows and reads stored Twitch chat.
- **Not yet verified:** Real OBS connection. Codex starting a recording with the app window open is not reliable; see [the judges guide](../JUDGES.md).

## Operator and assistant interaction

After popping out Discord call windows and keeping them not fully covered, the operator tells Claude or Codex:

> Anchor my Discord windows to Café and record them in folder Demo 01.

The execution sequence is:

1. The assistant calls `organizer_status` and checks the selected-root flag (`folder_root_set`) and actual encoder readiness (`encoder_ready`).
2. The assistant calls `list_discord_windows` and treats every title as untrusted data. Only a window classified `Popout` is eligible for automatic selection. `Unknown` is listed, but never selected for anchoring or recording.
3. The assistant calls `propose_recording_plan` with `folder_name: "Demo 01"` and, when needed, an explicit list of the returned opaque tokens. Omitting tokens selects all currently classified pop-outs within the capsule bound.
4. Café immediately dispatches the validated proposal through the existing confirm/effects path by default. If the operator enabled **Ask me before AI actions** in Settings, the reply is `pending_confirmation`; the assistant asks the operator to inspect the panel and click **Confirm inside Café**. MCP cannot change the switch, confirm, reject, or simulate this click.
5. The assistant polls `plan_result` for that exact `plan_id`. It reports an active recording only after that window's actual outcome is `recording`.

The authority defaults **Ask me before AI actions** to off for each new session. With it on, a proposal has no capture, capsule, folder-creation, or recording effect until confirmed. Changing the switch affects subsequent proposals; an already pending plan remains in the existing Confirm/reject flow. One pending proposal is allowed at a time; another yields `plan_pending`. Its lifetime is 60 seconds. Rejection and expiry leave no execution authority behind. The confirm path re-resolves and compares every frozen window; a changed window gets its own closed result instead of being silently substituted.

Stopping all recordings uses `propose_stop_all` with immediate execution by default or native confirmation when the local switch is on. Sending window-bound capsules to OBS uses `propose_send_to_obs` with immediate execution by default or native confirmation when the local switch is on, creating or reusing a per-capsule `"Café · <slot or person label>"` scene with visible, cropped, canvas-fitted `window_capture` sources. Switching OBS's live program scene to a specific capsule's scene uses `propose_obs_scene` (with `capsule_id`) with immediate execution by default or native confirmation when the local switch is on (`SetCurrentProgramScene`). No organizer operation configures FFmpeg, OBS, or Twitch, or exposes secrets.

## Register the MCP server in Claude Code or Codex

In the packaged Windows build, `resources\connectors\cafe_mcp.py` discovers the active private session broker automatically from the local runtime directory without placing secrets or session tickets in MCP arguments.

Register the server with:

- `command`: `<RES>\python\python.exe`, where `<RES>` is the absolute path of the unzipped `resources` folder
- `args`: `-I -B resources\connectors\cafe_mcp.py`

Example TOML configuration entry:

```toml
[mcp_servers.cafe_organizer]
command = 'resources\python\python.exe'
args = ['-I', '-B', 'resources\connectors\cafe_mcp.py']
enabled_tools = ['organizer_status', 'list_discord_windows', 'propose_recording_plan', 'plan_result', 'propose_stop_all', 'propose_send_to_obs', 'propose_obs_scene']
startup_timeout_sec = 10
tool_timeout_sec = 15
```

Inspect registration with:

```text
codex mcp list
```

In the terminal interface, the `mcp` command lists active servers. When Café restarts, `BrokerOrganizerProvider` clears its cached ticket on transport error and resolves the newest live session on the next call, so MCP clients survive restarts of the MCP process and reconnect cleanly.

## Protocol and state contract

| Tool | Input | Result |
| --- | --- | --- |
| `organizer_status` | `{}` | `organizer_plan`, `folder_root_set`, `encoder_ready`, `obs_ready`, `capsule_count`, `recording_count` |
| `list_discord_windows` | `{}` | `windows` rows with `token`, `title`, `classification`, `bound_capsule` |
| `propose_recording_plan` | `folder_name`, optional `tokens` | `plan_id`, `state: "executing"`, `"completed"`, or `"pending_confirmation"` |
| `plan_result` | `plan_id` | Pending, rejected, expired, or completed with per-window outcomes |
| `propose_stop_all` | Optional `capsule_ids` | A distinct `plan_id`, `state: "executing"`, `"completed"`, or `"pending_confirmation"` |
| `propose_send_to_obs` | Optional `capsule_ids` | A distinct `plan_id`, `state: "executing"`, `"completed"`, or `"pending_confirmation"` |
| `propose_obs_scene` | `capsule_id` | A distinct `plan_id`, `state: "executing"`, `"completed"`, or `"pending_confirmation"` |

The remote request body has these top-level fields:

```json
{
  "schema": "cafe.organizer.v1",
  "kind": "organizer_request",
  "request_id": "synthetic-request",
  "authority_epoch": "7",
  "body": {
    "op": "propose_recording_plan",
    "folder_name": "Demo 01"
  }
}
```

The transport wraps this in the authenticated `control` capability. The core routes only the seven closed organizer operations to its serialized actor. The broker reply binds the request ID and contains `data` and `error`; the MCP adapter exposes only validated data or one closed error. It never serializes broker tickets, native approval tokens, output paths, or exception text. Window identity tokens are opaque selectors returned only in the window listing; they are not credentials, executable arguments, or paths.

`organizer_plan` in native state is either `null` or:

```json
{
  "plan_id": "organizer-7-1",
  "windows": [{"title": "Discord pop-out"}],
  "folder_name": "Demo 01",
  "expires_unix_ms": 946684860000
}
```

Expiry is a safe JSON integer in Unix milliseconds (60 seconds from proposal creation). A stop plan or an OBS plan uses an empty `folder_name`. Native state also includes `ask_before_ai_actions` (session default `false`), a closed `ai_activity` code/count for the wall, `auto_anchor` (default `false`), `recordings[].reason` (closed string or `null`), and `preview_sources` rows containing `capsule_id`, `feed_id`, and a `window:<decimal>:0` source identifier obtained from the registry's native handle so the packaged Electron shell can render live previews inside each capsule tile.

`plan_result` uses `state: "pending_confirmation"`, `"rejected"`, or `"expired"` with `windows: []`, or `state: "completed"` with terminal rows `{title, capsule_id, state}`. While the core is executing confirmed jobs, `plan_result` returns `unavailable`; poll again or inspect status.

A completed plan can contain `recording`, `stopped`, `sent`, and closed window failure reasons: `encoder_session_limit`, `capture_start_unavailable`, `window_unavailable`, `window_titles_identical`, `window_changed`, `folder_not_authorized`, `disk_write_failed`, `capsule_limit`, `already_recording`, `encoder_unavailable`, `obs_not_ready`, and `unavailable`.

Top-level errors are `folder_root_required`, `folder_name_invalid`, `no_discord_windows`, `window_titles_identical`, `window_unavailable`, `obs_not_ready`, `plan_pending`, `plan_unknown`, and `unavailable`. The folder argument is one ASCII segment matching `^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$`, bounded to 64 characters. Root selection and canonical-root/reparse validation belong to the native authority; MCP never receives or chooses the full recording path.

The private operator control schema is [`organizer_control.v1.json`](../schemas/organizer_control.v1.json). `organizer_plan_confirm`, `organizer_plan_reject`, and `set_auto_anchor`, and `set_ask_before_ai_actions` are local operator actions, never valid broker operations. The broker request schema is [`organizer_request.v1.json`](../schemas/organizer_request.v1.json).

## Durable action attribution and recorder pipeline

Capsule creation and pairing use durable `control` envelopes under the `organizer-` request namespace. Folder selection and recording start/stop use confirmed intent/result records under the `organizer.` namespace before creating subfolders or starting [`core/src/ffmpeg_recorder.rs`](../../core/src/ffmpeg_recorder.rs). Four-feed isolation is covered in [`core/src/organizer.rs`](../../core/src/organizer.rs) (`assert_four_feed_failure_isolation`) and verified with four concurrent windows on Windows hardware.

## Integration checks

Run the MCP integration test suites from the repository root:

```sh
python3 -B -m unittest discover -s tests/integration -p test_mcp_organizer.py
python3 -B -m unittest discover -s tests/integration -p test_mcp_control*.py
python3 -B -m unittest discover -s tests/integration -p test_mcp_server.py
```

Proposal replies with `executing` mean the existing effect path was dispatched; `completed` means terminal per-window results are available, including failures. Neither means all windows succeeded. Always poll `plan_result`. The wall reports fixed action text and counts, never window-title markup. No stream-start, Twitch Go-live, arbitrary-path, secret-access, or remote preference-setting capability is added.
