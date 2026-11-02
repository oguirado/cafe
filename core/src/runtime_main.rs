//! Single authority owner. Native effects remain explicitly blocked until configured.
use serde::Deserialize;
use std::{path::PathBuf,sync::{Arc,Mutex,Condvar},io::{Read,Write},time::{Duration,Instant}};
use serde_json::{json,Value};
use std::collections::BTreeMap;
use crate::organizer::{Organizer,PlanKind,MAX_CAPSULES,window_token,resolve_frozen_window};
use crate::discord_windows::{classify_discord_window,DiscordWindowClass};
use crate::control_intents::{ApprovedFolder,ControlState};
use crate::{capsule_registry::{Clock,Command,Envelope,SCHEMA},authority_journal::Journal,authority_auth::BrokerAuthority,control_intents::{ControlRuntime,ControlCommandSender,ControlActorClient,ControlAction,ActorControlAction,ActorCompletion},operator_protocol::{decode_operator,OperatorBody,OperatorRequest,MAX_OPERATOR_BYTES},runtime_storage::RuntimeStorage,windows_window_registry::{WindowRegistry,NativeAdapter},capsule_pairing::{PairingBook,PairRequest,Crop},runtime_recording::{RecordingSource,ExternalFfmpeg},runtime_effects::RuntimeEffects,runtime_service_owner::{ServiceClient,ServiceKey}};
struct Projection {pending:Mutex<Option<Vec<u8>>>,ready:Condvar}
impl Projection {
 fn publish(&self,value:Value){let bytes=serde_json::to_vec(&value).expect("owned projection serialization");if bytes.len()>32768{return;}if let Ok(mut pending)=self.pending.lock(){*pending=Some(bytes);self.ready.notify_one();}}
}
fn output()->Arc<Projection>{
 let shared=Arc::new(Projection{pending:Mutex::new(None),ready:Condvar::new()});let writer=shared.clone();
 std::thread::spawn(move||{let mut stdout=std::io::stdout().lock();loop{
  let bytes={let Ok(mut pending)=writer.pending.lock() else{return;};while pending.is_none(){pending=match writer.ready.wait(pending){Ok(value)=>value,Err(_)=>return};}pending.take().unwrap()};
  if stdout.write_all(&bytes).and_then(|_|stdout.write_all(b"\n")).and_then(|_|stdout.flush()).is_err(){return;}
 }});shared
}
fn input()->std::sync::mpsc::Receiver<Vec<u8>>{
 let (sender,receiver)=std::sync::mpsc::sync_channel(8);
 std::thread::spawn(move||{let mut stdin=std::io::stdin().lock();let mut line=Vec::new();let mut byte=[0u8;1];loop{
  match stdin.read(&mut byte){Ok(0)|Err(_)=>break,Ok(_)=>{line.push(byte[0]);if line.len()>MAX_OPERATOR_BYTES{let _=sender.send(line);return;}if byte[0]==b'\n'{if sender.send(std::mem::take(&mut line)).is_err(){return;}}}}
 }});receiver
}
fn result(state:&str,code:&str)->Value{json!({"state":state,"code":code})}
fn window_page(windows:&WindowRegistry,window_offset:u32)->(Vec<crate::windows_window_registry::WindowView>,usize,Option<usize>){
 let observed=windows.snapshots();let total=observed.len();let offset=window_offset as usize;let next=offset.saturating_add(3);
 (observed.into_iter().skip(offset).take(3).collect(),total,if next<total{Some(next)}else{None})
}
#[derive(Clone,Copy)]
enum PlanEffectKind {Record,Stop,Obs}
struct PlanEffect {plan_id:String,index:usize,capsule_id:String,kind:PlanEffectKind,window:Option<crate::windows_window_registry::WindowView>}
struct RuntimeOrganizer {
 plans:Organizer,
 ask_before_ai_actions:bool,
 ai_activity:Value,
 root:Option<(String,ApprovedFolder)>,
 effects:BTreeMap<String,PlanEffect>,
 failures:BTreeMap<String,&'static str>,
 reservations:BTreeMap<(crate::windows_window_registry::WindowHandle,u64),String>,
}
impl RuntimeOrganizer {
 fn new(epoch:Clock)->Self{Self{plans:Organizer::new(epoch),ask_before_ai_actions:false,ai_activity:Value::Null,root:None,effects:BTreeMap::new(),failures:BTreeMap::new(),reservations:BTreeMap::new()}}
 fn expire(&mut self,now:Instant)->bool{if self.plans.expire(now){self.root=None;true}else{false}}
 fn result(&mut self,plan:&str,index:usize,capsule:Option<String>,code:&str){
  let safe=match code {
   "recording"|"stopped"|"sent"|"obs_not_ready"|"window_changed"|"capsule_limit"|"already_recording"|"encoder_session_limit"|"capture_start_unavailable"|"window_unavailable"|"window_titles_identical"|"folder_not_authorized"|"disk_write_failed"|"encoder_unavailable"=>code,
   _=>"unavailable",
  };
  let _=self.plans.set_window_result(plan,index,capsule,safe);
 }
}

// Broker requests carry data only. Local confirmation/rejection are absent
// from this enum and cannot be reached through a broker-controlled op field.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct OrganizerRequest {schema:String,kind:String,request_id:String,authority_epoch:Clock,body:OrganizerRequestBody}
#[derive(Deserialize)]
#[serde(tag="op",rename_all="snake_case",deny_unknown_fields)]
enum OrganizerRequestBody {
 OrganizerStatus{},ListDiscordWindows{},
 ProposeRecordingPlan{folder_name:String,tokens:Option<Vec<String>>},
 PlanResult{plan_id:String},ProposeStopAll{capsule_ids:Option<Vec<String>>},
 ProposeSendToObs{capsule_ids:Option<Vec<String>>},
 ProposeObsScene{capsule_id:String},
}
fn opaque_id(value:&str)->bool{!value.is_empty()&&value.len()<=96&&value.as_bytes()[0].is_ascii_alphanumeric()&&value.bytes().all(|b|b.is_ascii_alphanumeric()||b"_.:-".contains(&b))}
fn now_unix_ms()->Result<u64,&'static str>{std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).ok().and_then(|value|u64::try_from(value.as_millis()).ok()).ok_or("unavailable")}
fn organizer_error(code:&str)->&'static str{match code{
 "folder_root_required"=>"folder_root_required","folder_name_invalid"=>"folder_name_invalid","no_discord_windows"=>"no_discord_windows",
 "window_titles_identical"=>"window_titles_identical","window_unavailable"=>"window_unavailable","obs_not_ready"=>"obs_not_ready","plan_pending"=>"plan_pending","plan_unknown"=>"plan_unknown",_=>"unavailable",
}}
/// Uses the unchanged three-window pages internally. Oversized discovery
/// returns unavailable, never a silently truncated list described as complete.
fn organizer_windows(windows:&WindowRegistry)->Result<Vec<crate::windows_window_registry::WindowView>,&'static str>{
 let mut rows=Vec::new();let mut offset=0;
 loop{let (page,total,next)=window_page(windows,offset);if total>256{return Err("unavailable");}rows.extend(page);match next{Some(next)=>offset=u32::try_from(next).map_err(|_|"unavailable")?,None=>break}}
 Ok(rows)
}
fn follow_resizes(book:&mut PairingBook,windows:&WindowRegistry,bindings:&Arc<std::sync::RwLock<Vec<crate::capsule_pairing::Binding>>>,engine:&RuntimeEffects<RecordingSource>){
 let busy=engine.resize_busy_capsules();
 if book.follow_window_resize(windows,&busy){if let Ok(mut current)=bindings.write(){*current=book.bindings();}}
}
fn current_bindings(runtime:&ControlRuntime<ControlCommandSender>,book:&PairingBook)->Vec<crate::capsule_pairing::Binding>{
 let state=runtime.capsule_snapshot();book.bindings().into_iter().filter(|binding|state.capsules.get(&binding.capsule_id)
  .is_some_and(|capsule|capsule.person_label==binding.person_label&&capsule.feeds.get(&binding.feed_id).is_some_and(|feed|feed.generation==binding.feed_generation&&feed.role==binding.role))).collect()
}
fn organizer_obs_capsules(runtime:&ControlRuntime<ControlCommandSender>,windows:&WindowRegistry,book:&PairingBook,capsule_ids:Option<Vec<String>>)->Result<Vec<(String,String)>,&'static str>{
 let state=runtime.capsule_snapshot();let bindings=current_bindings(runtime,book);
 let ids=match capsule_ids{
  Some(ids)=>{
   if ids.is_empty()||ids.len()>MAX_CAPSULES||ids.iter().any(|id|!opaque_id(id))||ids.iter().collect::<std::collections::BTreeSet<_>>().len()!=ids.len(){return Err("unavailable");}
   ids
  },
  None=>state.capsules.keys().filter(|id|bindings.iter().any(|b|&b.capsule_id==*id)).cloned().collect::<Vec<_>>(),
 };
 if ids.is_empty(){return Err("no_discord_windows");}
 let observed=organizer_windows(windows)?;let mut selected=Vec::new();
 for capsule_id in ids {
  let Some(capsule)=state.capsules.get(&capsule_id)else{return Err("unavailable");};
  let mut capsule_bindings=bindings.iter().filter(|b|b.capsule_id==capsule_id).collect::<Vec<_>>();
  if capsule.feeds.is_empty()||capsule_bindings.len()!=capsule.feeds.len(){return Err("window_unavailable");}
  capsule_bindings.sort_by_key(|b|(b.role,&b.feed_id));
  let mut titles=Vec::new();let mut roles=std::collections::BTreeSet::new();
  for binding in capsule_bindings {
   if !roles.insert(binding.role){return Err("window_unavailable");}
   let view=windows.resolve(&binding.window,binding.window_generation).map_err(|err|match err{crate::windows_window_registry::WindowError::AmbiguousDescriptor=>"window_titles_identical",_=>"window_unavailable"})?;
   if view.layout_revision!=binding.layout_revision{return Err("window_unavailable");}
   if observed.iter().filter(|v|v.visible&&!v.minimized&&v.title==view.title&&v.class==view.class&&v.executable==view.executable).count()!=1{return Err("window_titles_identical");}
   titles.push(view.title.clone());
  }
  if titles.is_empty(){return Err("window_unavailable");}
  selected.push((capsule_id,titles.join(" · ")));
 }
 Ok(selected)
}
fn propose_obs_scene_plan(runtime:&ControlRuntime<ControlCommandSender>,organizer:&mut RuntimeOrganizer,windows:&WindowRegistry,book:&PairingBook,capsule_id:String,obs_ready:bool,now:Instant,unix_ms:u64)->Result<Value,&'static str>{
 if !organizer.plans.projection().is_null(){return Err("plan_pending");}
 if !obs_ready{return Err("obs_not_ready");}
 let capsules=organizer_obs_capsules(runtime,windows,book,Some(vec![capsule_id]))?;
 let id=organizer.plans.propose_send_to_obs(capsules,obs_ready,now,unix_ms)?;organizer.root=None;Ok(json!({"plan_id":id,"state":"pending_confirmation"}))
}
fn organizer_request(value:&Value,runtime:&mut ControlRuntime<ControlCommandSender>,organizer:&mut RuntimeOrganizer,windows:&mut WindowRegistry,adapter:&mut NativeAdapter,book:&PairingBook,ffmpeg:Option<&ExternalFfmpeg>,engine:&RuntimeEffects<RecordingSource>,obs_ready:bool,live:impl Fn()->bool)->Value{
 let invalid=||json!({"request_id":"invalid","error":"unavailable","data":null});
 if serde_json::to_vec(value).map_or(true,|bytes|bytes.len()>MAX_OPERATOR_BYTES){return invalid();}
 let Ok(request)=serde_json::from_value::<OrganizerRequest>(value.clone())else{return invalid();};
 if request.schema!="cafe.organizer.v1"||request.kind!="organizer_request"||!opaque_id(&request.request_id)||request.authority_epoch!=runtime.capsule_snapshot().authority_epoch||!live(){return invalid();}
 let now=Instant::now();organizer.expire(now);
 let data=(||->Result<Value,&'static str>{match request.body{
  OrganizerRequestBody::OrganizerStatus{}=>Ok(json!({"organizer_plan":organizer.plans.projection(),"folder_root_set":runtime.organizer_root().is_some(),"encoder_ready":ffmpeg.is_some_and(ExternalFfmpeg::encoder_ready),"obs_ready":obs_ready,"capsule_count":runtime.capsule_snapshot().capsules.len(),"recording_count":engine.status().iter().filter(|row|row.state==crate::runtime_effects::CapsuleState::Running).count()})),
  OrganizerRequestBody::ListDiscordWindows{}=>{
   adapter.enumerate(windows).map_err(|_|"unavailable")?;let bindings=current_bindings(runtime,book);
   let rows=organizer_windows(windows)?.into_iter().filter_map(|view|{
    let class=classify_discord_window(&view);if class==DiscordWindowClass::NotDiscord{return None;}
    let bound=bindings.iter().find(|binding|binding.window==view.handle&&binding.window_generation==view.generation).map(|binding|binding.capsule_id.clone());
    Some(json!({"token":window_token(&view),"title":view.title,"classification":class,"bound_capsule":bound}))
   }).collect::<Vec<_>>();Ok(json!({"windows":rows}))
  },
  OrganizerRequestBody::ProposeRecordingPlan{folder_name,tokens}=>{
   // A pending proposal wins over any refreshed discovery or root failure.
   if !organizer.plans.projection().is_null(){return Err("plan_pending");}
   let root=runtime.organizer_root().ok_or("folder_root_required")?;root.revalidate().map_err(|_|"folder_root_required")?;
   if !crate::organizer::folder_name_valid(&folder_name){return Err("folder_name_invalid");}
   adapter.enumerate(windows).map_err(|_|"unavailable")?;
   if !live(){return Err("unavailable");}
   let id=organizer.plans.propose_recording(folder_name,tokens,&organizer_windows(windows)?,true,now,now_unix_ms()?)?;
   organizer.root=Some((id.clone(),root));Ok(json!({"plan_id":id,"state":"pending_confirmation"}))
  },
  OrganizerRequestBody::PlanResult{plan_id}=>{if !opaque_id(&plan_id){return Err("plan_unknown");}organizer.plans.plan_result(&plan_id)},
  OrganizerRequestBody::ProposeStopAll{capsule_ids}=>{
   if !organizer.plans.projection().is_null(){return Err("plan_pending");}
   let live_capsules=engine.status().into_iter().map(|row|row.capsule_id).collect::<Vec<_>>();
   let ids=capsule_ids.unwrap_or_else(||live_capsules.clone());if ids.iter().any(|id|!live_capsules.contains(id)){return Err("unavailable");}
   if ids.is_empty()&&runtime.has_unsettled_recording_start(){return Err("unavailable");}
   if !live(){return Err("unavailable");}let id=organizer.plans.propose_stop_all(ids,now,now_unix_ms()?)?;organizer.root=None;Ok(json!({"plan_id":id,"state":"pending_confirmation"}))
  },
  OrganizerRequestBody::ProposeSendToObs{capsule_ids}=>{
   if !organizer.plans.projection().is_null(){return Err("plan_pending");}
   if !obs_ready{return Err("obs_not_ready");}
   adapter.enumerate(windows).map_err(|_|"unavailable")?;
   if !live(){return Err("unavailable");}
   let capsules=organizer_obs_capsules(runtime,windows,book,capsule_ids)?;
   let id=organizer.plans.propose_send_to_obs(capsules,obs_ready,now,now_unix_ms()?)?;organizer.root=None;Ok(json!({"plan_id":id,"state":"pending_confirmation"}))
  },
  OrganizerRequestBody::ProposeObsScene{capsule_id}=>{
   if !organizer.plans.projection().is_null(){return Err("plan_pending");}
   if !obs_ready{return Err("obs_not_ready");}
   adapter.enumerate(windows).map_err(|_|"unavailable")?;
   if !live(){return Err("unavailable");}
   propose_obs_scene_plan(runtime,organizer,windows,book,capsule_id,obs_ready,now,now_unix_ms()?)
  },
 }})();
 let response=match data{Ok(data)=>json!({"request_id":request.request_id,"error":null,"data":data}),Err(code)=>json!({"request_id":request.request_id,"error":organizer_error(code),"data":null})};
 if serde_json::to_vec(&response).map_or(true,|bytes|bytes.len()>16_000){json!({"request_id":request.request_id,"error":"unavailable","data":null})}else{response}
}

