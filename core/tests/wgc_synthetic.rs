//! Windows-only TEST probe. NativeSupport alone never establishes capture success.
#[cfg(windows)]
#[path = "../src/capsule_registry.rs"] mod capsule_registry;
#[cfg(windows)]
#[path = "../src/windows_window_registry.rs"] mod windows_window_registry;
#[cfg(windows)]
#[path = "../src/capture_health.rs"] mod capture_health;
#[cfg(windows)]
#[path = "../src/capture_frame_pool.rs"] mod capture_frame_pool;
#[cfg(windows)]
#[path = "../src/windows_graphics_capture.rs"] mod windows_graphics_capture;

#[derive(serde::Serialize)]
struct ProbeReport {
    status: &'static str,
    reason: &'static str,
    profile: &'static str,
    frames: u32,
    first_source_time: Option<String>,
    last_source_time: Option<String>,
}

impl ProbeReport {
    fn new(status: &'static str, reason: &'static str) -> Self {
        Self { status, reason, profile: "synthetic_test", frames: 0,
            first_source_time: None, last_source_time: None }
    }
}

fn main() {
    let (report, exit_code) = run();
    // Single sanitized JSON object; no HWND, paths, OS text or pixel dumps.
    match serde_json::to_string(&report) {
        Ok(object) => println!("{object}"),
        Err(_) => {
            println!("{{\"status\":\"fail\",\"reason\":\"report_serialization\"}}");
            std::process::exit(1);
        }
    }
    std::process::exit(exit_code);
}

#[cfg(not(windows))]
fn run() -> (ProbeReport, i32) {
    (ProbeReport::new("not_available", "platform_unsupported"), 78)
}

#[cfg(windows)]
fn run() -> (ProbeReport, i32) {
    use windows::Win32::System::WinRT::{RoInitialize, RoUninitialize, RO_INIT_MULTITHREADED};
    struct Apartment;
    impl Drop for Apartment { fn drop(&mut self) { unsafe { RoUninitialize(); } } }
    // This precedes native_support and is balanced on the same main thread.
    if unsafe { RoInitialize(RO_INIT_MULTITHREADED) }.is_err() {
        return (ProbeReport::new("fail", "winrt_initialization"), 1);
    }
    let _apartment = Apartment;
    let runtime = match tokio::runtime::Builder::new_current_thread().enable_time().build() {
        Ok(runtime) => runtime,
        Err(_) => return (ProbeReport::new("fail", "runtime_initialization"), 1),
    };
    runtime.block_on(run_windows())
}

