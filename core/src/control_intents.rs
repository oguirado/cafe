//! Confirmed control admission, durable intent/outcome facts and authenticated dispatch.
//! Trusted operator approval is a local API. The broker cannot mint confirmations.
#![forbid(unsafe_code)]
use std::collections::BTreeMap;
use std::path::{Component, Path, PathBuf};
use std::sync::{Arc, Mutex, RwLock, atomic::{AtomicBool,Ordering}};
use std::time::{Duration, Instant};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use crate::authority_auth::{AuthenticatedBrokerPeer, ControlHandler};
use crate::authority_journal::{canonical_bytes, sha256_hex, ControlCompletion, ControlIntentRecord, ControlOperation, ControlResultRecord, Journal};
use crate::capsule_registry::{Clock,Capsule};
use crate::capsule_pairing::Binding;

const MAX_PENDING_CONFIRMATIONS: usize = 64;
const MAX_CONFIRMATION_TTL: Duration = Duration::from_secs(30);
const MAX_INTENT_BYTES: usize = 16384;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Intent {
    schema: String, kind: String, request_id: String,
    authority_epoch: Clock, expected_revision: Clock, body: IntentBody,
}
#[derive(Deserialize)]
#[serde(tag="op", rename_all="snake_case", deny_unknown_fields)]
enum IntentBody {
    SetFolder { confirmation_token: String, folder: String },
    StartRecording { confirmation_token: String, capsule_id: String },
    StopRecording { confirmation_token: String, capsule_id: String },
    SendToObs { confirmation_token: String, capsule_id: String },
}
impl IntentBody {
    fn token(&self) -> &str { match self { Self::SetFolder{confirmation_token,..} | Self::StartRecording{confirmation_token,..} | Self::StopRecording{confirmation_token,..} | Self::SendToObs{confirmation_token,..} => confirmation_token } }
    fn action(&self) -> ControlAction { match self {
        Self::SetFolder{folder,..} => ControlAction::SetFolder(PathBuf::from(folder)),
        Self::StartRecording{capsule_id,..} => ControlAction::StartRecording(capsule_id.clone()),
        Self::StopRecording{capsule_id,..} => ControlAction::StopRecording(capsule_id.clone()),
        Self::SendToObs{capsule_id,..} => ControlAction::SendToObs(capsule_id.clone()),
    }}
}

/// Trusted local approval input. Never deserialize this type from broker data.
#[derive(Clone)]
pub(crate) enum ControlAction { SetFolder(PathBuf), StartRecording(String), StopRecording(String), SendToObs(String) }
impl ControlAction {
    fn operation(&self) -> ControlOperation { match self { Self::SetFolder(_)=>ControlOperation::SetFolder, Self::StartRecording(_)=>ControlOperation::StartRecording, Self::StopRecording(_)=>ControlOperation::StopRecording, Self::SendToObs(_)=>ControlOperation::SendToObs } }
    fn capsule(&self) -> Option<&str> { match self { Self::SetFolder(_)=>None, Self::StartRecording(id)|Self::StopRecording(id)|Self::SendToObs(id)=>Some(id) } }
    fn body(&self, token: &str) -> Value { match self {
        Self::SetFolder(path)=>json!({"op":"set_folder","folder":path.to_str(),"confirmation_token":token}),
        Self::StartRecording(id)=>json!({"op":"start_recording","capsule_id":id,"confirmation_token":token}),
        Self::StopRecording(id)=>json!({"op":"stop_recording","capsule_id":id,"confirmation_token":token}),
        Self::SendToObs(id)=>json!({"op":"send_to_obs","capsule_id":id,"confirmation_token":token}),
    }}
}

/// Opaque confirmation visible only to the trusted local operator caller.
/// Deliberately has no Debug or Serialize implementation.
pub(crate) struct OperatorConfirmation { wire: Value }
impl OperatorConfirmation { pub(crate) fn into_private_wire(self) -> Value { self.wire } }

/// Folder capability produced only by canonicalization under an operator-selected root.
#[derive(Clone)]
pub(crate) struct ApprovedFolder { canonical: PathBuf }
impl ApprovedFolder {
    pub(crate) fn path(&self) -> &Path { &self.canonical }
    /// The actor calls this again immediately before opening output files.
    pub(crate) fn revalidate(&self)->Result<&Path,&'static str> {
        if canonical_folder(&self.canonical)?!=self.canonical { return Err("folder_not_authorized"); }
        Ok(&self.canonical)
    }
}

