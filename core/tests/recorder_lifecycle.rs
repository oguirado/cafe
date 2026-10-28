//! Handwritten timeline and byte oracles. Native Windows encoding is separate.
#[path="../src/capsule_registry.rs"] mod capsule_registry;
#[path="../src/authority_journal.rs"] mod authority_journal;
#[path="../src/journal_replay.rs"] mod journal_replay;
#[path="../src/capture_frame_pool.rs"] mod capture_frame_pool;
#[path="../src/recorder_pacer.rs"] mod recorder_pacer;
#[path="../src/ffmpeg_recorder.rs"] mod ffmpeg_recorder;
#[cfg(windows)]
#[path="../src/windows_journal_policy.rs"] mod windows_journal_policy;
use std::path::{Path,PathBuf};
use std::sync::{Arc,atomic::{AtomicU64,Ordering}};
use std::time::{Duration,Instant};
use capsule_registry::Clock;
use capture_frame_pool::{FramePool,PoolLimits,BorrowedFrame,OwnedFrame};
use authority_journal::Journal;
use recorder_pacer::{Pacer,PacerProfile,PacerError};
use ffmpeg_recorder::{Recorder,RecorderError,RecordingState,RecordingPlan,VideoProfile,Encoder,EncoderUse,preflight};
static SERIAL:AtomicU64=AtomicU64::new(0);
struct Scratch(PathBuf);
impl Scratch {
    fn new()->Self{let root=std::env::temp_dir().join(format!("cafe-recorder-{}-{}",std::process::id(),SERIAL.fetch_add(1,Ordering::SeqCst)));std::fs::create_dir(&root).unwrap();Self(root)}
    fn journal(&self)->Journal{Journal::open(&self.0.join("authority.journal"),"synthetic",Clock(1)).unwrap()}
}
impl Drop for Scratch{fn drop(&mut self){std::fs::remove_dir_all(&self.0).unwrap();}}
fn runtime()->tokio::runtime::Runtime{tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap()}
fn profile()->VideoProfile{VideoProfile{width:16,height:16,fps_num:25,fps_den:1,queue_capacity:4,write_timeout:Duration::from_secs(2),stop_timeout:Duration::from_secs(4),max_lateness_ns:1_000_000_000,max_hold_ns:1_000_000_000}}
fn plan()->RecordingPlan{RecordingPlan{media_id:"capsule-game-generation-one".into(),intent_event:"intent-one".into(),publish_event:"published-one".into(),staged_name:"segment.partial.mkv".into(),published_name:"segment.mkv".into()}}
fn pool(p:VideoProfile,slots:usize)->Arc<FramePool>{FramePool::new(PoolLimits{slots,max_frame_bytes:p.frame_bytes().unwrap(),max_width:p.width,max_height:p.height}).unwrap()}
fn frame(pool:&Arc<FramePool>,p:VideoProfile,clock:u64,color:[u8;4])->OwnedFrame{let bytes=color.repeat((p.width*p.height) as usize);pool.copy_from(BorrowedFrame{data:&bytes,width:p.width,height:p.height,source_stride:p.width as usize*4,source_time:Clock(clock)}).unwrap()}
fn pacer_profile()->PacerProfile{PacerProfile{fps_num:25,fps_den:1,max_lateness_ns:1_000_000_000,max_hold_ns:1_000_000_000}}
#[cfg(unix)]
fn ffmpeg()->PathBuf{let candidates=std::env::var_os("PATH").unwrap_or_default();for dir in std::env::split_paths(&candidates){let path=dir.join("ffmpeg");if path.is_file(){return path;}}panic!("BLOCKED: development FFmpeg absent; this is never a skipped product pass");}

#[test]
fn recorder_child_fixture(){
    let Ok(mode)=std::env::var("CAFE_RECORDER_FIXTURE_MODE") else{return;};
    use std::io::Read;
    match mode.as_str(){
        "partial"=>{let mut bytes=[0u8;7];std::io::stdin().read_exact(&mut bytes).unwrap();std::fs::write("partial.bytes",bytes).unwrap();std::process::exit(17);},
        "crash"=>std::process::exit(19),
        "slow"=>{std::thread::sleep(Duration::from_secs(3));let _=std::io::copy(&mut std::io::stdin(),&mut std::io::sink());},
        "delay_eof"=>{let _=std::io::copy(&mut std::io::stdin(),&mut std::io::sink());std::thread::sleep(Duration::from_secs(3));},
        "junk"=>{let _=std::io::copy(&mut std::io::stdin(),&mut std::io::sink());std::fs::write("segment.partial.mkv",b"not a video").unwrap();},
        "drain"=>{let _=std::io::copy(&mut std::io::stdin(),&mut std::io::sink());},
        _=>std::process::exit(23),
    }
}

