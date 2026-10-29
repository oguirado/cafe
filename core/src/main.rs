//! Single authority private-stdin runtime; unavailable effects remain explicit.

#[allow(dead_code)]
mod capsule_registry;
mod authority_journal;
mod journal_replay;

fn main() { if let Err(code)=runtime_main::run(){eprintln!("CAFE_RUNTIME_BLOCKED:{code}");std::process::exit(1);} }

#[allow(dead_code)]
mod windows_window_registry;
#[allow(dead_code)]
mod capsule_pairing;

#[allow(dead_code)]
mod capture_health;
#[allow(dead_code)]
mod capture_frame_pool;
#[allow(dead_code)]
mod windows_graphics_capture;

#[cfg(windows)]
mod windows_journal_policy;

#[allow(dead_code)]
mod recorder_pacer;
#[allow(dead_code)]
mod ffmpeg_recorder;

#[allow(dead_code)]
mod operator_protocol;

#[allow(dead_code)]
mod authority_auth;
#[allow(dead_code)]
mod runtime_storage;
#[cfg(windows)]
mod runtime_windows_security;

#[allow(dead_code)]
mod control_intents;
#[allow(dead_code)]
mod capture_pipeline;
#[allow(dead_code)]
mod runtime_effects;
mod runtime_main;

#[allow(dead_code)]
mod runtime_recording;
#[allow(dead_code)]
mod python_connector_supervisor;
#[allow(dead_code)]
mod runtime_service_owner;
