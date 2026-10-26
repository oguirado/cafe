# Contributing to Café

## Project status

Café is in development validation: an unfinished product, not a
finished application. The Rust core in `core/` is the authority; the
Python connectors and MCP broker in `connectors/` use standard-library
fixtures; the shell modules in `src/` are JavaScript with JSDoc checked
by the TypeScript checker. There is no authored TypeScript and no SQL.

Read these limits before claiming any result:

- Node fixture success is not packaged Electron success.
- Native Windows capture on Linux is explicitly unsupported.
- Cross-compilation to Windows proves type checking only.
- Real Discord capture, occlusion, source clock, budget, and Windows IPC
  acceptance remain pending.
- The hosted Windows workflow runtime is unexecuted locally and may
  fail honestly for unsupported IPC or native conditions. Never claim a
  green Windows run or a native pass.
- Synthetic WGC availability is separate and may explicitly be
  unavailable on the hosted runner.
- The manual receipt template will exist at `docs/acceptance/README.md`.

## Ground rules

- Everything written to disk is English: code, comments, docs, and
  messages.
- The operator alone commits. Repository creation, push, visibility,
  and settings changes need the operator's explicit GO naming the act;
  these documents never authorize them.
- The operator has chosen the MIT license and may veto before the first
  push.
- Dependencies and versions need explicit approval. No unauthorized
  installs, and no new dependency without operator approval.
- Never commit, log, or paste credentials, tokens, or private data.
  Never pass raw handles or tokens to the renderer or to MCP.

## How to propose a change

The repository has not been created remotely yet, so there is no public
pull request flow. Coordinate through the existing private channel
agreed with the operator before preparing a change, and keep the change
to one unit: no more than three files or one concern, with its gate and
its negative case. See `CODE_OF_CONDUCT.md` for how we work together
and `SECURITY.md` for how to report a vulnerability.

## Commands

Repository commands and controllers are composed during the build and
validated before publication; they are not claimed to have executed on
GitHub. Use these exact shapes with no invented extra flags:

```sh
python3 -B tools/run_ci.py
python3 -B tools/clean_export_test.py
python3 -B tools/verify_clean_clone.py
python3 -B tools/install_pre_push.py
npm ci --ignore-scripts
npm run check
cargo test --offline --locked
cargo check --target x86_64-pc-windows-msvc --offline --locked --tests
```

See `README.md` for the exact quick start and CI profile.

## Development tooling profile

Development tooling only, not a claim that packaged runtime baselines
are implemented: Rust 1.97.0, Python 3.12.3, Node 24.14.0, and the
TypeScript 5.9.3 checker. The tiktoken 0.13.0 tokenizer is CI tooling,
not a connector product dependency.

## Pre-push guard

Pre-push source lives at `tools/hooks/pre-push`. Install it with:

```sh
python3 -B tools/install_pre_push.py
```

The guard fails closed on publication lint or claims failures, on a
missing or stale real clean-clone receipt, or when secret scanning is
unavailable or failing. Never bypass it with `--no-verify`.

The current clean-export gate is accurately an exact prepared-inventory
export WITHOUT `.git`; it is not a committed clean clone. After the
operator's first commit, the real local clone gate must run and produce
a private `.git` receipt bound to HEAD and tree before push. Gitleaks
is pinned at 8.18.4 in CI; these documents do not authorize downloading
a local binary.
