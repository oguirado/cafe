//! Independent transition oracles: expected states/replies are hand-authored.
#[path = "../src/capsule_registry.rs"]
mod capsule_registry;

use capsule_registry::*;
use std::collections::{BTreeMap, BTreeSet};

const CREATE: &str = r#"{"op":"create_capsule","capsule_id":"c1","person_label":"Guest","generation":"1"}"#;
const GAME: &str = r#"{"op":"add_feed","capsule_id":"c1","feed_id":"f1","role":"game","generation":"2"}"#;
const CAMERA: &str = r#"{"op":"add_feed","capsule_id":"c1","feed_id":"f2","role":"camera","generation":"3"}"#;
const REMOVE: &str = r#"{"op":"remove_feed","capsule_id":"c1","feed_id":"f1","generation":"2"}"#;
const PROPOSE: &str = r#"{"op":"propose","proposal_id":"p1","capsule_id":"c1","feed_id":"f1","generation":"2","description":"original"}"#;

fn wire(id: &str, revision: u64, body: &str) -> String {
    format!(r#"{{"schema":"cafe.control.v1","kind":"intent","request_id":"{id}","authority_epoch":"1","expected_revision":"{revision}","body":{body}}}"#)
}

fn empty() -> Snapshot {
    Snapshot { authority_epoch: Clock(1), revision: Clock(0), highest_generation: Clock(0),
        capsules: BTreeMap::new(), proposals: BTreeMap::new(), seen_requests: BTreeSet::new() }
}

// Inputs below are fixed facts of each test, not observations from Registry.
fn expected(revision: u64, highest: u64, feeds: &[(&str, Role, u64)], seen: &[&str], proposal: bool) -> Snapshot {
    let feeds = feeds.iter().map(|(id, role, generation)| (id.to_string(), Feed {
        feed_id: id.to_string(), role: role.clone(), generation: Clock(*generation) })).collect();
    let capsule = Capsule { capsule_id: "c1".into(), person_label: "Guest".into(),
        generation: Clock(1), feeds };
    let proposals = if proposal { BTreeMap::from([("p1".into(), Proposal {
        proposal_id: "p1".into(), capsule_id: "c1".into(), feed_id: "f1".into(),
        generation: Clock(2), description: "original".into() })]) } else { BTreeMap::new() };
    Snapshot { authority_epoch: Clock(1), revision: Clock(revision), highest_generation: Clock(highest),
        capsules: BTreeMap::from([("c1".into(), capsule)]), proposals,
        seen_requests: seen.iter().map(|id| id.to_string()).collect() }
}

fn admitted(id: &str, revision: u64) -> Reply {
    Reply { request_id: id.into(), state: Outcome::Admitted, revision: Clock(revision),
        error: None, receipt: Some(Receipt { request_id: id.into(), revision: Clock(revision) }) }
}

fn rejected(id: &str, revision: u64, error: &str) -> Reply {
    Reply { request_id: id.into(), state: Outcome::Rejected, revision: Clock(revision),
        error: Some(error.into()), receipt: None }
}

fn game_registry() -> Registry {
    let mut registry = Registry::new(Clock(1));
    assert_eq!(registry.decode_and_apply(&wire("r1", 0, CREATE)).unwrap(), admitted("r1", 1));
    assert_eq!(registry.snapshot(), expected(1, 1, &[], &["r1"], false));
    assert_eq!(registry.decode_and_apply(&wire("r2", 1, GAME)).unwrap(), admitted("r2", 2));
    assert_eq!(registry.snapshot(), expected(2, 2, &[("f1", Role::Game, 2)], &["r1", "r2"], false));
    registry
}

#[test]
fn create_capsule_has_manual_initial_transition() {
    let mut registry = Registry::new(Clock(1));
    assert_eq!(registry.snapshot(), empty());
    assert_eq!(registry.decode_and_apply(&wire("r1", 0, CREATE)).unwrap(), admitted("r1", 1));
    assert_eq!(registry.snapshot(), expected(1, 1, &[], &["r1"], false));
}

#[test]
fn add_game_has_manual_transition() {
    let registry = game_registry();
    assert_eq!(registry.snapshot(), expected(2, 2, &[("f1", Role::Game, 2)], &["r1", "r2"], false));
}

#[test]
fn distinct_game_and_camera_roles_are_admitted() {
    let mut registry = game_registry();
    assert_eq!(registry.decode_and_apply(&wire("r3", 2, CAMERA)).unwrap(), admitted("r3", 3));
    assert_eq!(registry.snapshot(), expected(3, 3,
        &[("f1", Role::Game, 2), ("f2", Role::Camera, 3)], &["r1", "r2", "r3"], false));
}

#[test]
fn unknown_envelope_key_is_rejected() {
    let mut registry = Registry::new(Clock(1));
    let input = wire("r1", 0, CREATE).replacen(r#""schema":"#, r#""unexpected":true,"schema":"#, 1);
    assert!(registry.decode_and_apply(&input).is_err());
    assert_eq!(registry.snapshot(), empty());
}

#[test]
fn numeric_clock_is_rejected_even_if_in_range() {
    let mut registry = Registry::new(Clock(1));
    for (from, to) in [(r#""authority_epoch":"1""#, r#""authority_epoch":1"#),
                       (r#""expected_revision":"0""#, r#""expected_revision":0"#),
                       (r#""generation":"1""#, r#""generation":1"#)] {
        assert!(registry.decode_and_apply(&wire("r1", 0, CREATE).replace(from, to)).is_err());
        assert_eq!(registry.snapshot(), empty());
    }
}

#[test]
fn overflow_and_noncanonical_clocks_are_rejected() {
    let mut registry = Registry::new(Clock(1));
    let overflow = format!("{}0", u64::MAX);
    for value in [overflow.as_str(), "01", "+1", "-1", " 1", "1 ", "", "1.0"] {
        let input = wire("r1", 0, CREATE).replace(r#""expected_revision":"0""#,
            &format!(r#""expected_revision":"{value}""#));
        assert!(registry.decode_and_apply(&input).is_err(), "invalid clock was accepted");
        assert_eq!(registry.snapshot(), empty());
    }
}

#[test]
fn duplicate_role_is_rejected_without_mutation() {
    let mut registry = game_registry();
    let body = r#"{"op":"add_feed","capsule_id":"c1","feed_id":"f2","role":"game","generation":"3"}"#;
    assert_eq!(registry.decode_and_apply(&wire("r3", 2, body)).unwrap(), rejected("r3", 2, "duplicate_role"));
    assert_eq!(registry.snapshot(), expected(2, 2, &[("f1", Role::Game, 2)], &["r1", "r2"], false));
}

#[test]
fn nonexistent_feed_is_rejected() {
    let mut registry = game_registry();
    let body = r#"{"op":"remove_feed","capsule_id":"c1","feed_id":"missing","generation":"2"}"#;
    assert_eq!(registry.decode_and_apply(&wire("r3", 2, body)).unwrap(), rejected("r3", 2, "feed_not_found"));
    assert_eq!(registry.snapshot(), expected(2, 2, &[("f1", Role::Game, 2)], &["r1", "r2"], false));
}

#[test]
fn removed_generation_cannot_be_recycled() {
    let mut registry = game_registry();
    assert_eq!(registry.decode_and_apply(&wire("r3", 2, REMOVE)).unwrap(), admitted("r3", 3));
    assert_eq!(registry.snapshot(), expected(3, 2, &[], &["r1", "r2", "r3"], false));
    assert_eq!(registry.decode_and_apply(&wire("r4", 3, GAME)).unwrap(), rejected("r4", 3, "generation_reused"));
    assert_eq!(registry.snapshot(), expected(3, 2, &[], &["r1", "r2", "r3"], false));
    let fresh = GAME.replace(r#""generation":"2""#, r#""generation":"3""#);
    assert_eq!(registry.decode_and_apply(&wire("r5", 3, &fresh)).unwrap(), admitted("r5", 4));
    assert_eq!(registry.snapshot(), expected(4, 3, &[("f1", Role::Game, 3)], &["r1", "r2", "r3", "r5"], false));
}

#[test]
fn stale_revision_is_rejected() {
    let mut registry = game_registry();
    assert_eq!(registry.decode_and_apply(&wire("r3", 1, CAMERA)).unwrap(), rejected("r3", 2, "stale_revision"));
    assert_eq!(registry.snapshot(), expected(2, 2, &[("f1", Role::Game, 2)], &["r1", "r2"], false));
}

#[test]
fn admitted_request_id_cannot_cause_a_second_effect() {
    let mut registry = game_registry();
    assert_eq!(registry.decode_and_apply(&wire("r2", 2, CAMERA)).unwrap(), rejected("r2", 2, "duplicate_request_id"));
    assert_eq!(registry.snapshot(), expected(2, 2, &[("f1", Role::Game, 2)], &["r1", "r2"], false));
    assert_eq!(registry.decode_and_apply(&wire("r2", 1, GAME)).unwrap(), rejected("r2", 2, "duplicate_request_id"));
    assert_eq!(registry.snapshot(), expected(2, 2, &[("f1", Role::Game, 2)], &["r1", "r2"], false));
}

#[test]
fn proposal_original_data_is_immutable() {
    let mut registry = game_registry();
    assert_eq!(registry.decode_and_apply(&wire("r3", 2, PROPOSE)).unwrap(), admitted("r3", 3));
    assert_eq!(registry.snapshot(), expected(3, 2, &[("f1", Role::Game, 2)], &["r1", "r2", "r3"], true));
    let changed = PROPOSE.replace("original", "changed");
    assert_eq!(registry.decode_and_apply(&wire("r4", 3, &changed)).unwrap(), rejected("r4", 3, "proposal_exists"));
    assert_eq!(registry.snapshot(), expected(3, 2, &[("f1", Role::Game, 2)], &["r1", "r2", "r3"], true));
}

#[test]
fn sequence_is_deterministic_and_matches_manual_terminal_state() {
    let mut first = Registry::new(Clock(1));
    let mut second = Registry::new(Clock(1));
    for (id, revision, body) in [("r1", 0, CREATE), ("r2", 1, GAME), ("r3", 2, CAMERA),
                                ("r4", 3, PROPOSE), ("r5", 4, REMOVE)] {
        let input = wire(id, revision, body);
        assert_eq!(first.decode_and_apply(&input).unwrap(), admitted(id, revision + 1));
        assert_eq!(second.decode_and_apply(&input).unwrap(), admitted(id, revision + 1));
    }
    let terminal = expected(5, 3, &[("f2", Role::Camera, 3)], &["r1", "r2", "r3", "r4", "r5"], true);
    assert_eq!(first.snapshot(), terminal);
    assert_eq!(second.snapshot(), terminal);
    assert_eq!(serde_json::to_string(&first.snapshot()).unwrap(), serde_json::to_string(&second.snapshot()).unwrap());
}

#[test]
fn stale_epoch_is_rejected() {
    let mut registry = Registry::new(Clock(1));
    let input = wire("r1", 0, CREATE).replace(r#""authority_epoch":"1""#, r#""authority_epoch":"2""#);
    assert_eq!(registry.decode_and_apply(&input).unwrap(), rejected("r1", 0, "stale_epoch"));
    assert_eq!(registry.snapshot(), empty());
}

#[test]
fn reply_has_exact_shape_and_decimal_revision() {
    let mut registry = Registry::new(Clock(1));
    let reply = registry.decode_and_apply(&wire("r1", 0, CREATE)).unwrap();
    assert_eq!(serde_json::to_value(reply).unwrap(), serde_json::json!({
        "request_id": "r1", "state": "admitted", "revision": "1", "error": null,
        "receipt": {"request_id": "r1", "revision": "1"} }));
}

#[test]
fn large_clocks_roundtrip_as_strings_without_precision_loss() {
    let largest = u64::MAX.to_string();
    let encoded = format!("\"{largest}\"");
    assert_eq!(serde_json::from_str::<Clock>(&encoded).unwrap(), Clock(u64::MAX));
    assert_eq!(serde_json::to_string(&Clock(u64::MAX)).unwrap(), encoded);
    let large = (1_u64 << 53) + 1;
    let input = wire("r1", 0, CREATE).replace(r#""authority_epoch":"1""#,
        &format!(r#""authority_epoch":"{large}""#));
    let mut registry = Registry::new(Clock(large));
    assert_eq!(registry.decode_and_apply(&input).unwrap(), admitted("r1", 1));
    let mut manual = expected(1, 1, &[], &["r1"], false);
    manual.authority_epoch = Clock(large);
    assert_eq!(registry.snapshot(), manual);
    assert_eq!(serde_json::to_value(registry.snapshot()).unwrap()["authority_epoch"], large.to_string());
}

#[test]
fn body_unknown_fields_and_duplicate_json_keys_are_rejected() {
    let mut registry = Registry::new(Clock(1));
    let extra_body = CREATE.replace(r#""op":"#, r#""extra":true,"op":"#);
    let duplicate_body = CREATE.replace(r#""capsule_id":"c1""#, r#""capsule_id":"c1","capsule_id":"c2""#);
    let duplicate_op = CREATE.replace(r#""op":"create_capsule""#, r#""op":"create_capsule","op":"create_capsule""#);
    let duplicate_header = wire("r1", 0, CREATE).replace(r#""request_id":"r1""#, r#""request_id":"r1","request_id":"r2""#);
    for input in [wire("r1", 0, &extra_body), wire("r1", 0, &duplicate_body), wire("r1", 0, &duplicate_op), duplicate_header] {
        assert!(registry.decode_and_apply(&input).is_err());
        assert_eq!(registry.snapshot(), empty());
    }
}

#[test]
fn missing_null_fields_and_unknown_variants_are_rejected() {
    let mut registry = Registry::new(Clock(1));
    let missing = wire("r1", 0, CREATE).replace(r#""expected_revision":"0","#, "");
    let null = wire("r1", 0, CREATE).replace(CREATE, "null");
    let role = GAME.replace("game", "other");
    let command = CREATE.replace("create_capsule", "external_effect");
    for input in [missing, null, wire("r1", 0, &role), wire("r1", 0, &command)] {
        assert!(registry.decode_and_apply(&input).is_err());
        assert_eq!(registry.snapshot(), empty());
    }
}

#[test]
fn typed_apply_cannot_bypass_schema_validation() {
    let mut registry = Registry::new(Clock(1));
    let mut request = decode_envelope(&wire("r1", 0, CREATE)).unwrap();
    request.schema = "unsupported".into();
    assert_eq!(registry.apply(request), rejected("r1", 0, "invalid_schema"));
    assert_eq!(registry.snapshot(), empty());
    let mut request = decode_envelope(&wire("r1", 0, CREATE)).unwrap();
    request.kind = "external_effect".into();
    assert_eq!(registry.apply(request), rejected("r1", 0, "invalid_kind"));
    assert_eq!(registry.snapshot(), empty());
}

#[test]
fn oversized_envelope_is_rejected() {
    let mut registry = Registry::new(Clock(1));
    let input = format!("{}{}", " ".repeat(MAX_ENVELOPE_BYTES), wire("r1", 0, CREATE));
    assert!(registry.decode_and_apply(&input).is_err());
    assert_eq!(registry.snapshot(), empty());
}

#[test]
fn stale_feed_generation_cannot_remove_or_propose() {
    let mut registry = game_registry();
    for body in [REMOVE.replace(r#""generation":"2""#, r#""generation":"1""#),
                 PROPOSE.replace(r#""generation":"2""#, r#""generation":"1""#)] {
        assert_eq!(registry.decode_and_apply(&wire("r3", 2, &body)).unwrap(), rejected("r3", 2, "generation_mismatch"));
        assert_eq!(registry.snapshot(), expected(2, 2, &[("f1", Role::Game, 2)], &["r1", "r2"], false));
    }
}

#[test]
fn modifying_observation_copy_cannot_mutate_authority() {
    let registry = game_registry();
    let mut observation = registry.snapshot();
    observation.capsules.clear();
    observation.seen_requests.clear();
    observation.highest_generation = Clock(0);
    observation.revision = Clock(0);
    assert_eq!(registry.snapshot(), expected(2, 2, &[("f1", Role::Game, 2)], &["r1", "r2"], false));
}

#[test]
fn wire_schema_kind_and_identities_are_closed() {
    let mut registry = Registry::new(Clock(1));
    for input in [wire("r1", 0, CREATE).replace("cafe.control.v1", "unsupported"),
                  wire("r1", 0, CREATE).replace(r#""kind":"intent""#, r#""kind":"other""#),
                  wire("", 0, CREATE), wire("space id", 0, CREATE),
                  wire("r1", 0, &CREATE.replace(r#""capsule_id":"c1""#, r#""capsule_id":"""#))] {
        assert!(registry.decode_and_apply(&input).is_err());
        assert_eq!(registry.snapshot(), empty());
    }
    let mut request = decode_envelope(&wire("r1", 0, CREATE)).unwrap();
    request.request_id.clear();
    assert_eq!(registry.apply(request), rejected("", 0, "invalid_identity"));
    assert_eq!(registry.snapshot(), empty());
}
