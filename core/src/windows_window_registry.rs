// BEGIN CODEX IDENTITY — security-owned, preserved byte-exact.
use std::collections::{BTreeMap, BTreeSet};
use serde::Serialize;
use crate::capsule_registry::Clock;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct Rect { pub x: i32, pub y: i32, pub width: u32, pub height: u32 }

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(transparent)]
pub struct WindowHandle(String);

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct WindowView {
    pub handle: WindowHandle, pub generation: Clock, pub layout_revision: Clock,
    pub process_id: u32, pub executable: String, pub title: String, pub class: String,
    pub rect: Rect, pub visible: bool, pub minimized: bool,
}

/// Synthetic observations only; slot/incarnation are fixtures, never native handles.
#[derive(Clone, Debug)]
pub struct FixtureWindow {
    pub slot: String, pub incarnation: u64, pub process_id: u32, pub process_birth: u64,
    pub executable: String, pub title: String, pub class: String, pub rect: Rect,
    pub visible: bool, pub minimized: bool, pub layout: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum WindowError { Unsupported, InvalidDescriptor, DuplicateObservation, IdentityExhausted, StaleWindow, AmbiguousDescriptor, Unavailable, NativeFailure }

// Native numbers and lifetime/process witnesses are confined to this module.
#[derive(Clone, PartialEq, Eq, PartialOrd, Ord)]
enum WindowKey { Fixture(String), #[cfg(windows)] Native(usize) }
#[derive(Clone)]
struct Observation {
    key: WindowKey, witness: u64, process_birth: u64, process_id: u32,
    executable: String, title: String, class: String, rect: Rect,
    visible: bool, minimized: bool, layout: String,
}
#[derive(Clone)]
struct Entry { observation: Observation, view: WindowView }

pub struct WindowRegistry { epoch: Clock, next: u64, entries: BTreeMap<WindowKey, Entry> }

impl WindowRegistry {
    pub fn new(epoch: Clock) -> Self { Self { epoch, next: 0, entries: BTreeMap::new() } }
    pub fn refresh_fixtures(&mut self, fixtures: &[FixtureWindow]) -> Result<Vec<WindowView>, WindowError> {
        let observations = fixtures.iter().map(|f| Observation { key: WindowKey::Fixture(f.slot.clone()), witness: f.incarnation,
            process_birth: f.process_birth, process_id: f.process_id, executable: f.executable.clone(), title: f.title.clone(),
            class: f.class.clone(), rect: f.rect, visible: f.visible, minimized: f.minimized, layout: f.layout.clone() }).collect();
        self.reconcile(observations)
    }
    fn reconcile(&mut self, observations: Vec<Observation>) -> Result<Vec<WindowView>, WindowError> {
        let mut observations = observations;
        observations.sort_by(|a, b| a.key.cmp(&b.key));
        let mut keys = BTreeSet::new();
        for o in &observations {
            if !keys.insert(o.key.clone()) { return Err(WindowError::DuplicateObservation); }
            if o.witness == 0 || o.process_birth == 0 || o.process_id == 0 || o.executable.is_empty() || o.class.is_empty()
                || o.rect.width == 0 || o.rect.height == 0 || matches!(&o.key, WindowKey::Fixture(s) if s.is_empty()) {
                return Err(WindowError::InvalidDescriptor);
            }
        }
        let mut next = self.next;
        let mut entries = BTreeMap::new();
        for o in observations {
            let same = self.entries.get(&o.key).filter(|e| e.observation.witness == o.witness
                && e.observation.process_birth == o.process_birth && e.observation.process_id == o.process_id
                && e.observation.executable == o.executable && e.observation.class == o.class);
            let (handle, generation, layout_revision) = if let Some(old) = same {
                let changed = old.observation.rect.width != o.rect.width || old.observation.rect.height != o.rect.height || old.observation.layout != o.layout;
                let layout = if changed { Clock(old.view.layout_revision.0.checked_add(1).ok_or(WindowError::IdentityExhausted)?) } else { old.view.layout_revision };
                (old.view.handle.clone(), old.view.generation, layout)
            } else {
                next = next.checked_add(1).ok_or(WindowError::IdentityExhausted)?;
                (WindowHandle(format!("window-{}-{}", self.epoch.0, next)), Clock(next), Clock(1))
            };
            let view = WindowView { handle, generation, layout_revision, process_id: o.process_id, executable: o.executable.clone(), title: o.title.clone(),
                class: o.class.clone(), rect: o.rect, visible: o.visible, minimized: o.minimized };
            entries.insert(o.key.clone(), Entry { observation: o, view });
        }
        self.next = next; self.entries = entries;
        Ok(self.snapshots())
    }
    pub fn snapshots(&self) -> Vec<WindowView> { self.entries.values().map(|e| e.view.clone()).collect() }
    pub fn resolve(&self, handle: &WindowHandle, generation: Clock) -> Result<WindowView, WindowError> {
        let e = self.entries.values().find(|e| &e.view.handle == handle && e.view.generation == generation).ok_or(WindowError::StaleWindow)?;
        if !e.view.visible || e.view.minimized { return Err(WindowError::Unavailable); }
        if self.entries.values().filter(|x| x.view.visible && !x.view.minimized && x.view.executable == e.view.executable
            && x.view.class == e.view.class && x.view.title == e.view.title).count() != 1 { return Err(WindowError::AmbiguousDescriptor); }
        Ok(e.view.clone())
    }
    /// Caller-observed call-layout change invalidates all earlier crop approvals.
    pub fn invalidate_layout(&mut self, handle: &WindowHandle) -> Result<(), WindowError> {
        let e = self.entries.values_mut().find(|e| &e.view.handle == handle).ok_or(WindowError::StaleWindow)?;
        e.view.layout_revision = Clock(e.view.layout_revision.0.checked_add(1).ok_or(WindowError::IdentityExhausted)?); Ok(())
    }
}
// END CODEX IDENTITY
// BEGIN CODEX NATIVE IDENTITY — security-owned, preserved byte-exact.
#[cfg(windows)]
mod native_identity {
    use super::WindowError;
    use crate::capsule_registry::Clock;
    use std::collections::BTreeMap;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use windows::core::{PCWSTR, PWSTR};
    use windows::Win32::Foundation::{HANDLE, HWND, FILETIME, CloseHandle};
    use windows::Win32::System::Threading::{OpenProcess,GetProcessTimes,QueryFullProcessImageNameW,PROCESS_QUERY_LIMITED_INFORMATION,PROCESS_NAME_WIN32};
    use windows::Win32::UI::WindowsAndMessaging::{GetWindowThreadProcessId,IsWindow,GetPropW,SetPropW,RemovePropW};
    static NEXT_COOKIE: AtomicUsize = AtomicUsize::new(1);
    struct Process(HANDLE);
    impl Drop for Process { fn drop(&mut self) { unsafe { let _ = CloseHandle(self.0); } } }
    pub(super) struct WitnessStore { property: Vec<u16>, owned: BTreeMap<usize, usize> }
    impl WitnessStore { pub(super) fn cleanup_property_name(&self) -> Vec<u16> { self.property.clone() }
        pub(super) fn new(epoch: Clock) -> Self {
            Self { property: format!("Cafe.WindowLifetime.{}",epoch.0).encode_utf16().chain(Some(0)).collect(), owned: BTreeMap::new() }
        }
        pub(super) fn observe(&mut self, hwnd: HWND) -> Result<(u64,u64,u32,String),WindowError> {
            unsafe {
                if !IsWindow(Some(hwnd)).as_bool() { return Err(WindowError::NativeFailure); }
                let mut pid=0; GetWindowThreadProcessId(hwnd,Some(&mut pid));
                if pid==0 { return Err(WindowError::NativeFailure); }
                let process=Process(OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,false,pid).map_err(|_|WindowError::NativeFailure)?);
                let (mut birth,mut exit,mut kernel,mut user)=(FILETIME::default(),FILETIME::default(),FILETIME::default(),FILETIME::default());
                GetProcessTimes(process.0,&mut birth,&mut exit,&mut kernel,&mut user).map_err(|_|WindowError::NativeFailure)?;
                let process_birth=((birth.dwHighDateTime as u64)<<32)|birth.dwLowDateTime as u64;
                if process_birth==0 { return Err(WindowError::NativeFailure); }
                let mut buf=vec![0u16;32768];let mut length=buf.len() as u32;
                QueryFullProcessImageNameW(process.0,PROCESS_NAME_WIN32,PWSTR(buf.as_mut_ptr()),&mut length).map_err(|_|WindowError::NativeFailure)?;
                if length==0 || length as usize>=buf.len() { return Err(WindowError::NativeFailure); }
                let image=String::from_utf16(&buf[..length as usize]).map_err(|_|WindowError::NativeFailure)?;
                let executable=image.rsplit(['\\','/']).next().filter(|s|!s.is_empty()).ok_or(WindowError::NativeFailure)?.to_owned();
                let raw=hwnd.0 as usize;let property=PCWSTR(self.property.as_ptr());
                let present=GetPropW(hwnd,property).0 as usize;
                let cookie=if present!=0 && self.owned.get(&raw)==Some(&present) { present } else {
                    let next=NEXT_COOKIE.fetch_update(Ordering::Relaxed,Ordering::Relaxed,|n|n.checked_add(1).filter(|x|*x<=isize::MAX as usize)).map_err(|_|WindowError::IdentityExhausted)?;
                    SetPropW(hwnd,property,Some(HANDLE(next as *mut core::ffi::c_void))).map_err(|_|WindowError::NativeFailure)?;next
                };
                let mut current_pid=0;GetWindowThreadProcessId(hwnd,Some(&mut current_pid));
                if !IsWindow(Some(hwnd)).as_bool() || current_pid!=pid || GetPropW(hwnd,property).0 as usize!=cookie { return Err(WindowError::NativeFailure); }
                self.owned.insert(raw,cookie);Ok((cookie as u64,process_birth,pid,executable))
            }
        }
    }
    impl Drop for WitnessStore {
        fn drop(&mut self) {
            for (&raw,&cookie) in &self.owned { unsafe {
                let hwnd=HWND(raw as *mut core::ffi::c_void);let property=PCWSTR(self.property.as_ptr());
                if IsWindow(Some(hwnd)).as_bool() && GetPropW(hwnd,property).0 as usize==cookie { let _=RemovePropW(hwnd,property); }
            } }
        }
    }
}
// END CODEX NATIVE IDENTITY

// BEGIN MUSE ADAPTER — native enumeration only; identity logic stays in Codex fragments above.
#[cfg(not(windows))]
pub struct NativeAdapter {
    _epoch: Clock,
}

#[cfg(not(windows))]
impl NativeAdapter {
    pub fn new(epoch: Clock) -> Self {
        Self { _epoch: epoch }
    }

