# Café — SEALED, REDUCED-SCOPE backend architecture

## 0 [TARGET]. Summary and diagram

Café is a desktop program for the streamer’s Windows PC. Guests share through Discord as usual. The host opens received stream and call pop-outs, associates their visible content with people, records those windows locally, and asks OBS to capture the same windows. Café neither transports guest video nor substitutes its own calling service. Holdings/MetaViewer presentation will be copied later; this seal specifies the backend↔UI contract only.

A capsule represents a connected person and has up to two feeds [TARGET]: game and camera. Each feed exposes [Record] and [Send to OBS]; [Record all] coordinates the selected available feeds. Recording preserves received Discord playback at its observed quality, including 1080p when available [TARGET], with unavoidable capture/re-encoding effects. A larger capture surface cannot establish upstream resolution. Mixed Discord application audio is acceptable; isolated participant audio is not promised.

**SEALED means design decisions are fixed, not that implementations, binaries, policies or integrations passed.** Operator rulings supplied for this wave override the previous architecture and audit wherever they conflict. Audit A01’s access-token exception is retained. Audit proposals to postpone native recording or limit FFmpeg to remuxing are superseded. Windows shipping and demo are mandatory; Linux arm64 is a best-effort development environment with honest capture stubs. Twitch collection is last in implementation priority but remains a required deliverable.

```text
Discord received pop-outs ── HWND / crop ──> Rust authority
                                          registry / capture / pacer
Electron renderer <─ narrow preload ─> main     │
                               child pipes ────┤
                                              ├─ FFmpeg child ─> chosen recording folder
                                              ├─ append-only authority journal
                                              ├─ vault / OAuth / JSONL writer
                                              │ private connector pipes
                                              v
                                  Python Twitch + OBS adapters
                                    │                 │
                              Twitch APIs       obs-websocket
                                                      │
                                             OBS Window Capture
                                             captures Discord itself

Local Codex ── MCP stdio ──> Python broker ── authenticated local seam ──> Rust
AI proposals ──> streamer confirmation in desktop UI ──> approved external effect
```

Evidence is the named local corpus only. Answer labels are citations, not independently checked primary sources; harvested counters, questions, malformed JSON fragments and “x+1” residue are discarded. “Cafe” means Café. Numeric section labels, application schema constants and proposed file layouts are [TARGET]; answer identifiers and decision identifiers are coordinates. Missing facts are `AUSENTE — no inventar`. No network qualification is claimed.

This mandate is scoped to the supplied architecture, decisions, audit rulings, answer corpus and bootstrap sections.

## 1 [TARGET]. Runtimes, crates and tools

All pins below are documentary baselines from answers. Freeze exact archives, dependency locks, enabled features and hashes during implementation. Never author TypeScript; use JavaScript, JSDoc and checkJs in separate main, preload and renderer contexts. Compiler tooling may consume dependency declarations without adding application sources in that language.

| Component | Pin / evidence | Selection |
|---|---|---|
| Rust compiler | AUSENTE — no inventar | Record available compiler before lock qualification |
| Tokio | 1.54.1 [CITED: C22] | Child processes, bounded channels, deadlines and pipe supervision |
| windows / windows-rs | 0.62.2 [CITED: D02] [CITED: D06] | Direct WGC and native audio bindings |
| windows-capture | 2.0.1 [CITED: D02] | Documentary alternative; not an additional capture dependency |
| Electron | 44.9.2 [CITED: C25] | Desktop shell and sidecar supervision |
| Embedded Chromium / Node.js | 152.0.7977.167 / 24.22.0 [CITED: C25] | Use Electron’s bundled runtimes |
| CPython / standalone distribution | 3.13.17 / 20261021 [CITED: C25] | Private packaged interpreter |
| FFmpeg | 8.1.3 [CITED: C22] [CITED: C25] | Audited executable with actual encoder tests |
| OBS Studio / obs-websocket | 32.2.2 / v5 [CITED: C09] [CITED: C10] | Probe requests and Windows source settings at runtime |
| Python mcp SDK | 2.3.0 [CITED: C18] | Standard stdio adapter, lifecycle smoke test required |
| Codex CLI | 0.164.0 [CITED: C19] | Cited registration baseline, not installed-host assertion |
| electron-builder | 26.18.0 [CITED: C25] | Unsigned Windows package |
| wasapi alternative | 0.26.0 [CITED: D06] | Not selected; direct windows bindings keep timing explicit |
| Optional Linux portal / PipeWire | ashpd 0.13.13 / pipewire 0.10.1 [CITED: D07] | Deferred convenience, not shipping dependencies |
| Vue, checkJs checker, Python OBS/Twitch libraries, vault crate | AUSENTE — no inventar | Do not manufacture versions or imply dependencies installed |

