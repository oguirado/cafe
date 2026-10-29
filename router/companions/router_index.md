# router_index — exact nodes/index/resolver, available but uncalibrated

<a id="router-index-overview"></a>
## Overview

This companion documents the R8 exact nodes/index/resolver surface:
compose the output of the existing Rust, JavaScript, and Python extractors
plus the actual HTML anchors and Markdown headings in `docs/` and `subjects/`,
validate `catalogue/nodes.jsonl` directly against `ctx.node.v2` with the
existing kit validator, build the unchanged kit index, and resolve exact
coordinates with `tools/resolve_cafe.py`.

Status is explicitly **available, uncalibrated**. There are no fitted Cafe
parameters: `catalogue/calibration.json` carries status `uncalibrated` with
null params, and the generated artifact `params.json` under `catalogue/router/` is absent. That absence is
the honest uncalibrated state, not a gap to fill with foreign numbers. No
papers calibration is copied, and no foreign params are ever passed to the
kit. Actual calibration is not implemented until post-demo; even if every
structural check passes, the router is never qualified here.

This is a documentation-only companion. It implements no route, context, or
compile logic, and it ships no code or tests. Codex owns all R8 logic,
validation, and tests. Muse writes only this file.

WARNING: an uncalibrated shortlist is never authority to edit. After any
route, resolve the exact coordinate and verify it before touching source.

<a id="router-index-reading-map"></a>
## Reading map

