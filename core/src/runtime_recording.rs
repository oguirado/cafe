//! Owned runtime construction: real native frames, approved hardware encoder,
//! bounded crop copies, existing P11 recorder. No renderer-supplied argv.
use std::{cell::{Cell,RefCell},path::{Path,PathBuf,Component},time::{Duration,Instant},sync::Arc};
use crate::{authority_journal::Journal,capsule_registry::Clock,capsule_pairing::{Binding,Crop},
 capture_frame_pool::{FramePool,PoolLimits,OwnedFrame,BorrowedFrame},capture_health::{CaptureState,HealthPolicy},
 capture_pipeline::{CapturePipeline,FrameSource},windows_graphics_capture::{NativeCapture,ObservedCaptureConfig,CaptureError,trace_stage,initialize_trace},
 windows_window_registry::{WindowRegistry,NativeAdapter},ffmpeg_recorder::{self,Encoder,EncoderUse,VideoProfile,Recorder,RecordingPlan}};

const MAX_FRAME_WIDTH:u32=3840;
const MAX_FRAME_HEIGHT:u32=2160;
const CAPTURE_POOL_SLOTS:usize=8;
// [TARGET] Four 1080p feeds fit below the existing aggregate pool bound.
// Hardware throughput and queue sufficiency remain PENDING-LIVE.
const RECORDING_QUEUE_CAPACITY:usize=32;
const CROP_POOL_SLOTS:usize=RECORDING_QUEUE_CAPACITY+4;
fn observed_pool_limits(width:u32,height:u32,slots:usize)->Result<PoolLimits,&'static str>{
 if width==0||height==0||width>MAX_FRAME_WIDTH||height>MAX_FRAME_HEIGHT||slots==0{return Err("crop_resource_bound");}
 let max_frame_bytes=(width as usize).checked_mul(height as usize).and_then(|value|value.checked_mul(4)).ok_or("crop_resource_bound")?;
 Ok(PoolLimits{slots,max_frame_bytes,max_width:width,max_height:height})
}
fn hardware_candidates(preferred:Option<Encoder>)->[Encoder;3]{
 match preferred{
  Some(Encoder::Amf)=>[Encoder::Amf,Encoder::Nvenc,Encoder::Qsv],
  Some(Encoder::Qsv)=>[Encoder::Qsv,Encoder::Nvenc,Encoder::Amf],
  _=>[Encoder::Nvenc,Encoder::Amf,Encoder::Qsv],
 }
}

/// Operator-selected external binary. Approval is checked again by preflight and
/// WGC delivers no frame while a window's pixels do not change, so a still window
/// is not a failure: the last frame repeats. Closure and minimizing are detected separately.
const RECORDING_MAX_HOLD_NS:u64=3_600_000_000_000;
const RECORDING_STALL_BUDGET:Clock=Clock(36_000_000_000);
/// at spawn; this object never substitutes a system binary or software codec.
pub(crate) struct ExternalFfmpeg {path:PathBuf,sha256:String,preferred_encoder:Cell<Option<Encoder>>,verified_preflight:RefCell<Option<ffmpeg_recorder::Preflight>>}
impl ExternalFfmpeg {
 pub(crate) fn select(path:&Path,sha256:&str)->Result<Self,&'static str>{
  if !path.is_absolute() || path.components().any(|c|matches!(c,Component::ParentDir)) || sha256.len()!=64 || !sha256.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b)){return Err("ffmpeg_selection_invalid");}
  let mut current=PathBuf::new();for c in path.components(){current.push(c);let meta=std::fs::symlink_metadata(&current).map_err(|_|"ffmpeg_selection_unavailable")?;if meta.file_type().is_symlink(){return Err("ffmpeg_selection_link");}
   #[cfg(windows)] {use std::os::windows::fs::MetadataExt;if meta.file_attributes()&0x400!=0{return Err("ffmpeg_selection_link");}}
  }
  if !path.is_file(){return Err("ffmpeg_selection_invalid");}
  // Stream a bounded buffer: never load an operator-selected executable whole.
  use std::io::Read;use sha2::{Digest,Sha256};let mut hash=Sha256::new();let mut input=std::fs::File::open(path).map_err(|_|"ffmpeg_selection_unavailable")?;let mut buffer=[0u8;65536];
  loop{let n=input.read(&mut buffer).map_err(|_|"ffmpeg_selection_unavailable")?;if n==0{break;}hash.update(&buffer[..n]);}
  if hash.finalize().iter().map(|byte|format!("{byte:02x}")).collect::<String>()!=sha256{return Err("ffmpeg_hash_mismatch");}
  Ok(Self{path:path.to_path_buf(),sha256:sha256.into(),preferred_encoder:Cell::new(None),verified_preflight:RefCell::new(None)})
 }
 /// True only after a successful hardware trial for this approval. It is not
 /// evidence that another concurrent session can start (PENDING-LIVE).
 pub(crate) fn encoder_ready(&self)->bool{self.preferred_encoder.get().is_some()}
}