No FFmpeg Rust binding is required. Use Tokio directly rather than inheriting a blocking sidecar log iterator. Direct windows bindings cost more native plumbing but preserve frame ownership, timestamp metadata and failure reporting. The dependency licence matrix and exact platform artifact inventory remain implementation gates. [CITED: C22] [CITED: D02]

## 2 [TARGET]. Components and ownership

| Component | Authority and responsibility | Failure boundary |
|---|---|---|
| Rust reducer and registry | Capsule/feed identities, generations, revisions, admission, confirmation | Pure transitions; rejects invalid intents before effects |
| Rust window/capture adapter | Enumerate native candidates, bind handles, capture and track geometry | Closed/minimized/stalled targets become unavailable |
| Rust recorder coordinator | Frame ownership, pacer, audio timeline, child lifecycle, inventory | Partial feed failures remain visible during Record all |
| Rust persistence | Append-only journal and sole Twitch JSONL writer | Writer failure prevents durable acknowledgement |
| Rust vault | OAuth, refresh serialization, persistent provider credentials | Failed secure storage disables connection |
| Python connector process | Twitch EventSub/Helix and OBS protocol adapters | Observations cannot directly rewrite authority state |
| Python MCP broker | Typed stdio tools, local authority rendezvous, bounded responses | Client loss cannot approve or replay effects |
| Electron main/preload | Supervise Rust, validate IPC caller, expose narrow facade | Renderer replacement cannot stop native recording |
| Renderer contract consumer | Display snapshots, submit typed intents and streamer confirmations | Stale projection disables mutation |

Rust owns files, media and credential lifecycles. Python may hold short-lived Twitch access tokens in memory injected privately by Rust, per A01; it never refreshes or persists them. Rust retains the OBS password and performs the credential-bearing OBS handshake/transport; Python translates approved requests and sanitized responses through that transport. The A01 exception applies to short-lived Twitch access tokens, not an OBS password-read API. A full token-owning OBS SDK is therefore unnecessary; the Python adapter owns protocol semantics rather than credential custody. [CITED: C10] [CITED: C24]

## 3 [TARGET]. Boundary contracts

Application envelopes below are proposed Café contracts [TARGET], not additional provider APIs. A common control message is `{schema,kind,request_id,authority_epoch,expected_revision,body}`; replies are `{request_id,state,revision,error,receipt}`. Outcome distinguishes rejected, admitted, dispatched, observed and uncertain. Large clock integers cross JavaScript as decimal strings. Unknown keys, incompatible schemas, oversized lines, invalid identities and stale revisions reject before admission. Queue sizes, byte limits and deadlines must be frozen in a validated profile before listeners open. [CITED: C20] [CITED: C21]

| Boundary | Transport and sketch | Failure behavior |
|---|---|---|
| Rust↔Electron | Anonymous child JSONL pipes; `intent`, `snapshot`, `delta`, `confirm`, `result` | Incremental bounded parsing; EOF marks backend unavailable; stderr drained separately |
| Rust↔Python connector | Rust-supervised private pipes; `connector_start`, `token_lease`, `observation`, `approved_effect`, `effect_result` | Child restart revokes its epoch and token lease; refresh requested through Rust |
| Rust↔independent Python broker | Authenticated loopback WebSocket from the prior architecture; capability restricted to broker tools | Loopback bind only; no wildcard interface; stale capability and authority restart reject |
| Python↔MCP client | Standard MCP SDK over stdio; typed arguments/results | Protocol-only stdout; sanitized stderr; lifecycle/tool discovery tested with actual Codex |
| Café↔OBS | Python semantic adapter through Rust-authenticated obs-websocket transport; `{op,d:{requestType,requestId,requestData}}` | No request before authentication; timeout yields uncertain and getter reconciliation |
| Café↔Twitch | Python EventSub WebSocket plus allowlisted Helix requests, Rust-owned OAuth endpoints | Expired token pauses requests; refresh only through Rust; gaps remain explicit |

The UI receives complete redacted snapshots containing capsule/feed handles, window generation, crop generation, observed geometry, readiness, recording state, OBS desired/observed state, consent reminder state, store health and pending proposals. Deltas carry their base revision. An epoch change or revision gap requires a snapshot. Labels are presentation, never identity. Closing, moving or hiding a panel has no recorder semantics.