/// Dispatch only a successfully validated proposal. The local preference is
/// authority-owned and absent from every broker request schema.
fn dispatch_organizer_proposal(response:&mut Value,operation:&str,runtime:&mut ControlRuntime<ControlCommandSender>,organizer:&mut RuntimeOrganizer,windows:&mut WindowRegistry,adapter:&mut NativeAdapter,book:&mut PairingBook,bindings:&Arc<std::sync::RwLock<Vec<crate::capsule_pairing::Binding>>>,engine:&RuntimeEffects<RecordingSource>,ffmpeg:Option<&ExternalFfmpeg>,obs_ready:bool,live:&dyn Fn()->bool){
 let mut enumerate=|windows:&mut WindowRegistry|adapter.enumerate(windows).is_ok();
 let mut preparation=OrganizerPreparation{enumerate:&mut enumerate,encoder_available:ffmpeg.is_some()&&cfg!(windows)};
 dispatch_organizer_proposal_prepared(response,operation,runtime,organizer,windows,book,bindings,engine,&mut preparation,obs_ready,live);
}
// Internal preparation seam: native callers derive encoder availability from
// their approved capability; tests replace only native discovery/encoder availability.
// No serialized request can create or modify this value.
struct OrganizerPreparation<'a>{enumerate:&'a mut dyn FnMut(&mut WindowRegistry)->bool,encoder_available:bool}
fn dispatch_organizer_proposal_prepared(response:&mut Value,operation:&str,runtime:&mut ControlRuntime<ControlCommandSender>,organizer:&mut RuntimeOrganizer,windows:&mut WindowRegistry,book:&mut PairingBook,bindings:&Arc<std::sync::RwLock<Vec<crate::capsule_pairing::Binding>>>,engine:&RuntimeEffects<RecordingSource>,preparation:&mut OrganizerPreparation<'_>,obs_ready:bool,live:&dyn Fn()->bool){
 if organizer.ask_before_ai_actions||!response["error"].is_null()||response["data"]["state"]!="pending_confirmation"{return;}
 let Some(plan_id)=response["data"]["plan_id"].as_str().map(str::to_owned)else{return;};
 let pending=organizer.plans.projection();
 // The response must name the current plan, never an older completed result.
 if pending["plan_id"]!=plan_id{return;}
 let Some(rows)=pending["windows"].as_array()else{return;};let count=rows.len();
 let code=match operation{"propose_recording_plan"=>"ai_recording_submitted","propose_stop_all"=>"ai_stop_submitted","propose_send_to_obs"=>"ai_obs_submitted","propose_obs_scene"=>"ai_scene_submitted",_=>return};
 let reply=confirm_organizer_prepared(&plan_id,runtime,organizer,windows,book,bindings,engine,preparation,obs_ready,live);
 if reply["code"]!="organizer_plan_confirmed"{if organizer.plans.reject(&plan_id,Instant::now()).is_ok(){organizer.root=None;}response["error"]=json!("unavailable");response["data"]=Value::Null;return;}
 // Effects remain asynchronous; executing never claims a successful recording.
 response["data"]["state"]=json!(if organizer.plans.plan_result(&plan_id).is_ok_and(|row|row["state"]=="completed"){"completed"}else{"executing"});
 organizer.ai_activity=json!({"code":code,"count":count});
}

/// Ordinary structural intents retain the organizer actor in their closed
/// request/event namespace. Confirmed controls use SHA256("organizer") in the
/// existing journal identity field; no new journal schema is invented here.
fn organizer_structure(runtime:&mut ControlRuntime<ControlCommandSender>,id:&str,body:Command)->Result<(),&'static str>{
 organizer_structure_live(runtime,id,body,&||true)
}
fn organizer_structure_live(runtime:&mut ControlRuntime<ControlCommandSender>,id:&str,body:Command,live:&dyn Fn()->bool)->Result<(),&'static str>{
 let id=id.replace('.',"-"); // Structural envelopes allow only alphanumerics, '-' and '_'.
 let state=runtime.capsule_snapshot();let envelope=Envelope{schema:SCHEMA.into(),kind:"intent".into(),request_id:id.clone(),authority_epoch:state.authority_epoch,expected_revision:state.revision,body};
 if !live(){return Err("unavailable");}
 runtime.journal_for_media_actor().append_control(&id,envelope).map(|_|()).map_err(|_|"disk_write_failed")
}
fn pair_observed_live(runtime:&mut ControlRuntime<ControlCommandSender>,windows:&WindowRegistry,book:&mut PairingBook,bindings:&Arc<std::sync::RwLock<Vec<crate::capsule_pairing::Binding>>>,view:&crate::windows_window_registry::WindowView,capsule_id:&str,id:&str,live:&dyn Fn()->bool)->Result<(),&'static str>{
 let id=id.replace('.',"-");
 let state=runtime.capsule_snapshot();let capsule=state.capsules.get(capsule_id).ok_or("unavailable")?;
 let next=state.highest_generation.0.checked_add(1).ok_or("unavailable")?;
 let request=PairRequest{request_id:id.clone(),authority_epoch:state.authority_epoch,expected_revision:state.revision,capsule_id:capsule_id.into(),person_label:capsule.person_label.clone(),feed_id:format!("organizer-feed-{next}"),role:crate::capsule_registry::Role::Game,window:view.handle.clone(),window_generation:view.generation,layout_revision:view.layout_revision,crop:Crop{x:0,y:0,width:view.rect.width,height:view.rect.height}};
 let proposal=book.propose(&state,windows,request).map_err(|error|match error{crate::capsule_pairing::PairError::Window(crate::windows_window_registry::WindowError::AmbiguousDescriptor)=>"window_titles_identical",_=>"window_unavailable"})?;
 if !live(){return Err("unavailable");}
 runtime.journal_for_media_actor().append_control(&id,proposal.envelope().clone()).map_err(|_|"disk_write_failed")?;
 book.commit_proposed(proposal,&runtime.capsule_snapshot(),windows).map_err(|_|"window_unavailable")?;
 *bindings.write().map_err(|_|"unavailable")?=book.bindings();Ok(())
}
/// Reuses only an exact, one-game-feed capsule. An organizer plan must never
/// start unrelated feeds merely because they share a manually paired capsule.
fn anchor_observed(runtime:&mut ControlRuntime<ControlCommandSender>,windows:&WindowRegistry,book:&mut PairingBook,bindings:&Arc<std::sync::RwLock<Vec<crate::capsule_pairing::Binding>>>,view:&crate::windows_window_registry::WindowView,id:&str,busy:&[String],reservations:&mut BTreeMap<(crate::windows_window_registry::WindowHandle,u64),String>)->Result<String,&'static str>{
 anchor_classified(runtime,windows,book,bindings,view,id,busy,reservations,classify_discord_window(view))
}
fn anchor_classified(runtime:&mut ControlRuntime<ControlCommandSender>,windows:&WindowRegistry,book:&mut PairingBook,bindings:&Arc<std::sync::RwLock<Vec<crate::capsule_pairing::Binding>>>,view:&crate::windows_window_registry::WindowView,id:&str,busy:&[String],reservations:&mut BTreeMap<(crate::windows_window_registry::WindowHandle,u64),String>,classification:DiscordWindowClass)->Result<String,&'static str>{
 anchor_classified_live(runtime,windows,book,bindings,view,id,busy,reservations,classification,&||true)
}
fn anchor_classified_live(runtime:&mut ControlRuntime<ControlCommandSender>,windows:&WindowRegistry,book:&mut PairingBook,bindings:&Arc<std::sync::RwLock<Vec<crate::capsule_pairing::Binding>>>,view:&crate::windows_window_registry::WindowView,id:&str,busy:&[String],reservations:&mut BTreeMap<(crate::windows_window_registry::WindowHandle,u64),String>,classification:DiscordWindowClass,live:&dyn Fn()->bool)->Result<String,&'static str>{
 if classification!=DiscordWindowClass::Popout{return Err("window_unavailable");}
 let observed=resolve_frozen_window(windows,view)?;
 let state=runtime.capsule_snapshot();
 if let Some(binding)=current_bindings(runtime,book).into_iter().find(|binding|binding.window==view.handle&&binding.window_generation==view.generation){
  let capsule=state.capsules.get(&binding.capsule_id).ok_or("unavailable")?;
  if capsule.feeds.len()!=1||binding.role!=crate::capsule_registry::Role::Game{return Err("unavailable");}
  if busy.contains(&binding.capsule_id){return Err("already_recording");}
  reservations.insert(crate::organizer::identity(view),binding.capsule_id.clone());
  let full=Crop{x:0,y:0,width:view.rect.width,height:view.rect.height};
  if binding.layout_revision!=view.layout_revision||binding.crop!=full{
   organizer_structure_live(runtime,&format!("{id}.remove"),Command::RemoveFeed{capsule_id:binding.capsule_id.clone(),feed_id:binding.feed_id,generation:binding.feed_generation},live)?;
   pair_observed_live(runtime,windows,book,bindings,&observed,&binding.capsule_id,&format!("{id}.pair"),live)?;
  }
  return Ok(binding.capsule_id);
 }
 if let Some(capsule_id)=reservations.get(&crate::organizer::identity(view)).cloned(){
  if busy.contains(&capsule_id){return Err("already_recording");}
  if state.capsules.get(&capsule_id).is_none_or(|capsule|!capsule.feeds.is_empty()){return Err("unavailable");}
  pair_observed_live(runtime,windows,book,bindings,&observed,&capsule_id,&format!("{id}.pair"),live)?;return Ok(capsule_id);
 }
 if state.capsules.len()>=MAX_CAPSULES{return Err("capsule_limit");}
 let slot=(1..=MAX_CAPSULES).find(|slot|!state.capsules.contains_key(&format!("organizer-{}-{slot}",state.authority_epoch.0))).ok_or("capsule_limit")?;
 let capsule_id=format!("organizer-{}-{slot}",state.authority_epoch.0);
 let generation=Clock(state.highest_generation.0.checked_add(1).ok_or("unavailable")?);
 organizer_structure_live(runtime,&format!("{id}.create"),Command::CreateCapsule{capsule_id:capsule_id.clone(),person_label:format!("slot-{slot}"),generation},live)?;
 reservations.insert(crate::organizer::identity(view),capsule_id.clone());
 pair_observed_live(runtime,windows,book,bindings,&observed,&capsule_id,&format!("{id}.pair"),live)?;Ok(capsule_id)
}
fn track_plan_control(organizer:&mut RuntimeOrganizer,runtime:&mut ControlRuntime<ControlCommandSender>,plan:&str,index:usize,capsule_id:String,kind:PlanEffectKind,window:Option<crate::windows_window_registry::WindowView>,live:&dyn Fn()->bool){
 let (suffix,action)=match kind{
  PlanEffectKind::Record=>("start",ControlAction::StartRecording(capsule_id.clone())),
  PlanEffectKind::Stop=>("stop",ControlAction::StopRecording(capsule_id.clone())),
  PlanEffectKind::Obs=>("obs",ControlAction::SendToObs(capsule_id.clone())),
 };
 let id=format!("organizer.{plan}.{index}.{suffix}");
 let reply=match kind{
  PlanEffectKind::Record|PlanEffectKind::Stop=>runtime.handle_organizer_live(&id,runtime.capsule_snapshot().authority_epoch,action,live),
  PlanEffectKind::Obs=>runtime.handle_operator_live(&id,runtime.capsule_snapshot().authority_epoch,runtime.revision(),action,live),
 };
 if reply.state==ControlState::Pending {
  organizer.effects.insert(format!("ctl.{}",crate::authority_journal::sha256_hex(id.as_bytes())),PlanEffect{plan_id:plan.into(),index,capsule_id,kind,window});
 }else{
  let code=match kind{PlanEffectKind::Obs=>"window_unavailable",_=>"unavailable"};
  organizer.result(plan,index,Some(capsule_id.clone()),code);
  if !matches!(kind,PlanEffectKind::Obs){organizer.failures.insert(capsule_id,"recording_interrupted");}
 }
}
fn complete_organizer_effect(organizer:&mut RuntimeOrganizer,effect_id:&str,committed:bool,failure:Option<&str>){
 if let Some(effect)=organizer.effects.remove(effect_id){
  let state=if committed{match effect.kind{PlanEffectKind::Stop=>"stopped",PlanEffectKind::Obs=>"sent",PlanEffectKind::Record=>"recording"}}else{failure.unwrap_or("unavailable")};
  organizer.result(&effect.plan_id,effect.index,Some(effect.capsule_id),state);
 }
}
fn confirm_organizer(plan_id:&str,runtime:&mut ControlRuntime<ControlCommandSender>,organizer:&mut RuntimeOrganizer,windows:&mut WindowRegistry,adapter:&mut NativeAdapter,book:&mut PairingBook,bindings:&Arc<std::sync::RwLock<Vec<crate::capsule_pairing::Binding>>>,engine:&RuntimeEffects<RecordingSource>,ffmpeg:Option<&ExternalFfmpeg>,obs_ready:bool)->Value{
 let mut enumerate=|windows:&mut WindowRegistry|adapter.enumerate(windows).is_ok();
 let mut preparation=OrganizerPreparation{enumerate:&mut enumerate,encoder_available:ffmpeg.is_some()&&cfg!(windows)};
 confirm_organizer_prepared(plan_id,runtime,organizer,windows,book,bindings,engine,&mut preparation,obs_ready,&||true)
}
fn confirm_organizer_prepared(plan_id:&str,runtime:&mut ControlRuntime<ControlCommandSender>,organizer:&mut RuntimeOrganizer,windows:&mut WindowRegistry,book:&mut PairingBook,bindings:&Arc<std::sync::RwLock<Vec<crate::capsule_pairing::Binding>>>,engine:&RuntimeEffects<RecordingSource>,preparation:&mut OrganizerPreparation<'_>,obs_ready:bool,live:&dyn Fn()->bool)->Value{
 confirm_organizer_status_prepared(plan_id,runtime,organizer,windows,book,bindings,&engine.status(),preparation,obs_ready,live)
}
// The actor owns the engine; this bounded snapshot is taken synchronously by
// the wrapper, with no yield before the no-op premise is checked.
fn confirm_organizer_status_prepared(plan_id:&str,runtime:&mut ControlRuntime<ControlCommandSender>,organizer:&mut RuntimeOrganizer,windows:&mut WindowRegistry,book:&mut PairingBook,bindings:&Arc<std::sync::RwLock<Vec<crate::capsule_pairing::Binding>>>,engine_status:&[crate::runtime_effects::CapsuleStatus],preparation:&mut OrganizerPreparation<'_>,obs_ready:bool,live:&dyn Fn()->bool)->Value{
 if !live(){return result("rejected","unavailable");}
 let now=Instant::now();
 if organizer.plans.pending_empty_stop(plan_id,now)&&(!engine_status.is_empty()||runtime.has_unsettled_recording_start()){return result("rejected","unavailable");}
 let plan=match organizer.plans.take_confirmed(plan_id,now){Ok(plan)=>plan,Err(code)=>return result("rejected",code)};
 let root=organizer.root.take().filter(|(id,_)|*id==plan.plan_id).map(|(_,root)|root);
 let epoch_ok=plan.authority_epoch==runtime.capsule_snapshot().authority_epoch;
 match plan.kind{
  PlanKind::Recording{windows:chosen,folder_name}=>{
   let enumeration=epoch_ok&&(preparation.enumerate)(windows);
   let validated=chosen.iter().map(|frozen|{if !enumeration{return Err("window_unavailable");}let view=resolve_frozen_window(windows,frozen)?;if classify_discord_window(&view)!=DiscordWindowClass::Popout{return Err("window_changed");}Ok(view)}).collect::<Vec<_>>();
   let prepared_folder=if !live(){Err("unavailable")}else if !validated.iter().any(Result::is_ok){Err("window_unavailable")}else if !preparation.encoder_available{Err("encoder_unavailable")}else{root.as_ref().ok_or("folder_not_authorized").and_then(|root|runtime.select_organizer_subfolder_live(&format!("organizer.{plan_id}.folder"),root,&folder_name,live))};
   let mut busy=engine_status.iter().filter(|row|row.state==crate::runtime_effects::CapsuleState::Running).map(|row|row.capsule_id.clone()).collect::<Vec<_>>();busy.extend(organizer.effects.values().map(|effect|effect.capsule_id.clone()));
   for (index,view) in validated.into_iter().enumerate(){
    let view=match view{Ok(view)=>view,Err(code)=>{organizer.result(plan_id,index,None,code);continue;}};
    if !live(){organizer.result(plan_id,index,None,"unavailable");continue;}
    if let Err(code)=prepared_folder{organizer.result(plan_id,index,None,code);continue;}
    match anchor_classified_live(runtime,windows,book,bindings,&view,&format!("organizer.{plan_id}.{index}"),&busy,&mut organizer.reservations,classify_discord_window(&view),live){
     Ok(capsule_id)=>{busy.push(capsule_id.clone());track_plan_control(organizer,runtime,plan_id,index,capsule_id,PlanEffectKind::Record,Some(view),live);},
     Err(code)=>organizer.result(plan_id,index,None,code),
    }
   }
  },
  PlanKind::StopAll{capsule_ids}=>{
   for (index,capsule_id) in capsule_ids.into_iter().enumerate(){if !epoch_ok||!live(){organizer.result(plan_id,index,Some(capsule_id),"unavailable");}else{track_plan_control(organizer,runtime,plan_id,index,capsule_id,PlanEffectKind::Stop,None,live);}}
  },
  PlanKind::SendToObs{capsules}=>{
   for (index,(capsule_id,_)) in capsules.into_iter().enumerate(){
    if !epoch_ok||!live(){organizer.result(plan_id,index,Some(capsule_id),"unavailable");}
    else if !obs_ready{organizer.result(plan_id,index,Some(capsule_id),"obs_not_ready");}
    else{track_plan_control(organizer,runtime,plan_id,index,capsule_id,PlanEffectKind::Obs,None,live);}
   }
  },
 }
 if !live(){result("rejected","unavailable")}else{result("pending","organizer_plan_confirmed")}
}
fn auto_anchor(runtime:&mut ControlRuntime<ControlCommandSender>,organizer:&mut RuntimeOrganizer,windows:&mut WindowRegistry,adapter:&mut NativeAdapter,book:&mut PairingBook,bindings:&Arc<std::sync::RwLock<Vec<crate::capsule_pairing::Binding>>>,engine:&RuntimeEffects<RecordingSource>)->bool{
 if !organizer.plans.poll_due(Instant::now()){return false;}
 if adapter.enumerate(windows).is_err(){return false;}
 let mut busy=engine.status().into_iter().filter(|row|row.state==crate::runtime_effects::CapsuleState::Running).map(|row|row.capsule_id).collect::<Vec<_>>();busy.extend(organizer.effects.values().map(|effect|effect.capsule_id.clone()));
 if let Ok(rows)=organizer_windows(windows){for (index,view) in rows.into_iter().filter(|view|classify_discord_window(view)==DiscordWindowClass::Popout).enumerate(){
  let id=format!("organizer.auto.{}.{}.{index}",runtime.capsule_snapshot().authority_epoch.0,runtime.revision().0);
  let _=anchor_observed(runtime,windows,book,bindings,&view,&id,&busy,&mut organizer.reservations);
 }}
 // Enumeration also changes feed availability/preview eligibility after a
 // close; recordings and their durable files are never removed here.
 true
}