#[test]
fn absolute_slots_repeat_and_replace_follow_manual_timeline(){
    let origin=Instant::now();let p=profile();let pool=pool(p,3);let mut pacer=Pacer::new(pacer_profile(),origin).unwrap();
    pacer.offer(frame(&pool,p,1,[1,2,3,255]),origin).unwrap();
    pacer.offer(frame(&pool,p,2,[9,8,7,255]),origin).unwrap();
    let first=pacer.select(origin).unwrap().unwrap();assert_eq!(first.index,0);assert_eq!(first.deadline,origin);assert_eq!(first.frame.pixels(),[9,8,7,255].repeat(256));assert!(!first.duplicate);
    assert!(pacer.select(origin+Duration::from_millis(39)).unwrap().is_none());
    let repeated=pacer.select(origin+Duration::from_millis(40)).unwrap().unwrap();assert_eq!(repeated.index,1);assert_eq!(repeated.deadline,origin+Duration::from_millis(40));assert!(repeated.duplicate);
    pacer.offer(frame(&pool,p,3,[4,5,6,255]),origin+Duration::from_millis(80)).unwrap();
    let third=pacer.select(origin+Duration::from_millis(80)).unwrap().unwrap();assert_eq!(third.index,2);assert!(!third.duplicate);assert_eq!(third.frame.pixels(),[4,5,6,255].repeat(256));
    let counts=pacer.counters();assert_eq!((counts.source_replaced,counts.repeated,counts.selected),(1,1,3));
    drop(first);drop(repeated);drop(third);drop(pacer);assert_eq!(pool.available(),3);
}

#[test]
fn rational_deadlines_do_not_drift_with_repeated_polls(){
    let origin=Instant::now();let p=profile();let pool=pool(p,1);let mut cadence=pacer_profile();cadence.fps_num=30_000;cadence.fps_den=1001;
    let mut pacer=Pacer::new(cadence,origin).unwrap();pacer.offer(frame(&pool,p,1,[0,0,0,255]),origin).unwrap();
    // Independent arithmetic for the first four NTSC-like slots.
    for (index,nanos) in [(0,0),(1,33_366_666),(2,66_733_333),(3,100_100_000)]{
        let slot=pacer.select(origin+Duration::from_nanos(nanos)).unwrap().unwrap();assert_eq!(slot.index,index);assert_eq!(slot.deadline,origin+Duration::from_nanos(nanos));
    }
}

#[test]
fn stale_source_lateness_and_missing_frames_reject_pacing(){
    let origin=Instant::now();let p=profile();let pool=pool(p,2);
    let mut absent=Pacer::new(pacer_profile(),origin).unwrap();assert!(matches!(absent.select(origin),Err(PacerError::SourceAbsent)));
    let mut stale=Pacer::new(pacer_profile(),origin).unwrap();stale.offer(frame(&pool,p,2,[0,0,0,255]),origin).unwrap();assert!(matches!(stale.offer(frame(&pool,p,1,[0,0,0,255]),origin),Err(PacerError::ClockRegression)));
    let mut late=Pacer::new(pacer_profile(),origin).unwrap();assert!(matches!(late.select(origin+Duration::from_secs(2)),Err(PacerError::TooLate)));
}

#[test]
fn full_queue_preserves_slots_and_never_completes(){runtime().block_on(async{
    let scratch=Scratch::new();let mut journal=scratch.journal();let origin=Instant::now();let mut p=profile();p.queue_capacity=1;let pool=pool(p,1);
    let mut recorder=Recorder::test_with_child("drain",&scratch.0,plan(),p,&mut journal,origin).unwrap();recorder.admit(frame(&pool,p,1,[1,2,3,255]),origin).unwrap();assert!(recorder.pace(origin).unwrap());
    assert!(matches!(recorder.pace(origin+Duration::from_millis(40)),Err(RecorderError::QueueFull)));assert_eq!(recorder.queue_len(),1);assert_eq!(recorder.counters().queue_full,1);assert_eq!(recorder.state(),RecordingState::Interrupted);
    assert!(recorder.stop(&mut journal).await.is_err());assert_eq!(journal.records().len(),1);assert_eq!(journal.records()[0].kind,"media_intent");assert!(!scratch.0.join("segment.mkv").exists());
});}