Intents include pairing, crop confirmation, record preparation/start/stop, OBS send, folder selection and connection setup. Filesystem intents carry approved folder handles rather than arbitrary paths. Native folder picking establishes Rust’s storage root. UI-originated actions express the streamer’s request; AI proposals require a separate authenticated UI confirmation bound to proposal digest, account, feed generation, crop, destination scene, epoch and expiry. Changed arguments invalidate approval. No MCP tool can issue confirmation.

The broker receives only a restricted rendezvous capability, never provider tokens. Exact installation rendezvous filename, ACL implementation and secret-store crate are AUSENTE — no inventar; the corresponding build unit supplies and tests them. Missing authentication blocks the broker seam. Bulk media never enters control pipes or MCP. [CITED: C18] [CITED: C20]

## 4 [TARGET]. Capsule model and window pairing

A capsule is `{capsule_id,person_label,connection_state,generation,feeds,consent_reference}`. A feed is `{feed_id,role,window_binding,crop,observed_format,record_state,obs_binding}`. Roles are game or camera; role uniqueness prevents duplicate slots. Connection state means the host’s association with a visible received participant, not an inferred Discord account connection. No Discord API is needed.

Rust enumerates top-level Windows candidates and records HWND, PID, executable/class, title, geometry, visibility and a local discovery generation. The host selects a preview, labels the person, assigns its role and confirms the content rectangle. The binding includes observed process identity and generation to resist recycled handles. Enumeration implementation beyond the cited HWND/PID primitives is required work; exact enumeration helper API is AUSENTE — no inventar. [CITED: D02] [CITED: D06]

A Go Live pop-out is a practical game feed. A separate camera-only native window is not documented reliably. The camera slot may reference the call pop-out plus an explicitly chosen tile crop; several feeds can therefore refer to different rectangles of the same window. When a layout changes, that crop loses confidence and must be rechecked. Café does not infer whose face occupies a tile. If stable isolation is unavailable, leave the camera slot unavailable and record the game; required-feed preparation must report the missing camera. [CITED: D01]

A title change on a still-valid native binding updates metadata without reallocating the feed. A close event invalidates its generation, stops new frame admission, finalizes the surviving recording prefix and requests hiding the managed OBS item. A recreated window requires re-pairing; same executable or title alone cannot establish identity. Duplicate OBS descriptors block automatic send. Minimized windows report unavailable rather than continuing a falsely healthy recording. Crop geometry uses capture pixels; DPI or size changes invalidate geometry-dependent approvals. [CITED: D02] [CITED: D04]

## 5 [TARGET]. Native recorder end to end

### Capture and frame path

Select direct Windows Graphics Capture through windows-rs, targeting the chosen HWND through `IGraphicsCaptureItemInterop::CreateForWindow`. Check `GraphicsCaptureSession::IsSupported`, create a BGRA-capable device, use `Direct3D11CaptureFramePool::CreateFreeThreaded`, and retain `SystemRelativeTime` with `ContentSize`. Copy into recorder-owned storage before releasing the capture frame. A queued borrowed capture surface is forbidden. The exact sketch in the answer omits production device construction and teardown; implementation must supply both. [CITED: D02]

The HWND path needs build 18362 [CITED: D02]; cursor exclusion needs build 19041 [CITED: D02]. Café’s initial Windows capture profile requires the latter [TARGET]. Border removal is optional, capability/access dependent, and must never defeat recording indicators. Ordinary Windows client marketing names do not prove optional API availability. Occluded-window capture is supported by the mechanism; source activity still requires a real Discord test. Minimized-window freshness is not supported. [CITED: D02]

The fixed pipeline is WGC texture → bounded recorder-owned staging buffers → row-pitch-aware tightly packed BGRA → monotonic selector/pacer → bounded ordered output queue → dedicated FFmpeg stdin writer. CPU readback and upload to a GPU encoder are real costs. This is deliberately the recstream ownership pattern, not a claim of zero-copy. Callbacks cannot block on disk or encoder pipes. Bound outstanding allocations as well as queue lengths. [CITED: C22] [CITED: D03]

At admission, freeze crop, output dimensions, rational frame cadence, color conversion, encoder and storage limits. Keep observed Discord quality distinct from window size and recording canvas. Do not upscale to claim missing detail. Preserve an available 1080p content region [TARGET]; otherwise report the lower observed capture profile. Exact bitrate, frame cadence and queue budgets are AUSENTE — no inventar until profile qualification. Recreate capture resources after size changes; either letterbox into the admitted canvas after crop reconfirmation or close the segment. Never feed changed raw dimensions into an existing stream.

