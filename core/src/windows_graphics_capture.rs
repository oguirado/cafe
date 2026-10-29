//! Direct Windows Graphics Capture source with bounded recorder-owned pool (P10).
//!
//! Linux is an explicit unsupported stub. On Windows, a WGC session copies
//! frames into one recorder-owned staging texture and a bounded [`FramePool`],
//! delegating every health transition to [`CaptureHealth`]. No raw native
//! handle ever leaves the registry: the producer holds only the opaque
//! [`CapturePermit`] capability.
//!
//! [`FramePool`]: crate::capture_frame_pool::FramePool
//! [`CaptureHealth`]: crate::capture_health::CaptureHealth
//! [`CapturePermit`]: crate::windows_window_registry::CapturePermit

use crate::capsule_registry::Clock;
use crate::capture_frame_pool::{OwnedFrame, PoolError, PoolLimits};
use crate::capture_health::{BorderStatus, CaptureState, HealthError, HealthPolicy};
use crate::windows_window_registry::{NativeAdapter, WindowError, WindowHandle, WindowRegistry};

#[cfg(windows)]
use crate::capture_frame_pool::BorrowedFrame;
#[cfg(windows)]
use crate::capture_health::{CaptureEvent, CaptureHealth};
#[cfg(windows)]
use crate::windows_window_registry::CapturePermit;

/// Native capture availability probe result.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NativeSupport {
    Supported,
    Unsupported,
    Unavailable,
}

/// Caller-supplied capture profile. All sizes and budgets are required
/// caller input; there are no production defaults here.
#[derive(Clone, Copy, Debug)]
pub struct CaptureConfig {
    pub width: u32,
    pub height: u32,
    pub pool: PoolLimits,
    pub health: HealthPolicy,
    pub borderless: bool,
    pub access_timeout_ms: u64,
}

/// Observed-size startup. Only resource/health budgets are caller-supplied.
/// Dimensions come from the identity-checked native item, never window bounds.
#[derive(Clone, Copy, Debug)]
pub struct ObservedCaptureConfig {
    pub pool: PoolLimits,
    pub health: HealthPolicy,
    pub borderless: bool,
    pub access_timeout_ms: u64,
}
impl ObservedCaptureConfig {
    /// Reject nonpositive/oversized observed sizes before native allocation.
    pub fn resolve_size(self,width:i32,height:i32)->Result<CaptureConfig,CaptureError> {
        let width=u32::try_from(width).map_err(|_|CaptureError::Geometry)?;
        let height=u32::try_from(height).map_err(|_|CaptureError::Geometry)?;
        let bytes=(width as usize).checked_mul(height as usize).and_then(|n|n.checked_mul(4)).ok_or(CaptureError::Geometry)?;
        if width==0 || height==0 || width>self.pool.max_width || height>self.pool.max_height || bytes>self.pool.max_frame_bytes || self.pool.slots==0 || self.health.stall_budget.0==0 || self.access_timeout_ms==0 {return Err(CaptureError::Geometry);}
        Ok(CaptureConfig{width,height,pool:self.pool,health:self.health,borderless:self.borderless,access_timeout_ms:self.access_timeout_ms})
    }
}

/// Typed capture failure. Sanitized codes only; no native identifiers,
/// pointers, or OS-localized strings.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CaptureError {
    Unsupported,
    Unavailable,
    Window(WindowError),
    Geometry,
    Device,
    Pool(PoolError),
    Health(HealthError),
    Border,
    Clock,
    Timeout,
}

impl From<PoolError> for CaptureError {
    fn from(error: PoolError) -> Self {
        CaptureError::Pool(error)
    }
}

impl From<HealthError> for CaptureError {
    fn from(error: HealthError) -> Self {
        CaptureError::Health(error)
    }
}

impl From<WindowError> for CaptureError {
    fn from(error: WindowError) -> Self {
        CaptureError::Window(error)
    }
}

#[cfg(not(windows))]
pub fn native_support() -> NativeSupport {
    NativeSupport::Unsupported
}

