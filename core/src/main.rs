//! P06 minimal authority entrypoint: silent skeleton only.

#[allow(dead_code)]
mod capsule_registry;
mod authority_journal;
mod journal_replay;

fn main() {}

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
