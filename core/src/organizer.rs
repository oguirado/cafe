//! Bounded proposal state, never an authority or a confirmation issuer.
//! Native Discord organization remains PENDING-LIVE.
#![forbid(unsafe_code)]

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::time::{Duration, Instant};
use serde_json::{json, Value};
use crate::capsule_registry::Clock;
use crate::discord_windows::{classify_discord_window, DiscordWindowClass};
use crate::windows_window_registry::{WindowError, WindowHandle, WindowRegistry, WindowView};

pub(crate) const MAX_CAPSULES: usize = 12;
pub(crate) const PLAN_TTL: Duration = Duration::from_secs(60);
const MAX_PLAN_RESULTS: usize = 64;
const MAX_SAFE_JSON_INTEGER: u64 = (1_u64 << 53) - 1;

pub(crate) fn folder_name_valid(name: &str) -> bool {
    let bytes = name.as_bytes();
    !bytes.is_empty() && bytes.len() <= 64 && bytes[0].is_ascii_alphanumeric()
        && bytes.iter().all(|byte| byte.is_ascii_alphanumeric() || b" _-".contains(byte))
}

fn identifier(value: &str) -> bool {
    !value.is_empty() && value.len() <= 96 && value.as_bytes()[0].is_ascii_alphanumeric()
        && value.bytes().all(|byte| byte.is_ascii_alphanumeric() || b"_.:-".contains(&byte))
}

pub(crate) fn window_token(view: &WindowView) -> String {
    // Serialization is the existing opaque-token boundary; native handles and
    // process lifetime witnesses are never reconstructed from this token.
    serde_json::to_value(&view.handle).ok().and_then(|value| value.as_str().map(str::to_owned)).unwrap_or_default()
}

/// A proposal contains observations, not authority. It deliberately cannot be
/// deserialized from remote input or serialized back as a confirmation token.
#[derive(Clone)]
pub(crate) enum PlanKind {
    Recording { windows: Vec<WindowView>, folder_name: String },
    StopAll { capsule_ids: Vec<String> },
    SendToObs { capsules: Vec<(String, String)> },
}

#[derive(Clone)]
pub(crate) struct PendingPlan {
    pub plan_id: String,
    pub authority_epoch: Clock,
    pub kind: PlanKind,
    pub expires_unix_ms: u64,
    deadline: Instant,
}

impl PendingPlan {
    fn titles(&self) -> Vec<String> {
        match &self.kind {
            PlanKind::Recording { windows, .. } => windows.iter().map(|view| view.title.clone()).collect(),
            PlanKind::StopAll { capsule_ids } => capsule_ids.clone(),
            PlanKind::SendToObs { capsules } => capsules.iter().map(|(_, title)| title.clone()).collect(),
        }
    }

