# Windows PC runbook — CAFE 0.1.0, unsigned RC 0.1.0-rc.1

## 0. Read first: scope and honesty rules

- This runbook is executed by the operator on the Windows x64 PC (reference: Intel i7,
  16 GB, GTX 1060). This file makes no claim of Windows execution. No step below was run
  on Windows by the author of this file.
- The agent performs no commit, push, install, or download. Every manual install below is
  a proposal for operator review with its official URL and known version.
  A proposal is not an approval.
- BLOCKED means stop: record the fixed failure code and the required command, then stop.
  Missing prerequisites are never success.
- AUSENTE means the item does not exist in the supplied context: do not invent it.
- A `Read-Host` checkpoint is input-only. It awards no AC, passes no gate, and is not
  evidence. Only the listed artifact evidence can move an AC, and only when it is actually
  captured on the PC.
- The packaging fixture is never the product. Fixture output is never presented as native
  functionality.
- Acceptance baseline (passive, parent-validated): schema cafe.night-acceptance.v1, score
  18 of 36, head 8780a4d, native-done-live false. No AC is DONE-LIVE before this runbook
  is executed with real evidence.
- Variables, never host paths: this file uses `$ReviewedCommit`, `$Repo`, `$PythonExe`,
  `$PythonArchive`, `$WebsocketsWheel`, `$ElectronCache`, `$OutputRoot`, and repo-relative
  paths. It contains no user profile path, no machine path, and no timestamp or
  process-id examples.
- No new dependencies, no commercial fonts, no bundled or distribution FFmpeg. The RC is
  unsigned.
- Pins and commands below come from the supplied D5 context package and the Thursday
  checklist draft (passive). Presence of every file on the PC is verified by D1 itself,
  not by this file.

### Linux development gates (passive reference only)

The five existing Linux commands below are development gates only. They do not launch the
product and do not certify Windows:

- `python3 -B tools/qualify_cafe_router.py`
- `python3 -B tools/run_integration_gate.py`
- `cargo test --offline --locked --test recorder_lifecycle`
- `node --test tests/security/backendSupervisor.test.js`
- `npm run check`

### Step format

Each numbered step S0-S9 has exactly one command, its expected output, exact repository
file:line or named-test evidence, and the AC that can increase only if the required native
evidence is captured. GUI-only steps use one `Read-Host` input-only checkpoint instead of
a product CLI, because no product CLI exists for UI button actions (AUSENTE).

## S0. Toolchain manual preparation — pins and official URLs (operator review)

All rows are manual proposals for operator review. Nothing here is installed by the agent.

| Component | Operator-approved pin | Official URL | Note |
|---|---|---|---|
| Rust toolchain | 1.97.0 plus target x86_64-pc-windows-msvc | https://rust-lang.github.io/rustup/installation/windows-msvc.html | Pin source: .github/workflows/ci.yml; Cargo.toml rust-version 1.97, edition 2024 |
| MSVC Build Tools / Windows SDK | AUSENTE (exact patch pin unknown; never install an assumed latest) | https://learn.microsoft.com/en-us/cpp/build/vscpp-step-0-installation?view=msvc-170 | Desktop development with C++ workload; an existing PC install counts only after the link.exe check in S1 |
| Node | 24.14.0 | https://nodejs.org/en/download/archive/v24.14.0 | D1 accepts v24.x; keep the exact pin; no nvm/winget latest shortcuts |
| Private CPython | 3.13.16+20261013; archive cpython-3.13.16+20261013-x86_64-pc-windows-msvc-install_only_stripped.tar.gz; 21987516 bytes; SHA256 7aba3985d4cee1c7cacb963ca6aa08a5d3e695d7f9609d9fabc56f22ddcf5a06 | https://github.com/astral-sh/python-build-standalone/releases/tag/20261013 | No python.org substitute is approved by this ruling |
| PSScriptAnalyzer | Must already exist; absent means D1 BLOCKED | https://github.com/PowerShell/PSScriptAnalyzer/releases | 1.24.0 supports PS5.1+, but no download or install is approved or executed here; exact newly installed analyzer pin approval is AUSENTE; PowerShell 5.1 builtin meets the script minimum; operator records the actual analyzer version |
| Cached dependencies | Electron 44.9.2; websockets 17.2 wheel; checked package-lock.json and Cargo.lock | (repo lockfiles) | Windows offline npm and Cargo caches are required; a Linux node_modules proves nothing about the Windows native optional-dependency cache; D1 never fetches, installs, or downloads |