async fn with_recording_ticks<F:std::future::Future>(future:F,engine:&mut RuntimeEffects<RecordingSource>,windows:&WindowRegistry)->F::Output{
 let mut future=Box::pin(future);
 loop{match tokio::time::timeout(Duration::from_millis(5),&mut future).await{Ok(value)=>return value,Err(_)=>{let _=engine.tick_all(windows,Instant::now()).await;}}}
}

fn snapshot(runtime:&ControlRuntime<ControlCommandSender>,windows:&WindowRegistry,last:&Value,configured:bool,engine:&RuntimeEffects<RecordingSource>,window_offset:u32,services:&ServiceClient,book:&PairingBook,organizer:&RuntimeOrganizer,adapter:&mut NativeAdapter)->Value{
 let (page,total,next)=window_page(windows,window_offset);
 let mut state=json!({"runtime":"authority_ready","platform":if cfg!(windows){"windows"}else{"unsupported"},"recording_available":false,"recording_configured":configured,"recordings":engine.status().iter().filter(|capsule|runtime.capsule_snapshot().capsules.contains_key(&capsule.capsule_id)).map(|capsule|json!({"capsule_id":capsule.capsule_id,"state":format!("{:?}",capsule.state),"feeds":capsule.feeds.iter().map(|feed|json!({"feed_id":feed.feed_id,"state":format!("{:?}",feed.state),"written_frames":feed.counters.written_frames.to_string(),"backpressure":feed.counters.backpressure.to_string()})).collect::<Vec<_>>()})).collect::<Vec<_>>(),"obs_available":services.obs_ready(),"obs_configurable":services.configurable(),"chat_available":services.chat_ready(),"chat_configurable":services.configurable()&&!services.chat_running(),"services":services.projection(),"mcp_control_available":false,"capsules":runtime.capsule_snapshot().capsules,"windows":page,"window_page":{"offset":window_offset,"next_offset":next,"total":total},"folder_configured":runtime.folder_configured(),"last_result":last});
 let bindings=current_bindings(runtime,book);
 let preview=bindings.iter().filter_map(|binding|windows.live_preview_source_id(&binding.window,binding.window_generation,binding.layout_revision,adapter).map(|source_id|{let top_trim=windows.resolve(&binding.window,binding.window_generation).map(|view|{let crop=crate::runtime_recording::obs_crop(binding.crop,(view.rect.width,view.rect.height),crate::runtime_recording::window_top_trim(&view,windows,binding));if crop.y!=binding.crop.y{crop.y.min(2160)}else{0}}).unwrap_or(0);json!({"capsule_id":binding.capsule_id,"feed_id":binding.feed_id,"source_id":source_id,"top_trim":top_trim})})).collect::<Vec<_>>();
 state["preview_sources"]=json!(&preview);
 state["preview_eligibility"]=json!(bindings.iter().filter(|binding|preview.iter().any(|row|row["capsule_id"]==binding.capsule_id&&row["feed_id"]==binding.feed_id)).map(|binding|json!({"capsule_id":binding.capsule_id,"feed_id":binding.feed_id,"layout_revision":binding.layout_revision})).collect::<Vec<_>>());
 state["organizer_plan"]=organizer.plans.projection();state["auto_anchor"]=json!(organizer.plans.auto_anchor());state["ask_before_ai_actions"]=json!(organizer.ask_before_ai_actions);state["ai_activity"]=organizer.ai_activity.clone();
 state["feed_availability"]=json!(bindings.iter().map(|binding|{let available=preview.iter().any(|row|row["capsule_id"]==binding.capsule_id&&row["feed_id"]==binding.feed_id);json!({"capsule_id":binding.capsule_id,"feed_id":binding.feed_id,"available":available})}).collect::<Vec<_>>());
 if let Some(rows)=state["recordings"].as_array_mut(){
  for (row,observed) in rows.iter_mut().zip(engine.status()){row["reason"]=json!(observed.reason);}
  for (capsule_id,reason) in &organizer.failures{if !rows.iter().any(|row|row["capsule_id"]==*capsule_id){rows.push(json!({"capsule_id":capsule_id,"state":"Interrupted","reason":reason,"feeds":[]}));}}
 }
 // Never truncate identifiers or descriptors. Oversized projections fail visibly;
 // durable recording state is not reset or represented as a successful empty set.
 if serde_json::to_vec(&state).map_or(true,|bytes|bytes.len()>30000){state=json!({"runtime":"projection_unavailable","code":"projection_budget_exceeded"});}
 json!({"kind":"snapshot","authority_epoch":runtime.capsule_snapshot().authority_epoch,"revision":runtime.revision(),"state":state})
}
// Static diagnostic categories only; admission and journal semantics are unchanged.
fn pair_rejection_code(error:&crate::capsule_pairing::PairError)->&'static str {
 use crate::capsule_pairing::PairError;
 use crate::windows_window_registry::WindowError;
 match error {
  PairError::Window(WindowError::Unavailable)=>"pair_window_unavailable",
  PairError::Window(WindowError::AmbiguousDescriptor)=>"window_titles_identical",
  PairError::Window(WindowError::StaleWindow)=>"pair_window_stale",
  PairError::Window(WindowError::Unsupported)=>"pair_platform_unsupported",
  PairError::Window(WindowError::IdentityExhausted)=>"pair_identity_exhausted",
  PairError::Window(WindowError::NativeFailure)=>"pair_native_failure",
  PairError::Window(WindowError::InvalidDescriptor|WindowError::DuplicateObservation)=>"pair_window_invalid",
  PairError::WrongPerson=>"pair_person_mismatch",
  PairError::InvalidCrop=>"pair_crop_invalid",
  PairError::StaleCrop=>"pair_crop_stale",
  PairError::UnknownCapsule=>"pair_capsule_unknown",
  PairError::AlreadyPaired=>"pair_role_already_paired",
  PairError::FeedUnavailable=>"pair_feed_unavailable",
  PairError::AuthorityRejected(_)=>"pair_authority_rejected",
  PairError::GenerationExhausted=>"pair_generation_exhausted",
  PairError::ExecutableOnlyForbidden=>"pair_executable_only_forbidden",
 }
}
fn mutate(request:OperatorRequest,runtime:&mut ControlRuntime<ControlCommandSender>,windows:&mut WindowRegistry,adapter:&mut NativeAdapter,book:&mut PairingBook,bindings:&Arc<std::sync::RwLock<Vec<crate::capsule_pairing::Binding>>>,ffmpeg:&mut Option<ExternalFfmpeg>,window_offset:&mut u32,services:&mut ServiceClient,organizer:&mut RuntimeOrganizer,engine:&RuntimeEffects<RecordingSource>)->Value{
 mutate_prepared(request,runtime,windows,adapter,book,bindings,ffmpeg,window_offset,services,organizer,engine,None)
}
// Production calls this only with None. Tests substitute native preparation
// after the same local epoch/revision/request admission, never serialized authority.
fn mutate_prepared(request:OperatorRequest,runtime:&mut ControlRuntime<ControlCommandSender>,windows:&mut WindowRegistry,adapter:&mut NativeAdapter,book:&mut PairingBook,bindings:&Arc<std::sync::RwLock<Vec<crate::capsule_pairing::Binding>>>,ffmpeg:&mut Option<ExternalFfmpeg>,window_offset:&mut u32,services:&mut ServiceClient,organizer:&mut RuntimeOrganizer,engine:&RuntimeEffects<RecordingSource>,preparation:Option<(&mut OrganizerPreparation<'_>,bool)>)->Value{
 let epoch=runtime.capsule_snapshot().authority_epoch;
 if request.authority_epoch!=epoch{return result("rejected","old_authority_epoch");}
 let query=matches!(&request.body,OperatorBody::Snapshot{}|OperatorBody::ListWindows{..});
 let effect=matches!(&request.body,OperatorBody::SetFolder{..}|OperatorBody::StartRecording{..}|OperatorBody::StopRecording{..}|OperatorBody::SendToObs{..});
 if !query&&!effect&&request.expected_revision!=runtime.revision(){return result("rejected","stale_revision");}
 let state=runtime.capsule_snapshot();
 if !query&&(state.seen_requests.contains(&request.request_id)||runtime.known_control_request(&request.request_id)){return result("rejected","request_id_conflict");}
 match request.body{
  OperatorBody::Snapshot{}=>result("observed","snapshot"),
  OperatorBody::OrganizerPlanConfirm{plan_id}=>match preparation{Some((preparation,obs_ready))=>confirm_organizer_prepared(&plan_id,runtime,organizer,windows,book,bindings,engine,preparation,obs_ready,&||true),None=>confirm_organizer(&plan_id,runtime,organizer,windows,adapter,book,bindings,engine,ffmpeg.as_ref(),services.obs_ready())},
  OperatorBody::OrganizerPlanReject{plan_id}=>match organizer.plans.reject(&plan_id,Instant::now()){Ok(())=>{organizer.root=None;result("rejected","organizer_plan_rejected")},Err(code)=>result("rejected",code)},
  OperatorBody::RemoveCapsule{capsule_id}=>{
   if !runtime.capsule_snapshot().capsules.contains_key(&capsule_id){return result("rejected","capsule_unknown");}
   // A closed or failed window can always be removed; a live recording must stop first.
   if engine.status().iter().any(|row|row.capsule_id==capsule_id&&row.state==crate::runtime_effects::CapsuleState::Running){return result("blocked","capsule_recording");}
   if organizer_structure(runtime,&request.request_id,Command::RemoveCapsule{capsule_id:capsule_id.clone()}).is_err(){return result("blocked","disk_write_failed");}
   book.remove_capsule(&capsule_id);
   if let Ok(mut current)=bindings.write(){*current=book.bindings();}
   organizer.failures.remove(&capsule_id);organizer.reservations.retain(|_,reserved|*reserved!=capsule_id);
   result("committed","capsule_removed")
  },
  OperatorBody::SetAskBeforeAiActions{enabled}=>{organizer.ask_before_ai_actions=enabled;result("configured","ai_confirmation_updated")},
  OperatorBody::SetAutoAnchor{enabled}=>{organizer.plans.set_auto_anchor(enabled);result("configured","auto_anchor_updated")},
  OperatorBody::ListWindows{offset}=>match adapter.enumerate(windows){Ok(_)=>{follow_resizes(book,windows,bindings,engine);let requested=offset.unwrap_or(0);if requested as usize>windows.snapshots().len(){*window_offset=0;result("rejected","window_page_invalidated")}else{*window_offset=requested;result("observed","windows_refreshed")}},Err(_)=>result("blocked","native_enumeration_unavailable")},
  OperatorBody::CreateCapsule{capsule_id,person_label}=>{
   if runtime.known_control_request(&request.request_id)||state.capsules.len()>=MAX_CAPSULES{return result("rejected","request_or_capacity_conflict");}
   let Some(next)=state.highest_generation.0.checked_add(1) else{return result("blocked","generation_exhausted");};
   let envelope=Envelope{schema:SCHEMA.into(),kind:"intent".into(),request_id:request.request_id.clone(),authority_epoch:epoch,expected_revision:state.revision,body:Command::CreateCapsule{capsule_id,person_label,generation:Clock(next)}};
   match runtime.journal_for_media_actor().append_control(&request.request_id,envelope){Ok(_)=>result("committed","capsule_created"),Err(_)=>result("rejected","capsule_not_durable")}
  },
  OperatorBody::PairWindow{capsule_id,person_label,feed_id,role,window_token,window_generation,layout_revision,crop}=>{
   if runtime.known_control_request(&request.request_id){return result("rejected","request_id_conflict");}
   let matching=windows.snapshots().into_iter().filter(|view|serde_json::to_value(&view.handle).ok()==Some(json!(window_token))).collect::<Vec<_>>();
   if matching.len()!=1{return result("rejected","window_token_unknown_or_ambiguous");}
   let request_pair=PairRequest{request_id:request.request_id.clone(),authority_epoch:epoch,expected_revision:state.revision,capsule_id,person_label,feed_id,role,window:matching[0].handle.clone(),window_generation,layout_revision,crop:Crop{x:crop.x,y:crop.y,width:crop.width,height:crop.height}};
   let proposal=match book.propose(&state,windows,request_pair){Ok(value)=>value,Err(error)=>return result("rejected",pair_rejection_code(&error))};
   if runtime.journal_for_media_actor().append_control(&request.request_id,proposal.envelope().clone()).is_err(){return result("rejected","pair_not_durable");}
   if book.commit_proposed(proposal,&runtime.capsule_snapshot(),windows).is_err(){return result("blocked","pair_reconfirmation_required");}
   match bindings.write(){Ok(mut shared)=>*shared=book.bindings(),Err(_)=>return result("blocked","binding_state_unavailable")};result("committed","window_paired")
  },
  OperatorBody::SetFolder{folder}=>serde_json::to_value(runtime.handle_operator_folder(&request.request_id,epoch,request.expected_revision,PathBuf::from(folder))).unwrap_or_else(|_|result("blocked","reply_encoding_failed")),
  OperatorBody::StartRecording{capsule_id}=>{if !cfg!(windows)||ffmpeg.is_none(){return result("blocked","recording_runtime_not_configured");}if adapter.enumerate(windows).is_ok(){follow_resizes(book,windows,bindings,engine);}serde_json::to_value(runtime.handle_operator(&request.request_id,epoch,request.expected_revision,ControlAction::StartRecording(capsule_id))).unwrap_or_else(|_|result("blocked","reply_encoding_failed"))},
  OperatorBody::StopRecording{capsule_id}=>serde_json::to_value(runtime.handle_operator(&request.request_id,epoch,request.expected_revision,ControlAction::StopRecording(capsule_id))).unwrap_or_else(|_|result("blocked","reply_encoding_failed")),
  OperatorBody::SendToObs{capsule_id}=>{if !services.obs_ready(){return result("blocked","obs_runtime_not_configured");}if adapter.enumerate(windows).is_ok(){follow_resizes(book,windows,bindings,engine);}serde_json::to_value(runtime.handle_operator(&request.request_id,epoch,request.expected_revision,ControlAction::SendToObs(capsule_id))).unwrap_or_else(|_|result("blocked","reply_encoding_failed"))},
  OperatorBody::ConfigureFfmpeg{path,sha256}=>match ExternalFfmpeg::select(&PathBuf::from(path),&sha256){Ok(approval)=>{*ffmpeg=Some(approval);result("configured","hardware_preflight_required")},Err(code)=>result("blocked",code)},
  OperatorBody::ConfigureObs{port,password}=>{
   if !services.configurable()||!runtime.accepts_configuration(){return result("blocked","private_python_unavailable");}
   let key=ServiceKey::Configuration(request.request_id.clone());let permit=match services.reserve(&key){Ok(value)=>value,Err(code)=>return result("blocked",code)};
   let present=matches!(&password,crate::operator_protocol::PasswordChoice::Provided{..});
   if runtime.journal_for_media_actor().append_service_configuration(&request.request_id,"obs",present).is_err(){return result("blocked","configuration_not_durable");}
   let choice=match password{crate::operator_protocol::PasswordChoice::Absent{}=>json!({"mode":"absent"}),crate::operator_protocol::PasswordChoice::Provided{value}=>json!({"mode":"provided","value":value.expose()})};
   services.submit(permit,key,json!({"op":"configure_obs","port":port,"password":choice}));result("pending","configuration_pending")
  },
  OperatorBody::ConfigureTwitch{channel}=>{
   if !services.configurable()||!runtime.accepts_configuration(){return result("blocked","private_python_unavailable");}
   if services.chat_running(){return result("rejected","chat_already_running");}
   let key=ServiceKey::Configuration(request.request_id.clone());let permit=match services.reserve(&key){Ok(value)=>value,Err(code)=>return result("blocked",code)};
   if runtime.journal_for_media_actor().append_service_configuration(&request.request_id,"chat",false).is_err(){return result("blocked","configuration_not_durable");}
   services.submit(permit,key,json!({"op":"start_chat","channel":channel}));result("pending","configuration_pending")
  },
 }
}
async fn execute_effect(job:&crate::control_intents::ActorControlJob,authority:&mut ControlRuntime<ControlCommandSender>,windows:&mut WindowRegistry,adapter:&mut NativeAdapter,book:&PairingBook,engine:&mut RuntimeEffects<RecordingSource>,ffmpeg:Option<&ExternalFfmpeg>,services:&mut ServiceClient,failure:&mut Option<&'static str>,frozen_window:Option<&crate::windows_window_registry::WindowView>)->Option<ActorCompletion> {
 let interrupted=Some(ActorCompletion::Interrupted);
 if job.authority_epoch!=authority.capsule_snapshot().authority_epoch{return interrupted;}
 if let ActorControlAction::StopRecording(capsule)=&job.action{
  // Tile cleanup remains admitted after closure, crop changes or ambiguous
  // titles. Stopping never selects or acts on a newly resolved window.
  return if engine.stop_capsule(&capsule.capsule_id,authority.journal_for_media_actor()).await.is_ok(){Some(ActorCompletion::Applied)}else{*failure=Some(engine.status().iter().find(|row|row.capsule_id==capsule.capsule_id).and_then(|row|row.reason).unwrap_or("recording_interrupted"));interrupted};
 }
 if matches!(&job.action,ActorControlAction::StartRecording{..}|ActorControlAction::SendToObs(_))&&adapter.enumerate(windows).is_err(){*failure=Some("window_unavailable");return interrupted;}
 if let Some(frozen)=frozen_window{if let Err(code)=resolve_frozen_window(windows,frozen){*failure=Some(code);return interrupted;}if classify_discord_window(frozen)!=DiscordWindowClass::Popout{*failure=Some("window_changed");return interrupted;}}
 if let ActorControlAction::SetFolder(folder)=&job.action{return if folder.revalidate().is_ok(){Some(ActorCompletion::Applied)}else{*failure=Some("folder_not_authorized");interrupted};}
 let current=authority.capsule_snapshot();
 let capsule=match &job.action{ActorControlAction::StartRecording{capsule,..}|ActorControlAction::StopRecording(capsule)|ActorControlAction::SendToObs(capsule)=>capsule,_=>return interrupted};
 let Some(actual)=current.capsules.get(&capsule.capsule_id) else{return interrupted;};
 if !job.matches_current_capsule(current.authority_epoch,actual)||!job.matches_current_bindings(&book.bindings())||job.window_bindings.len()!=actual.feeds.len(){*failure=Some("window_unavailable");return interrupted;}
 for binding in &job.window_bindings {let view=match windows.resolve(&binding.window,binding.window_generation){Ok(view)=>view,Err(crate::windows_window_registry::WindowError::AmbiguousDescriptor)=>{*failure=Some("window_titles_identical");return interrupted;},Err(_)=>{*failure=Some("window_unavailable");return interrupted;}};if view.layout_revision!=binding.layout_revision{*failure=Some("window_unavailable");return interrupted;}}
 match &job.action{
  ActorControlAction::StartRecording{capsule,folder}=>{
   // Retry after a failure starts clean; a live recording is still refused.
   if engine.status().iter().any(|live|live.capsule_id==capsule.capsule_id)&&!engine.clear_interrupted(&capsule.capsule_id).await{return interrupted;}
   let Some(approval)=ffmpeg else{*failure=Some("encoder_unavailable");return interrupted;};let Ok(root)=folder.revalidate() else{*failure=Some("folder_not_authorized");return interrupted;};
   // Finish all slow hardware trials before starting any native capture.
   let mut prepared=Vec::new();for (index,binding) in job.window_bindings.iter().enumerate(){
    let media_key=format!("rec-{}-{index}",crate::authority_journal::sha256_hex(job.effect_id.as_bytes()));
    match with_recording_ticks(crate::runtime_recording::prepare_feed(approval,binding,&media_key,root),engine,windows).await{Ok(value)=>prepared.push((binding,media_key,value)),Err(code)=>{*failure=Some(recording_state_reason(code));return interrupted;},}
   }
   let mut feeds:Vec<(String,crate::capture_pipeline::CapturePipeline<RecordingSource>)>=Vec::new();for (binding,media_key,approved) in prepared{
    if adapter.enumerate(windows).is_err(){*failure=Some("window_unavailable");for (_,pipeline) in &mut feeds{let _=pipeline.abort().await;}return interrupted;}
    if let Some(frozen)=frozen_window{if let Err(code)=resolve_frozen_window(windows,frozen){*failure=Some(code);for (_,pipeline) in &mut feeds{let _=pipeline.abort().await;}return interrupted;}}
    match with_recording_ticks(crate::runtime_recording::build_feed(approved,binding,&media_key,&capsule.person_label,windows,adapter,root,authority.journal_for_media_actor()),engine,windows).await{Ok(pipeline)=>feeds.push((binding.feed_id.clone(),pipeline)),Err(code)=>{*failure=Some(recording_state_reason(code));for (_,pipeline) in &mut feeds{let _=pipeline.abort().await;}return interrupted;}}
   }
   if engine.attach_capsule(capsule.capsule_id.clone(),feeds).await.is_ok(){Some(ActorCompletion::Applied)}else{*failure=Some("recording_capacity_exceeded");interrupted}
  },
  ActorControlAction::StopRecording(capsule)=>if engine.stop_capsule(&capsule.capsule_id,authority.journal_for_media_actor()).await.is_ok(){Some(ActorCompletion::Applied)}else{interrupted},
  ActorControlAction::SendToObs(capsule)=>{
   if !services.obs_ready(){*failure=Some("obs_not_ready");return interrupted;}
   let observed=windows.snapshots();let mut feeds=serde_json::Map::new();let mut crops=serde_json::Map::new();
   for binding in &job.window_bindings {
    let view=match windows.resolve(&binding.window,binding.window_generation){Ok(view)=>view,Err(crate::windows_window_registry::WindowError::AmbiguousDescriptor)=>{*failure=Some("window_titles_identical");return interrupted;},Err(_)=>{*failure=Some("window_unavailable");return interrupted;}};
    if observed.iter().filter(|v|v.visible&&!v.minimized&&v.title==view.title&&v.class==view.class&&v.executable==view.executable).count()!=1{*failure=Some("window_titles_identical");return interrupted;}
    let role=match binding.role{crate::capsule_registry::Role::Game=>"game",crate::capsule_registry::Role::Camera=>"camera"};
    if feeds.insert(role.into(),json!(format!("{}:{}:{}",view.title,view.class,view.executable))).is_some(){*failure=Some("window_unavailable");return interrupted;}
    let crop=crate::runtime_recording::obs_crop(binding.crop,(view.rect.width,view.rect.height),crate::runtime_recording::window_top_trim(&view,windows,binding));crops.insert(role.into(),json!({"x":crop.x,"y":crop.y,"width":crop.width,"height":crop.height}));
   }
   let label:String=capsule.person_label.chars().filter_map(|c|if c.is_ascii_alphanumeric()||c=='_'||c=='-'{Some(c)}else if c.is_whitespace(){Some('_')}else{None}).take(64).collect::<String>().trim_matches('_').to_string();
   let target=if label.is_empty(){&capsule.capsule_id}else{&label};
   let body=json!({"op":"send_capsule","capsule_id":target,"feeds":feeds,"crops":crops});
   if crate::authority_auth::validate_authority_frame(&crate::authority_auth::make_frame("service_request",job.authority_epoch.0,body.clone()),job.authority_epoch.0).is_err(){*failure=Some("unavailable");return interrupted;}
   let key=ServiceKey::Effect(job.effect_id.clone());let Ok(permit)=services.reserve(&key)else{*failure=Some("unavailable");return interrupted;};services.submit(permit,key,body);None
  },
  _=>interrupted,
 }
}
pub(crate) fn run()->Result<(),&'static str>{
 let root=std::env::var_os("CAFE_RUNTIME_ROOT").ok_or("runtime_root_absent")?;
 let storage=RuntimeStorage::open(&PathBuf::from(root))?;
 let executor=tokio::runtime::Builder::new_current_thread().enable_all().build().map_err(|_|"runtime_executor_unavailable")?;
 executor.block_on(async{
  let journal=Journal::open(&storage.session.join("authority.journal"),&storage.session_id,storage.epoch).map_err(|_|"runtime_journal_unavailable")?;
  let (effects_tx,mut effects_rx)=tokio::sync::mpsc::channel(MAX_CAPSULES*2+1);let bindings=Arc::new(std::sync::RwLock::new(Vec::new()));
  let sender=ControlCommandSender::new(effects_tx,bindings.clone());let mut authority=ControlRuntime::open(journal,storage.epoch,sender,&[storage.recordings.clone()])?;
  let (requests_tx,mut requests_rx)=tokio::sync::mpsc::channel(8);let client=ControlActorClient::new(requests_tx,Duration::from_secs(4))?;
  let broker=BrokerAuthority::start_control(&storage.session.join("broker"),"127.0.0.1",storage.epoch.0,Box::new(client)).await.map_err(|_|"runtime_broker_unavailable")?;
  let mut windows=WindowRegistry::new(storage.epoch);let mut adapter=NativeAdapter::new(storage.epoch);let mut book=PairingBook::new();let mut engine=RuntimeEffects::<RecordingSource>::new(MAX_CAPSULES,MAX_CAPSULES*2).map_err(|_|"runtime_effect_budget_invalid")?;
  let mut organizer=RuntimeOrganizer::new(storage.epoch);
  let mut services=ServiceClient::packaged(storage.chat.clone(),storage.epoch.0);let (mut ffmpeg,start_state,start_code)=startup_ffmpeg(std::env::var_os("CAFE_FFMPEG_PATH").map(PathBuf::from),std::env::var("CAFE_FFMPEG_SHA256").ok(),std::env::var_os("CAFE_FFMPEG_STARTUP_BLOCKED").is_some());let mut window_offset=0;let input=input();let output=output();let mut last=result(start_state,start_code);let mut preview_checked=Instant::now();output.publish(snapshot(&authority,&windows,&last,ffmpeg.is_some(),&engine,window_offset,&services,&book,&organizer,&mut adapter));
  loop{
   let mut changed=organizer.expire(Instant::now());
   for _ in 0..4{match input.try_recv(){Ok(bytes)=>{last=match decode_operator(&bytes){Ok(request)=>{let request_id=request.request_id.clone();let mut reply=mutate(request,&mut authority,&mut windows,&mut adapter,&mut book,&bindings,&mut ffmpeg,&mut window_offset,&mut services,&mut organizer,&engine);// Every result names its request, so the UI can match its own outcome.
if reply.get("request_id").is_none_or(Value::is_null){reply["request_id"]=json!(request_id);}reply},Err(code)=>result("rejected",code)};changed=true;},Err(std::sync::mpsc::TryRecvError::Empty)=>break,Err(std::sync::mpsc::TryRecvError::Disconnected)=>{let _=engine.abort_all().await;services.close().await;drop(broker);return Ok(());}}}
   for _ in 0..8{match requests_rx.try_recv(){Ok(request)=>{request.execute_with_organizer(&mut authority,|authority,value,peer|{let mut response=organizer_request(value,authority,&mut organizer,&mut windows,&mut adapter,&book,ffmpeg.as_ref(),&engine,services.obs_ready(),||peer.is_live());dispatch_organizer_proposal(&mut response,value["body"]["op"].as_str().unwrap_or(""),authority,&mut organizer,&mut windows,&mut adapter,&mut book,&bindings,&engine,ffmpeg.as_ref(),services.obs_ready(),&||peer.is_live());response});changed=true;},Err(_)=>break}}
   // Revisit trusted input between setup jobs instead of awaiting a whole batch.
   for _ in 0..1{match effects_rx.try_recv(){Ok(job)=>{
    let frozen=organizer.effects.get(&job.effect_id).and_then(|effect|effect.window.clone());
    let mut failure=None;let outcome=execute_effect(&job,&mut authority,&mut windows,&mut adapter,&book,&mut engine,ffmpeg.as_ref(),&mut services,&mut failure,frozen.as_ref()).await;
    if let Some(outcome)=outcome{
     let reply=authority.complete_from_actor(&job.effect_id,outcome);let committed=reply.state==ControlState::Committed;
     let capsule=match &job.action{ActorControlAction::StartRecording{capsule,..}|ActorControlAction::StopRecording(capsule)=>Some(&capsule.capsule_id),_=>None};
     let code=failure.unwrap_or(if committed{"recording_effect_completed"}else{"recording_interrupted"});
     if let Some(capsule)=capsule{if committed{organizer.failures.remove(capsule);}else{organizer.failures.insert(capsule.clone(),crate::runtime_effects::recording_reason(code));}}
     complete_organizer_effect(&mut organizer,&job.effect_id,committed,failure);
     last=serde_json::to_value(reply).unwrap_or_else(|_|result("blocked","reply_encoding_failed"));if capsule.is_some()&&!committed{last["code"]=json!(code);}
    }changed=true;
   },Err(_)=>break}}
   let (completed,service_changed)=services.drain();changed|=service_changed;
   for (key,applied) in completed {match key{
    ServiceKey::Configuration(_)=>last=if applied{result("configured","service_configured")}else{result("blocked","service_operation_failed")},
    ServiceKey::Effect(effect)=>{
     let outcome=if applied{ActorCompletion::Applied}else{ActorCompletion::Interrupted};
     let reply=authority.complete_from_actor(&effect,outcome);
     let committed=reply.state==ControlState::Committed;
     complete_organizer_effect(&mut organizer,&effect,committed,Some(if !services.obs_ready(){"obs_not_ready"}else{"unavailable"}));
     last=serde_json::to_value(reply).unwrap_or_else(|_|result("blocked","reply_encoding_failed"));
    },
   }changed=true;}
   if engine.tick_all(&windows,Instant::now()).await.is_err(){last=result("interrupted","recording_effect_failed");changed=true;}
   changed|=auto_anchor(&mut authority,&mut organizer,&mut windows,&mut adapter,&mut book,&bindings,&engine);
   if preview_checked.elapsed()>=Duration::from_secs(1){preview_checked=Instant::now();changed|=!current_bindings(&authority,&book).is_empty();}
   if changed{output.publish(snapshot(&authority,&windows,&last,ffmpeg.is_some(),&engine,window_offset,&services,&book,&organizer,&mut adapter));}tokio::time::sleep(Duration::from_millis(5)).await;
  }
 })
}