- [Overview](#router-index-overview)
- [Scope and non-goals](#router-index-scope)
- [Source profile and inventory](#router-index-source-profile)
- [Canonical compiled-source contexts and witnesses](#router-index-canonical-source-contexts)
- [Compose and node validation](#router-index-compose)
- [IDs, anchors, and byte widths](#router-index-ids-anchors-widths)
- [Sixteen-anchor sharding](#router-index-sharding)
- [Generated products](#router-index-products)
- [Build CLI](#router-index-build-cli)
- [Unchanged kit index](#router-index-kit-index)
- [Resolver CLI](#router-index-resolver-cli)
- [Optional route wrapper](#router-index-route-wrapper)
- [Calibration (unfitted by design)](#router-index-calibration)
- [Qualifier](#router-index-qualifier)
- [Three-language coordinate proof](#router-index-three-language-proof)
- [Seven negatives](#router-index-negatives)
- [Refusals](#router-index-refusals)
- [Rebuild rule](#router-index-rebuild-rule)
- [Oracle commands](#router-index-oracle-commands)
- [Limits and deferred work](#router-index-limits)

<a id="router-index-scope"></a>
## Scope and non-goals

This unit is a tooling unit: no Route, Context, or Compile. It composes
nodes, builds the kit index with an unchanged kit command, and resolves
exact keys to exact coordinates. It does not rank, does not compose context,
does not compile, and does not run live services or read runtime objects.

Kit tools and schema are never modified. If the kit rejects composed nodes,
the correct action is to STOP and report to the operator; there is no
workaround and no rewrite of nodes to please the kit. Vendor traces, R1
traces, and the private gate hash are preserved. Accepted W0/F0 and the
full current prepared export have private-policy count 0; private literals
remain external hashed policy and are never inlined here. There are no
commits, no push, no installs, no downloads, and no new packages.

Separate build targets per role apply, with owned-PID signal guards. Actual
mutants used by the gate do not touch signal targets; any mutation touching
them would require an isolated PID namespace. Every worker or judge command
that executes `node` or `npm` prepends the approved Node 24 binary directory,
supplied externally as `NODE24_BIN`, FIRST on `PATH`.

Rust, Node, and bootstrap gates run offline with the approved cache, under
bounded memory and time. No gate-owned temporary directories remain after the gate. Test
copies use a temporary directory with guaranteed cleanup, and signal guards
cover only owned processes.

<a id="router-index-source-profile"></a>
## Source profile and inventory

The versioned explicit `catalogue/source_profile.json` lists every source
the builder may consume:

- product Rust under `core/` and `src/`,
- JavaScript `src/` process roots,
- Python connectors under `connectors/`,
- Markdown under `docs/` and `subjects/`,
- the actual Rust fixture entry.

Coverage is compared against the versioned prepared inventory. The builder
never walks the tree for source discovery.

The Rust extension tool (not the kit) permits an optional entry or crate and
an explicit literal path-to-module mapping ONLY when that mapping is bound
to an actual `#[path]` declaration. The default R5 profile is unchanged.
P08 modules are indexed under their ACTUAL fixture crate
`cafe_authority_fixture`, not under a fictional production namespace; the
main binary and runtime are unchanged. The profile records an explicit
`unix=true` cfg for the tested Linux fixture: no host inference and no
Windows claim.

Source-file coverage is complete for the profile. An extra eligible file
without a semantic-id profile entry FAILS. Metadata, tools, and test
sources outside extractor ownership are not product API inventory.

<a id="router-index-canonical-source-contexts"></a>
## Canonical compiled-source contexts and witnesses

One physical file keeps one existing explicit `source_profile`
`symbol_prefix` canonical namespace. Multiple compiled Rust roots may
include and report witnesses for the same physical file; that sharing is
allowed and is not a duplicate-ownership violation. Any older absolute
reading that one physical file may appear in only one Rust root is
corrected here and no longer applies. Every non-owner compiled root is a
witness only: its context is normalized and compared against the canonical
owner, then discarded as an identity. There are no aliases, no new schema
fields, no clipped keys, and no arbitrary or order-dependent winners.

Single-root behavior is unchanged: a path reported by exactly one root
uses that root report directly. A path reported by more than one root is
resolved by `canonical_rust_report(path, contexts, declared_prefix)`,
where each context is `(crate, cfg, report)` with `crate` the normalized
Cargo binary name, `cfg` the target-config dict, and `report` the
extractor file report (`path`, `source_sha256`, `module`, `symbols`).

Normalization is strict and strips ONLY the known compiled crate prefix
(`crate` plus the double-colon separator):

1. For each context, require `report.get('path') == path`, `module` a
   string, and `module == crate` or `module` starting with the crate
   prefix; otherwise abstain with
   `invalid_compiled_source_namespace`.
2. For each symbol record, require `symbol` a string starting with the
   same crate prefix; otherwise abstain with
   `invalid_compiled_symbol_namespace`. Unknown compiled crate prefixes
   are never stripped.
3. Strip only that crate prefix from `module` (yielding an empty string
   when `module == crate`) and from each symbol string. Module tails and
   every other record field (`path`, `name`, `line`, `cfg`, symbol tail)
   are preserved; inputs are copied, never mutated.
4. Sort the normalized surface records by canonical record order and pair
   each normalized physical report with its `cfg`.

Witness equality: all `(cfg, physical)` tuples for the path must be
identical (`source_sha256`, `cfg` profile, module tail, and every
normalized public symbol record). Owner selection then requires exactly
one candidate report whose `module` plus separator equals the declared
`symbol_prefix` and whose every symbol retains that prefix. Selection
examines all contexts and is order-independent: reversed root order
returns the same unmodified canonical report with its original unstripped
symbols. Empty public surfaces still require exact `source_sha256`,
`cfg`, and module identity plus a unique declared owner.

The documented contract explicitly rejects these four cases:

1. Differing hash: two roots report a different `source_sha256` for the
   same physical file, public or private zero-surface. Abstain with
   `compiled_source_context_mismatch`.
2. Differing cfg/public surface: two roots differ in root `cfg`, symbol
   `cfg`, `line`, `name`, symbol tail, module tail, or symbol count
   (populated versus empty). Abstain with
   `compiled_source_context_mismatch`.
3. Missing canonical prefix: the declared prefix is missing, clipped,
   empty, or parent-crate-only relative to the compiled module, so no
   report qualifies. Abstain with
   `missing_or_ambiguous_canonical_source_owner`.
4. Duplicate canonical owner: two contexts both match the declared
   prefix, even if all bytes and symbols match. Abstain with
   `missing_or_ambiguous_canonical_source_owner`.

Related abstentions stay explicit: undeclared compiled sources abstain,
and compose-time prefix checks (`row['symbol_prefix']` equals
`file['module']` plus separator, exact prefix slicing for local anchors)
continue to refuse clipped or mismatched namespaces. Prior symbol
identity is retained: actual temporary imports of `authentication` and
`supervisor` into both existing roots preserve the complete previously
admitted `symbols.jsonl` bytes, and `enforce_admitted_symbols` still
verifies admitted records are a subset of current records, refusing with
`public_symbol_removed_requires_review` on any removal.

Schemas and the stock kit are unchanged: source profile
`cafe.source-profile.v1`, nodes `ctx.node.v2`, symbols
`{path, symbol, name, line}`, coordinates
`{id, anchor, path, name, line, symbol, source_sha256}`, routes
`id` TAB `path`, receipt `cafe.router-sources.v1`, build envelope
`cafe.nodes-build.v1`, and the exit-2 error envelope. The stock kit index
command and its `--check` form stay byte-identical, and the router stays
available but uncalibrated. Unrelated resolver and calibration rules
elsewhere in this companion are intact and unchanged.

<a id="router-index-compose"></a>
## Compose and node validation

The builder composes the output of the existing Rust, JavaScript, and
Python extractors plus the actual HTML anchors and Markdown headings found
in `docs/` and `subjects/`. Source bytes are hashed with SHA-256 over the raw bytes
before any decode, so original byte identity is preserved.

`catalogue/nodes.jsonl` is validated DIRECTLY against `ctx.node.v2` with
the existing kit `jsonschema_lite.validate` entry point. No extra fields
are permitted. Each file contributes a minimum of one node with a manual
semantic id from the profile. Files without public exports still get a file
header coordinate as their asset anchor, so file coverage stays complete.

Markdown handling keeps the actual HTML ids exactly, with duplicate refusal.
Separate `heading-N` semantic coordinate keys map to the actual heading
line; they are never invented browser fragments. Every anchor has exactly
one coordinate in the same file. The documentation primary ids
`architecture-v0-1`, `github-repo-rule`, and `backend-book` are preserved
for entry browse.

<a id="router-index-ids-anchors-widths"></a>
## IDs, anchors, and byte widths

Width limits are byte limits over UTF-8, not character counts:

- corpus `cafe:` id: at most 32 UTF-8 bytes,
- anchor: at most 64 UTF-8 bytes,
- family: at most 32 bytes,
- all schema fields bounded by the closed schema.

Node ids are manual semantic aliases with short shard suffixes such as
`-p2`, collision-checked. There is no path clipping and no hash clipping.
Fully qualified symbol strings stay exact, and they appear in exact form in
`catalogue/symbols.jsonl` records holding ONLY
`{path, symbol, name, line}`. The local anchor form removes only the known
exact module prefix already encoded by the file family; it never clips or
hashes names. All local method, facade, and type qualifiers are retained.
If the exact form is still too long, the builder REFUSES instead of
shortening it.

Multibyte anchors are measured in bytes: an anchor that fits in 64
characters but exceeds 64 UTF-8 bytes refuses.

<a id="router-index-sharding"></a>
## Sixteen-anchor sharding

When a file has more than 16 public anchors, all locations are sharded
across parts in the same family. Sharding never truncates: every anchor
keeps a full coordinate, and the shard suffix distinguishes the parts.
Sharded ids may legitimately share the same file path; that is full
coverage, not a duplicate. The `>16`-anchor case is covered end to end with
no lost location.

<a id="router-index-products"></a>
## Generated products

The builder emits a closed set of generated products:

- `catalogue/nodes.jsonl`: validated node records, full file coverage.
- `catalogue/symbols.jsonl`: one exact `{path, symbol, name, line}` record
  per admitted public symbol. This file is the admitted-symbols contract.
- `catalogue/coordinates.jsonl`: maps each node id plus anchor to the
  actual `path`, `name`, `line`, and `source_sha256`, plus the full symbol
  when applicable. Governance notes stay outside the closed node schema.
- `catalogue/router_sources.json`: closed receipt binding the explicit
  profile, tool versions, schema versions, source hashes, and family paths.
  It carries no host paths, no PIDs, and no timestamps.
- `catalogue/routes.tsv`: exactly `id` TAB `path` per line, with a head
  line plus FULL node coverage. No duplicate id, no alias confusion, no
  escape.
- `catalogue/router/`: the unchanged kit index output directory.
- `catalogue/calibration.json`: status `uncalibrated`, params null.

Node, provenance, source, lookup, and anchor history are reconciled. There
is no fuzzy-key fallback and no prefix fallback.

The builder keeps admitted public symbols. Before overwriting
`symbols.jsonl` it rejects any removal of an existing `path`/`symbol`/`name`
key. A legitimate public API removal requires operator review; it is never
a silent loss. The verifier re-extracts the current public surface and
compares the symbol table, so even an honest new source hash plus a rebuilt
kit index still FAILS when checked against the retained `symbols.jsonl`
after a dropped export. That failure is the contract working, not a stale
artifact.

<a id="router-index-build-cli"></a>
## Build CLI

Build all products from the explicit profile and the explicit versioned
inventory:

```sh
python3 -B tools/build_cafe_nodes.py
```

Verify determinism and freshness without writing:

```sh
python3 -B tools/build_cafe_nodes.py --check
```

`--check` byte-compares every generated artifact: nodes, symbols,
coordinates, routes, and provenance. Any difference fails. The first build
establishes the no-loss contract; every later build cannot drop an admitted
symbol. Rebuild after EVERY code unit: a source edit without a rebuild is a
stale product, and stale products fail lookup before any coordinate is
returned.

<a id="router-index-kit-index"></a>
## Unchanged kit index

Build the kit index with the EXACT unchanged kit command:

```sh
python3 -B router/tools/build_index_v2.py --nodes catalogue/nodes.jsonl --out catalogue/router --corpus cafe
```

Then run the SAME command with `--check`, which must print byte-identical:

```sh
python3 -B router/tools/build_index_v2.py --nodes catalogue/nodes.jsonl --out catalogue/router --corpus cafe --check
```

If the kit does not accept the composed nodes, STOP and report to the
operator. Do not reshape nodes, patch the kit, or invent an alternate index
path. Generated kit index files are tool output: byte-identical and
portable. The project reader checks manifest file hashes and node/tool
bindings without assuming that paper-source keys are filesystem paths. The
generic lookup is never fed the papers parser.

<a id="router-index-resolver-cli"></a>
## Resolver CLI

Resolve one exact key to exactly one current coordinate:

```sh
python3 -B tools/resolve_cafe.py 'cafe_core::capsule_registry::decode_envelope'
```

The result is exactly `{path, line, name, source_sha256}`. Accepted key
forms are the exact node id (primary anchor), the full symbol, the local
anchor when it is unique, or `id#anchor`. An optional `cafe:` prefix is
accepted exactly; it does not forgive any other misspelling.

Before returning coordinates, the resolver refuses duplicates, escapes,
clipped or overwidth keys, unsafe or symlink routes, ambiguous matches, and
stale hash, line, or name evidence. The current file line must contain the
returned `name`, and the raw-byte hash must match both the node and the
coordinate record. There is exactly one current coordinate, hash, and name
line per accepted key: one key, one live location.

The verifier checks ALL metadata and fresh complete extractions before
lookup. A missing or unreadable input FAILS; it is never skipped. There is
no ranking, no name fuzziness, and no service. An unknown or ambiguous
anchor refuses. The default builder and resolver status is explicitly
uncalibrated.

<a id="router-index-route-wrapper"></a>
## Optional route wrapper

The optional route wrapper answers a natural-language query with an
explicitly acknowledged uncalibrated shortlist:

```sh
python3 -B tools/resolve_cafe.py --route "<query>" --ack-uncalibrated
```

The `--ack-uncalibrated` flag is mandatory: without it the wrapper refuses.
The wrapper first verifies fresh products and a fresh index, then calls the
unchanged kit `route_v2` without foreign params. The kit built-in
uncalibrated defaults used on that path are NOT fitted Cafe params, and
this companion never presents them as calibration.

The shortlist is a finding aid only. It is never authority to edit: resolve
the exact coordinate after the route and verify the live line and hash
before changing anything.

<a id="router-index-calibration"></a>
## Calibration (unfitted by design)

There are no fitted Cafe parameters in R8, and no papers calibration is
copied. `catalogue/calibration.json` carries status `uncalibrated` with
null params, and the generated artifact `params.json` under `catalogue/router/` is absent. Any claim of a
calibrated router without params and evidence FAILS. Actual calibration is
not implemented until post-demo; even if all structural legs pass, the
router is NEVER qualified here. The protocol keeps its closed fields
unchanged as the future params reference; their absence is the honest
uncalibrated state. The README and this companion both carry the explicit
uncalibrated warning.

<a id="router-index-qualifier"></a>
## Qualifier

The `resolver_index` qualifier is implemented with a real non-empty
`tests/bootstrap/test_router_index.py` AND a real `python3 -B router/tools/build_index_v2.py --nodes catalogue/nodes.jsonl --out catalogue/router --corpus cafe --check`.
When every implemented leg passes, the global verdict is `router available
(uncalibrated)`, with state and availability `available_uncalibrated`,
`calibrated` false, and never qualified. `--require-qualified` always exits
nonzero until calibration is implemented and proven.

README, llms, and backend-book status text plus the entry test are updated
under the existing token limits with no host or volatile data. Existing
qualification and extractor test mocks are updated for the new available
but uncalibrated semantics with no oracle weakening.

<a id="router-index-three-language-proof"></a>
## Three-language coordinate proof

Exact coordinates are proven in all three product languages against their
real current locations, including:

- Rust `decode_envelope` at line 293,
- JavaScript preload `getSnapshot` at line 201,
- Python `serve_connector` at line 27.

Complete resolver outputs are compared to the extractor contracts plus the
manual existing golds. Expected fixtures are never self-generated from the
implementation under test.

<a id="router-index-negatives"></a>
## Seven negatives

Each of the following seven cases FAILS independently:

1. Source edit without rebuild: the stale hash or stale line is detected
   and lookup refuses before returning coordinates.
2. Duplicate node id: the build or verification refuses; no second record
   silently wins.
3. Duplicate anchor within a family: the build refuses; Markdown keeps
   actual HTML ids exactly and never merges duplicates.
4. A `../` route path is refused as unsafe; exact lookup keys are never filesystem paths.
5. Over-32-byte id that would require clipping: refused. Automatic clipping or hashing of public keys is forbidden; short semantic
   family ids are explicitly reviewed profile identities, not shortened symbols.
6. Removed public export after an HONEST source composition and kit
   rebuild: still fails against the retained `symbols.jsonl`. The verifier
   re-extracts the current public surface, sees the dropped
   `path`/`symbol`/`name` key, and the admitted-symbols contract fails.
   Legitimate removal requires operator review.
7. Missing params claiming calibrated: fails. Without fitted params and
   evidence, any calibrated claim fails, and `--require-qualified` stays
   nonzero.

Additional oracles cover byte-width multibyte anchors, full `>16`-anchor
coverage, an unknown schema field (refused, schema stays closed), exact
coordinates in all three languages, and collision or ambiguous-key
refusals.

<a id="router-index-refusals"></a>
## Refusals

The builder and resolver fail closed. They refuse duplicate ids, duplicate
family anchors, clipped or overwidth keys, unsafe or symlink routes,
ambiguous matches, stale hash/line/name evidence, unknown schema fields,
and unknown or ambiguous anchors. Missing or unreadable inputs fail; they
are never skipped. No fuzzy key, no prefix fallback, no silent merge, and
no truncation anywhere on the resolve path.

<a id="router-index-rebuild-rule"></a>
## Rebuild rule

Rebuild after each source change. Every code unit invalidates the composed
products until `tools/build_cafe_nodes.py` and the unchanged kit index
command are re-run and their `--check` forms are byte-identical. An
uncalibrated shortlist is not edit permission: route only to find
candidates, then resolve the exact coordinate and confirm the live line and
hash.

<a id="router-index-oracle-commands"></a>
## Oracle commands

Documentation review only: this companion executes nothing. The owning
(Codex) and judge commands below are recorded here as oracles, not run by
Muse:

```sh
python3 -B tools/build_cafe_nodes.py --check
python3 -B router/tools/build_index_v2.py --nodes catalogue/nodes.jsonl --out catalogue/router --corpus cafe --check
python3 -B tools/resolve_cafe.py 'cafe_core::capsule_registry::decode_envelope'
python3 -B tools/resolve_cafe.py --route "<query>" --ack-uncalibrated
```

Any `node` or `npm` step in those oracles runs with the Node 24 binary
directory FIRST on `PATH`, as stated in the scope section. Anything the kit
rejects stops the line and goes to the operator.

<a id="router-index-limits"></a>
## Limits and deferred work

The closed solution list is: direct schema validation, determinism and
`--check`, exact three-language coordinates, the seven negatives,
unchanged kit, no foreign params, uncalibrated labels in the README,
harness, and this companion, and no host or volatile data. Anything else is
deferred and does NOT fail. The first FAIL goes back to Codex for correction
with one final judge; a repeated FAIL stops the unit.

