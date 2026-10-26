//! P05 pure capsule/feed authority reducer.
//!
//! Zero-effect in-memory admission kernel: closed wire decoding plus a
//! deterministic `Registry` state machine. No I/O, clocks, randomness,
//! threads, processes, or network interaction.

#![forbid(unsafe_code)]

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

/// Wire schema identifier for control envelopes.
pub const SCHEMA: &str = "cafe.control.v1";

/// Local pure-decoder bound on envelope payload size in bytes.
pub const MAX_ENVELOPE_BYTES: usize = 16_384;

/// Canonical unsigned clock value.
///
/// Serializes as a decimal string; deserializes from canonical decimal
/// strings only (no JSON numbers, signs, whitespace, leading zeros, or
/// `u64` overflow).
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Clock(pub u64);

impl Serialize for Clock {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        serializer.serialize_str(&self.0.to_string())
    }
}

impl<'de> Deserialize<'de> for Clock {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        struct ClockVisitor;

        impl<'de> serde::de::Visitor<'de> for ClockVisitor {
            type Value = Clock;

            fn expecting(
                &self,
                formatter: &mut std::fmt::Formatter<'_>,
            ) -> std::fmt::Result {
                formatter.write_str("a canonical decimal u64 string")
            }

            fn visit_str<E>(self, value: &str) -> Result<Clock, E>
            where
                E: serde::de::Error,
            {
                match parse_canonical_u64(value) {
                    Some(inner) => Ok(Clock(inner)),
                    None => Err(E::custom("invalid canonical decimal clock")),
                }
            }

            fn visit_borrowed_str<E>(self, value: &'de str) -> Result<Clock, E>
            where
                E: serde::de::Error,
            {
                self.visit_str(value)
            }

            fn visit_string<E>(self, value: String) -> Result<Clock, E>
            where
                E: serde::de::Error,
            {
                self.visit_str(&value)
            }
        }

        deserializer.deserialize_str(ClockVisitor)
    }
}

/// Parses a canonical decimal `u64`: ASCII digits only, non-empty, no
/// leading zeros (except exactly `"0"`), no overflow.
fn parse_canonical_u64(text: &str) -> Option<u64> {
    if text.is_empty() {
        return None;
    }
    if !text.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    if text.len() > 1 && text.starts_with('0') {
        return None;
    }
    text.parse::<u64>().ok()
}

/// Feed role slot within a capsule.
#[derive(
    Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize,
)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    Game,
    Camera,
}

/// Closed wire request envelope.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Envelope {
    pub schema: String,
    pub kind: String,
    pub request_id: String,
    pub authority_epoch: Clock,
    pub expected_revision: Clock,
    pub body: Command,
}

/// Closed command body, tagged by `op`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    CreateCapsule {
        capsule_id: String,
        person_label: String,
        generation: Clock,
    },
    AddFeed {
        capsule_id: String,
        feed_id: String,
        role: Role,
        generation: Clock,
    },
    RemoveFeed {
        capsule_id: String,
        feed_id: String,
        generation: Clock,
    },
    Propose {
        proposal_id: String,
        capsule_id: String,
        feed_id: String,
        generation: Clock,
        description: String,
    },
}

/// Wire decoding failure. Carries no input excerpts or host paths.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DecodeError {
    Oversized,
    Malformed,
    InvalidSchema,
    InvalidKind,
    InvalidIdentity,
    InvalidPayload,
}

impl std::fmt::Display for DecodeError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let code = match self {
            DecodeError::Oversized => "oversized_envelope",
            DecodeError::Malformed => "malformed_envelope",
            DecodeError::InvalidSchema => "invalid_schema",
            DecodeError::InvalidKind => "invalid_kind",
            DecodeError::InvalidIdentity => "invalid_identity",
            DecodeError::InvalidPayload => "invalid_payload",
        };
        formatter.write_str(code)
    }
}

impl std::error::Error for DecodeError {}