#[cfg(test)]mod page_projection_tests{
 use super::*;use crate::windows_window_registry::{FixtureWindow,Rect};
 #[test]fn pages_cover_every_window_exactly_once_without_cutting_identity(){
  let mut registry=WindowRegistry::new(Clock(7));let input=(0..7).map(|index|FixtureWindow{slot:format!("slot-{index}"),incarnation:1,process_id:100+index,process_birth:1,executable:format!("synthetic-{index}.exe"),title:format!("Synthetic {index}"),class:format!("Class{index}"),rect:Rect{x:0,y:0,width:100,height:80},visible:true,minimized:false,layout:"one".into()}).collect::<Vec<_>>();registry.refresh_fixtures(&input).unwrap();
  let mut identities=std::collections::BTreeSet::new();for (offset,count,next) in [(0,3,Some(3)),(3,3,Some(6)),(6,1,None)]{let (page,total,actual_next)=window_page(&registry,offset);assert_eq!(total,7);assert_eq!(page.len(),count);assert_eq!(actual_next,next);for row in page{assert!(identities.insert(row.handle));}}
  assert_eq!(identities.len(),7);
 }
}

#[cfg(test)]mod organizer_runtime_tests{
 use super::*;
 use std::sync::atomic::{AtomicUsize,Ordering};
 static NEXT:AtomicUsize=AtomicUsize::new(0);
 struct Temporary(PathBuf);
 impl Temporary{fn new()->Self{let path=std::env::temp_dir().join(format!("cafe-organizer-runtime-{}-{}",std::process::id(),NEXT.fetch_add(1,Ordering::Relaxed)));std::fs::create_dir(&path).unwrap();Self(path)}}
 impl Drop for Temporary{fn drop(&mut self){let _=std::fs::remove_dir_all(&self.0);}}
 fn wire(body:Value)->Value{json!({"schema":"cafe.organizer.v1","kind":"organizer_request","request_id":"synthetic-request","authority_epoch":"77","body":body})}
 #[test]fn remote_schema_cannot_carry_native_approval_or_configuration(){
  for op in ["organizer_plan_confirm","organizer_plan_reject","set_auto_anchor","set_ask_before_ai_actions","set_folder","send_to_obs","configure_ffmpeg","configure_obs","configure_twitch"]{
   assert!(serde_json::from_value::<OrganizerRequest>(wire(json!({"op":op}))).is_err());
  }
  for enabled in [true,false]{assert!(serde_json::from_value::<OrganizerRequest>(wire(json!({"op":"set_ask_before_ai_actions","enabled":enabled}))).is_err());}
  assert!(serde_json::from_value::<OrganizerRequest>(wire(json!({"op":"propose_stop_all","ask_before_ai_actions":false}))).is_err());
  let mut forged=wire(json!({"op":"organizer_status"}));forged["actor"]=json!("operator");assert!(serde_json::from_value::<OrganizerRequest>(forged).is_err());
  assert!(serde_json::from_value::<OrganizerRequest>(wire(json!({"op":"propose_recording_plan","folder_name":"Session","confirmation_token":"synthetic"}))).is_err());
 }
 #[test]fn native_organizer_ops_require_exact_local_schema(){
  for body in [json!({"op":"organizer_plan_confirm","plan_id":"organizer-77-1"}),json!({"op":"organizer_plan_reject","plan_id":"organizer-77-1"}),json!({"op":"set_auto_anchor","enabled":true}),json!({"op":"set_ask_before_ai_actions","enabled":true})]{
   let local=json!({"schema":"cafe.operator.v1","kind":"operator_intent","request_id":"synthetic-local","authority_epoch":"77","expected_revision":"0","body":body});
   assert!(decode_operator(&serde_json::to_vec(&local).unwrap()).is_ok());assert!(decode_operator(&serde_json::to_vec(&wire(body)).unwrap()).is_err());
  }
 }
 #[test]fn direct_default_uses_confirm_path_and_local_switch_restores_pending_plan(){
  tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap().block_on(async {
  let temp=Temporary::new();let journal=Journal::open(&temp.0.join("authority.journal"),"synthetic",Clock(77)).unwrap();let (sender,mut effects)=tokio::sync::mpsc::channel(25);let bindings=Arc::new(std::sync::RwLock::new(Vec::new()));
  let mut runtime=ControlRuntime::open(journal,Clock(77),ControlCommandSender::new(sender,bindings.clone()),&[temp.0.clone()]).unwrap();let mut organizer=RuntimeOrganizer::new(Clock(77));let mut windows=WindowRegistry::new(Clock(77));let mut adapter=NativeAdapter::new(Clock(77));let mut book=PairingBook::new();let engine=RuntimeEffects::<RecordingSource>::new(MAX_CAPSULES,24).unwrap();
  assert!(!organizer.ask_before_ai_actions);
  for operation in ["propose_recording_plan","propose_stop_all","propose_send_to_obs","propose_obs_scene"]{
   let id=if operation=="propose_recording_plan"{let mut fixture=fixture(320);fixture.class="Chrome_WidgetWin_1".into();let views=windows.refresh_fixtures(&[fixture]).unwrap();organizer.plans.propose_recording("Session".into(),None,&views,true,Instant::now(),0).unwrap()}else if operation=="propose_stop_all"{organizer.plans.propose_stop_all(vec!["missing-capsule".into()],Instant::now(),0).unwrap()}else{organizer.plans.propose_send_to_obs(vec![("missing-capsule".into(),"<b>untrusted</b>".into())],true,Instant::now(),0).unwrap()};
   let mut response=json!({"error":null,"data":{"plan_id":id,"state":"pending_confirmation"}});
   dispatch_organizer_proposal(&mut response,operation,&mut runtime,&mut organizer,&mut windows,&mut adapter,&mut book,&bindings,&engine,None,false,&||true);
   assert_eq!(response["data"]["state"],"completed");assert!(organizer.plans.projection().is_null());
   let outcome=organizer.plans.plan_result(&id).unwrap();assert_eq!(outcome["state"],"completed");
   assert_ne!(outcome["windows"][0]["state"],"sent");assert_ne!(outcome["windows"][0]["state"],"stopped");
   assert_eq!(organizer.ai_activity["count"],1);assert!(!organizer.ai_activity.to_string().contains("untrusted"));
  }
  // Only a validated local operator envelope changes the session preference.
  let local=json!({"schema":"cafe.operator.v1","kind":"operator_intent","request_id":"local-preference","authority_epoch":"77","expected_revision":runtime.revision(),"body":{"op":"set_ask_before_ai_actions","enabled":true}});
  let mut ffmpeg=None;let mut offset=0;let mut services=ServiceClient::packaged(temp.0.clone(),77);
  let reply=mutate(decode_operator(&serde_json::to_vec(&local).unwrap()).unwrap(),&mut runtime,&mut windows,&mut adapter,&mut book,&bindings,&mut ffmpeg,&mut offset,&mut services,&mut organizer,&engine);
  assert_eq!(reply["code"],"ai_confirmation_updated");assert!(organizer.ask_before_ai_actions);
  let id=organizer.plans.propose_stop_all(vec!["missing-capsule".into()],Instant::now(),0).unwrap();
  let mut response=json!({"error":null,"data":{"plan_id":id,"state":"pending_confirmation"}});
  dispatch_organizer_proposal(&mut response,"propose_stop_all",&mut runtime,&mut organizer,&mut windows,&mut adapter,&mut book,&bindings,&engine,None,false,&||true);
  assert_eq!(response["data"]["state"],"pending_confirmation");assert_eq!(organizer.plans.projection()["plan_id"],id);
  assert!(effects.try_recv().is_err());
  assert!(!RuntimeOrganizer::new(Clock(78)).ask_before_ai_actions);
  services.close().await;
  });
 }
 fn select_fixture_root(runtime:&mut ControlRuntime<ControlCommandSender>,effects:&mut tokio::sync::mpsc::Receiver<crate::control_intents::ActorControlJob>,root:&std::path::Path){
  let reply=runtime.handle_operator_folder("fixture-root",Clock(77),runtime.revision(),root.to_path_buf());assert_eq!(reply.state,ControlState::Pending);
  let job=effects.try_recv().unwrap();assert!(matches!(job.action,ActorControlAction::SetFolder(_)));
  assert_eq!(runtime.complete_from_actor(&job.effect_id,ActorCompletion::Applied).state,ControlState::Committed);
 }
 #[test]fn direct_admission_revoked_during_native_preparation_never_writes_or_dispatches(){
  let temp=Temporary::new();let journal=Journal::open(&temp.0.join("authority.journal"),"synthetic",Clock(77)).unwrap();let (sender,mut effects)=tokio::sync::mpsc::channel(25);let bindings=Arc::new(std::sync::RwLock::new(Vec::new()));
  let mut runtime=ControlRuntime::open(journal,Clock(77),ControlCommandSender::new(sender,bindings.clone()),&[temp.0.clone()]).unwrap();let mut organizer=RuntimeOrganizer::new(Clock(77));let mut windows=WindowRegistry::new(Clock(77));let mut book=PairingBook::new();let engine=RuntimeEffects::<RecordingSource>::new(MAX_CAPSULES,24).unwrap();
  select_fixture_root(&mut runtime,&mut effects,&temp.0);
  let mut fixture=fixture(320);fixture.class="Chrome_WidgetWin_1".into();let views=windows.refresh_fixtures(&[fixture]).unwrap();
  let id=organizer.plans.propose_recording("ExpiredSession".into(),None,&views,true,Instant::now(),0).unwrap();organizer.root=Some((id.clone(),runtime.organizer_root().unwrap()));
  let before=runtime.journal_for_media_actor().records().len();let before_revision=runtime.revision();
  let live=std::cell::Cell::new(true);let prepared=std::cell::Cell::new(false);
  // Deterministically revoke admission while the native preparation call is in flight.
  let mut enumerate=|_:&mut WindowRegistry|{assert!(live.get());prepared.set(true);live.set(false);true};
  let mut preparation=OrganizerPreparation{enumerate:&mut enumerate,encoder_available:true};
  let mut response=json!({"error":null,"data":{"plan_id":id,"state":"pending_confirmation"}});
  dispatch_organizer_proposal_prepared(&mut response,"propose_recording_plan",&mut runtime,&mut organizer,&mut windows,&mut book,&bindings,&engine,&mut preparation,false,&||live.get());
  assert!(prepared.get());assert_eq!(response["error"],"unavailable");assert!(response["data"].is_null());
  assert_eq!(runtime.journal_for_media_actor().records().len(),before);assert_eq!(runtime.revision(),before_revision);
  assert!(runtime.capsule_snapshot().capsules.is_empty());assert!(book.bindings().is_empty());assert!(organizer.effects.is_empty());assert!(effects.try_recv().is_err());assert!(!temp.0.join("ExpiredSession").exists());
  let outcome=organizer.plans.plan_result(&id).unwrap();assert_eq!(outcome["state"],"completed");assert_eq!(outcome["windows"][0]["state"],"unavailable");
  let id=organizer.plans.propose_recording("AlreadyRevoked".into(),None,&views,true,Instant::now(),0).unwrap();organizer.root=Some((id.clone(),runtime.organizer_root().unwrap()));
  let mut response=json!({"error":null,"data":{"plan_id":id,"state":"pending_confirmation"}});
  let mut never_enumerate=|_:&mut WindowRegistry|->bool{panic!("revoked request reached preparation")};let mut preparation=OrganizerPreparation{enumerate:&mut never_enumerate,encoder_available:true};
  dispatch_organizer_proposal_prepared(&mut response,"propose_recording_plan",&mut runtime,&mut organizer,&mut windows,&mut book,&bindings,&engine,&mut preparation,false,&||false);
  assert_eq!(response["error"],"unavailable");assert!(organizer.plans.projection().is_null());assert!(organizer.root.is_none());assert_eq!(organizer.plans.plan_result(&id).unwrap()["state"],"rejected");assert_eq!(runtime.journal_for_media_actor().records().len(),before);assert!(effects.try_recv().is_err());
 }
 #[test]fn queued_operator_or_organizer_start_blocks_empty_stop_before_effect_dequeue(){
  for operator_origin in [true,false]{
   let temp=Temporary::new();let journal=Journal::open(&temp.0.join("authority.journal"),"synthetic",Clock(77)).unwrap();let (sender,mut effects)=tokio::sync::mpsc::channel(25);let bindings=Arc::new(std::sync::RwLock::new(Vec::new()));
   let mut runtime=ControlRuntime::open(journal,Clock(77),ControlCommandSender::new(sender,bindings.clone()),&[temp.0.clone()]).unwrap();let mut organizer=RuntimeOrganizer::new(Clock(77));let mut windows=WindowRegistry::new(Clock(77));let mut adapter=NativeAdapter::new(Clock(77));let mut book=PairingBook::new();let engine=RuntimeEffects::<RecordingSource>::new(MAX_CAPSULES,24).unwrap();
   select_fixture_root(&mut runtime,&mut effects,&temp.0);let mut observed=fixture(320);observed.class="Chrome_WidgetWin_1".into();let view=windows.refresh_fixtures(&[observed]).unwrap().remove(0);
   let capsule=anchor_observed(&mut runtime,&windows,&mut book,&bindings,&view,"fixture.anchor",&[],&mut organizer.reservations).unwrap();
   let action=ControlAction::StartRecording(capsule.clone());let reply=if operator_origin{runtime.handle_operator("queued-start",Clock(77),runtime.revision(),action)}else{runtime.handle_organizer("queued-start",Clock(77),action)};
   assert_eq!(reply.state,ControlState::Pending);assert!(engine.status().is_empty());
   let before=runtime.journal_for_media_actor().records().len();let revision=runtime.revision();
   let mut response=organizer_request(&wire(json!({"op":"propose_stop_all"})),&mut runtime,&mut organizer,&mut windows,&mut adapter,&book,None,&engine,false,||true);
   dispatch_organizer_proposal(&mut response,"propose_stop_all",&mut runtime,&mut organizer,&mut windows,&mut adapter,&mut book,&bindings,&engine,None,false,&||true);
   assert_eq!(response["error"],"unavailable","queued Start must prevent a false empty Completed");assert!(response["data"].is_null());assert!(organizer.plans.projection().is_null());
   assert_eq!(runtime.revision(),revision);assert_eq!(runtime.journal_for_media_actor().records().len(),before);
   let job=effects.try_recv().expect("Start remains a real queued effect until the actor handles it");assert!(matches!(&job.action,ActorControlAction::StartRecording{capsule:queued,..} if queued.capsule_id==capsule));assert!(effects.try_recv().is_err());
   assert_eq!(runtime.complete_from_actor(&job.effect_id,ActorCompletion::Interrupted).state,ControlState::Interrupted);
   let mut response=organizer_request(&wire(json!({"op":"propose_stop_all"})),&mut runtime,&mut organizer,&mut windows,&mut adapter,&book,None,&engine,false,||true);
   dispatch_organizer_proposal(&mut response,"propose_stop_all",&mut runtime,&mut organizer,&mut windows,&mut adapter,&mut book,&bindings,&engine,None,false,&||true);
   assert!(response["error"].is_null());assert_eq!(response["data"]["state"],"completed");assert!(effects.try_recv().is_err());
   // A manual empty proposal may become unsafe before the local Confirm click.
   let pending=organizer.plans.propose_stop_all(vec![],Instant::now(),0).unwrap();
   assert_eq!(runtime.handle_operator("late-start",Clock(77),runtime.revision(),ControlAction::StartRecording(capsule.clone())).state,ControlState::Pending);
   let reply=confirm_organizer(&pending,&mut runtime,&mut organizer,&mut windows,&mut adapter,&mut book,&bindings,&engine,None,false);
   assert_eq!(reply["code"],"unavailable");assert_eq!(organizer.plans.plan_result(&pending).unwrap()["state"],"pending_confirmation");
   let late=effects.try_recv().unwrap();assert!(matches!(late.action,ActorControlAction::StartRecording{..}));assert!(effects.try_recv().is_err());
   runtime.complete_from_actor(&late.effect_id,ActorCompletion::Interrupted);
   assert_eq!(confirm_organizer(&pending,&mut runtime,&mut organizer,&mut windows,&mut adapter,&mut book,&bindings,&engine,None,false)["code"],"organizer_plan_confirmed");
   assert_eq!(organizer.plans.plan_result(&pending).unwrap()["state"],"completed");
   assert!(!runtime.has_unsettled_recording_start());
   // A completed Start is no longer unsettled, but its current engine row
   // still invalidates the earlier empty plan. Supply the engine projection
   // explicitly while retaining the real queue and durable completion path.
   let pending=organizer.plans.propose_stop_all(vec![],Instant::now(),0).unwrap();
   assert_eq!(runtime.handle_operator("applied-start",Clock(77),runtime.revision(),ControlAction::StartRecording(capsule.clone())).state,ControlState::Pending);
   let applied=effects.try_recv().unwrap();assert_eq!(runtime.complete_from_actor(&applied.effect_id,ActorCompletion::Applied).state,ControlState::Committed);
   assert!(!runtime.has_unsettled_recording_start());
   let mut rows=vec![crate::runtime_effects::CapsuleStatus{capsule_id:capsule.clone(),state:crate::runtime_effects::CapsuleState::Running,reason:None,feeds:vec![]}];
   let mut enumerate=|_:&mut WindowRegistry|->bool{panic!("empty stop must not enumerate")};let mut preparation=OrganizerPreparation{enumerate:&mut enumerate,encoder_available:false};
   let before=runtime.journal_for_media_actor().records().len();
   for state in [crate::runtime_effects::CapsuleState::Running,crate::runtime_effects::CapsuleState::Interrupted]{
    rows[0].state=state;
    let reply=confirm_organizer_status_prepared(&pending,&mut runtime,&mut organizer,&mut windows,&mut book,&bindings,&rows,&mut preparation,false,&||true);
    assert_eq!(reply["code"],"unavailable");assert_eq!(organizer.plans.plan_result(&pending).unwrap()["state"],"pending_confirmation");assert!(effects.try_recv().is_err());assert_eq!(runtime.journal_for_media_actor().records().len(),before);
   }
   let reply=confirm_organizer_status_prepared(&pending,&mut runtime,&mut organizer,&mut windows,&mut book,&bindings,&[],&mut preparation,false,&||true);
   assert_eq!(reply["code"],"organizer_plan_confirmed");assert_eq!(organizer.plans.plan_result(&pending).unwrap()["state"],"completed");
   assert_eq!(runtime.handle_operator("unresolved-start",Clock(77),runtime.revision(),ControlAction::StartRecording(capsule)).state,ControlState::Pending);
   drop(runtime);
   let journal=Journal::open(&temp.0.join("authority.journal"),"synthetic",Clock(77)).unwrap();let (sender,_recovered_effects)=tokio::sync::mpsc::channel(25);
   let recovered=ControlRuntime::open(journal,Clock(77),ControlCommandSender::new(sender,bindings.clone()),&[temp.0.clone()]).unwrap();
   assert!(recovered.has_unsettled_recording_start(),"unresolved durable Start must stay conservative after restart");
  }
 }
 #[test]fn empty_stop_request_has_completed_receipt_without_durable_or_effect_mutation(){
  for ask_first in [false,true]{
   let temp=Temporary::new();let journal=Journal::open(&temp.0.join("authority.journal"),"synthetic",Clock(77)).unwrap();let (sender,mut effects)=tokio::sync::mpsc::channel(25);let bindings=Arc::new(std::sync::RwLock::new(Vec::new()));
   let mut runtime=ControlRuntime::open(journal,Clock(77),ControlCommandSender::new(sender,bindings.clone()),&[temp.0.clone()]).unwrap();let mut organizer=RuntimeOrganizer::new(Clock(77));organizer.ask_before_ai_actions=ask_first;
   let mut windows=WindowRegistry::new(Clock(77));let mut adapter=NativeAdapter::new(Clock(77));let mut book=PairingBook::new();let engine=RuntimeEffects::<RecordingSource>::new(MAX_CAPSULES,24).unwrap();
   let request=wire(json!({"op":"propose_stop_all"}));
   let denied=organizer_request(&request,&mut runtime,&mut organizer,&mut windows,&mut adapter,&book,None,&engine,false,||false);assert_eq!(denied["error"],"unavailable");assert!(organizer.plans.projection().is_null());
   let mut stale=request.clone();stale["authority_epoch"]=json!("78");let denied=organizer_request(&stale,&mut runtime,&mut organizer,&mut windows,&mut adapter,&book,None,&engine,false,||true);assert_eq!(denied["error"],"unavailable");assert!(organizer.plans.projection().is_null());
   let before=runtime.journal_for_media_actor().records().len();let revision=runtime.revision();
   let mut response=organizer_request(&request,&mut runtime,&mut organizer,&mut windows,&mut adapter,&book,None,&engine,false,||true);
   assert!(response["error"].is_null(),"empty stop must not return unavailable");let id=response["data"]["plan_id"].as_str().unwrap().to_owned();
   let conflict=organizer_request(&request,&mut runtime,&mut organizer,&mut windows,&mut adapter,&book,None,&engine,false,||true);assert_eq!(conflict["error"],"plan_pending");
   dispatch_organizer_proposal(&mut response,"propose_stop_all",&mut runtime,&mut organizer,&mut windows,&mut adapter,&mut book,&bindings,&engine,None,false,&||true);
   if ask_first{
    assert_eq!(response["data"]["state"],"pending_confirmation");
    let reply=confirm_organizer(&id,&mut runtime,&mut organizer,&mut windows,&mut adapter,&mut book,&bindings,&engine,None,false);assert_eq!(reply["code"],"organizer_plan_confirmed");
   }else{assert_eq!(response["data"]["state"],"completed");assert_eq!(organizer.ai_activity["count"],0);}
   let outcome=organizer_request(&wire(json!({"op":"plan_result","plan_id":id})),&mut runtime,&mut organizer,&mut windows,&mut adapter,&book,None,&engine,false,||true);
   assert!(outcome["error"].is_null());assert_eq!(outcome["data"],json!({"plan_id":id,"state":"completed","windows":[]}));
   assert!(organizer.plans.projection().is_null());assert!(organizer.effects.is_empty());assert!(effects.try_recv().is_err());assert!(engine.status().is_empty());assert!(book.bindings().is_empty());assert_eq!(runtime.revision(),revision);assert_eq!(runtime.journal_for_media_actor().records().len(),before);
  }
 }
 #[test]fn valid_direct_routes_queue_correlated_effects_and_manual_mode_waits_for_local_confirm(){
  tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap().block_on(async {
  for ask_first in [false,true]{
   let temp=Temporary::new();let journal=Journal::open(&temp.0.join("authority.journal"),"synthetic",Clock(77)).unwrap();let (sender,mut effects)=tokio::sync::mpsc::channel(25);let bindings=Arc::new(std::sync::RwLock::new(Vec::new()));
   let mut runtime=ControlRuntime::open(journal,Clock(77),ControlCommandSender::new(sender,bindings.clone()),&[temp.0.clone()]).unwrap();let mut organizer=RuntimeOrganizer::new(Clock(77));let mut windows=WindowRegistry::new(Clock(77));let mut adapter=NativeAdapter::new(Clock(77));let mut book=PairingBook::new();let engine=RuntimeEffects::<RecordingSource>::new(MAX_CAPSULES,24).unwrap();let mut services=ServiceClient::packaged(temp.0.clone(),77);let mut ffmpeg=None;let mut offset=0;
   select_fixture_root(&mut runtime,&mut effects,&temp.0);
   let mut fixture=fixture(320);fixture.class="Chrome_WidgetWin_1".into();let views=windows.refresh_fixtures(&[fixture]).unwrap();
   let capsule=anchor_observed(&mut runtime,&windows,&mut book,&bindings,&views[0],"fixture.anchor",&[],&mut organizer.reservations).unwrap();
   let toggle=OperatorRequest{schema:"cafe.operator.v1".into(),kind:"operator_intent".into(),request_id:"fixture-preference".into(),authority_epoch:Clock(77),expected_revision:runtime.revision(),body:OperatorBody::SetAskBeforeAiActions{enabled:ask_first}};
   assert_eq!(mutate(toggle,&mut runtime,&mut windows,&mut adapter,&mut book,&bindings,&mut ffmpeg,&mut offset,&mut services,&mut organizer,&engine)["code"],"ai_confirmation_updated");
   for operation in ["propose_recording_plan","propose_stop_all","propose_send_to_obs","propose_obs_scene"]{
    // These fixtures replace only native enumeration and encoder availability.
    // Folder confinement, capsule/feed validation, admission and receipts remain real.
    let mut enumerate=|_:&mut WindowRegistry|true;let mut preparation=OrganizerPreparation{enumerate:&mut enumerate,encoder_available:true};
    let proposal=match operation{
     "propose_recording_plan"=>{let id=organizer.plans.propose_recording("Session".into(),None,&windows.snapshots(),true,Instant::now(),0).unwrap();organizer.root=Some((id.clone(),runtime.organizer_root().unwrap()));json!({"plan_id":id,"state":"pending_confirmation"})},
     "propose_stop_all"=>{let id=organizer.plans.propose_stop_all(vec![capsule.clone()],Instant::now(),0).unwrap();json!({"plan_id":id,"state":"pending_confirmation"})},
     "propose_send_to_obs"=>{let selected=organizer_obs_capsules(&runtime,&windows,&book,Some(vec![capsule.clone()])).unwrap();let id=organizer.plans.propose_send_to_obs(selected,true,Instant::now(),0).unwrap();json!({"plan_id":id,"state":"pending_confirmation"})},
     _=>propose_obs_scene_plan(&runtime,&mut organizer,&windows,&book,capsule.clone(),true,Instant::now(),0).unwrap(),
    };
    let id=proposal["plan_id"].as_str().unwrap().to_string();let mut response=json!({"error":null,"data":proposal});let before=runtime.journal_for_media_actor().records().len();
    dispatch_organizer_proposal_prepared(&mut response,operation,&mut runtime,&mut organizer,&mut windows,&mut book,&bindings,&engine,&mut preparation,true,&||true);
    if ask_first{
     assert_eq!(response["data"]["state"],"pending_confirmation");assert!(effects.try_recv().is_err());assert_eq!(runtime.journal_for_media_actor().records().len(),before);
     let wire=json!({"schema":"cafe.operator.v1","kind":"operator_intent","request_id":format!("local-confirm-{id}"),"authority_epoch":"77","expected_revision":runtime.revision(),"body":{"op":"organizer_plan_confirm","plan_id":id}});
     for field in ["authority_epoch","expected_revision"]{
      let mut stale=wire.clone();stale[field]=json!(if field=="authority_epoch"{"78"}else{"18446744073709551615"});
      let reply=mutate_prepared(decode_operator(&serde_json::to_vec(&stale).unwrap()).unwrap(),&mut runtime,&mut windows,&mut adapter,&mut book,&bindings,&mut ffmpeg,&mut offset,&mut services,&mut organizer,&engine,Some((&mut preparation,true)));
      assert_eq!(reply["state"],"rejected");assert!(effects.try_recv().is_err());assert_eq!(runtime.journal_for_media_actor().records().len(),before);
     }
     let local=decode_operator(&serde_json::to_vec(&wire).unwrap()).unwrap();
     let reply=mutate_prepared(local,&mut runtime,&mut windows,&mut adapter,&mut book,&bindings,&mut ffmpeg,&mut offset,&mut services,&mut organizer,&engine,Some((&mut preparation,true)));
     assert_eq!(reply["code"],"organizer_plan_confirmed");
    }else{assert_eq!(response["data"]["state"],"executing");}
    let job=effects.try_recv().unwrap();assert!(effects.try_recv().is_err());
    let expected=match operation{
     "propose_recording_plan"=>{assert!(matches!(&job.action,ActorControlAction::StartRecording{capsule:c,..} if c.capsule_id==capsule));assert_eq!(job.window_bindings.len(),1);"recording"},
     "propose_stop_all"=>{assert!(matches!(&job.action,ActorControlAction::StopRecording(c) if c.capsule_id==capsule));"stopped"},
     _=>{assert!(matches!(&job.action,ActorControlAction::SendToObs(c) if c.capsule_id==capsule));"sent"},
    };
    assert_eq!(organizer.effects[&job.effect_id].plan_id,id);assert!(organizer.plans.plan_result(&id).is_err());
    complete_organizer_effect(&mut organizer,"foreign-effect",true,None);assert!(organizer.plans.plan_result(&id).is_err());
    let reply=runtime.complete_from_actor(&job.effect_id,ActorCompletion::Applied);assert_eq!(reply.state,ControlState::Committed);
    complete_organizer_effect(&mut organizer,&job.effect_id,true,None);
    let outcome=organizer.plans.plan_result(&id).unwrap();assert_eq!(outcome["state"],"completed");assert_eq!(outcome["windows"][0]["capsule_id"],capsule);assert_eq!(outcome["windows"][0]["state"],expected);assert!(organizer.effects.is_empty());
   }
   services.close().await;
  }
  });
 }
 #[test]fn status_and_refused_remote_requests_have_no_durable_effect(){
  let temp=Temporary::new();let journal=Journal::open(&temp.0.join("authority.journal"),"synthetic",Clock(77)).unwrap();let (sender,mut effects)=tokio::sync::mpsc::channel(2);let shared=Arc::new(std::sync::RwLock::new(Vec::new()));
  let mut runtime=ControlRuntime::open(journal,Clock(77),ControlCommandSender::new(sender,shared),&[temp.0.clone()]).unwrap();let mut organizer=RuntimeOrganizer::new(Clock(77));let mut windows=WindowRegistry::new(Clock(77));let mut adapter=NativeAdapter::new(Clock(77));let book=PairingBook::new();let engine=RuntimeEffects::<RecordingSource>::new(MAX_CAPSULES,24).unwrap();
  let status=organizer_request(&wire(json!({"op":"organizer_status"})),&mut runtime,&mut organizer,&mut windows,&mut adapter,&book,None,&engine,false,||true);assert_eq!(status["data"]["capsule_count"],0);assert_eq!(status["data"]["encoder_ready"],false);assert_eq!(status["data"]["obs_ready"],false);assert_eq!(status["data"]["folder_root_set"],false);
  let proposal=organizer_request(&wire(json!({"op":"propose_recording_plan","folder_name":"Session"})),&mut runtime,&mut organizer,&mut windows,&mut adapter,&book,None,&engine,false,||true);assert_eq!(proposal["error"],"folder_root_required");
  let obs_refused=organizer_request(&wire(json!({"op":"propose_send_to_obs"})),&mut runtime,&mut organizer,&mut windows,&mut adapter,&book,None,&engine,false,||true);assert_eq!(obs_refused["error"],"obs_not_ready");assert!(organizer.plans.projection().is_null());
  let scene_refused=organizer_request(&wire(json!({"op":"propose_obs_scene","capsule_id":"organizer-77-1"})),&mut runtime,&mut organizer,&mut windows,&mut adapter,&book,None,&engine,false,||true);assert_eq!(scene_refused["error"],"obs_not_ready");assert!(organizer.plans.projection().is_null());
  assert_eq!(organizer_request(&wire(json!({"op":"organizer_status"})),&mut runtime,&mut organizer,&mut windows,&mut adapter,&book,None,&engine,false,||false)["error"],"unavailable");
  organizer.plans.propose_stop_all(vec!["synthetic".into()],Instant::now(),0).unwrap();
  let conflict=organizer_request(&wire(json!({"op":"propose_stop_all","capsule_ids":["absent"]})),&mut runtime,&mut organizer,&mut windows,&mut adapter,&book,None,&engine,false,||true);assert_eq!(conflict["error"],"plan_pending");
  let obs_conflict=organizer_request(&wire(json!({"op":"propose_send_to_obs"})),&mut runtime,&mut organizer,&mut windows,&mut adapter,&book,None,&engine,true,||true);assert_eq!(obs_conflict["error"],"plan_pending");
  let scene_conflict=organizer_request(&wire(json!({"op":"propose_obs_scene","capsule_id":"organizer-77-1"})),&mut runtime,&mut organizer,&mut windows,&mut adapter,&book,None,&engine,true,||true);assert_eq!(scene_conflict["error"],"plan_pending");
  assert!(effects.try_recv().is_err());assert!(runtime.journal_for_media_actor().records().is_empty());
 }
 #[test]fn send_to_obs_proposal_and_confirm_require_unique_windows_and_operator_confirm(){
  let temp=Temporary::new();let journal=Journal::open(&temp.0.join("authority.journal"),"synthetic",Clock(77)).unwrap();let (sender,mut effects)=tokio::sync::mpsc::channel(25);let bindings=Arc::new(std::sync::RwLock::new(Vec::new()));
  let mut runtime=ControlRuntime::open(journal,Clock(77),ControlCommandSender::new(sender,bindings.clone()),&[temp.0.clone()]).unwrap();let mut organizer=RuntimeOrganizer::new(Clock(77));let mut windows=WindowRegistry::new(Clock(77));let mut adapter=NativeAdapter::new(Clock(77));let mut book=PairingBook::new();let engine=RuntimeEffects::<RecordingSource>::new(MAX_CAPSULES,24).unwrap();let mut reservations=BTreeMap::new();
  assert_eq!(organizer_obs_capsules(&runtime,&windows,&book,None),Err("no_discord_windows"));
  let mut f1=fixture(320);f1.slot="slot-1".into();f1.title="Stream One".into();
  let mut f2=fixture(320);f2.slot="slot-2".into();f2.title="Stream Two".into();
  let views=windows.refresh_fixtures(&[f1.clone(),f2.clone()]).unwrap();
  let cap1=anchor_classified(&mut runtime,&windows,&mut book,&bindings,&views[0],"organizer.auto.1",&[],&mut reservations,DiscordWindowClass::Popout).unwrap();
  let cap2=anchor_classified(&mut runtime,&windows,&mut book,&bindings,&views[1],"organizer.auto.2",&[],&mut reservations,DiscordWindowClass::Popout).unwrap();
  let selected=organizer_obs_capsules(&runtime,&windows,&book,None).unwrap();
  assert_eq!(selected,vec![(cap1.clone(),"Stream One".into()),(cap2.clone(),"Stream Two".into())]);
  let id=organizer.plans.propose_send_to_obs(selected,true,Instant::now(),1000).unwrap();
  assert_eq!(organizer.plans.projection()["folder_name"],"");
  assert_eq!(organizer.plans.projection()["windows"],json!([{"title":"Stream One"},{"title":"Stream Two"}]));
  assert!(effects.try_recv().is_err());
  let not_ready_id=Organizer::new(Clock(77)).propose_send_to_obs(vec![(cap1.clone(),"Stream One".into())],false,Instant::now(),1000);
  assert_eq!(not_ready_id,Err("obs_not_ready"));
  // Confirm when obs_ready becomes false records obs_not_ready per capsule without queuing effects.
  let reply=confirm_organizer(&id,&mut runtime,&mut organizer,&mut windows,&mut adapter,&mut book,&bindings,&engine,None,false);
  assert_eq!(reply["state"],"pending");
  assert!(effects.try_recv().is_err());
  let res=organizer.plans.plan_result(&id).unwrap();
  assert_eq!(res["state"],"completed");
  assert_eq!(res["windows"],json!([{"title":"Stream One","capsule_id":cap1,"state":"obs_not_ready"},{"title":"Stream Two","capsule_id":cap2,"state":"obs_not_ready"}]));
  // Confirm when obs_ready is true queues SendToObs effects one by one.
  let selected2=organizer_obs_capsules(&runtime,&windows,&book,Some(vec![cap1.clone(),cap2.clone()])).unwrap();
  let id2=organizer.plans.propose_send_to_obs(selected2,true,Instant::now(),2000).unwrap();
  let reply2=confirm_organizer(&id2,&mut runtime,&mut organizer,&mut windows,&mut adapter,&mut book,&bindings,&engine,None,true);
  assert_eq!(reply2["state"],"pending");
  let job1=effects.try_recv().unwrap();let job2=effects.try_recv().unwrap();
  assert!(matches!(&job1.action,ActorControlAction::SendToObs(c) if c.capsule_id==cap1 && c.person_label=="slot-1"));
  assert!(matches!(&job2.action,ActorControlAction::SendToObs(c) if c.capsule_id==cap2 && c.person_label=="slot-2"));
  // propose_obs_scene proposes a single capsule and requires operator Confirm before queuing SendToObs.
  let scene_prop=propose_obs_scene_plan(&runtime,&mut organizer,&windows,&book,cap1.clone(),true,Instant::now(),3000).unwrap();
  let scene_plan_id=scene_prop["plan_id"].as_str().unwrap().to_string();
  assert_eq!(scene_prop["state"],"pending_confirmation");
  assert!(effects.try_recv().is_err());
  let reply3=confirm_organizer(&scene_plan_id,&mut runtime,&mut organizer,&mut windows,&mut adapter,&mut book,&bindings,&engine,None,true);
  assert_eq!(reply3["state"],"pending");
  let job3=effects.try_recv().unwrap();
  assert!(matches!(&job3.action,ActorControlAction::SendToObs(c) if c.capsule_id==cap1 && c.person_label=="slot-1"));
  // Duplicate window titles are rejected with window_titles_identical.
  f2.title="Stream One".into();
  windows.refresh_fixtures(&[f1,f2]).unwrap();
  assert_eq!(organizer_obs_capsules(&runtime,&windows,&book,None),Err("window_titles_identical"));
 }
 #[test]fn native_capsule_limit_is_twelve_and_twelve_fit_the_projection(){
  let executor=tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();executor.block_on(async{
   let temp=Temporary::new();let journal=Journal::open(&temp.0.join("authority.journal"),"synthetic",Clock(77)).unwrap();let (sender,_effects)=tokio::sync::mpsc::channel(25);let bindings=Arc::new(std::sync::RwLock::new(Vec::new()));
   let mut runtime=ControlRuntime::open(journal,Clock(77),ControlCommandSender::new(sender,bindings.clone()),&[temp.0.clone()]).unwrap();let mut organizer=RuntimeOrganizer::new(Clock(77));let mut windows=WindowRegistry::new(Clock(77));let mut adapter=NativeAdapter::new(Clock(77));let mut book=PairingBook::new();let engine=RuntimeEffects::<RecordingSource>::new(MAX_CAPSULES,24).unwrap();let mut services=ServiceClient::packaged(temp.0.clone(),77);let mut ffmpeg=None;let mut offset=0;
   for index in 0..=MAX_CAPSULES{let request=OperatorRequest{schema:"cafe.operator.v1".into(),kind:"operator_intent".into(),request_id:format!("create-{index}"),authority_epoch:Clock(77),expected_revision:runtime.revision(),body:OperatorBody::CreateCapsule{capsule_id:format!("synthetic-{index}-{}","x".repeat(80)),person_label:"S".repeat(128)}};
    let reply=mutate(request,&mut runtime,&mut windows,&mut adapter,&mut book,&bindings,&mut ffmpeg,&mut offset,&mut services,&mut organizer,&engine);
    assert_eq!(reply["state"],if index<MAX_CAPSULES{"committed"}else{"rejected"});
   }
   assert_eq!(runtime.capsule_snapshot().capsules.len(),12);
   let view=snapshot(&runtime,&windows,&json!({}),false,&engine,0,&services,&book,&organizer,&mut adapter);
   assert_eq!(view["state"]["runtime"],"authority_ready");assert_eq!(runtime.capsule_snapshot().capsules.len(),12);services.close().await;
  });
 }
 fn fixture(width:u32)->crate::windows_window_registry::FixtureWindow{crate::windows_window_registry::FixtureWindow{slot:"synthetic-slot".into(),incarnation:1,process_id:77,process_birth:1,executable:"Discord.exe".into(),title:"Synthetic call".into(),class:"Synthetic".into(),rect:crate::windows_window_registry::Rect{x:0,y:0,width,height:180},visible:true,minimized:false,layout:"synthetic".into()}}
 #[test]fn synthetic_anchor_is_durable_idempotent_and_repairs_only_when_idle(){
  let temp=Temporary::new();let journal=Journal::open(&temp.0.join("authority.journal"),"synthetic",Clock(77)).unwrap();let (sender,mut effects)=tokio::sync::mpsc::channel(25);let bindings=Arc::new(std::sync::RwLock::new(Vec::new()));
  let mut runtime=ControlRuntime::open(journal,Clock(77),ControlCommandSender::new(sender,bindings.clone()),&[temp.0.clone()]).unwrap();let mut windows=WindowRegistry::new(Clock(77));let mut book=PairingBook::new();let mut reservations=BTreeMap::new();let view=windows.refresh_fixtures(&[fixture(320)]).unwrap().remove(0);
  assert_eq!(anchor_observed(&mut runtime,&windows,&mut book,&bindings,&view,"organizer.auto.unmeasured",&[],&mut reservations),Err("window_unavailable"));
  let id=anchor_classified(&mut runtime,&windows,&mut book,&bindings,&view,"organizer.auto.1",&[],&mut reservations,DiscordWindowClass::Popout).unwrap();
  assert_eq!(runtime.capsule_snapshot().capsules.len(),1);assert_eq!(current_bindings(&runtime,&book).len(),1);let revision=runtime.revision();
  assert_eq!(anchor_classified(&mut runtime,&windows,&mut book,&bindings,&view,"organizer.auto.2",&[],&mut reservations,DiscordWindowClass::Popout).unwrap(),id);assert_eq!(runtime.revision(),revision);
  let resized=windows.refresh_fixtures(&[fixture(640)]).unwrap().remove(0);
  assert_eq!(anchor_classified(&mut runtime,&windows,&mut book,&bindings,&resized,"organizer.auto.3",&[id.clone()],&mut reservations,DiscordWindowClass::Popout),Err("already_recording"));assert_eq!(runtime.revision(),revision);
  anchor_classified(&mut runtime,&windows,&mut book,&bindings,&resized,"organizer.auto.4",&[],&mut reservations,DiscordWindowClass::Popout).unwrap();
  let current=current_bindings(&runtime,&book);assert_eq!(current.len(),1);assert_eq!(current[0].crop,Crop{x:0,y:0,width:640,height:180});assert_eq!(runtime.capsule_snapshot().capsules.len(),1);assert!(effects.try_recv().is_err());
  assert!(runtime.journal_for_media_actor().records().iter().all(|record|record.kind=="control"&&record.payload["request_id"].as_str().unwrap().starts_with("organizer-")));
 }
 #[test]fn whole_window_binding_follows_resize_unless_recording(){
  let temp=Temporary::new();let journal=Journal::open(&temp.0.join("authority.journal"),"synthetic",Clock(77)).unwrap();let (sender,_effects)=tokio::sync::mpsc::channel(25);let bindings=Arc::new(std::sync::RwLock::new(Vec::new()));
  let mut runtime=ControlRuntime::open(journal,Clock(77),ControlCommandSender::new(sender,bindings.clone()),&[temp.0.clone()]).unwrap();let mut windows=WindowRegistry::new(Clock(77));let mut book=PairingBook::new();let mut reservations=BTreeMap::new();let view=windows.refresh_fixtures(&[fixture(320)]).unwrap().remove(0);
  let id=anchor_classified(&mut runtime,&windows,&mut book,&bindings,&view,"organizer.auto.1",&[],&mut reservations,DiscordWindowClass::Popout).unwrap();
  windows.refresh_fixtures(&[fixture(640)]).unwrap();
  assert!(!book.follow_window_resize(&windows,&[id].into_iter().collect()));assert_eq!(book.bindings()[0].crop.width,320);
  assert!(book.follow_window_resize(&windows,&Default::default()));assert_eq!(book.bindings()[0].crop,Crop{x:0,y:0,width:640,height:180});
  assert!(!book.follow_window_resize(&windows,&Default::default()));
 }
 #[test]fn closed_window_capsule_is_removed_durably_and_forgets_its_binding(){
  let temp=Temporary::new();let path=temp.0.join("authority.journal");let journal=Journal::open(&path,"synthetic",Clock(77)).unwrap();let (sender,_effects)=tokio::sync::mpsc::channel(25);let bindings=Arc::new(std::sync::RwLock::new(Vec::new()));
  let mut runtime=ControlRuntime::open(journal,Clock(77),ControlCommandSender::new(sender,bindings.clone()),&[temp.0.clone()]).unwrap();let mut windows=WindowRegistry::new(Clock(77));let mut book=PairingBook::new();let mut reservations=BTreeMap::new();let view=windows.refresh_fixtures(&[fixture(320)]).unwrap().remove(0);
  let id=anchor_classified(&mut runtime,&windows,&mut book,&bindings,&view,"organizer.auto.1",&[],&mut reservations,DiscordWindowClass::Popout).unwrap();
  windows.refresh_fixtures(&[]).unwrap();
  organizer_structure(&mut runtime,"remove-closed",Command::RemoveCapsule{capsule_id:id.clone()}).unwrap();book.remove_capsule(&id);
  assert!(!runtime.capsule_snapshot().capsules.contains_key(&id));assert!(book.bindings().is_empty());
  assert!(organizer_structure(&mut runtime,"remove-again",Command::RemoveCapsule{capsule_id:id.clone()}).is_err()||!runtime.capsule_snapshot().capsules.contains_key(&id));
 }
 #[test]fn failed_pairing_retries_reserved_capsule_without_creating_another(){
  let temp=Temporary::new();let journal=Journal::open(&temp.0.join("authority.journal"),"synthetic",Clock(77)).unwrap();let (sender,_effects)=tokio::sync::mpsc::channel(25);let bindings=Arc::new(std::sync::RwLock::new(Vec::new()));
  let mut runtime=ControlRuntime::open(journal,Clock(77),ControlCommandSender::new(sender,bindings.clone()),&[temp.0.clone()]).unwrap();let mut windows=WindowRegistry::new(Clock(77));let view=windows.refresh_fixtures(&[fixture(320)]).unwrap().remove(0);let mut book=PairingBook::new();let mut reservations=BTreeMap::new();
  organizer_structure(&mut runtime,"organizer.seed",Command::CreateCapsule{capsule_id:"synthetic-seed".into(),person_label:"seed".into(),generation:Clock(u64::MAX-1)}).unwrap();
  for index in 0..2{assert_eq!(anchor_classified(&mut runtime,&windows,&mut book,&bindings,&view,&format!("organizer.auto.{index}"),&[],&mut reservations,DiscordWindowClass::Popout),Err("unavailable"));assert_eq!(runtime.capsule_snapshot().capsules.len(),2);assert_eq!(reservations.len(),1);}
 }
}

