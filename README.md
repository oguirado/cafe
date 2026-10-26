# Café

Café is a Windows desktop tool for Twitch streamers, built with OpenAI Codex (Sol 6.1). Rust authority, Python connector fixtures and JavaScript/JSDoc shell modules have development coverage. Production wiring, packaged Electron and real Discord capture acceptance remain pending. This is not a finished application.

Context status: **router available (uncalibrated)**. Uncalibrated warning: no Café parameters have been fitted; `params.json` is absent. `--require-qualified` fails even when structural checks pass. DEV/TEST calibration is after the demo. Never borrow another corpus's parameters. Rebuild after indexed source changes:

```sh
python3 -B tools/build_cafe_nodes.py
python3 -B router/tools/build_index_v2.py --nodes catalogue/nodes.jsonl --out catalogue/router --corpus cafe
python3 -B router/tools/build_index_v2.py --nodes catalogue/nodes.jsonl --out catalogue/router --corpus cafe --check
python3 -B tools/resolve_cafe.py 'cafe_core::capsule_registry::decode_envelope'
python3 -B tools/resolve_cafe.py --route 'capsule registry' --ack-uncalibrated
```

The generic `catalogue/routes.tsv` lookup is consumed by `tools/resolve_cafe.py`. It rejects stale source hashes, ambiguous keys and unsafe paths. An uncalibrated shortlist is not permission to edit.

## Development quick start

Prerequisites: Rust 1.97.0 with the Windows standard-library target, Python 3.12.3 and Node 24.14.0. These are development tooling versions, not proof of the planned packaged runtimes. The only npm package is TypeScript 5.9.3 as a JavaScript checker; no TypeScript is authored. Tokenizer tooling is pinned in `requirements-ci.txt`; its vocabulary is shipped with license/provenance.

```sh
python3 -m pip install --require-hashes -r requirements-ci.txt
npm ci --ignore-scripts
cargo fetch --locked
python3 -B tools/run_ci.py
python3 -B tools/clean_export_test.py
python3 -B tools/install_pre_push.py
```

The clean-export test copies exactly the prepared Git inventory without Git metadata or ignored working material, then runs the Linux CI gate profile. It uses the approved offline Cargo cache and lock-matched Node modules. This is **not** a clean-clone claim. After the operator's first commit, with a clean tree, run `python3 -B tools/verify_clean_clone.py` before push; the guard requires its fresh private Git receipt. Do not bypass the hook.

## CI and native acceptance

The workflow schedules Rust tests and Windows type checks, Python, Node, checkJs, the one-command integration gate, router checks, publication lint and claims on Ubuntu and Windows. The Linux prepared-export profile is tested locally. The hosted Windows runtime matrix is unexecuted here; known Windows IPC/native limitations must fail honestly, never be converted to success.

A separate owned-window WGC job distinguishes actual capture from **not available on hosted runner**. It does not accept live Discord behavior or production timing budgets. Gitleaks is pinned by version/checksum in CI, scans complete history and tests all three credential controls. If its local binary is absent, the scan is pending and the hook blocks; no local download is automatic.

Manual Windows scenarios and receipt template: [acceptance](docs/acceptance/README.md). See [SECURITY](SECURITY.md), [CONTRIBUTING](CONTRIBUTING.md), [conduct](CODE_OF_CONDUCT.md), [router companion](router/companions/router_index.md) and [AGENTS](AGENTS.md). License: [MIT](LICENSE), subject to operator veto before the first push. Repository creation, commits, push and settings need the operator's separate authorization.
