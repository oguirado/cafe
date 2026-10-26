//! P06 authority append-only journal.
//!
//! Single-writer durable log of control and media receipts. Records are
//! canonical JSON objects chained by SHA-256 digests and stored in
//! length-prefixed frames with a commit trailer. Staging writes the frame
//! header plus body; only `commit` appends the trailer and flushes, so a
//! crash between the two reopens as a quarantined interrupted suffix.
//!
//! Recovery scanning lives here so both `Journal::open` and
//! `journal_replay::recover` share one locked validator. Items shared with
//! the replay module are `pub(crate)`; the unrestricted `pub` surface is
//! exactly the specified unit interface.

#![forbid(unsafe_code)]

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::capsule_registry::{Clock, Envelope, Outcome, Registry};

/// Journal wire schema identifier.
pub const JOURNAL_SCHEMA: &str = "cafe.journal.v1";

/// Maximum canonical record body size in bytes.
pub const MAX_RECORD_BYTES: usize = 1_048_576;

/// Genesis prior digest for sequence 1: 64 lowercase ASCII zeros.
const GENESIS_DIGEST: &str =
    "0000000000000000000000000000000000000000000000000000000000000000";

/// Four-byte commit trailer terminating every durable frame.
const TRAILER: &[u8] = b"CJOK";

/// Process-wide staged-frame token source; every stage gets a unique id.
static NEXT_STAGE_ID: AtomicU64 = AtomicU64::new(1);

/// Journal failure. Carries no paths or input excerpts.
#[derive(Debug, PartialEq, Eq)]
pub enum JournalError {
    Io,
    Locked,
    Corrupt,
    Invalid,
    Pending,
    Media,
}

impl std::fmt::Display for JournalError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let code = match self {
            JournalError::Io => "journal_io",
            JournalError::Locked => "journal_locked",
            JournalError::Corrupt => "journal_corrupt",
            JournalError::Invalid => "journal_invalid",
            JournalError::Pending => "journal_pending",
            JournalError::Media => "journal_media",
        };
        formatter.write_str(code)
    }
}

impl std::error::Error for JournalError {}

/// Durability state of an append token.
#[derive(Debug, PartialEq, Eq)]
pub enum AppendState {
    Pending,
    Committed,
}

/// One hash-chained journal record: exactly nine wire fields.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Record {
    pub schema: String,
    pub session_id: String,
    pub authority_epoch: Clock,
    pub sequence: Clock,
    pub event_id: String,
    pub kind: String,
    pub payload: Value,
    pub prior_digest: String,
    pub record_digest: String,
}

/// Pre-durable stage token. Not a receipt; proves nothing durable.
#[derive(Debug)]
pub struct Pending {
    stage_id: u64,
    event_id: String,
}

impl Pending {
    pub fn state(&self) -> AppendState {
        AppendState::Pending
    }
}

/// Durable commit receipt. Constructible only through `Journal::commit`
/// (or the atomic append helpers) after the frame trailer is flushed.
#[derive(Debug)]
pub struct Committed {
    sequence: Clock,
    event_id: String,
    record_digest: String,
}

impl Committed {
    pub fn state(&self) -> AppendState {
        AppendState::Committed
    }

    pub fn sequence(&self) -> Clock {
        self.sequence
    }

    pub fn event_id(&self) -> &str {
        &self.event_id
    }

    pub fn record_digest(&self) -> &str {
        &self.record_digest
    }
}

/// Exact `media_intent` payload: `{media_id, name}` and nothing else.
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct MediaIntentPayload {
    media_id: String,
    name: String,
}

/// Exact `media_published` payload: `{byte_len, media_id, name, sha256}`.
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct MediaPublishedPayload {
    byte_len: Clock,
    media_id: String,
    name: String,
    sha256: String,
}

/// In-memory staged frame awaiting its commit trailer.
struct StagedControl {
    stage_id: u64,
    event_id: String,
    record: Record,
    envelope: Envelope,
}