    fn projection(&self) -> Value {
        let folder = match &self.kind { PlanKind::Recording { folder_name, .. } => folder_name.as_str(), PlanKind::StopAll { .. } | PlanKind::SendToObs { .. } => "" };
        json!({"plan_id": self.plan_id, "windows": self.titles().into_iter().map(|title| json!({"title": title})).collect::<Vec<_>>(),
            "folder_name": folder, "expires_unix_ms": self.expires_unix_ms})
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum PlanState { PendingConfirmation, Executing, Completed, Rejected, Expired }

struct WindowResult { title: String, capsule_id: Option<String>, state: &'static str }
struct PlanResult { state: PlanState, windows: Vec<WindowResult> }

/// No durable "on" setting: constructing an authority always starts disabled.
pub(crate) struct Organizer {
    epoch: Clock,
    sequence: u64,
    pending: Option<PendingPlan>,
    results: BTreeMap<String, PlanResult>,
    result_order: VecDeque<String>,
    auto_anchor: bool,
    last_poll: Option<Instant>,
}

impl Organizer {
    pub(crate) fn new(epoch: Clock) -> Self {
        Self { epoch, sequence: 0, pending: None, results: BTreeMap::new(), result_order: VecDeque::new(), auto_anchor: false, last_poll: None }
    }

    pub(crate) fn auto_anchor(&self) -> bool { self.auto_anchor }
    pub(crate) fn set_auto_anchor(&mut self, enabled: bool) { self.auto_anchor = enabled; }

    /// Toggling the option does not reset this throttle or create a recording.
    pub(crate) fn poll_due(&mut self, now: Instant) -> bool {
        if !self.auto_anchor || self.last_poll.is_some_and(|prior| now.checked_duration_since(prior).is_none_or(|elapsed| elapsed < Duration::from_secs(1))) { return false; }
        self.last_poll = Some(now);
        true
    }

    pub(crate) fn projection(&self) -> Value {
        self.pending.as_ref().map_or(Value::Null, PendingPlan::projection)
    }

    pub(crate) fn expire(&mut self, now: Instant) -> bool {
        if !self.pending.as_ref().is_some_and(|plan| now >= plan.deadline) { return false; }
        if let Some(plan) = self.pending.take() {
            if let Some(result) = self.results.get_mut(&plan.plan_id) { result.state = PlanState::Expired; }
        }
        true
    }

    fn ready(&mut self, now: Instant) -> Result<(), &'static str> {
        self.expire(now);
        if self.pending.is_some() { return Err("plan_pending"); }
        if self.results.len() >= MAX_PLAN_RESULTS {
            let position = self.result_order.iter().position(|id| self.results.get(id).is_some_and(|result|
                matches!(result.state, PlanState::Completed | PlanState::Rejected | PlanState::Expired)));
            let Some(position) = position else { return Err("unavailable"); };
            if let Some(id) = self.result_order.remove(position) { self.results.remove(&id); }
        }
        Ok(())
    }

    fn insert(&mut self, kind: PlanKind, now: Instant, unix_ms: u64) -> Result<String, &'static str> {
        self.ready(now)?;
        let deadline = now.checked_add(PLAN_TTL).ok_or("unavailable")?;
        let expires_unix_ms = unix_ms.checked_add(60_000).filter(|expiry| *expiry <= MAX_SAFE_JSON_INTEGER).ok_or("unavailable")?;
        let next = self.sequence.checked_add(1).ok_or("unavailable")?;
        let plan_id = format!("organizer-{}-{next}", self.epoch.0);
        let plan = PendingPlan { plan_id: plan_id.clone(), authority_epoch: self.epoch, kind, expires_unix_ms, deadline };
        let windows = plan.titles().into_iter().map(|title| WindowResult { title, capsule_id: None, state: "pending" }).collect();
        self.results.insert(plan_id.clone(), PlanResult { state: PlanState::PendingConfirmation, windows });
        self.result_order.push_back(plan_id.clone());
        self.pending = Some(plan);
        self.sequence = next;
        Ok(plan_id)
    }

    pub(crate) fn propose_recording(&mut self, folder_name: String, tokens: Option<Vec<String>>, views: &[WindowView], folder_root_set: bool, now: Instant, unix_ms: u64) -> Result<String, &'static str> {
        self.ready(now)?;
        if !folder_root_set { return Err("folder_root_required"); }
        if !folder_name_valid(&folder_name) { return Err("folder_name_invalid"); }
        let windows = select_popouts(views, tokens.as_deref(), classify_discord_window)?;
        self.insert(PlanKind::Recording { windows, folder_name }, now, unix_ms)
    }

