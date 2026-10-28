// Validation, process ownership, licensing and artifact barriers.
use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, Instant};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWriteExt};
use tokio::process::{Child, ChildStdin, Command};
use tokio::task::JoinHandle;
use sha2::{Digest, Sha256};
use crate::authority_journal::{Committed, Journal};
use crate::capture_frame_pool::OwnedFrame;
use crate::recorder_pacer::{Pacer, PacerProfile, PacerError, OutputFrame};

#[derive(Clone,Copy,Debug,PartialEq,Eq)]
pub enum Encoder { Ffv1, Nvenc, Amf, Qsv }
impl Encoder {
    pub fn from_name(name:&str)->Result<Self,RecorderError>{match name {"ffv1"=>Ok(Self::Ffv1),"h264_nvenc"=>Ok(Self::Nvenc),"h264_amf"=>Ok(Self::Amf),"h264_qsv"=>Ok(Self::Qsv),_=>Err(RecorderError::UnapprovedCodec)}}
    pub fn name(self)->&'static str{match self{Self::Ffv1=>"ffv1",Self::Nvenc=>"h264_nvenc",Self::Amf=>"h264_amf",Self::Qsv=>"h264_qsv"}}
}
#[derive(Clone,Debug)]
pub enum EncoderUse { Development, Production { audited_sha256:String } }
#[derive(Clone,Copy,Debug,PartialEq,Eq)]
pub struct VideoProfile {
    pub width:u32,pub height:u32,pub fps_num:u32,pub fps_den:u32,
    pub queue_capacity:usize,pub write_timeout:Duration,pub stop_timeout:Duration,
    pub max_lateness_ns:u64,pub max_hold_ns:u64,
}
impl VideoProfile {
    pub fn validate(self)->Result<Self,RecorderError>{
        if self.width==0 || self.height==0 || self.fps_num==0 || self.fps_den==0 || self.queue_capacity==0 || self.write_timeout.is_zero() || self.stop_timeout.is_zero() || self.max_lateness_ns==0 || self.max_hold_ns==0{return Err(RecorderError::InvalidProfile);}
        self.frame_bytes()?;self.pacer().map_err(RecorderError::Pacer)?;Ok(self)
    }
    pub fn frame_bytes(self)->Result<usize,RecorderError>{(self.width as usize).checked_mul(self.height as usize).and_then(|v|v.checked_mul(4)).filter(|&v|v>0).ok_or(RecorderError::InvalidProfile)}
    fn pacer(self)->Result<PacerProfile,PacerError>{PacerProfile{fps_num:self.fps_num,fps_den:self.fps_den,max_lateness_ns:self.max_lateness_ns,max_hold_ns:self.max_hold_ns}.validate()}
}
#[derive(Clone,Copy,Debug,PartialEq,Eq)]
pub enum RecorderError {InvalidProfile,InvalidPath,UnapprovedCodec,BlockedAudit,BlockedEncoder,ExecutableChanged,Spawn,ProcessOwnership,WriteFailed,PartialFrame,WriteTimeout,ChildExit,StopTimeout,ArtifactInvalid,Journal,QueueFull,State,DiskFull,Pacer(PacerError)}
#[derive(Clone,Copy,Debug,PartialEq,Eq)]
pub enum RecordingState { Recording, Finalizing, Complete, Interrupted }
#[derive(Clone,Copy,Debug,Default,PartialEq,Eq)]
pub struct RecorderCounters {pub written_frames:u64,pub written_bytes:u64,pub backpressure:u64,pub partial_bytes:u64,pub queue_full:u64}
#[derive(Clone,Debug)]
pub struct RecordingPlan {pub media_id:String,pub intent_event:String,pub publish_event:String,pub staged_name:String,pub published_name:String}
pub struct RecordingReceipt {pub journal:Committed,pub frames:u64,pub bytes:u64,pub encoder:Encoder}
pub struct Preflight {binary:PathBuf,digest:String,encoder:Encoder,profile:VideoProfile}
impl Preflight {pub fn encoder(&self)->Encoder{self.encoder} pub fn profile(&self)->VideoProfile{self.profile}}
fn safe_name(name:&str)->bool{!name.is_empty() && name.len()<=128 && name.ends_with(".mkv") && !name.starts_with('.') && !name.contains("..") && name.bytes().all(|b|b.is_ascii_alphanumeric() || matches!(b,b'_'|b'-'|b'.'))}
fn validate_plan(root:&Path,plan:&RecordingPlan)->Result<(),RecorderError>{
    let meta=std::fs::symlink_metadata(root).map_err(|_|RecorderError::InvalidPath)?;
    if meta.file_type().is_symlink() || !meta.is_dir() || !safe_name(&plan.staged_name) || !safe_name(&plan.published_name) || plan.staged_name==plan.published_name || plan.media_id.is_empty() || plan.intent_event.is_empty() || plan.publish_event.is_empty(){return Err(RecorderError::InvalidPath);}
    for name in [&plan.staged_name,&plan.published_name]{match std::fs::symlink_metadata(root.join(name)){Err(error) if error.kind()==std::io::ErrorKind::NotFound=>{},_=>return Err(RecorderError::InvalidPath)}}
    Ok(())
}
fn binary_digest(binary:&Path)->Result<String,RecorderError>{let bytes=std::fs::read(binary).map_err(|_|RecorderError::Spawn)?;Ok(Sha256::digest(bytes).iter().map(|byte|format!("{byte:02x}")).collect())}
fn complete_argv(profile:VideoProfile,encoder:Encoder,output:&Path)->Vec<std::ffi::OsString>{
    ["-hide_banner","-loglevel","error","-nostdin","-n","-f","rawvideo","-pixel_format","bgra","-video_size",&format!("{}x{}",profile.width,profile.height),"-framerate",&format!("{}/{}",profile.fps_num,profile.fps_den),"-i","pipe:0","-an","-c:v",encoder.name(),"-f","matroska"].into_iter().map(std::ffi::OsString::from).chain(std::iter::once(output.as_os_str().to_owned())).collect()
}
async fn bounded_read<R:AsyncRead+Unpin>(mut reader:R,limit:usize)->Result<Vec<u8>,RecorderError>{
    let mut output=Vec::new();let mut chunk=[0u8;4096];let mut exceeded=false;
    loop{let n=reader.read(&mut chunk).await.map_err(|_|RecorderError::ArtifactInvalid)?;if n==0{break;}if output.len().saturating_add(n)>limit{exceeded=true;}else if !exceeded{output.extend_from_slice(&chunk[..n]);}}
    if exceeded{Err(RecorderError::ArtifactInvalid)}else{Ok(output)}
}
async fn drain_stderr<R:AsyncRead+Unpin>(mut reader:R){let mut bytes=[0u8;4096];while let Ok(n)=reader.read(&mut bytes).await{if n==0{break;}}}
struct ChildSupervisor {child:Child,stdin:Option<ChildStdin>,pid:u32,stderr:Option<JoinHandle<()>>,write_timeout:Duration,stop_timeout:Duration,last_partial:usize}
impl ChildSupervisor {
    fn spawn(binary:&Path,args:&[std::ffi::OsString],profile:VideoProfile)->Result<Self,RecorderError>{
        let mut command=Command::new(binary);command.args(args).stdin(Stdio::piped()).stdout(Stdio::null()).stderr(Stdio::piped());
        let mut child=command.spawn().map_err(|_|RecorderError::Spawn)?;let pid=child.id().ok_or(RecorderError::ProcessOwnership)?;
        if pid<=1 || pid==std::process::id(){return Err(RecorderError::ProcessOwnership);}
        let stdin=child.stdin.take().ok_or(RecorderError::Spawn)?;let stderr=child.stderr.take().ok_or(RecorderError::Spawn)?;
        Ok(Self{child,stdin:Some(stdin),pid,stderr:Some(tokio::spawn(drain_stderr(stderr))),write_timeout:profile.write_timeout,stop_timeout:profile.stop_timeout,last_partial:0})
    }
    fn owned(&self)->Result<(),RecorderError>{if self.pid<=1 || self.pid==std::process::id() || self.child.id()!=Some(self.pid){Err(RecorderError::ProcessOwnership)}else{Ok(())}}
    async fn write_frame(&mut self,bytes:&[u8])->Result<(),RecorderError>{
        self.last_partial=0;let stdin=self.stdin.as_mut().ok_or(RecorderError::State)?;let mut offset=0;
        let outcome=tokio::time::timeout(self.write_timeout,async{
            while offset<bytes.len(){match stdin.write(&bytes[offset..]).await{Ok(0)=>return Err(RecorderError::PartialFrame),Ok(n)=>offset+=n,Err(_)=>return Err(if offset==0{RecorderError::WriteFailed}else{RecorderError::PartialFrame})}}Ok(())
        }).await;
        self.last_partial=offset;
        match outcome{Ok(result)=>result,Err(_)=>Err(RecorderError::WriteTimeout)}
    }
    async fn finish(&mut self)->Result<(),RecorderError>{
        self.stdin.take();let outcome=tokio::time::timeout(self.stop_timeout,self.child.wait()).await;
        let result=match outcome{Ok(Ok(status)) if status.success()=>Ok(()),Ok(_)=>Err(RecorderError::ChildExit),Err(_)=>{self.abort().await?;Err(RecorderError::StopTimeout)}};
        if let Some(mut task)=self.stderr.take(){if tokio::time::timeout(self.stop_timeout,&mut task).await.is_err(){task.abort();let _=task.await;}}result
    }
    async fn abort(&mut self)->Result<(),RecorderError>{
        self.stdin.take();if self.child.try_wait().map_err(|_|RecorderError::ChildExit)?.is_none(){self.owned()?;self.child.start_kill().map_err(|_|RecorderError::ChildExit)?;tokio::time::timeout(self.stop_timeout,self.child.wait()).await.map_err(|_|RecorderError::StopTimeout)?.map_err(|_|RecorderError::ChildExit)?;}
        if let Some(mut task)=self.stderr.take(){if tokio::time::timeout(self.stop_timeout,&mut task).await.is_err(){task.abort();let _=task.await;}}Ok(())
    }
}
impl Drop for ChildSupervisor {fn drop(&mut self){if self.child.id().is_some() && self.owned().is_ok(){let _=self.child.start_kill();}if let Some(task)=self.stderr.take(){task.abort();}}}
async fn tool_output(binary:&Path,args:&[std::ffi::OsString],deadline:Duration)->Result<Vec<u8>,RecorderError>{
    let mut child=Command::new(binary).args(args).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().map_err(|_|RecorderError::Spawn)?;
    let pid=child.id().ok_or(RecorderError::ProcessOwnership)?;if pid<=1 || pid==std::process::id(){return Err(RecorderError::ProcessOwnership);}
    let stdout=child.stdout.take().ok_or(RecorderError::Spawn)?;let stderr=child.stderr.take().ok_or(RecorderError::Spawn)?;
    let mut output=tokio::spawn(bounded_read(stdout,262144));let mut err=tokio::spawn(drain_stderr(stderr));
    let status=tokio::time::timeout(deadline,child.wait()).await;
    let good=match status{Ok(Ok(status))=>status.success(),Ok(Err(_))=>false,Err(_)=>{if child.id()==Some(pid) && pid>1 && pid!=std::process::id(){let _=child.start_kill();let _=tokio::time::timeout(deadline,child.wait()).await;}false}};
    let bytes=match tokio::time::timeout(deadline,&mut output).await{Ok(Ok(result))=>result,_=>{output.abort();let _=output.await;Err(RecorderError::ArtifactInvalid)}};if tokio::time::timeout(deadline,&mut err).await.is_err(){err.abort();let _=err.await;}if !good{return Err(RecorderError::ArtifactInvalid);}bytes
}
async fn validate_artifact(binary:&Path,path:&Path,expected:u64,deadline:Duration)->Result<(),RecorderError>{
    if expected==0{return Err(RecorderError::ArtifactInvalid);}
    let args:[std::ffi::OsString;15]=["-hide_banner".into(),"-loglevel".into(),"error".into(),"-xerror".into(),"-nostats".into(),"-progress".into(),"pipe:1".into(),"-i".into(),path.as_os_str().to_owned(),"-map".into(),"0:v:0".into(),"-f".into(),"null".into(),"-".into(),"-nostdin".into()];
    let bytes=tool_output(binary,&args,deadline).await?;let text=std::str::from_utf8(&bytes).map_err(|_|RecorderError::ArtifactInvalid)?;
    let frames=text.lines().filter_map(|line|line.strip_prefix("frame=")).filter_map(|v|v.trim().parse::<u64>().ok()).last();
    if frames!=Some(expected){return Err(RecorderError::ArtifactInvalid);}Ok(())
}
pub async fn preflight(binary:&Path,encoder:Encoder,usage:EncoderUse,profile:VideoProfile,probe_path:&Path)->Result<Preflight,RecorderError>{
    let profile=profile.validate()?;if std::fs::symlink_metadata(probe_path).is_ok(){return Err(RecorderError::InvalidPath);}let digest=binary_digest(binary)?;
    if let EncoderUse::Production{audited_sha256}=usage{
        if encoder==Encoder::Ffv1 || audited_sha256.len()!=64 || audited_sha256!=digest{return Err(RecorderError::BlockedAudit);}
        let version=tool_output(binary,&["-version".into()],profile.stop_timeout).await?;let text=String::from_utf8_lossy(&version);
        if !text.starts_with("ffmpeg version 8.1.3") || ["--enable-libx264","--enable-gpl","--enable-nonfree"].iter().any(|flag|text.contains(flag)){return Err(RecorderError::BlockedAudit);}
    }
    let inventory=tool_output(binary,&["-hide_banner".into(),"-encoders".into()],profile.stop_timeout).await?;
    require_encoder(&inventory,encoder)?;
    let result=async{
        let mut child=ChildSupervisor::spawn(binary,&complete_argv(profile,encoder,probe_path),profile)?;
        let pixels=vec![0;profile.frame_bytes()?];for _ in 0..2{if child.write_frame(&pixels).await.is_err(){let _=child.abort().await;return Err(RecorderError::BlockedEncoder);}}
        child.finish().await.map_err(|_|RecorderError::BlockedEncoder)?;validate_artifact(binary,probe_path,2,profile.stop_timeout).await.map_err(|_|RecorderError::BlockedEncoder)
    }.await;
    let _=std::fs::remove_file(probe_path);result?;Ok(Preflight{binary:binary.to_path_buf(),digest,encoder,profile})
}