#[cfg(not(windows))]
pub struct NativeCapture {
    _unused: (),
}

#[cfg(not(windows))]
impl NativeCapture {
    pub async fn start_observed(_registry:&WindowRegistry,_adapter:&mut NativeAdapter,_handle:WindowHandle,_generation:Clock,_config:ObservedCaptureConfig)->Result<Self,CaptureError> {
        Err(CaptureError::Unsupported)
    }
    pub async fn start(
        _registry: &WindowRegistry,
        _adapter: &mut NativeAdapter,
        _handle: WindowHandle,
        _generation: Clock,
        _config: CaptureConfig,
    ) -> Result<Self, CaptureError> {
        Err(CaptureError::Unsupported)
    }

    pub fn take_latest(&self) -> Option<OwnedFrame> {
        None
    }

    pub fn poll_state(&self, _registry: &WindowRegistry) -> CaptureState {
        CaptureState::Closed
    }

    pub fn border_status(&self) -> BorderStatus {
        BorderStatus::Unsupported
    }

    pub fn stop(&mut self) -> Result<(), CaptureError> {
        Ok(())
    }
}

#[cfg(windows)]
pub fn native_support() -> NativeSupport {
    match windows::Graphics::Capture::GraphicsCaptureSession::IsSupported() {
        Ok(true) => NativeSupport::Supported,
        Ok(false) => NativeSupport::Unsupported,
        Err(_) => NativeSupport::Unavailable,
    }
}

/// Owning-thread WinRT apartment guard. `RoUninitialize` runs only when
/// `RoInitialize` succeeded on this thread; initialization failure (including
/// a conflicting apartment mode) fails closed at setup.
#[cfg(windows)]
struct ApartmentGuard {
    active: bool,
}

#[cfg(windows)]
impl ApartmentGuard {
    fn initialize() -> Result<Self, CaptureError> {
        use windows::Win32::System::WinRT::{RO_INIT_MULTITHREADED, RoInitialize};
        unsafe {
            RoInitialize(RO_INIT_MULTITHREADED).map_err(|_| CaptureError::Unavailable)?;
        }
        Ok(Self { active: true })
    }
}

#[cfg(windows)]
impl Drop for ApartmentGuard {
    fn drop(&mut self) {
        if self.active {
            unsafe {
                windows::Win32::System::WinRT::RoUninitialize();
            }
        }
    }
}

/// RAII guard closing a borrowed capture frame on every callback path.
#[cfg(windows)]
struct FrameCloseGuard {
    frame: windows::Graphics::Capture::Direct3D11CaptureFrame,
    closed: bool,
}

#[cfg(windows)]
impl FrameCloseGuard {
    fn new(frame: &windows::Graphics::Capture::Direct3D11CaptureFrame) -> Self {
        Self { frame: frame.clone(), closed: false }
    }
}

#[cfg(windows)]
impl Drop for FrameCloseGuard {
    fn drop(&mut self) {
        if !self.closed { let _ = self.frame.Close(); }
    }
}
#[cfg(windows)]
impl FrameCloseGuard {
    fn finish(mut self) -> Result<(), CaptureError> {
        self.frame.Close().map_err(|_| CaptureError::Device)?;
        self.closed = true;
        Ok(())
    }
}

/// RAII guard unmapping the staging texture on every callback path.
#[cfg(windows)]
struct MapGuard {
    context: windows::Win32::Graphics::Direct3D11::ID3D11DeviceContext,
    resource: windows::Win32::Graphics::Direct3D11::ID3D11Resource,
}

#[cfg(windows)]
impl Drop for MapGuard {
    fn drop(&mut self) {
        unsafe {
            self.context.Unmap(&self.resource, 0);
        }
    }
}