    pub(crate) fn propose_stop_all(&mut self, capsule_ids: Vec<String>, now: Instant, unix_ms: u64) -> Result<String, &'static str> {
        self.ready(now)?;
        if capsule_ids.len() > MAX_CAPSULES || capsule_ids.iter().any(|id| !identifier(id))
            || capsule_ids.iter().collect::<BTreeSet<_>>().len() != capsule_ids.len() { return Err("unavailable"); }
        self.insert(PlanKind::StopAll { capsule_ids }, now, unix_ms)
    }

    pub(crate) fn pending_empty_stop(&self, plan_id: &str, now: Instant) -> bool {
        self.pending.as_ref().is_some_and(|plan| plan.plan_id == plan_id && now < plan.deadline
            && matches!(&plan.kind, PlanKind::StopAll { capsule_ids } if capsule_ids.is_empty()))
    }

    pub(crate) fn propose_send_to_obs(&mut self, capsules: Vec<(String, String)>, obs_ready: bool, now: Instant, unix_ms: u64) -> Result<String, &'static str> {
        self.ready(now)?;
        if !obs_ready { return Err("obs_not_ready"); }
        if capsules.is_empty() || capsules.len() > MAX_CAPSULES
            || capsules.iter().any(|(id, title)| !identifier(id) || title.len() > 2048 || title.contains('\0'))
            || capsules.iter().map(|(id, _)| id).collect::<BTreeSet<_>>().len() != capsules.len() { return Err("unavailable"); }
        self.insert(PlanKind::SendToObs { capsules }, now, unix_ms)
    }

    /// Called by the authority after local Confirm or direct-mode admission.
    /// This consumes a pending plan; it does not grant control approval.
    pub(crate) fn take_confirmed(&mut self, plan_id: &str, now: Instant) -> Result<PendingPlan, &'static str> {
        self.expire(now);
        if !self.pending.as_ref().is_some_and(|plan| plan.plan_id == plan_id) { return Err("plan_unknown"); }
        let plan = self.pending.take().ok_or("plan_unknown")?;
        // A confirmed empty stop has no actor outcomes to wait for. Other plans
        // remain executing until their actual per-window outcomes arrive.
        self.results.get_mut(plan_id).ok_or("plan_unknown")?.state =
            if matches!(&plan.kind, PlanKind::StopAll { capsule_ids } if capsule_ids.is_empty()) {
                PlanState::Completed
            } else { PlanState::Executing };
        Ok(plan)
    }

    pub(crate) fn reject(&mut self, plan_id: &str, now: Instant) -> Result<(), &'static str> {
        self.expire(now);
        if !self.pending.as_ref().is_some_and(|plan| plan.plan_id == plan_id) { return Err("plan_unknown"); }
        self.pending = None;
        self.results.get_mut(plan_id).ok_or("plan_unknown")?.state = PlanState::Rejected;
        Ok(())
    }

    /// State comes from an observed actor outcome, never queue admission.
    pub(crate) fn set_window_result(&mut self, plan_id: &str, index: usize, capsule_id: Option<String>, state: &str) -> Result<(), &'static str> {
        let state = closed_window_result(state).ok_or("unavailable")?;
        if capsule_id.as_ref().is_some_and(|id| !identifier(id)) { return Err("unavailable"); }
        let result = self.results.get_mut(plan_id).ok_or("plan_unknown")?;
        if result.state != PlanState::Executing { return Err("plan_unknown"); }
        let row = result.windows.get_mut(index).ok_or("unavailable")?;
        if row.state != "pending" { return Err("unavailable"); }
        row.capsule_id = capsule_id;
        row.state = state;
        if result.windows.iter().all(|row| row.state != "pending") { result.state = PlanState::Completed; }
        Ok(())
    }

    pub(crate) fn plan_result(&self, plan_id: &str) -> Result<Value, &'static str> {
        let result = self.results.get(plan_id).ok_or("plan_unknown")?;
        let state = match result.state {
            PlanState::PendingConfirmation => "pending_confirmation", PlanState::Rejected => "rejected",
            PlanState::Expired => "expired", PlanState::Completed => "completed",
            PlanState::Executing => return Err("unavailable"),
        };
        let rows = if result.state == PlanState::Completed {
            result.windows.iter().map(|row| json!({"title": row.title, "capsule_id": row.capsule_id, "state": row.state})).collect::<Vec<_>>()
        } else { Vec::new() };
        Ok(json!({"plan_id": plan_id, "state": state, "windows": rows}))
    }
}

fn closed_window_result(state: &str) -> Option<&'static str> {
    match state {
        "recording" => Some("recording"), "stopped" => Some("stopped"), "sent" => Some("sent"),
        "obs_not_ready" => Some("obs_not_ready"),
        "encoder_session_limit" => Some("encoder_session_limit"),
        "encoder_unavailable" => Some("encoder_unavailable"),
        "capture_start_unavailable" => Some("capture_start_unavailable"),
        "window_unavailable" => Some("window_unavailable"),
        "window_titles_identical" => Some("window_titles_identical"),
        "window_changed" => Some("window_changed"),
        "folder_not_authorized" => Some("folder_not_authorized"),
        "disk_write_failed" => Some("disk_write_failed"),
        "capsule_limit" => Some("capsule_limit"),
        "already_recording" => Some("already_recording"),
        "unavailable" => Some("unavailable"),
        _ => None,
    }
}

fn select_popouts(views: &[WindowView], tokens: Option<&[String]>, classify: fn(&WindowView) -> DiscordWindowClass) -> Result<Vec<WindowView>, &'static str> {
    if let Some(tokens) = tokens {
        if tokens.is_empty() || tokens.len() > MAX_CAPSULES || tokens.iter().any(|token| token.is_empty() || token.len() > 128 || token.contains('\0'))
            || tokens.iter().collect::<BTreeSet<_>>().len() != tokens.len() { return Err("unavailable"); }
    }
    let selected = views.iter().filter(|view| tokens.is_none_or(|tokens| tokens.iter().any(|token| *token == window_token(view))))
        .filter(|view| classify(view) == DiscordWindowClass::Popout && view.visible && !view.minimized)
        .cloned().collect::<Vec<_>>();
    if selected.is_empty() || tokens.is_some_and(|tokens| tokens.len() != selected.len()) { return Err("no_discord_windows"); }
    if selected.len() > MAX_CAPSULES { return Err("unavailable"); }
    for view in &selected {
        if views.iter().filter(|other| other.visible && !other.minimized && other.executable == view.executable && other.class == view.class && other.title == view.title).count() != 1 {
            return Err("window_titles_identical");
        }
    }
    Ok(selected)
}