/// Actual production adapters implement these typed commands; there is no counter effect.
pub(crate) enum ControlDispatch<'a> {
    SetFolder { folder: &'a ApprovedFolder, effect_id: &'a str, epoch: Clock },
    StartRecording { capsule_id: &'a str, capsule: &'a Capsule, folder: &'a ApprovedFolder, effect_id: &'a str, epoch: Clock },
    StopRecording { capsule_id: &'a str, capsule: &'a Capsule, effect_id: &'a str, epoch: Clock },
    SendToObs { capsule_id: &'a str, capsule: &'a Capsule, effect_id: &'a str, epoch: Clock },
}
/// Pending means bounded queue admission only, never an acknowledged effect.
pub(crate) enum DispatchOutcome { Applied, Interrupted, Pending }
pub(crate) enum ActorCompletion { Applied, Interrupted }

/// Owned actor job freezes capsule/feed generations; no secret confirmation is copied.
pub(crate) enum ActorControlAction {
    SetFolder(ApprovedFolder),
    StartRecording { capsule: Capsule, folder: ApprovedFolder },
    StopRecording(Capsule),
    SendToObs(Capsule),
}
pub(crate) struct ActorControlJob { pub effect_id: String, pub authority_epoch: Clock, pub action: ActorControlAction, pub window_bindings: Vec<Binding> }
impl ActorControlJob {
    /// Compare the frozen pairing snapshot, then resolve its original opaque handles in the actor.
    pub(crate) fn matches_current_bindings(&self,current:&[Binding])->bool {
        let mut selected=current.iter().filter(|binding|self.window_bindings.iter().any(|prior|prior.feed_id==binding.feed_id)).collect::<Vec<_>>();
        selected.sort_by(|a,b|a.feed_id.cmp(&b.feed_id));
        selected.len()==self.window_bindings.len() && selected.iter().zip(&self.window_bindings).all(|(a,b)|*a==b)
    }
    /// The actor must also validate its opaque window binding before touching an actual window.
    pub(crate) fn matches_current_capsule(&self, epoch:Clock, current:&Capsule)->bool {
        if self.authority_epoch!=epoch { return false; }
        match &self.action {ActorControlAction::SetFolder(_)=>false,ActorControlAction::StartRecording{capsule,..}|ActorControlAction::StopRecording(capsule)|ActorControlAction::SendToObs(capsule)=>capsule==current}
    }
}
impl ControlDispatch<'_> {
    fn into_owned(self)->ActorControlJob {match self {
        Self::SetFolder{folder,effect_id,epoch}=>ActorControlJob{effect_id:effect_id.into(),authority_epoch:epoch,action:ActorControlAction::SetFolder(folder.clone()),window_bindings:Vec::new()},
        Self::StartRecording{capsule,folder,effect_id,epoch,..}=>ActorControlJob{effect_id:effect_id.into(),authority_epoch:epoch,action:ActorControlAction::StartRecording{capsule:capsule.clone(),folder:folder.clone()},window_bindings:Vec::new()},
        Self::StopRecording{capsule,effect_id,epoch,..}=>ActorControlJob{effect_id:effect_id.into(),authority_epoch:epoch,action:ActorControlAction::StopRecording(capsule.clone()),window_bindings:Vec::new()},
        Self::SendToObs{capsule,effect_id,epoch,..}=>ActorControlJob{effect_id:effect_id.into(),authority_epoch:epoch,action:ActorControlAction::SendToObs(capsule.clone()),window_bindings:Vec::new()},
    }}
}
/// Production bridge: only successful bounded try_send yields Pending, never Committed.
pub(crate) struct ControlCommandSender { sender:tokio::sync::mpsc::Sender<ActorControlJob>, bindings:Arc<RwLock<Vec<Binding>>> }
impl ControlCommandSender {
    pub(crate) fn new(sender:tokio::sync::mpsc::Sender<ActorControlJob>,bindings:Arc<RwLock<Vec<Binding>>>)->Self {Self{sender,bindings}}
}
impl ControlEffects for ControlCommandSender {
    fn dispatch(&mut self,command:ControlDispatch<'_>,_journal:&mut Journal)->DispatchOutcome {
        let mut job=command.into_owned();
        let capsule=match &job.action {ActorControlAction::SetFolder(_)=>None,ActorControlAction::StartRecording{capsule,..}|ActorControlAction::StopRecording(capsule)|ActorControlAction::SendToObs(capsule)=>Some(capsule)};
        if let Some(capsule)=capsule {
            if capsule.feeds.is_empty(){return DispatchOutcome::Interrupted;}
            let Ok(bindings)=self.bindings.read() else{return DispatchOutcome::Interrupted;};
            for feed in capsule.feeds.values() {
                let selected=bindings.iter().filter(|binding|binding.capsule_id==capsule.capsule_id && binding.person_label==capsule.person_label && binding.feed_id==feed.feed_id && binding.role==feed.role && binding.feed_generation==feed.generation).collect::<Vec<_>>();
                if selected.len()!=1 || selected[0].window_generation.0==0 || selected[0].layout_revision.0==0 {return DispatchOutcome::Interrupted;}
                job.window_bindings.push(selected[0].clone());
            }
            job.window_bindings.sort_by(|a,b|a.feed_id.cmp(&b.feed_id));
        }
        if self.sender.try_send(job).is_ok() {DispatchOutcome::Pending}else{DispatchOutcome::Interrupted}
    }
}
pub(crate) trait ControlEffects: Send {
    /// The same journal permits stop/finalization to publish media before its control receipt.
    fn dispatch(&mut self, command: ControlDispatch<'_>, journal: &mut Journal) -> DispatchOutcome;
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all="snake_case")]
pub(crate) enum ControlState { Pending, Committed, Rejected, Interrupted, Blocked }
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub(crate) struct ControlReceipt { schema: &'static str, op: ControlOperation, effect_id: String }
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub(crate) struct ControlReply { pub request_id: String, pub state: ControlState, pub revision: Clock, pub error: Option<&'static str>, pub receipt: Option<ControlReceipt> }

// Private Rust-only origin. No Deserialize/Serialize or external constructor.
enum AdmissionPeer<'a> { Broker(&'a AuthenticatedBrokerPeer), Operator(Clock) }
impl AdmissionPeer<'_> {
    fn identity(&self)->&str {match self {Self::Broker(peer)=>peer.identity(),Self::Operator(_)=>"operator"}}
    fn epoch(&self)->u64 {match self {Self::Broker(peer)=>peer.epoch(),Self::Operator(epoch)=>epoch.0}}
    fn is_live(&self)->bool {match self {Self::Broker(peer)=>peer.is_live(),Self::Operator(_)=>true}}
    fn authorized(&self)->bool {match self {Self::Broker(peer)=>peer.identity()=="broker",Self::Operator(_)=>true}}
}
struct Confirmation { fingerprint: String, identity: String, expires: Instant }
struct Historic { intent: ControlIntentRecord, outcome: Option<(ControlResultRecord, Clock)>, in_flight: bool }

