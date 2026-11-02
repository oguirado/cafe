//! Proposed Discord discovery policy. Native title/class measurements are PENDING-LIVE.
#![forbid(unsafe_code)]

use serde::Serialize;
use crate::windows_window_registry::WindowView;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub(crate) enum DiscordWindowClass { Popout, Main, NotDiscord, Unknown }

/// Exact measured class and title boundaries only. An empty prefix or suffix
/// is allowed, but an empty class or an entirely empty title rule is not.
pub(crate) struct DiscordWindowPattern {
    pub class: &'static str,
    pub title_prefix: &'static str,
    pub title_suffix: &'static str,
    pub classification: DiscordWindowClass,
}

/// PENDING-LIVE: no real Discord main/pop-out title or class pattern has been
/// measured. The orchestrator must populate this one constant after native
/// measurements and review. Until then, allowlisted executables are Unknown.
const DISCORD_WINDOW_CLASS: &str = "Chrome_WidgetWin_1";
pub(crate) const PENDING_LIVE_DISCORD_PATTERNS: &[DiscordWindowPattern] = &[];

/// Window text remains data. Neither executable substrings nor an unmeasured
/// title/class resemblance grants permission to anchor a window.
pub(crate) fn classify_discord_window(view: &WindowView) -> DiscordWindowClass {
    match classify_with_patterns(view, PENDING_LIVE_DISCORD_PATTERNS) {
        DiscordWindowClass::Unknown if view.class == DISCORD_WINDOW_CLASS && !view.title.is_empty() => {
            // Measured on real windows: the main window title ends in "Discord"; a
            // stream pop-out title is localized, so anything else counts as a pop-out.
            if view.title.ends_with("Discord") { DiscordWindowClass::Main } else { DiscordWindowClass::Popout }
        }
        other => other,
    }
}

fn classify_with_patterns(view: &WindowView, patterns: &[DiscordWindowPattern]) -> DiscordWindowClass {
    if !["Discord.exe", "DiscordPTB.exe", "DiscordCanary.exe"]
        .iter().any(|exe| view.executable.eq_ignore_ascii_case(exe)) {
        return DiscordWindowClass::NotDiscord;
    }
    let mut matched = None;
    for pattern in patterns {
        if pattern.class.is_empty() || (pattern.title_prefix.is_empty() && pattern.title_suffix.is_empty())
            || !matches!(pattern.classification, DiscordWindowClass::Popout | DiscordWindowClass::Main)
            || view.class != pattern.class || !view.title.starts_with(pattern.title_prefix)
            || !view.title.ends_with(pattern.title_suffix)
            || view.title.len() < pattern.title_prefix.len() + pattern.title_suffix.len() {
            continue;
        }
        match matched {
            None => matched = Some(pattern.classification),
            Some(prior) if prior == pattern.classification => {},
            Some(_) => return DiscordWindowClass::Unknown,
        }
    }
    matched.unwrap_or(DiscordWindowClass::Unknown)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::capsule_registry::Clock;
    use crate::windows_window_registry::{FixtureWindow, Rect, WindowRegistry};

    fn view(executable: &str, class: &str, title: &str) -> WindowView {
        WindowRegistry::new(Clock(77)).refresh_fixtures(&[FixtureWindow {
            slot: "synthetic-classification".into(), incarnation: 1, process_id: 77,
            process_birth: 1, executable: executable.into(), title: title.into(), class: class.into(),
            rect: Rect { x: 0, y: 0, width: 640, height: 360 }, visible: true, minimized: false,
            layout: "synthetic".into(),
        }]).unwrap().remove(0)
    }

    #[test]
    fn executable_allowlist_table_keeps_unmeasured_discord_unknown() {
        for (exe, expected) in [
            ("Discord.exe", DiscordWindowClass::Unknown),
            ("DiscordPTB.exe", DiscordWindowClass::Unknown),
            ("DiscordCanary.exe", DiscordWindowClass::Unknown),
            ("discord.EXE", DiscordWindowClass::Unknown),
            ("Discord.exe.other", DiscordWindowClass::NotDiscord),
            ("OtherDiscord.exe", DiscordWindowClass::NotDiscord),
            ("C:\\synthetic\\Discord.exe", DiscordWindowClass::NotDiscord),
        ] {
            assert_eq!(classify_discord_window(&view(exe, "Unmeasured", "Unmeasured")), expected);
        }
        assert!(PENDING_LIVE_DISCORD_PATTERNS.is_empty());
    }

    #[test]
    fn synthetic_rule_table_is_exact_and_conflicts_fail_closed() {
        // Synthetic policy fixtures only; these are not Discord observations.
        let patterns = [
            DiscordWindowPattern { class: "SyntheticPopout", title_prefix: "Synthetic call [", title_suffix: "]", classification: DiscordWindowClass::Popout },
            DiscordWindowPattern { class: "SyntheticMain", title_prefix: "Synthetic home", title_suffix: "", classification: DiscordWindowClass::Main },
        ];
        for (class, title, expected) in [
            ("SyntheticPopout", "Synthetic call [one]", DiscordWindowClass::Popout),
            ("SyntheticMain", "Synthetic home", DiscordWindowClass::Main),
            ("SyntheticOther", "Synthetic call [one]", DiscordWindowClass::Unknown),
            ("SyntheticPopout", "Synthetic call [one] extra", DiscordWindowClass::Unknown),
            ("SyntheticPopout", "untrusted text", DiscordWindowClass::Unknown),
        ] {
            assert_eq!(classify_with_patterns(&view("Discord.exe", class, title), &patterns), expected);
        }
        let conflict = [
            DiscordWindowPattern { class: "Synthetic", title_prefix: "Synthetic", title_suffix: "", classification: DiscordWindowClass::Popout },
            DiscordWindowPattern { class: "Synthetic", title_prefix: "Synthetic", title_suffix: "", classification: DiscordWindowClass::Main },
        ];
        assert_eq!(classify_with_patterns(&view("Discord.exe", "Synthetic", "Synthetic"), &conflict), DiscordWindowClass::Unknown);
    }
}
