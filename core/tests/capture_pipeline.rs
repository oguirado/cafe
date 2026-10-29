//! Independent frame-to-child observations; native Discord verification is separate.
#[path="../src/capsule_registry.rs"] mod capsule_registry;
#[path="../src/authority_journal.rs"] mod authority_journal;
#[path="../src/journal_replay.rs"] mod journal_replay;
#[path="../src/capture_frame_pool.rs"] mod capture_frame_pool;
#[path="../src/capture_health.rs"] mod capture_health;
#[path="../src/windows_window_registry.rs"] mod windows_window_registry;
#[path="../src/windows_graphics_capture.rs"] mod windows_graphics_capture;
#[path="../src/recorder_pacer.rs"] mod recorder_pacer;
#[path="../src/ffmpeg_recorder.rs"] mod ffmpeg_recorder;
#[path="../src/capture_pipeline.rs"] mod capture_pipeline;
#[cfg(windows)]
#[path="../src/windows_journal_policy.rs"] mod windows_journal_policy;
use std::{path::PathBuf,sync::{Arc,atomic::{AtomicBool,AtomicU64,Ordering}},time::{Duration,Instant}};
use capsule_registry::Clock;
use authority_journal::Journal;
use capture_frame_pool::{FramePool,PoolLimits,BorrowedFrame,OwnedFrame};
use capture_health::CaptureState;
use windows_graphics_capture::CaptureError;
use windows_window_registry::WindowRegistry;
use ffmpeg_recorder::{Recorder,RecordingState,RecordingPlan,VideoProfile};
use capture_pipeline::{CapturePipeline,FrameSource,Tick};
static SERIAL:AtomicU64=AtomicU64::new(0);
struct Scratch(PathBuf);
impl Scratch{fn new()->Self{let p=std::env::temp_dir().join(format!("cafe-pipeline-{}-{}",std::process::id(),SERIAL.fetch_add(1,Ordering::SeqCst)));std::fs::create_dir(&p).unwrap();Self(p)}}
impl Drop for Scratch{fn drop(&mut self){std::fs::remove_dir_all(&self.0).unwrap();}}
struct Source{frame:Option<OwnedFrame>,state:CaptureState,stopped:Arc<AtomicBool>,fail_stop:bool}
impl FrameSource for Source{
 fn take_latest(&mut self)->Option<OwnedFrame>{self.frame.take()}
 fn state(&self,_:&WindowRegistry)->CaptureState{self.state}
 fn stop(&mut self)->Result<(),CaptureError>{self.stopped.store(true,Ordering::SeqCst);if self.fail_stop{Err(CaptureError::Unsupported)}else{Ok(())}}
}
fn runtime()->tokio::runtime::Runtime{tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap()}
fn profile()->VideoProfile{VideoProfile{width:16,height:16,fps_num:25,fps_den:1,queue_capacity:4,write_timeout:Duration::from_secs(2),stop_timeout:Duration::from_secs(4),max_lateness_ns:1_000_000_000,max_hold_ns:1_000_000_000}}
fn plan()->RecordingPlan{RecordingPlan{media_id:"pipeline-game".into(),intent_event:"intent".into(),publish_event:"published".into(),staged_name:"segment.partial.mkv".into(),published_name:"segment.mkv".into()}}
fn source(state:CaptureState,with_frame:bool,fail_stop:bool)->Source{
 let p=profile();let pool=FramePool::new(PoolLimits{slots:1,max_frame_bytes:1024,max_width:16,max_height:16}).unwrap();
 let frame=if with_frame{Some(pool.copy_from(BorrowedFrame{data:&[11,22,33,255].repeat(256),width:p.width,height:p.height,source_stride:64,source_time:Clock(1)}).unwrap())}else{None};
 Source{frame,state,stopped:Arc::new(AtomicBool::new(false)),fail_stop}
}
#[test]
fn recorder_child_fixture(){if let Ok(mode)=std::env::var("CAFE_RECORDER_FIXTURE_MODE"){match mode.as_str(){"drain"=>{let _=std::io::copy(&mut std::io::stdin(),&mut std::io::sink());},"crash"=>std::process::exit(19),_=>std::process::exit(23)}}}

