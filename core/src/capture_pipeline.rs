//! Runtime capture-to-recorder pipeline (U1a).
//!
//! Generic only at the capture input (`S: FrameSource`); the recorder is the
//! concrete P11 `Recorder`. The production caller adapter wraps
//! `NativeCapture` and queries the live `WindowRegistry`.

use std::time::Instant;

use crate::authority_journal::Journal;
use crate::capture_frame_pool::OwnedFrame;
use crate::capture_health::CaptureState;
use crate::ffmpeg_recorder::{
    Recorder, RecorderCounters, RecorderError, RecordingReceipt, RecordingState,
};
use crate::windows_graphics_capture::{CaptureError, NativeCapture};
use crate::windows_window_registry::WindowRegistry;

/// Capture input backend behind the pipeline.
pub trait FrameSource {
    /// Take the latest available frame, if any.
    fn take_latest(&mut self) -> Option<OwnedFrame>;
    /// Query current source state against the live registry.
    fn state(&self, registry: &WindowRegistry) -> CaptureState;
    /// Stop capture. Best-effort on failure paths, receipt-blocking on stop.
    fn stop(&mut self) -> Result<(), CaptureError>;
}

/// Adapter over native APIs; the executable caller is wired separately.
pub struct NativeSource {
    capture: NativeCapture,
}

impl NativeSource {
    pub fn new(capture: NativeCapture) -> Self {
        Self { capture }
    }
}

impl FrameSource for NativeSource {
    fn take_latest(&mut self) -> Option<OwnedFrame> {
        self.capture.take_latest()
    }

    fn state(&self, registry: &WindowRegistry) -> CaptureState {
        self.capture.poll_state(registry)
    }

    fn stop(&mut self) -> Result<(), CaptureError> {
        self.capture.stop()
    }
}

/// Per-tick outcome. Waiting never claims a write.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tick {
    Waiting,
    Idle,
    Written,
}

/// Pipeline terminal failures. Never completes on Source or Recorder.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PipelineError {
    Source,
    Recorder(RecorderError),
    Stopped,
}

/// Capture-to-recorder runtime pipeline.
pub struct CapturePipeline<S: FrameSource> {
    source: S,
    recorder: Recorder,
    started: bool,
    stopped: bool,
    cleanup_released: bool,
    live_pump: bool,
}

impl<S: FrameSource> CapturePipeline<S> {
    pub fn new(source: S, recorder: Recorder) -> Self {
        Self {
            source,
            recorder,
            started: false,
            stopped: false,
            cleanup_released: false,
            live_pump: false,
        }
    }

    /// Live runtime keeps pacing independent of the bounded pipe writer.
    pub fn new_live(source: S, recorder: Recorder) -> Self {
        let mut pipeline = Self::new(source, recorder);
        pipeline.live_pump = true;
        pipeline
    }

    /// Drive one capture-to-recorder step on the caller-supplied monotonic `now`.
    ///
    /// Calls `take_latest()` before `state(registry)`. Only `Healthy` admits,
    /// paces, and pumps. Waiting returns `Waiting` without touching the pacer.
    /// Any unhealthy state stops the source, interrupts the recorder, and
    /// never completes. Any recorder error stops the source, interrupts the
    /// recorder, and propagates. A tick after stop has no effect.
    pub async fn tick(
        &mut self,
        registry: &WindowRegistry,
        now: Instant,
    ) -> Result<Tick, PipelineError> {
        let live = self.live_pump;
        self.tick_mode(registry, now, live).await
    }

    /// Live cadence path: confirmed writes are collected without waiting for IO.
    pub async fn tick_nonblocking(&mut self, registry: &WindowRegistry, now: Instant) -> Result<Tick, PipelineError> {
        self.tick_mode(registry, now, true).await
    }

