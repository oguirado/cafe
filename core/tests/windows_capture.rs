//! Handwritten TEST profiles and byte oracles; these are not native measurements.
#[path = "../src/capsule_registry.rs"] mod capsule_registry;
#[path = "../src/windows_window_registry.rs"] mod windows_window_registry;
#[path = "../src/capture_health.rs"] mod capture_health;
#[path = "../src/capture_frame_pool.rs"] mod capture_frame_pool;
#[path = "../src/windows_graphics_capture.rs"] mod windows_graphics_capture;

use capsule_registry::Clock;
use capture_frame_pool::{BorrowedFrame, FramePool, PoolLimits};
use capture_health::{BorderStatus, CaptureEvent, CaptureHealth, CaptureState, HealthError, HealthPolicy};

fn health() -> CaptureHealth {
    CaptureHealth::new(HealthPolicy { stall_budget: Clock(10) }, 7, 2, 2,
                       BorderStatus::Required).unwrap()
}

fn limits() -> PoolLimits {
    PoolLimits { slots: 2, max_frame_bytes: 64, max_width: 4, max_height: 4 }
}

#[test]
fn padded_rows_have_exact_owned_pixels_after_source_destruction() {
    let pool = FramePool::new(limits()).unwrap();
    let owned = {
        let mut source = vec![1,2,3,255, 4,5,6,254, 99,98,97,96,
                              11,12,13,253, 14,15,16,252, 89,88,87,86];
        let owned = pool.copy_from(BorrowedFrame { data: &source, width: 2, height: 2,
            source_stride: 12, source_time: Clock(123) }).unwrap();
        source.fill(0);
        owned
    };
    assert_eq!(owned.pixels(), &[1,2,3,255,4,5,6,254,11,12,13,253,14,15,16,252]);
    assert_eq!(owned.width(), 2);
    assert_eq!(owned.height(), 2);
    assert_eq!(owned.source_stride(), 12);
    assert_eq!(owned.packed_stride(), 8);
    assert_eq!(owned.source_time(), Clock(123));
    assert_eq!(serde_json::to_string(&owned.source_time()).unwrap(), "\"123\"");
}

#[test]
fn leases_exhaust_exact_capacity_and_drop_returns_each_slot() {
    let pool = FramePool::new(limits()).unwrap();
    let bytes = [21,22,23,24];
    let take = || pool.copy_from(BorrowedFrame { data: &bytes, width: 1, height: 1,
        source_stride: 4, source_time: Clock(1) });
    assert_eq!((pool.capacity(),pool.available(),pool.in_use()), (2,2,0));
    let first = take().unwrap();
    let second = take().unwrap();
    assert_eq!((pool.available(),pool.in_use()), (0,2));
    assert!(take().is_err());
    drop(first);
    assert_eq!((pool.available(),pool.in_use()), (1,1));
    let third = take().unwrap();
    assert_eq!(second.pixels(), &[21,22,23,24]);
    drop(second);
    drop(third);
    assert_eq!((pool.available(),pool.in_use()), (2,0));
}

#[test]
fn invalid_pool_limits_never_allocate_a_usable_pool() {
    for profile in [
        PoolLimits { slots: 0, ..limits() },
        PoolLimits { max_frame_bytes: 0, ..limits() },
        PoolLimits { max_width: 0, ..limits() },
        PoolLimits { max_height: 0, ..limits() },
        PoolLimits { slots: usize::MAX, max_frame_bytes: usize::MAX, ..limits() },
        PoolLimits { slots: 1, max_frame_bytes: usize::MAX, ..limits() },
    ] { assert!(FramePool::new(profile).is_err()); }
}

#[test]
fn malformed_source_geometry_stride_lengths_and_overflow_reject() {
    let pool = FramePool::new(limits()).unwrap();
    let data = [0;64];
    for (width,height,stride,len) in [
        (0,1,4,4), (1,0,4,4), (5,1,20,20), (1,5,4,20),
        (2,2,7,16), (2,2,12,19), (1,2,usize::MAX,64),
        (u32::MAX,u32::MAX,usize::MAX,64),
    ] {
        assert!(pool.copy_from(BorrowedFrame { data: &data[..len], width, height,
            source_stride: stride, source_time: Clock(1) }).is_err());
        assert_eq!((pool.available(),pool.in_use()), (2,0));
    }
    let tiny = FramePool::new(PoolLimits { max_frame_bytes: 4, ..limits() }).unwrap();
    assert!(tiny.copy_from(BorrowedFrame { data: &data, width: 2, height: 1,
        source_stride: 8, source_time: Clock(1) }).is_err());
}