/// Callback-shared state: serialized immediate context, health, latest slot,
/// opaque permit, and recorder-owned staging resource. Never holds WGC event
/// sources, so handler closures cannot form reference cycles.
#[cfg(windows)]
struct SharedState {
    health: CaptureHealth,
    latest: Option<OwnedFrame>,
    pool: std::sync::Arc<crate::capture_frame_pool::FramePool>,
    permit: CapturePermit,
    generation: u64,
    width: u32,
    height: u32,
    device: windows::Win32::Graphics::Direct3D11::ID3D11Device,
    context: windows::Win32::Graphics::Direct3D11::ID3D11DeviceContext,
    staging: windows::Win32::Graphics::Direct3D11::ID3D11Resource,
    last_observation: Clock,
}

#[cfg(windows)]
fn lock_shared(
    shared: &std::sync::Arc<std::sync::Mutex<SharedState>>,
) -> std::sync::MutexGuard<'_, SharedState> {
    shared.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// Actual monotonic observation clock via QPC, scaled with checked `u128`
/// arithmetic into 100-ns [`Clock`] units. Never copies source timestamps.
#[cfg(windows)]
fn qpc_now() -> Result<Clock, CaptureError> {
    use windows::Win32::System::Performance::{QueryPerformanceCounter, QueryPerformanceFrequency};
    let mut counter: i64 = 0;
    let mut frequency: i64 = 0;
    unsafe {
        QueryPerformanceCounter(&mut counter).map_err(|_| CaptureError::Clock)?;
        QueryPerformanceFrequency(&mut frequency).map_err(|_| CaptureError::Clock)?;
    }
    if counter < 0 || frequency <= 0 {
        return Err(CaptureError::Clock);
    }
    let ticks = (counter as u128) * 10_000_000 / (frequency as u128);
    let ticks = u64::try_from(ticks).map_err(|_| CaptureError::Clock)?;
    Ok(Clock(ticks))
}

/// True only for genuine device-removed HRESULTs, never for transient
/// empty-queue polls.
#[cfg(windows)]
fn device_removed_reason(device: &windows::Win32::Graphics::Direct3D11::ID3D11Device) -> bool {
    // Any failing removal-reason probe makes this device unusable. Empty queue
    // polls are classified separately and do not produce a device failure.
    unsafe { device.GetDeviceRemovedReason() }.is_err()
}

/// Identity-failure mapping shared by permit checks and registry resolves:
/// unavailable reads as minimized, stale reads as closed, anything else
/// reads as a source failure. Never reports healthy on a failed check.
#[cfg(windows)]
fn report_identity_failure(health: &mut CaptureHealth, error: WindowError) {
    match error {
        WindowError::Unavailable => health.event(CaptureEvent::Minimized),
        WindowError::StaleWindow => health.event(CaptureEvent::Closed),
        _ => health.event(CaptureEvent::SourceFailed),
    }
}

#[cfg(windows)]
fn note_device_or_source(state: &mut SharedState) {
    if device_removed_reason(&state.device) {
        state.health.event(CaptureEvent::DeviceLost);
    } else {
        state.health.event(CaptureEvent::SourceFailed);
    }
}

#[cfg(windows)]
fn frame_surface_texture(
    frame: &windows::Graphics::Capture::Direct3D11CaptureFrame,
) -> windows::core::Result<windows::Win32::Graphics::Direct3D11::ID3D11Texture2D> {
    use windows::Win32::System::WinRT::Direct3D11::IDirect3DDxgiInterfaceAccess;
    use windows::core::Interface;
    let surface = frame.Surface()?;
    let access: IDirect3DDxgiInterfaceAccess = surface.cast()?;
    unsafe { access.GetInterface() }
}

