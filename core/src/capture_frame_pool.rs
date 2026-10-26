//! Bounded recorder-owned frame pool (P10).
//!
//! Copies borrowed row-pitch source bytes into preallocated tightly packed
//! BGRA slots. All bounds come from the caller-supplied [`PoolLimits`];
//! there are no production defaults here. Slots are leased as [`OwnedFrame`]
//! values and return to the pool on [`Drop`], preserving reserved capacity.

#![forbid(unsafe_code)]

use std::sync::{Arc, Mutex};

use crate::capsule_registry::Clock;

/// Caller-supplied pool profile. Every field must be positive.
/// `max_frame_bytes` is an independent upper bound and may be smaller
/// than `max_width * max_height * 4`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PoolLimits {
    pub slots: usize,
    pub max_frame_bytes: usize,
    pub max_width: u32,
    pub max_height: u32,
}

/// Borrowed source frame. The byte slice, stride, and timestamp describe
/// caller-owned memory that is never retained past [`FramePool::copy_from`].
#[derive(Clone, Copy)]
pub struct BorrowedFrame<'a> {
    pub data: &'a [u8],
    pub width: u32,
    pub height: u32,
    pub source_stride: usize,
    pub source_time: Clock,
}

/// Pool rejection reasons. Callers treat every variant as a refusal;
/// no partial lease is ever handed out on error.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PoolError {
    ZeroLimits,
    InvalidGeometry,
    ArithmeticOverflow,
    FrameTooLarge,
    InvalidStride,
    SourceTooShort,
    Exhausted,
}

/// Preallocated bounded pool. Slot buffers are allocated once at creation
/// and recycled; the hot path performs no heap allocation.
pub struct FramePool {
    limits: PoolLimits,
    free: Mutex<Vec<Vec<u8>>>,
}

impl FramePool {
    /// Creates a pool with `limits.slots` buffers of `limits.max_frame_bytes`
    /// bytes each. Validates checked aggregate arithmetic before allocating.
    pub fn new(limits: PoolLimits) -> Result<Arc<FramePool>, PoolError> {
        if limits.slots == 0
            || limits.max_frame_bytes == 0
            || limits.max_width == 0
            || limits.max_height == 0
        {
            return Err(PoolError::ZeroLimits);
        }
        limits
            .slots
            .checked_mul(limits.max_frame_bytes)
            .ok_or(PoolError::ArithmeticOverflow)?;
        let mut free = Vec::new();
        free.try_reserve_exact(limits.slots).map_err(|_| PoolError::FrameTooLarge)?;
        for _ in 0..limits.slots {
            let mut slot = Vec::new();
            slot.try_reserve_exact(limits.max_frame_bytes).map_err(|_| PoolError::FrameTooLarge)?;
            slot.resize(limits.max_frame_bytes, 0u8);
            free.push(slot);
        }
        Ok(Arc::new(FramePool {
            limits,
            free: Mutex::new(free),
        }))
    }

    /// Copies exactly `width * 4` bytes per row into a leased slot, skipping
    /// source row padding. Validates geometry, stride, and source length
    /// before consuming a slot. Fails closed with [`PoolError::Exhausted`]
    /// when every slot is leased; never falls back to heap allocation.
    pub fn copy_from(self: &Arc<Self>, frame: BorrowedFrame<'_>) -> Result<OwnedFrame, PoolError> {
        if frame.width == 0
            || frame.height == 0
            || frame.width > self.limits.max_width
            || frame.height > self.limits.max_height
        {
            return Err(PoolError::InvalidGeometry);
        }
        let packed_stride = (frame.width as usize)
            .checked_mul(4)
            .ok_or(PoolError::ArithmeticOverflow)?;
        let total = (frame.width as usize)
            .checked_mul(frame.height as usize)
            .and_then(|pixels| pixels.checked_mul(4))
            .ok_or(PoolError::ArithmeticOverflow)?;
        if total > self.limits.max_frame_bytes {
            return Err(PoolError::FrameTooLarge);
        }
        if frame.source_stride < packed_stride {
            return Err(PoolError::InvalidStride);
        }
        let height = frame.height as usize;
        let required = (height - 1)
            .checked_mul(frame.source_stride)
            .and_then(|offset| offset.checked_add(packed_stride))
            .ok_or(PoolError::ArithmeticOverflow)?;
        if frame.data.len() < required {
            return Err(PoolError::SourceTooShort);
        }
        let mut buffer = {
            let mut free = self.free.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
            free.pop().ok_or(PoolError::Exhausted)?
        };
        for row in 0..height {
            let src_start = row * frame.source_stride;
            let dst_start = row * packed_stride;
            buffer[dst_start..dst_start + packed_stride]
                .copy_from_slice(&frame.data[src_start..src_start + packed_stride]);
        }
        Ok(OwnedFrame {
            buffer,
            len: total,
            width: frame.width,
            height: frame.height,
            source_stride: frame.source_stride,
            source_time: frame.source_time,
            pool: Arc::clone(self),
        })
    }

    /// Number of currently free slots.
    pub fn available(&self) -> usize {
        self.free.lock().unwrap_or_else(|poisoned| poisoned.into_inner()).len()
    }

    /// Number of currently leased slots.
    pub fn in_use(&self) -> usize {
        self.limits.slots - self.available()
    }

    /// Total number of slots.
    pub fn capacity(&self) -> usize {
        self.limits.slots
    }
}

impl std::fmt::Debug for FramePool {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("FramePool")
            .field("slots", &self.limits.slots)
            .field("max_frame_bytes", &self.limits.max_frame_bytes)
            .field("max_width", &self.limits.max_width)
            .field("max_height", &self.limits.max_height)
            .field("available", &self.available())
            .finish()
    }
}

/// One leased pool slot holding tightly packed BGRA bytes. Not [`Clone`];
/// the preallocated buffer returns to the pool on [`Drop`], even if the
/// original pool handle was dropped.
pub struct OwnedFrame {
    buffer: Vec<u8>,
    len: usize,
    width: u32,
    height: u32,
    source_stride: usize,
    source_time: Clock,
    pool: Arc<FramePool>,
}

impl OwnedFrame {
    /// Tightly packed `width * height * 4` BGRA bytes.
    pub fn pixels(&self) -> &[u8] {
        &self.buffer[..self.len]
    }

    pub fn width(&self) -> u32 {
        self.width
    }

    pub fn height(&self) -> u32 {
        self.height
    }

    /// Original source row stride the bytes were copied from.
    pub fn source_stride(&self) -> usize {
        self.source_stride
    }

    /// Tightly packed row stride (`width * 4`).
    pub fn packed_stride(&self) -> usize {
        self.width as usize * 4
    }

    /// Original source timestamp preserved from the borrowed frame.
    pub fn source_time(&self) -> Clock {
        self.source_time
    }
}

impl Drop for OwnedFrame {
    fn drop(&mut self) {
        let mut buffer = Vec::new();
        std::mem::swap(&mut buffer, &mut self.buffer);
        let mut free = self.pool.free.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        if free.len() < self.pool.limits.slots {
            free.push(buffer);
        }
    }
}

impl std::fmt::Debug for OwnedFrame {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("OwnedFrame")
            .field("width", &self.width)
            .field("height", &self.height)
            .field("len", &self.len)
            .field("source_stride", &self.source_stride)
            .field("source_time", &self.source_time)
            .finish()
    }
}
