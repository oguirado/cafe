//! Runtime effects manager observations; native capture verification is separate.
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
#[path="../src/runtime_effects.rs"] mod runtime_effects;
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
use capture_pipeline::{CapturePipeline,FrameSource};
use runtime_effects::{RuntimeEffects,EngineError,CapsuleState};
static SERIAL:AtomicU64=AtomicU64::new(0);
struct Scratch(PathBuf);
impl Scratch{fn new()->Self{let p=std::env::temp_dir().join(format!("cafe-effects-{}-{}",std::process::id(),SERIAL.fetch_add(1,Ordering::SeqCst)));std::fs::create_dir(&p).unwrap();Self(p)}}
impl Drop for Scratch{fn drop(&mut self){std::fs::remove_dir_all(&self.0).unwrap();}}
struct Source{frame:Option<OwnedFrame>,state:CaptureState,stopped:Arc<AtomicBool>,fail_stop:bool}
impl FrameSource for Source{
 fn take_latest(&mut self)->Option<OwnedFrame>{self.frame.take()}
 fn state(&self,_:&WindowRegistry)->CaptureState{self.state}
 fn stop(&mut self)->Result<(),CaptureError>{self.stopped.store(true,Ordering::SeqCst);if self.fail_stop{Err(CaptureError::Unsupported)}else{Ok(())}}
}
fn runtime()->tokio::runtime::Runtime{tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap()}
fn profile()->VideoProfile{VideoProfile{width:16,height:16,fps_num:25,fps_den:1,queue_capacity:4,write_timeout:Duration::from_secs(2),stop_timeout:Duration::from_secs(4),max_lateness_ns:1_000_000_000,max_hold_ns:1_000_000_000}}
fn plan_for(tag:&str)->RecordingPlan{RecordingPlan{media_id:format!("media-{tag}"),intent_event:format!("intent-{tag}"),publish_event:format!("published-{tag}"),staged_name:format!("seg-{tag}.partial.mkv"),published_name:format!("seg-{tag}.mkv")}}
fn source(state:CaptureState,with_frame:bool,fail_stop:bool)->Source{
 let p=profile();let pool=FramePool::new(PoolLimits{slots:1,max_frame_bytes:1024,max_width:16,max_height:16}).unwrap();
 let frame=if with_frame{Some(pool.copy_from(BorrowedFrame{data:&[11,22,33,255].repeat(256),width:p.width,height:p.height,source_stride:64,source_time:Clock(1)}).unwrap())}else{None};
 Source{frame,state,stopped:Arc::new(AtomicBool::new(false)),fail_stop}
}
#[test]
fn recorder_child_fixture(){if let Ok(mode)=std::env::var("CAFE_RECORDER_FIXTURE_MODE"){match mode.as_str(){"drain"=>{let _=std::io::copy(&mut std::io::stdin(),&mut std::io::sink());},"crash"=>std::process::exit(19),_=>std::process::exit(23)}}}

#[test]
fn success_attach_and_tick_counts(){runtime().block_on(async{
 let tmp=Scratch::new();let mut journal=Journal::open(&tmp.0.join("authority.journal"),"synthetic",Clock(1)).unwrap();let now=Instant::now();
 let mut engine:RuntimeEffects<Source>=RuntimeEffects::new(2,4).unwrap();
 let recorder=Recorder::test_with_child("drain",&tmp.0,plan_for("t1a"),profile(),&mut journal,now).unwrap();
 let pipeline=CapturePipeline::new(source(CaptureState::Healthy,true,false),recorder);
 engine.attach_capsule("cap-success".into(),vec![("feed-1".into(),pipeline)]).await.unwrap();
 let registry=WindowRegistry::new(Clock(1));
 engine.tick_all(&registry,now).await.unwrap();
 let status=engine.status();
 assert_eq!(status.len(),1);assert_eq!(status[0].capsule_id,"cap-success");assert_eq!(status[0].state,CapsuleState::Running);
 assert_eq!(status[0].feeds.len(),1);assert_eq!(status[0].feeds[0].feed_id,"feed-1");
 assert_eq!(status[0].feeds[0].state,RecordingState::Recording);
 assert_eq!(status[0].feeds[0].counters.written_frames,1);assert_eq!(status[0].feeds[0].counters.written_bytes,1024);
 assert_eq!(journal.records().len(),1);assert!(!tmp.0.join("seg-t1a.mkv").exists());
 engine.abort_all().await.unwrap();
});}