/// Frame-arrival processing: validate, copy into the bounded pool, release
/// every borrowed native resource, then admit. No borrowed frame, surface,
/// texture, or mapped pointer escapes this function.
#[cfg(windows)]
fn handle_frame(
    shared: &std::sync::Arc<std::sync::Mutex<SharedState>>,
    frame: &windows::Graphics::Capture::Direct3D11CaptureFrame,
) {
    use windows::Win32::Graphics::Direct3D11::{D3D11_MAP_READ, D3D11_MAPPED_SUBRESOURCE};
    use windows::Win32::Graphics::Dxgi::Common::DXGI_FORMAT_B8G8R8A8_UNORM;
    use windows::core::Interface;

    let close_guard = FrameCloseGuard::new(frame);
    let mut state = lock_shared(shared);
    if device_removed_reason(&state.device) {
        state.latest.take(); state.health.event(CaptureEvent::DeviceLost); return;
    }

    if let Err(error) = state.permit.check() {
        report_identity_failure(&mut state.health, error);
        return;
    }

    let size = match frame.ContentSize() {
        Ok(size) => size,
        Err(_) => {
            note_device_or_source(&mut state);
            return;
        }
    };
    if size.Width != state.width as i32 || size.Height != state.height as i32 {
        state.health.event(CaptureEvent::Resized);
        return;
    }

    let duration = match frame.SystemRelativeTime() {
        Ok(span) => span.Duration,
        Err(_) => {
            note_device_or_source(&mut state);
            return;
        }
    };
    if duration < 0 {
        state.health.event(CaptureEvent::SourceFailed);
        return;
    }
    let source_time = Clock(duration as u64);

    let observed_at = match qpc_now() {
        Ok(now) => now,
        Err(_) => {
            state.health.event(CaptureEvent::SourceFailed);
            return;
        }
    };

    let texture = match frame_surface_texture(frame) {
        Ok(texture) => texture,
        Err(_) => {
            note_device_or_source(&mut state);
            return;
        }
    };
    let mut desc: windows::Win32::Graphics::Direct3D11::D3D11_TEXTURE2D_DESC =
        Default::default();
    unsafe {
        texture.GetDesc(&mut desc);
    }
    if desc.Format != DXGI_FORMAT_B8G8R8A8_UNORM || desc.SampleDesc.Count != 1 {
        state.health.event(CaptureEvent::SourceFailed);
        return;
    }
    if desc.Width != state.width || desc.Height != state.height {
        state.health.event(CaptureEvent::Resized);
        return;
    }
    let source: windows::Win32::Graphics::Direct3D11::ID3D11Resource = match texture.cast() {
        Ok(resource) => resource,
        Err(_) => {
            state.health.event(CaptureEvent::SourceFailed);
            return;
        }
    };
    unsafe {
        state.context.CopyResource(&state.staging, &source);
    }

    let mut mapped = D3D11_MAPPED_SUBRESOURCE {
        pData: std::ptr::null_mut(),
        RowPitch: 0,
        DepthPitch: 0,
    };
    if unsafe {
        state.context.Map(&state.staging, 0, D3D11_MAP_READ, 0, Some(&mut mapped))
    }
    .is_err()
    {
        note_device_or_source(&mut state);
        return;
    }
    let map_guard = MapGuard {
        context: state.context.clone(),
        resource: state.staging.clone(),
    };

    let row_pitch = mapped.RowPitch as usize;
    let height = state.height as usize;
    let map_len = match row_pitch.checked_mul(height) {
        Some(len) => len,
        None => {
            state.health.event(CaptureEvent::SourceFailed);
            return;
        }
    };
    if mapped.pData.is_null() || row_pitch == 0 || map_len == 0 {
        state.health.event(CaptureEvent::SourceFailed);
        return;
    }
    let data: &[u8] =
        unsafe { std::slice::from_raw_parts(mapped.pData as *const u8, map_len) };

    // Free the superseded latest lease before copying so a single-slot pool
    // cannot deadlock on its own replacement.
    state.latest.take();
    let owned = match state.pool.copy_from(BorrowedFrame {
        data,
        width: state.width,
        height: state.height,
        source_stride: row_pitch,
        source_time,
    }) {
        Ok(owned) => owned,
        Err(PoolError::Exhausted) => {
            state.health.event(CaptureEvent::Backpressure);
            return;
        }
        Err(_) => {
            state.health.event(CaptureEvent::SourceFailed);
            return;
        }
    };

    // Release every borrowed native resource before admission or publish.
    drop(map_guard);
    if close_guard.finish().is_err() {
        state.health.event(CaptureEvent::SourceFailed);
        return;
    }

    if let Err(error) = state.permit.check() {
        report_identity_failure(&mut state.health, error);
        return;
    }
    let generation = state.generation;
    let width = state.width;
    let height = state.height;
    if state
        .health
        .admit_frame(generation, width, height, source_time, observed_at)
        .is_ok()
    {
        if observed_at.0 > state.last_observation.0 {
            state.last_observation = observed_at;
        }
        state.latest = Some(owned);
    }
}

