//! Runtime effects manager over concrete capture pipelines.
//!
//! Crate-private generic manager over existing `CapturePipeline<S>`.
//! Parent owns admission, identities, capabilities, preflight, supervision,
//! and validation. All IDs here are trusted Rust inputs already checked.
//! This module never deserializes or validates external identities or paths,
//! never constructs peers or tokens, never creates capture or recorder
//! objects, and never invokes FFmpeg. It only drives the accepted
//! `tick`/`stop`/`abort` methods and reads bounded state and counters.
//!
//! Caller must call `abort_all` explicitly on shutdown. There is no `Drop`
//! runtime or signal logic here.

use std::collections::{BTreeMap, BTreeSet};
use std::time::Instant;

use crate::authority_journal::Journal;
use crate::capture_pipeline::{CapturePipeline, FrameSource, PipelineError};
use crate::ffmpeg_recorder::{RecorderCounters, RecordingReceipt, RecordingState};
use crate::windows_window_registry::WindowRegistry;

/// Manager failures. Explicit per rejection; tick/stop/abort carry the
/// first underlying pipeline cause. Never silently ignored.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum EngineError {
    /// Zero capsule or feed budget in `new`.
    InvalidBudget,
    /// Attach with no feeds.
    EmptyFeeds,
    /// Attach with an already-present capsule id.
    DuplicateCapsule,
    /// Attach with a repeated feed key inside the incoming batch.
    DuplicateFeed,
    /// Attach would exceed the parent-supplied budgets.
    CapacityExceeded,
    /// Aggregate count addition overflowed.
    Overflow,
    /// Incoming pipeline not in `Recording` state.
    InvalidPipelineState,
    /// Stop for an absent capsule (including after successful removal).
    NotFound,
    /// At least one feed tick failed; capsule(s) marked interrupted.
    Tick(PipelineError),
    /// Capsule stop failed; batch retained as interrupted, never complete.
    Stop(PipelineError),
    /// Abort-all observed a non-cleanup pipeline failure.
    Abort(PipelineError),
}

impl std::fmt::Display for EngineError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidBudget => write!(f, "invalid zero budget"),
            Self::EmptyFeeds => write!(f, "empty feed batch"),
            Self::DuplicateCapsule => write!(f, "duplicate capsule"),
            Self::DuplicateFeed => write!(f, "duplicate feed key"),
            Self::CapacityExceeded => write!(f, "capacity exceeded"),
            Self::Overflow => write!(f, "count overflow"),
            Self::InvalidPipelineState => write!(f, "incoming pipeline not recording"),
            Self::NotFound => write!(f, "capsule not found"),
            Self::Tick(inner) => write!(f, "tick failed: {inner:?}"),
            Self::Stop(inner) => write!(f, "stop failed: {inner:?}"),
            Self::Abort(inner) => write!(f, "abort failed: {inner:?}"),
        }
    }
}

impl std::error::Error for EngineError {}

/// Capsule lifecycle as seen by the manager.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum CapsuleState {
    Running,
    Interrupted,
}

/// Bounded read-only feed view: ids, recorder state, and counters only.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct FeedStatus {
    pub feed_id: String,
    pub state: RecordingState,
    pub counters: RecorderCounters,
}

/// Bounded read-only capsule view: ids, manager state, and feed views.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct CapsuleStatus {
    pub capsule_id: String,
    pub state: CapsuleState,
    pub feeds: Vec<FeedStatus>,
}

/// One admitted batch: manager state plus its live pipelines.
struct Capsule<S: FrameSource> {
    state: CapsuleState,
    feeds: BTreeMap<String, CapturePipeline<S>>,
}

/// Generic runtime manager over concrete pipelines.
pub(crate) struct RuntimeEffects<S: FrameSource> {
    max_capsules: usize,
    max_feeds: usize,
    capsules: BTreeMap<String, Capsule<S>>,
}

