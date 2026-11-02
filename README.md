# Café

Café is a Windows desktop tool for creators and Twitch streamers, built with OpenAI Codex (Sol 6.1) [CITED]. Guests share on Discord as usual; Café captures popped-out windows as capsules with live previews, records feeds through Windows Graphics Capture and FFmpeg/NVENC, and stores Twitch chat as JSONL readable over MCP.

**Judges and AI assistants:** start with the [install and test guide](docs/JUDGES.md), including limitations and recovery steps.


**Built for the Codex x TwitchCon Hackathon** by OpenAI and Twitch. **Demo video:** https://www.youtube.com/watch?v=MgCl3-jeYTk

## Measured scope and limits

- **Hardware [MEASURED]:** Only the MSI laptop with GTX 1060, i7-8750H, 16 GB RAM and Windows 10 was tested.
- **Recording [MEASURED]:** Real Discord pop-outs produced valid MKV files, including simultaneous capture behind a maximized Café. Live tile previews worked. These short tests do not establish sustained reliability.
- **Known failure [MEASURED]:** Final tests had unexpected recording stops; the cause remains unknown. See the [guide's recovery steps](docs/JUDGES.md). Lag is possible; remote desktop capture in the demo may add lag, without a proven cause.
- **Real OBS [MEASURED]:** One capsule sent with the OBS button created its scene, in one run.
- **Unverified:** Repeated OBS use, reliable Codex recording with the app open, higher frame rates, other GPUs and long runs.

- **Occlusion [MEASURED]:** Another opaque window fully covering a Discord pop-out can cause black preview/recording. Do not minimize or resize recorded windows.

## Running & MCP registration

Select a recording folder, pop out your Discord call window, and use **Add window** / **REC**. For MCP setup, read the [judges guide](docs/JUDGES.md). Direct AI execution and **Ask me before AI actions** are implemented; their current Windows behavior is not fully verified. The local switch requires **Confirm** when enabled; MCP cannot change it. Recording state, **Stop all** and per-capsule **Stop** remain available.

## Router status

Context status: **router available (uncalibrated)**. Uncalibrated warning: no Café parameters have been fitted; `params.json` is absent. `--require-qualified` fails even when structural checks pass. DEV/TEST calibration is after the demo. Rebuild:

```sh
python3 -B tools/build_cafe_nodes.py
python3 -B tools/resolve_cafe.py 'cafe_core::capsule_registry::decode_envelope'
python3 -B tools/resolve_cafe.py --route 'capsule registry' --ack-uncalibrated
```

`catalogue/routes.tsv` lookup is consumed by `tools/resolve_cafe.py`. Agents: `python3 tools/route_cafe.py locate "<symptom>"`, see `route/README.md`.

## Development & CI

Prerequisites [CITED]: Rust 1.97.0, Python 3.12.3, Node 24.14.0, TypeScript 5.9.3 (`checkJs` only; no authored TypeScript), tokenizer in `requirements-ci.txt`.

```sh
python3 -m pip install --require-hashes -r requirements-ci.txt
npm ci --ignore-scripts
cargo fetch --locked
python3 -B tools/run_ci.py
python3 -B tools/clean_export_test.py
python3 -B tools/install_pre_push.py
```

Before push: `python3 -B tools/verify_clean_clone.py`. CI runs Rust, Python, Node, checkJs, integration, router, publication lint, claims, owned-window WGC (**not available on hosted runner** when absent) and Gitleaks scans. See [acceptance](docs/acceptance/README.md), [SECURITY](SECURITY.md), [CONTRIBUTING](CONTRIBUTING.md), [conduct](CODE_OF_CONDUCT.md), [router](router/companions/router_index.md), [AGENTS](AGENTS.md), and [MIT](LICENSE).
