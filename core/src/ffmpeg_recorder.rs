// Validation, process ownership, licensing and artifact barriers.
use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWriteExt};
use tokio::process::{Child, ChildStdin, Command};
use tokio::task::JoinHandle;
use sha2::{Digest, Sha256};
use crate::authority_journal::{Committed, Journal};
use crate::capture_frame_pool::OwnedFrame;
use crate::recorder_pacer::{Pacer, PacerProfile, PacerError, OutputFrame, PreviewSelector, PreviewCounters};

#[derive(Clone,Copy,Debug,PartialEq,Eq)]
pub enum Encoder { Ffv1, Nvenc, Amf, Qsv }
impl Encoder {
    pub fn from_name(name:&str)->Result<Self,RecorderError>{match name {"ffv1"=>Ok(Self::Ffv1),"h264_nvenc"=>Ok(Self::Nvenc),"h264_amf"=>Ok(Self::Amf),"h264_qsv"=>Ok(Self::Qsv),_=>Err(RecorderError::UnapprovedCodec)}}
    pub fn name(self)->&'static str{match self{Self::Ffv1=>"ffv1",Self::Nvenc=>"h264_nvenc",Self::Amf=>"h264_amf",Self::Qsv=>"h264_qsv"}}
}
#[derive(Clone,Debug)]
pub enum EncoderUse { Development, Production { audited_sha256:String }, OperatorApprovedExternalTest { audited_sha256:String } }
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
#[derive(Clone,Debug)]
pub struct Preflight {binary:PathBuf,digest:String,encoder:Encoder,profile:VideoProfile}
impl Preflight {
    pub fn encoder(&self)->Encoder{self.encoder}
    pub fn profile(&self)->VideoProfile{self.profile}
    /// Reuse a proven binary+digest+encoder trial for a new validated profile.
    /// Raw BGRA pipe framing and hardware encoder selection do not depend on a
    /// trial per resolution; `Recorder::start_with_clock` still re-verifies the
    /// binary SHA-256 on disk before spawning and fails closed if the encoder
    /// rejects the target dimensions or runs out of hardware sessions.
    pub fn with_profile(&self,profile:VideoProfile)->Result<Self,RecorderError>{
        let profile=profile.validate()?;
        if binary_digest(&self.binary)?!=self.digest{return Err(RecorderError::ExecutableChanged);}
        Ok(Self{binary:self.binary.clone(),digest:self.digest.clone(),encoder:self.encoder,profile})
    }
}
fn safe_name(name:&str)->bool{!name.is_empty() && name.len()<=128 && name.ends_with(".mkv") && !name.starts_with('.') && !name.contains("..") && name.bytes().all(|b|b.is_ascii_alphanumeric() || matches!(b,b'_'|b'-'|b'.'))}
fn validate_plan(root:&Path,plan:&RecordingPlan)->Result<(),RecorderError>{
    let meta=std::fs::symlink_metadata(root).map_err(|_|RecorderError::InvalidPath)?;
    if meta.file_type().is_symlink() || !meta.is_dir() || !safe_name(&plan.staged_name) || !safe_name(&plan.published_name) || plan.staged_name==plan.published_name || plan.media_id.is_empty() || plan.intent_event.is_empty() || plan.publish_event.is_empty(){return Err(RecorderError::InvalidPath);}
    for name in [&plan.staged_name,&plan.published_name]{match std::fs::symlink_metadata(root.join(name)){Err(error) if error.kind()==std::io::ErrorKind::NotFound=>{},_=>return Err(RecorderError::InvalidPath)}}
    Ok(())
}
fn binary_digest(binary:&Path)->Result<String,RecorderError>{let bytes=std::fs::read(binary).map_err(|_|RecorderError::Spawn)?;Ok(Sha256::digest(bytes).iter().map(|byte|format!("{byte:02x}")).collect())}
const FHD_VIDEO_FILTER:&str="scale=1920:1080:force_original_aspect_ratio=decrease:flags=bicubic,pad=1920:1080:(ow-iw)/2:(oh-ih)/2";
fn complete_argv(profile:VideoProfile,encoder:Encoder,output:&Path)->Vec<std::ffi::OsString>{
    let mut args:Vec<std::ffi::OsString>=["-hide_banner","-loglevel","error","-nostdin","-n","-f","rawvideo","-pixel_format","bgra","-video_size",&format!("{}x{}",profile.width,profile.height),"-framerate",&format!("{}/{}",profile.fps_num,profile.fps_den)].into_iter().map(std::ffi::OsString::from).collect();
    // Bounded hardware input thread absorbs encoder startup; no software fallback.
    if encoder!=Encoder::Ffv1 { args.extend(["-thread_queue_size".into(),"16".into()]); }
    args.extend(["-i","pipe:0"].into_iter().map(std::ffi::OsString::from));
    if encoder!=Encoder::Ffv1 { args.extend(["-vf".into(),FHD_VIDEO_FILTER.into()]); }
    args.extend(["-an","-c:v",encoder.name(),"-f","matroska"].into_iter().map(std::ffi::OsString::from));args.push(output.as_os_str().to_owned());args
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
            write_confirmed_chunks(stdin,bytes,&mut offset).await
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
    if let EncoderUse::OperatorApprovedExternalTest{audited_sha256}=&usage{if !external_test_pin_approved(encoder,&digest,audited_sha256){return Err(RecorderError::BlockedAudit);}}
    if let EncoderUse::Production{audited_sha256}=usage{
        if encoder==Encoder::Ffv1 || audited_sha256.len()!=64 || audited_sha256!=digest{return Err(RecorderError::BlockedAudit);}
        let version=tool_output(binary,&["-version".into()],profile.stop_timeout).await?;let text=String::from_utf8_lossy(&version);
        if !strict_production_build(&text){return Err(RecorderError::BlockedAudit);}
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
struct WriteAck { index: u64, bytes: usize, confirmed: usize, outcome: Result<(), RecorderError> }
struct InFlightWrite {
    task: JoinHandle<ChildStdin>,
    sender: tokio::sync::mpsc::Sender<OutputFrame>,
    acknowledgments: tokio::sync::mpsc::Receiver<WriteAck>,
    outstanding: usize,
}

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
    preview: Option<PreviewSelector>,
    latest_preview: Option<Arc<OwnedFrame>>,
    late_recovery: bool,
    in_flight: Option<InFlightWrite>,
    fresh_origin: bool,
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
        Self::start_with_clock(approval, root, plan, journal, Some(origin)).await.map(|(recorder, _)| recorder)
    }

    /// Start a live timeline after digest, durable intent and child creation.
    /// Startup work is not a missed media deadline. Explicit-clock callers of
    /// `start` retain their original timeline and all lateness limits.
    pub async fn start_ready(
        approval: Preflight,
        root: &Path,
        plan: RecordingPlan,
        journal: &mut Journal,
    ) -> Result<(Self, Instant), RecorderError> {
        Self::start_with_clock(approval, root, plan, journal, None).await
    }

    async fn start_with_clock(
        approval: Preflight,
        root: &Path,
        plan: RecordingPlan,
        journal: &mut Journal,
        requested_origin: Option<Instant>,
    ) -> Result<(Self, Instant), RecorderError> {
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
        let origin = requested_origin.unwrap_or_else(Instant::now);
        let pacer_profile = profile.pacer().map_err(RecorderError::Pacer)?;
        let pacer = Pacer::new(pacer_profile, origin).map_err(RecorderError::Pacer)?;
        Ok((Self {
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
            preview: None,
            latest_preview: None,
            late_recovery: false,
            in_flight: None,
            fresh_origin: requested_origin.is_none(),
            #[cfg(test)]
            disk_full: false,
        }, origin))
    }

    /// Live capture: host stalls or a full queue drop frames instead of interrupting.
    pub fn enable_late_recovery(&mut self) { self.late_recovery = true; }

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
        self.queue.len() + self.in_flight.as_ref().map_or(0, |worker| worker.outstanding)
    }

    /// Enable bounded preview decimation at `fps_num`/`fps_den`. Off by
    /// default; must be called while `Recording` and before any selected
    /// slot or queued frame. Validates before mutation.
    pub fn enable_preview(&mut self, fps_num: u32, fps_den: u32) -> Result<(), RecorderError> {
        if self.state != RecordingState::Recording {
            return Err(RecorderError::State);
        }
        if self.preview.is_some() {
            return Err(RecorderError::State);
        }
        if self.pacer.counters().selected != 0 || !self.queue.is_empty() {
            return Err(RecorderError::State);
        }
        let record = self.profile.pacer().map_err(RecorderError::Pacer)?;
        let selector =
            PreviewSelector::new(record, fps_num, fps_den).map_err(RecorderError::Pacer)?;
        self.preview = Some(selector);
        Ok(())
    }

    /// Copy of the preview counters, or `None` when preview is off.
    pub fn preview_counters(&self) -> Option<PreviewCounters> {
        self.preview.as_ref().map(|selector| selector.counters())
    }

    /// Take the newest held preview frame, if any. At most one is stored;
    /// pixels are shared via [`Arc`], never copied.
    pub fn take_preview(&mut self) -> Option<Arc<OwnedFrame>> {
        self.latest_preview.take()
    }

    /// Offer a frame with exact admitted geometry. Never encodes or blocks.
    pub fn admit(&mut self, frame: OwnedFrame, now: Instant) -> Result<(), RecorderError> {
        if self.state != RecordingState::Recording {
            return Err(RecorderError::State);
        }
        let expected = match self.profile.frame_bytes() {
            Ok(bytes) => bytes,
            Err(err) => {
                self.latest_preview = None;
                self.state = RecordingState::Interrupted;
                return Err(err);
            }
        };
        if frame.width() != self.profile.width
            || frame.height() != self.profile.height
            || frame.pixels().len() != expected
        {
            self.latest_preview = None;
            self.state = RecordingState::Interrupted;
            return Err(RecorderError::InvalidProfile);
        }
        if self.fresh_origin {
            // No media slot has been admitted yet. Setup of another feed must
            // not consume this feed's first deadline.
            self.pacer = Pacer::new(self.profile.pacer().map_err(RecorderError::Pacer)?, now).map_err(RecorderError::Pacer)?;
            self.fresh_origin = false;
        }
        if let Err(err) = self.pacer.offer(frame, now) {
            self.latest_preview = None;
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
        if self.late_recovery {
            self.pacer.skip_late(now);
            if self.queue_len() >= self.profile.queue_capacity {
                // A busy encoder or host drops frames; it does not end a live recording.
                self.counters.backpressure = self.counters.backpressure.saturating_add(1);
                return Ok(false);
            }
        }
        if self.queue_len() >= self.profile.queue_capacity {
            self.counters.queue_full = self.counters.queue_full.saturating_add(1);
            self.counters.backpressure = self.counters.backpressure.saturating_add(1);
            self.latest_preview = None;
            self.state = RecordingState::Interrupted;
            return Err(RecorderError::QueueFull);
        }
        match self.pacer.select(now) {
            Err(err) => {
                self.latest_preview = None;
                self.state = RecordingState::Interrupted;
                Err(RecorderError::Pacer(err))
            }
            Ok(Some(output)) => {
                if let Some(selector) = self.preview.as_mut() {
                    match selector.observe(&output) {
                        Ok(Some(preview)) => {
                            self.latest_preview = Some(preview);
                        }
                        Ok(None) => {}
                        Err(err) => {
                            self.latest_preview = None;
                            self.state = RecordingState::Interrupted;
                            return Err(RecorderError::Pacer(err));
                        }
                    }
                }
                self.queue.push_back(output);
                Ok(true)
            }
            Ok(None) => Ok(false),
        }
    }

    /// Write the oldest queued slot to the child. Never retries partials.
    fn check_pump_state(&mut self) -> Result<(), RecorderError> {
        if self.state != RecordingState::Recording && self.state != RecordingState::Finalizing { return Err(RecorderError::State); }
        #[cfg(test)]
        if self.disk_full { self.latest_preview = None; self.state = RecordingState::Interrupted; return Err(RecorderError::DiskFull); }
        Ok(())
    }

    /// Submit bounded slots to one ordered pipe owner. The writer can drain
    /// independently of the capture loop; queued and uncollected acknowledgments
    /// share queue_capacity. No frame is counted before write plus flush succeeds.
    pub fn begin_pump(&mut self) -> Result<bool, RecorderError> {
        self.check_pump_state()?;
        if self.queue.is_empty() { return Ok(false); }
        if self.in_flight.is_none() {
            let mut stdin = self.child.stdin.take().ok_or(RecorderError::State)?;
            let (sender, mut input) = tokio::sync::mpsc::channel::<OutputFrame>(self.profile.queue_capacity);
            let (acknowledge, acknowledgments) = tokio::sync::mpsc::channel(self.profile.queue_capacity);
            let timeout = self.child.write_timeout;
            let mut expected = self.counters.written_frames;
            let task = tokio::spawn(async move {
                while let Some(output) = input.recv().await {
                    let bytes = output.frame.pixels().len(); let mut confirmed = 0;
                    let outcome = if output.index != expected { Err(RecorderError::State) } else {
                        match tokio::time::timeout(timeout, write_confirmed_chunks(&mut stdin, output.frame.pixels(), &mut confirmed)).await {
                            Ok(value) => value, Err(_) => Err(RecorderError::WriteTimeout),
                        }
                    };
                    let failed = outcome.is_err();
                    let ack = WriteAck { index: output.index, bytes, confirmed, outcome };
                    drop(output);
                    if acknowledge.send(ack).await.is_err() || failed { break; }
                    expected = expected.saturating_add(1);
                }
                stdin
            });
            self.in_flight = Some(InFlightWrite { task, sender, acknowledgments, outstanding: 0 });
        }
        let worker = self.in_flight.as_mut().ok_or(RecorderError::State)?;
        let mut submitted = false;
        while let Some(output) = self.queue.pop_front() {
            if output.index != self.counters.written_frames.saturating_add(worker.outstanding as u64) {
                self.latest_preview = None; self.state = RecordingState::Interrupted; return Err(RecorderError::State);
            }
            match worker.sender.try_send(output) {
                Ok(()) => { worker.outstanding += 1; submitted = true; },
                Err(tokio::sync::mpsc::error::TrySendError::Full(output)) => { self.queue.push_front(output); break; },
                Err(tokio::sync::mpsc::error::TrySendError::Closed(_)) => {
                    self.latest_preview = None; self.state = RecordingState::Interrupted; return Err(RecorderError::WriteFailed);
                },
            }
        }
        Ok(submitted)
    }

    fn accept_write(&mut self, ack: WriteAck) -> Result<bool, RecorderError> {
        let worker = self.in_flight.as_mut().ok_or(RecorderError::State)?;
        worker.outstanding = worker.outstanding.checked_sub(1).ok_or(RecorderError::State)?;
        self.child.last_partial = ack.confirmed;
        match ack.outcome {
            Ok(()) if ack.index == self.counters.written_frames && ack.confirmed == ack.bytes => {
                self.counters.written_frames = self.counters.written_frames.saturating_add(1);
                self.counters.written_bytes = self.counters.written_bytes.saturating_add(ack.confirmed as u64);
                Ok(true)
            },
            other => {
                let err = match other { Err(value) => value, Ok(()) => RecorderError::State };
                self.counters.partial_bytes = ack.confirmed as u64;
                if err == RecorderError::WriteTimeout { self.counters.backpressure = self.counters.backpressure.saturating_add(1); }
                self.latest_preview = None; self.state = RecordingState::Interrupted; Err(err)
            },
        }
    }

    async fn finish_write(&mut self) -> Result<bool, RecorderError> {
        let Some(worker) = self.in_flight.as_mut() else { return Ok(false); };
        if worker.outstanding == 0 { return Ok(false); }
        match worker.acknowledgments.recv().await {
            Some(ack) => self.accept_write(ack),
            None => { self.latest_preview = None; self.state = RecordingState::Interrupted; Err(RecorderError::WriteFailed) },
        }
    }

    /// Collect confirmed acknowledgments without waiting on pipe I/O.
    pub async fn poll_pump(&mut self) -> Result<bool, RecorderError> {
        self.check_pump_state()?; let mut written = false;
        loop {
            let Some(worker) = self.in_flight.as_mut() else { break; };
            match worker.acknowledgments.try_recv() {
                Ok(ack) => { self.accept_write(ack)?; written = true; },
                Err(tokio::sync::mpsc::error::TryRecvError::Empty) => break,
                Err(tokio::sync::mpsc::error::TryRecvError::Disconnected) if worker.outstanding == 0 => break,
                Err(_) => { self.latest_preview = None; self.state = RecordingState::Interrupted; return Err(RecorderError::WriteFailed); },
            }
        }
        Ok(written)
    }

    /// Compatibility/drain API: collect exactly one confirmed frame.
    pub async fn pump_one(&mut self) -> Result<bool, RecorderError> {
        self.begin_pump()?;
        self.finish_write().await
    }

    async fn close_writer(&mut self) -> Result<(), RecorderError> {
        if let Some(mut worker) = self.in_flight.take() {
            drop(worker.sender);
            match tokio::time::timeout(self.child.stop_timeout, &mut worker.task).await {
                Ok(Ok(stdin)) => self.child.stdin = Some(stdin),
                _ => { worker.task.abort(); let _ = worker.task.await; return Err(RecorderError::StopTimeout); },
            }
        }
        Ok(())
    }

    async fn abort_writer(&mut self) -> Result<(), RecorderError> {
        let outcome = self.child.abort().await;
        if let Some(mut worker) = self.in_flight.take() {
            drop(worker.sender);
            if tokio::time::timeout(self.child.stop_timeout, &mut worker.task).await.is_err() { worker.task.abort(); let _ = worker.task.await; }
        }
        outcome
    }

    /// Abort the child and keep partial evidence; no receipt is emitted.
    pub async fn interrupt(&mut self) -> Result<(), RecorderError> {
        self.latest_preview = None;
        self.state = RecordingState::Interrupted;
        self.abort_writer().await
    }

    /// Drain, finish the child, decode-validate, then publish durably.
    pub async fn stop(&mut self, journal: &mut Journal) -> Result<RecordingReceipt, RecorderError> {
        if self.state == RecordingState::Interrupted {
            self.latest_preview = None;
            let _ = self.abort_writer().await;
            return Err(RecorderError::State);
        }
        if self.state == RecordingState::Complete {
            return Err(RecorderError::State);
        }
        self.latest_preview = None;
        self.state = RecordingState::Finalizing;
        loop {
            match self.pump_one().await {
                Ok(true) => {}
                Ok(false) => break,
                Err(err) => {
                    self.latest_preview = None;
                    self.state = RecordingState::Interrupted;
                    let _ = self.abort_writer().await;
                    return Err(err);
                }
            }
        }
        if let Err(err) = self.close_writer().await {
            self.latest_preview = None; self.state = RecordingState::Interrupted;
            let _ = self.abort_writer().await; return Err(err);
        }
        if let Err(err) = self.child.finish().await {
            self.latest_preview = None;
            self.state = RecordingState::Interrupted;
            let _ = self.abort_writer().await;
            return Err(err);
        }
        if self.counters.written_frames == 0 {
            self.latest_preview = None;
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
            self.latest_preview = None;
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
                self.latest_preview = None;
                self.state = RecordingState::Interrupted;
                return Err(RecorderError::Journal);
            }
        };
        self.latest_preview = None;
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
        Ok(Self{profile,root:root.to_path_buf(),plan,pacer:Pacer::new(profile.pacer().map_err(RecorderError::Pacer)?,origin).map_err(RecorderError::Pacer)?,queue:VecDeque::new(),child,state:RecordingState::Recording,counters:RecorderCounters::default(),binary:std::env::current_exe().map_err(|_|RecorderError::Spawn)?,encoder:Encoder::Ffv1,preview:None,latest_preview:None,late_recovery:false,in_flight:None,fresh_origin:false,disk_full:false})
    }
    pub(crate) fn test_disk_full(&mut self){self.disk_full=true;}
    pub(crate) fn test_argv(profile:VideoProfile,encoder:Encoder,path:&Path)->Vec<std::ffi::OsString>{complete_argv(profile,encoder,path)}
}