#[cfg(test)]mod pairing_diagnostic_tests {
 use super::pair_rejection_code;
 use crate::capsule_pairing::PairError;
 use crate::windows_window_registry::WindowError;
 #[test]fn pairing_rejections_have_distinct_static_categories(){
  assert_eq!(pair_rejection_code(&PairError::Window(WindowError::Unavailable)),"pair_window_unavailable");
  assert_eq!(pair_rejection_code(&PairError::Window(WindowError::AmbiguousDescriptor)),"window_titles_identical");
  assert_eq!(pair_rejection_code(&PairError::InvalidCrop),"pair_crop_invalid");
  assert_eq!(pair_rejection_code(&PairError::StaleCrop),"pair_crop_stale");
 }
 #[test]fn pairing_authority_error_never_exposes_its_payload(){
  assert_eq!(pair_rejection_code(&PairError::AuthorityRejected("SYNTHETIC_SECRET_CANARY".into())),"pair_authority_rejected");
 }
}

// Public diagnostic values are closed literals, never child output or paths.
fn recording_state_reason(code:&str)->&'static str{
 match code{
  "hardware_encoder_blocked"|"ffmpeg_audit_blocked"|"recorder_start_failed"=>"encoder_unavailable",
  "capture_first_frame_timeout"|"capture_not_healthy"|"capture_minimized"|"capture_frozen"|"capture_device_lost"|"capture_resize_required"|"capture_backpressure"|"capture_border_unavailable"|"capture_source_failed"|"capture_clock_regression"|"crop_resource_bound"|"observed_crop_outside_frame"|"observed_crop_overflow"|"crop_pool_exhausted"=>"capture_start_unavailable",
  "window_reconfirmation_required"|"window_layout_changed"|"capture_closed"|"capture_generation_mismatch"=>"window_unavailable",
  other=>crate::runtime_effects::recording_reason(other),
 }
}
fn recording_failure_code(code: &str) -> &'static str {
    match code {
        "ffmpeg_audit_blocked" => "recording_ffmpeg_not_approved",
        "hardware_encoder_blocked" => "recording_hardware_preflight_blocked",
        "capture_start_unavailable" => "recording_capture_unavailable",
        "capture_first_frame_timeout" => "recording_first_frame_timeout",
        "capture_not_healthy" => "recording_capture_unhealthy",
        "capture_minimized" => "recording_capture_minimized",
        "capture_frozen" => "recording_capture_frozen",
        "capture_closed" => "recording_capture_closed",
        "capture_device_lost" => "recording_capture_device_lost",
        "capture_resize_required" => "recording_capture_resize_required",
        "capture_generation_mismatch" => "recording_capture_generation_mismatch",
        "capture_backpressure" => "recording_capture_backpressure",
        "capture_border_unavailable" => "recording_capture_border_unavailable",
        "capture_source_failed" => "recording_capture_source_failed",
        "capture_clock_regression" => "recording_capture_clock_regression",
        "window_layout_changed" => "recording_binding_changed",
        "crop_resource_bound" | "observed_crop_outside_frame" | "observed_crop_overflow" | "crop_pool_exhausted" => "recording_crop_invalid",
        "recorder_start_failed" => "recording_recorder_start_failed",
        _ => "recording_setup_failed",
    }
}
#[cfg(test)] mod recording_diagnostic_tests {
    use super::recording_failure_code;
    #[test] fn approved_recording_failures_have_static_categories() {
        assert_eq!(recording_failure_code("ffmpeg_audit_blocked"), "recording_ffmpeg_not_approved");
        assert_eq!(recording_failure_code("hardware_encoder_blocked"), "recording_hardware_preflight_blocked");
        assert_eq!(recording_failure_code("capture_first_frame_timeout"), "recording_first_frame_timeout");
        assert_eq!(recording_failure_code("observed_crop_outside_frame"), "recording_crop_invalid");
    }
    #[test] fn capture_state_failures_have_closed_public_categories() {
        assert_eq!(recording_failure_code("capture_not_healthy"), "recording_capture_unhealthy");
        for (internal,public) in [
            ("capture_minimized","recording_capture_minimized"),
            ("capture_frozen","recording_capture_frozen"),
            ("capture_closed","recording_capture_closed"),
            ("capture_device_lost","recording_capture_device_lost"),
            ("capture_resize_required","recording_capture_resize_required"),
            ("capture_generation_mismatch","recording_capture_generation_mismatch"),
            ("capture_backpressure","recording_capture_backpressure"),
            ("capture_border_unavailable","recording_capture_border_unavailable"),
            ("capture_source_failed","recording_capture_source_failed"),
            ("capture_clock_regression","recording_capture_clock_regression"),
        ] { assert_eq!(recording_failure_code(internal),public); }
    }
    #[test] fn unknown_recording_errors_never_escape() {
        for code in ["SYNTHETIC_SECRET_CANARY", "capture_SYNTHETIC_SECRET_CANARY", "capture_source_failed:SYNTHETIC_SECRET_CANARY"] {
            assert_eq!(recording_failure_code(code), "recording_setup_failed");
        }
    }
}