/// `AsyncStatus` wire values (`windows-future-0.3.2/src/bindings.rs:967-970`).
/// The async types are not re-exported through the approved `windows`
/// feature set, so the status is read through its public `.0` field.
#[cfg(windows)]
const ASYNC_COMPLETED: i32 = 1;
#[cfg(windows)]
const ASYNC_CANCELED: i32 = 2;
#[cfg(windows)]
const ASYNC_ERROR: i32 = 3;

/// Internal access-request poll quantum. The caller-supplied
/// `access_timeout_ms` remains the only bounding budget; this quantum only
/// sets how promptly a grant is noticed. Not a production measurement.
#[cfg(windows)]
const ACCESS_POLL_QUANTUM: std::time::Duration = std::time::Duration::from_millis(5);

/// Border qualification. `borderless == false` leaves the OS recording
/// indicator untouched. `borderless == true` requires API presence, an
/// explicit OS access grant within the caller timeout, and a verified
/// setter round-trip; anything else fails closed.
///
/// The wait is bounded by an approved Tokio deadline: the async operation
/// exposes no `.await` under the approved feature set (`IntoFuture` needs
/// the unapproved `windows/std` feature) and no blocking `.get()` exists
/// in `windows-future 0.3.2`, so completion is polled with Tokio sleeps
/// that never wake past the caller deadline.
#[cfg(windows)]
async fn qualify_border(
    session: &windows::Graphics::Capture::GraphicsCaptureSession,
    borderless: bool,
    access_timeout_ms: u64,
) -> Result<BorderStatus, CaptureError> {
    use windows::Graphics::Capture::{
        GraphicsCaptureAccess, GraphicsCaptureAccessKind, IGraphicsCaptureSession3,
    };
    use windows::Security::Authorization::AppCapabilityAccess::AppCapabilityAccessStatus;
    use windows::core::Interface;

    if !borderless {
        return Ok(BorderStatus::Required);
    }
    session.cast::<IGraphicsCaptureSession3>().map_err(|_| CaptureError::Border)?;
    let request =
        GraphicsCaptureAccess::RequestAccessAsync(GraphicsCaptureAccessKind::Borderless)
            .map_err(|_| CaptureError::Border)?;
    let deadline =
        tokio::time::Instant::now() + std::time::Duration::from_millis(access_timeout_ms);
    loop {
        let status = request.Status().map_err(|_| CaptureError::Border)?;
        if status.0 == ASYNC_COMPLETED {
            break;
        }
        if status.0 == ASYNC_CANCELED || status.0 == ASYNC_ERROR {
            return Err(CaptureError::Border);
        }
        let now = tokio::time::Instant::now();
        if now >= deadline {
            let _ = request.Cancel();
            return Err(CaptureError::Timeout);
        }
        tokio::time::sleep_until(deadline.min(now + ACCESS_POLL_QUANTUM)).await;
    }
    let status = request.GetResults().map_err(|_| CaptureError::Border)?;
    if status != AppCapabilityAccessStatus::Allowed {
        return Err(CaptureError::Border);
    }
    session.SetIsBorderRequired(false).map_err(|_| CaptureError::Border)?;
    match session.IsBorderRequired() {
        Ok(false) => Ok(BorderStatus::Available),
        _ => Err(CaptureError::Border),
    }
}

