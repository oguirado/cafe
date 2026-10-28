//! Absolute-deadline frame selector/pacer (P11).
//!
//! Slot deadlines derive from an immutable origin via checked rational
//! arithmetic, never from accumulated relative sleeps. At most one source
//! frame is retained; outputs share pixels through [`Arc`] only. Offering a
//! frame never encodes, touches disk, or blocks on encoder pipes.

#![forbid(unsafe_code)]

use std::sync::Arc;
use std::time::{Duration, Instant};

use crate::capsule_registry::Clock;
use crate::capture_frame_pool::OwnedFrame;

/// Caller-supplied pacing budgets. There are no production defaults.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub struct PacerProfile {
    pub fps_num: u32,
    pub fps_den: u32,
    pub max_lateness_ns: u64,
    pub max_hold_ns: u64,
}

impl PacerProfile {
    /// All fields must be positive and the frame period floor at least 1 ns.
    pub fn validate(self) -> Result<Self, PacerError> {
        if self.fps_num == 0
            || self.fps_den == 0
            || self.max_lateness_ns == 0
            || self.max_hold_ns == 0
        {
            return Err(PacerError::InvalidProfile);
        }
        let period_ns = 1_000_000_000u128
            .checked_mul(self.fps_den as u128)
            .and_then(|scaled| scaled.checked_div(self.fps_num as u128))
            .ok_or(PacerError::InvalidProfile)?;
        if period_ns < 1 {
            return Err(PacerError::InvalidProfile);
        }
        Ok(self)
    }
}

/// Pacer rejection reasons.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum PacerError {
    InvalidProfile,
    ClockRegression,
    TooLate,
    SourceAbsent,
    SourceStalled,
    Overflow,
}

/// Explicit slot counters; drops and repeats are counted separately.
#[derive(Copy, Clone, Debug, Default, PartialEq, Eq)]
pub struct PacerCounters {
    pub source_replaced: u64,
    pub repeated: u64,
    pub selected: u64,
}

/// One selected timeline slot; pixels shared via [`Arc`], never copied.
#[derive(Clone, Debug)]
pub struct OutputFrame {
    pub index: u64,
    pub deadline: Instant,
    pub frame: Arc<OwnedFrame>,
    pub duplicate: bool,
}

/// Buffered source awaiting selection.
struct CurrentSource {
    frame: Arc<OwnedFrame>,
    arrival_now: Instant,
    source_time: Clock,
    selected: bool,
}

/// Absolute monotonic selector over a single buffered source frame.
pub struct Pacer {
    profile: PacerProfile,
    origin: Instant,
    index: u64,
    current: Option<CurrentSource>,
    last_arrival_now: Option<Instant>,
    counters: PacerCounters,
}

impl Pacer {
    /// Validate `profile` and start slot selection at index 0 from `origin`.
    pub fn new(profile: PacerProfile, origin: Instant) -> Result<Self, PacerError> {
        let profile = profile.validate()?;
        Ok(Self {
            profile,
            origin,
            index: 0,
            current: None,
            last_arrival_now: None,
            counters: PacerCounters::default(),
        })
    }

    /// Buffer `frame` as the current source. Counts a replacement only when
    /// the previous source was never selected.
    pub fn offer(&mut self, frame: OwnedFrame, now: Instant) -> Result<(), PacerError> {
        if now < self.origin {
            return Err(PacerError::ClockRegression);
        }
        if let Some(last) = self.last_arrival_now {
            if now < last {
                return Err(PacerError::ClockRegression);
            }
        }
        let source_time = frame.source_time();
        if let Some(current) = self.current.as_ref() {
            if source_time < current.source_time {
                return Err(PacerError::ClockRegression);
            }
            if !current.selected {
                self.counters.source_replaced = self
                    .counters
                    .source_replaced
                    .checked_add(1)
                    .ok_or(PacerError::Overflow)?;
            }
        }
        self.current = Some(CurrentSource {
            frame: Arc::new(frame),
            arrival_now: now,
            source_time,
            selected: false,
        });
        self.last_arrival_now = Some(now);
        Ok(())
    }

    /// Select at most one due slot. Returns `None` before its deadline.
    pub fn select(&mut self, now: Instant) -> Result<Option<OutputFrame>, PacerError> {
        if now < self.origin {
            return Err(PacerError::ClockRegression);
        }
        let slot_index = self.index;
        let offset_ns = u64::try_from(
            (slot_index as u128)
                .checked_mul(1_000_000_000u128)
                .and_then(|scaled| scaled.checked_mul(self.profile.fps_den as u128))
                .and_then(|scaled| scaled.checked_div(self.profile.fps_num as u128))
                .ok_or(PacerError::Overflow)?,
        )
        .map_err(|_| PacerError::Overflow)?;
        let deadline = self
            .origin
            .checked_add(Duration::from_nanos(offset_ns))
            .ok_or(PacerError::Overflow)?;
        if now < deadline {
            return Ok(None);
        }
        if now.duration_since(deadline).as_nanos() > u128::from(self.profile.max_lateness_ns) {
            return Err(PacerError::TooLate);
        }
        let current = self.current.as_mut().ok_or(PacerError::SourceAbsent)?;
        if now < current.arrival_now {
            return Err(PacerError::ClockRegression);
        }
        if now.duration_since(current.arrival_now).as_nanos()
            > u128::from(self.profile.max_hold_ns)
        {
            return Err(PacerError::SourceStalled);
        }
        let duplicate = current.selected;
        let next_index = slot_index.checked_add(1).ok_or(PacerError::Overflow)?;
        let selected = self
            .counters
            .selected
            .checked_add(1)
            .ok_or(PacerError::Overflow)?;
        let repeated = if duplicate {
            self.counters
                .repeated
                .checked_add(1)
                .ok_or(PacerError::Overflow)?
        } else {
            self.counters.repeated
        };
        self.index = next_index;
        self.counters.selected = selected;
        self.counters.repeated = repeated;
        current.selected = true;
        Ok(Some(OutputFrame {
            index: slot_index,
            deadline,
            frame: Arc::clone(&current.frame),
            duplicate,
        }))
    }

    /// Copy of the explicit slot counters.
    pub fn counters(&self) -> PacerCounters {
        self.counters
    }
}