fn startup_ffmpeg(path: Option<PathBuf>, sha256: Option<String>, blocked: bool) -> (Option<ExternalFfmpeg>, &'static str, &'static str) {
    if path.is_none() && sha256.is_none() && !blocked { return (None, "observed", "authority_started"); }
    if !blocked { if let (Some(path), Some(digest)) = (path, sha256) {
        if matches!(crate::ffmpeg_recorder::EncoderUse::operator_selected(digest.clone()), crate::ffmpeg_recorder::EncoderUse::OperatorApprovedExternalTest { .. }) {
            if let Ok(selected) = ExternalFfmpeg::select(&path, &digest) { return (Some(selected), "configured", "hardware_preflight_required"); }
        }
    }}
    (None, "blocked", "ffmpeg_startup_selection_blocked")
}
#[cfg(test)] mod startup_ffmpeg_tests {
    use super::startup_ffmpeg;
    use std::path::PathBuf;
    #[test] fn no_default_preserves_manual_setup() {
        let (selected,state,code) = startup_ffmpeg(None,None,false);
        assert!(selected.is_none()); assert_eq!((state,code),("observed","authority_started"));
    }
    #[test] fn incomplete_or_unapproved_defaults_never_configure() {
        for (path,pin,blocked) in [(None,None,true),(Some(PathBuf::from("synthetic.exe")),None,false),(None,Some("f".repeat(64)),false),(Some(PathBuf::from("synthetic.exe")),Some("f".repeat(64)),false)] {
            let (selected,state,code) = startup_ffmpeg(path,pin,blocked);
            assert!(selected.is_none()); assert_eq!((state,code),("blocked","ffmpeg_startup_selection_blocked"));
        }
    }
}