fn require_encoder(inventory:&[u8],encoder:Encoder)->Result<(),RecorderError>{if String::from_utf8_lossy(inventory).lines().any(|line|line.split_whitespace().nth(1)==Some(encoder.name())){Ok(())}else{Err(RecorderError::BlockedEncoder)}}
// Recorder orchestration (P11). The validated security boundary
// above owns preflight, policy, artifact validation and child supervision.

/// Single-owner paced recorder: pacer, bounded queue and supervised child.
pub struct Recorder {
    profile: VideoProfile,
    root: PathBuf,
    plan: RecordingPlan,
    pacer: Pacer,
    queue: VecDeque<OutputFrame>,
    child: ChildSupervisor,
    state: RecordingState,
    counters: RecorderCounters,
    binary: PathBuf,
    encoder: Encoder,
    #[cfg(test)]
    disk_full: bool,
}

impl Recorder {
    /// Validate plan/profile/digest, record durable intent, then spawn FFmpeg.
    pub async fn start(
        approval: Preflight,
        root: &Path,
        plan: RecordingPlan,
        journal: &mut Journal,
        origin: Instant,
    ) -> Result<Self, RecorderError> {
        validate_plan(root, &plan)?;
        let profile = approval.profile().validate()?;
        let encoder = approval.encoder();
        if binary_digest(&approval.binary)? != approval.digest {
            return Err(RecorderError::ExecutableChanged);
        }
        journal
            .append_media_intent(&plan.intent_event, &plan.media_id, &plan.published_name)
            .map_err(|_| RecorderError::Journal)?;
        let output = root.join(&plan.staged_name);
        let argv = complete_argv(profile, encoder, &output);
        let binary = approval.binary;
        let child = ChildSupervisor::spawn(&binary, &argv, profile)?;
        let pacer_profile = profile.pacer().map_err(RecorderError::Pacer)?;
        let pacer = Pacer::new(pacer_profile, origin).map_err(RecorderError::Pacer)?;
        Ok(Self {
            profile,
            root: root.to_path_buf(),
            plan,
            pacer,
            queue: VecDeque::with_capacity(profile.queue_capacity),
            child,
            state: RecordingState::Recording,
            counters: RecorderCounters::default(),
            binary,
            encoder,
            #[cfg(test)]
            disk_full: false,
        })
    }