/// Outcome of scanning the on-disk prefix during `open`.
struct Scan {
    records: Vec<Record>,
    registry: Registry,
    event_ids: BTreeSet<String>,
    media_intents: BTreeMap<String, String>,
    media_published: BTreeSet<String>,
    prefix_len: usize,
    suffix: Vec<u8>,
}

/// Exclusive single-writer journal handle. Holds the locked file for its
/// whole lifetime; dropping releases the lock.
pub struct Journal {
    file: File,
    path: PathBuf,
    session_id: String,
    epoch: Clock,
    records: Vec<Record>,
    registry: Registry,
    event_ids: BTreeSet<String>,
    media_intents: BTreeMap<String, String>,
    media_published: BTreeSet<String>,
    status: crate::journal_replay::RecoveryStatus,
    interrupted: Vec<u8>,
    staged: Option<StagedControl>,
    poisoned: bool,
}

/// Canonical JSON bytes: recursively sorted object keys, compact UTF-8,
/// integers in decimal form. Any floating-point number anywhere fails.
pub fn canonical_bytes(value: &Value) -> Result<Vec<u8>, JournalError> {
    let sorted = sort_value(value)?;
    serde_json::to_vec(&sorted).map_err(|_| JournalError::Invalid)
}

/// Lowercase hexadecimal SHA-256 digest of `bytes`.
pub(crate) fn sha256_hex(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let digest = Sha256::digest(bytes);
    let mut out = String::with_capacity(64);
    for byte in digest {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 15) as usize] as char);
    }
    out
}

/// Safe publication name: one normal relative path component, no
/// separators, no dot segments, no NUL.
pub(crate) fn is_safe_name(name: &str) -> bool {
    if name.is_empty() || name == "." || name == ".." {
        return false;
    }
    if name.contains('/') || name.contains('\\') || name.contains('\0') {
        return false;
    }
    let mut components = Path::new(name).components();
    match (components.next(), components.next()) {
        (Some(Component::Normal(_)), None) => true,
        _ => false,
    }
}

/// Recursively sorts object keys; rejects every float number.
fn sort_value(value: &Value) -> Result<Value, JournalError> {
    match value {
        Value::Null | Value::Bool(_) | Value::String(_) => Ok(value.clone()),
        Value::Number(number) => {
            if number.is_i64() || number.is_u64() {
                Ok(value.clone())
            } else {
                Err(JournalError::Invalid)
            }
        }
        Value::Array(items) => {
            let mut out = Vec::with_capacity(items.len());
            for item in items {
                out.push(sort_value(item)?);
            }
            Ok(Value::Array(out))
        }
        Value::Object(map) => {
            let mut entries: Vec<(&String, &Value)> = map.iter().collect();
            entries.sort_by(|left, right| left.0.cmp(right.0));
            let mut out = serde_json::Map::with_capacity(map.len());
            for (key, item) in entries {
                out.insert(key.clone(), sort_value(item)?);
            }
            Ok(Value::Object(out))
        }
    }
}

