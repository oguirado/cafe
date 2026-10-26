# Router local changes

The upstream manifest `vendor_manifest.json` is retained unchanged; local
overrides are explicitly bounded to the four rows below. Every other
vendored file matches its upstream SHA256 exactly.

| File | Upstream SHA256 | Local SHA256 | Reason |
|---|---|---|---|
| tests/common.py | ef0ba3e5869fd94bc2c8fb460a28b0e360584b5d38db928478cd8715386d15a2 | 06a0786a8081a984649df3716bc46d352c0a6a9e9533b5a9de3cdcaa6114a5da | Pilot corpus path is optional via ROUTER_PILOT_REPO instead of a machine-specific absolute default; all helpers unchanged. |
| tests/test_T0_freeze.py | 1cb26a265707ac126a473fff51ad8ffbbeb89213726d1f02d0c1bcb03c632b03 | f4a9e65ca28a6df8ec9b425c54530c6d146f9403edf17477bbff623f625e3c48 | Portable frozen-hash check kept as the gate; pilot checks are separate, read ROUTER_PILOT_V1 optionally, and skip explicitly as NOT RUN when inputs are absent. |
| tests/fixtures/gates/privacy_path.txt | d90ce6d74172d8c827737249e6a8fedba15234e500eea250f60773464c8948d2 | dc6e63f5fde36cd916ec424f9f70443948be22e985e3365f0c8c761fbbc3979e | Portable negative credential marker replaces a literal home-path fixture; g_privacy still rejects it without exposing private content. |
| tools/gates_v2.py | 4a5c08b8ced43ab3dfe5a7fdeff856fbf0154c2830ceef814be5354b6ed7c1cc | 7fdacaacccaba5d361456cfce159017821857c2204fea104b86aaf5f84e045e1 | Generic privacy patterns retained; optional reviewed external hash policy replaces embedded private identities. Findings redact matched text and filenames. |

## Notes

- Hashes are lowercase SHA256 hex over final file bytes. Upstream hashes are
  copied unchanged from `vendor_manifest.json`; local hashes are freshly
  computed. This file is not hashed into itself.
- H1a changes only the current privacy implementation and negative fixture;
  frozen upstream hashes remain unchanged, and both paths keep failing closed.
- No prior notices or deviations existed; this file is new.