Step command (verification only, installs nothing):

```powershell
Get-Command git,rustup,node,npm.cmd,link.exe -ErrorAction Stop
```

Expected output: one resolved row per tool (git, rustup, node, npm.cmd, link.exe). A missing
tool raises a terminating error naming it; record TOOLCHAIN_MISSING and stop.

Evidence: tools/windows/build_rc_contract.py:69-71.

AC: none. This step is a prerequisite only.

## S1. D1 offline preflight (Windows x64, no build yet)

Command (one):

```powershell
& $PythonExe -I -B tools/windows/build_rc_contract.py --source $Repo --commit $ReviewedCommit --python $PythonExe --python-archive $PythonArchive --websockets-wheel $WebsocketsWheel --electron-cache $ElectronCache --output $OutputRoot --preflight-only
```

Run from the reviewed clean checkout at `$ReviewedCommit` (40 lowercase hex). `$OutputRoot`
must be a new directory outside the checkout. All paths are absolute local paths without
links or reparse points.

Expected output (exit 0):

```json
{"schema": "cafe.windows.rc-build.v1", "status": "preflight_pass"}
```

Any missing prerequisite prints instead (exit 2):

```json
{"code": "<code>", "required_command": "<required_command>", "schema": "cafe.windows.rc-build.v1", "status": "blocked"}
```

with `<code>` from the fixed set in source (WINDOWS_REQUIRED, TOOLCHAIN_MISSING,
REVIEWED_COMMIT_REQUIRED, CHECKOUT_ROOT_REQUIRED, REVIEWED_COMMIT_MISMATCH,
CLEAN_CHECKOUT_REQUIRED, RUST_PIN_REQUIRED, MSVC_TARGET_MISSING, NODE_24_REQUIRED,
MSVC_LINKER_REQUIRED, APPROVED_PYTHON_ARCHIVE_REQUIRED, APPROVED_PYTHON_REQUIRED,
OFFLINE_NODE_CACHE_REQUIRED, SOURCE_INVENTORY_REQUIRED, and path-shape codes). Record the
code and stop.

Checks covered: Windows platform; five tools present; rustc and cargo report 1.97.0 via
rustup; MSVC target installed; node reports v24; link.exe identifies as the Microsoft
linker (a shadowing GNU link is rejected); approved Python archive size and SHA256;
private Python reports 3.13.16; required files tools/build_windows_app.js,
tools/package_windows.py, tools/check_shell_archive.py, package-lock.json, Cargo.lock
present; node_modules directory present; source inventory from git ls-files within 1-4096
entries. Absent PSScriptAnalyzer, missing caches, and any absent required file are
BLOCKED, never success.

Evidence: tools/windows/build_rc_contract.py:66-114 (preflight), :225-254 (argument
contract, result JSON, exit codes).

AC: none alone. Preflight is the gate for S2; it moves no AC by itself. Hosted CI still
needs a new GO (C01 stays PARTIAL).

## S2. D1 offline build of the unsigned RC (Windows x64)

Command (one), run from the reviewed checkout:

```powershell
.\tools\windows\build_rc.ps1 -ReviewedCommit $ReviewedCommit -PythonExe $PythonExe -PythonArchive $PythonArchive -WebsocketsWheel $WebsocketsWheel -ElectronCache $ElectronCache -OutputRoot $OutputRoot
```

The script requires PSScriptAnalyzer to be present and its own Error/Warning analysis to
be clean; it verifies every file of the supplied private Python against
tools/windows/python_runtime_pins.json (archive 21987516 bytes, SHA256
7aba3985d4cee1c7cacb963ca6aa08a5d3e695d7f9609d9fabc56f22ddcf5a06, version
3.13.16+20261013) before invoking it, then delegates to the contract.

Expected output (exit 0):

```json
{"manifest": "manifest.json", "schema": "cafe.windows.rc-build.v1", "status": "built_unsigned_native_pending", "version": "0.1.0-rc.1"}
```