The pacer uses absolute monotonic deadlines. Each output slot retains its timeline position. Source-store replacement is counted separately from intentional repeats; missing frames may use a labelled held frame within the admitted stall policy. Full output queue, excessive lateness or a partially written raw frame fails the segment rather than silently shortening elapsed video. A stall beyond the qualified limit stops recording and exposes the reason. [CITED: C22]

### Encoding and container

Rust launches the audited FFmpeg executable directly with an argument vector, without a shell. The build unit constructs a complete tested rawvideo invocation from the cited input options; the harvested truncated command lines are not executable recipes. Stdin receives pixels only; FFmpeg’s input cadence and frame count express the paced timeline. Drain stderr independently into a bounded sanitized diagnostic tail. [CITED: C22] [CITED: D03]

Choose a working H.264 hardware encoder through a real preflight on the demo PC. Candidate names are `h264_nvenc`, `h264_amf` and `h264_qsv`; `hevc_nvenc` is a separately qualified alternative, not the default archival profile. Compiled support does not establish hardware/driver availability. No silent libx264 fallback: it changes performance and distribution obligations. If no approved encoder initializes, refuse recording with an actionable capability report. [CITED: D03]

Select Matroska as the active sink, favoring recoverability of written media; fragmented MP4 remains a future qualified alternate from the previous design. Ordinary unfinished MP4 is not the crash-safe recording sink. Recoverability does not guarantee survival of a buffered tail, power loss or filesystem damage. Startup scans interrupted files, validates readable streams and marks them recovered-partial. A successful process exit alone cannot classify media complete. [CITED: D03]

Use the chosen root with proposed relative names `Cafe/session_<opaque>/take_<opaque>/capsule_<opaque>_<role>_segment_<opaque>.partial.mkv` [TARGET]. IDs, not participant-supplied names, determine filenames. Finalization publishes a validated `.mkv` without overwriting another file. The associated manifest records feed generation, crop, quality caveats, timestamps, drops/repeats, encoder, byte count, digest, consent reference and completion state. Paths are schema targets, not existing files. Low disk, lost folder access and failed flush stop admission and preserve recoverable evidence.

On stop, cease capture admission, drain bounded queued slots, close stdin, wait for FFmpeg within the frozen deadline, validate output and publish the receipt. Deadline failure produces interrupted state; implementation must establish bounded child ownership on Windows. Renderer crashes leave Rust recording; Rust failure closes the media producer, while surviving FFmpeg may finish EOF processing. Recovery never depends on a destructor. The independent FFmpeg `gfxcapture` GPU route is deferred because it changes capture ownership and requires separate lifecycle qualification. [CITED: D03]

### Mixed audio and synchronization

Capture Discord’s rendered process-tree mix through WASAPI process loopback. Resolve the appropriate audio-producing tree, not merely any Discord executable. Use `ActivateAudioInterfaceAsync`, `VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK` and include-target-process-tree activation; retain packet timing and discontinuity flags. Direct windows bindings are selected; no endpoint or desktop audio fallback is automatic. The documented process-loopback floor is build 20348 [CITED: D06]. Windows 10 therefore needs runtime probing, with an explicit unsupported state on older installations. A streamer may separately consent to an identified whole-output-device fallback profile; otherwise record video with audio clearly unavailable. [CITED: D06]

For the MVP, record the shared mixed audio as a companion stream/file owned by Rust, referenced by every participating feed’s manifest. This avoids inventing an unverified extra Windows FFmpeg input-pipe contract. The proposed sink stores PCM with explicit format, sample count and durable-prefix metadata, then finalizes an interoperable audio file after validation; exact container writer/library is AUSENTE — no inventar and must be qualified in the audio unit. Recording completeness includes that companion when audio was required. Embedded per-video audio is deferred until the corresponding muxing profile is proved.

WGC and WASAPI use a common QPC-derived timeline; the audio packet position is already in 100-nanosecond units [CITED: D06]. Preserve the actual initial audio/video offset against a shared take epoch. Fill documented gaps deliberately, trim overlaps under policy, and log drift correction rather than stamping callback arrivals. Rawvideo drops timestamp metadata, so paced output positions and the manifest preserve the relation. The companion audio retains its own sample-to-clock map. Common host timing cannot undo offsets already introduced by Discord. Validate with decoded visible/audible references; no frame-accurate or synchronized classification without a frozen tolerance and measured evidence. [CITED: D06] [CITED: C06]

## 6 [TARGET]. OBS automation

[Send to OBS] commands OBS to capture the selected Discord window itself. No Café video is piped into OBS. The password wizard accepts host/port/password, checks authentication and reports capabilities. Default connection is local to the host; exact port default is AUSENTE — no inventar. Local Codex must reach this host through Café’s broker. [CITED: D04]