#[test]
fn owned_frame_keeps_lease_alive_after_pool_handle_drop() {
    let pool = FramePool::new(limits()).unwrap();
    let bytes = [41,42,43,44];
    let owned = pool.copy_from(BorrowedFrame { data: &bytes, width: 1, height: 1,
        source_stride: 4, source_time: Clock(9) }).unwrap();
    drop(pool);
    assert_eq!(owned.pixels(), &[41,42,43,44]);
}

#[test]
fn waiting_is_not_healthy_and_positive_policy_is_required() {
    assert_eq!(health().status(Clock(1)), CaptureState::Waiting);
    assert!(CaptureHealth::new(HealthPolicy { stall_budget: Clock(0) },7,2,2,
        BorderStatus::Required).is_err());
    assert!(CaptureHealth::new(HealthPolicy { stall_budget: Clock(10) },7,0,2,
        BorderStatus::Required).is_err());
}

#[test]
fn healthy_requires_fresh_copy_and_expires_at_budget_boundary() {
    let mut h = health();
    h.admit_frame(7,2,2,Clock(100),Clock(101)).unwrap();
    assert_eq!(h.status(Clock(109)), CaptureState::Healthy);
    assert_eq!(h.status(Clock(110)), CaptureState::Frozen);
    h.admit_frame(7,2,2,Clock(111),Clock(112)).unwrap();
    assert_eq!(h.status(Clock(112)), CaptureState::Healthy);
}

#[test]
fn repeated_or_backward_source_clock_cannot_refresh_health() {
    for repeated in [Clock(100),Clock(99)] {
        let mut h = health();
        h.admit_frame(7,2,2,Clock(100),Clock(101)).unwrap();
        assert_eq!(h.admit_frame(7,2,2,repeated,Clock(102)), Err(HealthError::Frozen));
        assert_eq!(h.status(Clock(102)), CaptureState::Frozen);
    }
}

#[test]
fn future_stale_and_regressing_arrivals_never_report_healthy() {
    for (source,arrival) in [(101,100),(90,100),(89,100)] {
        let mut h = health();
        assert_eq!(h.admit_frame(7,2,2,Clock(source),Clock(arrival)), Err(HealthError::StaleSource));
        assert_eq!(h.status(Clock(arrival)), CaptureState::Frozen);
    }
    let mut h = health();
    h.admit_frame(7,2,2,Clock(100),Clock(101)).unwrap();
    assert_eq!(h.status(Clock(104)), CaptureState::Healthy);
    assert_eq!(h.admit_frame(7,2,2,Clock(102),Clock(103)), Err(HealthError::ClockRegression));
    assert_eq!(h.status(Clock(104)), CaptureState::ClockRegression);
    let mut h = health();
    h.admit_frame(7,2,2,Clock(100),Clock(101)).unwrap();
    assert_eq!(h.status(Clock(100)), CaptureState::ClockRegression);
}

#[test]
fn minimized_requires_restored_then_a_fresh_admitted_copy() {
    let mut h = health();
    h.admit_frame(7,2,2,Clock(100),Clock(101)).unwrap();
    h.event(CaptureEvent::Minimized);
    h.event(CaptureEvent::Waiting);
    assert_eq!(h.admit_frame(7,2,2,Clock(102),Clock(103)), Err(HealthError::NotAdmissible));
    assert_eq!(h.status(Clock(103)), CaptureState::Minimized);
    h.event(CaptureEvent::Restored);
    assert_eq!(h.status(Clock(103)), CaptureState::Waiting);
    h.admit_frame(7,2,2,Clock(104),Clock(105)).unwrap();
    assert_eq!(h.status(Clock(105)), CaptureState::Healthy);
}