/// Crop is applied to owned CPU copies after capture, never in the callback.
/// It retains the first observed frame until the recorder starts.
pub(crate) struct RecordingSource {capture:NativeCapture,first:Option<OwnedFrame>,pool:Arc<FramePool>,crop:crate::capsule_pairing::Crop,failed:bool}
fn crop_frame(pool:&Arc<FramePool>,frame:&OwnedFrame,crop:crate::capsule_pairing::Crop)->Result<OwnedFrame,&'static str>{
 if crop.width==0 || crop.height==0 || crop.x.checked_add(crop.width).is_none_or(|v|v>frame.width()) || crop.y.checked_add(crop.height).is_none_or(|v|v>frame.height()){return Err("observed_crop_outside_frame");}
 let start=(crop.y as usize).checked_mul(frame.packed_stride()).and_then(|v|v.checked_add(crop.x as usize*4)).ok_or("observed_crop_overflow")?;
 pool.copy_from(BorrowedFrame{data:frame.pixels().get(start..).ok_or("observed_crop_outside_frame")?,width:crop.width,height:crop.height,source_stride:frame.packed_stride(),source_time:frame.source_time()}).map_err(|_|"crop_pool_exhausted")
}
impl FrameSource for RecordingSource {
 fn take_latest(&mut self)->Option<OwnedFrame>{let frame=self.first.take().or_else(||self.capture.take_latest())?;match crop_frame(&self.pool,&frame,self.crop){Ok(copy)=>Some(copy),Err(_)=>{self.failed=true;None}}}
 fn state(&self,registry:&WindowRegistry)->CaptureState{if self.failed{CaptureState::SourceFailed}else{self.capture.poll_state(registry)}}
 fn stop(&mut self)->Result<(),CaptureError>{self.first.take();self.capture.stop()}
}

fn recording_profile(width:u32,height:u32)->VideoProfile{
 VideoProfile{width,height,fps_num:30,fps_den:1,queue_capacity:RECORDING_QUEUE_CAPACITY,write_timeout:Duration::from_secs(2),stop_timeout:Duration::from_secs(8),max_lateness_ns:100_000_000,max_hold_ns:RECORDING_MAX_HOLD_NS}
}

// Measured on a real pop-out at 96 DPI: 22 px of title bar and a 1 px separator.
const DISCORD_POPOUT_TITLEBAR_TOP_PX:u32=23;
/// Pixels of Discord's own title bar to hide on a full-window pop-out binding.
pub(crate) fn window_top_trim(view:&crate::windows_window_registry::WindowView,windows:&WindowRegistry,binding:&Binding)->u32{
 if view.executable.eq_ignore_ascii_case("Discord.exe")&&crate::discord_windows::classify_discord_window(view)==crate::discord_windows::DiscordWindowClass::Popout{popout_top_trim(windows.window_dpi(&binding.window,binding.window_generation))}else{0}
}
/// The crop OBS receives: a whole pop-out loses its title bar, a chosen crop stays as chosen.
pub(crate) fn obs_crop(requested:Crop,window:(u32,u32),top_trim:u32)->Crop{
 if requested==(Crop{x:0,y:0,width:window.0,height:window.1})&&top_trim>0&&top_trim<window.1{Crop{x:0,y:top_trim,width:window.0,height:window.1-top_trim}}else{requested}
}
fn popout_top_trim(dpi:u32)->u32{
 ((DISCORD_POPOUT_TITLEBAR_TOP_PX as u64).saturating_mul(dpi as u64)/96) as u32
}

