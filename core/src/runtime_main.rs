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
 root:Option<(String,ApprovedFolder)>,
 effects:BTreeMap<String,PlanEffect>,
 failures:BTreeMap<String,&'static str>,
 reservations:BTreeMap<(crate::windows_window_registry::WindowHandle,u64),String>,
}
impl RuntimeOrganizer {
 fn new(epoch:Clock)->Self{Self{plans:Organizer::new(epoch),root:None,effects:BTreeMap::new(),failures:BTreeMap::new(),reservations:BTreeMap::new()}}
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
 }})();
 let response=match data{Ok(data)=>json!({"request_id":request.request_id,"error":null,"data":data}),Err(code)=>json!({"request_id":request.request_id,"error":organizer_error(code),"data":null})};
 if serde_json::to_vec(&response).map_or(true,|bytes|bytes.len()>16_000){json!({"request_id":request.request_id,"error":"unavailable","data":null})}else{response}
}

/// Ordinary structural intents retain the organizer actor in their closed
/// request/event namespace. Confirmed controls use SHA256("organizer") in the
/// existing journal identity field; no new journal schema is invented here.
fn organizer_structure(runtime:&mut ControlRuntime<ControlCommandSender>,id:&str,body:Command)->Result<(),&'static str>{
 let id=id.replace('.',"-"); // Structural envelopes allow only alphanumerics, '-' and '_'.
 let state=runtime.capsule_snapshot();let envelope=Envelope{schema:SCHEMA.into(),kind:"intent".into(),request_id:id.clone(),authority_epoch:state.authority_epoch,expected_revision:state.revision,body};
 runtime.journal_for_media_actor().append_control(&id,envelope).map(|_|()).map_err(|_|"disk_write_failed")
}
fn pair_observed(runtime:&mut ControlRuntime<ControlCommandSender>,windows:&WindowRegistry,book:&mut PairingBook,bindings:&Arc<std::sync::RwLock<Vec<crate::capsule_pairing::Binding>>>,view:&crate::windows_window_registry::WindowView,capsule_id:&str,id:&str)->Result<(),&'static str>{
 let id=id.replace('.',"-");
 let state=runtime.capsule_snapshot();let capsule=state.capsules.get(capsule_id).ok_or("unavailable")?;
 let next=state.highest_generation.0.checked_add(1).ok_or("unavailable")?;
 let request=PairRequest{request_id:id.clone(),authority_epoch:state.authority_epoch,expected_revision:state.revision,capsule_id:capsule_id.into(),person_label:capsule.person_label.clone(),feed_id:format!("organizer-feed-{next}"),role:crate::capsule_registry::Role::Game,window:view.handle.clone(),window_generation:view.generation,layout_revision:view.layout_revision,crop:Crop{x:0,y:0,width:view.rect.width,height:view.rect.height}};
 let proposal=book.propose(&state,windows,request).map_err(|error|match error{crate::capsule_pairing::PairError::Window(crate::windows_window_registry::WindowError::AmbiguousDescriptor)=>"window_titles_identical",_=>"window_unavailable"})?;
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
   organizer_structure(runtime,&format!("{id}.remove"),Command::RemoveFeed{capsule_id:binding.capsule_id.clone(),feed_id:binding.feed_id,generation:binding.feed_generation})?;
   pair_observed(runtime,windows,book,bindings,&observed,&binding.capsule_id,&format!("{id}.pair"))?;
  }
  return Ok(binding.capsule_id);
 }
 if let Some(capsule_id)=reservations.get(&crate::organizer::identity(view)).cloned(){
  if busy.contains(&capsule_id){return Err("already_recording");}
  if state.capsules.get(&capsule_id).is_none_or(|capsule|!capsule.feeds.is_empty()){return Err("unavailable");}
  pair_observed(runtime,windows,book,bindings,&observed,&capsule_id,&format!("{id}.pair"))?;return Ok(capsule_id);
 }
 if state.capsules.len()>=MAX_CAPSULES{return Err("capsule_limit");}
 let slot=(1..=MAX_CAPSULES).find(|slot|!state.capsules.contains_key(&format!("organizer-{}-{slot}",state.authority_epoch.0))).ok_or("capsule_limit")?;
 let capsule_id=format!("organizer-{}-{slot}",state.authority_epoch.0);
 let generation=Clock(state.highest_generation.0.checked_add(1).ok_or("unavailable")?);
 organizer_structure(runtime,&format!("{id}.create"),Command::CreateCapsule{capsule_id:capsule_id.clone(),person_label:format!("slot-{slot}"),generation})?;
 reservations.insert(crate::organizer::identity(view),capsule_id.clone());
 pair_observed(runtime,windows,book,bindings,&observed,&capsule_id,&format!("{id}.pair"))?;Ok(capsule_id)
}
fn track_plan_control(organizer:&mut RuntimeOrganizer,runtime:&mut ControlRuntime<ControlCommandSender>,plan:&str,index:usize,capsule_id:String,kind:PlanEffectKind,window:Option<crate::windows_window_registry::WindowView>){
 let (suffix,action)=match kind{
  PlanEffectKind::Record=>("start",ControlAction::StartRecording(capsule_id.clone())),
  PlanEffectKind::Stop=>("stop",ControlAction::StopRecording(capsule_id.clone())),
  PlanEffectKind::Obs=>("obs",ControlAction::SendToObs(capsule_id.clone())),
 };
 let id=format!("organizer.{plan}.{index}.{suffix}");
 let reply=match kind{
  PlanEffectKind::Record|PlanEffectKind::Stop=>runtime.handle_organizer(&id,runtime.capsule_snapshot().authority_epoch,action),
  PlanEffectKind::Obs=>runtime.handle_operator(&id,runtime.capsule_snapshot().authority_epoch,runtime.revision(),action),
 };
 if reply.state==ControlState::Pending {
  organizer.effects.insert(format!("ctl.{}",crate::authority_journal::sha256_hex(id.as_bytes())),PlanEffect{plan_id:plan.into(),index,capsule_id,kind,window});
 }else{
  let code=match kind{PlanEffectKind::Obs=>"window_unavailable",_=>"unavailable"};
  organizer.result(plan,index,Some(capsule_id.clone()),code);
  if !matches!(kind,PlanEffectKind::Obs){organizer.failures.insert(capsule_id,"recording_interrupted");}
 }
}
fn confirm_organizer(plan_id:&str,runtime:&mut ControlRuntime<ControlCommandSender>,organizer:&mut RuntimeOrganizer,windows:&mut WindowRegistry,adapter:&mut NativeAdapter,book:&mut PairingBook,bindings:&Arc<std::sync::RwLock<Vec<crate::capsule_pairing::Binding>>>,engine:&RuntimeEffects<RecordingSource>,ffmpeg:Option<&ExternalFfmpeg>,obs_ready:bool)->Value{
 let plan=match organizer.plans.take_confirmed(plan_id,Instant::now()){Ok(plan)=>plan,Err(code)=>return result("rejected",code)};
 let root=organizer.root.take().filter(|(id,_)|*id==plan.plan_id).map(|(_,root)|root);
 let epoch_ok=plan.authority_epoch==runtime.capsule_snapshot().authority_epoch;
 match plan.kind{
  PlanKind::Recording{windows:chosen,folder_name}=>{
   let enumeration=epoch_ok&&adapter.enumerate(windows).is_ok();
   let validated=chosen.iter().map(|frozen|{if !enumeration{return Err("window_unavailable");}let view=resolve_frozen_window(windows,frozen)?;if classify_discord_window(&view)!=DiscordWindowClass::Popout{return Err("window_changed");}Ok(view)}).collect::<Vec<_>>();
   let preparation=if !validated.iter().any(Result::is_ok){Err("window_unavailable")}else if ffmpeg.is_none()||!cfg!(windows){Err("encoder_unavailable")}else{root.as_ref().ok_or("folder_not_authorized").and_then(|root|runtime.select_organizer_subfolder(&format!("organizer.{plan_id}.folder"),root,&folder_name))};
   let mut busy=engine.status().into_iter().filter(|row|row.state==crate::runtime_effects::CapsuleState::Running).map(|row|row.capsule_id).collect::<Vec<_>>();busy.extend(organizer.effects.values().map(|effect|effect.capsule_id.clone()));
   for (index,view) in validated.into_iter().enumerate(){
    let view=match view{Ok(view)=>view,Err(code)=>{organizer.result(plan_id,index,None,code);continue;}};
    if let Err(code)=preparation{organizer.result(plan_id,index,None,code);continue;}
    match anchor_observed(runtime,windows,book,bindings,&view,&format!("organizer.{plan_id}.{index}"),&busy,&mut organizer.reservations){
     Ok(capsule_id)=>{busy.push(capsule_id.clone());track_plan_control(organizer,runtime,plan_id,index,capsule_id,PlanEffectKind::Record,Some(view));},
     Err(code)=>organizer.result(plan_id,index,None,code),
    }
   }
  },
  PlanKind::StopAll{capsule_ids}=>{
   for (index,capsule_id) in capsule_ids.into_iter().enumerate(){if !epoch_ok{organizer.result(plan_id,index,Some(capsule_id),"unavailable");}else{track_plan_control(organizer,runtime,plan_id,index,capsule_id,PlanEffectKind::Stop,None);}}
  },
  PlanKind::SendToObs{capsules}=>{
   for (index,(capsule_id,_)) in capsules.into_iter().enumerate(){
    if !epoch_ok{organizer.result(plan_id,index,Some(capsule_id),"unavailable");}
    else if !obs_ready{organizer.result(plan_id,index,Some(capsule_id),"obs_not_ready");}
    else{track_plan_control(organizer,runtime,plan_id,index,capsule_id,PlanEffectKind::Obs,None);}
   }
  },
 }
 result("pending","organizer_plan_confirmed")
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
 let mut state=json!({"runtime":"authority_ready","platform":if cfg!(windows){"windows"}else{"unsupported"},"recording_available":false,"recording_configured":configured,"recordings":engine.status().iter().map(|capsule|json!({"capsule_id":capsule.capsule_id,"state":format!("{:?}",capsule.state),"feeds":capsule.feeds.iter().map(|feed|json!({"feed_id":feed.feed_id,"state":format!("{:?}",feed.state),"written_frames":feed.counters.written_frames.to_string(),"backpressure":feed.counters.backpressure.to_string()})).collect::<Vec<_>>()})).collect::<Vec<_>>(),"obs_available":services.obs_ready(),"obs_configurable":services.configurable(),"chat_available":services.chat_ready(),"chat_configurable":services.configurable()&&!services.chat_running(),"services":services.projection(),"mcp_control_available":false,"capsules":runtime.capsule_snapshot().capsules,"windows":page,"window_page":{"offset":window_offset,"next_offset":next,"total":total},"folder_configured":runtime.folder_configured(),"last_result":last});
 let bindings=current_bindings(runtime,book);
 let preview=bindings.iter().filter_map(|binding|windows.live_preview_source_id(&binding.window,binding.window_generation,binding.layout_revision,adapter).map(|source_id|json!({"capsule_id":binding.capsule_id,"feed_id":binding.feed_id,"source_id":source_id}))).collect::<Vec<_>>();
 state["preview_sources"]=json!(&preview);
 state["organizer_plan"]=organizer.plans.projection();state["auto_anchor"]=json!(organizer.plans.auto_anchor());
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
 let epoch=runtime.capsule_snapshot().authority_epoch;
 if request.authority_epoch!=epoch{return result("rejected","old_authority_epoch");}
 let query=matches!(&request.body,OperatorBody::Snapshot{}|OperatorBody::ListWindows{..});
 let effect=matches!(&request.body,OperatorBody::SetFolder{..}|OperatorBody::StartRecording{..}|OperatorBody::StopRecording{..}|OperatorBody::SendToObs{..});
 if !query&&!effect&&request.expected_revision!=runtime.revision(){return result("rejected","stale_revision");}
 let state=runtime.capsule_snapshot();
 if !query&&(state.seen_requests.contains(&request.request_id)||runtime.known_control_request(&request.request_id)){return result("rejected","request_id_conflict");}
 match request.body{
  OperatorBody::Snapshot{}=>result("observed","snapshot"),
  OperatorBody::OrganizerPlanConfirm{plan_id}=>confirm_organizer(&plan_id,runtime,organizer,windows,adapter,book,bindings,engine,ffmpeg.as_ref(),services.obs_ready()),
  OperatorBody::OrganizerPlanReject{plan_id}=>match organizer.plans.reject(&plan_id,Instant::now()){Ok(())=>{organizer.root=None;result("rejected","organizer_plan_rejected")},Err(code)=>result("rejected",code)},
  OperatorBody::SetAutoAnchor{enabled}=>{organizer.plans.set_auto_anchor(enabled);result("configured","auto_anchor_updated")},
  OperatorBody::ListWindows{offset}=>match adapter.enumerate(windows){Ok(_)=>{let requested=offset.unwrap_or(0);if requested as usize>windows.snapshots().len(){*window_offset=0;result("rejected","window_page_invalidated")}else{*window_offset=requested;result("observed","windows_refreshed")}},Err(_)=>result("blocked","native_enumeration_unavailable")},
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
  OperatorBody::StartRecording{capsule_id}=>{if !cfg!(windows)||ffmpeg.is_none(){return result("blocked","recording_runtime_not_configured");}serde_json::to_value(runtime.handle_operator(&request.request_id,epoch,request.expected_revision,ControlAction::StartRecording(capsule_id))).unwrap_or_else(|_|result("blocked","reply_encoding_failed"))},
  OperatorBody::StopRecording{capsule_id}=>serde_json::to_value(runtime.handle_operator(&request.request_id,epoch,request.expected_revision,ControlAction::StopRecording(capsule_id))).unwrap_or_else(|_|result("blocked","reply_encoding_failed")),
  OperatorBody::SendToObs{capsule_id}=>{if !services.obs_ready(){return result("blocked","obs_runtime_not_configured");}serde_json::to_value(runtime.handle_operator(&request.request_id,epoch,request.expected_revision,ControlAction::SendToObs(capsule_id))).unwrap_or_else(|_|result("blocked","reply_encoding_failed"))},
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
   if engine.status().iter().any(|live|live.capsule_id==capsule.capsule_id){return interrupted;}
   let Some(approval)=ffmpeg else{*failure=Some("encoder_unavailable");return interrupted;};let Ok(root)=folder.revalidate() else{*failure=Some("folder_not_authorized");return interrupted;};
   // Finish all slow hardware trials before starting any native capture.
   let mut prepared=Vec::new();for (index,binding) in job.window_bindings.iter().enumerate(){
    let media_key=format!("rec-{}-{index}",crate::authority_journal::sha256_hex(job.effect_id.as_bytes()));
    match with_recording_ticks(crate::runtime_recording::prepare_feed(approval,binding,&media_key,root),engine,windows).await{Ok(value)=>prepared.push((binding,media_key,value)),Err(code)=>{*failure=Some(recording_state_reason(code));return interrupted;},}
   }
   let mut feeds:Vec<(String,crate::capture_pipeline::CapturePipeline<RecordingSource>)>=Vec::new();for (binding,media_key,approved) in prepared{
    if adapter.enumerate(windows).is_err(){*failure=Some("window_unavailable");for (_,pipeline) in &mut feeds{let _=pipeline.abort().await;}return interrupted;}
    if let Some(frozen)=frozen_window{if let Err(code)=resolve_frozen_window(windows,frozen){*failure=Some(code);for (_,pipeline) in &mut feeds{let _=pipeline.abort().await;}return interrupted;}}
    match with_recording_ticks(crate::runtime_recording::build_feed(approved,binding,&media_key,windows,adapter,root,authority.journal_for_media_actor()),engine,windows).await{Ok(pipeline)=>feeds.push((binding.feed_id.clone(),pipeline)),Err(code)=>{*failure=Some(recording_state_reason(code));for (_,pipeline) in &mut feeds{let _=pipeline.abort().await;}return interrupted;}}
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
    crops.insert(role.into(),json!({"x":binding.crop.x,"y":binding.crop.y,"width":binding.crop.width,"height":binding.crop.height}));
   }
   let body=json!({"op":"send_capsule","capsule_id":capsule.capsule_id,"feeds":feeds,"crops":crops});
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
   for _ in 0..8{match requests_rx.try_recv(){Ok(request)=>{request.execute_with_organizer(&mut authority,|authority,value,peer|organizer_request(value,authority,&mut organizer,&mut windows,&mut adapter,&book,ffmpeg.as_ref(),&engine,services.obs_ready(),||peer.is_live()));changed=true;},Err(_)=>break}}
   // Revisit trusted input between setup jobs instead of awaiting a whole batch.
   for _ in 0..1{match effects_rx.try_recv(){Ok(job)=>{
    let frozen=organizer.effects.get(&job.effect_id).and_then(|effect|effect.window.clone());
    let mut failure=None;let outcome=execute_effect(&job,&mut authority,&mut windows,&mut adapter,&book,&mut engine,ffmpeg.as_ref(),&mut services,&mut failure,frozen.as_ref()).await;
    if let Some(outcome)=outcome{
     let reply=authority.complete_from_actor(&job.effect_id,outcome);let committed=reply.state==ControlState::Committed;
     let capsule=match &job.action{ActorControlAction::StartRecording{capsule,..}|ActorControlAction::StopRecording(capsule)=>Some(&capsule.capsule_id),_=>None};
     let code=failure.unwrap_or(if committed{"recording_effect_completed"}else{"recording_interrupted"});
     if let Some(capsule)=capsule{if committed{organizer.failures.remove(capsule);}else{organizer.failures.insert(capsule.clone(),crate::runtime_effects::recording_reason(code));}}
     if let Some(effect)=organizer.effects.remove(&job.effect_id){let state=if committed{match effect.kind{PlanEffectKind::Stop=>"stopped",PlanEffectKind::Obs=>"sent",PlanEffectKind::Record=>"recording"}}else{failure.unwrap_or("unavailable")};organizer.result(&effect.plan_id,effect.index,Some(effect.capsule_id),state);}
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
     if let Some(plan_effect)=organizer.effects.remove(&effect){
      let state=if committed{"sent"}else if !services.obs_ready(){"obs_not_ready"}else{"unavailable"};
      organizer.result(&plan_effect.plan_id,plan_effect.index,Some(plan_effect.capsule_id),state);
     }
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
  for op in ["organizer_plan_confirm","organizer_plan_reject","set_auto_anchor","set_folder","send_to_obs","configure_ffmpeg","configure_obs","configure_twitch"]{
   assert!(serde_json::from_value::<OrganizerRequest>(wire(json!({"op":op}))).is_err());
  }
  let mut forged=wire(json!({"op":"organizer_status"}));forged["actor"]=json!("operator");assert!(serde_json::from_value::<OrganizerRequest>(forged).is_err());
  assert!(serde_json::from_value::<OrganizerRequest>(wire(json!({"op":"propose_recording_plan","folder_name":"Session","confirmation_token":"synthetic"}))).is_err());
 }
 #[test]fn native_organizer_ops_require_exact_local_schema(){
  for body in [json!({"op":"organizer_plan_confirm","plan_id":"organizer-77-1"}),json!({"op":"organizer_plan_reject","plan_id":"organizer-77-1"}),json!({"op":"set_auto_anchor","enabled":true})]{
   let local=json!({"schema":"cafe.operator.v1","kind":"operator_intent","request_id":"synthetic-local","authority_epoch":"77","expected_revision":"0","body":body});
   assert!(decode_operator(&serde_json::to_vec(&local).unwrap()).is_ok());assert!(decode_operator(&serde_json::to_vec(&wire(body)).unwrap()).is_err());
  }
 }
 #[test]fn status_and_refused_remote_requests_have_no_durable_effect(){
  let temp=Temporary::new();let journal=Journal::open(&temp.0.join("authority.journal"),"synthetic",Clock(77)).unwrap();let (sender,mut effects)=tokio::sync::mpsc::channel(2);let shared=Arc::new(std::sync::RwLock::new(Vec::new()));
  let mut runtime=ControlRuntime::open(journal,Clock(77),ControlCommandSender::new(sender,shared),&[temp.0.clone()]).unwrap();let mut organizer=RuntimeOrganizer::new(Clock(77));let mut windows=WindowRegistry::new(Clock(77));let mut adapter=NativeAdapter::new(Clock(77));let book=PairingBook::new();let engine=RuntimeEffects::<RecordingSource>::new(MAX_CAPSULES,24).unwrap();
  let status=organizer_request(&wire(json!({"op":"organizer_status"})),&mut runtime,&mut organizer,&mut windows,&mut adapter,&book,None,&engine,false,||true);assert_eq!(status["data"]["capsule_count"],0);assert_eq!(status["data"]["encoder_ready"],false);assert_eq!(status["data"]["obs_ready"],false);assert_eq!(status["data"]["folder_root_set"],false);
  let proposal=organizer_request(&wire(json!({"op":"propose_recording_plan","folder_name":"Session"})),&mut runtime,&mut organizer,&mut windows,&mut adapter,&book,None,&engine,false,||true);assert_eq!(proposal["error"],"folder_root_required");
  let obs_refused=organizer_request(&wire(json!({"op":"propose_send_to_obs"})),&mut runtime,&mut organizer,&mut windows,&mut adapter,&book,None,&engine,false,||true);assert_eq!(obs_refused["error"],"obs_not_ready");assert!(organizer.plans.projection().is_null());
  assert_eq!(organizer_request(&wire(json!({"op":"organizer_status"})),&mut runtime,&mut organizer,&mut windows,&mut adapter,&book,None,&engine,false,||false)["error"],"unavailable");
  organizer.plans.propose_stop_all(vec!["synthetic".into()],Instant::now(),0).unwrap();
  let conflict=organizer_request(&wire(json!({"op":"propose_stop_all","capsule_ids":["absent"]})),&mut runtime,&mut organizer,&mut windows,&mut adapter,&book,None,&engine,false,||true);assert_eq!(conflict["error"],"plan_pending");
  let obs_conflict=organizer_request(&wire(json!({"op":"propose_send_to_obs"})),&mut runtime,&mut organizer,&mut windows,&mut adapter,&book,None,&engine,true,||true);assert_eq!(obs_conflict["error"],"plan_pending");
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
  assert!(matches!(&job1.action,ActorControlAction::SendToObs(c) if c.capsule_id==cap1));
  assert!(matches!(&job2.action,ActorControlAction::SendToObs(c) if c.capsule_id==cap2));
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
