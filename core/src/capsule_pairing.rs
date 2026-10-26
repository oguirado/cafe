use std::collections::BTreeMap;
use serde::Serialize;
use crate::capsule_registry::{Clock, Role, Registry, Envelope, Command, Outcome, SCHEMA};
use crate::windows_window_registry::{WindowRegistry, WindowHandle, WindowView, WindowError};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct Crop {
    pub x: u32,
    pub y: u32,
    pub width: u32,
    pub height: u32,
}

// Trusted Rust boundary ONLY — do NOT derive or implement Deserialize!
#[derive(Clone, Debug)]
pub struct PairRequest {
    pub request_id: String,
    pub authority_epoch: Clock,
    pub expected_revision: Clock,
    pub capsule_id: String,
    pub person_label: String,
    pub feed_id: String,
    pub role: Role,
    pub window: WindowHandle,
    pub window_generation: Clock,
    pub layout_revision: Clock,
    pub crop: Crop,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Binding {
    pub capsule_id: String,
    pub person_label: String,
    pub feed_id: String,
    pub role: Role,
    pub window: WindowHandle,
    pub window_generation: Clock,
    pub layout_revision: Clock,
    pub crop: Crop,
    pub feed_generation: Clock,
}

pub struct PairingBook {
    bindings: BTreeMap<String, Binding>,
}

impl PairingBook {
    pub fn new() -> Self {
        Self {
            bindings: BTreeMap::new(),
        }
    }

    pub fn pair(
        &mut self,
        authority: &mut Registry,
        windows: &WindowRegistry,
        request: PairRequest,
    ) -> Result<Binding, PairError> {
        checked_pair(authority, windows, request, &mut self.bindings)
    }

    pub fn bindings(&self) -> Vec<Binding> {
        self.bindings.values().cloned().collect()
    }

    pub fn current(
        &self,
        feed_id: &str,
        authority: &Registry,
        windows: &WindowRegistry,
    ) -> Result<Binding, PairError> {
        let binding = self
            .bindings
            .get(feed_id)
            .ok_or(PairError::FeedUnavailable)?;
        checked_binding(authority, windows, binding)
    }

    pub fn pair_by_executable(&self, _executable: &str) -> Result<(), PairError> {
        Err(PairError::ExecutableOnlyForbidden)
    }
}

// BEGIN CODEX PAIR ADMISSION — security-owned, preserved byte-exact.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PairError { Window(WindowError), WrongPerson, InvalidCrop, StaleCrop, UnknownCapsule, AlreadyPaired, FeedUnavailable, AuthorityRejected(String), GenerationExhausted, ExecutableOnlyForbidden }
impl From<WindowError> for PairError { fn from(e: WindowError) -> Self { Self::Window(e) } }

fn checked_crop(crop: Crop, view: &WindowView, layout: Clock) -> Result<(), PairError> {
    if layout != view.layout_revision { return Err(PairError::StaleCrop); }
    if crop.width == 0 || crop.height == 0 || crop.x.checked_add(crop.width).is_none_or(|x| x > view.rect.width)
        || crop.y.checked_add(crop.height).is_none_or(|y| y > view.rect.height) { return Err(PairError::InvalidCrop); }
    Ok(())
}
fn checked_pair(authority: &mut Registry, windows: &WindowRegistry, request: PairRequest, bindings: &mut BTreeMap<String, Binding>) -> Result<Binding, PairError> {
    let before = authority.snapshot();
    let capsule = before.capsules.get(&request.capsule_id).ok_or(PairError::UnknownCapsule)?;
    if request.person_label != capsule.person_label { return Err(PairError::WrongPerson); }
    let view = windows.resolve(&request.window, request.window_generation)?;
    checked_crop(request.crop, &view, request.layout_revision)?;
    if bindings.contains_key(&request.feed_id) { return Err(PairError::AlreadyPaired); }
    let feed_generation = Clock(before.highest_generation.0.checked_add(1).ok_or(PairError::GenerationExhausted)?);
    let binding = Binding { capsule_id: request.capsule_id.clone(), person_label: request.person_label.clone(), feed_id: request.feed_id.clone(),
        role: request.role, window: request.window.clone(), window_generation: request.window_generation,
        layout_revision: request.layout_revision, crop: request.crop, feed_generation };
    let reply = authority.apply(Envelope { schema: SCHEMA.into(), kind: "intent".into(), request_id: request.request_id,
        authority_epoch: request.authority_epoch, expected_revision: request.expected_revision,
        body: Command::AddFeed { capsule_id: request.capsule_id, feed_id: request.feed_id.clone(), role: request.role, generation: feed_generation } });
    if reply.state != Outcome::Admitted { return Err(PairError::AuthorityRejected(reply.error.unwrap_or_else(|| "rejected".into()))); }
    bindings.insert(request.feed_id, binding.clone()); Ok(binding)
}
fn checked_binding(authority: &Registry, windows: &WindowRegistry, binding: &Binding) -> Result<Binding, PairError> {
    let state = authority.snapshot();
    let capsule = state.capsules.get(&binding.capsule_id).ok_or(PairError::FeedUnavailable)?;
    let feed = capsule.feeds.get(&binding.feed_id).ok_or(PairError::FeedUnavailable)?;
    if capsule.person_label != binding.person_label || feed.generation != binding.feed_generation || feed.role != binding.role { return Err(PairError::FeedUnavailable); }
    let view = windows.resolve(&binding.window, binding.window_generation)?;
    checked_crop(binding.crop, &view, binding.layout_revision)?; Ok(binding.clone())
}
// END CODEX PAIR ADMISSION