plus the manifest.json file in $OutputRoot with schema cafe.windows.rc-manifest.v1, version
0.1.0-rc.1, target x86_64-pc-windows-msvc, windows_verification pending, source_commit
`$ReviewedCommit`, and sorted file rows (path, bytes, sha256) for exactly
authority/cafe-core.exe and cafe-0.1.0-rc.1.zip. The archive_files array records every ZIP
entry with its exact relative path, byte length and SHA256. Artifact-bound validation
compares both artifact files and every archive entry to the bytes on disk before publishing
the manifest; --verify-manifest rechecks an existing manifest against its artifact directory. The build compiles the authority with
cargo offline locked for the MSVC target, runs build:preload, build:renderer, and check
offline, packages the shell, verifies the archive (Cafe.exe entry), and refuses lockfile
or checkout drift. Any failure prints the blocked shape from S1 with its fixed code (exit
2); record it and stop.

Evidence: tools/windows/build_rc.ps1:28-38 (archive and pin-set check), :80-94 (analysis
gate, private-runtime verification, contract call);
tools/windows/build_rc_contract.py:185-222 (build, package, manifest).

AC: C10 can increase only if the manifest plus the authority EXE and ZIP hashes from the
reviewed commit are recorded. windows_verification stays pending until S3. The RC is
unsigned; there is no distribution FFmpeg; no Windows binary selftest command exists
(AUSENTE).

## S3. Native app start from the unsigned ZIP (GUI-only)

UI actions: extract cafe-0.1.0-rc.1.zip into a new directory; launch the packaged Cafe.exe;
let the shell spawn its private Rust backend authority (backend/cafe-core.exe) directly
with an empty argument vector and the reviewed minimal environment; private Python is
reserved for connector processes, not for launching Rust; read all five applied fuses from the actual executable.

Checkpoint (one command; input-only):

```powershell
$StartNote = Read-Host 'Start checkpoint: ZIP extracted, Cafe.exe launched, five fuses read (type observed summary)'
```

Expected output: none (the typed text is stored in `$StartNote`). The typed text awards no
AC, passes no gate, and is not evidence.

Required artifact evidence: ZIP and EXE SHA256 values; startup log;
resources/cafe-build.json showing schema cafe.unsigned-windows-app.v1 with authority_sha256
matching backend/cafe-core.exe; the five fuse values; confirmation that no host Python was
used.

Evidence: src/main/runtimeBackend.js:42-68 (packaged backend: receipt check, authority hash
check, minimal environment); named check tools/check_shell_archive.py.

AC: C10 can move toward DONE only if the artifacts above are captured from the real
unsigned build. Fixture output presented as native functionality is forbidden.

## S4. Discord pop-outs: two people, game plus camera each (GUI-only)

UI actions: create two capsules, one per real person; pop out the Discord windows (game
plus camera per person); pair each window through the app window-token flow; change a
window title and confirm the identity persists; close and recreate a window and confirm
reconfirmation is required.

Checkpoint (one command; input-only):

```powershell
$DiscordNote = Read-Host 'Discord checkpoint: two capsules paired, title change kept identity, recreate needed reconfirmation (type observed summary)'
```

Expected output: none (input stored in `$DiscordNote`). The typed text awards no AC, passes
no gate, and is not evidence.

Required artifact evidence: observed window snapshots (three per page with offsets);
pairing results (window_paired or the exact rejection code); identity-persistence and
reconfirmation records. Published evidence must contain no raw window handle, password, or
token values.

Evidence: core/src/runtime_main.rs:23-26 (window paging), :45-61 (ListWindows refresh,
PairWindow proposal, durability, reconfirmation).

AC: C02 (two real people on Discord) and C03 (capture binding) can increase only if the
artifacts above are captured.

## S5. WGC native capture path (GUI-only)

UI actions: run the in-app native window enumeration; observe live game and camera frames
through the Windows capture path; record the observed resolution (1080p when offered).

Checkpoint (one command; input-only):

```powershell
$WgcNote = Read-Host 'WGC checkpoint: native enumeration refreshed, live frames observed (type observed resolution)'
```

Expected output: none (input stored in `$WgcNote`). The typed text awards no AC, passes no
gate, and is not evidence.

Required artifact evidence: windows_refreshed result with snapshots; observed resolution.
Native capture is explicitly unsupported outside Windows (snapshot platform unsupported);
Linux behavior proves nothing here.