#[test]
fn closed_and_device_lost_are_terminal_despite_later_events() {
    for (event,expected) in [(CaptureEvent::Closed,CaptureState::Closed),
                             (CaptureEvent::DeviceLost,CaptureState::DeviceLost)] {
        let mut h = health();
        h.admit_frame(7,2,2,Clock(100),Clock(101)).unwrap();
        h.event(event);
        h.event(CaptureEvent::Restored);
        h.event(CaptureEvent::Waiting);
        assert_eq!(h.admit_frame(7,2,2,Clock(102),Clock(103)), Err(HealthError::Terminal));
        assert_eq!(h.status(Clock(103)), expected);
    }
}

#[test]
fn resize_and_generation_change_need_explicit_new_capture() {
    let mut h = health();
    assert_eq!(h.admit_frame(7,3,2,Clock(100),Clock(101)), Err(HealthError::GeometryMismatch));
    h.event(CaptureEvent::Restored);
    assert_eq!(h.admit_frame(7,2,2,Clock(102),Clock(103)), Err(HealthError::Terminal));
    assert_eq!(h.status(Clock(103)), CaptureState::ResizeRequired);
    let mut h = health();
    h.event(CaptureEvent::Resized);
    assert_eq!(h.status(Clock(1)), CaptureState::ResizeRequired);
    let mut h = health();
    assert_eq!(h.admit_frame(8,2,2,Clock(100),Clock(101)), Err(HealthError::GenerationMismatch));
    assert_eq!(h.admit_frame(7,2,2,Clock(102),Clock(103)), Err(HealthError::Terminal));
    assert_eq!(h.status(Clock(103)), CaptureState::GenerationMismatch);
}

#[test]
fn border_unavailable_is_never_promoted_by_copy_or_restore() {
    for border in [BorderStatus::Unsupported,BorderStatus::Denied,BorderStatus::Unknown] {
        let mut h = CaptureHealth::new(HealthPolicy{stall_budget:Clock(10)},7,2,2,border).unwrap();
        h.event(CaptureEvent::Restored);
        h.event(CaptureEvent::Waiting);
        assert_eq!(h.admit_frame(7,2,2,Clock(100),Clock(101)),Err(HealthError::NotAdmissible));
        assert_eq!(h.status(Clock(101)), CaptureState::BorderUnavailable);
    }
    let mut h = health();
    h.event(CaptureEvent::BorderUnavailable);
    assert!(h.admit_frame(7,2,2,Clock(100),Clock(101)).is_err());
    assert_eq!(h.status(Clock(101)), CaptureState::BorderUnavailable);
}

#[test]
fn backpressure_and_source_failure_invalidate_success_until_next_copy() {
    for (event,expected) in [(CaptureEvent::Backpressure,CaptureState::Backpressure),
                             (CaptureEvent::SourceFailed,CaptureState::SourceFailed)] {
        let mut h = health();
        h.admit_frame(7,2,2,Clock(100),Clock(101)).unwrap();
        h.event(event);
        assert_eq!(h.status(Clock(102)), expected);
        h.admit_frame(7,2,2,Clock(103),Clock(104)).unwrap();
        assert_eq!(h.status(Clock(104)), CaptureState::Healthy);
    }
}

#[test]
#[cfg(not(windows))]
fn linux_native_capture_is_explicitly_unsupported() {
    assert_eq!(windows_graphics_capture::native_support(),
               windows_graphics_capture::NativeSupport::Unsupported);
}

#[test]
fn observed_dimensions_are_exact_and_resource_bounded_without_window_guessing() {
    use windows_graphics_capture::ObservedCaptureConfig;
    let budget=ObservedCaptureConfig{pool:limits(),health:HealthPolicy{stall_budget:Clock(10)},borderless:false,access_timeout_ms:1000};
    let got=budget.resolve_size(3,2).unwrap();assert_eq!((got.width,got.height),(3,2));
    for (w,h) in [(0,2),(2,0),(-1,2),(2,-1),(5,2),(2,5),(i32::MAX,i32::MAX)] {assert!(budget.resolve_size(w,h).is_err());}
    assert!(ObservedCaptureConfig{pool:PoolLimits{max_frame_bytes:16,..limits()},..budget}.resolve_size(3,2).is_err());
}