#[test]
fn waiting_never_claims_a_frame_or_receipt(){runtime().block_on(async{
 let tmp=Scratch::new();let mut journal=Journal::open(&tmp.0.join("authority.journal"),"synthetic",Clock(1)).unwrap();let now=Instant::now();
 let recorder=Recorder::test_with_child("drain",&tmp.0,plan(),profile(),&mut journal,now).unwrap();let mut pipeline=CapturePipeline::new(source(CaptureState::Waiting,false,false),recorder);let registry=WindowRegistry::new(Clock(1));
 assert!(matches!(pipeline.tick(&registry,now).await,Ok(Tick::Waiting)));assert_eq!(pipeline.recorder_counters().written_frames,0);assert_eq!(pipeline.recorder_state(),RecordingState::Recording);
 assert!(pipeline.stop(&mut journal).await.is_err());assert_ne!(pipeline.recorder_state(),RecordingState::Complete);assert_eq!(journal.records().len(),1);
});}

#[test]
fn unhealthy_source_is_rejected_before_writing(){runtime().block_on(async{
 for state in [CaptureState::Waiting,CaptureState::Closed,CaptureState::DeviceLost,CaptureState::GenerationMismatch,CaptureState::BorderUnavailable,CaptureState::Frozen]{
  let tmp=Scratch::new();let mut journal=Journal::open(&tmp.0.join("authority.journal"),"synthetic",Clock(1)).unwrap();let now=Instant::now();let recorder=Recorder::test_with_child("drain",&tmp.0,plan(),profile(),&mut journal,now).unwrap();
  let src=source(state,true,false);let stopped=src.stopped.clone();let mut pipeline=CapturePipeline::new(src,recorder);
  assert!(pipeline.tick(&WindowRegistry::new(Clock(1)),now).await.is_err());assert!(stopped.load(Ordering::SeqCst));assert_eq!(pipeline.recorder_counters().written_frames,0);assert_eq!(pipeline.recorder_state(),RecordingState::Interrupted);assert!(pipeline.stop(&mut journal).await.is_err());assert_eq!(journal.records().len(),1);
 }
});}

#[test]
fn source_stop_failure_cannot_publish_completion(){runtime().block_on(async{
 let tmp=Scratch::new();let mut journal=Journal::open(&tmp.0.join("authority.journal"),"synthetic",Clock(1)).unwrap();let now=Instant::now();let recorder=Recorder::test_with_child("drain",&tmp.0,plan(),profile(),&mut journal,now).unwrap();let mut pipeline=CapturePipeline::new(source(CaptureState::Healthy,true,true),recorder);
 assert!(matches!(pipeline.tick(&WindowRegistry::new(Clock(1)),now).await,Ok(Tick::Written)));assert_eq!(pipeline.recorder_counters().written_bytes,1024);
 assert!(matches!(pipeline.stop(&mut journal).await,Err(capture_pipeline::PipelineError::Source)));assert_eq!(pipeline.recorder_state(),RecordingState::Interrupted);assert_eq!(journal.records().len(),1);assert!(!tmp.0.join("segment.mkv").exists());
 assert!(pipeline.tick(&WindowRegistry::new(Clock(1)),now+Duration::from_millis(40)).await.is_err());assert_eq!(pipeline.recorder_counters().written_frames,1);
});}

