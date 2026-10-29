//! Owned runtime construction: real native frames, approved hardware encoder,
//! bounded crop copies, existing P11 recorder. No renderer-supplied argv.
use std::{path::{Path,PathBuf,Component},time::{Duration,Instant},sync::Arc};
use crate::{authority_journal::Journal,capsule_registry::Clock,capsule_pairing::Binding,
 capture_frame_pool::{FramePool,PoolLimits,OwnedFrame,BorrowedFrame},capture_health::{CaptureState,HealthPolicy},
 capture_pipeline::{CapturePipeline,FrameSource},windows_graphics_capture::{NativeCapture,ObservedCaptureConfig,CaptureError},
 windows_window_registry::{WindowRegistry,NativeAdapter},ffmpeg_recorder::{self,Encoder,EncoderUse,VideoProfile,Recorder,RecordingPlan}};

/// Operator-selected external binary. Approval is checked again by preflight and
/// at spawn; this object never substitutes a system binary or software codec.
pub(crate) struct ExternalFfmpeg {path:PathBuf,sha256:String}
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
  Ok(Self{path:path.to_path_buf(),sha256:sha256.into()})
 }
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

pub(crate) async fn prepare_feed(approval:&ExternalFfmpeg,binding:&Binding,effect_id:&str,folder:&Path)->Result<ffmpeg_recorder::Preflight,&'static str>{
 if binding.crop.width==0||binding.crop.height==0||binding.crop.width>3840||binding.crop.height>2160{return Err("crop_resource_bound");}
  let profile=VideoProfile{width:binding.crop.width,height:binding.crop.height,fps_num:60,fps_den:1,queue_capacity:3,write_timeout:Duration::from_secs(2),stop_timeout:Duration::from_secs(8),max_lateness_ns:100_000_000,max_hold_ns:250_000_000};
  let mut selected=None;
  // Every hardware candidate must actually encode and decode. No ffv1/x264 fallback.
  for encoder in [Encoder::Nvenc,Encoder::Amf,Encoder::Qsv]{let probe=folder.join(format!("probe-{effect_id}.mkv"));if let Ok(value)=ffmpeg_recorder::preflight(&approval.path,encoder,EncoderUse::Production{audited_sha256:approval.sha256.clone()},profile,&probe).await{selected=Some(value);break;}}
  selected.ok_or("hardware_encoder_blocked")

}
pub(crate) async fn build_feed(approved:ffmpeg_recorder::Preflight,binding:&Binding,effect_id:&str,windows:&WindowRegistry,adapter:&mut NativeAdapter,folder:&Path,journal:&mut Journal)->Result<CapturePipeline<RecordingSource>,&'static str>{
 if !cfg!(windows){return Err("native_capture_unsupported");}
 if effect_id.is_empty() || effect_id.len()>96 || !effect_id.bytes().all(|b|b.is_ascii_alphanumeric()||b==b'-'||b==b'_'){return Err("recording_effect_invalid");}
 let view=windows.resolve(&binding.window,binding.window_generation).map_err(|_|"window_reconfirmation_required")?;
 if binding.layout_revision!=view.layout_revision{return Err("window_layout_changed");}
 let config=ObservedCaptureConfig{pool:PoolLimits{slots:8,max_frame_bytes:3840*2160*4,max_width:3840,max_height:2160},health:HealthPolicy{stall_budget:Clock(2_000_000_000)},borderless:false,access_timeout_ms:3000};
 let mut capture=NativeCapture::start_observed(windows,adapter,binding.window.clone(),binding.window_generation,config).await.map_err(|_|"capture_start_unavailable")?;
 let construction=async {
  let deadline=Instant::now()+Duration::from_secs(3);
  let first=loop{if let Some(frame)=capture.take_latest(){break frame;}if Instant::now()>=deadline{return Err("capture_first_frame_timeout");}match capture.poll_state(windows){CaptureState::Waiting|CaptureState::Healthy=>{},_=>return Err("capture_not_healthy")};tokio::time::sleep(Duration::from_millis(5)).await;};
  let pool=FramePool::new(PoolLimits{slots:4,max_frame_bytes:(binding.crop.width as usize).checked_mul(binding.crop.height as usize).and_then(|v|v.checked_mul(4)).filter(|v|*v<=3840*2160*4).ok_or("crop_resource_bound")?,max_width:binding.crop.width,max_height:binding.crop.height}).map_err(|_|"crop_resource_bound")?;
  // Verify the crop against observed pixels, not only window rectangle metadata.
  drop(crop_frame(&pool,&first,binding.crop)?);
  let plan=RecordingPlan{media_id:format!("media-{effect_id}"),intent_event:format!("intent-{effect_id}"),publish_event:format!("publish-{effect_id}"),staged_name:format!("{effect_id}.partial.mkv"),published_name:format!("{effect_id}.mkv")};
  let mut recorder=Recorder::start(approved,folder,plan,journal,Instant::now()).await.map_err(|_|"recorder_start_failed")?;
  recorder.enable_preview(10,1).map_err(|_|"recorder_start_failed")?;
  Ok((first,pool,recorder))
 }.await;
 match construction{Ok((first,pool,recorder))=>Ok(CapturePipeline::new(RecordingSource{capture,first:Some(first),pool,crop:binding.crop,failed:false},recorder)),Err(code)=>{let _=capture.stop();Err(code)}}
}

#[cfg(test)]mod tests{
 use super::*;use crate::capsule_pairing::Crop;
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
  let result=(||{let hash=crate::authority_journal::sha256_hex(b"synthetic binary");assert!(ExternalFfmpeg::select(&path,&hash).is_ok());assert!(ExternalFfmpeg::select(&path,&"0".repeat(64)).is_err());assert!(ExternalFfmpeg::select(Path::new("tool.bin"),&hash).is_err());})();std::fs::remove_dir_all(root).unwrap();result
 }
}