impl<S: FrameSource> RuntimeEffects<S> {
    /// Store parent budgets. Rejects zero. No up-front allocation.
    pub(crate) fn new(max_capsules: usize, max_feeds: usize) -> Result<Self, EngineError> {
        if max_capsules == 0 || max_feeds == 0 {
            return Err(EngineError::InvalidBudget);
        }
        Ok(Self {
            max_capsules,
            max_feeds,
            capsules: BTreeMap::new(),
        })
    }

    /// Admit one whole capsule batch. Async because rejected incoming
    /// pipelines must be aborted (they may own media intents and childs).
    /// On any rejection, aborts only the incoming batch and never touches
    /// an existing recording.
    pub(crate) async fn attach_capsule(
        &mut self,
        capsule_id: String,
        feeds: Vec<(String, CapturePipeline<S>)>,
    ) -> Result<(), EngineError> {
        if feeds.is_empty() {
            return Err(EngineError::EmptyFeeds);
        }
        {
            let mut seen = BTreeSet::new();
            for (key, _) in &feeds {
                if !seen.insert(key.clone()) {
                    abort_incoming(feeds).await;
                    return Err(EngineError::DuplicateFeed);
                }
            }
        }
        if self.capsules.contains_key(&capsule_id) {
            abort_incoming(feeds).await;
            return Err(EngineError::DuplicateCapsule);
        }
        let mut current_total: usize = 0;
        for capsule in self.capsules.values() {
            current_total = match current_total.checked_add(capsule.feeds.len()) {
                Some(v) => v,
                None => {
                    abort_incoming(feeds).await;
                    return Err(EngineError::Overflow);
                }
            };
        }
        let new_total = match current_total.checked_add(feeds.len()) {
            Some(v) => v,
            None => {
                abort_incoming(feeds).await;
                return Err(EngineError::Overflow);
            }
        };
        let new_capsules = match self.capsules.len().checked_add(1) {
            Some(v) => v,
            None => {
                abort_incoming(feeds).await;
                return Err(EngineError::Overflow);
            }
        };
        if new_capsules > self.max_capsules || new_total > self.max_feeds {
            abort_incoming(feeds).await;
            return Err(EngineError::CapacityExceeded);
        }
        for (_, pipeline) in &feeds {
            if pipeline.recorder_state() != RecordingState::Recording {
                abort_incoming(feeds).await;
                return Err(EngineError::InvalidPipelineState);
            }
        }
        let mut map = BTreeMap::new();
        for (key, pipeline) in feeds {
            map.insert(key, pipeline);
        }
        self.capsules.insert(
            capsule_id,
            Capsule {
                state: CapsuleState::Running,
                feeds: map,
            },
        );
        Ok(())
    }

    /// Tick every feed of every running capsule with the accepted
    /// `pipeline.tick`. On a feed failure, marks that capsule interrupted
    /// and aborts every other feed of the same capsule; other capsules
    /// are preserved with counters and partial evidence. Never publishes.
    /// Returns the first tick cause when any feed failed.
    pub(crate) async fn tick_all(
        &mut self,
        registry: &WindowRegistry,
        now: Instant,
    ) -> Result<(), EngineError> {
        let mut first_err: Option<PipelineError> = None;
        for capsule in self.capsules.values_mut() {
            if capsule.state != CapsuleState::Running {
                continue;
            }
            let keys: Vec<String> = capsule.feeds.keys().cloned().collect();
            let mut failed: Option<PipelineError> = None;
            let mut failed_key: Option<String> = None;
            for key in &keys {
                let pipeline = match capsule.feeds.get_mut(key) {
                    Some(p) => p,
                    None => continue,
                };
                match pipeline.tick(registry, now).await {
                    Ok(_) => {}
                    Err(err) => {
                        failed = Some(err);
                        failed_key = Some(key.clone());
                        break;
                    }
                }
            }
            if let Some(err) = failed {
                capsule.state = CapsuleState::Interrupted;
                for key in &keys {
                    if Some(key) == failed_key.as_ref() {
                        continue;
                    }
                    if let Some(pipeline) = capsule.feeds.get_mut(key) {
                        let _ = pipeline.abort().await;
                    }
                }
                if first_err.is_none() {
                    first_err = Some(err);
                }
            }
        }
        match first_err {
            Some(err) => Err(EngineError::Tick(err)),
            None => Ok(()),
        }
    }