#[cfg(windows)]
pub struct NativeCapture {
    shared: std::sync::Arc<std::sync::Mutex<SharedState>>,
    session: Option<windows::Graphics::Capture::GraphicsCaptureSession>,
    wgc_pool: Option<windows::Graphics::Capture::Direct3D11CaptureFramePool>,
    item: Option<windows::Graphics::Capture::GraphicsCaptureItem>,
    frame_token: Option<i64>,
    closed_token: Option<i64>,
    handle: WindowHandle,
    generation: Clock,
    border: BorderStatus,
    apartment: ApartmentGuard,
    _thread: std::marker::PhantomData<std::rc::Rc<()>>,
}

#[cfg(windows)]
impl NativeCapture {
    /// Sample checked native geometry; existing start obtains its own fresh
    /// identity permit and checks the size again. A race rejects, never guesses.
    pub async fn start_observed(registry:&WindowRegistry,adapter:&mut NativeAdapter,handle:WindowHandle,generation:Clock,config:ObservedCaptureConfig)->Result<Self,CaptureError> {
        let observed={
            let _apartment=ApartmentGuard::initialize()?;
            let permit=registry.capture_permit(&handle,generation,adapter)?;
            let size=permit.item().Size().map_err(|_|CaptureError::Device)?;
            config.resolve_size(size.Width,size.Height)?
        };
        Self::start(registry,adapter,handle,generation,observed).await
    }
    pub async fn start(
        registry: &WindowRegistry,
        adapter: &mut NativeAdapter,
        handle: WindowHandle,
        generation: Clock,
        config: CaptureConfig,
    ) -> Result<Self, CaptureError> {
        use windows::Graphics::Capture::{Direct3D11CaptureFramePool, GraphicsCaptureSession};
        use windows::Graphics::DirectX::Direct3D11::IDirect3DDevice;
        use windows::Graphics::DirectX::DirectXPixelFormat;
        use windows::Graphics::SizeInt32;
        use windows::Win32::Foundation::HMODULE;
        use windows::Win32::Graphics::Direct3D::D3D_DRIVER_TYPE_HARDWARE;
        use windows::Win32::Graphics::Direct3D11::{
            D3D11_CPU_ACCESS_READ, D3D11_SDK_VERSION, D3D11_TEXTURE2D_DESC, D3D11_USAGE_STAGING,
            D3D11CreateDevice, D3D11_CREATE_DEVICE_BGRA_SUPPORT, ID3D11Device, ID3D11DeviceContext,
            ID3D11Resource,
        };
        use windows::Win32::Graphics::Dxgi::Common::{DXGI_FORMAT_B8G8R8A8_UNORM, DXGI_SAMPLE_DESC};
        use windows::Win32::Graphics::Dxgi::IDXGIDevice;
        use windows::Win32::System::WinRT::Direct3D11::CreateDirect3D11DeviceFromDXGIDevice;
        use windows::core::Interface;

        if config.width == 0 || config.height == 0 {
            return Err(CaptureError::Geometry);
        }
        if config.width > i32::MAX as u32 || config.height > i32::MAX as u32 {
            return Err(CaptureError::Geometry);
        }
        let pool = crate::capture_frame_pool::FramePool::new(config.pool)?;
        let apartment = ApartmentGuard::initialize()?;
        match GraphicsCaptureSession::IsSupported() {
            Ok(true) => {}
            Ok(false) => return Err(CaptureError::Unsupported),
            Err(_) => return Err(CaptureError::Unavailable),
        }

        let mut device_out: Option<ID3D11Device> = None;
        let mut context_out: Option<ID3D11DeviceContext> = None;
        unsafe {
            D3D11CreateDevice(
                None,
                D3D_DRIVER_TYPE_HARDWARE,
                HMODULE::default(),
                D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                None,
                D3D11_SDK_VERSION,
                Some(&mut device_out),
                None,
                Some(&mut context_out),
            )
            .map_err(|error| if error.code() == windows::Win32::Graphics::Dxgi::DXGI_ERROR_UNSUPPORTED { CaptureError::Unsupported } else { CaptureError::Device })?;
        }
        let device = device_out.ok_or(CaptureError::Device)?;
        let context = context_out.ok_or(CaptureError::Device)?;

        let direct_device: IDirect3DDevice =
            (|| -> windows::core::Result<IDirect3DDevice> {
                let dxgi: IDXGIDevice = device.cast()?;
                unsafe { CreateDirect3D11DeviceFromDXGIDevice(&dxgi) }?.cast()
            })()
            .map_err(|_| CaptureError::Device)?;

        let permit = registry.capture_permit(&handle, generation, adapter)?;
        let item = permit.item();
        let initial = item.Size().map_err(|_| CaptureError::Device)?;
        if initial.Width != config.width as i32 || initial.Height != config.height as i32 {
            return Err(CaptureError::Geometry);
        }

        let buffers = config.pool.slots.min(i32::MAX as usize).max(1) as i32;
        let size = SizeInt32 {
            Width: config.width as i32,
            Height: config.height as i32,
        };
        let wgc_pool = Direct3D11CaptureFramePool::CreateFreeThreaded(
            &direct_device,
            DirectXPixelFormat::B8G8R8A8UIntNormalized,
            buffers,
            size,
        )
        .map_err(|_| CaptureError::Device)?;
        let session = wgc_pool.CreateCaptureSession(&item).map_err(|_| CaptureError::Device)?;

        let border = qualify_border(&session, config.borderless, config.access_timeout_ms).await?;
        let health = CaptureHealth::new(
            config.health,
            generation.0,
            config.width,
            config.height,
            border,
        )?;

        let staging_desc = D3D11_TEXTURE2D_DESC {
            Width: config.width,
            Height: config.height,
            MipLevels: 1,
            ArraySize: 1,
            Format: DXGI_FORMAT_B8G8R8A8_UNORM,
            SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
            Usage: D3D11_USAGE_STAGING,
            BindFlags: 0,
            CPUAccessFlags: D3D11_CPU_ACCESS_READ.0 as u32,
            MiscFlags: 0,
        };
        let mut staging_out: Option<windows::Win32::Graphics::Direct3D11::ID3D11Texture2D> = None;
        unsafe {
            device
                .CreateTexture2D(&staging_desc, None, Some(&mut staging_out))
                .map_err(|_| CaptureError::Device)?;
        }
        let staging_texture = staging_out.ok_or(CaptureError::Device)?;
        let staging: ID3D11Resource = staging_texture.cast().map_err(|_| CaptureError::Device)?;

        let observed = qpc_now()?;
        let shared = std::sync::Arc::new(std::sync::Mutex::new(SharedState {
            health,
            latest: None,
            pool,
            permit,
            generation: generation.0,
            width: config.width,
            height: config.height,
            device,
            context,
            staging,
            last_observation: observed,
        }));

        let frame_shared = std::sync::Arc::clone(&shared);
        let frame_handler = windows::Foundation::TypedEventHandler::<
            Direct3D11CaptureFramePool,
            windows::core::IInspectable,
        >::new(move |sender, _args| {
            let pool = match sender.as_ref() {
                Some(pool) => pool,
                None => return Ok(()),
            };
            match pool.TryGetNextFrame() {
                Ok(frame) => handle_frame(&frame_shared, &frame),
                Err(_) => {
                    let mut state = lock_shared(&frame_shared);
                    if device_removed_reason(&state.device) {
                        state.health.event(CaptureEvent::DeviceLost);
                    }
                }
            }
            Ok(())
        });
        let frame_token = wgc_pool.FrameArrived(&frame_handler).map_err(|_| CaptureError::Device)?;

        let closed_shared = std::sync::Arc::clone(&shared);
        let closed_handler = windows::Foundation::TypedEventHandler::<
            windows::Graphics::Capture::GraphicsCaptureItem,
            windows::core::IInspectable,
        >::new(move |_sender, _args| {
            let mut state = lock_shared(&closed_shared);
            state.latest.take();
            state.health.event(CaptureEvent::Closed);
            Ok(())
        });
        let closed_token = match item.Closed(&closed_handler) {
            Ok(token) => token,
            Err(_) => {
                let _ = wgc_pool.RemoveFrameArrived(frame_token);
                return Err(CaptureError::Device);
            }
        };

        if session.StartCapture().is_err() {
            let _ = wgc_pool.RemoveFrameArrived(frame_token);
            let _ = item.RemoveClosed(closed_token);
            return Err(CaptureError::Device);
        }

        Ok(Self {
            shared,
            session: Some(session),
            wgc_pool: Some(wgc_pool),
            item: Some(item),
            frame_token: Some(frame_token),
            closed_token: Some(closed_token),
            handle,
            generation,
            border,
            apartment,
            _thread: std::marker::PhantomData,
        })
    }