/// True for exactly 64 lowercase hexadecimal characters.
fn is_lower_hex_64(text: &str) -> bool {
    text.len() == 64
        && text
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

/// Serializes a record to its nine-field JSON value.
fn record_value(record: &Record) -> Result<Value, JournalError> {
    serde_json::to_value(record).map_err(|_| JournalError::Invalid)
}

/// Digest over the eight-field preimage (full record minus
/// the self-referential `record_digest`, including `prior_digest`).
fn digest_of_preimage(record: &Record) -> Result<String, JournalError> {
    let mut value = record_value(record)?;
    match &mut value {
        Value::Object(map) => {
            map.remove("record_digest");
        }
        _ => return Err(JournalError::Invalid),
    }
    let bytes = canonical_bytes(&value)?;
    Ok(sha256_hex(&bytes))
}

/// Flushes the directory owning `path` so creations and renames there
/// survive a crash.
fn sync_parent_dir(path: &Path) -> Result<(), JournalError> {
    let parent = path
        .parent()
        .filter(|candidate| !candidate.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let dir = File::open(parent).map_err(|_| JournalError::Io)?;
    dir.sync_all().map_err(|_| JournalError::Io)?;
    Ok(())
}

/// Scans frames from offset zero, fully validating every complete record
/// (framing, canonical bytes, chain, replay) before reporting a torn tail.
/// Any complete corrupt frame aborts with `Corrupt` and no side effects.
fn scan_prefix(
    bytes: &[u8],
    session_id: &str,
    epoch: Clock,
) -> Result<Scan, JournalError> {
    let mut records = Vec::new();
    let mut registry = Registry::new(epoch);
    let mut event_ids = BTreeSet::new();
    let mut media_intents = BTreeMap::new();
    let mut media_published = BTreeSet::new();
    let mut offset = 0usize;
    let mut expected_sequence: u64 = 1;
    let mut expected_prior = GENESIS_DIGEST.to_string();
    loop {
        let remaining = bytes.len() - offset;
        if remaining == 0 {
            return Ok(Scan {
                records,
                registry,
                event_ids,
                media_intents,
                media_published,
                prefix_len: offset,
                suffix: Vec::new(),
            });
        }
        if remaining < 8 {
            return Ok(Scan {
                records,
                registry,
                event_ids,
                media_intents,
                media_published,
                prefix_len: offset,
                suffix: bytes[offset..].to_vec(),
            });
        }
        let length = u32::from_be_bytes([
            bytes[offset],
            bytes[offset + 1],
            bytes[offset + 2],
            bytes[offset + 3],
        ]);
        let complement = u32::from_be_bytes([
            bytes[offset + 4],
            bytes[offset + 5],
            bytes[offset + 6],
            bytes[offset + 7],
        ]);
        if complement != !length || length == 0 || length as usize > MAX_RECORD_BYTES
        {
            return Err(JournalError::Corrupt);
        }
        let body_len = length as usize;
        let total = 8usize + body_len + 4;
        if remaining < total {
            return Ok(Scan {
                records,
                registry,
                event_ids,
                media_intents,
                media_published,
                prefix_len: offset,
                suffix: bytes[offset..].to_vec(),
            });
        }
        let body = &bytes[offset + 8..offset + 8 + body_len];
        let trailer = &bytes[offset + 8 + body_len..offset + 8 + body_len + 4];
        if trailer != TRAILER {
            return Err(JournalError::Corrupt);
        }
        let record = validate_record(
            body,
            session_id,
            epoch,
            expected_sequence,
            &expected_prior,
            &mut registry,
            &mut event_ids,
            &mut media_intents,
            &mut media_published,
        )?;
        expected_prior = record.record_digest.clone();
        records.push(record);
        expected_sequence =
            expected_sequence.checked_add(1).ok_or(JournalError::Corrupt)?;
        offset += total;
    }
}

/// Validates one complete record body: canonical bytes equal the stored
/// bytes, fields and hash chain match, and the payload applies cleanly.
fn validate_record(
    body: &[u8],
    session_id: &str,
    epoch: Clock,
    expected_sequence: u64,
    expected_prior: &str,
    registry: &mut Registry,
    event_ids: &mut BTreeSet<String>,
    media_intents: &mut BTreeMap<String, String>,
    media_published: &mut BTreeSet<String>,
) -> Result<Record, JournalError> {
    let parsed: Value = serde_json::from_slice(body).map_err(|_| JournalError::Corrupt)?;
    let canonical = canonical_bytes(&parsed).map_err(|_| JournalError::Corrupt)?;
    if canonical.as_slice() != body {
        return Err(JournalError::Corrupt);
    }
    let record: Record =
        serde_json::from_value(parsed.clone()).map_err(|_| JournalError::Corrupt)?;
    if record.schema != JOURNAL_SCHEMA {
        return Err(JournalError::Corrupt);
    }
    if record.session_id.is_empty() || record.session_id != session_id {
        return Err(JournalError::Corrupt);
    }
    if record.authority_epoch != epoch {
        return Err(JournalError::Corrupt);
    }
    if record.sequence != Clock(expected_sequence) {
        return Err(JournalError::Corrupt);
    }
    if record.event_id.is_empty() {
        return Err(JournalError::Corrupt);
    }
    if !event_ids.insert(record.event_id.clone()) {
        return Err(JournalError::Corrupt);
    }
    if record.prior_digest != expected_prior {
        return Err(JournalError::Corrupt);
    }
    let preimage = match parsed {
        Value::Object(mut map) => {
            map.remove("record_digest");
            Value::Object(map)
        }
        _ => return Err(JournalError::Corrupt),
    };
    let preimage_bytes =
        canonical_bytes(&preimage).map_err(|_| JournalError::Corrupt)?;
    if sha256_hex(&preimage_bytes) != record.record_digest {
        return Err(JournalError::Corrupt);
    }
    match record.kind.as_str() {
        "control" => {
            let envelope: Envelope = serde_json::from_value(record.payload.clone())
                .map_err(|_| JournalError::Corrupt)?;
            if registry.apply(envelope).state != Outcome::Admitted {
                return Err(JournalError::Corrupt);
            }
        }
        "media_intent" => {
            let intent: MediaIntentPayload =
                serde_json::from_value(record.payload.clone())
                    .map_err(|_| JournalError::Corrupt)?;
            if intent.media_id.is_empty() || !is_safe_name(&intent.name) {
                return Err(JournalError::Corrupt);
            }
            if media_intents.insert(intent.media_id, intent.name).is_some() {
                return Err(JournalError::Corrupt);
            }
        }
        "media_published" => {
            let receipt: MediaPublishedPayload =
                serde_json::from_value(record.payload.clone())
                    .map_err(|_| JournalError::Corrupt)?;
            if receipt.byte_len == Clock(0) || !is_lower_hex_64(&receipt.sha256) {
                return Err(JournalError::Corrupt);
            }
            if !is_safe_name(&receipt.name) {
                return Err(JournalError::Corrupt);
            }
            match media_intents.get(&receipt.media_id) {
                Some(name) if *name == receipt.name => {}
                _ => return Err(JournalError::Corrupt),
            }
            if !media_published.insert(receipt.media_id) {
                return Err(JournalError::Corrupt);
            }
        }
        _ => return Err(JournalError::Corrupt),
    }
    Ok(record)
}

/// Creates the quarantine sidecar next to the journal with `create_new`,
/// falling back to deterministic numbered siblings when taken.
fn create_sidecar(path: &Path) -> Result<File, JournalError> {
    let first = path.with_extension("interrupted");
    match OpenOptions::new().write(true).create_new(true).open(&first) {
        Ok(file) => return Ok(file),
        Err(error) if error.kind() != std::io::ErrorKind::AlreadyExists => {
            return Err(JournalError::Io);
        }
        Err(_) => {}
    }
    let mut counter: u32 = 1;
    loop {
        let candidate = path.with_extension(format!("interrupted.{counter}"));
        match OpenOptions::new().write(true).create_new(true).open(&candidate) {
            Ok(file) => return Ok(file),
            Err(error) if error.kind() != std::io::ErrorKind::AlreadyExists => {
                return Err(JournalError::Io);
            }
            Err(_) => {}
        }
        counter = counter.checked_add(1).ok_or(JournalError::Io)?;
    }
}

impl Journal {
    /// Opens or creates the journal, takes the exclusive lock, validates
    /// the committed prefix, and quarantines a torn final suffix.
    pub fn open(path: &Path, session_id: &str, epoch: Clock) -> Result<Self, JournalError> {
        if session_id.is_empty() {
            return Err(JournalError::Invalid);
        }
        let mut file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .open(path)
            .map_err(|_| JournalError::Io)?;
        file.try_lock().map_err(|_| JournalError::Locked)?;
        let mut bytes = Vec::new();
        file.read_to_end(&mut bytes).map_err(|_| JournalError::Io)?;
        let scan = scan_prefix(&bytes, session_id, epoch)?;
        let (status, interrupted) = if scan.suffix.is_empty() {
            (crate::journal_replay::RecoveryStatus::Clean, Vec::new())
        } else {
            let mut sidecar = create_sidecar(path)?;
            sidecar.write_all(&scan.suffix).map_err(|_| JournalError::Io)?;
            sidecar.sync_all().map_err(|_| JournalError::Io)?;
            sync_parent_dir(path)?;
            file.set_len(scan.prefix_len as u64).map_err(|_| JournalError::Io)?;
            file.sync_all().map_err(|_| JournalError::Io)?;
            (
                crate::journal_replay::RecoveryStatus::Interrupted,
                scan.suffix,
            )
        };
        file.seek(SeekFrom::End(0)).map_err(|_| JournalError::Io)?;
        Ok(Journal {
            file,
            path: path.to_path_buf(),
            session_id: session_id.to_string(),
            epoch,
            records: scan.records,
            registry: scan.registry,
            event_ids: scan.event_ids,
            media_intents: scan.media_intents,
            media_published: scan.media_published,
            status,
            interrupted,
            staged: None,
            poisoned: false,
        })
    }

    /// Validates a control request against the durable ledger and writes
    /// the frame header plus body without the commit trailer or receipt.
    pub fn stage_control(
        &mut self,
        event_id: &str,
        request: Envelope,
    ) -> Result<Pending, JournalError> {
        if self.poisoned {
            return Err(JournalError::Io);
        }
        if self.staged.is_some() {
            return Err(JournalError::Pending);
        }
        if event_id.is_empty() || self.event_ids.contains(event_id) {
            return Err(JournalError::Invalid);
        }
        let mut scratch = Registry::new(self.epoch);
        for record in &self.records {
            if record.kind == "control" {
                let envelope: Envelope = serde_json::from_value(record.payload.clone())
                    .map_err(|_| JournalError::Corrupt)?;
                if scratch.apply(envelope).state != Outcome::Admitted {
                    return Err(JournalError::Corrupt);
                }
            }
        }
        if scratch.apply(request.clone()).state != Outcome::Admitted {
            return Err(JournalError::Invalid);
        }
        let payload =
            serde_json::to_value(&request).map_err(|_| JournalError::Invalid)?;
        let (record, body) = self.build_record(event_id, "control", payload)?;
        let length = body.len() as u32;
        let mut frame = Vec::with_capacity(8 + body.len());
        frame.extend_from_slice(&length.to_be_bytes());
        frame.extend_from_slice(&(!length).to_be_bytes());
        frame.extend_from_slice(&body);
        if self.file.write_all(&frame).is_err() {
            self.poisoned = true;
            return Err(JournalError::Io);
        }
        let stage_id = NEXT_STAGE_ID.fetch_add(1, Ordering::SeqCst);
        self.staged = Some(StagedControl {
            stage_id,
            event_id: event_id.to_string(),
            record,
            envelope: request,
        });
        Ok(Pending {
            stage_id,
            event_id: event_id.to_string(),
        })
    }

    /// Appends the commit trailer, flushes, and only then advances the
    /// durable ledger and returns the receipt.
    pub fn commit(&mut self, pending: Pending) -> Result<Committed, JournalError> {
        if self.poisoned {
            return Err(JournalError::Io);
        }
        let staged = match self.staged.take() {
            Some(staged) => staged,
            None => return Err(JournalError::Pending),
        };
        if staged.stage_id != pending.stage_id || staged.event_id != pending.event_id
        {
            self.staged = Some(staged);
            return Err(JournalError::Pending);
        }
        if self.file.write_all(TRAILER).is_err() {
            self.poisoned = true;
            return Err(JournalError::Io);
        }
        if self.file.sync_all().is_err() {
            self.poisoned = true;
            return Err(JournalError::Io);
        }
        if sync_parent_dir(&self.path).is_err() {
            self.poisoned = true;
            return Err(JournalError::Io);
        }
        if self.registry.apply(staged.envelope).state != Outcome::Admitted {
            self.poisoned = true;
            return Err(JournalError::Corrupt);
        }
        self.event_ids.insert(staged.record.event_id.clone());
        let committed = Committed {
            sequence: staged.record.sequence,
            event_id: staged.record.event_id.clone(),
            record_digest: staged.record.record_digest.clone(),
        };
        self.records.push(staged.record);
        Ok(committed)
    }

    /// Stages and commits one control record.
    pub fn append_control(
        &mut self,
        event_id: &str,
        request: Envelope,
    ) -> Result<Committed, JournalError> {
        let pending = self.stage_control(event_id, request)?;
        self.commit(pending)
    }

    /// Commits a typed media intent for a new media id.
    pub fn append_media_intent(
        &mut self,
        event_id: &str,
        media_id: &str,
        published_name: &str,
    ) -> Result<Committed, JournalError> {
        if self.poisoned {
            return Err(JournalError::Io);
        }
        if self.staged.is_some() {
            return Err(JournalError::Pending);
        }
        if event_id.is_empty() || self.event_ids.contains(event_id) {
            return Err(JournalError::Invalid);
        }
        if media_id.is_empty() || self.media_intents.contains_key(media_id) {
            return Err(JournalError::Invalid);
        }
        if !is_safe_name(published_name) {
            return Err(JournalError::Media);
        }
        let payload = serde_json::to_value(MediaIntentPayload {
            media_id: media_id.to_string(),
            name: published_name.to_string(),
        })
        .map_err(|_| JournalError::Invalid)?;
        let (record, body) = self.build_record(event_id, "media_intent", payload)?;
        self.write_committed_frame(&body)?;
        self.event_ids.insert(record.event_id.clone());
        self.media_intents.insert(media_id.to_string(), published_name.to_string());
        let committed = Committed {
            sequence: record.sequence,
            event_id: record.event_id.clone(),
            record_digest: record.record_digest.clone(),
        };
        self.records.push(record);
        Ok(committed)
    }

    /// Validates and publishes a staged media file under its intent name
    /// without clobbering, then commits the publication receipt.
    pub fn publish_media(
        &mut self,
        event_id: &str,
        media_root: &Path,
        media_id: &str,
        staged_name: &str,
    ) -> Result<Committed, JournalError> {
        if self.poisoned {
            return Err(JournalError::Io);
        }
        if self.staged.is_some() {
            return Err(JournalError::Pending);
        }
        if event_id.is_empty() || self.event_ids.contains(event_id) {
            return Err(JournalError::Invalid);
        }
        let published_name = match self.media_intents.get(media_id) {
            Some(name) => name.clone(),
            None => return Err(JournalError::Media),
        };
        if self.media_published.contains(media_id) {
            return Err(JournalError::Media);
        }
        if !is_safe_name(staged_name) || !is_safe_name(&published_name) {
            return Err(JournalError::Media);
        }
        let root_meta =
            std::fs::symlink_metadata(media_root).map_err(|_| JournalError::Media)?;
        if root_meta.file_type().is_symlink() || !root_meta.is_dir() {
            return Err(JournalError::Media);
        }
        let staged_path = media_root.join(staged_name);
        let staged_meta =
            std::fs::symlink_metadata(&staged_path).map_err(|_| JournalError::Media)?;
        if staged_meta.file_type().is_symlink() || !staged_meta.is_file() {
            return Err(JournalError::Media);
        }
        if staged_meta.len() == 0 {
            return Err(JournalError::Media);
        }
        let bytes = std::fs::read(&staged_path).map_err(|_| JournalError::Io)?;
        if bytes.is_empty() {
            return Err(JournalError::Media);
        }
        let byte_len = Clock(bytes.len() as u64);
        let sha256 = sha256_hex(&bytes);
        File::open(&staged_path)
            .map_err(|_| JournalError::Io)?
            .sync_all()
            .map_err(|_| JournalError::Io)?;
        let target_path = media_root.join(&published_name);
        if staged_name != published_name {
            match std::fs::symlink_metadata(&target_path) {
                Ok(_) => return Err(JournalError::Media),
                Err(error)
                    if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(_) => return Err(JournalError::Io),
            }
            std::fs::rename(&staged_path, &target_path).map_err(|_| JournalError::Io)?;
            File::open(&target_path)
                .map_err(|_| JournalError::Io)?
                .sync_all()
                .map_err(|_| JournalError::Io)?;
            File::open(media_root)
                .map_err(|_| JournalError::Io)?
                .sync_all()
                .map_err(|_| JournalError::Io)?;
        }
        let payload = serde_json::to_value(MediaPublishedPayload {
            byte_len,
            media_id: media_id.to_string(),
            name: published_name,
            sha256,
        })
        .map_err(|_| JournalError::Invalid)?;
        let (record, body) = self.build_record(event_id, "media_published", payload)?;
        self.write_committed_frame(&body)?;
        self.event_ids.insert(record.event_id.clone());
        self.media_published.insert(media_id.to_string());
        let committed = Committed {
            sequence: record.sequence,
            event_id: record.event_id.clone(),
            record_digest: record.record_digest.clone(),
        };
        self.records.push(record);
        Ok(committed)
    }

    /// Durable committed records only; excludes any staged frame.
    pub fn records(&self) -> &[Record] {
        &self.records
    }

    /// Recovery outcome observed while opening.
    pub fn recovery_status(&self) -> crate::journal_replay::RecoveryStatus {
        self.status
    }

    /// Exact quarantined suffix bytes from opening; empty when clean.
    pub fn interrupted_bytes(&self) -> &[u8] {
        &self.interrupted
    }

    /// Owned observation of the durable control ledger for replay.
    pub(crate) fn snapshot(&self) -> crate::capsule_registry::Snapshot {
        self.registry.snapshot()
    }

    /// Next sequence: exactly one past the last committed record.
    fn next_sequence(&self) -> Result<Clock, JournalError> {
        match self.records.last() {
            None => Ok(Clock(1)),
            Some(last) => last
                .sequence
                .0
                .checked_add(1)
                .map(Clock)
                .ok_or(JournalError::Invalid),
        }
    }

    /// Prior digest chaining to the last committed record, else genesis.
    fn prior_digest(&self) -> String {
        self.records
            .last()
            .map(|record| record.record_digest.clone())
            .unwrap_or_else(|| GENESIS_DIGEST.to_string())
    }

    /// Builds a fully digested record plus its bounded canonical body.
    fn build_record(
        &self,
        event_id: &str,
        kind: &str,
        payload: Value,
    ) -> Result<(Record, Vec<u8>), JournalError> {
        let mut record = Record {
            schema: JOURNAL_SCHEMA.to_string(),
            session_id: self.session_id.clone(),
            authority_epoch: self.epoch,
            sequence: self.next_sequence()?,
            event_id: event_id.to_string(),
            kind: kind.to_string(),
            payload,
            prior_digest: self.prior_digest(),
            record_digest: String::new(),
        };
        record.record_digest = digest_of_preimage(&record)?;
        let body = canonical_bytes(&record_value(&record)?)?;
        if body.len() > MAX_RECORD_BYTES {
            return Err(JournalError::Invalid);
        }
        Ok((record, body))
    }

    /// Writes one complete frame (header, body, trailer) and flushes it
    /// durably; poisons the handle on any uncertain failure.
    fn write_committed_frame(&mut self, body: &[u8]) -> Result<(), JournalError> {
        let length = body.len() as u32;
        let mut frame = Vec::with_capacity(8 + body.len() + 4);
        frame.extend_from_slice(&length.to_be_bytes());
        frame.extend_from_slice(&(!length).to_be_bytes());
        frame.extend_from_slice(body);
        frame.extend_from_slice(TRAILER);
        if self.file.write_all(&frame).is_err() {
            self.poisoned = true;
            return Err(JournalError::Io);
        }
        if self.file.sync_all().is_err() {
            self.poisoned = true;
            return Err(JournalError::Io);
        }
        if sync_parent_dir(&self.path).is_err() {
            self.poisoned = true;
            return Err(JournalError::Io);
        }
        Ok(())
    }
}