/// Trim invisible right/bottom window margins (e.g. Chromium 870x488 item vs
/// 856x481 content) and optional top chrome only when the binding represents a
/// full-window capture. Any user-chosen crop is preserved verbatim.
fn resolve_recording_crop(requested:Crop,window:(u32,u32),content:Option<(u32,u32)>,top_trim:u32)->Crop{
 if requested.x!=0||requested.y!=0||requested.width!=window.0||requested.height!=window.1{
  return requested;
 }
 let (w,h)=match content{
  Some((cw,ch)) if cw>0&&ch>0&&cw<=window.0&&ch<=window.1=>(cw,ch),
  _=>window,
 };
 if top_trim>0&&top_trim<h{
  Crop{x:0,y:top_trim,width:w,height:h-top_trim}
 }else{
  Crop{x:0,y:0,width:w,height:h}
 }
}

pub(crate) async fn prepare_feed(approval:&ExternalFfmpeg,binding:&Binding,effect_id:&str,folder:&Path)->Result<ffmpeg_recorder::Preflight,&'static str>{
 initialize_trace();
 trace_stage("encoder_preflight_begin");
 observed_pool_limits(binding.crop.width,binding.crop.height,CROP_POOL_SLOTS)?;
 let profile=recording_profile(binding.crop.width,binding.crop.height);
 // Reuse a proven trial for this exact approved binary, digest, and hardware
 // encoder across feeds and frame sizes without re-running the slow encode/decode
 // subprocess trial. `with_profile` and `Recorder::start_ready` both re-verify
 // the binary hash on disk; no software fallback is ever introduced.
 let cached=approval.verified_preflight.borrow().clone();
 if let Some(cached)=cached{
  match cached.with_profile(profile){
   Ok(reused)=>{trace_stage("encoder_preflight_ok");return Ok(reused);},
   Err(_)=>{approval.preferred_encoder.set(None);*approval.verified_preflight.borrow_mut()=None;},
  }
 }
 let mut selected=None;
 for encoder in hardware_candidates(approval.preferred_encoder.get()){let probe=folder.join(format!("probe-{effect_id}.mkv"));match ffmpeg_recorder::preflight(&approval.path,encoder,EncoderUse::operator_selected(approval.sha256.clone()),profile,&probe).await{Ok(value)=>{trace_stage("encoder_preflight_ok");approval.preferred_encoder.set(Some(encoder));*approval.verified_preflight.borrow_mut()=Some(value.clone());selected=Some(value);break;},Err(ffmpeg_recorder::RecorderError::BlockedAudit)=>{approval.preferred_encoder.set(None);*approval.verified_preflight.borrow_mut()=None;return Err("ffmpeg_audit_blocked");},Err(_)=>{},}}
 if selected.is_none(){approval.preferred_encoder.set(None);*approval.verified_preflight.borrow_mut()=None;}
 selected.ok_or("hardware_encoder_blocked")
}
/// Preserve the first-frame gate while retaining its closed rejection category.
fn first_frame_failure(state: CaptureState) -> Option<&'static str> {
 match state {
  CaptureState::Waiting | CaptureState::Healthy => None,
  CaptureState::Minimized => Some("capture_minimized"),
  CaptureState::Frozen => Some("capture_frozen"),
  CaptureState::Closed => Some("capture_closed"),
  CaptureState::DeviceLost => Some("capture_device_lost"),
  CaptureState::ResizeRequired => Some("capture_resize_required"),
  CaptureState::GenerationMismatch => Some("capture_generation_mismatch"),
  CaptureState::Backpressure => Some("capture_backpressure"),
  CaptureState::BorderUnavailable => Some("capture_border_unavailable"),
  CaptureState::SourceFailed => Some("capture_source_failed"),
  CaptureState::ClockRegression => Some("capture_clock_regression"),
 }
}