    /// Takes the latest admitted frame, if the live permit check still holds.
    /// A failed check drops cached pixels and is never reported as healthy.
    pub fn take_latest(&self) -> Option<OwnedFrame> {
        let mut state = lock_shared(&self.shared);
        if device_removed_reason(&state.device) {
            state.latest.take(); state.health.event(CaptureEvent::DeviceLost);
        }
        if let Err(error) = state.permit.check() {
            state.latest.take();
            report_identity_failure(&mut state.health, error);
            return None;
        }
        state.latest.take()
    }

    /// Rechecks the live permit, the registry identity, and health against an
    /// actual QPC timestamp. Never reports healthy from a stale cache.
    pub fn poll_state(&self, registry: &WindowRegistry) -> CaptureState {
        let mut state = lock_shared(&self.shared);
        if device_removed_reason(&state.device) {
            state.latest.take(); state.health.event(CaptureEvent::DeviceLost);
        }
        if let Err(error) = state.permit.check() {
            report_identity_failure(&mut state.health, error);
        }
        if let Err(error) = registry.resolve(&self.handle, self.generation) {
            report_identity_failure(&mut state.health, error);
        }
        let now = match qpc_now() {
            Ok(now) => now,
            Err(_) => {
                state.health.event(CaptureEvent::SourceFailed);
                state.last_observation
            }
        };
        if now.0 > state.last_observation.0 {
            state.last_observation = now;
        }
        state.health.status(now)
    }

