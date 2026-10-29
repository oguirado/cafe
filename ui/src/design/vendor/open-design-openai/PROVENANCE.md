# OpenDesign reference subset provenance

Source repository: [nexu-io/open-design](https://github.com/nexu-io/open-design).
Pinned source commit: `f1a0b60c6cd1c9f5c735ae5645e47244b468e71c`.
Design-system source: [pinned upstream directory](https://github.com/nexu-io/open-design/tree/f1a0b60c6cd1c9f5c735ae5645e47244b468e71c/design-systems/openai).
License: Apache-2.0; the complete supplied license is retained as `LICENSE`.

This is a curated reference fixture inspired by OpenAI, supplied by OpenDesign. It is not a claim of original OpenAI source, Café brand identity, affiliation or endorsement. The source's descriptive font and brand names are retained for audit fidelity.

| Vendored file | Supplied original source name | SHA256 | Changes |
|---|---|---|---|
| `tokens.css` | `tokens.css` | `e9a7a05bcc5bd698f43b62d9c9a3eacc135f20cee8ec9b6e433f0a8766131fec` | none; byte-identical |
| `design-tokens.json` | `design-tokens.json` | `c18bcf8bf4683c4829c8c2434d35572c13ca260fd4068892c86356d57e4a13ed` | none; byte-identical |
| `components.manifest.json` | `components.manifest.json` | `1211494cfb49212329ee03fed6da3be17bc30fcdc7f96ca4775f34d108e62dbf` | none; byte-identical |
| `DESIGN.md` | `DESIGN.md` | `8dc42258ca6c5dea1c498068b6054abdd40b9b81d1607887b5c0a3a1d788a505` | none; byte-identical |
| `USAGE.md` | `USAGE.md` | `750a19cee7b38ba68469980cf8050d6d773a48998dcde60328bed361614f2ca6` | none; byte-identical |
| `LICENSE` | `LICENSE.open-design` | `9d95806a26532623360eb84bb17d298f394b55ef73fb4c0796d99b4319b2b0da` | filename only: LICENSE.open-design renamed to LICENSE |

The five design/reference files are byte-identical. The license text is byte-identical; its supplied filename `LICENSE.open-design` is renamed to `LICENSE`. No translations, component HTML, kit HTML, preview pages, source evidence tree, logos, fonts or other assets are included in this subset. References in the original metadata to omitted fixture files remain descriptive evidence; they do not imply those files are installed.

Café-specific typography belongs in the separate override `ui/src/design/tokens.cafe.css`. Café requires that application override to use system fonts for every font family; the original vendor font-family declarations are preserved here only as reference evidence. This drop supplies no font assets, remote font loading or `@font-face` rules. Application overrides must not edit these vendored bytes.

The upstream metadata's existing generation fields are preserved as part of the byte-identical source, not new Café runtime or build claims. Per-file evidence for this provenance document and the root notice is recorded in the external delivery manifest; this file does not attempt a recursive self-hash.