The following ordered sequence is the sealed allowlist. Provider constants in the settings sketch inherit [CITED: D04]. Perform each correlated request only after the preceding required response; batches are not transactions.

| Step | Exact request / action | Gate and negative |
|---|---|---|
| Authenticate | Hello → Identify → Identified | No requests before successful handshake |
| Discover | `GetVersion`, `GetInputKindList`, `GetSceneList`, `GetInputList` | Required requests and `window_capture` must exist |
| Reserve identity | Reuse a journal-owned input or `CreateInput` in the selected existing staging scene | Name collision with an unowned input refuses |
| Enumerate | `GetInputPropertiesListPropertyItems` with `propertyName:"window"`; also inspect `"method"` | Select enabled exact descriptor; ambiguity refuses |
| Bind | `SetInputSettings`, `overlay:true` | WGC method supported; selected descriptor still matches native target |
| Crop | `CreateSourceFilter` with `filterKind:"crop_filter"`, or `SetSourceFilterSettings` for owned filter | Crop valid in source pixels; changed geometry refuses |
| Layout | `SetSceneItemTransform` using returned/resolved item identity | Only approved writable transform fields |
| Validate/show | `SetSceneItemEnabled` in staging; `CreateSceneItem` in a distinct destination only if absent; enable destination after confirmation | Request success alone cannot establish visible pixels |
| Reconcile | `GetInputSettings`, `GetSceneItemId`, `GetSceneItemTransform`, `GetSceneItemEnabled` | Mismatch or timeout remains uncertain |

Create hidden with `inputKind:"window_capture"`, `inputSettings:{window:"",method:2,priority:0,cursor:false,client_area:true,compatibility:false,capture_audio:false}`, `sceneItemEnabled:false` [CITED: D04]. Creation already creates the staging scene item: do not duplicate it. If destination and staging differ, resolve the existing destination item with `GetSceneItemId` and create only the missing instance with `CreateSceneItem`, initially hidden. If they are the same scene, reuse that item. No automatic program-scene switch is implied. [CITED: C10] Copy OBS’s returned `itemValue`, not the display label. Its descriptor is encoded title/class/executable; it is not an HWND. Descriptor escaping replaces `#` then `:` as specified in the answer, but enumeration avoids hand-built descriptors.

For crop use `{relative:true,left,top,right,bottom}` or `{relative:false,left,top,cx,cy}`. No fixed Discord crop offsets are sealed. Transform uses `sceneItemTransform` with `positionX`, `positionY`, bounds and scaling fields supported by the prior request matrix; preserve aspect ratio with `OBS_BOUNDS_SCALE_INNER`. Source-filter crop and scene-item crop must not both crop the same content inadvertently. [CITED: D04] [CITED: C10]

Use strict title matching, never executable fallback. OBS may keep its existing HWND through a title change, but later reacquisition can need the new descriptor. Native closure detection requests hiding the managed item; OBS does not automatically remove it. Identical descriptors cannot uniquely target windows through stock settings, so automatic send refuses ambiguity. A close/hide race remains a privacy risk requiring the negative test. Shared Discord audio stays managed separately in OBS to avoid doubling existing Desktop Audio. [CITED: D04] [CITED: D06]

## 7 [TARGET]. Twitch data layer

Rust runs public-client Device Code Flow without an embedded client secret. Use the returned authorization URI, polling interval and expiry, validate account/client/scopes at startup and hourly [CITED: D05], serialize refresh and durably replace rotating refresh credentials before further issuance. Python receives only the current short-lived access lease. Secrets never enter JSONL, UI projections, journal payloads or MCP. [CITED: D05]

Subscriptions use the broadcaster’s user access token and the Welcome session ID. The table defines the required collection set. B denotes the authenticated broadcaster. Condition bindings shown as targets must be checked against exact provider fixtures before subscription creation; harvested main-table condition cells are missing, so they are not claimed as observed responses. Lifecycle/chat deletion condition keys are explicitly supplied by D05; raid direction keys are supplied by C13.