#[cfg(test)]
impl Recorder {pub(crate) fn test_encoder_absent()->Result<(),RecorderError>{require_encoder(b"Encoders:\n V..... ffv1 Lossless video\n",Encoder::Nvenc)}}

#[cfg(test)]
impl Preflight {
    pub(crate) fn test_synthetic(binary:PathBuf,digest:String,encoder:Encoder,profile:VideoProfile)->Self{Self{binary,digest,encoder,profile}}
}

// A successful Tokio write may only enqueue a Windows blocking-pipe write.
// Flush each chunk inside the same frame deadline before counting its bytes.
pub(crate) async fn write_confirmed_chunks<W:tokio::io::AsyncWrite+Unpin>(writer:&mut W,bytes:&[u8],confirmed:&mut usize)->Result<(),RecorderError>{
    while *confirmed<bytes.len(){
        let count=writer.write(&bytes[*confirmed..]).await.map_err(|_|if *confirmed==0{RecorderError::WriteFailed}else{RecorderError::PartialFrame})?;
        if count==0{return Err(RecorderError::PartialFrame);}
        writer.flush().await.map_err(|_|if *confirmed==0{RecorderError::WriteFailed}else{RecorderError::PartialFrame})?;
        *confirmed+=count;
    }
    Ok(())
}

