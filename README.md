# Café

Café is a Windows desktop tool for creators and Twitch streamers, built with OpenAI Codex (Sol 6.1). Guests share on Discord as usual; Café captures popped-out Discord call and desktop windows as capsules with live tile previews, records each feed via Windows Graphics Capture and FFmpeg/NVENC, lets AI assistants propose recording plans over MCP that run only after clicking **Confirm**, sends capsules to OBS, and stores Twitch chat as JSONL over MCP.

## Verified on Windows (GTX 1060)

- **Discord WGC + NVENC [MEASURED]:** Pop-out captured and recorded to a valid MKV (30 s, 1810 frames, clean stop).
- **Multi-window [MEASURED]:** Two and four windows at once (Discord pop-out + test windows), ~30 s valid MKVs; still windows keep recording.
- **Live tile preview [MEASURED]:** Live pop-out preview inside its capsule tile in the packaged app.
- **AI organizer over MCP [MEASURED]:** Claude Code CLI listed windows and proposed a 60 s plan with a subfolder; after one **Confirm** click Café created the capsule, anchored the pop-out, and recorded a 45 s valid MKV.
- **Manual UI & MCP resilience [MEASURED]:** One-click **Add window** and **REC**; second MCP client works and survives restarts.

## Known limits

- **Occlusion [MEASURED]:** Fully covering a Discord pop-out makes Discord stop painting it (preview/recording go black). Keep pop-outs visible.
- **Not yet verified:** Multiple concurrent Discord pop-outs, live OBS connection, and live OpenAI Codex client (same MCP registration).

## Running & MCP registration

Select your recording folder, pop out your Discord call window (not fully covered), and use **Add window** / **REC** or connect an MCP client (`resources\python\python.exe -I -B resources\connectors\cafe_mcp.py`). Tools: `organizer_status`, `list_discord_windows`, `propose_recording_plan`, `propose_stop_all`, `propose_send_to_obs`. Only the operator's **Confirm** click runs actions; the AI never sees secrets. See [MCP organizer](docs/bridge/CODEX_ORGANIZER.md) and [Devpost](submission/DEVPOST.md).

## Router status

Context status: **router available (uncalibrated)**. Uncalibrated warning: no Café parameters have been fitted; `params.json` is absent. `--require-qualified` fails even when structural checks pass. DEV/TEST calibration is after the demo. Rebuild after source changes:

```sh
python3 -B tools/build_cafe_nodes.py
python3 -B router/tools/build_index_v2.py --nodes catalogue/nodes.jsonl --out catalogue/router --corpus cafe
python3 -B router/tools/build_index_v2.py --nodes catalogue/nodes.jsonl --out catalogue/router --corpus cafe --check
python3 -B tools/resolve_cafe.py 'cafe_core::capsule_registry::decode_envelope'
python3 -B tools/resolve_cafe.py --route 'capsule registry' --ack-uncalibrated
```

`catalogue/routes.tsv` lookup is consumed by `tools/resolve_cafe.py`, rejecting stale hashes, ambiguous keys and unsafe paths.

## Development & CI

Prerequisites: Rust 1.97.0, Python 3.12.3, Node 24.14.0, TypeScript 5.9.3 (`checkJs` only; no authored TypeScript), tokenizer in `requirements-ci.txt`.

```sh
python3 -m pip install --require-hashes -r requirements-ci.txt
npm ci --ignore-scripts
cargo fetch --locked
python3 -B tools/run_ci.py
python3 -B tools/clean_export_test.py
python3 -B tools/install_pre_push.py
```

Before push, run `python3 -B tools/verify_clean_clone.py` on a clean committed tree. CI runs Rust, Python, Node, checkJs, integration, router, publication lint, claims, owned-window WGC (**not available on hosted runner** when absent) and Gitleaks scans. See [acceptance](docs/acceptance/README.md), [SECURITY](SECURITY.md), [CONTRIBUTING](CONTRIBUTING.md), [conduct](CODE_OF_CONDUCT.md), [router](router/companions/router_index.md), [AGENTS](AGENTS.md), and [MIT](LICENSE).