    /// Stop one capsule via exclusive `pipeline.stop` (source stop, drain,
    /// decode validation, durable publication). Returns the batch receipts
    /// only when every feed stop succeeded, and removes only then. On any
    /// failure, marks interrupted, aborts remaining feeds, retains the
    /// batch, and returns the cause. Never claims partial completion.
    pub(crate) async fn stop_capsule(
        &mut self,
        capsule_id: &str,
        journal: &mut Journal,
    ) -> Result<Vec<RecordingReceipt>, EngineError> {
        let keys: Vec<String> = match self.capsules.get(capsule_id) {
            Some(capsule) => capsule.feeds.keys().cloned().collect(),
            None => return Err(EngineError::NotFound),
        };
        let mut receipts = Vec::new();
        for (index, key) in keys.iter().enumerate() {
            let outcome = {
                let capsule = match self.capsules.get_mut(capsule_id) {
                    Some(c) => c,
                    None => return Err(EngineError::NotFound),
                };
                let pipeline = match capsule.feeds.get_mut(key) {
                    Some(p) => p,
                    None => continue,
                };
                pipeline.stop(journal).await
            };
            match outcome {
                Ok(receipt) => receipts.push(receipt),
                Err(err) => {
                    if let Some(capsule) = self.capsules.get_mut(capsule_id) {
                        capsule.state = CapsuleState::Interrupted;
                        for remaining in keys.iter().skip(index + 1) {
                            if let Some(pipeline) = capsule.feeds.get_mut(remaining) {
                                let _ = pipeline.abort().await;
                            }
                        }
                    }
                    return Err(EngineError::Stop(err));
                }
            }
        }
        self.capsules.remove(capsule_id);
        Ok(receipts)
    }

    /// Attempt abort on every feed of every capsule, preserving
    /// interrupted state. `Stopped` is treated as already-cleaned cleanup;
    /// any other pipeline failure is reported after all attempts.
    pub(crate) async fn abort_all(&mut self) -> Result<(), EngineError> {
        let mut first_err: Option<PipelineError> = None;
        for capsule in self.capsules.values_mut() {
            let keys: Vec<String> = capsule.feeds.keys().cloned().collect();
            for key in &keys {
                if let Some(pipeline) = capsule.feeds.get_mut(key) {
                    if let Err(err) = pipeline.abort().await {
                        if !matches!(err, PipelineError::Stopped) && first_err.is_none() {
                            first_err = Some(err);
                        }
                    }
                }
            }
            capsule.state = CapsuleState::Interrupted;
        }
        match first_err {
            Some(err) => Err(EngineError::Abort(err)),
            None => Ok(()),
        }
    }

    /// Bounded read-only snapshot: capsule ids, manager states, feed ids,
    /// recorder states, and counters. Sorted by id. No handles, paths,
    /// tokens, or pids.
    pub(crate) fn status(&self) -> Vec<CapsuleStatus> {
        self.capsules
            .iter()
            .map(|(capsule_id, capsule)| {
                let feeds = capsule
                    .feeds
                    .iter()
                    .map(|(feed_id, pipeline)| FeedStatus {
                        feed_id: feed_id.clone(),
                        state: pipeline.recorder_state(),
                        counters: pipeline.recorder_counters(),
                    })
                    .collect();
                CapsuleStatus {
                    capsule_id: capsule_id.clone(),
                    state: capsule.state,
                    feeds,
                }
            })
            .collect()
    }
}

/// Abort every incoming pipeline during attach rejection. Best-effort
/// cleanup; the caller returns the original rejection cause.
async fn abort_incoming<S: FrameSource>(feeds: Vec<(String, CapturePipeline<S>)>) {
    for (_, mut pipeline) in feeds {
        let _ = pipeline.abort().await;
    }
}