// OK-5 authorizes this exact external tool for operator trials, not distribution
// or certification as the audited production build. Never a software fallback.
const OPERATOR_EXTERNAL_TEST_SHA256: &str = "c6caa1894d3276556c84b36acc1d219d69f972c0a7525b23f1cad2bdf05c4231";
impl EncoderUse {
    pub(crate) fn operator_selected(sha256: String) -> Self {
        if sha256 == OPERATOR_EXTERNAL_TEST_SHA256 { Self::OperatorApprovedExternalTest { audited_sha256: sha256 } }
        else { Self::Production { audited_sha256: sha256 } }
    }
}
fn external_test_pin_approved(encoder: Encoder, actual: &str, supplied: &str) -> bool {
    encoder != Encoder::Ffv1 && actual == OPERATOR_EXTERNAL_TEST_SHA256 && supplied == OPERATOR_EXTERNAL_TEST_SHA256
}
fn strict_production_build(text: &str) -> bool {
    text.starts_with("ffmpeg version 8.1.3") && !["--enable-libx264","--enable-gpl","--enable-nonfree"].iter().any(|flag| text.contains(flag))
}
#[cfg(test)] mod approved_external_policy_tests {
    use super::*;
    #[test] fn approved_external_requires_exact_pin_and_hardware() {
        for encoder in [Encoder::Nvenc, Encoder::Amf, Encoder::Qsv] {
            assert!(external_test_pin_approved(encoder, OPERATOR_EXTERNAL_TEST_SHA256, OPERATOR_EXTERNAL_TEST_SHA256));
            assert!(!external_test_pin_approved(encoder, &"f".repeat(64), OPERATOR_EXTERNAL_TEST_SHA256));
            assert!(!external_test_pin_approved(encoder, OPERATOR_EXTERNAL_TEST_SHA256, &"f".repeat(64)));
        }
        assert!(!external_test_pin_approved(Encoder::Ffv1, OPERATOR_EXTERNAL_TEST_SHA256, OPERATOR_EXTERNAL_TEST_SHA256));
    }
    #[test] fn operator_selection_never_falls_back_to_development() {
        assert!(matches!(EncoderUse::operator_selected(OPERATOR_EXTERNAL_TEST_SHA256.into()), EncoderUse::OperatorApprovedExternalTest { .. }));
        assert!(matches!(EncoderUse::operator_selected("f".repeat(64)), EncoderUse::Production { .. }));
    }
    #[test] fn strict_production_policy_is_preserved() {
        assert!(strict_production_build("ffmpeg version 8.1.3\nconfiguration: --enable-nvenc"));
        for text in ["ffmpeg version n8.1.3", "ffmpeg version 8.1.4", "ffmpeg version 8.1.3 --enable-gpl", "ffmpeg version 8.1.3 --enable-libx264", "ffmpeg version 8.1.3 --enable-nonfree"] { assert!(!strict_production_build(text)); }
    }
    #[test] fn fhd_filter_is_present_and_exact_for_odd_wider_and_taller_inputs() {
        for (width, height) in [(856, 481), (2560, 1080), (1080, 1920), (1920, 1080)] {
            let profile = VideoProfile { width, height, fps_num: 60, fps_den: 1, queue_capacity: 4, write_timeout: Duration::from_secs(2), stop_timeout: Duration::from_secs(4), max_lateness_ns: 100_000_000, max_hold_ns: 1_000_000_000 };
            for encoder in [Encoder::Nvenc, Encoder::Amf, Encoder::Qsv] {
                let args = complete_argv(profile, encoder, Path::new("segment.partial.mkv"));
                let actual: Vec<_> = args.iter().map(|s| s.to_str().unwrap()).collect();
                let size_pos = actual.iter().position(|s| *s == "-video_size").unwrap();
                assert_eq!(actual[size_pos + 1], format!("{width}x{height}"));
                let input_pos = actual.iter().position(|s| *s == "-i").unwrap();
                let vf_pos = actual.iter().position(|s| *s == "-vf").unwrap();
                let cv_pos = actual.iter().position(|s| *s == "-c:v").unwrap();
                assert!(input_pos < vf_pos && vf_pos < cv_pos);
                assert_eq!(actual.iter().filter(|s| **s == "-vf").count(), 1);
                assert_eq!(actual[vf_pos + 1], "scale=1920:1080:force_original_aspect_ratio=decrease:flags=bicubic,pad=1920:1080:(ow-iw)/2:(oh-ih)/2");
            }
        }
    }
}

impl Drop for Recorder {
    fn drop(&mut self) { if let Some(pending) = self.in_flight.take() { pending.task.abort(); } }
}