/// Shared envelope/payload validation outcome used by both the wire
/// decoder and direct `Registry::apply` calls.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ValidationFailure {
    InvalidSchema,
    InvalidKind,
    InvalidIdentity,
    InvalidPayload,
}

impl ValidationFailure {
    fn code(self) -> &'static str {
        match self {
            ValidationFailure::InvalidSchema => "invalid_schema",
            ValidationFailure::InvalidKind => "invalid_kind",
            ValidationFailure::InvalidIdentity => "invalid_identity",
            ValidationFailure::InvalidPayload => "invalid_payload",
        }
    }

    fn decode_error(self) -> DecodeError {
        match self {
            ValidationFailure::InvalidSchema => DecodeError::InvalidSchema,
            ValidationFailure::InvalidKind => DecodeError::InvalidKind,
            ValidationFailure::InvalidIdentity => DecodeError::InvalidIdentity,
            ValidationFailure::InvalidPayload => DecodeError::InvalidPayload,
        }
    }
}

/// Identity rule: byte length `1..=128`, ASCII alphanumeric/`-`/`_` only.
fn is_valid_id(value: &str) -> bool {
    let len = value.len();
    (1..=128).contains(&len)
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
}

/// Presentation label rule: at most 256 UTF-8 bytes and nonblank.
fn is_valid_label(value: &str) -> bool {
    value.len() <= 256 && !value.trim().is_empty()
}

/// Proposal description rule: at most 512 UTF-8 bytes and nonblank.
fn is_valid_description(value: &str) -> bool {
    value.len() <= 512 && !value.trim().is_empty()
}

/// Validates schema, kind, identities, and label/description payloads in
/// the exact order required by `Registry::apply` step 1.
fn validate_envelope(request: &Envelope) -> Result<(), ValidationFailure> {
    if request.schema != SCHEMA {
        return Err(ValidationFailure::InvalidSchema);
    }
    if request.kind != "intent" {
        return Err(ValidationFailure::InvalidKind);
    }
    if !is_valid_id(&request.request_id) {
        return Err(ValidationFailure::InvalidIdentity);
    }
    match &request.body {
        Command::CreateCapsule {
            capsule_id,
            person_label,
            ..
        } => {
            if !is_valid_id(capsule_id) {
                return Err(ValidationFailure::InvalidIdentity);
            }
            if !is_valid_label(person_label) {
                return Err(ValidationFailure::InvalidPayload);
            }
        }
        Command::AddFeed {
            capsule_id,
            feed_id,
            ..
        } => {
            if !is_valid_id(capsule_id) || !is_valid_id(feed_id) {
                return Err(ValidationFailure::InvalidIdentity);
            }
        }
        Command::RemoveFeed {
            capsule_id,
            feed_id,
            ..
        } => {
            if !is_valid_id(capsule_id) || !is_valid_id(feed_id) {
                return Err(ValidationFailure::InvalidIdentity);
            }
        }
        Command::Propose {
            proposal_id,
            capsule_id,
            feed_id,
            description,
            ..
        } => {
            if !is_valid_id(proposal_id)
                || !is_valid_id(capsule_id)
                || !is_valid_id(feed_id)
            {
                return Err(ValidationFailure::InvalidIdentity);
            }
            if !is_valid_description(description) {
                return Err(ValidationFailure::InvalidPayload);
            }
        }
    }
    Ok(())
}

/// Decodes and validates one wire envelope.
///
/// Rejects oversized payloads, then deserializes directly into the typed
/// `Envelope` (never via an intermediate generic map, so duplicate JSON
/// keys and unknown fields fail), then runs shared payload validation.
pub fn decode_envelope(raw: &str) -> Result<Envelope, DecodeError> {
    if raw.len() > MAX_ENVELOPE_BYTES {
        return Err(DecodeError::Oversized);
    }
    let envelope: Envelope =
        serde_json::from_str(raw).map_err(|_| DecodeError::Malformed)?;
    validate_envelope(&envelope).map_err(ValidationFailure::decode_error)?;
    Ok(envelope)
}