    pub fn enumerate(
        &mut self,
        _registry: &mut WindowRegistry,
    ) -> Result<Vec<WindowView>, WindowError> {
        Err(WindowError::Unsupported)
    }
}

#[cfg(windows)]
pub struct NativeAdapter {
    store: native_identity::WitnessStore,
}

#[cfg(windows)]
struct EnumCtx {
    hwnds: Vec<usize>,
    overflow: bool,
}

#[cfg(windows)]
unsafe extern "system" fn enum_proc(
    hwnd: windows::Win32::Foundation::HWND,
    lparam: windows::Win32::Foundation::LPARAM,
) -> windows::core::BOOL {
    use windows::Win32::Foundation::{FALSE, TRUE};
    if lparam.0 == 0 {
        return FALSE;
    }
    let ctx = &mut *(lparam.0 as *mut EnumCtx);
    if ctx.hwnds.len() >= 4096 {
        ctx.overflow = true;
        return FALSE;
    }
    ctx.hwnds.push(hwnd.0 as usize);
    TRUE
}

#[cfg(windows)]
impl NativeAdapter {
    pub fn new(epoch: Clock) -> Self {
        Self {
            store: native_identity::WitnessStore::new(epoch),
        }
    }

    pub fn enumerate(
        &mut self,
        registry: &mut WindowRegistry,
    ) -> Result<Vec<WindowView>, WindowError> {
        use windows::Win32::Foundation::{HWND, LPARAM};
        use windows::Win32::UI::WindowsAndMessaging::{
            EnumWindows, IsIconic, IsWindowVisible,
        };

        let mut ctx = EnumCtx {
            hwnds: Vec::new(),
            overflow: false,
        };
        unsafe {
            EnumWindows(
                Some(enum_proc),
                LPARAM(&mut ctx as *mut EnumCtx as isize),
            )
            .map_err(|_| WindowError::NativeFailure)?;
        }
        if ctx.overflow {
            return Err(WindowError::NativeFailure);
        }
        let mut observations = Vec::with_capacity(ctx.hwnds.len());
        for raw in ctx.hwnds {
            let hwnd = HWND(raw as *mut core::ffi::c_void);
            let (cookie, process_birth, pid, executable) = self.store.observe(hwnd)?;
            let title = read_title(hwnd)?;
            let class = read_class(hwnd)?;
            let rect = read_rect(hwnd)?;
            let (visible, minimized) = unsafe {
                (
                    IsWindowVisible(hwnd).as_bool(),
                    IsIconic(hwnd).as_bool(),
                )
            };
            let again = self.store.observe(hwnd)?;
            if again != (cookie, process_birth, pid, executable.clone()) {
                return Err(WindowError::NativeFailure);
            }
            observations.push(Observation {
                key: WindowKey::Native(raw),
                witness: cookie,
                process_birth,
                process_id: pid,
                executable,
                title,
                class,
                rect,
                visible,
                minimized,
                layout: String::new(),
            });
        }
        registry.reconcile(observations)
    }
}

#[cfg(windows)]
fn read_title(hwnd: windows::Win32::Foundation::HWND) -> Result<String, WindowError> {
    use windows::Win32::UI::WindowsAndMessaging::GetWindowTextW;
    let mut buf = [0u16; 512];
    let len = unsafe { GetWindowTextW(hwnd, &mut buf) };
    if len < 0 || (len as usize) + 1 >= buf.len() {
        return Err(WindowError::NativeFailure);
    }
    String::from_utf16(&buf[..len as usize]).map_err(|_| WindowError::NativeFailure)
}

#[cfg(windows)]
fn read_class(hwnd: windows::Win32::Foundation::HWND) -> Result<String, WindowError> {
    use windows::Win32::UI::WindowsAndMessaging::GetClassNameW;
    let mut buf = [0u16; 512];
    let len = unsafe { GetClassNameW(hwnd, &mut buf) };
    if len <= 0 || (len as usize) + 1 >= buf.len() {
        return Err(WindowError::NativeFailure);
    }
    String::from_utf16(&buf[..len as usize]).map_err(|_| WindowError::NativeFailure)
}

#[cfg(windows)]
fn read_rect(hwnd: windows::Win32::Foundation::HWND) -> Result<Rect, WindowError> {
    use windows::Win32::Foundation::RECT;
    use windows::Win32::UI::WindowsAndMessaging::GetWindowRect;
    let mut raw = RECT::default();
    unsafe {
        GetWindowRect(hwnd, &mut raw as *mut RECT)
            .map_err(|_| WindowError::NativeFailure)?;
    }
    if raw.right <= raw.left || raw.bottom <= raw.top {
        return Err(WindowError::NativeFailure);
    }
    let width = raw
        .right
        .checked_sub(raw.left)
        .and_then(|w| u32::try_from(w).ok())
        .ok_or(WindowError::NativeFailure)?;
    let height = raw
        .bottom
        .checked_sub(raw.top)
        .and_then(|h| u32::try_from(h).ok())
        .ok_or(WindowError::NativeFailure)?;
    if width == 0 || height == 0 {
        return Err(WindowError::NativeFailure);
    }
    Ok(Rect {
        x: raw.left,
        y: raw.top,
        width,
        height,
    })
}
// END MUSE ADAPTER

// Capture capability creation stays inside the registry; raw native keys never escape.
#[cfg(windows)]
impl WindowRegistry {
    pub(crate) fn create_capture_item(&self, handle: &WindowHandle, generation: Clock, adapter: &mut NativeAdapter) -> Result<windows::Graphics::Capture::GraphicsCaptureItem, WindowError> {
        use windows::core::factory;
        use windows::Win32::Foundation::HWND;
        use windows::Win32::UI::WindowsAndMessaging::{IsWindowVisible,IsIconic};
        use windows::Win32::System::WinRT::Graphics::Capture::IGraphicsCaptureItemInterop;
        use windows::Graphics::Capture::GraphicsCaptureItem;
        self.resolve(handle,generation)?;
        let entry=self.entries.values().find(|e| &e.view.handle==handle && e.view.generation==generation).ok_or(WindowError::StaleWindow)?;
        let raw=match entry.observation.key { WindowKey::Native(raw)=>raw, _=>return Err(WindowError::Unsupported) };
        let hwnd=HWND(raw as *mut core::ffi::c_void);
        let expected=(entry.observation.witness,entry.observation.process_birth,entry.observation.process_id,entry.observation.executable.clone());
        if adapter.store.observe(hwnd)?!=expected { return Err(WindowError::StaleWindow); }
        if unsafe { !IsWindowVisible(hwnd).as_bool() || IsIconic(hwnd).as_bool() } { return Err(WindowError::Unavailable); }
        let interop:IGraphicsCaptureItemInterop=factory::<GraphicsCaptureItem,IGraphicsCaptureItemInterop>().map_err(|_|WindowError::NativeFailure)?;
        let item=unsafe { interop.CreateForWindow::<GraphicsCaptureItem>(hwnd) }.map_err(|_|WindowError::NativeFailure)?;
        if adapter.store.observe(hwnd)?!=expected { return Err(WindowError::StaleWindow); }
        if unsafe { !IsWindowVisible(hwnd).as_bool() || IsIconic(hwnd).as_bool() } { return Err(WindowError::Unavailable); }
        Ok(item)
    }
}

// Append inside windows_window_registry.rs. Windows-only probe support, never serialized.
#[cfg(windows)]
pub(crate) use synthetic_owned_window::SyntheticWindowFixture;

#[cfg(windows)]
mod synthetic_owned_window {
    use super::{NativeAdapter, Observation, WindowError, WindowKey, WindowRegistry, WindowView};
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::mpsc::{self, Receiver, Sender, RecvTimeoutError};
    use std::thread::{self, JoinHandle};
    use std::time::Duration;
    use windows::core::PCWSTR;
    use windows::Win32::Foundation::{COLORREF, HINSTANCE, HWND, LPARAM, LRESULT, RECT, WPARAM};
    use windows::Win32::Graphics::Gdi::{
        BeginPaint, CreateSolidBrush, DeleteObject, EndPaint, FillRect, HGDIOBJ,
        InvalidateRect, PAINTSTRUCT, UpdateWindow,
    };
    use windows::Win32::System::LibraryLoader::GetModuleHandleW;
    use windows::Win32::UI::WindowsAndMessaging::{
        CreateWindowExW, DefWindowProcW, DestroyWindow, DispatchMessageW, GetClientRect,
        GetPropW, IsIconic, IsWindowVisible, MSG, PeekMessageW, PM_REMOVE,
        RegisterClassW, RemovePropW, ShowWindow, SW_SHOWNORMAL, TranslateMessage,
        UnregisterClassW, WINDOW_EX_STYLE, WM_CLOSE, WM_PAINT, WNDCLASSW, WS_OVERLAPPEDWINDOW,
    };