    /// Current lifecycle state.
    pub fn state(&self) -> RecordingState {
        self.state
    }

    /// Copy of the recorder counters.
    pub fn counters(&self) -> RecorderCounters {
        self.counters
    }

    /// Copy of the pacer slot counters.
    pub fn pacer_counters(&self) -> crate::recorder_pacer::PacerCounters {
        self.pacer.counters()
    }

    /// Supervised FFmpeg child pid.
    pub fn pid(&self) -> u32 {
        self.child.pid
    }

    /// Number of queued output slots.
    pub fn queue_len(&self) -> usize {
        self.queue.len()
    }

    /// Offer a frame with exact admitted geometry. Never encodes or blocks.
    pub fn admit(&mut self, frame: OwnedFrame, now: Instant) -> Result<(), RecorderError> {
        if self.state != RecordingState::Recording {
            return Err(RecorderError::State);
        }
        let expected = match self.profile.frame_bytes() {
            Ok(bytes) => bytes,
            Err(err) => {
                self.state = RecordingState::Interrupted;
                return Err(err);
            }
        };
        if frame.width() != self.profile.width
            || frame.height() != self.profile.height
            || frame.pixels().len() != expected
        {
            self.state = RecordingState::Interrupted;
            return Err(RecorderError::InvalidProfile);
        }
        if let Err(err) = self.pacer.offer(frame, now) {
            self.state = RecordingState::Interrupted;
            return Err(RecorderError::Pacer(err));
        }
        Ok(())
    }

