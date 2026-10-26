# extract_rust — bounded Rust public-surface extractor

<a id="extract-rust-overview"></a>
## Overview

`tools/extract_rust.py` is a tooling unit that extracts the declared public
surface of a small, explicitly listed set of Rust sources into deterministic
JSON. It performs no routing, no context composition, and no compilation.
Node sharding, index, resolver, and lookup composition belong to a separate
unit; this companion claims no router qualification, and the router remains
unavailable here.

The current entry point is `core/src/main.rs` and the crate namespace is
`cafe_core`. This narrow profile covers the current P05 source; it is not a
general Rust parser and does not accept arbitrary Rust.

<a id="extract-rust-reading-map"></a>
## Reading map

- [Overview](#extract-rust-overview)
- [Method and non-goals](#extract-rust-method)
- [CLI](#extract-rust-cli)
- [Input inventory](#extract-rust-input-inventory)
- [Output contract](#extract-rust-output-contract)
- [Errors](#extract-rust-errors)
- [Supported subset](#extract-rust-supported-subset)
- [Module resolution](#extract-rust-module-resolution)
- [Refusals (fail closed)](#extract-rust-refusals)
- [Lexer](#extract-rust-lexer)
- [Attribute allowlist](#extract-rust-attributes)
- [Configuration profiles](#extract-rust-cfg)
- [Handwritten golden oracle](#extract-rust-golden-oracle)
- [Qualification (honest scope)](#extract-rust-qualification)
- [Oracle commands](#extract-rust-oracle-commands)
- [Negative cases](#extract-rust-negative-cases)
- [Limits](#extract-rust-limits)

<a id="extract-rust-method"></a>
## Method and non-goals

The extractor is a standard-library Python lexical scanner plus a
balanced-delimiter item recognizer. It is explicitly not a full Rust parser,
compiler, macro expander, type checker, or reachability analysis. Anything
outside the supported subset below refuses instead of guessing.

<a id="extract-rust-cli"></a>
## CLI

```sh
python3 -B tools/extract_rust.py --files core/src/authority_journal.rs core/src/capsule_registry.rs core/src/journal_replay.rs core/src/main.rs
```

Optional flags:

- `--inventory <external-json-snapshot>` — explicit inventory snapshot,
  required for copies without version-control metadata.
- `--cfg-profile <external-json-profile>` — explicit configuration profile.

The repository root is the parent of the `tools/` directory. A
programmatic entry point `extract(root, paths, inventory, profile=None)`
serves explicit fixtures. There are no other inputs: no host target
inference, no default features, no environment-derived configuration.

<a id="extract-rust-input-inventory"></a>
## Input inventory

- The file list is an explicit, nonempty, sorted, unique set of canonical
  relative `.rs` paths. There is no recursive tree traversal.
- Live inventories use version-control cached entries plus non-ignored
  prepared public files. Copies without version control require an explicit
  inventory JSON snapshot; the extractor never infers or traverses a tree.
- The following refuse: symlinks and escapes, paths outside the supplied
  inventory, missing or unlisted modules, unreachable or duplicate
  module-file bindings, and ambiguous ownership.
- Source bytes are read only from the explicit path list and hashed with
  SHA-256 before strict UTF-8 decoding. Original byte and newline identity
  is preserved; source line counts use LF.

<a id="extract-rust-output-contract"></a>
## Output contract

Output is deterministic, compact, sorted-key JSON terminated with LF:

- `schema` is `cafe.rust-surface.v1`; `crate` is `cafe_core`.
- `cfg_profile` is the explicit normalized profile map.
- `files` holds one entry per input file: `{path, source_sha256,
  symbols: [...]}`. Every input file appears with its hash, including the
  root when it contributes no public items.
- Each symbol holds `{path, symbol, name, line, cfg}`, where `cfg` is the
  canonical declared configuration-expression list inherited from lexical
  modules and `impl` blocks.
- `symbol` is the qualified `crate/module/name` path; inherent methods use
  the `crate/module/Type/method` form joined by `::`.
- Output contains no timestamps, process ids, or host paths. Repeated runs
  over the same inputs are byte-identical.

<a id="extract-rust-errors"></a>
## Errors

The CLI rejects invalid input with exit status 2 and a JSON error carrying a
code, path, and line. It never prints raw host exceptions and never emits a
partial successful standard output alongside a failure.

<a id="extract-rust-supported-subset"></a>
## Supported subset

Only explicitly unrestricted declarations are recorded:

- `pub fn`, `pub struct`, `pub enum`, `pub trait`, `pub type`, `pub const`,
  and `pub mod` declarations.
- Public inherent methods.

Immutable private or restricted-visibility `static` declarations with a
non-block initializer are recognized and omitted from the public inventory.
Public, mutable and block-initialized statics remain unsupported and refuse.

Explicitly excluded by the syntactic inventory contract: fields, enum
variants, implicit trait members, and `pub(crate)`, `pub(super)`, and
`pub(in ...)` items. Private module declarations still establish qualified
names. Trait implementations over simple concrete types may be traversed
for syntax but contribute no implicit exports.

<a id="extract-rust-module-resolution"></a>
## Module resolution

- Named out-of-line and inline modules resolve solely from the explicit
  file list, using the standard `name.rs` or `name/mod.rs` candidates.
- Each source has exactly one owner; duplicate or ambiguous bindings
  refuse.
- The root must be `main.rs`. There is no filename guessing for unrelated
  files.
- An inherent `impl` block must name one simple concrete type.

<a id="extract-rust-refusals"></a>
## Refusals (fail closed)

The extractor refuses the whole run on any of the following:

- Generic or qualified inherent `impl` blocks, inner `cfg` attributes, `cfg_attr`, path
  attributes, unknown potentially expanding attributes, `extern` blocks,
  all re-exports, unions, public or mutable/block-initialized `static` items, and item-level macro
  invocations.
- Any `macro_rules!` definition, even a private one, because no reviewed
  expansion exists.
- `pub use` items, both glob and named forms.
- Unknown configuration predicates, keys, operators, or types, even in
  inactive branches. Unknown configuration never guesses from the host.

<a id="extract-rust-lexer"></a>
## Lexer

The lexer recognizes nested block and line comments, escaped strings and
characters, byte, C, and raw strings, and lifetimes, so `pub` lookalikes
inside comments or string literals never become token declarations.

<a id="extract-rust-attributes"></a>
## Attribute allowlist

Only these attributes are accepted; anything else refuses:

- `derive` restricted to `Clone`, `Copy`, `Debug`, `PartialEq`, `Eq`,
  `PartialOrd`, `Ord`, `Serialize`, `Deserialize`, `Default`, and `Hash`.
- `serde`, `allow`, `deny`, `warn`, `forbid`, `doc`, `repr`, `inline`,
  `must_use`, `non_exhaustive`, `cold`, and `cfg`.

<a id="extract-rust-cfg"></a>
## Configuration profiles

- A profile is a map from identifier keys to `bool`, `string`, or sorted
  unique string lists.
- Supported predicates: bare flags, `key = "value"`, and `all`, `any`,
  `not` combinations, evaluated only against declared keys.
- Only active declarations are emitted, with their inherited declared
  configuration; the profile is recorded in the output.
- There is no host or default-target inference. An empty profile is the
  honest profile for configuration-free source.

<a id="extract-rust-golden-oracle"></a>
## Handwritten golden oracle

The table below records the frozen P05 baseline. The test now also pins
separately handwritten journal-module declarations and all source hashes.
The explicit CLI includes those journal modules; no tree discovery is used.

The golden test is authored by hand before extraction and pins exact
qualified names with source lines. `core/src/main.rs` contributes no public
declarations. `core/src/capsule_registry.rs` contributes 20 symbols:

| Symbol | Line |
|---|---:|
| `SCHEMA` (constant) | 14 |
| `MAX_ENVELOPE_BYTES` (constant) | 17 |
| `Clock` | 25 |
| `Role` | 102 |
| `Envelope` | 110 |
| `Command` | 122 |
| `DecodeError` | 150 |
| `decode_envelope` | 293 |
| `Feed` | 306 |
| `Capsule` | 315 |
| `Proposal` | 325 |
| `Snapshot` | 336 |
| `Outcome` | 348 |
| `Receipt` | 356 |
| `Reply` | 364 |
| `Registry` | 374 |
| `Registry::new` | 385 |
| `Registry::snapshot` | 397 |
| `Registry::decode_and_apply` | 409 |
| `Registry::apply` | 416 |

Exact byte hashes are frozen separately from the independently written
names and lines. All 20 symbols are retained with no anchor-cap
truncation. The executable golden test verifies these handwritten lines
against the frozen source revision at composition time.

<a id="extract-rust-qualification"></a>
## Qualification (honest scope)

- The Rust leg passes only when its real test suite executes, is nonempty,
  and passes. A missing, failed, or empty suite fails the leg.
- The unimplemented extraction/resolver legs and the global router stay unavailable in this unit.
- Existing entry, portability, and qualification regression tests must stay
  green.
- This unit emits no node records, changes no route tables, and claims no
  router qualification: composition owns that judgment.

<a id="extract-rust-oracle-commands"></a>
## Oracle commands

```sh
python3 -B -m unittest discover -s tests/bootstrap -p test_extract_rust.py
python3 -B tools/qualify_cafe_router.py
```

These are the executable oracles. They are run by the implementer at
composition time, after the extractor and its golden tests exist; this
companion documents them but does not execute them.

<a id="extract-rust-negative-cases"></a>
## Negative cases

The following negative oracle cases are required; each runs in a temporary
copy and its executable form belongs to the implementer, not to this
companion:

- Removing `pub` from a real public function without changing the
  handwritten gold makes the golden test fail.
- `pub` lookalikes in comments and strings are never extracted.
- A `macro_rules!` definition with public expansion refuses explicitly.
- `pub use x::*` refuses explicitly.
- A non-listed file is never read, including a non-listed declared module,
  which refuses without opening the file.
- Known and unknown configuration predicates behave as specified
  (unknown refuses, even in inactive branches).
- Source hashes are computed over bytes before decoding.
- Lines and qualified symbols match exactly.
- Repeated runs are deterministic and byte-identical.
- Unsafe paths (symlinks, escapes, out-of-inventory paths) refuse.
- Lexer edge cases (nested comments, escaped and raw strings,
  lifetimes) yield no false declarations.
- The qualifier passes only on an executed, passing, nonempty selection.

<a id="extract-rust-limits"></a>
## Limits

This is a bounded syntactic inventory, not a Rust toolchain: no macro
expansion, no name resolution beyond the explicit list, no type checking,
no reachability, and no host inference. Declarations outside the allowlist
refuse rather than degrade. Findings outside the closed review list are
deferred, not marked as failures.
