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

    /// Validated 60/1 profile with caller-supplied budgets; no globals.
    pub fn sixty_fps(max_lateness_ns: u64, max_hold_ns: u64) -> Result<Self, PacerError> {
        Self {
            fps_num: 60,
            fps_den: 1,
            max_lateness_ns,
            max_hold_ns,
        }
        .validate()
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

/// Explicit preview counters; emission and decimation counted separately.
#[derive(Copy, Clone, Debug, Default, PartialEq, Eq)]
pub struct PreviewCounters {
    pub emitted: u64,
    pub decimated: u64,
}

/// Bounded preview decimator over recording-slot indices.
///
/// The recording slot index of each observed [`OutputFrame`] is the preview
/// clock: the first observed output must be index 0 and later indices must
/// strictly increase. Holds only scalar state; emitted previews share pixels
/// through [`Arc`] only and no frame is ever retained.
pub struct PreviewSelector {
    record_num: u32,
    record_den: u32,
    preview_num: u32,
    preview_den: u32,
    last_index: Option<u64>,
    last_tick: u64,
    counters: PreviewCounters,
}

impl PreviewSelector {
    /// Validate `record` and bind a preview rate of `fps_num`/`fps_den`.
    /// Rejects zero rates, preview rates above 60 FPS, and preview rates
    /// above the recording rate. All comparisons are checked `u128`
    /// cross-products; no floating point.
    pub fn new(
        record: PacerProfile,
        fps_num: u32,
        fps_den: u32,
    ) -> Result<Self, PacerError> {
        let record = record.validate()?;
        if fps_num == 0 || fps_den == 0 {
            return Err(PacerError::InvalidProfile);
        }
        let sixty = 60u128
            .checked_mul(fps_den as u128)
            .ok_or(PacerError::InvalidProfile)?;
        if (fps_num as u128) > sixty {
            return Err(PacerError::InvalidProfile);
        }
        let preview_scaled = (fps_num as u128)
            .checked_mul(record.fps_den as u128)
            .ok_or(PacerError::InvalidProfile)?;
        let record_scaled = (record.fps_num as u128)
            .checked_mul(fps_den as u128)
            .ok_or(PacerError::InvalidProfile)?;
        if preview_scaled > record_scaled {
            return Err(PacerError::InvalidProfile);
        }
        Ok(Self {
            record_num: record.fps_num,
            record_den: record.fps_den,
            preview_num: fps_num,
            preview_den: fps_den,
            last_index: None,
            last_tick: 0,
            counters: PreviewCounters::default(),
        })
    }

    /// Preview tick of recording slot `index`:
    /// `floor(index * preview_num * record_den / (preview_den * record_num))`.
    fn tick_for(
        index: u64,
        preview_num: u32,
        preview_den: u32,
        record_num: u32,
        record_den: u32,
    ) -> Result<u64, PacerError> {
        let numerator = (index as u128)
            .checked_mul(preview_num as u128)
            .and_then(|scaled| scaled.checked_mul(record_den as u128))
            .ok_or(PacerError::Overflow)?;
        let denominator = (preview_den as u128)
            .checked_mul(record_num as u128)
            .ok_or(PacerError::Overflow)?;
        let tick = numerator
            .checked_div(denominator)
            .ok_or(PacerError::Overflow)?;
        u64::try_from(tick).map_err(|_| PacerError::Overflow)
    }

    /// Observe one recording output. Emits index 0, then emits exactly when
    /// the preview tick increases relative to the preceding recording index;
    /// otherwise counts one decimation. At most one preview per call; never
    /// touches [`Pacer`] state. Rejects duplicate or regressing indices and
    /// any arithmetic or counter overflow with no partial mutation.
    pub fn observe(
        &mut self,
        output: &OutputFrame,
    ) -> Result<Option<Arc<OwnedFrame>>, PacerError> {
        let index = output.index;
        match self.last_index {
            None => {
                if index != 0 {
                    return Err(PacerError::ClockRegression);
                }
            }
            Some(last) => {
                if index <= last {
                    return Err(PacerError::ClockRegression);
                }
            }
        }
        let tick = Self::tick_for(
            index,
            self.preview_num,
            self.preview_den,
            self.record_num,
            self.record_den,
        )?;
        let emit = match self.last_index {
            None => true,
            Some(_) => tick > self.last_tick,
        };
        if emit {
            let emitted = self
                .counters
                .emitted
                .checked_add(1)
                .ok_or(PacerError::Overflow)?;
            self.last_index = Some(index);
            self.last_tick = tick;
            self.counters.emitted = emitted;
            Ok(Some(Arc::clone(&output.frame)))
        } else {
            let decimated = self
                .counters
                .decimated
                .checked_add(1)
                .ok_or(PacerError::Overflow)?;
            self.last_index = Some(index);
            self.last_tick = tick;
            self.counters.decimated = decimated;
            Ok(None)
        }
    }

    /// Copy of the explicit preview counters.
    pub fn counters(&self) -> PreviewCounters {
        self.counters
    }
}