/// Owns the writer and serializes admission with effect dispatch. No remote issuer exists.
pub(crate) struct ControlRuntime<E: ControlEffects> {
    journal: Journal, epoch: Clock, effects: E, roots: Vec<PathBuf>,
    confirmations: BTreeMap<String, Confirmation>, history: BTreeMap<String, Historic>,
    selected_folder: Option<ApprovedFolder>, pending_folders: BTreeMap<String,ApprovedFolder>, poisoned: bool,
}

fn identifier(value: &str) -> bool { !value.is_empty() && value.len()<=96 && value.as_bytes()[0].is_ascii_alphanumeric() && value.bytes().all(|b|b.is_ascii_alphanumeric()||b"_.:-".contains(&b)) }
fn token_shape(value: &str) -> bool { (32..=256).contains(&value.len()) && value.bytes().all(|b|b.is_ascii_alphanumeric()||b"_-".contains(&b)) }
fn contains_link(path: &Path) -> bool {
    path.ancestors().any(|part| match std::fs::symlink_metadata(part) {
        Ok(meta) => {
            #[cfg(windows)] { use std::os::windows::fs::MetadataExt; if meta.file_attributes() & 0x400 != 0 { return true; } }
            meta.file_type().is_symlink()
        },
        Err(_) => true,
    })
}
fn canonical_folder(path: &Path) -> Result<PathBuf, &'static str> {
    if !path.is_absolute() || path.components().any(|part| matches!(part, Component::ParentDir)) || path.to_string_lossy().split(['/', '\\']).any(|part|part=="..") || contains_link(path) { return Err("folder_not_authorized"); }
    #[cfg(windows)] {
        use std::path::Prefix;
        if !matches!(path.components().next(), Some(Component::Prefix(prefix)) if matches!(prefix.kind(), Prefix::Disk(_) | Prefix::VerbatimDisk(_))) { return Err("folder_not_authorized"); }
    }
    let canonical=path.canonicalize().map_err(|_|"folder_not_authorized")?;
    if !canonical.is_dir() || canonical.to_str().is_none() { return Err("folder_not_authorized"); }
    Ok(canonical)
}

