# Backend book

<a id="backend-overview"></a>

## Overview

Evidence tags: MEASURED is observed evidence, CITED means architecture source, TARGET means future design.

This card follows the [architecture source](../../docs/architecture/ARCHITECTURE.v0.1.md) sections 2, 3 and 9 [CITED]; their design remains [TARGET]. No runtime claim below is MEASURED. The oracle near the end checks documentation navigation only.

All component identities below are future targets; no implementation location is asserted.

| Future identity [TARGET] | Responsibility [TARGET] |
| --- | --- |
| Rust authority [TARGET] | Planned capsule and feed admission plus confirmation duty [TARGET] |
| Python connectors + MCP [TARGET] | Planned Twitch and OBS protocol translation duty [TARGET] |
| JS shell [TARGET] | Planned display and typed intent submission duty [TARGET] |
| recorder [TARGET] | Planned frame and audio timeline coordination duty [TARGET] |
| OBS adapter [TARGET] | Planned scene request shaping duty through owned transport [TARGET] |
| Twitch data layer [TARGET] | Planned event observation and allowlisted read duty [TARGET] |

**Reading map**

- [Overview](#backend-overview)
- [Ownership and boundaries](#backend-boundaries)
- [Journal design](#backend-journal)
- [Documentation oracle](#backend-oracle)
- [Negative knowledge](#backend-negative)

<a id="backend-boundaries"></a>

## Ownership and boundaries

Control messages use a shared envelope with schema, kind, request id, authority epoch, expected revision and body; replies carry request id, state, revision, error and receipt [CITED] [TARGET]. Outcomes distinguish rejected, admitted, dispatched, observed and uncertain [CITED] [TARGET]. Unknown keys, incompatible schemas, oversized lines, invalid identities and stale revisions reject before admission [CITED] [TARGET].

Rust and the shell exchange intent, snapshot, delta, confirm and result records over supervised pipes; a closed pipe marks the backend unavailable and stderr stays separate [CITED] [TARGET]. Rust supervises the Python connector over private pipes with start, token lease, observation, approved effect and effect result records; a restart revokes epoch and lease [CITED] [TARGET].

UI snapshots carry capsule and feed handles, generations, geometry, readiness, recording state, OBS desired and observed state, consent reminders, store health and pending proposals; deltas carry a base revision and a gap calls for a fresh snapshot [CITED] [TARGET].

Intents cover pairing, crop confirmation, record preparation, start and stop, OBS send, folder choice and connection setup; folder intents carry approved handles rather than free paths [CITED] [TARGET]. AI proposals need a separate authenticated UI confirmation bound to digest, account, generations, crop, scene, epoch and expiry; changed arguments void approval and no MCP tool can confirm [CITED] [TARGET].

<a id="backend-journal"></a>

## Journal design

Rust is the sole writer of an append-only journal; no SQL is needed [CITED] [TARGET]. Records carry schema, session id, authority epoch, sequence, event id, kind, payload, prior digest and record digest [CITED] [TARGET]. Encoding, digest choice, flush policy and framing freeze before durability review [CITED] [TARGET]. Durable admission precedes any committed reply, and replay rebuilds dedup state, generations, proposals and recording inventory [CITED] [TARGET].

A torn tail record may be quarantined as an interrupted suffix while corruption inside the committed prefix blocks replay [CITED] [TARGET]. External OBS success and journal admission stay separate states; lost replies call for readback rather than blind repeat [CITED] [TARGET]. Record-all keeps per-feed outcomes and cannot invent atomic start [CITED] [TARGET].

Media publication follows intent, file validation, publication, then durable receipt, with startup reconciling edges [CITED] [TARGET]. The journal holds control receipts rather than sensitive payload copies, so it stays append-only; deletion rewrites retained segments through the sole writer and records only opaque purge receipts [CITED] [TARGET]. Checkpoints need verified ancestry plus replay equality before rotation [CITED] [TARGET].

Journal durability is a future [TARGET]; OBS and media stay external and never commit atomically with the journal [TARGET]. Twitch event data lives in a separate retention-governed set [CITED] [TARGET].

<a id="backend-oracle"></a>

## Documentation oracle

Run this command from the repository root to check documentation navigation:

`python3 -B -m unittest discover -s tests/bootstrap -p 'test_entry_contract.py'`

The oracle above validates documentation navigation, not runtime implementation. It checks advertised hops, anchor use, route coverage and entry token caps [TARGET]. No passing oracle output proves runtime behavior; no runtime proof is invented here.

<a id="backend-negative"></a>

## Negative knowledge

- router available (uncalibrated)
- generic lookup resolves current indexed coordinates through tools/resolve_cafe.py
- Rust, JavaScript and Python extractors pass structural checks; router calibration remains pending
- production wiring and packaged Windows verification remain pending
- no code locations resolved for future components, and this card asserts none
- OBS and media effects stay outside journal atomicity
- no source or runtime proof is invented