    pub fn border_status(&self) -> BorderStatus {
        self.border
    }

    /// Unregisters handlers and closes native resources, then publishes
    /// `Closed`. Never holds the shared-state lock while waiting on
    /// in-flight callbacks. Idempotent.
    pub fn stop(&mut self) -> Result<(), CaptureError> {
        self.shutdown()
    }

    fn shutdown(&mut self) -> Result<(), CaptureError> {
        let mut failed = false;
        let frame_token = self.frame_token.take();
        let closed_token = self.closed_token.take();
        if let (Some(pool), Some(token)) = (self.wgc_pool.as_ref(), frame_token) {
            failed |= pool.RemoveFrameArrived(token).is_err();
        }
        if let (Some(item), Some(token)) = (self.item.as_ref(), closed_token) {
            failed |= item.RemoveClosed(token).is_err();
        }
        if let Some(session) = self.session.as_ref() {
            failed |= session.Close().is_err();
        }
        if let Some(pool) = self.wgc_pool.as_ref() {
            failed |= pool.Close().is_err();
        }
        self.session.take();
        self.wgc_pool.take();
        self.item.take();
        let mut state = lock_shared(&self.shared);
        state.latest.take();
        state.health.event(CaptureEvent::Closed);
        if failed { Err(CaptureError::Device) } else { Ok(()) }
    }
}

#[cfg(windows)]
impl Drop for NativeCapture {
    fn drop(&mut self) {
        let _ = self.shutdown();
    }
}