#[cfg(windows)]
async fn run_windows() -> (ProbeReport, i32) {
    use capsule_registry::Clock;
    use capture_frame_pool::PoolLimits;
    use capture_health::{BorderStatus, CaptureState, HealthPolicy};
    use std::time::{Duration, Instant};
    use windows_graphics_capture::{native_support, CaptureConfig, CaptureError, NativeCapture, NativeSupport};
    use windows_window_registry::{NativeAdapter, SyntheticWindowFixture, WindowError, WindowRegistry};

    // Entire active portion shares this TEST deadline; fixture close has its own bounded wait.
    let deadline = Instant::now() + Duration::from_secs(15);
    match native_support() {
        NativeSupport::Supported => {}
        NativeSupport::Unsupported => return (ProbeReport::new("not_available", "wgc_unsupported"), 78),
        NativeSupport::Unavailable => return (ProbeReport::new("not_available", "wgc_capability_unavailable"), 78),
    }
    let mut registry = WindowRegistry::new(Clock(1));
    let mut adapter = NativeAdapter::new(Clock(1));
    let (fixture, view) = match SyntheticWindowFixture::open(&mut registry, &mut adapter) {
        Ok(pair) => pair,
        Err(WindowError::Unavailable) => return (ProbeReport::new("not_available", "owned_window_unavailable"), 78),
        Err(_) => return (ProbeReport::new("fail", "owned_window_setup"), 1),
    };
    let mut report = ProbeReport::new("fail", "capture_setup");
    let mut exit_code = 1;
    let setup = (|| {
        let permit = registry.capture_permit(&view.handle, view.generation, &mut adapter)
            .map_err(|_| "capture_permit")?;
        permit.check().map_err(|_| "capture_permit_livecheck")?;
        let size = permit.item().Size().map_err(|_| "capture_item_size")?;
        let width = u32::try_from(size.Width).ok().filter(|n| *n > 0).ok_or("capture_item_geometry")?;
        let height = u32::try_from(size.Height).ok().filter(|n| *n > 0).ok_or("capture_item_geometry")?;
        let bytes = usize::try_from(width).ok().and_then(|w| w.checked_mul(4))
            .and_then(|row| usize::try_from(height).ok().and_then(|h| row.checked_mul(h)))
            .ok_or("capture_item_overflow")?;
        let config = CaptureConfig {
            width, height,
            pool: PoolLimits { slots: 2, max_frame_bytes: bytes, max_width: width, max_height: height },
            health: HealthPolicy { stall_budget: Clock(50_000_000) },
            borderless: false,
            access_timeout_ms: 2_000,
        };
        Ok::<_, &'static str>(config)
    })();
    match setup {
        Err(reason) => report.reason = reason,
        Ok(config) => {
            let expected_width = config.width;
            let expected_height = config.height;
            let remaining = deadline.saturating_duration_since(Instant::now());
            let setup_wait = remaining.min(Duration::from_secs(4));
            let started = tokio::time::timeout(setup_wait, NativeCapture::start(
                &registry, &mut adapter, view.handle.clone(), view.generation, config)).await;
            match started {
                Err(_) => report.reason = "capture_start_timeout",
                Ok(Err(error)) => {
                    match error {
                        CaptureError::Unsupported => {
                            report.status = "not_available";
                            report.reason = "capture_unsupported";
                            exit_code = 78;
                        }
                        CaptureError::Unavailable => {
                            report.status = "not_available";
                            report.reason = "capture_capability_unavailable";
                            exit_code = 78;
                        }
                        // Device also covers bridge/session/staging/callback errors: not a hardware-only code.
                        _ => report.reason = "capture_start_error",
                    }
                }
                Ok(Ok(mut capture)) => {
                    if capture.border_status() != BorderStatus::Required {
                        report.reason = "border_indicator_not_preserved";
                    } else {
                        report.reason = "frame_deadline";
                        let mut previous: Option<Clock> = None;
                        while Instant::now() < deadline {
                            let state = capture.poll_state(&registry);
                            if !matches!(state, CaptureState::Waiting | CaptureState::Healthy | CaptureState::Backpressure) {
                                report.reason = "capture_state_rejected";
                                break;
                            }
                            if let Some(frame) = capture.take_latest() {
                                if frame.width() != expected_width || frame.height() != expected_height {
                                    report.reason = "frame_geometry";
                                    break;
                                }
                                let source = frame.source_time();
                                if previous.is_some_and(|last| source.0 <= last.0) {
                                    report.reason = "source_clock_not_increasing";
                                    break;
                                }
                                if !known_center(&frame) {
                                    report.reason = "owned_frame_center_mismatch";
                                    break;
                                }
                                if report.first_source_time.is_none() { report.first_source_time = Some(source.0.to_string()); }
                                report.last_source_time = Some(source.0.to_string());
                                report.frames += 1;
                                previous = Some(source);
                                // Drop the lease before the next callback; do not consume all pool slots.
                                drop(frame);
                                if report.frames >= 2 {
                                    if capture.poll_state(&registry) != CaptureState::Healthy {
                                        report.reason = "capture_not_healthy";
                                        break;
                                    }
                                    report.status = "pass";
                                    report.reason = "actual_owned_frames_verified";
                                    exit_code = 0;
                                    break;
                                }
                            }
                            tokio::time::sleep(Duration::from_millis(10)).await;
                        }
                    }
                    if capture.stop().is_err() {
                        report.status = "fail";
                        report.reason = "capture_stop_error";
                        exit_code = 1;
                    }
                }
            }
        }
    }
    // Always stop capture before destroying the owned fixture. Cleanup failure overrides PASS.
    if fixture.close().is_err() {
        report.status = "fail";
        report.reason = "owned_window_close_error";
        exit_code = 1;
    }
    (report, exit_code)
}

#[cfg(windows)]
fn known_center(frame: &capture_frame_pool::OwnedFrame) -> bool {
    // TEST paint oracle: five-by-five center neighborhood, BGRA tolerance two per channel.
    let width = frame.width() as usize;
    let height = frame.height() as usize;
    if width < 5 || height < 5 || frame.pixels().is_empty() { return false; }
    let Some(stride) = width.checked_mul(4) else { return false; };
    let Some(required) = stride.checked_mul(height) else { return false; };
    if frame.packed_stride() != stride || frame.pixels().len() != required { return false; }
    for y in height / 2 - 2 ..= height / 2 + 2 {
        for x in width / 2 - 2 ..= width / 2 + 2 {
            let Some(index) = y.checked_mul(stride).and_then(|row| x.checked_mul(4).and_then(|col| row.checked_add(col))) else { return false; };
            let Some(pixel) = frame.pixels().get(index..index + 4) else { return false; };
            for (actual, expected) in pixel.iter().zip([0xac_u8, 0x68, 0x24, 0xff]) {
                if actual.abs_diff(expected) > 2 { return false; }
            }
        }
    }
    true
}
