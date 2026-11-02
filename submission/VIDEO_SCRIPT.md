# Café — 180-second live demonstration script

Production direction: eight consecutive segments cover about 180 seconds (3 minutes) of real captured footage from the packaged Windows application. No synthetic cards or simulated recordings are used. Keep Discord pop-out windows uncovered during capture so Discord continues rendering frames. `<CODEX_SESSION_ID>` remains an unfilled session placeholder in documentation examples.

Source context: the [project overview](../README.md), [acceptance runbook](../docs/acceptance/README.md), and [MCP organizer guide](../docs/bridge/CODEX_ORGANIZER.md).

## 000-020 seconds — 1. Pop out Discord call windows

Visual: Real Windows desktop with a Discord call; the operator pops out the Discord call window and keeps it visible on screen without covering it completely.

> Guests join through Discord as usual. We start by popping out the Discord call window on Windows and keeping it uncovered on screen so Discord continues painting live frames for capture.

## 020-040 seconds — 2. Open Café and show the empty wall

Visual: Open the packaged Café desktop application on Windows, showing the clean empty capsule wall with the output root folder configured.

> Next, we open Café. The capsule wall starts empty, with our recording root folder already selected inside Café.

## 040-065 seconds — 3. Ask Claude or Codex over MCP to anchor and record

Visual: Switch to the MCP assistant CLI (Claude Code or Codex) connected via `resources\python\python.exe` with `-I -B resources\connectors\cafe_mcp.py`, and enter the prompt.

> In our MCP assistant, we say: "Anchor my Discord windows to Café and record them in folder Demo 01." Over MCP, the assistant checks organizer readiness, lists the windows, classifies the Discord pop-out, and submits a 60-second recording plan for folder Demo 01.

## 065-100 seconds — 4. Confirm the AI plan, live preview in tiles, and recording timers

Visual: The AI plan confirmation panel appears inside Café showing the target pop-out and subfolder `Demo 01`. The operator clicks Confirm once. The capsule tile appears on the wall with a live video preview of the pop-out, and recording starts with an active per-tile timer.

> A confirmation panel appears right inside Café showing the proposed window and folder Demo 01. With one click on Confirm, Café creates the capsule, anchors only the Discord pop-out, renders its live preview inside the capsule tile, and starts hardware-encoded NVENC recording with a live timer.

## 100-125 seconds — 5. Stop recording, open the folder, and play the MKV file

Visual: Click stop in Café, open the `Demo 01` subfolder on disk, and play back the recorded MKV file showing smooth captured video and clean stop.

> When we stop the recording, FFmpeg flushes and closes the container cleanly. Opening folder Demo 01 on disk, we play back the recorded MKV file directly to verify the captured frames.

## 125-150 seconds — 6. One-click Add window for any Windows window and per-tile REC

Visual: Click Add window in Café to anchor additional Windows applications directly from the UI, showing multiple live tiles and starting per-tile REC simultaneously (up to four windows recorded at once).

> You can also control everything directly in Café without an AI client: click Add window to anchor any Windows window in one click, preview multiple feeds side by side, and toggle REC per tile to record up to four windows at once.

## 150-165 seconds — 7. Optional: Send a capsule to OBS

Visual: Optional segment (include only if live OBS connection is verified before recording): click Send to OBS on a capsule tile to bind a single Window Capture source in OBS.

> Optionally, if OBS is connected and verified before recording, one click sends a capsule to OBS and reuses its Window Capture source without duplicates.

## 165-180 seconds — 8. Security in one sentence

Visual: Split view of the MCP assistant terminal and Café's native confirmation bar.

> Throughout every step, Café enforces one strict boundary: the AI proposes, only the operator's click runs it, and the AI never sees secrets.