    /// Select at most one due slot into the bounded queue. A full queue
    /// interrupts without dropping any queued slot.
    pub fn pace(&mut self, now: Instant) -> Result<bool, RecorderError> {
        if self.state != RecordingState::Recording {
            return Err(RecorderError::State);
        }
        if self.queue.len() >= self.profile.queue_capacity {
            self.counters.queue_full = self.counters.queue_full.saturating_add(1);
            self.counters.backpressure = self.counters.backpressure.saturating_add(1);
            self.state = RecordingState::Interrupted;
            return Err(RecorderError::QueueFull);
        }
        match self.pacer.select(now) {
            Err(err) => {
                self.state = RecordingState::Interrupted;
                Err(RecorderError::Pacer(err))
            }
            Ok(Some(output)) => {
                self.queue.push_back(output);
                Ok(true)
            }
            Ok(None) => Ok(false),
        }
    }

    /// Write the oldest queued slot to the child. Never retries partials.
    pub async fn pump_one(&mut self) -> Result<bool, RecorderError> {
        if self.state != RecordingState::Recording && self.state != RecordingState::Finalizing {
            return Err(RecorderError::State);
        }
        #[cfg(test)]
        if self.disk_full {
            self.state = RecordingState::Interrupted;
            return Err(RecorderError::DiskFull);
        }
        let Some(output) = self.queue.pop_front() else {
            return Ok(false);
        };
        if output.index != self.counters.written_frames {
            self.state = RecordingState::Interrupted;
            return Err(RecorderError::State);
        }
        let bytes = output.frame.pixels();
        let len = bytes.len();
        match self.child.write_frame(bytes).await {
            Ok(()) => {
                self.counters.written_frames = self.counters.written_frames.saturating_add(1);
                self.counters.written_bytes =
                    self.counters.written_bytes.saturating_add(len as u64);
                Ok(true)
            }
            Err(err) => {
                self.counters.partial_bytes = self.child.last_partial as u64;
                if err == RecorderError::WriteTimeout {
                    self.counters.backpressure = self.counters.backpressure.saturating_add(1);
                }
                self.state = RecordingState::Interrupted;
                Err(err)
            }
        }
    }