impl<E: ControlEffects> ControlRuntime<E> {
    pub(crate) fn open(journal: Journal, epoch: Clock, effects: E, allowed_roots: &[PathBuf]) -> Result<Self, &'static str> {
        if epoch.0==0 || journal.authority_epoch()!=epoch || allowed_roots.is_empty() { return Err("control_configuration_invalid"); }
        let mut roots=Vec::new();
        for root in allowed_roots { roots.push(canonical_folder(root)?); }
        let mut history: BTreeMap<String, Historic>=BTreeMap::new();
        for record in journal.records() {
            if record.authority_epoch!=epoch { return Err("control_epoch_mismatch"); }
            match record.kind.as_str() {
                "confirmed_control_intent" => {
                    let intent: ControlIntentRecord=serde_json::from_value(record.payload.clone()).map_err(|_|"control_journal_invalid")?;
                    history.insert(intent.request_id.clone(),Historic{intent,outcome:None,in_flight:false});
                },
                "confirmed_control_result" => {
                    let result: ControlResultRecord=serde_json::from_value(record.payload.clone()).map_err(|_|"control_journal_invalid")?;
                    let prior=history.get_mut(&result.request_id).ok_or("control_journal_invalid")?;
                    prior.outcome=Some((result,record.sequence));
                },
                _=>{},
            }
        }
        // Folder plaintext and confirmation tokens are intentionally not restored.
        // An interrupted or previously active effect is never automatically redispatched.
        Ok(Self{journal,epoch,effects,roots,confirmations:BTreeMap::new(),history,selected_folder:None,pending_folders:BTreeMap::new(),poisoned:false})
    }
    #[cfg(test)]
    pub(crate) fn expire_confirmations_for_test(&mut self) { for value in self.confirmations.values_mut() { value.expires=Instant::now(); } }
    /// The sole authority actor may borrow this; never hold a synchronous mutex across await.
    pub(crate) fn journal_for_media_actor(&mut self)->&mut Journal { &mut self.journal }
    pub(crate) fn capsule_snapshot(&self) -> crate::capsule_registry::Snapshot { self.journal.snapshot() }
    /// Trusted private parent after a native directory picker, never broker data.
    /// No filesystem effect or grant occurs for stale epoch/revision or invalid id.
    pub(crate) fn handle_operator_folder(&mut self,id:&str,epoch:Clock,expected:Clock,path:PathBuf)->ControlReply {
        if self.poisoned{return self.reply(id,ControlState::Blocked,Some("authority_requires_recovery"));}
        if epoch!=self.epoch{return self.reply(id,ControlState::Rejected,Some("old_authority_epoch"));}
        if !identifier(id){return self.reply("invalid",ControlState::Rejected,Some("invalid_control_intent"));}
        if self.history.contains_key(id){return self.handle_operator(id,epoch,expected,ControlAction::SetFolder(path));}
        if expected!=self.revision(){return self.reply(id,ControlState::Rejected,Some("stale_revision"));}
        let canonical=match canonical_folder(&path){Ok(value)=>value,Err(code)=>return self.reply(id,ControlState::Rejected,Some(code))};
        if !self.roots.contains(&canonical){if self.roots.len()>=16{return self.reply(id,ControlState::Blocked,Some("folder_selection_capacity"));}self.roots.push(canonical.clone());}
        self.handle_operator(id,epoch,expected,ControlAction::SetFolder(canonical))
    }
    pub(crate) fn folder_configured(&self)->bool{self.selected_folder.is_some()}
    pub(crate) fn known_control_request(&self,id:&str)->bool{self.history.contains_key(id)}
    pub(crate) fn revision(&self) -> Clock { self.journal.records().last().map_or(Clock(0),|r|r.sequence) }
    fn reply(&self, id:&str,state:ControlState,error:Option<&'static str>) -> ControlReply { ControlReply{request_id:id.into(),state,revision:self.revision(),error,receipt:None} }
    fn normalized(&self, action:&ControlAction) -> Result<ControlAction,&'static str> {
        match action {
            ControlAction::SetFolder(path) => {
                if !path.to_str().is_some_and(|value| !value.is_empty() && value.chars().count()<=4096) { return Err("folder_not_authorized"); }
                let canonical=canonical_folder(path)?;
                if !self.roots.iter().any(|root|canonical.starts_with(root)) { return Err("folder_not_authorized"); }
                Ok(ControlAction::SetFolder(canonical))
            },
            other => {
                let Some(id)=other.capsule() else { return Err("invalid_control_intent"); };
                if !identifier(id) || !self.journal.snapshot().capsules.contains_key(id) { return Err("capsule_unavailable"); }
                Ok(other.clone())
            },
        }
    }
    fn fingerprint(&self,id:&str,revision:Clock,action:&ControlAction) -> Result<String,&'static str> {
        let body=match action {
            ControlAction::SetFolder(path)=>json!({"op":"set_folder","folder":path.to_str().ok_or("folder_not_authorized")?}),
            ControlAction::StartRecording(id)=>json!({"op":"start_recording","capsule_id":id}),
            ControlAction::StopRecording(id)=>json!({"op":"stop_recording","capsule_id":id}),
            ControlAction::SendToObs(id)=>json!({"op":"send_to_obs","capsule_id":id}),
        };
        let value=json!({"schema":"cafe.control_intent.v1","kind":"control_intent","request_id":id,"authority_epoch":self.epoch,"expected_revision":revision,"body":body});
        Ok(sha256_hex(&canonical_bytes(&value).map_err(|_|"invalid_control_intent")?))
    }
    /// Call only after a trusted operator gesture on the local authority UI path.
    /// Neither ControlHandler nor broker capabilities expose this method.
    pub(crate) fn confirm_for_operator(&mut self, identity:&str, request_id:&str, action:ControlAction, ttl:Duration) -> Result<OperatorConfirmation,&'static str> {
        self.confirm_at(identity,request_id,action,ttl,Instant::now())
    }
    fn confirm_at(&mut self, identity:&str, request_id:&str, action:ControlAction, ttl:Duration, now:Instant) -> Result<OperatorConfirmation,&'static str> {
        if identity!="broker" {return Err("confirmation_denied");}
        self.issue_confirmation(identity,request_id,action,ttl,now)
    }
    fn issue_confirmation(&mut self, identity:&str, request_id:&str, action:ControlAction, ttl:Duration, now:Instant) -> Result<OperatorConfirmation,&'static str> {
        if self.poisoned || !matches!(identity,"broker"|"operator") || !identifier(request_id) || ttl.is_zero() || ttl>MAX_CONFIRMATION_TTL || self.history.contains_key(request_id) { return Err("confirmation_denied"); }
        let normalized=self.normalized(&action)?;
        self.confirmations.retain(|_,c|c.expires>now);
        if self.confirmations.len()>=MAX_PENDING_CONFIRMATIONS { return Err("confirmation_capacity"); }
        let bytes=crate::authority_auth::entropy().map_err(|_|"confirmation_entropy_unavailable")?;
        let token=bytes.iter().map(|b|format!("{b:02x}")).collect::<String>();
        if self.confirmations.contains_key(&token) { return Err("confirmation_entropy_collision"); }
        let expires=now.checked_add(ttl).ok_or("confirmation_denied")?;
        let wire=json!({"schema":"cafe.control_intent.v1","kind":"control_intent","request_id":request_id,"authority_epoch":self.epoch,"expected_revision":self.revision(),"body":action.body(&token)});
        if serde_json::to_vec(&json!({"epoch":self.epoch,"capability":"control","body":&wire})).map_err(|_|"invalid_control_intent")?.len()+1>MAX_INTENT_BYTES { return Err("invalid_control_intent"); }
        self.confirmations.insert(token.clone(),Confirmation{fingerprint:self.fingerprint(request_id,self.revision(),&normalized)?,identity:identity.into(),expires});
        Ok(OperatorConfirmation{wire})
    }
    fn replay_reply(&self,id:&str,args:&str,identity:&str)->Option<ControlReply> {
        let prior=self.history.get(id)?;
        if prior.intent.args_sha256!=args || prior.intent.identity_sha256!=identity {return Some(self.reply(id,ControlState::Rejected,Some("request_id_conflict")));}
        Some(match &prior.outcome {
            Some((result,revision)) if result.state==ControlCompletion::Committed=>ControlReply{request_id:id.into(),state:ControlState::Committed,revision:*revision,error:None,receipt:Some(ControlReceipt{schema:"cafe.control_receipt.v1",op:prior.intent.operation.clone(),effect_id:prior.intent.effect_id.clone()})},
            None if prior.in_flight=>self.reply(id,ControlState::Pending,None),
            _=>self.reply(id,ControlState::Interrupted,Some("prior_effect_not_replayed")),
        })
    }
    /// Called only by the private stdin owner after closed local-UI decoding.
    /// It never returns a confirmation, grants a remote issuer or fabricates a peer.
    pub(crate) fn handle_operator(&mut self,id:&str,epoch:Clock,expected_revision:Clock,action:ControlAction)->ControlReply {
        if !identifier(id) {return self.reply("invalid",ControlState::Rejected,Some("invalid_control_intent"));}
        if self.poisoned {return self.reply(id,ControlState::Blocked,Some("authority_requires_recovery"));}
        if epoch!=self.epoch {return self.reply(id,ControlState::Rejected,Some("control_identity_or_epoch_denied"));}
        let normalized=match self.normalized(&action) {Ok(value)=>value,Err(code)=>return self.reply(id,ControlState::Rejected,Some(code))};
        let args=match self.fingerprint(id,expected_revision,&normalized) {Ok(value)=>value,Err(code)=>return self.reply(id,ControlState::Rejected,Some(code))};
        let identity=sha256_hex(b"operator");
        if let Some(reply)=self.replay_reply(id,&args,&identity) {return reply;}
        if expected_revision!=self.revision() {return self.reply(id,ControlState::Rejected,Some("stale_revision"));}
        let now=Instant::now();
        let wire=match self.issue_confirmation("operator",id,normalized,MAX_CONFIRMATION_TTL,now) {Ok(value)=>value.into_private_wire(),Err(code)=>return self.reply(id,ControlState::Rejected,Some(code))};
        self.handle_admitted(AdmissionPeer::Operator(epoch),&wire,now)
    }
    pub(crate) fn handle(&mut self, peer:&AuthenticatedBrokerPeer, value:&Value) -> ControlReply { self.handle_at(peer,value,Instant::now()) }
    fn handle_at(&mut self, peer:&AuthenticatedBrokerPeer, value:&Value, now:Instant) -> ControlReply {
        self.handle_admitted(AdmissionPeer::Broker(peer),value,now)
    }
    fn handle_admitted(&mut self, peer:AdmissionPeer<'_>, value:&Value, now:Instant) -> ControlReply {
        let invalid=||ControlReply{request_id:"invalid".into(),state:ControlState::Rejected,revision:self.revision(),error:Some("invalid_control_intent"),receipt:None};
        let Ok(bytes)=serde_json::to_vec(value) else { return invalid(); };
        if bytes.len()>MAX_INTENT_BYTES { return invalid(); }
        let Ok(intent)=serde_json::from_value::<Intent>(value.clone()) else { return invalid(); };
        if intent.schema!="cafe.control_intent.v1" || intent.kind!="control_intent" || !identifier(&intent.request_id) || !token_shape(intent.body.token()) { return invalid(); }
        let reflected=match &intent.body { IntentBody::SetFolder{folder,..}=>folder.contains(intent.body.token()),_=>intent.body.action().capsule().is_some_and(|id|id.contains(intent.body.token())) };
        if intent.request_id.contains(intent.body.token()) || reflected { return invalid(); }
        let id=&intent.request_id;
        if self.poisoned { return self.reply(id,ControlState::Blocked,Some("authority_requires_recovery")); }
        if !peer.is_live() || !peer.authorized() || peer.epoch()!=self.epoch.0 || intent.authority_epoch!=self.epoch { return self.reply(id,ControlState::Rejected,Some("control_identity_or_epoch_denied")); }
        let action=match self.normalized(&intent.body.action()) { Ok(action)=>action,Err(code)=>return self.reply(id,ControlState::Rejected,Some(code)) };
        let args=match self.fingerprint(id,intent.expected_revision,&action) {Ok(value)=>value,Err(code)=>return self.reply(id,ControlState::Rejected,Some(code))};
        let identity=sha256_hex(peer.identity().as_bytes());
        if let Some(reply)=self.replay_reply(id,&args,&identity) {return reply;}
        if intent.expected_revision!=self.revision() { return self.reply(id,ControlState::Rejected,Some("stale_revision")); }
        let Some(confirmation)=self.confirmations.remove(intent.body.token()) else { return self.reply(id,ControlState::Rejected,Some("confirmation_required")); };
        if confirmation.identity!=peer.identity() || confirmation.fingerprint!=args || confirmation.expires<=now { return self.reply(id,ControlState::Rejected,Some("confirmation_binding_or_expiry")); }
        if matches!(action,ControlAction::StartRecording(_)) && self.selected_folder.is_none() { return self.reply(id,ControlState::Blocked,Some("recording_folder_required")); }
        let effect_id=format!("ctl.{}",sha256_hex(id.as_bytes()));
        let record=ControlIntentRecord{request_id:id.clone(),identity_sha256:identity,operation:action.operation(),args_sha256:args,capsule_id:action.capsule().map(str::to_owned),expected_revision:intent.expected_revision,effect_id:effect_id.clone()};
        if !peer.is_live() {return self.reply(id,ControlState::Rejected,Some("control_peer_revoked"));}
        if self.journal.append_confirmed_intent(record.clone()).is_err() { self.poisoned=true;return self.reply(id,ControlState::Blocked,Some("intent_not_durable")); }
        self.history.insert(id.clone(),Historic{intent:record,outcome:None,in_flight:true});
        if !peer.is_live() {return self.complete_from_actor(&effect_id,ActorCompletion::Interrupted);}
        let new_folder=if let ControlAction::SetFolder(path)=&action {Some(ApprovedFolder{canonical:path.clone()})}else{None};
        // Recheck every selected folder immediately before giving its capability to an adapter.
        let selected=new_folder.as_ref().or(self.selected_folder.as_ref());
        if selected.is_some_and(|folder|canonical_folder(folder.path()).is_err()) {
            self.poisoned=true;return self.reply(id,ControlState::Interrupted,Some("folder_changed_after_admission"));
        }
        let snapshot=self.journal.snapshot();
        let capsule=action.capsule().and_then(|id|snapshot.capsules.get(id));
        let command=match &action {
            ControlAction::SetFolder(_)=>ControlDispatch::SetFolder{folder:new_folder.as_ref().expect("folder invariant"),effect_id:&effect_id,epoch:self.epoch},
            ControlAction::StartRecording(capsule_id)=>ControlDispatch::StartRecording{capsule_id,capsule:capsule.expect("capsule invariant"),folder:self.selected_folder.as_ref().expect("folder precondition"),effect_id:&effect_id,epoch:self.epoch},
            ControlAction::StopRecording(capsule_id)=>ControlDispatch::StopRecording{capsule_id,capsule:capsule.expect("capsule invariant"),effect_id:&effect_id,epoch:self.epoch},
            ControlAction::SendToObs(capsule_id)=>ControlDispatch::SendToObs{capsule_id,capsule:capsule.expect("capsule invariant"),effect_id:&effect_id,epoch:self.epoch},
        };
        if !peer.is_live() {return self.complete_from_actor(&effect_id,ActorCompletion::Interrupted);}
        let outcome=self.effects.dispatch(command,&mut self.journal);
        if let Some(folder)=new_folder { self.pending_folders.insert(effect_id.clone(),folder); }
        match outcome {
            DispatchOutcome::Pending=>self.reply(id,ControlState::Pending,None),
            DispatchOutcome::Applied=>self.complete_from_actor(&effect_id,ActorCompletion::Applied),
            DispatchOutcome::Interrupted=>self.complete_from_actor(&effect_id,ActorCompletion::Interrupted),
        }
    }
    /// Trusted internal actor completion only. There is no corresponding broker capability.
    /// Media must be validated/published by the single writer before an Applied stop completion.
    pub(crate) fn complete_from_actor(&mut self,effect_id:&str,outcome:ActorCompletion)->ControlReply {
        let Some((id,prior))=self.history.iter().find(|(_,prior)|prior.intent.effect_id==effect_id) else { return self.reply("invalid",ControlState::Rejected,Some("unknown_actor_completion")); };
        let id=id.clone();let operation=prior.intent.operation.clone();
        let applied=matches!(outcome,ActorCompletion::Applied);
        if self.poisoned { return self.reply(&id,ControlState::Blocked,Some("authority_requires_recovery")); }
        if let Some((result,revision))=&prior.outcome {
            if applied && result.state==ControlCompletion::Committed {
                return ControlReply{request_id:id,state:ControlState::Committed,revision:*revision,error:None,receipt:Some(ControlReceipt{schema:"cafe.control_receipt.v1",op:operation,effect_id:effect_id.into()})};
            }
            return self.reply(&id,ControlState::Interrupted,Some("actor_completion_already_recorded"));
        }
        if !prior.in_flight { return self.reply(&id,ControlState::Interrupted,Some("prior_effect_not_replayed")); }
        let result=ControlResultRecord{request_id:id.clone(),effect_id:effect_id.into(),state:if applied{ControlCompletion::Committed}else{ControlCompletion::Interrupted}};
        let receipt=match self.journal.append_confirmed_result(result.clone()) { Ok(receipt)=>receipt,Err(_)=>{self.poisoned=true;return self.reply(&id,ControlState::Interrupted,Some("result_not_durable"));} };
        let prior=self.history.get_mut(&id).expect("durable intent invariant");prior.outcome=Some((result,receipt.sequence()));prior.in_flight=false;
        let folder=self.pending_folders.remove(effect_id);
        if !applied { return self.reply(&id,ControlState::Interrupted,Some("effect_interrupted")); }
        if folder.is_some() { self.selected_folder=folder; }
        ControlReply{request_id:id,state:ControlState::Committed,revision:receipt.sequence(),error:None,receipt:Some(ControlReceipt{schema:"cafe.control_receipt.v1",op:operation,effect_id:effect_id.into()})}
    }
}