/// Active feed slot observation.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Feed {
    pub feed_id: String,
    pub role: Role,
    pub generation: Clock,
}

/// Participant capsule observation.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Capsule {
    pub capsule_id: String,
    pub person_label: String,
    pub generation: Clock,
    pub feeds: BTreeMap<String, Feed>,
}

/// Immutable proposal record observation.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Proposal {
    pub proposal_id: String,
    pub capsule_id: String,
    pub feed_id: String,
    pub generation: Clock,
    pub description: String,
}

/// Read-only owned observation of registry state.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Snapshot {
    pub authority_epoch: Clock,
    pub revision: Clock,
    pub highest_generation: Clock,
    pub capsules: BTreeMap<String, Capsule>,
    pub proposals: BTreeMap<String, Proposal>,
    pub seen_requests: BTreeSet<String>,
}

/// Pure admission outcome (never a provider or dispatch effect).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    Admitted,
    Rejected,
}

/// In-memory admission identity for an admitted request.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Receipt {
    pub request_id: String,
    pub revision: Clock,
}

/// Control reply: exactly five fields, always serialized even when null.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Reply {
    pub request_id: String,
    pub state: Outcome,
    pub revision: Clock,
    pub error: Option<String>,
    pub receipt: Option<Receipt>,
}

/// Pure capsule/feed authority reducer. All mutable state is private;
/// observation flows through owned `Snapshot` values only.
pub struct Registry {
    authority_epoch: Clock,
    revision: Clock,
    highest_generation: Clock,
    capsules: BTreeMap<String, Capsule>,
    proposals: BTreeMap<String, Proposal>,
    seen_requests: BTreeSet<String>,
}

impl Registry {
    /// Creates a fresh registry at `revision 0` and watermark `0`.
    pub fn new(epoch: Clock) -> Self {
        Registry {
            authority_epoch: epoch,
            revision: Clock(0),
            highest_generation: Clock(0),
            capsules: BTreeMap::new(),
            proposals: BTreeMap::new(),
            seen_requests: BTreeSet::new(),
        }
    }

    /// Returns an owned clone of current state.
    pub fn snapshot(&self) -> Snapshot {
        Snapshot {
            authority_epoch: self.authority_epoch,
            revision: self.revision,
            highest_generation: self.highest_generation,
            capsules: self.capsules.clone(),
            proposals: self.proposals.clone(),
            seen_requests: self.seen_requests.clone(),
        }
    }

    /// Decodes one wire envelope and applies it.
    pub fn decode_and_apply(&mut self, raw: &str) -> Result<Reply, DecodeError> {
        let envelope = decode_envelope(raw)?;
        Ok(self.apply(envelope))
    }