    // Test geometry/limits, not production observations.
    const TEST_OUTER_WIDTH: i32 = 320;
    const TEST_OUTER_HEIGHT: i32 = 240;
    const START_WAIT: Duration = Duration::from_secs(5);
    const CLOSE_WAIT: Duration = Duration::from_secs(5);
    const PUMP_WAIT: Duration = Duration::from_millis(10);
    const TEST_COLOR: COLORREF = COLORREF(0x00ac6824); // RGB 24,68,ac; BGRA ac,68,24,ff.
    static NEXT_CLASS: AtomicU64 = AtomicU64::new(1);

    // No HWND/raw identity in the facade, no Debug/Serialize implementation.
    pub(crate) struct SyntheticWindowFixture {
        shutdown: Option<Sender<()>>,
        done: Receiver<Result<(), WindowError>>,
        thread: Option<JoinHandle<()>>,
    }

    impl SyntheticWindowFixture {
        pub(crate) fn open(
            registry: &mut WindowRegistry,
            adapter: &mut NativeAdapter,
        ) -> Result<(Self, WindowView), WindowError> {
            // Probe uses a dedicated empty registry: never evict unrelated identities.
            if !registry.entries.is_empty() { return Err(WindowError::InvalidDescriptor); }
            let sequence = NEXT_CLASS.fetch_update(Ordering::Relaxed, Ordering::Relaxed,
                |n| n.checked_add(1)).map_err(|_| WindowError::IdentityExhausted)?;
            let class: Vec<u16> = format!("CafeCaptureFixture{sequence}").encode_utf16().chain(Some(0)).collect();
            let property = adapter.store.cleanup_property_name();
            let (ready_tx, ready_rx) = mpsc::channel::<Result<usize, WindowError>>();
            let (shutdown_tx, shutdown_rx) = mpsc::channel::<()>();
            let (done_tx, done_rx) = mpsc::channel::<Result<(), WindowError>>();
            let worker = thread::Builder::new().name("cafe-capture-fixture".into())
                .spawn(move || window_thread(class, property, ready_tx, shutdown_rx, done_tx))
                .map_err(|_| WindowError::Unavailable)?;
            let fixture = Self { shutdown: Some(shutdown_tx), done: done_rx, thread: Some(worker) };
            // Raw value exists only in this registry submodule's private channel/local scope.
            let raw = ready_rx.recv_timeout(START_WAIT).map_err(|_| WindowError::Unavailable)??;
            let hwnd = HWND(raw as *mut core::ffi::c_void);
            let (witness, process_birth, process_id, executable) = adapter.store.observe(hwnd)?;
            let title = super::read_title(hwnd)?;
            let class = super::read_class(hwnd)?;
            let rect = super::read_rect(hwnd)?;
            let visible = unsafe { IsWindowVisible(hwnd).as_bool() };
            let minimized = unsafe { IsIconic(hwnd).as_bool() };
            if !visible || minimized { return Err(WindowError::Unavailable); }
            if adapter.store.observe(hwnd)? != (witness, process_birth, process_id, executable.clone()) {
                return Err(WindowError::StaleWindow);
            }
            let mut views = registry.reconcile(vec![Observation {
                key: WindowKey::Native(raw), witness, process_birth, process_id, executable,
                title, class, rect, visible, minimized, layout: String::new(),
            }])?;
            let view = views.pop().ok_or(WindowError::NativeFailure)?;
            registry.resolve(&view.handle, view.generation)?;
            Ok((fixture, view))
        }