/// Re-resolve the exact observation at click time. Classification is checked
/// separately by the authority; this helper cannot authorize a window.
pub(crate) fn resolve_frozen_window(windows: &WindowRegistry, frozen: &WindowView) -> Result<WindowView, &'static str> {
    let view = windows.resolve(&frozen.handle, frozen.generation).map_err(|error| match error {
        WindowError::AmbiguousDescriptor => "window_titles_identical", _ => "window_unavailable",
    })?;
    if view != *frozen { return Err("window_changed"); }
    Ok(view)
}

/// Stable opaque identities are suitable for idempotence; native HWND values
/// and titles are not identity keys.
pub(crate) fn identity(view: &WindowView) -> (WindowHandle, u64) { (view.handle.clone(), view.generation.0) }

#[cfg(test)]
mod tests {
    use super::*;
    use crate::windows_window_registry::{FixtureWindow, Rect};

    fn fixture(slot: &str, title: &str) -> FixtureWindow {
        FixtureWindow { slot: slot.into(), incarnation: 1, process_id: 10, process_birth: 1,
            executable: "Discord.exe".into(), title: title.into(), class: "SyntheticPopout".into(),
            rect: Rect { x: 0, y: 0, width: 320, height: 200 }, visible: true, minimized: false, layout: "one".into() }
    }

    #[test]
    fn selected_popouts_are_frozen_and_descriptor_ambiguity_is_distinct() {
        let mut registry = WindowRegistry::new(Clock(7));
        let views = registry.refresh_fixtures(&[fixture("one", "Synthetic one"), fixture("two", "Synthetic two")]).unwrap();
        let selected = select_popouts(&views, Some(&[window_token(&views[1])]), |_| DiscordWindowClass::Popout).unwrap();
        assert_eq!(selected, vec![views[1].clone()]);
        let duplicate = registry.refresh_fixtures(&[fixture("one", "Synthetic one"), fixture("two", "Synthetic one")]).unwrap();
        assert_eq!(select_popouts(&duplicate, None, |_| DiscordWindowClass::Popout).unwrap_err(), "window_titles_identical");
    }

    #[test]
    fn text_is_data_and_unknown_is_never_selected() {
        let mut registry = WindowRegistry::new(Clock(7));
        let views = registry.refresh_fixtures(&[fixture("one", "Synthetic; uninterpreted text")]).unwrap();
        assert_eq!(select_popouts(&views, None, |_| DiscordWindowClass::Unknown).unwrap_err(), "no_discord_windows");
        assert_eq!(select_popouts(&views, Some(&[window_token(&views[0]), window_token(&views[0])]), |_| DiscordWindowClass::Popout).unwrap_err(), "unavailable");
        assert_eq!(select_popouts(&views, Some(&["unobserved-token".into()]), |_| DiscordWindowClass::Popout).unwrap_err(), "no_discord_windows");
    }

    #[test]
    fn recording_execution_is_not_a_completed_result() {
        let now = Instant::now();
        let mut organizer = Organizer::new(Clock(7));
        let mut registry = WindowRegistry::new(Clock(7));
        let views = registry.refresh_fixtures(&[fixture("one", "Synthetic one"), fixture("two", "Synthetic two")]).unwrap();
        let id = organizer.insert(PlanKind::Recording { windows: views.clone(), folder_name: "Session 1".into() }, now, 1000).unwrap();
        assert_eq!(organizer.projection(), json!({"plan_id": id, "windows": [{"title": "Synthetic one"}, {"title": "Synthetic two"}], "folder_name": "Session 1", "expires_unix_ms": 61000}));
        let confirmed = organizer.take_confirmed(&id, now).unwrap();
        assert_eq!(confirmed.authority_epoch, Clock(7));
        assert!(organizer.projection().is_null());
        assert_eq!(organizer.plan_result(&id), Err("unavailable"));
        organizer.set_window_result(&id, 0, Some("slot-1".into()), "recording").unwrap();
        assert_eq!(organizer.plan_result(&id), Err("unavailable"));
        organizer.set_window_result(&id, 1, Some("slot-2".into()), "encoder_session_limit").unwrap();
        let final_result = organizer.plan_result(&id).unwrap();
        assert_eq!(final_result["state"], "completed");
        assert_eq!(final_result["windows"][0]["state"], "recording");
        assert_eq!(final_result["windows"][1]["state"], "encoder_session_limit");
        assert!(organizer.take_confirmed(&id, now).is_err());
        assert!(organizer.set_window_result(&id, 0, None, "stopped").is_err());
    }

