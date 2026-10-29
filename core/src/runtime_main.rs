//! Single authority owner. Native effects remain explicitly blocked until configured.
use std::{path::PathBuf,sync::{Arc,Mutex,Condvar},io::{Read,Write},time::{Duration,Instant}};
use serde_json::{json,Value};
use crate::{capsule_registry::{Clock,Command,Envelope,SCHEMA},authority_journal::Journal,authority_auth::BrokerAuthority,control_intents::{ControlRuntime,ControlCommandSender,ControlActorClient,ControlAction,ActorControlAction,ActorCompletion},operator_protocol::{decode_operator,OperatorBody,OperatorRequest,MAX_OPERATOR_BYTES},runtime_storage::RuntimeStorage,windows_window_registry::{WindowRegistry,NativeAdapter},capsule_pairing::{PairingBook,PairRequest,Crop},runtime_recording::{RecordingSource,ExternalFfmpeg},runtime_effects::RuntimeEffects,runtime_service_owner::{ServiceClient,ServiceKey}};
struct Projection {pending:Mutex<Option<Vec<u8>>>,ready:Condvar}
impl Projection {
 fn publish(&self,value:Value){let bytes=serde_json::to_vec(&value).expect("owned projection serialization");if bytes.len()>4096{return;}if let Ok(mut pending)=self.pending.lock(){*pending=Some(bytes);self.ready.notify_one();}}
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
fn snapshot(runtime:&ControlRuntime<ControlCommandSender>,windows:&WindowRegistry,last:&Value,configured:bool,engine:&RuntimeEffects<RecordingSource>,window_offset:u32,services:&ServiceClient)->Value{
 let (page,total,next)=window_page(windows,window_offset);
 let mut state=json!({"runtime":"authority_ready","platform":if cfg!(windows){"windows"}else{"unsupported"},"recording_available":false,"recording_configured":configured,"recordings":engine.status().iter().map(|capsule|json!({"capsule_id":capsule.capsule_id,"state":format!("{:?}",capsule.state),"feeds":capsule.feeds.iter().map(|feed|json!({"feed_id":feed.feed_id,"state":format!("{:?}",feed.state),"written_frames":feed.counters.written_frames.to_string(),"backpressure":feed.counters.backpressure.to_string()})).collect::<Vec<_>>()})).collect::<Vec<_>>(),"obs_available":services.obs_ready(),"obs_configurable":services.configurable(),"chat_available":services.chat_ready(),"chat_configurable":services.configurable()&&!services.chat_running(),"services":services.projection(),"mcp_control_available":false,"capsules":runtime.capsule_snapshot().capsules,"windows":page,"window_page":{"offset":window_offset,"next_offset":next,"total":total},"folder_configured":runtime.folder_configured(),"last_result":last});
 // Never truncate identifiers or descriptors. Oversized projections fail visibly;
 // durable recording state is not reset or represented as a successful empty set.
 if serde_json::to_vec(&state).map_or(true,|bytes|bytes.len()>3500){state=json!({"runtime":"projection_unavailable","code":"projection_budget_exceeded"});}
 json!({"kind":"snapshot","authority_epoch":runtime.capsule_snapshot().authority_epoch,"revision":runtime.revision(),"state":state})
}
fn mutate(request:OperatorRequest,runtime:&mut ControlRuntime<ControlCommandSender>,windows:&mut WindowRegistry,adapter:&mut NativeAdapter,book:&mut PairingBook,bindings:&Arc<std::sync::RwLock<Vec<crate::capsule_pairing::Binding>>>,ffmpeg:&mut Option<ExternalFfmpeg>,window_offset:&mut u32,services:&mut ServiceClient)->Value{
 let epoch=runtime.capsule_snapshot().authority_epoch;
 if request.authority_epoch!=epoch{return result("rejected","old_authority_epoch");}
 let query=matches!(&request.body,OperatorBody::Snapshot{}|OperatorBody::ListWindows{..});
 let effect=matches!(&request.body,OperatorBody::SetFolder{..}|OperatorBody::StartRecording{..}|OperatorBody::StopRecording{..}|OperatorBody::SendToObs{..});
 if !query&&!effect&&request.expected_revision!=runtime.revision(){return result("rejected","stale_revision");}
 let state=runtime.capsule_snapshot();
 if !query&&(state.seen_requests.contains(&request.request_id)||runtime.known_control_request(&request.request_id)){return result("rejected","request_id_conflict");}
 match request.body{
  OperatorBody::Snapshot{}=>result("observed","snapshot"),
  OperatorBody::ListWindows{offset}=>match adapter.enumerate(windows){Ok(_)=>{let requested=offset.unwrap_or(0);if requested as usize>windows.snapshots().len(){*window_offset=0;result("rejected","window_page_invalidated")}else{*window_offset=requested;result("observed","windows_refreshed")}},Err(_)=>result("blocked","native_enumeration_unavailable")},
  OperatorBody::CreateCapsule{capsule_id,person_label}=>{
   if runtime.known_control_request(&request.request_id)||state.capsules.len()>=2{return result("rejected","request_or_capacity_conflict");}
   let Some(next)=state.highest_generation.0.checked_add(1) else{return result("blocked","generation_exhausted");};
   let envelope=Envelope{schema:SCHEMA.into(),kind:"intent".into(),request_id:request.request_id.clone(),authority_epoch:epoch,expected_revision:state.revision,body:Command::CreateCapsule{capsule_id,person_label,generation:Clock(next)}};
   match runtime.journal_for_media_actor().append_control(&request.request_id,envelope){Ok(_)=>result("committed","capsule_created"),Err(_)=>result("rejected","capsule_not_durable")}
  },
  OperatorBody::PairWindow{capsule_id,person_label,feed_id,role,window_token,window_generation,layout_revision,crop}=>{
   if runtime.known_control_request(&request.request_id){return result("rejected","request_id_conflict");}
   let matching=windows.snapshots().into_iter().filter(|view|serde_json::to_value(&view.handle).ok()==Some(json!(window_token))).collect::<Vec<_>>();
   if matching.len()!=1{return result("rejected","window_token_unknown_or_ambiguous");}
   let request_pair=PairRequest{request_id:request.request_id.clone(),authority_epoch:epoch,expected_revision:state.revision,capsule_id,person_label,feed_id,role,window:matching[0].handle.clone(),window_generation,layout_revision,crop:Crop{x:crop.x,y:crop.y,width:crop.width,height:crop.height}};
   let proposal=match book.propose(&state,windows,request_pair){Ok(value)=>value,Err(_)=>return result("rejected","pair_proposal_rejected")};
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
async fn execute_effect(job:&crate::control_intents::ActorControlJob,authority:&mut ControlRuntime<ControlCommandSender>,windows:&mut WindowRegistry,adapter:&mut NativeAdapter,book:&PairingBook,engine:&mut RuntimeEffects<RecordingSource>,ffmpeg:Option<&ExternalFfmpeg>,services:&mut ServiceClient)->Option<ActorCompletion> {
 let interrupted=Some(ActorCompletion::Interrupted);
 if matches!(&job.action,ActorControlAction::SendToObs(_))&&adapter.enumerate(windows).is_err(){return interrupted;}
 if job.authority_epoch!=authority.capsule_snapshot().authority_epoch{return interrupted;}
 if let ActorControlAction::SetFolder(folder)=&job.action{return if folder.revalidate().is_ok(){Some(ActorCompletion::Applied)}else{interrupted};}
 let current=authority.capsule_snapshot();
 let capsule=match &job.action{ActorControlAction::StartRecording{capsule,..}|ActorControlAction::StopRecording(capsule)|ActorControlAction::SendToObs(capsule)=>capsule,_=>return interrupted};
 let Some(actual)=current.capsules.get(&capsule.capsule_id) else{return interrupted;};
 if !job.matches_current_capsule(current.authority_epoch,actual)||!job.matches_current_bindings(&book.bindings())||job.window_bindings.len()!=actual.feeds.len(){return interrupted;}
 for binding in &job.window_bindings {let Ok(view)=windows.resolve(&binding.window,binding.window_generation) else{return interrupted;};if view.layout_revision!=binding.layout_revision{return interrupted;}}
 match &job.action{
  ActorControlAction::StartRecording{capsule,folder}=>{
   if engine.status().iter().any(|live|live.capsule_id==capsule.capsule_id){return interrupted;}
   let Some(approval)=ffmpeg else{return interrupted;};let Ok(root)=folder.revalidate() else{return interrupted;};
   // Finish all slow hardware trials before starting any native capture.
   let mut prepared=Vec::new();for (index,binding) in job.window_bindings.iter().enumerate(){
    let media_key=format!("rec-{}-{index}",crate::authority_journal::sha256_hex(job.effect_id.as_bytes()));
    match crate::runtime_recording::prepare_feed(approval,binding,&media_key,root).await{Ok(value)=>prepared.push((binding,media_key,value)),Err(_)=>return interrupted,}
   }
   let mut feeds=Vec::new();for (binding,media_key,approved) in prepared{
    match crate::runtime_recording::build_feed(approved,binding,&media_key,windows,adapter,root,authority.journal_for_media_actor()).await{Ok(pipeline)=>feeds.push((binding.feed_id.clone(),pipeline)),Err(_)=>{for (_,pipeline) in &mut feeds{let _=pipeline.abort().await;}return interrupted;}}
   }
   if engine.attach_capsule(capsule.capsule_id.clone(),feeds).await.is_ok(){Some(ActorCompletion::Applied)}else{interrupted}
  },
  ActorControlAction::StopRecording(capsule)=>if engine.stop_capsule(&capsule.capsule_id,authority.journal_for_media_actor()).await.is_ok(){Some(ActorCompletion::Applied)}else{interrupted},
  ActorControlAction::SendToObs(capsule)=>{
   if !services.obs_ready(){return interrupted;}
   let observed=windows.snapshots();let mut feeds=serde_json::Map::new();let mut crops=serde_json::Map::new();
   for binding in &job.window_bindings {
    let Ok(view)=windows.resolve(&binding.window,binding.window_generation) else{return interrupted;};
    if observed.iter().filter(|v|v.visible&&!v.minimized&&v.title==view.title&&v.class==view.class&&v.executable==view.executable).count()!=1{return interrupted;}
    let role=match binding.role{crate::capsule_registry::Role::Game=>"game",crate::capsule_registry::Role::Camera=>"camera"};
    if feeds.insert(role.into(),json!(format!("{}:{}:{}",view.title,view.class,view.executable))).is_some(){return interrupted;}
    crops.insert(role.into(),json!({"x":binding.crop.x,"y":binding.crop.y,"width":binding.crop.width,"height":binding.crop.height}));
   }
   let body=json!({"op":"send_capsule","capsule_id":capsule.capsule_id,"feeds":feeds,"crops":crops});
   if crate::authority_auth::validate_authority_frame(&crate::authority_auth::make_frame("service_request",job.authority_epoch.0,body.clone()),job.authority_epoch.0).is_err(){return interrupted;}
   let key=ServiceKey::Effect(job.effect_id.clone());let Ok(permit)=services.reserve(&key)else{return interrupted;};services.submit(permit,key,body);None
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
  let (effects_tx,mut effects_rx)=tokio::sync::mpsc::channel(8);let bindings=Arc::new(std::sync::RwLock::new(Vec::new()));
  let sender=ControlCommandSender::new(effects_tx,bindings.clone());let mut authority=ControlRuntime::open(journal,storage.epoch,sender,&[storage.recordings.clone()])?;
  let (requests_tx,mut requests_rx)=tokio::sync::mpsc::channel(8);let client=ControlActorClient::new(requests_tx,Duration::from_secs(4))?;
  let broker=BrokerAuthority::start_control(&storage.session.join("broker"),"127.0.0.1",storage.epoch.0,Box::new(client)).await.map_err(|_|"runtime_broker_unavailable")?;
  let mut windows=WindowRegistry::new(storage.epoch);let mut adapter=NativeAdapter::new(storage.epoch);let mut book=PairingBook::new();let mut engine=RuntimeEffects::<RecordingSource>::new(2,4).map_err(|_|"runtime_effect_budget_invalid")?;
  let mut services=ServiceClient::packaged(storage.chat.clone(),storage.epoch.0);let mut ffmpeg=None;let mut window_offset=0;let input=input();let output=output();let mut last=result("observed","authority_started");output.publish(snapshot(&authority,&windows,&last,ffmpeg.is_some(),&engine,window_offset,&services));
  loop{
   let mut changed=false;for _ in 0..4{match input.try_recv(){Ok(bytes)=>{last=match decode_operator(&bytes){Ok(request)=>mutate(request,&mut authority,&mut windows,&mut adapter,&mut book,&bindings,&mut ffmpeg,&mut window_offset,&mut services),Err(code)=>result("rejected",code)};changed=true;},Err(std::sync::mpsc::TryRecvError::Empty)=>break,Err(std::sync::mpsc::TryRecvError::Disconnected)=>{let _=engine.abort_all().await;services.close().await;drop(broker);return Ok(());}}}
   for _ in 0..8{match requests_rx.try_recv(){Ok(request)=>{request.execute_on(&mut authority);changed=true;},Err(_)=>break}}
   for _ in 0..8{match effects_rx.try_recv(){Ok(job)=>{let outcome=execute_effect(&job,&mut authority,&mut windows,&mut adapter,&book,&mut engine,ffmpeg.as_ref(),&mut services).await;if let Some(outcome)=outcome{let reply=authority.complete_from_actor(&job.effect_id,outcome);last=serde_json::to_value(reply).unwrap_or_else(|_|result("blocked","reply_encoding_failed"));}changed=true;},Err(_)=>break}}
   let (completed,service_changed)=services.drain();changed|=service_changed;
   for (key,applied) in completed {match key{
    ServiceKey::Configuration(_)=>last=if applied{result("configured","service_configured")}else{result("blocked","service_operation_failed")},
    ServiceKey::Effect(effect)=>{let outcome=if applied{ActorCompletion::Applied}else{ActorCompletion::Interrupted};last=serde_json::to_value(authority.complete_from_actor(&effect,outcome)).unwrap_or_else(|_|result("blocked","reply_encoding_failed"));},
   }changed=true;}
   if engine.tick_all(&windows,Instant::now()).await.is_err(){last=result("interrupted","recording_effect_failed");changed=true;}
   if changed{output.publish(snapshot(&authority,&windows,&last,ffmpeg.is_some(),&engine,window_offset,&services));}tokio::time::sleep(Duration::from_millis(5)).await;
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