#[test]
fn slow_real_child_counts_backpressure_and_keeps_partial_state(){runtime().block_on(async{
    let scratch=Scratch::new();let mut journal=scratch.journal();let origin=Instant::now();let mut p=profile();p.width=512;p.height=512;p.write_timeout=Duration::from_millis(40);let pool=pool(p,1);
    let mut recorder=Recorder::test_with_child("slow",&scratch.0,plan(),p,&mut journal,origin).unwrap();let pid=recorder.pid();assert!(pid>1 && pid!=std::process::id());recorder.admit(frame(&pool,p,1,[1,2,3,255]),origin).unwrap();recorder.pace(origin).unwrap();
    assert!(matches!(recorder.pump_one().await,Err(RecorderError::WriteTimeout)));assert_eq!(recorder.state(),RecordingState::Interrupted);assert_eq!(recorder.counters().backpressure,1);assert_eq!(recorder.counters().written_frames,0);assert!(recorder.counters().partial_bytes<p.frame_bytes().unwrap() as u64);assert!(recorder.stop(&mut journal).await.is_err());assert_eq!(journal.records().len(),1);
});}

#[test]
fn partial_raw_frame_is_not_retried_or_committed(){runtime().block_on(async{
    let scratch=Scratch::new();let mut journal=scratch.journal();let origin=Instant::now();let mut p=profile();p.width=512;p.height=512;let pool=pool(p,1);
    let mut recorder=Recorder::test_with_child("partial",&scratch.0,plan(),p,&mut journal,origin).unwrap();recorder.admit(frame(&pool,p,1,[1,2,3,255]),origin).unwrap();recorder.pace(origin).unwrap();
    // The independent child proves it consumed a prefix, even on an OS whose
    // failed pipe write supplies no completed-byte count to the parent.
    assert!(matches!(recorder.pump_one().await,Err(RecorderError::PartialFrame|RecorderError::WriteFailed)));assert_eq!(std::fs::read(scratch.0.join("partial.bytes")).unwrap(),[1,2,3,255,1,2,3]);assert_eq!(recorder.counters().written_frames,0);assert!(recorder.counters().partial_bytes<p.frame_bytes().unwrap() as u64);assert_eq!(recorder.state(),RecordingState::Interrupted);assert!(recorder.stop(&mut journal).await.is_err());assert_eq!(journal.records().len(),1);
});}

#[test]
fn child_crash_prevents_complete(){runtime().block_on(async{
    let scratch=Scratch::new();let mut journal=scratch.journal();let origin=Instant::now();let p=profile();let pool=pool(p,1);
    let mut recorder=Recorder::test_with_child("crash",&scratch.0,plan(),p,&mut journal,origin).unwrap();recorder.admit(frame(&pool,p,1,[0,0,0,255]),origin).unwrap();recorder.pace(origin).unwrap();let _=recorder.pump_one().await;
    assert!(recorder.stop(&mut journal).await.is_err());assert_eq!(recorder.state(),RecordingState::Interrupted);assert_eq!(journal.records().len(),1);assert!(!scratch.0.join("segment.mkv").exists());
});}

#[test]
fn simulated_disk_full_preserves_intent_and_rejects_receipt(){runtime().block_on(async{
    let scratch=Scratch::new();let mut journal=scratch.journal();let origin=Instant::now();let p=profile();let pool=pool(p,1);
    let mut recorder=Recorder::test_with_child("drain",&scratch.0,plan(),p,&mut journal,origin).unwrap();recorder.admit(frame(&pool,p,1,[0,0,0,255]),origin).unwrap();recorder.pace(origin).unwrap();recorder.test_disk_full();
    assert!(matches!(recorder.pump_one().await,Err(RecorderError::DiskFull)));assert_eq!(recorder.state(),RecordingState::Interrupted);assert_eq!(recorder.queue_len(),1);assert_eq!(recorder.counters().written_frames,0);assert!(recorder.stop(&mut journal).await.is_err());assert_eq!(journal.records().len(),1);
});}

