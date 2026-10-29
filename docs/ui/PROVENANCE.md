# Cafe Secure Shell — Provenance (G4)

## Scope of this unit

This unit authors exactly three files: `src/main/index.js` (secure Electron
boot shell), `src/preload/index.js` (sandbox preload entry guard), and this
document. All validation, admission, IPC policy, and backend supervision
logic is supplied by read-only repository modules; this unit orders calls
and owns lifecycle only.

## Renderer status

The approved MetaViewer visual design has not been supplied. G4 makes zero
renderer changes: nothing under `src/renderer/` was added or modified, and
no visual or layout claim is made here.

## Policy sources and fuse divergence

The main-process and preload security policy adapts the pinned MetaViewer
security exemplars (`6a8eb3076fb0e3f54f56b47141a742e214728575`).

Executable fuse policy diverges from MetaViewer: instead of MetaViewer's
custom binary wire fuse patch, which is never copied here, this project
enforces fuses through the official `@electron/fuses` API version `2.1.3`.
The official `@electron/fuses` `2.1.3` package is operator-approved; its
exact verified npm SRI is
sha512-LoKJUXNiJ4JM8IIrUltSHI+8pkogaGj5wmJx81jE/Wk3g2w1/kfMbTEKNoY5kitGE8hiC12h32R/1SlywFtxXg==.
Official API fuse flips and readback replace the custom MetaViewer byte
patch. Fuse application and verification run in repository tooling against
real Windows artifacts and are separate from the source orchestration gate;
the automated archive gate passes for a Windows fixture ZIP: the executable
extracted from that ZIP satisfies the official fuse readback and embedded
ASAR integrity checks. The otherwise identical valid unfused fixture is
rejected specifically by the fuse policy. This is archive inspection on
Linux, not native Windows startup or release-desktop acceptance.

The five operator-required fuse states are supplemented by
`GrantFileProtocolExtraPrivileges=false`, independently required by the
pinned MetaViewer packaging source and its `FUSE_TABLE` mandatory policy.
The remaining Windows fuse states are preserved from the official binary.

## Existing modules versus the future production caller

Existing modules (read-only for this unit):

- `src/main/shellSecurity.js` — launch assertion, session and window
  hardening, bootstrap protocol, caller admission, snapshot redaction, and
  the closed native IPC binding.
- `src/main/backendSupervisor.js` — single-life supervisor owning at most
  one native authority child over anonymous pipes, with origin-checked
  handle and subscribe entry points.
- `src/preload/preloadSecurity.js` and `src/preload/cafeBridge.js` —
  closed transport and frozen three-method renderer facade
  (`getSnapshot`, `submitIntent`, `onSnapshot`); no raw transport crosses
  the bridge.
- `tools/build_preload.js` — approved programmatic Vite library build that
  bundles the preload guard with the unchanged bridge factory into one
  CommonJS artifact with only `electron` external.

The future production caller is the Rust authority core at
`core/src/main.rs`, pending sprint W2 integration. Until W2 binds the
actual production caller, a shell created without a backend factory reports
`unavailable` with reason `authority_adapter_unbound`, and intent
submission reports `AUTHORITY_UNAVAILABLE`. No mock authority, fixture
success state, or invented native ABI stands in for the production caller.

## Frontend dependencies

Shell and UI tooling use the pinned manifest versions, including
`electron` `44.9.2`, `vite` `8.3.4`, `@electron/fuses` `2.1.3`, and
`typescript` `5.9.3` through `npm run check` over JavaScript with JSDoc. No
TypeScript sources are authored. Frontend dependencies exclude
terminal/PTY packages and `vue-tsc`.

## Pending work and explicit non-claims

- Native Electron and Windows startup remains pending (P21).
- Source tests in this repository do not prove C08, a packaged product, or
  live Discord/OBS acceptance.
- `product_release_version`: absent — not invented here.

## Lifecycle summary

The main entry requests the single-instance lock before any window or
backend effect; a secondary instance quits without side effects. Sandbox
and scheme privileges are registered before ready; session hardening,
bootstrap protocol installation, one native IPC binding, at most one
backend start, and one hardened bootstrap window follow after ready. The
package entry invokes the shell only inside the actual Electron browser
process; ordinary Node import stays inert, and a rejected boot quits with
exit code 1 and a fixed error. Backend startup failure removes privileged
handlers and propagates only the fixed boot error. Window recreation
reuses the same backend instance, and disposal is idempotent.

## Named controller coordinates

The async shell factory returns frozen instances of the exported named class
`DesktopShellController`: a `secondary` boolean with `getWindow` and
`dispose` methods backed by the same host-only lifecycle closures. Both the
secondary and primary branches construct the class; no anonymous controller
facade is returned. Async lifecycle, ordering, admission, and disposal
behavior are unchanged.


## Design system and presentation renderer

Café vendors the Apache-2.0 open-design design system from nexu-io/open-design,
commit f1a0b60c6cd1c9f5c735ae5645e47244b468e71c, design-systems/openai.
The exact source hashes, full license and unchanged upstream files are recorded
in ui/src/design/vendor/open-design-openai/PROVENANCE.md and LICENSE.
NOTICE contains attribution. The source folder used during preparation is not a
runtime or build dependency.

The separately licensed copied shell components remain byte-identical to their
recorded Capsule UI Kit source. Café owns the semantic adapter tokens.cafe.css,
the presentation wrapper and its build/verification tools. The adapter maps
light/dark roles to vendored tokens and the dark reference kit; it uses only the
system font stack. No commercial or downloaded font is included. Café remains
the displayed identity.

The standalone renderer is compiled to a finite JavaScript/CSS resource pair.
Both bytes and their manifest hashes are checked before the privileged host
serves their exact resource URLs. Frame navigation, native IPC and permissions
retain their separate closed rules. No Node capability or raw transport is
exposed to the renderer. The Vue build fixes production constants at compile
time; it does not create a browser process global. Authored JavaScript and
script-setup JSDoc are checked without authored TypeScript. Copied component
bodies are not rewritten to satisfy the checker.

The control renderer submits named intents through the existing three-method
frozen preload. Capsule creation and a native directory selection have been
exercised against the actual Rust authority on Linux, including a renderer
reload with the same authority process. Queued requests are never displayed as
committed effects. Capture on Linux is explicitly unsupported; recording needs
native Windows capture and a preflighted approved hardware encoder. OBS and
chat controls remain unavailable until their runtime owners are connected.

The local smoke command requires an actual built authority binary and an
approved Electron installation:
Run `tools/smoke_design.js` with Node, supplying the Electron executable,
authority executable and screenshot directory through its three named options.
The tool rejects missing or reordered options.
It launches the real shell and authority, injects only the native directory
picker result as test input, creates synthetic capsules, checks reload
continuity, and captures light and dark themes. This is a Linux integration
check, not a Windows PE or native recording acceptance claim. Native packaged
Windows verification remains pending the operator's PC run.

Authored entry and wrapper: [main.js](../../ui/src/main.js), [App.vue](../../ui/src/App.vue), [cafe.css](../../ui/src/cafe.css). The SFC body is outside the current code-extractor profile and is not claimed as indexed. The privileged renderer resource loader is covered by the explicit JavaScript source profile.