// GetLocalTime is supplied by Windows itself; no timestamp dependency or UTC fallback.
#[cfg(windows)]
fn recording_local_time()->Result<String,&'static str>{
 // SAFETY: GetLocalTime has no preconditions and returns a value.
 let value=unsafe{windows::Win32::System::SystemInformation::GetLocalTime()};
 if value.wYear<1601||!(1..=12).contains(&value.wMonth)||!(1..=31).contains(&value.wDay)||value.wHour>23||value.wMinute>59||value.wSecond>59{return Err("recorder_start_failed");}
 Ok(format!("{:04}-{:02}-{:02}_{:02}-{:02}-{:02}",value.wYear,value.wMonth,value.wDay,value.wHour,value.wMinute,value.wSecond))
}
#[cfg(not(windows))]
fn recording_local_time()->Result<String,&'static str>{Err("native_capture_unsupported")}

fn recording_published_name(label:&str,timestamp:&str,effect_id:&str,folder:&Path,journal:&Journal)->Result<String,&'static str>{
 // ASCII only, bounded below safe_name's byte ceiling; dots become underscores
 // so a label cannot create dot segments. Spaces use the existing accepted '_'.
 let label=label.trim().chars().filter_map(|c|if c.is_ascii_alphanumeric()||c=='-'||c=='_'{Some(c)}else if c.is_whitespace()||c=='.'{Some('_')}else{None}).take(80).collect::<String>();
 if label.is_empty(){return Ok(format!("{effect_id}.mkv"));}
 let stem=format!("{label}_{timestamp}");
 for suffix in 0..10000 {
  let name=if suffix==0{format!("{stem}.mkv")}else{format!("{stem}_{suffix}.mkv")};
  // Reserve against durable intents too: active recordings have no final file yet.
  if journal.records().iter().any(|record|record.kind=="media_intent"&&record.payload["name"].as_str().is_some_and(|reserved|reserved.eq_ignore_ascii_case(&name))){continue;}
  match std::fs::symlink_metadata(folder.join(&name)){
   Err(error) if error.kind()==std::io::ErrorKind::NotFound=>return Ok(name),
   Ok(_)=>continue,
   Err(_)=>return Err("disk_write_failed"),
  }
 }
 Err("disk_write_failed")
}

