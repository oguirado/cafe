# Café

## Inspiration

A streamer with guests on Discord gets one mixed picture, or asks every guest to install something and upload files afterwards. Clean per-guest footage is what makes a highlight, a split-screen or a good stream layout possible, and guests should not have to do anything for it.

## What it does

Café is a Windows desktop app for the host of a call. Guests share on Discord as usual and install nothing.

- **Add window:** pick a popped-out call window and it becomes a live tile on a video-call style wall.
- **Record all:** starts a separate file per selected window using the graphics card's hardware encoder. The measured output was 1920x1080 at 30 fps [MEASURED].
- **Stays out of the way:** in a verified sample, Discord pop-outs kept painting and recording behind a maximized Café. See the known limits below before testing.
- **Twitch chat, kept locally:** Café joins a public channel's chat read-only, with no login and no token, and stores received messages as typed JSONL on your PC.
- **Ask your AI:** Café is a local MCP server, so Codex can check the session, read the chat and search it through bounded, typed tools.

## How we built it

Rust owns all state and capture: Windows Graphics Capture frames are paced and piped to FFmpeg. Python holds the Twitch and OBS connectors and the MCP server. Electron with checked JavaScript draws the wall. Windows, titles and chat text are treated as untrusted data, and AI requests pass through the same validation as a click. An optional Ask me before AI actions setting adds a Confirm click inside Café.

## How we used Codex

Codex was the main builder. The main thread planned the units, wrote and tested code, and integrated work from a fleet of models running in separate worktrees. On the final day a Codex instance on Windows drove the real app and wrote evidence reports, while another fixed bugs with regression tests. Claude Code also took part as an orchestrator: it reviewed, integrated and ran the Windows gates. Codex is also a user of the product: it talks to Café over MCP to read status, list windows and read stored Twitch chat. Recording from Codex with the app window open remains unverified as a reliable flow.

## Challenges

- Chromium windows reported a content size smaller than their capture size, which looked like a resize and prevented frames from being recorded.
- Discord stopped painting a fully covered window, so a maximized app turned recordings black. Adjusting Café's opacity allowed recording behind Café in the verified sample.
- A strict frame pacer stopped recordings on stalls, and still windows were judged frozen. Changes addressed those cases, but some recordings still stopped unexpectedly during final testing. The cause was unknown before the deadline.

## Accomplishments

These results were verified on a single MSI laptop with a GTX 1060, an i7-8750H, 16 GB RAM and Windows 10 [MEASURED]:

- A pair of real Discord pop-outs recorded with Record all behind a maximized Café: 1,930 and 1,934 frames in about 64 seconds, at 1920x1080 [MEASURED], with no black frames in the sample we reviewed.
- Four windows recorded at once: one Discord pop-out and three test windows [MEASURED].
- One capsule sent to real OBS with one click, creating its scene.
- Real Twitch chat stored through the packaged connector on a public channel.

These are bounded observations, not a guarantee of uninterrupted recording. Use [the judge and AI guide](../docs/JUDGES.md) to confirm the behavior on your machine.

## What we learned

Measure on the real machine. Our most important bugs only appeared with real Discord windows on a real laptop, and our tests passed without them.

## Known limits

Windows only. Recordings are video only, with no audio. A hardware encoder is required, with NVIDIA NVENC the only one measured. A minimized or resized pop-out cannot be recorded, and off-screen windows or other virtual desktops do not work. Pop-outs must stay not fully covered by other apps; only Café itself may sit on top of them. The release ZIP is unsigned.

Some recordings stopped unexpectedly in final testing; the cause was unknown before the deadline. The workaround is **Stop all**, close Café fully, then reopen it. Reopening clears the Twitch channel and OBS setup: enter them again in **Settings**, then restart the Codex session and repeat the test.

Older builds may still show **Chat setup pending** while chat is working. MCP may report the encoder as **not ready** before the first recording. Neither status alone confirms that a recording has failed; check the app and the output file.

Testing is not fully covered beyond the laptop described above. Lag is possible. The demo footage used remote desktop into OBS on another machine, which may add visible lag absent from Café's own files; no cause was proven. Repeated OBS use, reliable Codex recording with the app window open, other GPUs and long runs have not been proven working. Recording at 60 fps [TARGET: unimplemented mode] is not supported in this release; the measured output is 30 fps [MEASURED].

## What's next

Higher frame rates, reliable OBS scene switching, and Codex running full recording sessions from a prompt. These are development goals, not verified capabilities.

## Testing instructions

Start with [docs/JUDGES.md](../docs/JUDGES.md). It is the installation and testing guide for judges and their AI assistants, including the exact FFmpeg build and recovery steps.

Requirements: Windows 10 or 11 [TARGET], a GPU with a hardware H.264 encoder (only NVIDIA NVENC has been measured), and Discord for the full flow. The actual measurements used Windows 10 [MEASURED].

1. Download `cafe.zip` from the release linked in the guide and unzip it. It is unsigned, so Windows may warn you.
2. Follow the guide to install the exact FFmpeg build. Keep both required hash checks: the downloaded archive and the installed executable. FFmpeg is not bundled.
3. Run `Cafe.exe`. The first launch took about 25 seconds on the measured laptop [MEASURED].
4. Open any visible window or a Discord pop-out. Click Add window, then Record all, wait, then Stop all.
5. Open each resulting MKV in `Videos\Cafe` and check the footage. A missing file or incomplete recording is a failed or incomplete test.
6. In Settings, enter a public Twitch channel and click Configure chat. Chat rows are stored locally, with no login.
7. To let an AI guide you, point it first at [docs/JUDGES.md](../docs/JUDGES.md), including its AI assistant instructions, and then at [AGENTS.md](../AGENTS.md) if code changes are needed. The chat test requires no passwords, tokens or stream keys.

## Codex sessions

Main build thread: `01a16d13-4b19-7170-acd0-15d76b88d4f7`

Additional Codex threads used in the project:

- `01a19a04-26cf-7aa0-b64b-7bb02328180a`
- `01a199ea-6224-71a2-bafc-9ff1a8ed301b`
- `01a199e7-13a2-79f1-8784-0a66f58c8220`
- `01a199b3-206e-7a91-855d-f1377840eaa9`
- `01a199a7-ca92-7471-9b6e-2b6989810602`
- `01a19d90-a5c7-77e3-9f82-981b584c2840`
- `01a19dd1-c4e1-75d3-9dcd-6d5f0af212a4`
- `01a19dce-78fb-7fa1-bcc4-fc4dbc6721a3`
- `01a19dce-2713-7d42-8108-4c32864c0f0d`
- `01a19dcd-df9d-77b3-9833-64e41b9a593a`
- `01a19dab-2ded-7fa1-8b95-c2149d2c84f0`
- `01a19dc5-f71b-7d22-af9d-c45985a64cdb`
