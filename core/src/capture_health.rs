//! Admission after an owned copy. Policy clocks share one monotonic domain.
use crate::capsule_registry::Clock;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HealthPolicy {
    pub stall_budget: Clock,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BorderStatus { Required, Available, Unsupported, Denied, Unknown }

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CaptureState {
    Waiting, Healthy, Minimized, Frozen, Closed, DeviceLost, ResizeRequired,
    GenerationMismatch, Backpressure, BorderUnavailable, SourceFailed, ClockRegression,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CaptureEvent {
    Waiting, Minimized, Restored, Closed, DeviceLost, Resized, Backpressure,
    BorderUnavailable, SourceFailed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HealthError {
    InvalidPolicy, InvalidGeometry, Terminal, GenerationMismatch, GeometryMismatch,
    NotAdmissible, Frozen, ClockRegression, StaleSource,
}

#[derive(Debug)]
pub struct CaptureHealth {
    policy: HealthPolicy,
    generation: u64,
    width: u32,
    height: u32,
    state: CaptureState,
    border_available: bool,
    last_source: Option<Clock>,
    last_arrival: Option<Clock>,
    last_observation: Option<Clock>,
}

impl CaptureHealth {
    pub fn new(policy: HealthPolicy, generation: u64, width: u32, height: u32,
               border: BorderStatus) -> Result<Self, HealthError> {
        if policy.stall_budget.0 == 0 { return Err(HealthError::InvalidPolicy); }
        if width == 0 || height == 0 { return Err(HealthError::InvalidGeometry); }
        let border_available = matches!(border, BorderStatus::Required | BorderStatus::Available);
        Ok(Self {
            policy, generation, width, height, border_available,
            state: if border_available { CaptureState::Waiting } else { CaptureState::BorderUnavailable },
            last_source: None, last_arrival: None, last_observation: None,
        })
    }

    fn requires_restart(&self) -> bool {
        matches!(self.state, CaptureState::Closed | CaptureState::DeviceLost |
                 CaptureState::ResizeRequired | CaptureState::GenerationMismatch)
    }

    pub fn event(&mut self, event: CaptureEvent) {
        if self.requires_restart() { return; }
        // Native failures dominate capability and temporary lifecycle events.
        self.state = match event {
            CaptureEvent::Closed => CaptureState::Closed,
            CaptureEvent::DeviceLost => CaptureState::DeviceLost,
            CaptureEvent::Resized => CaptureState::ResizeRequired,
            CaptureEvent::BorderUnavailable => {
                self.border_available = false;
                CaptureState::BorderUnavailable
            }
            _ if !self.border_available => CaptureState::BorderUnavailable,
            CaptureEvent::Minimized => CaptureState::Minimized,
            CaptureEvent::Restored if self.state == CaptureState::Minimized => CaptureState::Waiting,
            CaptureEvent::Restored => self.state,
            // Only an explicit Restored event clears minimization.
            _ if self.state == CaptureState::Minimized => CaptureState::Minimized,
            CaptureEvent::Waiting => CaptureState::Waiting,
            CaptureEvent::Backpressure => CaptureState::Backpressure,
            CaptureEvent::SourceFailed => CaptureState::SourceFailed,
        };
    }

    pub fn admit_frame(&mut self, generation: u64, width: u32, height: u32,
                       source_time: Clock, observed_at: Clock) -> Result<(), HealthError> {
        if self.requires_restart() { return Err(HealthError::Terminal); }
        if generation != self.generation {
            self.state = CaptureState::GenerationMismatch;
            return Err(HealthError::GenerationMismatch);
        }
        if width != self.width || height != self.height {
            self.state = CaptureState::ResizeRequired;
            return Err(HealthError::GeometryMismatch);
        }
        if !self.border_available || self.state == CaptureState::Minimized {
            return Err(HealthError::NotAdmissible);
        }
        if self.last_observation.is_some_and(|last| observed_at.0 < last.0) {
            self.state = CaptureState::ClockRegression;
            return Err(HealthError::ClockRegression);
        }
        self.last_observation = Some(observed_at);
        if self.last_source.is_some_and(|last| source_time.0 <= last.0) {
            self.state = CaptureState::Frozen;
            return Err(HealthError::Frozen);
        }
        if source_time.0 > observed_at.0 ||
            observed_at.0 - source_time.0 >= self.policy.stall_budget.0 {
            self.state = CaptureState::Frozen;
            return Err(HealthError::StaleSource);
        }
        self.last_source = Some(source_time);
        self.last_arrival = Some(observed_at);
        self.state = CaptureState::Healthy;
        Ok(())
    }

    pub fn status(&mut self, now: Clock) -> CaptureState {
        if self.requires_restart() { return self.state; }
        if self.last_observation.is_some_and(|last| now.0 < last.0) {
            self.state = CaptureState::ClockRegression;
            return self.state;
        }
        self.last_observation = Some(now);
        if self.state == CaptureState::Healthy {
            let fresh = match (self.last_source, self.last_arrival) {
                (Some(source), Some(arrival)) => now.0 >= source.0 && now.0 >= arrival.0 &&
                    now.0 - source.0 < self.policy.stall_budget.0 &&
                    now.0 - arrival.0 < self.policy.stall_budget.0,
                _ => false,
            };
            if !fresh { self.state = CaptureState::Frozen; }
        }
        self.state
    }
}