#[test]
fn duplicate_attach_cannot_damage_existing_batch(){runtime().block_on(async{
 let tmp=Scratch::new();let mut journal=Journal::open(&tmp.0.join("authority.journal"),"synthetic",Clock(1)).unwrap();let now=Instant::now();
 let mut engine:RuntimeEffects<Source>=RuntimeEffects::new(2,4).unwrap();
 let recorder=Recorder::test_with_child("drain",&tmp.0,plan_for("t2a"),profile(),&mut journal,now).unwrap();
 engine.attach_capsule("cap-a".into(),vec![("feed-1".into(),CapturePipeline::new(source(CaptureState::Healthy,true,false),recorder))]).await.unwrap();
 engine.tick_all(&WindowRegistry::new(Clock(1)),now).await.unwrap();
 let incoming_stopped;
 {
  let recorder2=Recorder::test_with_child("drain",&tmp.0,plan_for("t2dup"),profile(),&mut journal,now).unwrap();
  let src=source(CaptureState::Healthy,true,false);incoming_stopped=src.stopped.clone();
  let err=engine.attach_capsule("cap-a".into(),vec![("feed-9".into(),CapturePipeline::new(src,recorder2))]).await.unwrap_err();
  assert_eq!(err,EngineError::DuplicateCapsule);
 }
 assert!(incoming_stopped.load(Ordering::SeqCst));
 let status=engine.status();
 assert_eq!(status.len(),1);assert_eq!(status[0].state,CapsuleState::Running);
 assert_eq!(status[0].feeds[0].state,RecordingState::Recording);
 assert_eq!(status[0].feeds[0].counters.written_frames,1);assert_eq!(status[0].feeds[0].counters.written_bytes,1024);
 assert_eq!(journal.records().len(),2);
 engine.tick_all(&WindowRegistry::new(Clock(1)),now).await.unwrap();
 assert_eq!(engine.status()[0].feeds[0].counters.written_frames,1);
 engine.abort_all().await.unwrap();
});}

#[test]
fn empty_feeds_rejected(){runtime().block_on(async{
 let mut engine:RuntimeEffects<Source>=RuntimeEffects::new(2,4).unwrap();
 let err=engine.attach_capsule("cap-empty".into(),vec![]).await.unwrap_err();
 assert_eq!(err,EngineError::EmptyFeeds);assert!(engine.status().is_empty());
});}

#[test]
fn duplicate_feed_keys_rejected_and_aborts_incoming(){runtime().block_on(async{
 let tmp=Scratch::new();let mut journal=Journal::open(&tmp.0.join("authority.journal"),"synthetic",Clock(1)).unwrap();let now=Instant::now();
 let mut engine:RuntimeEffects<Source>=RuntimeEffects::new(2,4).unwrap();
 let r1=Recorder::test_with_child("drain",&tmp.0,plan_for("t4a"),profile(),&mut journal,now).unwrap();
 let s1=source(CaptureState::Healthy,true,false);let stopped1=s1.stopped.clone();
 let r2=Recorder::test_with_child("drain",&tmp.0,plan_for("t4b"),profile(),&mut journal,now).unwrap();
 let s2=source(CaptureState::Healthy,true,false);let stopped2=s2.stopped.clone();
 let err=engine.attach_capsule("cap-dupkey".into(),vec![("feed-1".into(),CapturePipeline::new(s1,r1)),("feed-1".into(),CapturePipeline::new(s2,r2))]).await.unwrap_err();
 assert_eq!(err,EngineError::DuplicateFeed);
 assert!(stopped1.load(Ordering::SeqCst));assert!(stopped2.load(Ordering::SeqCst));
 assert!(engine.status().is_empty());assert_eq!(journal.records().len(),2);
});}

