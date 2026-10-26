# extract_js — bounded JavaScript public-surface extractor

<a id="extract-js-overview"></a>
## Overview

`tools/extract_js.py` is a tooling unit that extracts the declared public
surface of a small, explicitly listed set of JavaScript sources into
deterministic JSON. It performs no routing, no context composition, and no
compilation. Node sharding, index, resolver, and lookup composition belong
to a separate unit; this companion claims no router qualification, and the
router remains unavailable here.

The current entry files are `src/main/backendSupervisor.js` and
`src/preload/cafeBridge.js`. This narrow profile covers the current frozen
source; it is not a general JavaScript parser and does not accept arbitrary
JavaScript. There are no product source changes, no new dependencies, no
downloads, and no TypeScript authored source.

<a id="extract-js-reading-map"></a>
## Reading map

- [Overview](#extract-js-overview)
- [Method and non-goals](#extract-js-method)
- [CLI](#extract-js-cli)
- [Input inventory](#extract-js-input-inventory)
- [Output contract](#extract-js-output-contract)
- [Errors](#extract-js-errors)
- [Supported subset](#extract-js-supported-subset)
- [Public typedef convention](#extract-js-typedefs)
- [Factory facade recognition](#extract-js-facade)
- [Module resolution](#extract-js-module-resolution)
- [Refusals (fail closed)](#extract-js-refusals)
- [Comments and literals](#extract-js-comments)
- [Process boundaries](#extract-js-process)
- [Handwritten golden oracle](#extract-js-golden-oracle)
- [Qualification (honest scope)](#extract-js-qualification)
- [Oracle commands](#extract-js-oracle-commands)
- [Negative cases](#extract-js-negative-cases)
- [Limits](#extract-js-limits)

<a id="extract-js-method"></a>
## Method and non-goals

The extractor is a compiler-backed syntactic inventory plus bounded
export and facade recognition. It reuses the approved installed TypeScript
compiler, exact version 5.9.3, strictly as a JavaScript AST parser: never
as a typechecker, never as a source executor, never loading a typechecker
program or resolver. A Node major version 24 runtime is required to run
the approved parser.

It is explicitly not full semantic module, type, reachability, dataflow,
or effect analysis. Anything outside the supported subset below refuses
instead of guessing. Library runtime loading is separate from corpus
ingestion; loading the parser never reads unlisted corpus sources.

<a id="extract-js-cli"></a>
## CLI

```sh
python3 -B tools/extract_js.py --files src/main/backendSupervisor.js src/preload/cafeBridge.js
```

Optional flags:

- `--inventory <external-json-snapshot>` — explicit inventory snapshot,
  required for copies without version-control metadata.

The repository root is the parent of the `tools/` directory. A
programmatic entry point `extract(root, paths, inventory)` serves explicit
fixtures. There are no implicit corpus inputs: no host path default, no Node 18
fallback, no environment-derived file discovery.

The Node runtime resolves from `PATH` and must validate as major version
24. An absolute external runtime path in `ROUTER_NODE` overrides `PATH`
lookup when set. The compiler resolves relative to the tool root at
`node_modules/typescript` and must check as exact version 5.9.3. A
missing runtime or compiler refuses explicitly; the extractor never
installs anything.

<a id="extract-js-input-inventory"></a>
## Input inventory

- The file list is an explicit, ordered, nonempty, unique set of canonical
  relative `.js` paths. There is no recursive tree traversal.
- Every path must sit inside `src/main`, `src/preload`, `src/renderer`,
  or `src/contracts`.
- Live inventories use version-control cached entries plus non-ignored
  prepared public files. Copies without version control require an
  explicit inventory JSON snapshot; the extractor never infers or
  traverses a tree.
- All paths are validated before any source byte is read. The following
  refuse: aliases and symlinks, escapes, missing or unversioned paths,
  and paths outside the supplied inventory.
- Source bytes are read only from the explicit path list and hashed with
  SHA-256 before strict UTF-8 decoding. Original byte identity is
  preserved.
- Only explicit source bytes are read: never recurse or traverse
  directories, never follow imports or `require`, never execute source,
  never load a typechecker program or resolver. Source import targets
  that are not listed are never read.

<a id="extract-js-output-contract"></a>
## Output contract

Output is deterministic, compact, sorted-key JSON terminated with LF:

- `schema` is `cafe.js-surface.v1`; `parser` is `{name: "typescript", version: "5.9.3"}`.
- `files` holds one entry per input file: `{path, source_sha256,
  symbols: [...]}`. Every input file appears with its hash.
- Each symbol holds exactly `{path, symbol, name, line, process}`.
- `process` is the exact `src` child folder: `main`, `preload`,
  `renderer`, or `contracts`.
- `symbol` is the `cafe_shell::` namespace plus the module path without
  the `src/` prefix and `.js` suffix joined by `::`, plus the exported
  name. Public typedefs use the `typedef::Name` segment; static factory
  facade members use the `factory::member` segment.
- Coordinates come from compiler AST token starts as 1-based JavaScript
  source lines: export clauses and assignments at the export name site,
  typedefs at the `@typedef` tag, static facade shorthand methods at the
  resolved local function declaration, aliases at the explicit property
  site.
- Names and paths are preserved. Output contains no machine paths, times,
  or process ids. Repeated runs over the same inputs are byte-identical.
- All entries are retained with no cap. This unit emits no node records,
  route tables, or index output.

<a id="extract-js-errors"></a>
## Errors

The CLI rejects invalid input with exit status 2 and a JSON error carrying
a code, path, and line. It never prints raw host exceptions and never
emits a partial successful standard output alongside a failure.

<a id="extract-js-supported-subset"></a>
## Supported subset

Only explicitly static declarations are recorded:

- ESM named `function`, `const` (`let` and `var` too), and `class`
  declarations; default declarations and expressions; explicit local
  `export {a as b}`; explicit local named re-exports with a unique target
  that is an existing export. There is no export-star and no namespace
  export.
- CommonJS top-level simple assignments `exports.x = ...` and
  `module.exports.x = ...`; literal `module.exports = {named,
  public: local}`; direct default `function` or `class` exports.
- Static object keys, shorthands, and methods are supported.

Runtime export names must be unique; the extractor never guesses a last
assignment. Ordinary exported functions returning non-objects are
inventoried only as declared exports, with no claim to hidden runtime
properties.

<a id="extract-js-typedefs"></a>
## Public typedef convention

- A public typedef is a simple identifier in an actual top-level attached
  JSDoc block. All consecutive attached blocks are considered, using the
  pinned compiler JSDoc metadata.
- A typedef is public by default unless the same block carries
  `@private` or `@internal`.
- Nested typedefs are not public. An explicitly public nested typedef
  refuses instead of inventing an export.
- There is no type-body or member expansion, JSDoc import types are not
  resolved, and duplicate public type names refuse.

<a id="extract-js-facade"></a>
## Factory facade recognition

To describe the actual frozen facades, directly exported functions that
return one static object literal, or `Object.freeze(literal)`, also emit
those explicit members. This is a documented syntactic profile, not
function execution:

- Local shorthand function bindings resolve inside the same function
  body only, not globally and not through inferred runtime values.
- Conditional, ambiguous, async/generator, or multiple object-return paths refuse, as do
  spreads, computed properties, and dynamic returned-object expansion.
- The recognized `Object.freeze` call must be unshadowed; source-local
  bindings and direct assignments that make that recognized identity
  ambiguous refuse. Other return calls are not expanded or inferred.
- The five host methods on the main side are host methods only; they are
  not renderer authority.

<a id="extract-js-module-resolution"></a>
## Module resolution

- Explicit local named re-exports resolve only in the in-memory explicit
  module graph built from the listed files.
- There is no extension or index inference. External, unlisted,
  ambiguous, or cyclic targets refuse.
- A named re-export to an unlisted target refuses without reading that
  target.

<a id="extract-js-refusals"></a>
## Refusals (fail closed)

The extractor refuses the whole run on any of the following:

- Spread, computed, or dynamic object exports.
- CommonJS alias reads and rebindings, conditional or nested export
  mutations, mixed ESM/CommonJS in one file, or a whole-module assignment
  combined with exports-alias writes.
- Unknown export syntax, TypeScript authored syntax, JSX, or decorators.
- Explicitly public nested typedefs and duplicate public type names.
- Any unlisted, ambiguous, external, or cyclic re-export target.

<a id="extract-js-comments"></a>
## Comments and literals

Ordinary comments, quoted strings, regular expressions, and templates
never create declarations or typedefs. `export` lookalikes inside them
produce no output.

<a id="extract-js-process"></a>
## Process boundaries

`process` records the exact source folder only: `main`, `preload`,
`renderer`, or `contracts`. It is not a runtime certification. In
particular, main and preload placement comes from the source folder, and
no runtime IPC claim is made.

<a id="extract-js-golden-oracle"></a>
## Handwritten golden oracle

The table below records the frozen source baseline: 22 rows with
handwritten names, lines, processes, and symbol-prefix literals. The gold
is authored by hand before extraction and is never generated from the
extractor. Exact source bytes of the frozen revision are pinned
separately.

`src/main/backendSupervisor.js` contributes 15 symbols (`main`):

| Symbol | Line |
|---|---:|
| `SupervisorCaller` (typedef) | 12 |
| `SupervisorStatus` (typedef) | 19 |
| `QueuedReceipt` (typedef) | 30 |
| `EventSource` (typedef) | 37 |
| `StdinAdapter` (typedef) | 43 |
| `ChildAdapter` (typedef) | 51 |
| `SpawnPipeOptions` (typedef) | 61 |
| `SpawnFn` (typedef) | 67 |
| `SupervisorOptions` (typedef) | 71 |
| `getStatus` (factory facade) | 280 |
| `handle` (factory facade) | 356 |
| `subscribe` (factory facade) | 448 |
| `start` (factory facade) | 474 |
| `dispose` (factory facade) | 518 |
| `createBackendSupervisor` (CommonJS) | 804 |

`src/preload/cafeBridge.js` contributes 7 symbols (`preload`):

| Symbol | Line |
|---|---:|
| `BridgeTransport` (typedef) | 12 |
| `BridgeStatus` (typedef) | 18 |
| `CafeBridge` (typedef) | 29 |
| `getSnapshot` (factory facade) | 201 |
| `submitIntent` (factory facade) | 210 |
| `onSnapshot` (factory facade) | 218 |
| `createCafeBridge` (CommonJS) | 230 |

Symbol prefixes use the `cafe_shell::` namespace with `typedef::` and
`factory::` segments as defined above. The executable golden test
verifies these handwritten names, lines, processes, and prefixes against
the frozen source revision at composition time.

<a id="extract-js-qualification"></a>
## Qualification (honest scope)

- The JavaScript leg passes only when its real test suite executes, is
  nonempty, and passes. A missing, failed, or empty suite fails the leg.
- The unimplemented extraction and resolver legs and the global router
  stay unavailable in this unit.
- Existing bootstrap, JavaScript check, and Rust qualification regression
  tests must stay green.
- This unit emits no node records, changes no route tables, and claims no
  router qualification: composition owns that judgment.

<a id="extract-js-oracle-commands"></a>
## Oracle commands

```sh
python3 -B -m unittest discover -s tests/bootstrap -p test_extract_js.py
python3 -B tools/qualify_cafe_router.py
```

These are the executable oracles. They are run by the implementer at
composition time, after the extractor and its golden tests exist; this
companion documents them but does not execute them.

<a id="extract-js-negative-cases"></a>
## Negative cases

The following negative oracle cases are required; each runs in a temporary
copy and its executable form belongs to the implementer, not to this
companion:

- Removing an export without changing the handwritten gold makes the
  golden test fail.
- Fake exports inside comments, strings, or templates produce no output.
- Export-star explicitly abstains.
- Computed CommonJS exports abstain.
- An unlisted source is never read, including a named re-export to an
  unlisted target, which refuses without reading the target.
- Typedef privacy, process roots, source lines and hashes, explicit
  re-exports, aliases, and defaults behave as specified.
- Malformed and unsupported syntax refuses explicitly.
- Repeated runs are deterministic and byte-identical.

<a id="extract-js-limits"></a>
## Limits

This is a compiler-backed syntactic inventory plus bounded export and
facade recognition: no type checking, no name resolution beyond the
explicit list, no reachability, no dataflow, and no host inference.
Unsupported export declarations refuse rather than invent symbols. Export names
and module paths use the documented ASCII identifier/path profile; reserved
CommonJS bindings, object accessors and prototype-setter exports refuse.
Unattached comments are not public typedef declarations. Ordinary exported
functions with other return shapes remain declared exports only; no unknown
facade members are inferred. JSDoc type
bodies are not expanded and JSDoc import types are not resolved. Findings
outside the closed review list are deferred, not marked as failures.