| Data | Type | Version | Scope | Condition binding |
|---|---|---|---|---|
| Chat | `channel.chat.message` | `1` [CITED: D05] | `user:read:chat` | broadcaster_user_id=B, user_id=B [TARGET] |
| Follows | `channel.follow` | `2` [CITED: D05] | `moderator:read:followers` | broadcaster_user_id=B, moderator_user_id=B [TARGET] |
| Subs / gifts | `channel.subscribe`, `channel.subscription.gift` | `1` [CITED: D05] | `channel:read:subscriptions` | broadcaster_user_id=B [TARGET] |
| Resub messages / ends | `channel.subscription.message`, `channel.subscription.end` | `1` [CITED: D05] | `channel:read:subscriptions` | broadcaster_user_id=B [TARGET] |
| Cheers | `channel.cheer` | `1` [CITED: D05] | `bits:read` | broadcaster_user_id=B [TARGET] |
| Incoming / outgoing raids | `channel.raid` | `1` [CITED: D05] | No topic scope | to_broadcaster_user_id=B / from_broadcaster_user_id=B [CITED: C13] |
| Custom redemptions | `channel.channel_points_custom_reward_redemption.add`, `.update` | `1` [CITED: D05] | `channel:read:redemptions` | broadcaster_user_id=B [TARGET] |
| Automatic redemptions | `channel.channel_points_automatic_reward_redemption.add` | `2` [CITED: D05] | `channel:read:redemptions` | broadcaster_user_id=B [TARGET] |
| Stream lifecycle | `stream.online`, `stream.offline` | `1` [CITED: D05] | No topic scope | broadcaster_user_id=B [CITED: D05] |
| Chat deletion | `channel.chat.message_delete`, `channel.chat.clear_user_messages`, `channel.chat.clear` | `1` [CITED: D05] | `user:read:chat` | broadcaster_user_id=B, user_id=B [CITED: D05] |

Do not sum gifts, recipient subscriptions and resub messages as independent new subscriptions. Cheers are collected without also subscribing to overlapping `channel.bits.use`. Subscribe through Helix `POST /eventsub/subscriptions`, reconcile through `GET /eventsub/subscriptions`, and receive notifications at the cited EventSub WebSocket endpoint. Planned reconnect migrates subscriptions; unexpected disconnect recreates them and records a gap. Durable message-ID dedup precedes acknowledgement to the local connector. No provider replay is assumed. [CITED: D05]

Sample Helix `GET /streams?user_id=<B>` at startup and every 60 seconds while live [TARGET; recommendation CITED: D05]. Honor returned rate-limit headers and reset times. Viewer count is an estimate sampled from current state, not unique audience or official historical analytics. Successful empty data means offline; failure means unknown, never zero. Welcome’s actual keepalive value governs liveness; do not hardcode a documentation example.

Write events through Rust to `Cafe/twitch/<account_handle>/<session_handle>/events_<segment>.jsonl` under the chosen folder [TARGET]. Schema: `{schema,event_id,provider_message_id,subscription_type,subscription_version,account_handle,stream_id,provider_time,received_at,received_monotonic,collection_epoch,payload,provenance}`. Samples add `{sampled_at,viewer_count,outcome}`; gaps carry reason and interval. Missing counts are null. Rotation is bounded by the implementation profile. Payloads are validated and minimized; no bearer material or arbitrary connector fields.

Retention is a required store feature, not an indefinite-archive promise. D05 cites restrictions on long-term non-chat storage and third-party AI disclosure. Use a 24-hour cache ceiling for non-chat data pending documented authorization [TARGET; policy reading CITED: D05]; chat expiry must be explicitly configured before collection. Physical purge/compaction must remove deleted text and invalidate query indexes. The append-only authority journal stores deletion receipts and opaque IDs, never chat text. MCP exposure to cloud Codex remains policy-gated; synthetic fixtures demonstrate the required tools when real-data permission is absent. Streamer confirmation alone does not supply Twitch’s permission. No markers or clips are in the MVP.

## 8 [TARGET]. MCP tools and local Codex

Use the standard Python SDK stdio broker. A01 and audit A10 resolve the prior design objections without claiming a new protocol-date pin. Closed schemas reject extra fields; tool responses carry provenance, revision, stale/gap state, cursor and redaction summary. All query bounds are validated against the configured profile. [CITED: C18] [CITED: C19]

| Tool [TARGET] | Typed arguments | Typed result / authority |
|---|---|---|
| `session_overview` | `{session_id:string}` | Redacted capsule, recording, OBS and collector state |
| `list_recordings` | `{session_id:string,cursor?:string,limit:integer}` | Manifest summaries and completeness, no arbitrary file reads |
| `query_twitch_events` | `{account_handle:string,kinds:string[],from:string,to:string,cursor?:string,limit:integer}` | Policy-filtered event page and collection gaps |
| `query_viewer_samples` | `{account_handle:string,from:string,to:string,cursor?:string,limit:integer}` | Sample page, unknown outcomes and coverage |
| `propose_obs_send` | `{session_id:string,feed_id:string,scene_handle:string,expected_revision:integer}` | Immutable proposal awaiting streamer confirmation |
| `propose_take` | `{session_id:string,feed_ids:string[],action:enum(prepare,start,stop),expected_revision:integer}` | Consent/readiness checked proposal; no self-approval |
| `operation_receipt` | `{request_id:string}` | Prior receipt or explicit uncertainty |