#[test]
fn capacity_overflow_rejected_and_aborts_incoming(){runtime().block_on(async{
 {
  let tmp=Scratch::new();let mut journal=Journal::open(&tmp.0.join("authority.journal"),"synthetic",Clock(1)).unwrap();let now=Instant::now();
  let mut engine:RuntimeEffects<Source>=RuntimeEffects::new(1,4).unwrap();
  let r1=Recorder::test_with_child("drain",&tmp.0,plan_for("t5a"),profile(),&mut journal,now).unwrap();
  engine.attach_capsule("cap-first".into(),vec![("feed-1".into(),CapturePipeline::new(source(CaptureState::Healthy,true,false),r1))]).await.unwrap();
  let r2=Recorder::test_with_child("drain",&tmp.0,plan_for("t5b"),profile(),&mut journal,now).unwrap();
  let s2=source(CaptureState::Healthy,true,false);let stopped2=s2.stopped.clone();
  let err=engine.attach_capsule("cap-second".into(),vec![("feed-1".into(),CapturePipeline::new(s2,r2))]).await.unwrap_err();
  assert_eq!(err,EngineError::CapacityExceeded);assert!(stopped2.load(Ordering::SeqCst));
  assert_eq!(engine.status().len(),1);assert_eq!(engine.status()[0].capsule_id,"cap-first");
  engine.abort_all().await.unwrap();
 }
 {
  let tmp=Scratch::new();let mut journal=Journal::open(&tmp.0.join("authority.journal"),"synthetic",Clock(1)).unwrap();let now=Instant::now();
  let mut engine:RuntimeEffects<Source>=RuntimeEffects::new(4,1).unwrap();
  let r1=Recorder::test_with_child("drain",&tmp.0,plan_for("t5c"),profile(),&mut journal,now).unwrap();
  engine.attach_capsule("cap-first".into(),vec![("feed-1".into(),CapturePipeline::new(source(CaptureState::Healthy,true,false),r1))]).await.unwrap();
  let r2=Recorder::test_with_child("drain",&tmp.0,plan_for("t5d"),profile(),&mut journal,now).unwrap();
  let s2=source(CaptureState::Healthy,true,false);let stopped2=s2.stopped.clone();
  let err=engine.attach_capsule("cap-second".into(),vec![("feed-1".into(),CapturePipeline::new(s2,r2))]).await.unwrap_err();
  assert_eq!(err,EngineError::CapacityExceeded);assert!(stopped2.load(Ordering::SeqCst));
  assert_eq!(engine.status().len(),1);
  engine.abort_all().await.unwrap();
 }
});}

#[test]
fn one_capsule_failure_isolates_other_capsule(){runtime().block_on(async{
 let tmp=Scratch::new();let mut journal=Journal::open(&tmp.0.join("authority.journal"),"synthetic",Clock(1)).unwrap();let now=Instant::now();
 let mut engine:RuntimeEffects<Source>=RuntimeEffects::new(2,4).unwrap();
 let ra=Recorder::test_with_child("drain",&tmp.0,plan_for("t6a"),profile(),&mut journal,now).unwrap();
 let sa=source(CaptureState::Healthy,true,false);let stopped_ok=sa.stopped.clone();
 let rb=Recorder::test_with_child("drain",&tmp.0,plan_for("t6b"),profile(),&mut journal,now).unwrap();
 let sb=source(CaptureState::Closed,true,false);let stopped_bad=sb.stopped.clone();
 engine.attach_capsule("cap-a".into(),vec![("a-feed-ok".into(),CapturePipeline::new(sa,ra)),("z-feed-bad".into(),CapturePipeline::new(sb,rb))]).await.unwrap();
 let rc=Recorder::test_with_child("drain",&tmp.0,plan_for("t6c"),profile(),&mut journal,now).unwrap();
 let sc=source(CaptureState::Healthy,true,false);let stopped_c=sc.stopped.clone();
 engine.attach_capsule("cap-b".into(),vec![("b-feed-ok".into(),CapturePipeline::new(sc,rc))]).await.unwrap();
 let err=engine.tick_all(&WindowRegistry::new(Clock(1)),now).await.unwrap_err();
 assert!(matches!(err,EngineError::Tick(_)));
 let status=engine.status();
 assert_eq!(status.len(),2);
 assert_eq!(status[0].capsule_id,"cap-a");assert_eq!(status[0].state,CapsuleState::Interrupted);
 assert_eq!(status[0].feeds.len(),2);
 assert_eq!(status[0].feeds[0].feed_id,"a-feed-ok");assert_eq!(status[0].feeds[0].state,RecordingState::Interrupted);
 assert_eq!(status[0].feeds[0].counters.written_frames,1);
 assert_eq!(status[0].feeds[1].feed_id,"z-feed-bad");assert_eq!(status[0].feeds[1].state,RecordingState::Interrupted);
 assert_eq!(status[0].feeds[1].counters.written_frames,0);
 assert_eq!(status[1].capsule_id,"cap-b");assert_eq!(status[1].state,CapsuleState::Running);
 assert_eq!(status[1].feeds[0].state,RecordingState::Recording);
 assert_eq!(status[1].feeds[0].counters.written_frames,1);assert_eq!(status[1].feeds[0].counters.written_bytes,1024);
 assert!(stopped_ok.load(Ordering::SeqCst));assert!(stopped_bad.load(Ordering::SeqCst));assert!(!stopped_c.load(Ordering::SeqCst));
 assert_eq!(journal.records().len(),3);
 engine.abort_all().await.unwrap();
});}

