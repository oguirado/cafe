# extract_py — bounded Python public-surface extractor

<a id="extract-py-overview"></a>
## Overview

`tools/extract_py.py` is a tooling unit that extracts the declared public
surface of a small, explicitly listed set of Python sources into
deterministic JSON. It performs no routing, no context composition, and no
build execution. AST compilation is syntax validation only. Node sharding, index, resolver, and lookup composition belong
to a separate unit; this companion claims no router qualification, and the
router remains unavailable here, pending the R8 resolver.

The current entry file is `connectors/cafe/authority_channel.py` under the
`connectors/` ownership root. This narrow profile covers the current frozen
source; its export profile does not support arbitrary dynamic Python, while
the underlying standard-library parser is real. There are no product source changes, no new dependencies, no
downloads, and no installs.

<a id="extract-py-reading-map"></a>
## Reading map

- [Overview](#extract-py-overview)
- [Method and non-goals](#extract-py-method)
- [CLI](#extract-py-cli)
- [Input inventory](#extract-py-input-inventory)
- [Output contract](#extract-py-output-contract)
- [Errors](#extract-py-errors)
- [Supported subset](#extract-py-supported-subset)
- [Literal constants](#extract-py-constants)
- [`__all__` literal limits](#extract-py-dunder-all)
- [Refusals (fail closed)](#extract-py-refusals)
- [Strings, comments, and docstrings](#extract-py-strings)
- [Handwritten golden oracle](#extract-py-golden-oracle)
- [Gate hygiene step zero](#extract-py-step-zero)
- [Qualification (honest scope)](#extract-py-qualification)
- [Oracle commands](#extract-py-oracle-commands)
- [Negative cases](#extract-py-negative-cases)
- [Limits](#extract-py-limits)

<a id="extract-py-method"></a>
## Method and non-goals

The extractor is a standard-library-only real Python parser. For each
listed file it detects the source encoding with `tokenize.detect_encoding`,
decodes strictly, parses with `ast.parse`, and additionally compiles the
resulting AST to catch semantic syntax errors only. It never executes and
never imports source: no `exec`, no `eval`, no module import, no dynamic
evaluation of any kind.

It is explicitly not a regular-expression scanner: no pattern over source
text is ever presented as a parser. It is not name resolution beyond the
single file, not a type checker, not reachability or dataflow analysis, and
not an import-graph builder. Anything outside the supported subset below
refuses instead of guessing.

<a id="extract-py-cli"></a>
## CLI

```sh
python3 -B tools/extract_py.py --files connectors/cafe/authority_channel.py
```

Optional flags:

- `--inventory <external-json-snapshot>` — explicit inventory snapshot,
  required for copies without version-control metadata.

The repository root is the parent of the `tools/` directory. A
programmatic entry point `extract(root, paths, inventory)` serves explicit
fixtures. There are no other inputs: no tree discovery, no host path
default, no environment-derived file list, no package installation.

<a id="extract-py-input-inventory"></a>
## Input inventory

- The file list is an explicit, sorted, nonempty, unique set of canonical
  relative `.py` paths. There is no recursive tree traversal, no source
  discovery, and no import execution.
- Every path must sit under the `connectors/` ownership root. Wrong
  ownership refuses.
- Live inventories use version-control cached entries plus non-ignored
  prepared public files. Copies without version control require an
  explicit inventory JSON snapshot; the extractor never infers or
  traverses a tree. Such a snapshot is explicit operator-supplied input,
  not a fresh version-control proof.
- All paths, the inventory, and ownership are validated before any source
  byte is read. The following refuse: aliases and symlinks, escapes,
  missing or unversioned paths, unlisted files, and paths outside the
  supplied inventory.
- Source bytes are read only from the explicit path list and hashed with
  SHA-256 over the raw bytes before the encoding-aware strict decode.
  Original byte identity is preserved.

<a id="extract-py-output-contract"></a>
## Output contract

Output is deterministic, compact, sorted-key JSON terminated with LF:

- `schema` is `cafe.python-surface.v1`; `parser` is `stdlib.ast`.
- `files` holds one entry per input file: `{path, module, owner,
  source_sha256, symbols: [...]}`. Every input file appears with its hash, computed over
  raw bytes before decoding.
- Each symbol holds exactly `{path, symbol, name, line, owner, kind}`.
- `owner` is the exact root from the ownership policy, currently `connectors/`.
- `symbol` is the dotted module path with the `connectors/` prefix removed
  plus the declared name, for example
  `cafe.authority_channel.serve_connector`. An `__init__.py` file uses
  package naming: the package path itself without the module suffix.
- `kind` is one of `function`, `async_function`, `class`, or `constant`.
- `line` is the 1-based source line of the declaring AST node: the
  `def`, `async def`, or `class` statement, or the constant binding.
- Names and paths are preserved. Output contains no timestamps, process
  ids, or host paths. Repeated runs over the same inputs are
  byte-identical.
- All entries are retained with no cap. This unit emits no node records,
  route tables, or index output.

<a id="extract-py-errors"></a>
## Errors

The CLI rejects invalid input with exit status 2 and a precise sanitized
JSON error carrying a code with the relative path and line only. It never
prints raw host exceptions and never emits a partial successful standard
output alongside a failure. Unknown syntax or undecodable source refuses
this way rather than guessing.

<a id="extract-py-supported-subset"></a>
## Supported subset

Only explicitly static module-level declarations are recorded, and only
when the bound name has no initial underscore:

- Public module-level `def` functions, including `async def`.
- Public module-level `class` definitions.
- Public module-level literal constant bindings (see below).

Explicitly excluded by the syntactic inventory contract: public methods,
nested functions and classes, function-local names, and every name with an
initial underscore. Imported helpers are not exported by default: an
imported name is not a local declaration, there is no implicit import
graph, and no explicit re-export form is supported, so imported names are
excluded.

Decorators and default values are evaluated at module level and are
therefore inspected for dynamic patterns, but they never create exports by
themselves. Module-executed compound branches are inspected conservatively
for dynamic export patterns; the extractor does not enter deferred function bodies; it inspects
class-body execution as well as decorators and defaults for direct dynamic namespace operations. Unsupported
multitarget, destructuring, or conditional public exports refuse instead of
inventing symbols.

<a id="extract-py-constants"></a>
## Literal constants

A constant is a public module-level simple-name binding whose value is a
literal accepted by `ast.literal_eval`. No runtime expression is executed
to determine a value. Capitalization does not determine constant status.

A public module-level simple binding to anything but such a literal
refuses: the extractor emits no guessed constant. Literal tuples and containers are supported; calculated, ambiguous,
conditional, mutated, or augmented public bindings refuse.

<a id="extract-py-dunder-all"></a>
## `__all__` literal limits

`__all__` is supported only as one literal list of unique, valid, public
identifier strings. When present it restricts the exported declared items
to the listed declared names.

- Every listed name must resolve to a declaration within the same file.
  Imported, ambiguous, or missing names refuse rather than guess.
- A private (initial-underscore) name claimed by `__all__` rejects:
  source-underscore names are never exported.
- Any other `__all__` shape explicitly abstains: calculated, tuple,
  concatenated, mutated, or augmented `__all__`, and any conditional or
  dynamic export binding.

<a id="extract-py-refusals"></a>
## Refusals (fail closed)

The extractor refuses the whole run on any of the following:

- Any `from x import *` module-execution scope, conservatively, even
  without `__all__`.
- Calculated, tuple, concatenated, mutated, or augmented `__all__`.
- Module-execution `exec`, and module-execution `globals()[...]`
  assignment.
- Unsupported conditional or dynamic export bindings, including
  multitarget, destructuring, or conditional public exports.
- Imported, ambiguous, or missing names listed in `__all__`, and any
  private name claimed by `__all__`.
- Unknown syntax or encoding, unsafe or unlisted paths, and wrong
  ownership.

<a id="extract-py-strings"></a>
## Strings, comments, and docstrings

Strings, comments, and docstrings are ignored for declarations, as are
function bodies. A fake `def` inside a string literal or a docstring never
becomes a declaration. Only real module-level AST bindings declare.

<a id="extract-py-golden-oracle"></a>
## Handwritten golden oracle

The table below records the frozen source baseline for
`connectors/cafe/authority_channel.py`. The gold is authored by hand before
extraction and is never generated from the extractor. Exact source bytes of
the frozen revision are pinned separately as raw-byte hashes.

The file contributes exactly one record:

| Path | Symbol | Name | Line | Owner | Kind |
|---|---|---|---:|---|---|
| `connectors/cafe/authority_channel.py` | `cafe.authority_channel.serve_connector` | `serve_connector` | 27 | `connectors/` | `async_function` |

`_write_stdout_frame` at line 18 is private and contributes nothing. The
imported helpers `MAX_LINE`, `make_frame`, `validate_frame`, and
`validate_lease` are not local declarations and contribute nothing. The
executable golden test verifies this handwritten record against the frozen
source revision at composition time.

<a id="extract-py-step-zero"></a>
## Gate hygiene step zero

Before extraction, the portable one-command runner
`tools/run_integration_gate.py` compiles the actual `cafe-authority-fixture`
with `--offline --locked` in its own `TemporaryDirectory` target, sets
`CAFE_AUTHORITY_FIXTURE` internally, executes the real
`tests/integration/test_authority_channel.py`, and always removes the target
and nested fixtures, including on failure: assertion, startup, and
constructor failures clean up through `TemporaryDirectory` and `addCleanup`
guarantees, and the runner always closes its own handles.

An optional ignored `local/integration_gate.json` or an explicit
environment configuration selects an external approved cargo and toolchain
cache; there is no host path default in publishable code. With no
configuration the runner uses `PATH` and the default cargo cache, and never
downloads or fallback-installs anything.

The integration leg must execute the one-command runner and must reject
missing, failed, zero, skipped, or unclean evidence, preserving the
nonempty-check and evidence invariants. Failure cleanup is tested
deliberately. Cleanup is restricted to directories owned by the invocation; no broad
temporary-directory deletion is performed. Probes use context-managed
temporaries, and the gate must leave no `cafe-*` temporary artifacts.

<a id="extract-py-qualification"></a>
## Qualification (honest scope)

- The Python leg passes only when its real test suite executes, is
  nonempty, and passes. A missing, failed, mutated, or empty selection
  fails the leg.
- The resolver leg stays unavailable: it belongs to R8 only. All three
  extractors pass when implemented; the global router stays unavailable in
  this unit.
- Existing entry, portability, and qualification regression tests must stay
  green.
- The current code contains no MCP tool registration, so this companion
  claims none. The actual MCP tool decorator registration profile is
  deferred until declared (P18); no heuristic tool guess is documented.
- This unit emits no node records, changes no route tables, and claims no
  router qualification: composition owns that judgment.

<a id="extract-py-oracle-commands"></a>
## Oracle commands

```sh
python3 -B -m unittest discover -s tests/bootstrap -p test_extract_py.py
python3 -B tools/run_integration_gate.py
python3 -B tools/qualify_cafe_router.py
```

These are the executable oracles. They are run by the implementer at
composition time, after the extractor and its golden tests exist; this
companion documents them but does not execute them.

<a id="extract-py-negative-cases"></a>
## Negative cases

The six required negative oracle cases follow; each runs in a temporary
copy and its executable form belongs to the implementer, not to this
companion:

1. Deleted public function versus the unchanged independent manual gold is
   a real failure: a copy that renames the public async `serve_connector`
   binding private, while the gold stays unchanged, yields one failure.
   An infrastructure-only outcome is not acceptable; a surviving mutant
   returning zero is not acceptable.
2. A fake `def` inside a string literal or docstring is ignored and yields
   no declaration.
3. Private initial-underscore names are ignored, and a private name claimed
   by `__all__` rejects.
4. A calculated `__all__` explicitly abstains.
5. A star import explicitly abstains.
6. An unlisted file is never read: a read spy proves no unlisted read, and
   a wrong-ownership or alias path refuses.

Supporting oracle behavior: strict input paths, encoding handling, and
hash-before-decode order; two deterministic CLI executions that are
byte-identical; no source execution; and one-command integration evidence
that cleans targets and temporaries even on failure.

<a id="extract-py-limits"></a>
## Limits

This is a bounded syntactic inventory built on the standard-library AST,
not a Python toolchain: no name resolution beyond the explicit file list,
no type checking, no reachability, no dataflow, no import graph, and no
host inference. Declarations outside the supported subset refuse rather
than degrade. The MCP tool profile stays deferred until declared, the
resolver stays pending R8, and the router stays unavailable. Findings
outside the closed review list are deferred, not marked as failures.