Evidence: core/src/runtime_main.rs:23-29 (paging and snapshot: platform,
recording_configured), :45 (enumeration success versus native_enumeration_unavailable).

AC: C03 can increase only if the artifacts above are captured. The ten-minute Discord
recording itself is evidenced in S6.

## S6. External FFmpeg plus GTX1060 NVENC plus ten-minute recording (GUI-only)

UI actions: in the native dialog select the external operator-approved FFmpeg (path plus
SHA256 recorded); confirm the authority reports hardware_preflight_required and then
configured; run the real inventory plus trial encode on the GTX1060 (h264_nvenc; no
software fallback exists); record ten minutes at the delivered resolution; stop through
the app; exercise a managed child failure and a simulated failed or full destination
through the tested failure path.

FFmpeg approval: the audit candidate 8.1.3 without libx264 still needs operator binary
approval and audit. No FFmpeg is bundled, downloaded, or distributed with the RC.

Checkpoint (one command; input-only):

```powershell
$RecordNote = Read-Host 'Recording checkpoint: approved FFmpeg pinned, NVENC trial passed, ten minutes recorded (type observed summary)'
```

Expected output: none (input stored in `$RecordNote`). The typed text awards no AC, passes
no gate, and is not evidence.

Required artifact evidence: observed encoder and argv policy; frame timing with duplicate,
drop, and backpressure counters; bounded memory; decodable files; durable receipts. On the
failure path: partial data retained, interrupted or blocked state visible, no false
complete.

Evidence: src/main/runtimeBackend.js:70-76 (native dialog selection plus digest);
core/src/runtime_main.rs:63 (start blocked unless Windows with configured FFmpeg), :66
(ConfigureFfmpeg select versus blocked code); named Linux test
core/tests/recorder_lifecycle.rs and named source core/src/runtime_recording.rs
(development reference only, not native evidence).

AC: C04 can increase only if the artifacts above are captured from the audited hardware
encoder run.

## S7. Real OBS over obs-websocket v5 (GUI-only)

UI actions: authenticate against the actual OBS 28+ over obs-websocket v5 on loopback
127.0.0.1 (password typed in the UI, never passed as argv and never written to logs); send
both role windows with the reviewed camera crop; repeat the operation and confirm
idempotence.

No CLI exists for these UI button actions (AUSENTE); do not invent one.

Checkpoint (one command; input-only):

```powershell
$ObsNote = Read-Host 'OBS checkpoint: real OBS pixels visible, crop reviewed, repeat idempotent (type observed summary)'
```

Expected output: none (input stored in `$ObsNote`). The typed text awards no AC, passes no
gate, and is not evidence.

Required artifact evidence: visible source pixels; no duplicate sources; repeated
operation idempotent; wrong crop or readback stays disabled; password absent from logs and
journal.

Evidence: named tests tests/integration/test_obs_bridge.py and
tests/integration/test_obs_crop.py (protocol, source, crop; not native evidence);
core/src/runtime_main.rs:65 (send blocked unless OBS ready), :67-74 (ConfigureObs
durability and pending configuration).

AC: C05 can increase only if the artifacts above are captured against real OBS pixels with
a live runtime binding.

## S8. Twitch anonymous read plus JSONL (GUI-only)

UI actions: explicitly choose the demo channel in the app (operator-selected, anonymous
read-only); collect the official IRC WebSocket traffic; store the JSONL.

Checkpoint (one command; input-only):

```powershell
$ChatNote = Read-Host 'Chat checkpoint: demo channel chosen, official traffic collected to JSONL (type observed summary)'
```

Expected output: none (input stored in `$ChatNote`). The typed text awards no AC, passes no
gate, and is not evidence.

Required artifact evidence: stored JSONL under CAFE_RUNTIME_ROOT/session-<epoch>/chat;
observed traffic counters. RuntimeStorage creates that session chat directory
(core/src/runtime_storage.rs:38-42). Unknown and idle
states are reported as-is; they are never fabricated into success.

Evidence: core/src/runtime_main.rs:29 (chat_available, chat_configurable, chat_running
projection), :75-81 (ConfigureTwitch durability, start_chat, chat_already_running
rejection).

