# extract_rust — bounded Rust public-surface extractor

<a id="extract-rust-overview"></a>
## Overview

`tools/extract_rust.py` is a tooling unit that extracts the declared public
surface of a small, explicitly listed set of Rust sources into deterministic
JSON. It performs no routing, no context composition, and no compilation.
Node sharding, index, resolver, and lookup composition belong to a separate
unit with a separate builder and resolver; this extractor alone never
qualifies the router, and the router globally remains available
(uncalibrated).

The compiled defaults are entry `core/src/main.rs` and crate namespace
`cafe_core`, overridable only through the explicit programmatic options
documented below. The frozen P05 symbol table in this companion is a
historical baseline, not the entire current extractor profile. This unit is
not a general Rust parser and does not accept arbitrary Rust.

<a id="extract-rust-reading-map"></a>
## Reading map

- [Overview](#extract-rust-overview)
- [Method and non-goals](#extract-rust-method)
- [CLI](#extract-rust-cli)
- [Programmatic API and root options](#extract-rust-api)
- [Input inventory](#extract-rust-input-inventory)
- [Output contract](#extract-rust-output-contract)
- [Errors](#extract-rust-errors)
- [Supported subset](#extract-rust-supported-subset)
- [Generic, lifetime, and trait rules](#extract-rust-generics)
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
outside the supported subset below refuses instead of guessing. Comments
and literals are lexed before punctuation; source is never searched with
`pub` pattern expressions.

<a id="extract-rust-cli"></a>
## CLI

Template — substitute the placeholder before running:

```sh
python3 -B tools/extract_rust.py --files core/src/authority_journal.rs core/src/capsule_pairing.rs core/src/capsule_registry.rs core/src/capture_frame_pool.rs core/src/capture_health.rs core/src/ffmpeg_recorder.rs core/src/journal_replay.rs core/src/main.rs core/src/recorder_pacer.rs core/src/windows_graphics_capture.rs core/src/windows_journal_policy.rs core/src/windows_window_registry.rs --cfg-profile <external-json-profile>
```

`<external-json-profile>` is not a real repository file and must be
substituted with an existing caller-provided flat JSON cfg map. For the
tested Unix fixture that map is `{"windows":false,"unix":true,"test":false}`.
It is not `catalogue/source_profile.json`, which is a larger manifest and
not the CLI cfg map. The full current source list contains conditional
declarations, so this command requires the explicit profile; without a
required cfg key the extractor abstains and never infers the host target.

Flags:

- `--inventory <external-json-snapshot>` — explicit inventory snapshot,
  required for copies without version-control metadata.
- `--cfg-profile <external-json-profile>` — explicit configuration profile,
  required for this full-source template.

The repository root is the parent of the `tools/` directory. The
programmatic entry point is
`extract(root, paths, inventory, profile=None, *, entry=ENTRY, crate=CRATE,
module_paths=None, include_modules=False)` for explicit fixtures. There
are no other inputs: no host target inference, no default features, no
environment-derived configuration.

<a id="extract-rust-api"></a>
## Programmatic API and root options

Default constants are `ENTRY = 'core/src/main.rs'` and
`CRATE = 'cafe_core'`. The `extract` signature is:

```py
extract(root, paths, inventory, profile=None, *, entry=ENTRY, crate=CRATE,
        module_paths=None, include_modules=False)
```

- `root` is the repository root used to resolve relative paths.
- `paths` is the explicit sorted unique list of relative `.rs` paths and
  must contain `entry`.
- `inventory` is the explicit versioned file list; every `paths` entry
  must be a member.
- `profile` is an explicit `cfg` map or `None`; it is validated and
  key-sorted, and there is no host inference.
- `entry` and `crate` override the compiled root and crate namespace;
  both must be valid explicit values.
- `module_paths` maps `"<file>::<mod>"` keys to explicit relative
  targets consumed by `#[path = "..."]` attributes; consumed entries
  must exactly equal the supplied map.
- `include_modules`, when true, adds a `module` field with the
  `::`-joined module path to each file object.

Supporting units in `tools/extract_rust.py`:

- `ExtractionError(ValueError)` with `__init__(self, code, path='',
  line=None)` and `code`, `path`, `line` attributes.
- `Token(NamedTuple)` with `text`, `line`, and `kind`
  (`'punct'`, `'literal'`, or `'ident'`).
- `tokens(text, path)`, `profile_map(profile)`,
  `cfg_value(ts, profile, path)`, `impl_owner(header, path, line)`,
  `Parser(path, text, profile, module_paths=None)`,
  `safe_path(path)`, `read_inline_inventory(root)`,
  `public_inventory(root, snapshot)`, and `main(argv=None)`.

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

- `schema` is `cafe.rust-surface.v1`; `crate` is the explicit crate
  namespace (default `cafe_core`).
- `cfg_profile` is the explicit normalized profile map.
- `files` holds one entry per input file: `{path, source_sha256,
  symbols: [...]}`. Every input file appears with its hash, including the
  root when it contributes no public items. When `include_modules` is
  true, each file entry also carries `module`.
- Each symbol holds `{path, symbol, name, line, cfg}`, where `cfg` is the
  canonical declared configuration-expression list inherited from lexical
  modules and `impl` blocks.
- `symbol` is the qualified `crate::module::name` path; inherent methods use
  the `crate::module::Type::method` form joined by `::`. Public trait
  members use the `crate::module::Trait::member` form. Every emitted symbol
  is unique; duplicates refuse.
- Symbols within each file are sorted by `(line, symbol)`.
- Output contains no timestamps, process ids, or host paths. Repeated runs
  over the same inputs are byte-identical.

<a id="extract-rust-errors"></a>
## Errors

The CLI rejects invalid input with exit status 2 and a JSON error carrying a
code, path, and line. It never prints raw host exceptions and never emits a
partial successful standard output alongside a failure. On
`ExtractionError` the envelope is `{"error": code, "line": line,
"path": path}` with sorted keys; on input-evidence failures the envelope
is `{"error": "input_evidence_unavailable"}`.

<a id="extract-rust-supported-subset"></a>
## Supported subset

Only explicitly unrestricted declarations are recorded:

- `pub fn`, `pub struct`, `pub enum`, `pub trait`, `pub type`, `pub const`,
  and `pub mod` declarations.
- Public inherent methods, including methods inside bounded single-generic
  or single-lifetime `impl` headers described below.
- Public trait members: `fn`, `type`, and `const` items declared inside
  `pub trait Name` are emitted as `Trait::member` without needing an
  item-level `pub` keyword.

Immutable private or restricted-visibility `static` declarations with a
non-block initializer are recognized and omitted from the public inventory.
Public, mutable and block-initialized statics remain unsupported and refuse.

Explicitly excluded by the syntactic inventory contract: fields, enum
variants, `pub(...)` restricted items such as `pub(crate)` and
`pub(super)`, private trait members, and trait-implementation members.
Private module declarations still establish qualified names. Trait
implementations over bounded shapes may be traversed for syntax but
contribute no implicit exports.

<a id="extract-rust-generics"></a>
## Generic, lifetime, and trait rules

The `impl` header recognizer is bounded and lexical. It accepts at most
one generic parameter, and every other generic shape refuses:

- An `impl` header accepts either no parameters, one generic type
  parameter (`<T>` or `<T: Bound>`), or one lifetime parameter
  (`<'a>`).
- A type parameter may carry one plain, possibly `::`-qualified trait
  bound without arguments, lifetimes, or `+` combinations.
- The inherent self type is a single unqualified owner identifier,
  optionally parameterized by the declared generic parameter or the
  anonymous lifetime `'_` (`Owner`, `Owner<T>`, `Owner<'a>`,
  `Owner<'_>`).
- Trait paths in `TraitPath for Owner` shapes may be `::`-qualified and
  may carry one bounded argument matching the declared parameter, `'_`,
  or a single concrete identifier. Concrete trait arguments are allowed
  only on the trait side, never on the self side.
- Accepted shapes include `impl<S: Source> Pipe<S>` emitting
  `Pipe::tick`, `impl Dispatch<'_>` emitting `Dispatch::complete`, and
  `impl<'de> serde::de::Visitor<'de> for Visitor`, which is accepted
  without exporting its trait-implementation methods.
- Inside `pub trait Foo`, items run in public-trait context and are
  emitted (`Source::take`, `Visible::Item`, `Visible::LIMIT`,
  `Visible::show`). Inside `impl Trait for Owner`, items run in
  trait-implementation context: unannotated items are silently ignored
  and `pub` items refuse.
- Private traits emit nothing: `trait Private { fn hidden(&self); }`
  contributes zero rows, while `pub trait Visible` emits the trait and
  all its `fn`, `type`, and `const` members.

The following refuse with `unsupported_impl`: `where` clauses, `const`
generics, multi-parameter headers, nested or associated bounds, compound
`+` bounds, lifetime bounds on type parameters, default type parameters,
concrete self specialization such as `impl X<u32>`, qualified self
owners such as `impl module::X`, multiple `for` keywords, and
semicolon-terminated `impl` declarations.

<a id="extract-rust-module-resolution"></a>
## Module resolution

- Named out-of-line and inline modules resolve solely from the explicit
  file list, using the standard sibling-file or directory-mod-file naming
  alternatives for the declared module name.
- Each source has exactly one owner; duplicate or ambiguous bindings
  refuse.
- The walk starts at the explicit `entry` (default `core/src/main.rs`).
  There is no filename guessing for unrelated files.
- An inherent `impl` block names one unqualified owner, optionally
  parameterized only by the bounded single generic parameter or `'_`
  form above.
- `#[path = "..."]` targets resolve only through the explicit
  `module_paths` map; consumed mappings must exactly equal the supplied
  map, and unmapped or non-`mod` path attributes refuse.
- Walked modules must exactly match the explicit path set; every path
  must be in the inventory and resolve without symlinks.

<a id="extract-rust-refusals"></a>
## Refusals (fail closed)

The extractor refuses the whole run on any of the following:

- Unbounded `impl` headers: `where` clauses, `const` generics,
  multi-parameter lists, nested or associated bounds, compound bounds,
  default parameters, concrete self specialization, qualified inherent
  owners, and malformed headers (`unsupported_impl`).
- `pub` items inside `impl Trait for Type` bodies
  (`unsupported_public_trait_impl`).
- Inner `cfg` attributes, `cfg_attr`, unmapped path attributes, unknown
  potentially expanding attributes, `extern` blocks outside the one
  bounded disabled-private shape, all re-exports, unions, public or
  mutable/block-initialized `static` items, and item-level macro
  invocations.
- Any `macro_rules!` definition, even a private one, because no reviewed
  expansion exists.
- `pub use` items, both glob (`glob_reexport`) and named
  (`unsupported_reexport`) forms.
- Unknown configuration predicates, keys, operators, or types, even in
  inactive branches. Unknown configuration never guesses from the host.
- Lexer and syntax failures: unterminated or unsupported literals,
  raw identifiers, non-ASCII code, unbalanced delimiters, and invalid
  UTF-8.
- Inventory and module-closure failures: unlisted sources, symlink or
  missing sources, ambiguous module owners, non-explicit or ambiguous
  module files, unused or missing explicit module paths, unresolved
  explicit sources, and duplicate symbols.

<a id="extract-rust-lexer"></a>
## Lexer

The lexer recognizes nested block and line comments, escaped strings and
characters, byte, C, and raw strings, and lifetimes, so `pub` lookalikes
inside comments or string literals never become token declarations. A
quote followed by an identifier without an immediate closing quote is a
lifetime. Delimiters must balance, non-ASCII code refuses, and any
`macro_rules` token refuses because no reviewed expansion exists.

<a id="extract-rust-attributes"></a>
## Attribute allowlist

Only these attributes are accepted; anything else refuses:

- `derive` restricted to `Clone`, `Copy`, `Debug`, `PartialEq`, `Eq`,
  `PartialOrd`, `Ord`, `Serialize`, `Deserialize`, `Default`, and `Hash`.
- `serde`, `allow`, `deny`, `warn`, `forbid`, `doc`, `repr`, `inline`,
  `must_use`, `non_exhaustive`, `cold`, and `cfg`.
- `path` only as `#[path = "..."]` on a `mod` item whose mapping is
  supplied explicitly in `module_paths`.

`cfg_attr`, inner `#![cfg(...)]` forms, unknown attributes, and unknown
derives refuse even inside ignored bodies or inactive items.

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

The table below records the frozen P05 baseline, which is one historical
baseline rather than the entire current extractor profile. The current
profile covers a larger explicit file list, and the implementer pins
additional separately handwritten module declarations and all source
hashes. The explicit CLI includes those modules; no tree discovery is used.

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
- The router globally remains available (uncalibrated); a separate
  builder and resolver own qualification, and this extractor alone never
  qualifies it.
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
companion documents them but does not execute them. The substantive
test suite is owned by the implementer; this companion makes no
test-execution claim.

<a id="extract-rust-negative-cases"></a>
## Negative cases

The following negative oracle cases are required; each runs in a temporary
copy and its executable form belongs to the implementer, not to this
companion:

- Removing `pub` from a real public function without changing the
  handwritten gold makes the golden test fail.
- `pub` lookalikes in comments and strings are never extracted.
- A `macro_rules!` definition with public expansion refuses explicitly.
- `pub use x::*` and named `pub use` forms refuse explicitly.
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
- Bounded generic accepts are pinned: `Source::take`, `Pipe::tick`,
  and `Dispatch::complete` with exact lines, while the bounded trait
  implementation contributes no member exports.
- Each of the following six generic and self shapes refuses with
  `unsupported_impl`: concrete self specialization (`impl X<u32>`),
  multi-parameter headers (`impl<T, U>`), `where` clauses, `const`
  generics (`impl<const N: usize>`), nested or associated bounds
  (`T: Bound<Item = T>`), and qualified self owners
  (`impl module::X`).
- Compound bounds, lifetime bounds on type parameters, default type
  parameters, multiple `for` keywords, and semicolon-terminated `impl`
  declarations refuse.
- Private trait members remain absent: a private trait contributes zero
  rows, while public trait members are emitted as `Trait::member`.
- A `pub` item inside a trait-implementation body refuses with
  `unsupported_public_trait_impl`.

<a id="extract-rust-limits"></a>
## Limits

This is a bounded syntactic inventory, not a Rust toolchain: no macro
expansion, no name resolution beyond the explicit list, no type checking,
no reachability, and no host inference. It makes no full parser,
compiler, typechecking, or macro-expansion claim. Declarations outside
the allowlist refuse rather than degrade. Findings outside the closed
review list are deferred, not marked as failures.