#[test]
fn codecs_and_profiles_fail_closed_without_fallback(){
    assert!(matches!(Encoder::from_name("libx264"),Err(RecorderError::UnapprovedCodec)));assert!(matches!(Encoder::from_name("h264"),Err(RecorderError::UnapprovedCodec)));assert!(matches!(Encoder::from_name("latest"),Err(RecorderError::UnapprovedCodec)));
    let mut p=profile();p.fps_num=0;assert!(matches!(p.validate(),Err(RecorderError::InvalidProfile)));let mut p=profile();p.queue_capacity=0;assert!(p.validate().is_err());
}

#[test]
fn argv_is_complete_literal_vector_without_shell(){
    let args=Recorder::test_argv(profile(),Encoder::Ffv1,Path::new("segment.partial.mkv"));let actual:Vec<_>=args.iter().map(|s|s.to_str().unwrap()).collect();
    assert_eq!(actual,vec!["-hide_banner","-loglevel","error","-nostdin","-n","-f","rawvideo","-pixel_format","bgra","-video_size","16x16","-framerate","25/1","-i","pipe:0","-an","-c:v","ffv1","-f","matroska","segment.partial.mkv"]);
}

#[cfg(unix)]
#[test]
fn real_preflight_encode_decode_then_durable_receipt(){runtime().block_on(async{
    let scratch=Scratch::new();let binary=ffmpeg();let p=profile();let approval=preflight(&binary,Encoder::Ffv1,EncoderUse::Development,p,&scratch.0.join("probe.mkv")).await.expect("BLOCKED: development FFV1 preflight failed");assert!(!scratch.0.join("probe.mkv").exists());
    let mut journal=scratch.journal();let origin=Instant::now();let pool=pool(p,2);let mut recorder=Recorder::start(approval,&scratch.0,plan(),&mut journal,origin).await.unwrap();
    recorder.admit(frame(&pool,p,1,[7,19,43,255]),origin).unwrap();
    for milliseconds in [0,40,80]{assert!(recorder.pace(origin+Duration::from_millis(milliseconds)).unwrap());assert!(recorder.pump_one().await.unwrap());}
    let receipt=recorder.stop(&mut journal).await.unwrap();assert_eq!(receipt.frames,3);assert_eq!(receipt.bytes,3072);assert_eq!(receipt.encoder,Encoder::Ffv1);assert_eq!(recorder.state(),RecordingState::Complete);assert_eq!(journal.records().len(),2);assert_eq!(journal.records()[1].kind,"media_published");assert!(scratch.0.join("segment.mkv").is_file());assert!(!scratch.0.join("segment.partial.mkv").exists());
    assert!(recorder.admit(frame(&pool,p,2,[0,0,0,255]),origin).is_err());assert!(recorder.stop(&mut journal).await.is_err());assert_eq!(journal.records().len(),2);
    let decoded=std::process::Command::new(&binary).args(["-hide_banner","-loglevel","error","-i"]).arg(scratch.0.join("segment.mkv")).args(["-f","rawvideo","-pix_fmt","bgra","pipe:1"]).output().unwrap();assert!(decoded.status.success());assert_eq!(decoded.stdout,[7,19,43,255].repeat(16*16*3));
});}

#[cfg(unix)]
#[test]
fn hardware_absence_is_explicit_and_production_dev_binary_is_blocked(){runtime().block_on(async{
    let scratch=Scratch::new();let binary=ffmpeg();let p=profile();
    let production=preflight(&binary,Encoder::Ffv1,EncoderUse::Production{audited_sha256:String::new()},p,&scratch.0.join("production.mkv")).await;assert!(matches!(production,Err(RecorderError::BlockedAudit)));
    let inventory=std::process::Command::new(&binary).args(["-hide_banner","-encoders"]).output().unwrap();assert!(inventory.status.success());let listed=String::from_utf8(inventory.stdout).unwrap();
    let result=preflight(&binary,Encoder::Nvenc,EncoderUse::Development,p,&scratch.0.join("hardware.mkv")).await;
    if !listed.lines().any(|l|l.split_whitespace().nth(1)==Some("h264_nvenc")){assert!(matches!(result,Err(RecorderError::BlockedEncoder)));}
    else {assert!(result.is_ok() || matches!(result,Err(RecorderError::BlockedEncoder)));}
    assert!(!scratch.0.join("hardware.mkv").exists());
});}