#[test]
fn abort_never_publishes(){runtime().block_on(async{
 let tmp=Scratch::new();let mut journal=Journal::open(&tmp.0.join("authority.journal"),"synthetic",Clock(1)).unwrap();let now=Instant::now();
 let mut engine:RuntimeEffects<Source>=RuntimeEffects::new(2,4).unwrap();
 let recorder=Recorder::test_with_child("drain",&tmp.0,plan_for("t7a"),profile(),&mut journal,now).unwrap();
 let src=source(CaptureState::Healthy,true,false);let stopped=src.stopped.clone();
 engine.attach_capsule("cap-abort".into(),vec![("feed-1".into(),CapturePipeline::new(src,recorder))]).await.unwrap();
 engine.tick_all(&WindowRegistry::new(Clock(1)),now).await.unwrap();
 engine.abort_all().await.unwrap();
 assert!(stopped.load(Ordering::SeqCst));
 let status=engine.status();
 assert_eq!(status.len(),1);assert_eq!(status[0].state,CapsuleState::Interrupted);
 assert_eq!(status[0].feeds[0].state,RecordingState::Interrupted);
 assert_eq!(status[0].feeds[0].counters.written_frames,1);
 assert_eq!(journal.records().len(),1);assert!(!tmp.0.join("seg-t7a.mkv").exists());
 engine.abort_all().await.unwrap();
 assert_eq!(engine.status()[0].state,CapsuleState::Interrupted);assert_eq!(journal.records().len(),1);
});}

#[test]
fn failed_stop_cannot_batch_complete(){runtime().block_on(async{
 let tmp=Scratch::new();let mut journal=Journal::open(&tmp.0.join("authority.journal"),"synthetic",Clock(1)).unwrap();let now=Instant::now();
 let mut engine:RuntimeEffects<Source>=RuntimeEffects::new(2,4).unwrap();
 let r1=Recorder::test_with_child("drain",&tmp.0,plan_for("t8a"),profile(),&mut journal,now).unwrap();
 let r2=Recorder::test_with_child("drain",&tmp.0,plan_for("t8b"),profile(),&mut journal,now).unwrap();
 engine.attach_capsule("cap-stop".into(),vec![("feed-1".into(),CapturePipeline::new(source(CaptureState::Healthy,true,false),r1)),("feed-2".into(),CapturePipeline::new(source(CaptureState::Healthy,true,false),r2))]).await.unwrap();
 engine.tick_all(&WindowRegistry::new(Clock(1)),now).await.unwrap();
 let err=match engine.stop_capsule("cap-stop",&mut journal).await{Ok(_)=>panic!("expected stop error"),Err(e)=>e};
 assert!(matches!(err,EngineError::Stop(_)));
 assert_eq!(engine.status().len(),1);assert_eq!(engine.status()[0].state,CapsuleState::Interrupted);
 assert_eq!(journal.records().len(),2);
 assert!(!tmp.0.join("seg-t8a.mkv").exists());assert!(!tmp.0.join("seg-t8b.mkv").exists());
 let err2=match engine.stop_capsule("cap-stop",&mut journal).await{Ok(_)=>panic!("expected stop error"),Err(e)=>e};
 assert!(matches!(err2,EngineError::Stop(_)));
 assert!(matches!(engine.stop_capsule("cap-missing",&mut journal).await,Err(EngineError::NotFound)));
 engine.abort_all().await.unwrap();
});}

#[test]
fn zero_and_huge_budgets(){
 assert!(matches!(RuntimeEffects::<Source>::new(0,4),Err(EngineError::InvalidBudget)));
 assert!(matches!(RuntimeEffects::<Source>::new(2,0),Err(EngineError::InvalidBudget)));
 assert!(matches!(RuntimeEffects::<Source>::new(0,0),Err(EngineError::InvalidBudget)));
 let engine:RuntimeEffects<Source>=RuntimeEffects::new(usize::MAX,usize::MAX).unwrap();
 assert!(engine.status().is_empty());
}

#[test]
fn invalid_pipeline_state_rejected(){runtime().block_on(async{
 let tmp=Scratch::new();let mut journal=Journal::open(&tmp.0.join("authority.journal"),"synthetic",Clock(1)).unwrap();let now=Instant::now();
 let mut engine:RuntimeEffects<Source>=RuntimeEffects::new(2,4).unwrap();
 let mut recorder=Recorder::test_with_child("drain",&tmp.0,plan_for("t10a"),profile(),&mut journal,now).unwrap();
 recorder.interrupt().await.unwrap();
 assert_eq!(recorder.state(),RecordingState::Interrupted);
 let src=source(CaptureState::Healthy,true,false);let stopped=src.stopped.clone();
 let err=engine.attach_capsule("cap-badstate".into(),vec![("feed-1".into(),CapturePipeline::new(src,recorder))]).await.unwrap_err();
 assert_eq!(err,EngineError::InvalidPipelineState);
 assert!(stopped.load(Ordering::SeqCst));assert!(engine.status().is_empty());assert_eq!(journal.records().len(),1);
});}