        pub(crate) fn close(mut self) -> Result<(), WindowError> {
            let result = self.stop();
            // Do not repeat a full receive deadline in Drop after an explicit timeout.
            if result.is_err() { self.thread.take(); }
            result
        }

        fn stop(&mut self) -> Result<(), WindowError> {
            if self.thread.is_none() { return Ok(()); }
            if let Some(sender) = self.shutdown.take() { let _ = sender.send(()); }
            match self.done.recv_timeout(CLOSE_WAIT) {
                Ok(result) => {
                    if let Some(worker) = self.thread.take() {
                        worker.join().map_err(|_| WindowError::NativeFailure)?;
                    }
                    result
                }
                Err(RecvTimeoutError::Disconnected) => {
                    if let Some(worker) = self.thread.take() {
                        worker.join().map_err(|_| WindowError::NativeFailure)?;
                    }
                    Err(WindowError::NativeFailure)
                }
                Err(RecvTimeoutError::Timeout) => Err(WindowError::Unavailable),
            }
        }
    }

    impl Drop for SyntheticWindowFixture {
        fn drop(&mut self) { let _ = self.stop(); }
    }

    struct OwnedWindow {
        hwnd: Option<HWND>,
        class: Vec<u16>,
        property: Vec<u16>,
        instance: HINSTANCE,
        registered: bool,
    }