Twitch is read-only in this scope. A future Twitch mutation would require the same streamer-confirmation gate; no generic Twitch write tool is exposed. Chat text is untrusted data and cannot invoke tools. Query tools enforce retention and sharing policy before returning data, including aggregates. Local Codex execution does not itself prove local AI processing.

Registration template, using the source’s syntax but a deployment-supplied path; placeholder executable is AUSENTE — no inventar until packaging [CITED: C19]:

```powershell
codex mcp add caf -- "<verified-installed-cafe-mcp-executable>"
codex mcp get caf --json
codex mcp list --json
```

No credentials belong in registration arguments or configuration. Use a verified absolute Windows launcher path; never copy a hypothetical Unix installation path into the package. Then run the actual local Codex client, discover the typed tools and invoke `session_overview` against the running desktop program. Listing a registration is insufficient evidence of a successful tool call.

## 9 [TARGET]. Append-only journal

Rust is the sole writer. No SQL is required. Records carry `{schema,session_id,authority_epoch,sequence,event_id,kind,payload,prior_digest,record_digest}`. Canonical encoding, digest algorithm, flush policy and framing are frozen before durability qualification; their exact library pins are AUSENTE — no inventar. Journal durable admission precedes a committed reply. Reducer replay rebuilds operation dedup, feed generations, proposals and recording inventory. [CITED: C23]

A torn final record may be quarantined as an interrupted suffix; corruption inside the committed prefix blocks replay. External OBS success and journal admission are separate states. Lost replies require readback, not blind repetition. Record-all preserves per-feed outcomes and cannot manufacture atomic start. Media publication uses intent → file validation/publication → durable receipt; startup reconciles boundaries. No journal transaction can atomically commit OBS and media files.

Twitch JSONL is a separate retention-governed dataset. The journal remains append-only because it contains control receipts rather than sensitive payload copies. Deletion rewrites retained data segments through the sole writer while recording only opaque purge receipts in the journal. Checkpoints need verified ancestry and replay equality before rotation; deleting evidence to make recovery pass is forbidden.

## 10 [TARGET]. Security and participant consent

Show a persistent reminder: “Record only with participants’ consent.” The host acknowledges consent before enabling recording, including shared call-view tiles and mixed Discord audio; joining a call is insufficient. The UI must name the save folder, mixed-audio scope and intended rebroadcast destination. Withdrawal stops future admission promptly and offers retention/deletion handling. Recording indicators remain visible even when capture borders are unavailable. [CITED: D01] [CITED: C17]

Keep OAuth refresh tokens and persistent OBS credentials in Rust’s verified OS-backed vault. Fail closed on insecure fallback; test with synthetic canaries, never real secrets in diagnostics. Private connector pipes are not event buses: token-lease records cannot reach journal serialization. Broker credentials are distinct from provider access. Renderer isolation, caller validation and narrow preload methods prevent filesystem and process-launch escape hatches. Paths stay under an approved canonical root; prevent traversal, unsafe links and accidental overwrite. [CITED: C20] [CITED: C24]

The settings contract remains small: save folder/disk, OBS wizard, Twitch Device Code Flow, plus necessary consent/state actions. Resource budgets, codec tuning and protocol settings belong to validated backend profiles rather than a wall of controls. Privacy and sharing permissions remain visible where they affect actual data use.

## 11 [TARGET]. Windows packaging

Ship an unsigned hackathon build for Windows 10/11 [TARGET], with Electron, the Rust executable, private CPython, Python adapters/broker and audited FFmpeg outside executable ASAR resources. Exact Windows CPU architecture, artifact names, runtime hashes and installer format are AUSENTE — no inventar until the demo-PC inventory is supplied. Do not claim support from runtime availability alone. No global Python or runtime download may rescue an incomplete package. [CITED: C25]

Audit the complete FFmpeg configuration, not just omission of libx264. Hardware encoders can exist in an LGPL-compatible build; actual dependencies and runtime support still matter. Ship licence texts, notices, corresponding source/build information and compatible redistribution terms. Reject nonredistributable configurations. Codec patents are a separate review question. Native encoding remains required despite the older audit’s remux-only proposal. [CITED: D03]