    /// Abort the child and keep partial evidence; no receipt is emitted.
    pub async fn interrupt(&mut self) -> Result<(), RecorderError> {
        self.state = RecordingState::Interrupted;
        self.child.abort().await
    }

    /// Drain, finish the child, decode-validate, then publish durably.
    pub async fn stop(&mut self, journal: &mut Journal) -> Result<RecordingReceipt, RecorderError> {
        if self.state == RecordingState::Interrupted {
            let _ = self.child.abort().await;
            return Err(RecorderError::State);
        }
        if self.state == RecordingState::Complete {
            return Err(RecorderError::State);
        }
        self.state = RecordingState::Finalizing;
        loop {
            match self.pump_one().await {
                Ok(true) => {}
                Ok(false) => break,
                Err(err) => {
                    self.state = RecordingState::Interrupted;
                    let _ = self.child.abort().await;
                    return Err(err);
                }
            }
        }
        if let Err(err) = self.child.finish().await {
            self.state = RecordingState::Interrupted;
            let _ = self.child.abort().await;
            return Err(err);
        }
        if self.counters.written_frames == 0 {
            self.state = RecordingState::Interrupted;
            return Err(RecorderError::ArtifactInvalid);
        }
        let staged = self.root.join(&self.plan.staged_name);
        if let Err(err) = validate_artifact(
            &self.binary,
            &staged,
            self.counters.written_frames,
            self.profile.stop_timeout,
        )
        .await
        {
            self.state = RecordingState::Interrupted;
            return Err(err);
        }
        let committed = match journal.publish_media(
            &self.plan.publish_event,
            &self.root,
            &self.plan.media_id,
            &self.plan.staged_name,
        ) {
            Ok(committed) => committed,
            Err(_) => {
                self.state = RecordingState::Interrupted;
                return Err(RecorderError::Journal);
            }
        };
        self.state = RecordingState::Complete;
        Ok(RecordingReceipt {
            journal: committed,
            frames: self.counters.written_frames,
            bytes: self.counters.written_bytes,
            encoder: self.encoder,
        })
    }
}
// Restricted test seams: none are compiled into the product API.
#[cfg(test)]
impl ChildSupervisor {
    fn fixture(mode:&str,profile:VideoProfile,root:&Path)->Result<Self,RecorderError>{
        let executable=std::env::current_exe().map_err(|_|RecorderError::Spawn)?;
        let mut child=Command::new(executable).args(["--exact","recorder_child_fixture","--nocapture"])
            .env("CAFE_RECORDER_FIXTURE_MODE",mode).current_dir(root).stdin(Stdio::piped()).stdout(Stdio::null()).stderr(Stdio::piped())
            .spawn().map_err(|_|RecorderError::Spawn)?;
        let pid=child.id().ok_or(RecorderError::ProcessOwnership)?;
        if pid<=1 || pid==std::process::id(){return Err(RecorderError::ProcessOwnership);}
        let stdin=child.stdin.take().ok_or(RecorderError::Spawn)?;let stderr=child.stderr.take().ok_or(RecorderError::Spawn)?;
        Ok(Self{child,stdin:Some(stdin),pid,stderr:Some(tokio::spawn(drain_stderr(stderr))),write_timeout:profile.write_timeout,stop_timeout:profile.stop_timeout,last_partial:0})
    }
}
#[cfg(test)]
impl Recorder {
    pub(crate) fn test_with_child(mode:&str,root:&Path,plan:RecordingPlan,profile:VideoProfile,journal:&mut Journal,origin:Instant)->Result<Self,RecorderError>{
        let profile=profile.validate()?;validate_plan(root,&plan)?;
        journal.append_media_intent(&plan.intent_event,&plan.media_id,&plan.published_name).map_err(|_|RecorderError::Journal)?;
        let child=ChildSupervisor::fixture(mode,profile,root)?;
        Ok(Self{profile,root:root.to_path_buf(),plan,pacer:Pacer::new(profile.pacer().map_err(RecorderError::Pacer)?,origin).map_err(RecorderError::Pacer)?,queue:VecDeque::new(),child,state:RecordingState::Recording,counters:RecorderCounters::default(),binary:std::env::current_exe().map_err(|_|RecorderError::Spawn)?,encoder:Encoder::Ffv1,disk_full:false})
    }
    pub(crate) fn test_disk_full(&mut self){self.disk_full=true;}
    pub(crate) fn test_argv(profile:VideoProfile,encoder:Encoder,path:&Path)->Vec<std::ffi::OsString>{complete_argv(profile,encoder,path)}
}

#[cfg(test)]
impl Recorder {pub(crate) fn test_encoder_absent()->Result<(),RecorderError>{require_encoder(b"Encoders:\n V..... ffv1 Lossless video\n",Encoder::Nvenc)}}