/// Register this adapter with BrokerAuthority::start_control; keep a clone only for trusted UI approval.
pub(crate) struct ControlBroker<E:ControlEffects>(pub(crate) Arc<Mutex<ControlRuntime<E>>>);
impl<E:ControlEffects> ControlHandler for ControlBroker<E> {
    fn handle(&mut self,peer:&AuthenticatedBrokerPeer,intent:&Value)->Value {
        match self.0.lock() {
            Ok(mut runtime)=> {
                let reply=runtime.handle(peer,intent);
                serde_json::to_value(&reply).unwrap_or_else(|_|json!({"request_id":reply.request_id,"state":"blocked","revision":reply.revision,"error":"control_response_unavailable","receipt":null}))
            },
            Err(error)=> {
                json!({"request_id":"invalid","state":"blocked","revision":error.get_ref().revision(),"error":"control_runtime_unavailable","receipt":null})
            },
        }
    }
}


/// Private admission message, consumed only by the authority-owned actor.
/// No Debug/Serialize implementation can expose the private wire confirmation.
pub(crate) struct ActorControlRequest {
    peer:AuthenticatedBrokerPeer, intent:Value,
    reply:std::sync::mpsc::SyncSender<Value>,
}
impl ActorControlRequest {
    pub(crate) fn execute_on<E:ControlEffects>(self,runtime:&mut ControlRuntime<E>) {
        let response=runtime.handle(&self.peer,&self.intent);
        let _=self.reply.try_send(serde_json::to_value(response).unwrap_or_else(|_|json!({"error":"control_actor_unavailable"})));
    }
}
/// Used by the offloaded broker callback; waits only within the explicit admission budget.
pub(crate) struct ControlActorClient { sender:tokio::sync::mpsc::Sender<ActorControlRequest>, wait:Duration }
impl ControlActorClient {
    pub(crate) fn new(sender:tokio::sync::mpsc::Sender<ActorControlRequest>,wait:Duration)->Result<Self,&'static str> {
        if wait.is_zero() || wait>=crate::authority_auth::CONTROL_REPLY_DEADLINE {return Err("control_actor_reply_budget_invalid");}
        Ok(Self{sender,wait})
    }
}
impl ControlHandler for ControlActorClient {
    fn handle(&mut self,peer:&AuthenticatedBrokerPeer,intent:&Value)->Value {
        let active=Arc::new(AtomicBool::new(true));
        let Some(deadline)=Instant::now().checked_add(self.wait) else{return json!({"error":"control_actor_unavailable"});};
        let Some(scoped_peer)=peer.for_actor_request(active.clone(),deadline) else{return json!({"error":"control_actor_unavailable"});};
        let (reply,receiver)=std::sync::mpsc::sync_channel(1);
        let message=ActorControlRequest{peer:scoped_peer,intent:intent.clone(),reply};
        if self.sender.try_send(message).is_err(){active.store(false,Ordering::Release);return json!({"error":"control_actor_queue_unavailable"});}
        match receiver.recv_timeout(self.wait) {
            Ok(response) if Instant::now()<deadline=>response,
            _=>{active.store(false,Ordering::Release);json!({"error":"control_actor_reply_unavailable"})},
        }
    }
}