Linux arm64 runs reducer, journal, contracts, broker fixtures and synthetic media tests. Capture may return an explicit unsupported capability. D07 finds no official Discord Linux arm64 desktop distribution; browser playback is a convenience, not Windows qualification. No signed multi-platform release is part of this seal. [CITED: D07]

## 12 [TARGET]. Gates with negatives

Every implementation unit supplies its gate before the dependent action. Commands and receipts belong in the build plan; named predicates here are not installed runners. A passing fixture cannot qualify native capture or the packaged demo.

| Gate | Positive evidence | Negative that must fail |
|---|---|---|
| Bootstrap | Reviewed copy hashes, capped entry plane, fresh source lookup and unique anchors | Missing kit, stale lookup, dangling anchor or unknown schema |
| Contract | Nonempty cross-runtime valid fixture set | Extra key, stale epoch, oversized line or precision loss |
| Registry | Manual pairing preserves person/feed generations | Reused HWND or duplicate descriptor silently rebinds |
| Capture | Actual restored and occluded Discord window produces fresh frames | Minimized, protected or closed target appears healthy |
| Recorder | Bounded allocation, paced timeline, playable written media | Partial write resumes or full queue silently shortens time |
| Audio | Correct process-tree mix and timestamp/sample mapping | Desktop fallback without consent or missing audio marked complete |
| Recovery | Interrupted media and valid journal prefix classified honestly | Internal corruption or missing file passes complete |
| OBS | Owned source, intended crop and visible captured pixels match approval | Name collision, ambiguous descriptor or setter-only success |
| Twitch | Correct scopes/conditions, refresh, dedup, reconnect and rate behavior | Missing scope, reused refresh credential or failed sample becomes zero |
| Data policy | Expiry/deletion removes retained payload and indexes | Tombstone leaves text queryable or export bypasses policy |
| Approval | Exact immutable proposal confirmed by streamer | AI approves itself or changed crop reuses consent |
| MCP | Actual local Codex discovery and read call | Registration alone or stdout noise counts as pass |
| Package | Installed unsigned artifact works without development runtimes | Global Python or unbundled DLL masks a missing dependency |

## 13 [TARGET]. Risks and open items

| Item | Sealed mitigation | Remaining closure |
|---|---|---|
| Camera isolation | Explicit crop pairing; unavailable is allowed | Stable call layout and resize/participant-change test |
| OBS identity gap | Strict descriptor, native monitoring, ambiguity refusal | Duplicate-title and closure/hide race on demo OBS |
| Raw-frame cost | Bounded buffers and measured admission | Sustained combined load on actual Windows hardware |
| Audio compatibility | Runtime probe; companion mix; explicit unsupported state | Audio container writer and ordinary Windows compatibility |
| Encoder availability | Real encode preflight, no silent licensing fallback | Audited artifact and selected GPU/driver |
| Drift and recovery | Source clocks, decoded reference, partial classifications | Tolerances and sustained crash/disk tests |
| Twitch evidence gaps | Target condition map; no question-fragment facts | Exact provider fixtures for missing condition cells |
| Twitch policy | Retention/deletion and sharing gate | Written permission or approved policy for real Codex data |
| Bootstrap interfaces | Required owned implementation units | No first router-qualified commit until source gates pass |
| Distribution | Unsigned Windows build and private sidecars | Demo architecture, vault backend, locks and install receipt |

These open items gate their affected operations; they do not restore abandoned product scope. Technical readiness and data-policy readiness are reported separately. Blocked Twitch authorization must remain visible because the hackathon requirement is not optional.

## 14 [TARGET]. Changes vs v2

Dropped: browser guest page, LiveKit/SFU, public tunnel/ingress, tus upload, Discord bot, Guest Star, sender-side recording, browser persistence and signed multi-OS packaging. Their old prerequisites and gates no longer apply. No remote recording is described as a sender-owned original.

Changed: host-side Discord window capture becomes the media boundary; Rust’s native recorder is mandatory; OBS uses Window Capture instead of a browser receiver. Mixed application audio replaces isolation claims. Camera pairing acknowledges the call-view limitation. Matroska active recording replaces the prior fragmented-MP4 default, retaining crash qualification. Windows alone ships; Linux arm64 is development convenience. JavaScript/JSDoc remains mandatory; authored TypeScript must never be introduced.

Retained and corrected: Rust authority, process separation, narrow UI seam, append-only journal, operator-only commits and small Codex units remain. A01 permits short-lived access tokens in Python memory. Twitch becomes a bounded local JSONL collection/query layer without markers or clips. Settings collapse to folder and connection wizards. Streamer confirmation still binds AI effects, while retention and third-party data permission remain independent gates.