pub(crate) async fn build_feed(approved:ffmpeg_recorder::Preflight,binding:&Binding,effect_id:&str,label:&str,windows:&WindowRegistry,adapter:&mut NativeAdapter,folder:&Path,journal:&mut Journal)->Result<CapturePipeline<RecordingSource>,&'static str>{
 if !cfg!(windows){return Err("native_capture_unsupported");}
 if effect_id.is_empty() || effect_id.len()>96 || !effect_id.bytes().all(|b|b.is_ascii_alphanumeric()||b==b'-'||b==b'_'){return Err("recording_effect_invalid");}
 let view=windows.resolve(&binding.window,binding.window_generation).map_err(|error|match error{crate::windows_window_registry::WindowError::AmbiguousDescriptor=>"window_titles_identical",_=>"window_unavailable"})?;
 if binding.layout_revision!=view.layout_revision{return Err("window_layout_changed");}
 // Native start re-observes its capture-item size and rejects any race or
 // geometry outside these bounded registry-observed dimensions.
 let config=ObservedCaptureConfig{pool:observed_pool_limits(view.rect.width,view.rect.height,CAPTURE_POOL_SLOTS)?,health:HealthPolicy{stall_budget:RECORDING_STALL_BUDGET},borderless:false,access_timeout_ms:3000};
 trace_stage("capture_begin");
 let mut capture=NativeCapture::start_observed(windows,adapter,binding.window.clone(),binding.window_generation,config).await.map_err(|_|{trace_stage("capture_failed");"capture_start_unavailable"})?;
 trace_stage("capture_started");
 let construction=async {
  let deadline=Instant::now()+Duration::from_secs(3);
  trace_stage("first_frame_wait");
  let first=loop{if let Some(frame)=capture.take_latest(){break frame;}if Instant::now()>=deadline{return Err("capture_first_frame_timeout");}if let Some(code)=first_frame_failure(capture.poll_state(windows)){return Err(code);}tokio::time::sleep(Duration::from_millis(5)).await;};
  trace_stage("first_frame_ready");
  let top_trim=window_top_trim(&view,windows,binding);
  let crop=resolve_recording_crop(binding.crop,(view.rect.width,view.rect.height),capture.content_size(),top_trim);
  let approved=if (crop.width,crop.height)!=(approved.profile().width,approved.profile().height){
   approved.with_profile(recording_profile(crop.width,crop.height)).map_err(recorder_start_failure)?
  }else{
   approved
  };
  let pool=FramePool::new(observed_pool_limits(crop.width,crop.height,CROP_POOL_SLOTS)?).map_err(|_|"crop_resource_bound")?;
  // Verify the crop against observed pixels, not only window rectangle metadata.
  drop(crop_frame(&pool,&first,crop)?);
  let published_name=recording_published_name(label,&recording_local_time()?,effect_id,folder,journal)?;
  let plan=RecordingPlan{media_id:format!("media-{effect_id}"),intent_event:format!("intent-{effect_id}"),publish_event:format!("publish-{effect_id}"),staged_name:format!("{effect_id}.partial.mkv"),published_name};
  trace_stage("recorder_begin");
  let (mut recorder,_ready)=Recorder::start_ready(approved,folder,plan,journal).await.map_err(recorder_start_failure)?;
  recorder.enable_late_recovery();
  recorder.enable_preview(10,1).map_err(|_|"recorder_start_failed")?;
  trace_stage("recorder_ready");
  Ok((first,pool,crop,recorder))
 }.await;
 match construction{Ok((first,pool,crop,recorder))=>Ok(CapturePipeline::new_live(RecordingSource{capture,first:Some(first),pool,crop,failed:false},recorder)),Err(code)=>{trace_stage("capture_failed");let _=capture.stop();Err(code)}}
}

fn recorder_start_failure(error:ffmpeg_recorder::RecorderError)->&'static str{
 match error{
  ffmpeg_recorder::RecorderError::InvalidPath=>"folder_not_authorized",
  ffmpeg_recorder::RecorderError::WriteFailed|ffmpeg_recorder::RecorderError::DiskFull=>"disk_write_failed",
  ffmpeg_recorder::RecorderError::BlockedEncoder=>"encoder_unavailable",
  _=>"recorder_start_failed",
 }
}

