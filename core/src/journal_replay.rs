//! P06 recovery: locked scan plus deterministic reducer replay.
//!
//! `recover` opens the journal under the same exclusive lock (validating
//! the committed prefix and quarantining a torn suffix), rebuilds the P05
//! registry from the committed control records, and verifies every
//! publication receipt against the actual media bytes on disk. An intent
//! without a durable receipt stays `Pending`; a receipt whose file is
//! missing or altered fails closed.

#![forbid(unsafe_code)]

use std::collections::BTreeMap;
use std::path::Path;
use serde::Deserialize;

use crate::authority_journal::{sha256_hex, Journal, JournalError, Record};
use crate::capsule_registry::{Clock, Snapshot};

/// Recovery outcome: clean prefix or quarantined interrupted suffix.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RecoveryStatus {
    Clean,
    Interrupted,
}

/// Publication state of one media id. `Complete` requires the durable
/// receipt plus validated bytes on disk.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum MediaState {
    Pending,
    Complete {
        name: String,
        byte_len: Clock,
        sha256: String,
    },
}

/// Deterministic recovery observation.
#[derive(Debug, PartialEq)]
pub struct Recovery {
    pub snapshot: Snapshot,
    pub media: BTreeMap<String, MediaState>,
    pub status: RecoveryStatus,
    pub quarantined_bytes: Vec<u8>,
    pub records: Vec<Record>,
}

/// Exact `media_intent` payload view for replay.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct MediaIntentView {
    media_id: String,
    name: String,
}

/// Exact `media_published` payload view for replay.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct MediaPublishedView {
    byte_len: Clock,
    media_id: String,
    name: String,
    sha256: String,
}

/// Recovers the journal: locked scan, reducer replay, and media receipt
/// verification under `media_root`.
pub fn recover(
    path: &Path,
    media_root: &Path,
    session_id: &str,
    epoch: Clock,
) -> Result<Recovery, JournalError> {
    let journal = Journal::open(path, session_id, epoch)?;
    let root_meta =
        std::fs::symlink_metadata(media_root).map_err(|_| JournalError::Media)?;
    if root_meta.file_type().is_symlink() || !root_meta.is_dir() {
        return Err(JournalError::Media);
    }
    let mut intents: BTreeMap<String, String> = BTreeMap::new();
    let mut receipts: BTreeMap<String, (String, Clock, String)> = BTreeMap::new();
    for record in journal.records() {
        match record.kind.as_str() {
            "media_intent" => {
                let intent: MediaIntentView =
                    serde_json::from_value(record.payload.clone())
                        .map_err(|_| JournalError::Corrupt)?;
                intents.insert(intent.media_id, intent.name);
            }
            "media_published" => {
                let receipt: MediaPublishedView =
                    serde_json::from_value(record.payload.clone())
                        .map_err(|_| JournalError::Corrupt)?;
                receipts.insert(
                    receipt.media_id,
                    (receipt.name, receipt.byte_len, receipt.sha256),
                );
            }
            _ => {}
        }
    }
    let mut media: BTreeMap<String, MediaState> = BTreeMap::new();
    for (media_id, name) in &intents {
        match receipts.get(media_id) {
            None => {
                media.insert(media_id.clone(), MediaState::Pending);
            }
            Some((receipt_name, byte_len, sha256)) => {
                if receipt_name != name {
                    return Err(JournalError::Corrupt);
                }
                let target = media_root.join(receipt_name);
                let meta = std::fs::symlink_metadata(&target)
                    .map_err(|_| JournalError::Media)?;
                if meta.file_type().is_symlink() || !meta.is_file() {
                    return Err(JournalError::Media);
                }
                if meta.len() == 0 {
                    return Err(JournalError::Media);
                }
                let bytes =
                    std::fs::read(&target).map_err(|_| JournalError::Io)?;
                if bytes.is_empty() {
                    return Err(JournalError::Media);
                }
                if Clock(bytes.len() as u64) != *byte_len {
                    return Err(JournalError::Media);
                }
                if sha256_hex(&bytes) != *sha256 {
                    return Err(JournalError::Media);
                }
                media.insert(
                    media_id.clone(),
                    MediaState::Complete {
                        name: receipt_name.clone(),
                        byte_len: *byte_len,
                        sha256: sha256.clone(),
                    },
                );
            }
        }
    }
    Ok(Recovery {
        snapshot: journal.snapshot(),
        media,
        status: journal.recovery_status(),
        quarantined_bytes: journal.interrupted_bytes().to_vec(),
        records: journal.records().to_vec(),
    })
}