    /// Applies one envelope through the exact six-step admission order.
    /// Any rejection leaves every field of `self` untouched.
    pub fn apply(&mut self, request: Envelope) -> Reply {
        // Step 1: structural validation (manual envelopes included).
        if let Err(failure) = validate_envelope(&request) {
            return reject(&request, self.revision, failure.code());
        }
        // Step 2: admitted request deduplication.
        if self.seen_requests.contains(&request.request_id) {
            return reject(&request, self.revision, "duplicate_request_id");
        }
        // Step 3: authority epoch and expected revision preconditions.
        if request.authority_epoch != self.authority_epoch {
            return reject(&request, self.revision, "stale_epoch");
        }
        if request.expected_revision != self.revision {
            return reject(&request, self.revision, "stale_revision");
        }
        // Step 4: read-only command preconditions.
        if let Err(code) = self.check_command(&request.body) {
            return reject(&request, self.revision, code);
        }
        // Step 5: checked revision increment precondition.
        let next_revision = match self.revision.0.checked_add(1) {
            Some(value) => Clock(value),
            None => return reject(&request, self.revision, "revision_overflow"),
        };
        // Step 6: atomic commit.
        let Envelope {
            request_id, body, ..
        } = request;
        match body {
            Command::CreateCapsule {
                capsule_id,
                person_label,
                generation,
            } => {
                self.capsules.insert(
                    capsule_id.clone(),
                    Capsule {
                        capsule_id,
                        person_label,
                        generation,
                        feeds: BTreeMap::new(),
                    },
                );
                self.highest_generation = generation;
            }
            Command::AddFeed {
                capsule_id,
                feed_id,
                role,
                generation,
            } => {
                if let Some(capsule) = self.capsules.get_mut(&capsule_id) {
                    capsule.feeds.insert(
                        feed_id.clone(),
                        Feed {
                            feed_id,
                            role,
                            generation,
                        },
                    );
                    self.highest_generation = generation;
                }
            }
            Command::RemoveFeed {
                capsule_id,
                feed_id,
                ..
            } => {
                if let Some(capsule) = self.capsules.get_mut(&capsule_id) {
                    capsule.feeds.remove(&feed_id);
                }
            }
            Command::Propose {
                proposal_id,
                capsule_id,
                feed_id,
                generation,
                description,
            } => {
                self.proposals.insert(
                    proposal_id.clone(),
                    Proposal {
                        proposal_id,
                        capsule_id,
                        feed_id,
                        generation,
                        description,
                    },
                );
            }
        }
        self.revision = next_revision;
        self.seen_requests.insert(request_id.clone());
        Reply {
            request_id: request_id.clone(),
            state: Outcome::Admitted,
            revision: next_revision,
            error: None,
            receipt: Some(Receipt {
                request_id,
                revision: next_revision,
            }),
        }
    }

    /// Read-only command preconditions in exact per-command error order.
    fn check_command(&self, command: &Command) -> Result<(), &'static str> {
        match command {
            Command::CreateCapsule {
                capsule_id,
                generation,
                ..
            } => {
                if self.capsules.contains_key(capsule_id) {
                    return Err("duplicate_capsule");
                }
                if generation.0 <= self.highest_generation.0 {
                    return Err("generation_reused");
                }
            }
            Command::AddFeed {
                capsule_id,
                feed_id,
                role,
                generation,
            } => {
                let capsule = match self.capsules.get(capsule_id) {
                    Some(capsule) => capsule,
                    None => return Err("capsule_not_found"),
                };
                if self
                    .capsules
                    .values()
                    .any(|entry| entry.feeds.contains_key(feed_id))
                {
                    return Err("duplicate_feed");
                }
                if capsule.feeds.values().any(|feed| feed.role == *role) {
                    return Err("duplicate_role");
                }
                if generation.0 <= self.highest_generation.0 {
                    return Err("generation_reused");
                }
            }
            Command::RemoveFeed {
                capsule_id,
                feed_id,
                generation,
            } => {
                let capsule = match self.capsules.get(capsule_id) {
                    Some(capsule) => capsule,
                    None => return Err("capsule_not_found"),
                };
                let feed = match capsule.feeds.get(feed_id) {
                    Some(feed) => feed,
                    None => return Err("feed_not_found"),
                };
                if feed.generation != *generation {
                    return Err("generation_mismatch");
                }
            }
            Command::Propose {
                proposal_id,
                capsule_id,
                feed_id,
                generation,
                ..
            } => {
                if self.proposals.contains_key(proposal_id) {
                    return Err("proposal_exists");
                }
                let capsule = match self.capsules.get(capsule_id) {
                    Some(capsule) => capsule,
                    None => return Err("capsule_not_found"),
                };
                let feed = match capsule.feeds.get(feed_id) {
                    Some(feed) => feed,
                    None => return Err("feed_not_found"),
                };
                if feed.generation != *generation {
                    return Err("generation_mismatch");
                }
            }
        }
        Ok(())
    }
}

/// Builds a rejection reply at the unchanged revision.
fn reject(request: &Envelope, revision: Clock, code: &str) -> Reply {
    Reply {
        request_id: request.request_id.clone(),
        state: Outcome::Rejected,
        revision,
        error: Some(code.to_string()),
        receipt: None,
    }
}
