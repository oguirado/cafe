# Router local changes

The upstream manifest `vendor_manifest.json` is retained unchanged; local
overrides are explicitly bounded to the rows below. Every other
vendored file matches its upstream SHA256 exactly.

| File | Upstream SHA256 | Local SHA256 | Reason |
|---|---|---|---|
| tests/common.py | ef0ba3e5869fd94bc2c8fb460a28b0e360584b5d38db928478cd8715386d15a2 | 06a0786a8081a984649df3716bc46d352c0a6a9e9533b5a9de3cdcaa6114a5da | Pilot corpus path is optional via ROUTER_PILOT_REPO instead of a machine-specific absolute default; all helpers unchanged. |
| tests/test_T0_freeze.py | 1cb26a265707ac126a473fff51ad8ffbbeb89213726d1f02d0c1bcb03c632b03 | f4a9e65ca28a6df8ec9b425c54530c6d146f9403edf17477bbff623f625e3c48 | Portable frozen-hash check kept as the gate; pilot checks are separate, read ROUTER_PILOT_V1 optionally, and skip explicitly as NOT RUN when inputs are absent. |
| tests/fixtures/gates/privacy_path.txt | d90ce6d74172d8c827737249e6a8fedba15234e500eea250f60773464c8948d2 | dc6e63f5fde36cd916ec424f9f70443948be22e985e3365f0c8c761fbbc3979e | Portable negative credential marker replaces a literal home-path fixture; g_privacy still rejects it without exposing private content. |
| tools/gates_v2.py | 4a5c08b8ced43ab3dfe5a7fdeff856fbf0154c2830ceef814be5354b6ed7c1cc | c2a9c975d34a4232e8bb07817e24d743067e307282252d99f6957b78ee7dfdae | Preserve all local privacy changes; use separator-aware blind DEV path guard shared with tuning. |
| tools/route_v2.py | e0f457ef1f03e3483873b07490710dfbcc02d2d9db6a489eafecfccfc1b18e15 | 27a230910e2936fb6dd9b92aa12e90c3e71b2a8fff6b35abd71a358814c826f0 | Windows drive-aware optional priority parsing; persistent separate Windows stdlib lock around complete record append; explicit partial-write failure, Unix O_APPEND retained. Shared separator-aware blind path check; ranking, calibration and byte limits unchanged. |
| tools/tune_v2.py | 41bc0461863f4e5732028ea92c06acf645630bd850753375e1185a21bb9029f7 | a7fd37e85c2953cb7f7a0690defdc6e9e99b320449610f64e8572d11a4d65550 | Refuse native, resolved and Windows separator bench path components before reading DEV; near matches remain permitted. |

## Notes

- Hashes are lowercase SHA256 hex over final file bytes. Upstream hashes are
  copied unchanged from `vendor_manifest.json`; local hashes are freshly
  computed. This file is not hashed into itself.
- H1a changes only the current privacy implementation and negative fixture;
  frozen upstream hashes remain unchanged, and both paths keep failing closed.
- No prior notices or deviations existed; this file is new.

CLI output additionally configures real text pipes for canonical UTF-8/LF bytes while retaining existing in-memory StringIO captures, avoiding Windows text newline translation and locale encoding; determinism gate assertions remain unchanged.