#[test]
fn stop_deadline_reaps_own_child_and_cannot_complete(){runtime().block_on(async{
    let scratch=Scratch::new();let mut journal=scratch.journal();let origin=Instant::now();let mut p=profile();p.stop_timeout=Duration::from_millis(40);let pool=pool(p,1);
    let mut recorder=Recorder::test_with_child("delay_eof",&scratch.0,plan(),p,&mut journal,origin).unwrap();recorder.admit(frame(&pool,p,1,[0,0,0,255]),origin).unwrap();recorder.pace(origin).unwrap();recorder.pump_one().await.unwrap();
    assert!(matches!(recorder.stop(&mut journal).await,Err(RecorderError::StopTimeout)));assert_eq!(recorder.state(),RecordingState::Interrupted);assert_eq!(journal.records().len(),1);assert!(!scratch.0.join("segment.mkv").exists());
});}

#[test]
fn successful_child_exit_without_decodable_artifact_is_incomplete(){runtime().block_on(async{
    let scratch=Scratch::new();let mut journal=scratch.journal();let origin=Instant::now();let p=profile();let pool=pool(p,1);
    let mut recorder=Recorder::test_with_child("junk",&scratch.0,plan(),p,&mut journal,origin).unwrap();recorder.admit(frame(&pool,p,1,[0,0,0,255]),origin).unwrap();recorder.pace(origin).unwrap();recorder.pump_one().await.unwrap();
    assert!(matches!(recorder.stop(&mut journal).await,Err(RecorderError::ArtifactInvalid)));assert_eq!(recorder.state(),RecordingState::Interrupted);assert_eq!(journal.records().len(),1);
});}

#[test]
fn unavailable_hardware_encoder_is_blocked_without_substitution(){assert!(matches!(Recorder::test_encoder_absent(),Err(RecorderError::BlockedEncoder)));}

mod confirmed_pipe_tests {

    use crate::ffmpeg_recorder::{write_confirmed_chunks,RecorderError};
    use std::{pin::Pin,task::{Context,Poll}};
    struct BufferedWriter {flushes:usize,fail_at:usize}
    impl tokio::io::AsyncWrite for BufferedWriter {
        fn poll_write(self:Pin<&mut Self>,_:&mut Context<'_>,bytes:&[u8])->Poll<std::io::Result<usize>>{Poll::Ready(Ok(bytes.len().min(2)))}
        fn poll_flush(mut self:Pin<&mut Self>,_:&mut Context<'_>)->Poll<std::io::Result<()>>{self.flushes+=1;if self.flushes==self.fail_at{Poll::Ready(Err(std::io::Error::other("synthetic flush failure")))}else{Poll::Ready(Ok(()))}}
        fn poll_shutdown(self:Pin<&mut Self>,_:&mut Context<'_>)->Poll<std::io::Result<()>>{Poll::Ready(Ok(()))}
    }
    #[test]
    fn buffered_acceptance_is_not_a_confirmed_frame(){
        tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap().block_on(async{
            let mut writer=BufferedWriter{flushes:0,fail_at:1};let mut confirmed=0;
            assert_eq!(write_confirmed_chunks(&mut writer,b"abcd",&mut confirmed).await,Err(RecorderError::WriteFailed));assert_eq!(confirmed,0);
            let mut writer=BufferedWriter{flushes:0,fail_at:2};let mut confirmed=0;
            assert_eq!(write_confirmed_chunks(&mut writer,b"abcd",&mut confirmed).await,Err(RecorderError::PartialFrame));assert_eq!(confirmed,2);
            let mut writer=BufferedWriter{flushes:0,fail_at:usize::MAX};let mut confirmed=0;
            assert_eq!(write_confirmed_chunks(&mut writer,b"abcd",&mut confirmed).await,Ok(()));assert_eq!(confirmed,4);assert_eq!(writer.flushes,2);
        });
    }

}