    #[test]
    fn stale_identity_title_geometry_and_layout_cannot_be_confirmed() {
        for changed in 0..5 {
            let mut registry = WindowRegistry::new(Clock(7));
            let original = fixture("one", "Synthetic one");
            let frozen = registry.refresh_fixtures(&[original.clone()]).unwrap().remove(0);
            let mut current = original;
            match changed { 0 => current.incarnation += 1, 1 => current.title.push_str(" changed"),
                2 => current.rect.width += 1, 3 => current.layout.push_str(" changed"), _ => current.minimized = true }
            registry.refresh_fixtures(&[current]).unwrap();
            assert!(resolve_frozen_window(&registry, &frozen).is_err());
        }
    }
}

#[cfg(test)]
mod contract_tests {

use std::time::{Duration, Instant};
use crate::capsule_registry::Clock;
use crate::organizer::{folder_name_valid, Organizer, MAX_CAPSULES, PLAN_TTL};

#[test]
fn folder_segment_boundary_table_rejects_paths_and_traversal() {
    for name in ["Session", "Session 2", "a_b-c", "0", &"a".repeat(64)] { assert!(folder_name_valid(name), "synthetic valid segment rejected"); }
    for name in ["", ".", "..", "../Session", "Session/child", "Session\\child", "C:Session", "C:\\Session", "/Session", " leading", "_leading", "-leading", "Session.", "Session\0", "á", &"a".repeat(65)] {
        assert!(!folder_name_valid(name), "synthetic invalid segment accepted");
    }
}

#[test]
fn one_pending_plan_expires_without_confirmation_or_effect() {
    let now = Instant::now();
    let mut organizer = Organizer::new(Clock(77));
    let id = organizer.propose_stop_all(vec!["slot-1".into()], now, 1000).unwrap();
    assert_eq!(organizer.plan_result(&id).unwrap()["state"], "pending_confirmation");
    assert_eq!(organizer.propose_stop_all(vec!["slot-2".into()], now, 1001), Err("plan_pending"));
    assert!(!organizer.expire(now + PLAN_TTL - Duration::from_millis(1)));
    assert!(organizer.expire(now + PLAN_TTL));
    assert!(organizer.projection().is_null());
    assert_eq!(organizer.plan_result(&id).unwrap()["state"], "expired");
    assert!(organizer.take_confirmed(&id, now + PLAN_TTL).is_err());
    assert!(!organizer.expire(now + PLAN_TTL));
}

#[test]
fn rejection_is_single_use_and_does_not_confirm_another_plan() {
    let now = Instant::now();
    let mut organizer = Organizer::new(Clock(77));
    let id = organizer.propose_stop_all(vec!["slot-1".into()], now, 1000).unwrap();
    assert!(organizer.reject("wrong-plan", now).is_err());
    assert_eq!(organizer.projection()["plan_id"], id);
    organizer.reject(&id, now).unwrap();
    assert_eq!(organizer.plan_result(&id).unwrap()["state"], "rejected");
    assert!(organizer.take_confirmed(&id, now).is_err());
    let next = organizer.propose_stop_all(vec!["slot-2".into()], now, 1000).unwrap();
    assert_ne!(id, next);
    assert!(organizer.take_confirmed(&id, now).is_err());
    assert_eq!(organizer.projection()["plan_id"], next);
}

#[test]
fn recording_requires_selected_root_and_measured_popouts() {
    let now = Instant::now();
    let mut organizer = Organizer::new(Clock(77));
    assert_eq!(organizer.propose_recording("Session".into(), None, &[], false, now, 0), Err("folder_root_required"));
    assert_eq!(organizer.propose_recording("../Session".into(), None, &[], true, now, 0), Err("folder_name_invalid"));
    assert_eq!(organizer.propose_recording("Session".into(), None, &[], true, now, 0), Err("no_discord_windows"));
    assert!(organizer.projection().is_null());
}

#[test]
fn auto_anchor_is_off_at_start_and_poll_rate_is_bounded_across_toggles() {
    let now = Instant::now();
    let mut organizer = Organizer::new(Clock(77));
    assert!(!organizer.auto_anchor());
    assert!(!organizer.poll_due(now));
    organizer.set_auto_anchor(true);
    assert!(organizer.poll_due(now));
    assert!(!organizer.poll_due(now));
    assert!(!organizer.poll_due(now + Duration::from_millis(999)));
    organizer.set_auto_anchor(false);
    organizer.set_auto_anchor(true);
    assert!(!organizer.poll_due(now + Duration::from_millis(999)));
    assert!(organizer.poll_due(now + Duration::from_secs(1)));
    assert!(organizer.projection().is_null());
    assert!(!Organizer::new(Clock(78)).auto_anchor());
}

#[test]
fn empty_stop_completes_only_after_confirmation_and_keeps_existing_gates() {
    let now = Instant::now();
    let mut organizer = Organizer::new(Clock(77));
    let id = organizer.propose_stop_all(vec![], now, 0).expect("empty stop must be a valid no-op plan");
    assert!(organizer.pending_empty_stop(&id, now));
    assert!(!organizer.pending_empty_stop("wrong-plan", now));
    assert!(!organizer.pending_empty_stop(&id, now + PLAN_TTL));
    assert_eq!(organizer.plan_result(&id).unwrap()["state"], "pending_confirmation");
    assert_eq!(organizer.propose_stop_all(vec![], now, 0), Err("plan_pending"));
    assert!(organizer.take_confirmed("wrong-plan", now).is_err());
    organizer.take_confirmed(&id, now).unwrap();
    assert_eq!(organizer.plan_result(&id).unwrap(), serde_json::json!({"plan_id":id,"state":"completed","windows":[]}));
    assert!(organizer.projection().is_null());
    assert!(organizer.take_confirmed(&id, now).is_err());
    let next = organizer.propose_stop_all(vec![], now, 0).unwrap();
    assert_ne!(id, next);
    assert!(organizer.expire(now + PLAN_TTL));
    assert!(organizer.take_confirmed(&next, now + PLAN_TTL).is_err());
    assert_eq!(organizer.plan_result(&next).unwrap()["state"], "expired");
    assert_eq!(organizer.propose_stop_all(vec![], now, u64::MAX), Err("unavailable"));
    assert!(organizer.projection().is_null());
    let nonempty = organizer.propose_stop_all(vec!["slot-1".into()], now, 0).unwrap();
    organizer.take_confirmed(&nonempty, now).unwrap();
    assert_eq!(organizer.plan_result(&nonempty), Err("unavailable"));
    organizer.set_window_result(&nonempty, 0, Some("slot-1".into()), "stopped").unwrap();
    assert_eq!(organizer.plan_result(&nonempty).unwrap()["state"], "completed");
}

#[test]
fn stop_plan_limits_and_result_states_are_closed() {
    let now = Instant::now();
    let mut organizer = Organizer::new(Clock(77));
    for ids in [vec!["slot-1".into(), "slot-1".into()], vec!["bad/id".into()], (0..MAX_CAPSULES + 1).map(|index| format!("slot-{index}")).collect()] {
        assert_eq!(organizer.propose_stop_all(ids, now, 0), Err("unavailable"));
    }
    let id = organizer.propose_stop_all(vec!["slot-1".into()], now, 0).unwrap();
    assert!(organizer.set_window_result(&id, 0, Some("slot-1".into()), "stopped").is_err());
    organizer.take_confirmed(&id, now).unwrap();
    assert_eq!(organizer.plan_result(&id), Err("unavailable"));
    assert!(organizer.set_window_result(&id, 0, Some("slot-1".into()), "SYNTHETIC_PRIVATE_ERROR").is_err());
    organizer.set_window_result(&id, 0, Some("slot-1".into()), "stopped").unwrap();
    assert_eq!(organizer.plan_result(&id).unwrap()["windows"][0]["state"], "stopped");
    assert_eq!(organizer.plan_result("unknown"), Err("plan_unknown"));
}

#[test]
fn expired_result_history_is_bounded_and_never_reuses_a_plan_id() {
    let now = Instant::now();
    let mut organizer = Organizer::new(Clock(77));
    let mut ids = std::collections::BTreeSet::new();
    let mut first = String::new();
    for index in 0..70_u64 {
        let at = now + Duration::from_secs(index * 61);
        let id = organizer.propose_stop_all(vec!["slot-1".into()], at, index * 61_000).unwrap();
        if first.is_empty() { first = id.clone(); }
        assert!(ids.insert(id));
    }
    assert_eq!(organizer.plan_result(&first), Err("plan_unknown"));
    assert_eq!(ids.len(), 70);
}

#[test]
fn wall_clock_overflow_never_creates_a_pending_plan() {
    let mut organizer = Organizer::new(Clock(77));
    assert_eq!(organizer.propose_stop_all(vec!["slot-1".into()], Instant::now(), u64::MAX), Err("unavailable"));
    assert!(organizer.projection().is_null());
}

#[test]
fn send_to_obs_plan_requires_ready_and_projects_empty_folder_and_per_capsule_results() {
    let now = Instant::now();
    let mut organizer = Organizer::new(Clock(77));
    assert_eq!(organizer.propose_send_to_obs(vec![("slot-1".into(), "Synthetic stream".into())], false, now, 1000), Err("obs_not_ready"));
    assert!(organizer.projection().is_null());
    for bad in [
        vec![],
        vec![("slot-1".into(), "One".into()), ("slot-1".into(), "Two".into())],
        vec![("bad/id".into(), "One".into())],
        vec![("slot-1".into(), "bad\0title".into())],
        vec![("slot-1".into(), "x".repeat(2049))],
        (0..MAX_CAPSULES + 1).map(|index| (format!("slot-{index}"), format!("Title {index}"))).collect(),
    ] {
        assert_eq!(organizer.propose_send_to_obs(bad, true, now, 1000), Err("unavailable"));
    }
    let id = organizer.propose_send_to_obs(vec![("slot-1".into(), "Synthetic stream 1".into()), ("slot-2".into(), "Synthetic stream 2".into())], true, now, 1000).unwrap();
    assert_eq!(organizer.projection(), serde_json::json!({
        "plan_id": id,
        "windows": [{"title": "Synthetic stream 1"}, {"title": "Synthetic stream 2"}],
        "folder_name": "",
        "expires_unix_ms": 61000
    }));
    assert_eq!(organizer.plan_result(&id).unwrap()["state"], "pending_confirmation");
    organizer.take_confirmed(&id, now).unwrap();
    assert!(organizer.projection().is_null());
    assert_eq!(organizer.plan_result(&id), Err("unavailable"));
    organizer.set_window_result(&id, 0, Some("slot-1".into()), "sent").unwrap();
    assert_eq!(organizer.plan_result(&id), Err("unavailable"));
    organizer.set_window_result(&id, 1, Some("slot-2".into()), "window_titles_identical").unwrap();
    let result = organizer.plan_result(&id).unwrap();
    assert_eq!(result["state"], "completed");
    assert_eq!(result["windows"][0], serde_json::json!({"title": "Synthetic stream 1", "capsule_id": "slot-1", "state": "sent"}));
    assert_eq!(result["windows"][1], serde_json::json!({"title": "Synthetic stream 2", "capsule_id": "slot-2", "state": "window_titles_identical"}));
}

/// Standalone synthetic recorder contract. The existing recorder test seam
/// launches its current executable with `--exact recorder_child_fixture`, so
/// the caller's test harness must declare that fixture at its root. The main
/// binary includes this file as a module and cannot supply that fixture name
/// through module declarations alone. A standalone harness calls this helper
/// from a #[test] and supplies the existing stdin-draining child fixture.
/// This exercises owned child pipes and the real manager/pipeline/recorder
/// code, but supplies synthetic frames: native concurrency is PENDING-LIVE.
pub(crate) fn assert_four_feed_failure_isolation() {
    use std::{path::PathBuf, sync::{Arc, atomic::{AtomicBool, AtomicU64, Ordering}}};
    use crate::{authority_journal::Journal,
        capture_frame_pool::{BorrowedFrame, FramePool, OwnedFrame, PoolLimits},
        capture_health::CaptureState,
        capture_pipeline::{CapturePipeline, FrameSource, PipelineError},
        ffmpeg_recorder::{Recorder, RecordingPlan, RecordingState, VideoProfile},
        runtime_effects::{CapsuleState, EngineError, RuntimeEffects},
        windows_graphics_capture::CaptureError,
        windows_window_registry::WindowRegistry};

    static NEXT_RUN: AtomicU64 = AtomicU64::new(0);
    struct Scratch(PathBuf);
    impl Drop for Scratch {
        fn drop(&mut self) { let _ = std::fs::remove_dir_all(&self.0); }
    }
    struct SyntheticSource {
        pool: Arc<FramePool>,
        source_time: u64,
        closed: Arc<AtomicBool>,
        stopped: Arc<AtomicBool>,
    }
    impl FrameSource for SyntheticSource {
        fn take_latest(&mut self) -> Option<OwnedFrame> {
            self.source_time += 40_000_000;
            Some(self.pool.copy_from(BorrowedFrame {
                data: &[17; 1024], width: 16, height: 16, source_stride: 64,
                source_time: Clock(self.source_time),
            }).unwrap())
        }
        fn state(&self, _: &WindowRegistry) -> CaptureState {
            if self.closed.load(Ordering::Acquire) { CaptureState::Closed } else { CaptureState::Healthy }
        }
        fn stop(&mut self) -> Result<(), CaptureError> {
            self.stopped.store(true, Ordering::Release);
            Ok(())
        }
    }

    let executor = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
    executor.block_on(async {
        let temp = Scratch(std::env::temp_dir().join(format!("cafe-organizer-synthetic-{}-{}",
            std::process::id(), NEXT_RUN.fetch_add(1, Ordering::Relaxed))));
        std::fs::create_dir(&temp.0).unwrap();
        let mut journal = Journal::open(&temp.0.join("authority.journal"), "synthetic", Clock(77)).unwrap();
        let origin = Instant::now();
        let profile = VideoProfile {
            width: 16, height: 16, fps_num: 25, fps_den: 1, queue_capacity: 4,
            write_timeout: Duration::from_secs(2), stop_timeout: Duration::from_secs(4),
            max_lateness_ns: 1_000_000_000, max_hold_ns: 1_000_000_000,
        };
        let mut engine = RuntimeEffects::<SyntheticSource>::new(MAX_CAPSULES, MAX_CAPSULES * 2).unwrap();
        let mut signals = Vec::new();
        let mut child_ids = std::collections::BTreeSet::new();
        for index in 1..=4 {
            let tag = format!("slot-{index}");
            let recorder = Recorder::test_with_child("drain", &temp.0, RecordingPlan {
                media_id: format!("media-{tag}"), intent_event: format!("intent-{tag}"),
                publish_event: format!("published-{tag}"), staged_name: format!("{tag}.partial.mkv"),
                published_name: format!("{tag}.mkv"),
            }, profile, &mut journal, origin).unwrap();
            assert!(child_ids.insert(recorder.pid()), "each synthetic feed owns a distinct child");
            let closed = Arc::new(AtomicBool::new(false));
            let stopped = Arc::new(AtomicBool::new(false));
            let source = SyntheticSource {
                pool: FramePool::new(PoolLimits { slots: 4, max_frame_bytes: 1024, max_width: 16, max_height: 16 }).unwrap(),
                source_time: 0, closed: closed.clone(), stopped: stopped.clone(),
            };
            engine.attach_capsule(tag, vec![(format!("feed-{index}"), CapturePipeline::new(source, recorder))]).await.unwrap();
            signals.push((closed, stopped));
        }

        let registry = WindowRegistry::new(Clock(77));
        engine.tick_all(&registry, origin).await.unwrap();
        let active = engine.status();
        assert_eq!(active.len(), 4);
        for capsule in active {
            assert_eq!(capsule.state, CapsuleState::Running);
            assert_eq!(capsule.reason, None);
            assert_eq!(capsule.feeds.len(), 1);
            assert_eq!(capsule.feeds[0].state, RecordingState::Recording);
            assert_eq!(capsule.feeds[0].counters.written_frames, 1);
            assert_eq!(capsule.feeds[0].counters.written_bytes, 1024);
        }

        // Close only slot-2 after all four children have accepted a real write.
        signals[1].0.store(true, Ordering::Release);
        assert_eq!(engine.tick_all(&registry, origin + Duration::from_millis(40)).await,
            Err(EngineError::Tick(PipelineError::Source)));
        for (index, capsule) in engine.status().iter().enumerate() {
            assert_eq!(capsule.capsule_id, format!("slot-{}", index + 1));
            if index == 1 {
                assert_eq!(capsule.state, CapsuleState::Interrupted);
                assert_eq!(capsule.reason, Some("capture_source_unavailable"));
                assert_eq!(capsule.feeds[0].counters.written_frames, 1);
                assert!(signals[index].1.load(Ordering::Acquire));
            } else {
                assert_eq!(capsule.state, CapsuleState::Running);
                assert_eq!(capsule.reason, None);
                assert_eq!(capsule.feeds[0].state, RecordingState::Recording);
                assert_eq!(capsule.feeds[0].counters.written_frames, 2);
                assert!(!signals[index].1.load(Ordering::Acquire));
            }
        }
        // A later tick still advances all three survivors, not just their state.
        engine.tick_all(&registry, origin + Duration::from_millis(80)).await.unwrap();
        for (index, capsule) in engine.status().iter().enumerate() {
            assert_eq!(capsule.feeds[0].counters.written_frames, if index == 1 { 1 } else { 3 });
            assert_eq!(capsule.feeds[0].counters.written_bytes, if index == 1 { 1024 } else { 3072 });
        }
        assert_eq!(journal.records().len(), 4, "only the four durable media intents exist");
        for index in 1..=4 { assert!(!temp.0.join(format!("slot-{index}.mkv")).exists()); }
        engine.abort_all().await.unwrap();
        assert!(signals.iter().all(|(_, stopped)| stopped.load(Ordering::Acquire)));
        assert_eq!(journal.records().len(), 4, "abort must never publish media");
    });
}
}