AC: C06 can increase only if the artifacts above are captured from the live
operator-selected channel.

## S9. Codex stdio MCP: six read tools (official syntax)

Configure the actual stdio read server in Codex with the official checked syntax
(https://learn.chatgpt.com/docs/extend/mcp?surface=cli):

```text
codex mcp add <name> -- <stdio command>
```

For the private Python in isolated mode, the stdio command uses an explicit trusted
bootstrap sys.path argument pointing at the packaged resources/connectors, not system
PYTHONPATH (ignored under -I). There is no npx, pip, or MCP SDK dependency.

Step command (one; verification):

```powershell
codex mcp list
```

Expected output: the list names the configured cafe server. An empty list or an unknown
name means not configured; record that and stop.

Configure --data with the existing session chat DIRECTORY
(CAFE_RUNTIME_ROOT/session-<epoch>/chat), never an individual JSONL file.
Then ask the six supported typed tools returned by the MCP list-tools reply over the S8 JSONL inside
Codex (tool names are taken from the live the MCP list-tools reply reply, never assumed).

Required artifact evidence: the six real tool replies tied to the session (payload plus
scan stats). The list output alone awards no AC.

Evidence: named tests tests/integration/test_mcp_server.py (six read tools, typed calls,
bounded replies) and source connectors/cafe/mcp_server.py (stdio serve and --data directory
entry point); core/src/runtime_storage.rs:38-42 (session chat directory).

AC: C07 can increase only if the six real tool replies are captured.

MCP controls stay BLOCKED in the default deployment: the component tools require an
injected trusted control plane; that injection is absent from the packaged runtime.
mcp_control_available is false
(core/src/runtime_main.rs:29), and the native Rust local confirmation bridge does not
exist. This runbook teaches no approval bypass and creates no token files. C08 cannot move
until the trusted local approval bridge exists and its end-to-end confirmation flow is
evidenced.

## Renderer lifecycle and final acceptance (notes, not steps)

- Reload the UI while an accepted authority operation is active. Required evidence: same
  authority process, current epoch and revision snapshot, no duplicate recorder or service
  owner. This supports final acceptance; it carries no separate AC.
- Before filming: recheck publication, claims, and secret scans; store the native
  checklist results; recalculate acceptance from evidence only.
- Still pending after S0-S9: C08 (MCP controls and end-to-end confirmation), C09 (product
  controls integration; the accepted UI control source is unlinked), C11 (final
  secret-scan repeat and final hero), C12 (recorded demo and selected session ID).
- Never represent a test fixture as native functionality. Every pending item stays
  explicit.

## Step to AC map

| Step | Command kind | Evidence anchor | AC if native evidence captured |
|---|---|---|---|
| S0 toolchain prep | Get-Command verify | tools/windows/build_rc_contract.py:69-71 | none (prerequisite) |
| S1 D1 preflight | contract --preflight-only | tools/windows/build_rc_contract.py:66-114,225-254 | none alone (gate for S2) |
| S2 D1 build | build_rc.ps1 | tools/windows/build_rc.ps1:80-94; tools/windows/build_rc_contract.py:185-222 | C10 partial (pending until S3) |
| S3 native start | Read-Host (input-only) | src/main/runtimeBackend.js:42-68; tools/check_shell_archive.py (named) | C10 toward DONE |
| S4 Discord pop-outs | Read-Host (input-only) | core/src/runtime_main.rs:23-26,45-61 | C02, C03 |
| S5 WGC capture | Read-Host (input-only) | core/src/runtime_main.rs:23-29,45 | C03 |
| S6 FFmpeg + NVENC + 10 min | Read-Host (input-only) | src/main/runtimeBackend.js:70-76; core/src/runtime_main.rs:63,66; core/tests/recorder_lifecycle.rs (named) | C04 |
| S7 real OBS | Read-Host (input-only) | tests/integration/test_obs_bridge.py; tests/integration/test_obs_crop.py; core/src/runtime_main.rs:65,67-74 | C05 |
| S8 Twitch + JSONL | Read-Host (input-only) | core/src/runtime_main.rs:29,75-81 | C06 |
| S9 Codex MCP reads | codex mcp list | tests/integration/test_mcp_server.py (named); core/src/runtime_storage.rs:38-42 | C07 (C08 BLOCKED) |