#[cfg(test)]mod tests{
 use super::*;use crate::capsule_pairing::Crop;
 #[test]fn observed_pools_preserve_geometry_bounds_without_reserving_4k_for_small_windows(){
  let small=observed_pool_limits(640,360,CAPTURE_POOL_SLOTS).unwrap();
  assert_eq!(small.max_frame_bytes,640*360*4);assert_eq!((small.max_width,small.max_height),(640,360));assert_eq!(small.slots,8);
  assert_eq!(observed_pool_limits(3840,2160,8).unwrap().max_frame_bytes,3840*2160*4);
  for (width,height,slots) in [(0,1080,8),(1920,0,8),(3841,2160,8),(3840,2161,8),(1920,1080,0),(u32::MAX,u32::MAX,8)]{
   assert_eq!(observed_pool_limits(width,height,slots),Err("crop_resource_bound"));
  }
 }
 #[test]fn four_1080p_pool_profiles_fit_the_unchanged_two_gib_aggregate_bound(){
  let capture=observed_pool_limits(1920,1080,CAPTURE_POOL_SLOTS).unwrap();let crop=observed_pool_limits(1920,1080,CROP_POOL_SLOTS).unwrap();
  let bytes=4*(capture.slots*capture.max_frame_bytes+crop.slots*crop.max_frame_bytes);
  assert_eq!(bytes,1_459_814_400);assert!(bytes<2*1024*1024*1024);
 }
 #[test]fn successful_hardware_choice_is_prioritized_without_adding_software_fallback(){
  for preferred in [None,Some(Encoder::Nvenc),Some(Encoder::Amf),Some(Encoder::Qsv),Some(Encoder::Ffv1)]{
   let candidates=hardware_candidates(preferred);assert!(!candidates.contains(&Encoder::Ffv1));
   for hardware in [Encoder::Nvenc,Encoder::Amf,Encoder::Qsv]{assert_eq!(candidates.iter().filter(|candidate|**candidate==hardware).count(),1);}
   if let Some(encoder)=preferred.filter(|encoder|*encoder!=Encoder::Ffv1){assert_eq!(candidates[0],encoder);}
  }
 }
 #[test]fn recorder_start_causes_remain_closed_without_inventing_session_exhaustion(){
  use ffmpeg_recorder::RecorderError;
  assert_eq!(recorder_start_failure(RecorderError::InvalidPath),"folder_not_authorized");
  assert_eq!(recorder_start_failure(RecorderError::DiskFull),"disk_write_failed");
  assert_eq!(recorder_start_failure(RecorderError::BlockedEncoder),"encoder_unavailable");
  assert_eq!(recorder_start_failure(RecorderError::ChildExit),"recorder_start_failed");
 }
 #[test]fn first_frame_states_keep_the_exact_admission_boundary(){
  assert_eq!(first_frame_failure(CaptureState::Waiting),None);
  assert_eq!(first_frame_failure(CaptureState::Healthy),None);
  for (state,expected) in [
   (CaptureState::Minimized,"capture_minimized"),
   (CaptureState::Frozen,"capture_frozen"),
   (CaptureState::Closed,"capture_closed"),
   (CaptureState::DeviceLost,"capture_device_lost"),
   (CaptureState::ResizeRequired,"capture_resize_required"),
   (CaptureState::GenerationMismatch,"capture_generation_mismatch"),
   (CaptureState::Backpressure,"capture_backpressure"),
   (CaptureState::BorderUnavailable,"capture_border_unavailable"),
   (CaptureState::SourceFailed,"capture_source_failed"),
   (CaptureState::ClockRegression,"capture_clock_regression"),
  ]{assert_eq!(first_frame_failure(state),Some(expected));}
 }
 #[test]fn crop_uses_actual_rows_and_source_clock(){
  let original=FramePool::new(PoolLimits{slots:1,max_frame_bytes:48,max_width:4,max_height:3}).unwrap();
  let pixels=(0u8..48).collect::<Vec<_>>();let frame=original.copy_from(BorrowedFrame{data:&pixels,width:4,height:3,source_stride:16,source_time:Clock(88)}).unwrap();
  let pool=FramePool::new(PoolLimits{slots:2,max_frame_bytes:16,max_width:2,max_height:2}).unwrap();
  let crop=Crop{x:1,y:1,width:2,height:2};let result=crop_frame(&pool,&frame,crop).unwrap();assert_eq!(result.pixels(),[20,21,22,23,24,25,26,27,36,37,38,39,40,41,42,43]);assert_eq!(result.source_time(),Clock(88));
  assert!(crop_frame(&pool,&frame,Crop{x:3,..crop}).is_err());assert!(crop_frame(&pool,&frame,Crop{x:u32::MAX,..crop}).is_err());
 }
 #[test]fn selection_requires_actual_digest_and_nonlink_file(){
  struct Cleanup(PathBuf);impl Drop for Cleanup{fn drop(&mut self){let _=std::fs::remove_dir_all(&self.0);}}
  let root=std::env::temp_dir().join(format!("ffmpeg-selection-{}",std::process::id()));std::fs::create_dir(&root).unwrap();let _cleanup=Cleanup(root.clone());let path=root.join("tool.bin");std::fs::write(&path,b"synthetic binary").unwrap();
  let result=(||{let hash=crate::authority_journal::sha256_hex(b"synthetic binary");let approval=ExternalFfmpeg::select(&path,&hash).unwrap();assert!(!approval.encoder_ready());assert!(ExternalFfmpeg::select(&path,&"0".repeat(64)).is_err());assert!(ExternalFfmpeg::select(Path::new("tool.bin"),&hash).is_err());})();std::fs::remove_dir_all(root).unwrap();result
 }
 #[test]fn full_window_crop_trims_to_content_size_while_preserving_user_crop(){
  let full=Crop{x:0,y:0,width:870,height:488};
  assert_eq!(resolve_recording_crop(full,(870,488),Some((856,481)),0),Crop{x:0,y:0,width:856,height:481});
  assert_eq!(resolve_recording_crop(full,(870,488),None,0),full);
  assert_eq!(resolve_recording_crop(full,(870,488),Some((870,488)),0),full);
  assert_eq!(resolve_recording_crop(full,(870,488),Some((856,481)),32),Crop{x:0,y:32,width:856,height:449});
  assert_eq!(resolve_recording_crop(full,(870,488),None,32),Crop{x:0,y:32,width:870,height:456});
  assert_eq!(resolve_recording_crop(full,(870,488),Some((856,481)),481),Crop{x:0,y:0,width:856,height:481});
  let user_inside=Crop{x:10,y:12,width:640,height:360};
  assert_eq!(resolve_recording_crop(user_inside,(870,488),Some((856,481)),0),user_inside);
  assert_eq!(resolve_recording_crop(user_inside,(870,488),Some((856,481)),32),user_inside);
  let user_top_left=Crop{x:0,y:0,width:856,height:481};
  assert_eq!(resolve_recording_crop(user_top_left,(870,488),Some((856,481)),0),user_top_left);
  assert_eq!(resolve_recording_crop(user_top_left,(870,488),Some((856,481)),32),user_top_left);
 }
 #[test]fn obs_crop_hides_the_bar_only_on_a_whole_window(){
  let full=Crop{x:0,y:0,width:870,height:488};
  assert_eq!(obs_crop(full,(870,488),23),Crop{x:0,y:23,width:870,height:465});
  assert_eq!(obs_crop(full,(870,488),0),full);
  let chosen=Crop{x:10,y:0,width:100,height:100};
  assert_eq!(obs_crop(chosen,(870,488),23),chosen);
 }
 #[test]fn readable_names_reserve_disk_and_journal_names_and_survive_reopen(){
  let root=std::env::temp_dir().join(format!("cafe-readable-{}-{}",std::process::id(),std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()));std::fs::create_dir(&root).unwrap();
  struct Cleanup(PathBuf);impl Drop for Cleanup{fn drop(&mut self){let _=std::fs::remove_dir_all(&self.0);}}let _cleanup=Cleanup(root.clone());
  let path=root.join("journal");let mut journal=Journal::open(&path,"readable",Clock(77)).unwrap();
  let timestamp="2026-11-02_14-05-06";
  let name=recording_published_name(" Guest One ",timestamp,"rec-fixed-0",&root,&journal).unwrap();assert_eq!(name,"Guest_One_2026-11-02_14-05-06.mkv");
  std::fs::write(root.join(&name),b"existing").unwrap();
  let reserved=recording_published_name("Guest One",timestamp,"rec-fixed-0",&root,&journal).unwrap();assert_eq!(reserved,"Guest_One_2026-11-02_14-05-06_1.mkv");
  journal.append_media_intent("intent-rec-fixed-0","media-rec-fixed-0",&reserved).unwrap();drop(journal);
  let mut journal=Journal::open(&path,"readable",Clock(77)).unwrap();
  assert_eq!(recording_published_name("Guest One",timestamp,"rec-fixed-1",&root,&journal).unwrap(),"Guest_One_2026-11-02_14-05-06_2.mkv");
  assert_eq!(recording_published_name("",timestamp,"rec-fixed-0",&root,&journal).unwrap(),"rec-fixed-0.mkv");
  let sanitized=recording_published_name("../A: B/é",timestamp,"rec-fixed-1",&root,&journal).unwrap();assert!(!sanitized.contains(".."));assert!(sanitized.bytes().all(|b|b.is_ascii_alphanumeric()||matches!(b,b'_'|b'-'|b'.')));
  let long=recording_published_name(&"X".repeat(128),timestamp,"rec-fixed-1",&root,&journal).unwrap();assert!(long.len()<=128);
  std::fs::write(root.join("rec-fixed-0.partial.mkv"),b"synthetic media").unwrap();
  journal.publish_media("publish-rec-fixed-0",&root,"media-rec-fixed-0","rec-fixed-0.partial.mkv").unwrap();
  assert_eq!(std::fs::read(root.join(&name)).unwrap(),b"existing");assert_eq!(std::fs::read(root.join(&reserved)).unwrap(),b"synthetic media");drop(journal);
  let journal=Journal::open(&path,"readable",Clock(77)).unwrap();assert_eq!(journal.records().last().unwrap().payload["name"],reserved);
 }
 #[cfg(windows)] #[test]fn local_recording_timestamp_has_safe_wall_clock_shape(){let stamp=recording_local_time().unwrap();assert_eq!(stamp.len(),19);assert_eq!(&stamp[10..11],"_");assert!(stamp.bytes().all(|b|b.is_ascii_digit()||b==b'-'||b==b'_'));}
 #[test]fn popout_top_trim_scales_from_base_constant_by_dpi(){
  assert_eq!(popout_top_trim(96),23);assert_eq!(popout_top_trim(144),34);
  for dpi in [0,96,120,144,192]{
   assert_eq!(popout_top_trim(dpi),(DISCORD_POPOUT_TITLEBAR_TOP_PX*dpi)/96);
  }
 }
 #[test]fn cached_preflight_skips_repeat_trial_across_sizes_and_invalidates_on_tamper(){
  struct Cleanup(PathBuf);impl Drop for Cleanup{fn drop(&mut self){let _=std::fs::remove_dir_all(&self.0);}}
  let root=std::env::temp_dir().join(format!("ffmpeg-reuse-{}",std::process::id()));std::fs::create_dir(&root).unwrap();let _cleanup=Cleanup(root.clone());
  let path=root.join("tool.bin");std::fs::write(&path,b"approved synthetic binary").unwrap();
  let hash=crate::authority_journal::sha256_hex(b"approved synthetic binary");
  let approval=ExternalFfmpeg::select(&path,&hash).unwrap();
  let seeded=ffmpeg_recorder::Preflight::test_synthetic(path.clone(),hash.clone(),Encoder::Nvenc,recording_profile(1280,720));
  approval.preferred_encoder.set(Some(Encoder::Nvenc));
  *approval.verified_preflight.borrow_mut()=Some(seeded);
  let runtime=tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
  let binding=Binding{capsule_id:"cap-1".into(),person_label:"guest".into(),feed_id:"feed-1".into(),role:crate::capsule_registry::Role::Game,window:crate::windows_window_registry::WindowRegistry::new(Clock(1)).refresh_fixtures(&[crate::windows_window_registry::FixtureWindow{slot:"s".into(),incarnation:1,process_id:2,process_birth:1,executable:"Discord.exe".into(),title:"Popout".into(),class:"Chrome".into(),rect:crate::windows_window_registry::Rect{x:0,y:0,width:856,height:481},visible:true,minimized:false,layout:"l".into()}]).unwrap().remove(0).handle,window_generation:Clock(1),layout_revision:Clock(1),crop:Crop{x:0,y:0,width:856,height:481},feed_generation:Clock(1)};
  let reused=runtime.block_on(prepare_feed(&approval,&binding,"eff-1",&root)).unwrap();
  assert_eq!(reused.encoder(),Encoder::Nvenc);
  assert_eq!((reused.profile().width,reused.profile().height),(856,481));
  std::fs::write(&path,b"tampered binary").unwrap();
  assert!(runtime.block_on(prepare_feed(&approval,&binding,"eff-2",&root)).is_err());
  assert!(!approval.encoder_ready());
  assert!(approval.verified_preflight.borrow().is_none());
 }
}