#[test]
fn child_failure_never_produces_a_complete_pipeline(){runtime().block_on(async{
 let tmp=Scratch::new();let mut journal=Journal::open(&tmp.0.join("authority.journal"),"synthetic",Clock(1)).unwrap();let now=Instant::now();let recorder=Recorder::test_with_child("crash",&tmp.0,plan(),profile(),&mut journal,now).unwrap();let mut pipeline=CapturePipeline::new(source(CaptureState::Healthy,true,false),recorder);
 let _=pipeline.tick(&WindowRegistry::new(Clock(1)),now).await;assert!(pipeline.stop(&mut journal).await.is_err());assert_ne!(pipeline.recorder_state(),RecordingState::Complete);assert_eq!(journal.records().len(),1);
});}

#[cfg(unix)]
#[test]
fn actual_ffmpeg_receipt_follows_source_stop_and_decoded_artifact(){runtime().block_on(async{
 use ffmpeg_recorder::{preflight,Encoder,EncoderUse};
 let binary=std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()).map(|p|p.join("ffmpeg")).find(|p|p.is_file()).expect("BLOCKED: development FFmpeg absent");let tmp=Scratch::new();let p=profile();let approval=preflight(&binary,Encoder::Ffv1,EncoderUse::Development,p,&tmp.0.join("probe.mkv")).await.unwrap();
 let mut journal=Journal::open(&tmp.0.join("authority.journal"),"synthetic",Clock(1)).unwrap();let now=Instant::now();let recorder=Recorder::start(approval,&tmp.0,plan(),&mut journal,now).await.unwrap();let src=source(CaptureState::Healthy,true,false);let stopped=src.stopped.clone();let mut pipeline=CapturePipeline::new(src,recorder);
 assert!(matches!(pipeline.tick(&WindowRegistry::new(Clock(1)),now).await,Ok(Tick::Written)));let receipt=pipeline.stop(&mut journal).await.unwrap();assert!(stopped.load(Ordering::SeqCst));assert_eq!(receipt.frames,1);assert_eq!(receipt.bytes,1024);assert_eq!(pipeline.recorder_state(),RecordingState::Complete);assert_eq!(journal.records().len(),2);assert_eq!(journal.records()[1].kind,"media_published");
 assert!(pipeline.tick(&WindowRegistry::new(Clock(1)),now+Duration::from_millis(40)).await.is_err());
});}

#[test]
fn explicit_abort_never_publishes_even_after_a_written_frame(){runtime().block_on(async{
 let tmp=Scratch::new();let mut journal=Journal::open(&tmp.0.join("authority.journal"),"synthetic",Clock(1)).unwrap();let now=Instant::now();
 let recorder=Recorder::test_with_child("drain",&tmp.0,plan(),profile(),&mut journal,now).unwrap();let src=source(CaptureState::Healthy,true,false);let stopped=src.stopped.clone();let mut pipeline=CapturePipeline::new(src,recorder);
 assert!(matches!(pipeline.tick(&WindowRegistry::new(Clock(1)),now).await,Ok(Tick::Written)));
 pipeline.abort().await.unwrap();assert!(stopped.load(Ordering::SeqCst));assert_eq!(pipeline.recorder_state(),RecordingState::Interrupted);
 assert!(pipeline.stop(&mut journal).await.is_err());assert_eq!(journal.records().len(),1);assert!(!tmp.0.join("segment.mkv").exists());
});}
#[test]
fn failed_source_abort_still_interrupts_its_own_recorder(){runtime().block_on(async{
 let tmp=Scratch::new();let mut journal=Journal::open(&tmp.0.join("authority.journal"),"synthetic",Clock(1)).unwrap();let now=Instant::now();
 let recorder=Recorder::test_with_child("drain",&tmp.0,plan(),profile(),&mut journal,now).unwrap();let mut pipeline=CapturePipeline::new(source(CaptureState::Healthy,false,true),recorder);
 assert!(matches!(pipeline.abort().await,Err(capture_pipeline::PipelineError::Source)));assert_eq!(pipeline.recorder_state(),RecordingState::Interrupted);
 assert!(pipeline.stop(&mut journal).await.is_err());assert_eq!(journal.records().len(),1);assert!(!tmp.0.join("segment.mkv").exists());
});}
