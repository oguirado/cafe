//! Independent admitted-operation book, real file faults and recovery oracles.
#[path = "../src/capsule_registry.rs"]
mod capsule_registry;
#[path = "../src/authority_journal.rs"]
mod authority_journal;
#[path = "../src/journal_replay.rs"]
mod journal_replay;

use authority_journal::{canonical_bytes, AppendState, Journal, JournalError};
use capsule_registry::{Capsule, Clock, Envelope, Feed, Proposal, Role, Snapshot};
use journal_replay::{recover, MediaState, RecoveryStatus};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};

static NEXT: AtomicUsize = AtomicUsize::new(0);
const SESSION: &str = "session-test";
const MEDIA: &[u8] = b"synthetic-media-byte-fixture";
// All four operations and expected facts are hand-authored, never replay snapshots.
const BOOK: [(&str, &str, u64, &str); 4] = [
    ("event-create", "request-create", 0, r#"{"op":"create_capsule","capsule_id":"cap-one","person_label":"Guest","generation":"1"}"#),
    ("event-feed", "request-feed", 1, r#"{"op":"add_feed","capsule_id":"cap-one","feed_id":"feed-one","role":"game","generation":"2"}"#),
    ("event-propose", "request-propose", 2, r#"{"op":"propose","proposal_id":"proposal-one","capsule_id":"cap-one","feed_id":"feed-one","generation":"2","description":"original"}"#),
    ("event-remove", "request-remove", 3, r#"{"op":"remove_feed","capsule_id":"cap-one","feed_id":"feed-one","generation":"2"}"#),
];

struct Fixture { root: PathBuf, log: PathBuf, media: PathBuf }
impl Fixture {
    fn new() -> Self {
        let root = loop {
            let candidate = std::env::temp_dir().join(format!("cafe-journal-test-{}-{}", std::process::id(), NEXT.fetch_add(1, Ordering::SeqCst)));
            match fs::create_dir(&candidate) {
                Ok(()) => break candidate,
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(e) => panic!("temporary directory unavailable: {}", e.kind()),
            }
        };
        let media = root.join("media"); fs::create_dir(&media).unwrap();
        Self { log: root.join("journal.bin"), media, root }
    }
    fn journal(&self) -> Journal { Journal::open(&self.log, SESSION, Clock(7)).unwrap() }
    fn write_media(&self) {
        let mut file = File::create(self.media.join("clip.partial")).unwrap();
        file.write_all(MEDIA).unwrap(); file.sync_all().unwrap();
    }
}
impl Drop for Fixture { fn drop(&mut self) { fs::remove_dir_all(&self.root).unwrap(); } }

fn request(index: usize) -> Envelope {
    let (_, id, revision, body) = BOOK[index];
    let text = format!(r#"{{"schema":"cafe.control.v1","kind":"intent","request_id":"{id}","authority_epoch":"7","expected_revision":"{revision}","body":{body}}}"#);
    capsule_registry::decode_envelope(&text).unwrap()
}
fn empty() -> Snapshot {
    Snapshot { authority_epoch: Clock(7), revision: Clock(0), highest_generation: Clock(0),
        capsules: BTreeMap::new(), proposals: BTreeMap::new(), seen_requests: BTreeSet::new() }
}
fn expected(count: usize) -> Snapshot {
    if count == 0 { return empty(); }
    let feeds = if count == 2 || count == 3 { BTreeMap::from([("feed-one".into(), Feed {
        feed_id: "feed-one".into(), role: Role::Game, generation: Clock(2) })]) } else { BTreeMap::new() };
    let proposals = if count >= 3 { BTreeMap::from([("proposal-one".into(), Proposal {
        proposal_id: "proposal-one".into(), capsule_id: "cap-one".into(), feed_id: "feed-one".into(),
        generation: Clock(2), description: "original".into() })]) } else { BTreeMap::new() };
    // The prefix uses manual input identities, never actual journal or reducer observations.
    let ids = ["request-create", "request-feed", "request-propose", "request-remove"];
    Snapshot { authority_epoch: Clock(7), revision: Clock(count as u64),
        highest_generation: Clock(if count == 1 { 1 } else { 2 }),
        capsules: BTreeMap::from([("cap-one".into(), Capsule { capsule_id: "cap-one".into(),
            person_label: "Guest".into(), generation: Clock(1), feeds })]),
        proposals, seen_requests: ids[..count].iter().map(|v| v.to_string()).collect() }
}
fn append_book(journal: &mut Journal, count: usize) {
    for (index, (event_id, _, _, _)) in BOOK.iter().enumerate().take(count) {
        let committed = journal.append_control(event_id, request(index)).unwrap();
        assert_eq!(committed.state(), AppendState::Committed);
        assert_eq!(committed.sequence(), Clock(index as u64 + 1));
        assert_eq!(committed.event_id(), *event_id);
        assert_eq!(committed.record_digest().len(), 64);
    }
}
fn digest(bytes: &[u8]) -> String { Sha256::digest(bytes).iter().map(|b| format!("{b:02x}")).collect() }
// Independent framing/rehashing of deliberately corrupted INPUT fixtures. No journal
// helper constructs any expected transition, expected wire bytes or expected hash.
fn frame(mut record: Value) -> Vec<u8> {
    record.as_object_mut().unwrap().remove("record_digest");
    let preimage = serde_json::to_vec(&record).unwrap();
    record["record_digest"] = Value::String(digest(&preimage));
    let body = serde_json::to_vec(&record).unwrap();
    let length = body.len() as u32;
    let mut out = length.to_be_bytes().to_vec(); out.extend_from_slice(&(!length).to_be_bytes());
    out.extend_from_slice(&body); out.extend_from_slice(b"CJOK"); out
}
fn first_value(bytes: &[u8]) -> Value {
    let length = u32::from_be_bytes(bytes[..4].try_into().unwrap()) as usize;
    serde_json::from_slice(&bytes[8..8+length]).unwrap()
}
fn assert_blocked(fixture: &Fixture) {
    let before = fs::read(&fixture.log).unwrap();
    assert!(recover(&fixture.log, &fixture.media, SESSION, Clock(7)).is_err());
    assert_eq!(fs::read(&fixture.log).unwrap(), before);
    assert!(!fixture.log.with_extension("interrupted").exists());
}

#[test]
fn canonical_profile_orders_keys_and_rejects_all_floats() {
    assert_eq!(canonical_bytes(&json!({"z": [true, {"b": 2, "a": 1}], "a": "fixture"})).unwrap(),
               br#"{"a":"fixture","z":[true,{"a":1,"b":2}]}"#);
    for value in [json!(1.5), json!({"nested": [1.0]}), json!({"negative": -0.0})] {
        assert_eq!(canonical_bytes(&value), Err(JournalError::Invalid));
    }
    assert_eq!(digest(b"abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
}

#[test]
fn first_record_matches_manual_canonical_preimage_and_chain() {
    let f = Fixture::new(); let mut journal = f.journal(); append_book(&mut journal, 1); drop(journal);
    let bytes = fs::read(&f.log).unwrap(); let value = first_value(&bytes);
    let preimage = br#"{"authority_epoch":"7","event_id":"event-create","kind":"control","payload":{"authority_epoch":"7","body":{"capsule_id":"cap-one","generation":"1","op":"create_capsule","person_label":"Guest"},"expected_revision":"0","kind":"intent","request_id":"request-create","schema":"cafe.control.v1"},"prior_digest":"0000000000000000000000000000000000000000000000000000000000000000","schema":"cafe.journal.v1","sequence":"1","session_id":"session-test"}"#;
    let mut actual = value.clone(); actual.as_object_mut().unwrap().remove("record_digest");
    assert_eq!(serde_json::to_vec(&actual).unwrap(), preimage);
    assert_eq!(value["record_digest"], digest(preimage));
    let length = u32::from_be_bytes(bytes[..4].try_into().unwrap());
    assert_eq!(u32::from_be_bytes(bytes[4..8].try_into().unwrap()), !length);
    assert_eq!(&bytes[8+length as usize..], b"CJOK");
    assert_eq!(frame(value), bytes);
}

#[test]
fn replay_matches_every_manual_admitted_prefix() {
    for count in 0..=4 {
        let f = Fixture::new(); let mut journal = f.journal(); append_book(&mut journal, count); drop(journal);
        let recovery = recover(&f.log, &f.media, SESSION, Clock(7)).unwrap();
        assert_eq!(recovery.snapshot, expected(count));
        assert_eq!(recovery.status, RecoveryStatus::Clean);
        assert!(recovery.quarantined_bytes.is_empty()); assert_eq!(recovery.records.len(), count);
    }
}

#[test]
fn replay_is_deterministic_against_independent_terminal_book() {
    let a = Fixture::new(); let b = Fixture::new();
    for f in [&a, &b] { let mut journal = f.journal(); append_book(&mut journal, 4); }
    assert_eq!(fs::read(&a.log).unwrap(), fs::read(&b.log).unwrap());
    let one = recover(&a.log, &a.media, SESSION, Clock(7)).unwrap();
    let two = recover(&a.log, &a.media, SESSION, Clock(7)).unwrap();
    let three = recover(&b.log, &b.media, SESSION, Clock(7)).unwrap();
    assert_eq!(one.snapshot, expected(4)); assert_eq!(two.snapshot, expected(4)); assert_eq!(three.snapshot, expected(4));
    assert_eq!(one, two); assert_eq!(two, three);
}

#[test]
fn torn_final_header_body_and_trailer_are_interrupted_and_quarantined() {
    let source = Fixture::new(); let mut journal = source.journal(); append_book(&mut journal, 1);
    let prefix = fs::read(&source.log).unwrap();
    journal.append_control(BOOK[1].0, request(1)).unwrap(); drop(journal);
    let all = fs::read(&source.log).unwrap(); let tail = &all[prefix.len()..];
    for cut in [1, 4, 7, 8, 9, tail.len()/2, tail.len()-3, tail.len()-1] {
        let f = Fixture::new(); let mut broken = prefix.clone(); broken.extend_from_slice(&tail[..cut]); fs::write(&f.log, &broken).unwrap();
        let recovery = recover(&f.log, &f.media, SESSION, Clock(7)).unwrap();
        assert_eq!(recovery.status, RecoveryStatus::Interrupted);
        assert_eq!(recovery.snapshot, expected(1)); assert_eq!(recovery.records.len(), 1);
        assert_eq!(recovery.quarantined_bytes, tail[..cut]);
        assert_eq!(fs::read(f.log.with_extension("interrupted")).unwrap(), tail[..cut]);
        assert_eq!(fs::read(&f.log).unwrap(), prefix);
    }
}

#[test]
fn changed_committed_prefix_byte_blocks_without_truncation() {
    let f = Fixture::new(); let mut journal = f.journal(); append_book(&mut journal, 3); drop(journal);
    let mut bytes = fs::read(&f.log).unwrap(); let at = bytes.windows(12).position(|v| v == b"event-create").unwrap();
    bytes[at] = b'E'; fs::write(&f.log, bytes).unwrap(); assert_blocked(&f);
}

#[test]
fn full_bad_length_or_trailer_is_corruption_not_interrupted() {
    for header in [true, false] {
        let f = Fixture::new(); let mut journal = f.journal(); append_book(&mut journal, 1); drop(journal);
        let mut bytes = fs::read(&f.log).unwrap(); let index = if header { 3 } else { bytes.len()-1 };
        bytes[index] ^= 1; fs::write(&f.log, bytes).unwrap(); assert_blocked(&f);
    }
}

#[test]
fn second_writer_and_recovery_are_rejected_while_lock_held() {
    let f = Fixture::new(); let mut holder = f.journal();
    assert!(matches!(Journal::open(&f.log, SESSION, Clock(7)), Err(JournalError::Locked)));
    assert!(matches!(recover(&f.log, &f.media, SESSION, Clock(7)), Err(JournalError::Locked)));
    append_book(&mut holder, 1);
    // A second handle reads the actual committed bytes while the writer stays live.
    let mut reader = fs::File::open(&f.log).unwrap();
    let mut observed = Vec::new();
    std::io::Read::read_to_end(&mut reader, &mut observed).unwrap();
    assert!(!observed.is_empty());
    assert_eq!(observed, fs::read(&f.log).unwrap());
    assert!(matches!(Journal::open(&f.log, SESSION, Clock(7)), Err(JournalError::Locked)));
    let mut lock_name = f.log.as_os_str().to_os_string(); lock_name.push(".lock");
    let lock_path = std::path::PathBuf::from(lock_name);
    assert!(lock_path.is_file()); drop(holder);
    assert!(lock_path.is_file(), "persistent lock path must never be unlinked");
    let reopened = f.journal(); assert_eq!(reopened.records().len(), 1);
    drop(reopened); drop(reader);
}

#[test]
fn append_without_sync_is_pending_never_committed_or_replayed() {
    let f = Fixture::new(); let mut journal = f.journal();
    let pending = journal.stage_control(BOOK[0].0, request(0)).unwrap();
    assert_eq!(pending.state(), AppendState::Pending); assert!(journal.records().is_empty());
    assert!(journal.stage_control(BOOK[1].0, request(1)).is_err());
    drop(pending); drop(journal);
    let recovery = recover(&f.log, &f.media, SESSION, Clock(7)).unwrap();
    assert_eq!(recovery.snapshot, empty()); assert!(recovery.records.is_empty());
    assert_eq!(recovery.status, RecoveryStatus::Interrupted); assert!(!recovery.quarantined_bytes.is_empty());
    assert_eq!(fs::read(&f.log).unwrap(), b"");
}

#[test]
fn explicit_commit_promotes_only_after_durable_barrier() {
    let f = Fixture::new(); let mut journal = f.journal();
    let pending = journal.stage_control(BOOK[0].0, request(0)).unwrap(); assert_eq!(pending.state(), AppendState::Pending);
    let committed = journal.commit(pending).unwrap(); assert_eq!(committed.state(), AppendState::Committed);
    assert_eq!(committed.sequence(), Clock(1)); assert_eq!(journal.records().len(), 1);
    #[cfg(unix)]
    assert_eq!(journal.directory_flush_count(), 1, "Unix must execute directory fsync");
    drop(journal);
    assert_eq!(recover(&f.log, &f.media, SESSION, Clock(7)).unwrap().snapshot, expected(1));
    // Actual platform path plus instance-local failure injection; no cross-test globals.
    let faults = [authority_journal::DurabilityFault::File, authority_journal::DurabilityFault::Parent];
    for fault in faults {
        let failed = Fixture::new(); let mut writer = failed.journal();
        let pending = writer.stage_control(BOOK[0].0, request(0)).unwrap();
        assert_eq!(pending.state(), AppendState::Pending);
        writer.inject_durability_fault(fault);
        assert!(matches!(writer.commit(pending), Err(JournalError::Io)));
        assert!(writer.has_pending_stage()); assert!(writer.records().is_empty());
        assert_eq!(writer.snapshot(), empty());
        assert!(writer.append_control(BOOK[0].0, request(0)).is_err());
    }
    #[cfg(unix)]
    {
        let failed = Fixture::new(); let mut writer = failed.journal();
        let pending = writer.stage_control(BOOK[0].0, request(0)).unwrap();
        writer.inject_durability_fault(authority_journal::DurabilityFault::Directory);
        assert!(matches!(writer.commit(pending), Err(JournalError::Io)));
        assert!(writer.has_pending_stage()); assert!(writer.records().is_empty());
        assert_eq!(writer.snapshot(), empty());
        assert_eq!(writer.directory_flush_count(), 0);
    }
}

#[test]
fn published_file_without_durable_receipt_remains_incomplete() {
    let f = Fixture::new(); let mut journal = f.journal();
    journal.append_media_intent("event-intent", "media-one", "clip.bin").unwrap(); f.write_media();
    fs::rename(f.media.join("clip.partial"), f.media.join("clip.bin")).unwrap(); drop(journal);
    let recovery = recover(&f.log, &f.media, SESSION, Clock(7)).unwrap();
    assert_eq!(recovery.snapshot, empty());
    assert_eq!(recovery.media, BTreeMap::from([("media-one".into(), MediaState::Pending)]));
}

#[test]
fn validated_publication_plus_durable_receipt_is_complete() {
    let f = Fixture::new(); let mut journal = f.journal();
    journal.append_media_intent("event-intent", "media-one", "clip.bin").unwrap(); f.write_media();
    let receipt = journal.publish_media("event-published", &f.media, "media-one", "clip.partial").unwrap();
    assert_eq!(receipt.state(), AppendState::Committed); assert_eq!(receipt.sequence(), Clock(2)); drop(journal);
    assert!(!f.media.join("clip.partial").exists()); assert_eq!(fs::read(f.media.join("clip.bin")).unwrap(), MEDIA);
    let recovery = recover(&f.log, &f.media, SESSION, Clock(7)).unwrap();
    assert_eq!(recovery.snapshot, empty());
    assert_eq!(recovery.media, BTreeMap::from([("media-one".into(), MediaState::Complete {
        name: "clip.bin".into(), byte_len: Clock(MEDIA.len() as u64), sha256: digest(MEDIA) })]));
}

#[test]
fn torn_media_receipt_never_completes_recording() {
    let f = Fixture::new(); let mut journal = f.journal();
    journal.append_media_intent("event-intent", "media-one", "clip.bin").unwrap();
    let prefix = fs::read(&f.log).unwrap(); f.write_media();
    journal.publish_media("event-published", &f.media, "media-one", "clip.partial").unwrap(); drop(journal);
    let mut bytes = fs::read(&f.log).unwrap(); bytes.pop(); fs::write(&f.log, &bytes).unwrap();
    let recovery = recover(&f.log, &f.media, SESSION, Clock(7)).unwrap();
    assert_eq!(recovery.status, RecoveryStatus::Interrupted);
    assert_eq!(recovery.media["media-one"], MediaState::Pending);
    assert_eq!(fs::read(&f.log).unwrap(), prefix);
}

#[test]
fn missing_or_changed_published_media_blocks_false_completion() {
    for missing in [false, true] {
        let f = Fixture::new(); let mut journal = f.journal();
        journal.append_media_intent("event-intent", "media-one", "clip.bin").unwrap(); f.write_media();
        journal.publish_media("event-published", &f.media, "media-one", "clip.partial").unwrap(); drop(journal);
        if missing { fs::remove_file(f.media.join("clip.bin")).unwrap(); }
        else { fs::write(f.media.join("clip.bin"), b"changed").unwrap(); }
        assert!(recover(&f.log, &f.media, SESSION, Clock(7)).is_err());
    }
}

#[test]
fn wrong_prior_digest_blocks_even_with_valid_recomputed_record_digest() {
    let f = Fixture::new(); let mut journal = f.journal(); append_book(&mut journal, 1); drop(journal);
    let mut record = first_value(&fs::read(&f.log).unwrap());
    record["prior_digest"] = Value::String("f".repeat(64)); fs::write(&f.log, frame(record)).unwrap(); assert_blocked(&f);
}

#[test]
fn unknown_twitch_record_kind_and_float_payload_are_rejected() {
    for field in ["kind", "float"] {
        let f = Fixture::new(); let mut journal = f.journal(); append_book(&mut journal, 1); drop(journal);
        let mut record = first_value(&fs::read(&f.log).unwrap());
        if field == "kind" { record["kind"] = json!("twitch_payload"); record["payload"] = json!({"message":"synthetic-forbidden-data"}); }
        else { record["payload"]["authority_epoch"] = json!(7.0); }
        fs::write(&f.log, frame(record)).unwrap(); assert_blocked(&f);
    }
}

#[test]
fn duplicate_events_and_rejected_controls_do_not_append() {
    let f = Fixture::new(); let mut journal = f.journal(); append_book(&mut journal, 1);
    let before = fs::read(&f.log).unwrap();
    assert!(journal.append_control(BOOK[0].0, request(1)).is_err());
    assert!(journal.append_control("event-repeated-request", request(0)).is_err());
    assert_eq!(fs::read(&f.log).unwrap(), before); drop(journal);
    assert_eq!(recover(&f.log, &f.media, SESSION, Clock(7)).unwrap().snapshot, expected(1));

}

#[test]
fn wrong_session_epoch_sequence_and_duplicate_wire_fields_block() {
    for which in ["session_id", "authority_epoch", "sequence", "duplicate"] {
        let f = Fixture::new(); let mut journal = f.journal(); append_book(&mut journal, 1); drop(journal);
        let bytes = fs::read(&f.log).unwrap(); let mut record = first_value(&bytes);
        if which == "duplicate" {
            let body = serde_json::to_string(&record).unwrap().replacen("{", "{\"session_id\":\"session-test\",", 1);
            let n = body.len() as u32; let mut corrupt = n.to_be_bytes().to_vec(); corrupt.extend_from_slice(&(!n).to_be_bytes());
            corrupt.extend_from_slice(body.as_bytes()); corrupt.extend_from_slice(b"CJOK"); fs::write(&f.log, corrupt).unwrap();
        } else {
            record[which] = if which == "session_id" { json!("another-session") } else { json!("99") };
            fs::write(&f.log, frame(record)).unwrap();
        }
        assert_blocked(&f);
    }
}

#[test]
fn publication_refuses_missing_intent_unsafe_names_and_overwrite() {
    let f = Fixture::new(); let mut journal = f.journal(); f.write_media();
    assert!(journal.publish_media("event-no-intent", &f.media, "media-one", "clip.partial").is_err());
    assert!(journal.append_media_intent("event-unsafe", "unsafe", "../escape.bin").is_err());
    journal.append_media_intent("event-intent", "media-one", "clip.bin").unwrap();
    fs::write(f.media.join("clip.bin"), b"existing").unwrap();
    assert!(journal.publish_media("event-overwrite", &f.media, "media-one", "clip.partial").is_err());
    assert_eq!(fs::read(f.media.join("clip.bin")).unwrap(), b"existing");
}

#[cfg(windows)]
#[path = "../src/windows_journal_policy.rs"]
mod windows_journal_policy;