    async fn tick_mode(&mut self, registry: &WindowRegistry, now: Instant, nonblocking: bool) -> Result<Tick, PipelineError> {
        if self.stopped {
            return Err(PipelineError::Stopped);
        }
        let frame = self.source.take_latest();
        let state = self.source.state(registry);
        // Source admission: no frame in a waiting state, no unbounded wait after start.
        if admit_source_state(state, self.started, frame.is_some()).is_err() {
            let _ = self.release_resources().await;
            return Err(PipelineError::Source);
        }
        match state {
            CaptureState::Healthy => {
                let acknowledged = if nonblocking {
                    match self.recorder.poll_pump().await {
                        Ok(value) => value,
                        Err(err) => { let _ = self.release_resources().await; return Err(PipelineError::Recorder(err)); },
                    }
                } else { false };
                if let Some(owned) = frame {
                    if let Err(err) = self.recorder.admit(owned, now) {
                        let _ = self.release_resources().await;
                        return Err(PipelineError::Recorder(err));
                    }
                    self.started = true;
                } else if !self.started {
                    return Ok(Tick::Waiting);
                }
                if let Err(err) = self.recorder.pace(now) {
                    let _ = self.release_resources().await;
                    return Err(PipelineError::Recorder(err));
                }
                let pumped = if nonblocking { self.recorder.begin_pump().map(|_| acknowledged) } else { self.recorder.pump_one().await };
                match pumped {
                    Err(err) => {
                        let _ = self.release_resources().await;
                        Err(PipelineError::Recorder(err))
                    }
                    Ok(true) => Ok(Tick::Written),
                    Ok(false) => Ok(Tick::Idle),
                }
            }
            CaptureState::Waiting => Ok(Tick::Waiting),
            _ => {
                let _ = self.release_resources().await;
                Err(PipelineError::Source)
            }
        }
    }

    /// Two-phase stop: stop capture first, then drain and validate the recorder.
    ///
    /// A failed capture stop interrupts the recorder and cannot publish a
    /// complete receipt. Recorder failures propagate. Marks stopped on every path.
    pub async fn stop(
        &mut self,
        journal: &mut Journal,
    ) -> Result<RecordingReceipt, PipelineError> {
        if self.stopped {
            return Err(PipelineError::Stopped);
        }
        if self.source.stop().is_err() {
            let _ = self.recorder.interrupt().await;
            self.stopped = true;
            return Err(PipelineError::Source);
        }
        match self.recorder.stop(journal).await {
            Ok(receipt) => {
                self.stopped = true;
                self.cleanup_released = true;
                Ok(receipt)
            }
            Err(err) => {
                self.stopped = true;
                Err(PipelineError::Recorder(err))
            }
        }
    }

    /// Terminal abort: always attempt both source stop and child interruption.
    /// It never drains/publishes media or returns a complete receipt.
    pub async fn abort(&mut self)->Result<(),PipelineError> {
        if self.stopped {return Err(PipelineError::Stopped);}
        self.release_resources().await
    }

    /// Neither Interrupted nor stopped proves native cleanup. Keep the witness
    /// false unless capture teardown AND recorder interruption both succeeded.
    async fn release_resources(&mut self)->Result<(),PipelineError> {
        let source_result=self.source.stop();
        let recorder_result=self.recorder.interrupt().await;
        self.cleanup_released=source_result.is_ok()&&recorder_result.is_ok();
        self.stopped=true;
        if source_result.is_err() {return Err(PipelineError::Source);}
        recorder_result.map_err(PipelineError::Recorder)
    }

    pub(crate) fn resources_released(&self)->bool {
        self.cleanup_released
    }

    /// Read-only recorder state observability.
    pub fn recorder_state(&self) -> RecordingState {
        self.recorder.state()
    }

    /// Read-only recorder counters observability.
    pub fn recorder_counters(&self) -> RecorderCounters {
        self.recorder.counters()
    }
}

// Source state admission; all other capture states fail closed.
fn admit_source_state(state: CaptureState, started: bool, has_frame: bool) -> Result<(), PipelineError> {
    match state {
        CaptureState::Healthy => Ok(()),
        CaptureState::Waiting if !started && !has_frame => Ok(()),
        _ => Err(PipelineError::Source),
    }
}