    impl OwnedWindow {
        fn cleanup(&mut self) -> Result<(), WindowError> {
            let mut failed = false;
            if let Some(hwnd) = self.hwnd.take() {
                unsafe {
                    // Only this known adapter property on this thread's OWN window.
                    let key = PCWSTR(self.property.as_ptr());
                    if !GetPropW(hwnd, key).0.is_null() { let _ = RemovePropW(hwnd, key); }
                    if DestroyWindow(hwnd).is_err() { failed = true; }
                }
            }
            if self.registered {
                self.registered = false;
                if unsafe { UnregisterClassW(PCWSTR(self.class.as_ptr()), Some(self.instance)) }.is_err() {
                    failed = true;
                }
            }
            if failed { Err(WindowError::NativeFailure) } else { Ok(()) }
        }
    }

    impl Drop for OwnedWindow {
        fn drop(&mut self) { let _ = self.cleanup(); }
    }

    fn window_thread(
        class: Vec<u16>, property: Vec<u16>, ready: Sender<Result<usize, WindowError>>,
        shutdown: Receiver<()>, done: Sender<Result<(), WindowError>>,
    ) {
        let instance = match unsafe { GetModuleHandleW(None) } {
            Ok(module) => HINSTANCE(module.0),
            Err(_) => { let _ = ready.send(Err(WindowError::Unavailable)); return; }
        };
        let mut owned = OwnedWindow { hwnd: None, class, property, instance, registered: false };
        let setup = (|| -> Result<HWND, WindowError> {
            let descriptor = WNDCLASSW { lpfnWndProc: Some(window_proc), hInstance: instance,
                lpszClassName: PCWSTR(owned.class.as_ptr()), ..Default::default() };
            if unsafe { RegisterClassW(&descriptor) } == 0 { return Err(WindowError::Unavailable); }
            owned.registered = true;
            let caption: Vec<u16> = "Capture Fixture".encode_utf16().chain(Some(0)).collect();
            let hwnd = unsafe { CreateWindowExW(WINDOW_EX_STYLE::default(), PCWSTR(owned.class.as_ptr()),
                PCWSTR(caption.as_ptr()), WS_OVERLAPPEDWINDOW, 40, 40,
                TEST_OUTER_WIDTH, TEST_OUTER_HEIGHT, None, None, Some(instance), None) }
                .map_err(|_| WindowError::Unavailable)?;
            owned.hwnd = Some(hwnd);
            unsafe {
                let _ = ShowWindow(hwnd, SW_SHOWNORMAL);
                let _ = InvalidateRect(Some(hwnd), None, false);
                let _ = UpdateWindow(hwnd);
            }
            if !unsafe { IsWindowVisible(hwnd).as_bool() } || unsafe { IsIconic(hwnd).as_bool() } {
                return Err(WindowError::Unavailable);
            }
            Ok(hwnd)
        })();
        let hwnd = match setup {
            Ok(hwnd) => hwnd,
            Err(error) => {
                let _ = owned.cleanup();
                let _ = ready.send(Err(error));
                let _ = done.send(Ok(()));
                return;
            }
        };
        if ready.send(Ok(hwnd.0 as usize)).is_ok() {
            loop {
                match shutdown.recv_timeout(PUMP_WAIT) {
                    Ok(()) | Err(RecvTimeoutError::Disconnected) => break,
                    Err(RecvTimeoutError::Timeout) => {}
                }
                let mut message = MSG::default();
                // One message each iteration keeps shutdown polling bounded.
                if unsafe { PeekMessageW(&mut message, None, 0, 0, PM_REMOVE).as_bool() } {
                    unsafe { let _ = TranslateMessage(&message); let _ = DispatchMessageW(&message); }
                }
                // Produce fresh frames even if the first paint preceded StartCapture.
                unsafe { let _ = InvalidateRect(Some(hwnd), None, false); }
            }
        }
        let result = owned.cleanup();
        let _ = done.send(result);
    }

    unsafe extern "system" fn window_proc(hwnd: HWND, message: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
        if message == WM_CLOSE {
            // Only the owner-thread cleanup path may destroy the fixture and its property.
            return LRESULT(0);
        }
        if message == WM_PAINT {
            let mut paint = PAINTSTRUCT::default();
            let dc = unsafe { BeginPaint(hwnd, &mut paint) };
            if !dc.0.is_null() {
                let brush = unsafe { CreateSolidBrush(TEST_COLOR) };
                if !brush.0.is_null() {
                    let mut client = RECT::default();
                    if unsafe { GetClientRect(hwnd, &mut client) }.is_ok() {
                        unsafe { let _ = FillRect(dc, &client, brush); }
                    }
                    unsafe { let _ = DeleteObject(HGDIOBJ(brush.0)); }
                }
            }
            unsafe { let _ = EndPaint(hwnd, &paint); }
            return LRESULT(0);
        }
        unsafe { DefWindowProcW(hwnd, message, wparam, lparam) }
    }
}

// Registry-owned capability: raw native identity remains private to this module.
#[cfg(windows)]
#[derive(Clone)]
pub(crate) struct CapturePermit {
    item: windows::Graphics::Capture::GraphicsCaptureItem,
    native_key: usize, cookie: u64, birth: u64, pid: u32, property: Vec<u16>,
}
#[cfg(windows)]
impl CapturePermit {
    pub(crate) fn item(&self) -> windows::Graphics::Capture::GraphicsCaptureItem { self.item.clone() }
    pub(crate) fn check(&self) -> Result<(), WindowError> {
        use windows::core::PCWSTR;
        use windows::Win32::Foundation::{HWND,FILETIME,CloseHandle};
        use windows::Win32::UI::WindowsAndMessaging::{IsWindow,IsWindowVisible,IsIconic,GetPropW,GetWindowThreadProcessId};
        use windows::Win32::System::Threading::{OpenProcess,GetProcessTimes,PROCESS_QUERY_LIMITED_INFORMATION};
        let hwnd=HWND(self.native_key as *mut core::ffi::c_void);
        unsafe {
            if !IsWindow(Some(hwnd)).as_bool() { return Err(WindowError::StaleWindow); }
            if !IsWindowVisible(hwnd).as_bool() || IsIconic(hwnd).as_bool() { return Err(WindowError::Unavailable); }
            let mut pid=0;GetWindowThreadProcessId(hwnd,Some(&mut pid));
            if pid!=self.pid || GetPropW(hwnd,PCWSTR(self.property.as_ptr())).0 as usize as u64!=self.cookie { return Err(WindowError::StaleWindow); }
            let process=OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,false,pid).map_err(|_|WindowError::NativeFailure)?;
            let (mut birth,mut exit,mut kernel,mut user)=(FILETIME::default(),FILETIME::default(),FILETIME::default(),FILETIME::default());
            let read=GetProcessTimes(process,&mut birth,&mut exit,&mut kernel,&mut user);
            let _=CloseHandle(process);
            read.map_err(|_|WindowError::NativeFailure)?;
            let birth=((birth.dwHighDateTime as u64)<<32)|birth.dwLowDateTime as u64;
            if birth!=self.birth || GetPropW(hwnd,PCWSTR(self.property.as_ptr())).0 as usize as u64!=self.cookie || !IsWindow(Some(hwnd)).as_bool() { return Err(WindowError::StaleWindow); }
            if !IsWindowVisible(hwnd).as_bool() || IsIconic(hwnd).as_bool() { return Err(WindowError::Unavailable); }
            Ok(())
        }
    }
}
#[cfg(windows)]
impl WindowRegistry {
    pub(crate) fn capture_permit(&self, handle:&WindowHandle, generation:Clock, adapter:&mut NativeAdapter) -> Result<CapturePermit,WindowError> {
        let item=self.create_capture_item(handle,generation,adapter)?;
        let entry=self.entries.values().find(|e| &e.view.handle==handle && e.view.generation==generation).ok_or(WindowError::StaleWindow)?;
        let native_key=match entry.observation.key { WindowKey::Native(key)=>key,_=>return Err(WindowError::Unsupported) };
        let permit=CapturePermit { item,native_key,cookie:entry.observation.witness,birth:entry.observation.process_birth,pid:entry.observation.process_id,property:adapter.store.cleanup_property_name() };
        permit.check()?;Ok(permit)
    }
}
