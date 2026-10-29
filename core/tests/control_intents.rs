//! Real HMAC peers, independent expected control transitions, and durable real-file oracles.
#[cfg(windows)]
#[path="../src/windows_journal_policy.rs"] mod windows_journal_policy;
#[path="../src/capsule_registry.rs"] mod capsule_registry;
#[path="../src/authority_journal.rs"] mod authority_journal;
#[path="../src/journal_replay.rs"] mod journal_replay;
#[path="../src/authority_auth.rs"] mod authority_auth;
#[path="../src/windows_window_registry.rs"] mod windows_window_registry;
#[path="../src/capsule_pairing.rs"] mod capsule_pairing;
#[path="../src/control_intents.rs"] mod control_intents;
use authority_auth::{BrokerAuthority,read_frame,write_frame,hmac_sha256};
use authority_journal::{Journal,DurabilityFault};
use capsule_registry::Clock;
use control_intents::*;
use serde_json::{json,Value};
use std::path::{Path,PathBuf};
use std::sync::{Arc,Mutex,atomic::{AtomicUsize,Ordering}};
use std::time::Duration;
use std::io::Write;
static NEXT:AtomicUsize=AtomicUsize::new(0);
thread_local! { static DEFERRED_CLEANUP:std::cell::RefCell<Vec<PathBuf>>=const {std::cell::RefCell::new(Vec::new())}; }

struct Temporary { root:PathBuf }
impl Temporary {
    fn new()->Self {
        loop {
            let root=std::env::temp_dir().join(format!("cafe-control-test-{}-{}",std::process::id(),NEXT.fetch_add(1,Ordering::SeqCst)));
            match std::fs::create_dir(&root) {Ok(())=>return Self{root},Err(e) if e.kind()==std::io::ErrorKind::AlreadyExists=>{},Err(_)=>panic!("temporary directory unavailable")}
        }
    }
}
impl Drop for Temporary { fn drop(&mut self){
    if std::fs::remove_dir_all(&self.root).is_err(){DEFERRED_CLEANUP.with(|paths|paths.borrow_mut().push(self.root.clone()));}
} }

#[derive(Clone,Copy)]
enum Fault { None, EffectInterrupted, ResultFlush }
struct FileEffects { root:PathBuf, events:Arc<Mutex<Vec<String>>>, fault:Fault }
impl ControlEffects for FileEffects {
    fn dispatch(&mut self,command:ControlDispatch<'_>,journal:&mut Journal)->DispatchOutcome {
        if matches!(self.fault,Fault::EffectInterrupted) {return DispatchOutcome::Interrupted;}
        let label=match command {
            ControlDispatch::SetFolder{folder,effect_id,..}=>{assert!(folder.path().is_dir());assert!(effect_id.starts_with("ctl."));"set_folder"},
            ControlDispatch::StartRecording{capsule_id,folder,effect_id,..}=>{assert_eq!(capsule_id,"cap-one");assert!(folder.path().is_dir());assert!(effect_id.starts_with("ctl."));"start_recording"},
            ControlDispatch::StopRecording{capsule_id,effect_id,..}=>{assert_eq!(capsule_id,"cap-one");assert!(effect_id.starts_with("ctl."));"stop_recording"},
            ControlDispatch::SendToObs{capsule_id,effect_id,..}=>{assert_eq!(capsule_id,"cap-one");assert!(effect_id.starts_with("ctl."));"send_to_obs"},
        };
        let mut marker=std::fs::OpenOptions::new().create_new(true).write(true).open(self.root.join(label)).unwrap();
        marker.write_all(b"real synthetic fixture effect\n").unwrap();marker.sync_all().unwrap();
        self.events.lock().unwrap().push(label.into());
        if matches!(self.fault,Fault::ResultFlush) {journal.inject_durability_fault(DurabilityFault::File);}
        DispatchOutcome::Applied
    }
}
struct Fixture { runtime:Arc<Mutex<ControlRuntime<FileEffects>>>, events:Arc<Mutex<Vec<String>>>, directory:Temporary }
impl Fixture {
    fn new(fault:Fault,flush_intent:bool)->Self {
        let directory=Temporary::new();let root=&directory.root;
        std::fs::create_dir(root.join("recordings")).unwrap();
        let mut journal=Journal::open(&root.join("journal.bin"),"control-session",Clock(7)).unwrap();
        // Hand-authored capsule/feed input book; never generated from reducer output.
        for (index,body) in [json!({"op":"create_capsule","capsule_id":"cap-one","person_label":"Guest","generation":"1"}),json!({"op":"add_feed","capsule_id":"cap-one","feed_id":"game-one","role":"game","generation":"2"}),json!({"op":"add_feed","capsule_id":"cap-one","feed_id":"camera-one","role":"camera","generation":"3"})].into_iter().enumerate() {
            let input=json!({"schema":"cafe.control.v1","kind":"intent","request_id":format!("seed-{index}"),"authority_epoch":"7","expected_revision":index.to_string(),"body":body});
            journal.append_control(&format!("seed-event-{index}"),serde_json::from_value(input).unwrap()).unwrap();
        }
        if flush_intent {journal.inject_durability_fault(DurabilityFault::File);}
        let events=Arc::new(Mutex::new(Vec::new()));
        let effects=FileEffects{root:root.clone(),events:events.clone(),fault};
        let runtime=ControlRuntime::open(journal,Clock(7),effects,&[root.join("recordings")]).unwrap();
        Self{directory,events,runtime:Arc::new(Mutex::new(runtime))}
    }
    fn folder(&self)->PathBuf {self.directory.root.join("recordings")}
    fn approve(&self,id:&str,action:ControlAction)->Value {self.runtime.lock().unwrap().confirm_for_operator("broker",id,action,Duration::from_secs(10)).unwrap().into_private_wire()}
    async fn broker(&self)->(BrokerAuthority,Value) {
        let dir=self.directory.root.join("rendezvous");
        let broker=BrokerAuthority::start_control(&dir,"127.0.0.1",7,Box::new(ControlBroker(self.runtime.clone()))).await.unwrap();
        let bootstrap=private_bootstrap(&dir).await;
        (broker,bootstrap)
    }
    fn assert_no_effects(&self){assert!(self.events.lock().unwrap().is_empty());}
}
#[cfg(unix)]
async fn private_bootstrap(dir:&Path)->Value {
    // Keep the private canonical root for folder capabilities. Only the socket
    // address uses a held directory descriptor, matching the production bind.
    use std::os::fd::AsRawFd;
    let directory=std::fs::File::open(dir).unwrap();
    let address=format!("/proc/self/fd/{}/broker.sock",directory.as_raw_fd());
    let mut stream=tokio::net::UnixStream::connect(address).await.unwrap();
    read_frame(&mut stream).await.unwrap().unwrap()
}
#[cfg(windows)]
async fn private_bootstrap(dir:&Path)->Value {
    use tokio::net::windows::named_pipe::ClientOptions;
    let metadata:Value=serde_json::from_slice(&std::fs::read(dir.join("broker.pipe")).unwrap()).unwrap();
    let address=format!("{0}{0}.{0}pipe{0}{1}",'\\',metadata["pipe_name"].as_str().unwrap());
    let mut stream=ClientOptions::new().open(&address).unwrap();
    let result=read_frame(&mut stream).await.unwrap().unwrap();
    write_frame(&mut stream,&json!({"received":true})).await.unwrap();
    result
}
fn from_hex(text:&str)->Vec<u8>{(0..text.len()).step_by(2).map(|i|u8::from_str_radix(&text[i..i+2],16).unwrap()).collect()}
async fn authenticate(bootstrap:&Value,identity:&str,correct:bool)->tokio::net::TcpStream {
    let port=u16::try_from(bootstrap["port"].as_u64().unwrap()).unwrap();
    let mut stream=tokio::net::TcpStream::connect(("127.0.0.1",port)).await.unwrap();
    let challenge=read_frame(&mut stream).await.unwrap().unwrap();
    let nonce=challenge["nonce"].as_str().unwrap();
    let bytes=hmac_sha256(&from_hex(bootstrap["secret"].as_str().unwrap()),format!("cafe.broker.auth.v1\n{identity}\n7\n{nonce}").as_bytes());
    let proof=if correct{bytes.iter().map(|b|format!("{b:02x}")).collect::<String>()}else{"0".repeat(64)};
    write_frame(&mut stream,&json!({"identity":identity,"epoch":"7","proof":proof})).await.unwrap();
    stream
}
async fn authenticated(bootstrap:&Value)->tokio::net::TcpStream {
    let mut stream=authenticate(bootstrap,"broker",true).await;
    assert_eq!(read_frame(&mut stream).await.unwrap().unwrap(),json!({"authenticated":true}));stream
}
async fn call(stream:&mut tokio::net::TcpStream,intent:Value)->Value {
    write_frame(stream,&json!({"epoch":"7","capability":"control","body":intent})).await.unwrap();
    read_frame(stream).await.unwrap().unwrap()
}
fn run<F:std::future::Future<Output=()>>(future:F){
    let runtime=tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
    let result=std::panic::catch_unwind(std::panic::AssertUnwindSafe(||runtime.block_on(future)));
    drop(runtime); // Join bounded blocking callbacks before deleting owned Windows files.
    DEFERRED_CLEANUP.with(|paths|for path in paths.borrow_mut().drain(..){std::fs::remove_dir_all(path).expect("owned temporary cleanup failed");});
    if let Err(error)=result {std::panic::resume_unwind(error);}
}

#[test] fn authenticated_folder_start_stop_obs_have_handwritten_transitions(){run(async {
    let f=Fixture::new(Fault::None,false);let (broker,bootstrap)=f.broker().await;let mut peer=authenticated(&bootstrap).await;
    let actions=[ControlAction::SetFolder(f.folder()),ControlAction::StartRecording("cap-one".into()),ControlAction::StopRecording("cap-one".into()),ControlAction::SendToObs("cap-one".into())];
    for (index,action) in actions.into_iter().enumerate(){
        let wire=f.approve(&format!("operation-{index}"),action);let answer=call(&mut peer,wire).await;
        assert_eq!(answer["state"],"committed");assert_eq!(answer["revision"],(5+index*2).to_string());assert_eq!(answer["error"],Value::Null);
        assert_eq!(answer["receipt"]["schema"],"cafe.control_receipt.v1");
    }
    assert_eq!(*f.events.lock().unwrap(),["set_folder","start_recording","stop_recording","send_to_obs"]);
    assert_eq!(f.runtime.lock().unwrap().revision(),Clock(11));drop(peer);drop(broker);tokio::task::yield_now().await;
});}
#[test] fn exact_retry_is_deduplicated_but_changed_request_is_rejected(){run(async {
    let f=Fixture::new(Fault::None,false);let (broker,bootstrap)=f.broker().await;let mut peer=authenticated(&bootstrap).await;
    let wire=f.approve("once",ControlAction::SetFolder(f.folder()));let answer=call(&mut peer,wire.clone()).await;
    assert_eq!(call(&mut peer,wire.clone()).await,answer);
    let mut changed=wire;changed["body"]=json!({"op":"send_to_obs","capsule_id":"cap-one","confirmation_token":"a".repeat(64)});
    assert_eq!(call(&mut peer,changed).await["error"],"request_id_conflict");assert_eq!(f.events.lock().unwrap().len(),1);drop(peer);drop(broker);tokio::task::yield_now().await;
});}
#[test] fn confirmation_binding_rejects_changed_operation_arguments(){run(async {
    let f=Fixture::new(Fault::None,false);let (broker,bootstrap)=f.broker().await;let mut peer=authenticated(&bootstrap).await;
    let mut wire=f.approve("bound",ControlAction::SetFolder(f.folder()));wire["body"]=json!({"op":"send_to_obs","capsule_id":"cap-one","confirmation_token":wire["body"]["confirmation_token"].clone()});
    assert_eq!(call(&mut peer,wire).await["error"],"confirmation_binding_or_expiry");f.assert_no_effects();assert_eq!(f.runtime.lock().unwrap().revision(),Clock(3));drop(peer);drop(broker);tokio::task::yield_now().await;
});}
#[test] fn expired_confirmation_and_reuse_fail_before_effect(){run(async {
    let f=Fixture::new(Fault::None,false);let (broker,bootstrap)=f.broker().await;let mut peer=authenticated(&bootstrap).await;
    let wire=f.approve("expired",ControlAction::SetFolder(f.folder()));f.runtime.lock().unwrap().expire_confirmations_for_test();
    assert_eq!(call(&mut peer,wire.clone()).await["error"],"confirmation_binding_or_expiry");
    assert_eq!(call(&mut peer,wire).await["error"],"confirmation_required");f.assert_no_effects();drop(peer);drop(broker);tokio::task::yield_now().await;
});}
#[test] fn stale_revision_and_old_epoch_are_rejected_before_effect(){run(async {
    let f=Fixture::new(Fault::None,false);let (broker,bootstrap)=f.broker().await;let mut peer=authenticated(&bootstrap).await;
    let old=f.approve("stale",ControlAction::SendToObs("cap-one".into()));let folder=f.approve("folder",ControlAction::SetFolder(f.folder()));assert_eq!(call(&mut peer,folder).await["state"],"committed");
    assert_eq!(call(&mut peer,old.clone()).await["error"],"stale_revision");let mut stale_epoch=old;stale_epoch["authority_epoch"]=json!("6");
    assert_eq!(call(&mut peer,stale_epoch).await["error"],"control_identity_or_epoch_denied");assert_eq!(*f.events.lock().unwrap(),["set_folder"]);drop(peer);drop(broker);tokio::task::yield_now().await;
});}
#[test] fn closed_wire_unknown_keys_and_unsafe_clocks_reject(){run(async {
    let f=Fixture::new(Fault::None,false);let (broker,bootstrap)=f.broker().await;let mut peer=authenticated(&bootstrap).await;
    for mutation in 0..5 {
        let mut wire=f.approve(&format!("closed-{mutation}"),ControlAction::SendToObs("cap-one".into()));
        match mutation {0=>wire["extra"]=json!(true),1=>wire["body"]["extra"]=json!(true),2=>wire["expected_revision"]=json!(3),3=>wire["authority_epoch"]=json!("18446744073709551616"),_=>wire["expected_revision"]=json!("03")}
        assert_eq!(call(&mut peer,wire).await["error"],"invalid_control_intent");
    }
    f.assert_no_effects();drop(peer);drop(broker);tokio::task::yield_now().await;
});}
#[test] fn folder_escape_and_remote_issuer_are_denied(){
    let f=Fixture::new(Fault::None,false);let mut runtime=f.runtime.lock().unwrap();
    assert!(runtime.confirm_for_operator("connector","wrong",ControlAction::SetFolder(f.folder()),Duration::from_secs(1)).is_err());
    assert!(runtime.confirm_for_operator("broker","outside",ControlAction::SetFolder(f.directory.root.clone()),Duration::from_secs(1)).is_err());
    assert!(runtime.confirm_for_operator("broker","relative",ControlAction::SetFolder(PathBuf::from("relative")),Duration::from_secs(1)).is_err());
    assert!(runtime.confirm_for_operator("broker","ttl",ControlAction::SetFolder(f.folder()),Duration::from_secs(31)).is_err());
}
#[test] fn bad_hmac_and_wrong_transport_identity_have_no_effect(){run(async {
    for (identity,correct) in [("broker",false),("connector",true)] {
        let f=Fixture::new(Fault::None,false);let (broker,bootstrap)=f.broker().await;let mut peer=authenticate(&bootstrap,identity,correct).await;
        assert!(read_frame(&mut peer).await.unwrap().is_none());f.assert_no_effects();assert_eq!(f.runtime.lock().unwrap().revision(),Clock(3));drop(peer);drop(broker);tokio::task::yield_now().await;
    }
});}
#[test] fn arbitrary_capability_or_remote_confirmation_issuer_never_reaches_handler(){run(async {
    let f=Fixture::new(Fault::None,false);let (broker,bootstrap)=f.broker().await;let mut peer=authenticated(&bootstrap).await;
    write_frame(&mut peer,&json!({"epoch":"7","capability":"confirm_for_operator","body":{}})).await.unwrap();
    assert_eq!(read_frame(&mut peer).await.unwrap().unwrap()["error"],"capability_denied");f.assert_no_effects();drop(peer);drop(broker);tokio::task::yield_now().await;
});}
#[test] fn durable_intent_flush_failure_prevents_dispatch(){run(async {
    let f=Fixture::new(Fault::None,true);let (broker,bootstrap)=f.broker().await;let mut peer=authenticated(&bootstrap).await;
    let wire=f.approve("flush",ControlAction::SetFolder(f.folder()));let answer=call(&mut peer,wire).await;
    assert_eq!(answer["state"],"blocked");assert_eq!(answer["error"],"intent_not_durable");assert_eq!(answer["receipt"],Value::Null);f.assert_no_effects();drop(peer);drop(broker);tokio::task::yield_now().await;
});}
#[test] fn result_flush_failure_never_reports_committed_or_dispatches_twice(){run(async {
    let f=Fixture::new(Fault::ResultFlush,false);let (broker,bootstrap)=f.broker().await;let mut peer=authenticated(&bootstrap).await;
    let wire=f.approve("result-flush",ControlAction::SetFolder(f.folder()));let answer=call(&mut peer,wire.clone()).await;
    assert_eq!(answer["state"],"interrupted");assert_eq!(answer["error"],"result_not_durable");assert_eq!(answer["receipt"],Value::Null);
    assert_eq!(call(&mut peer,wire).await["state"],"blocked");assert_eq!(f.events.lock().unwrap().len(),1);drop(peer);drop(broker);tokio::task::yield_now().await;
});}
#[test] fn interrupted_effect_remains_interrupted_and_never_retries(){run(async {
    let f=Fixture::new(Fault::EffectInterrupted,false);let (broker,bootstrap)=f.broker().await;let mut peer=authenticated(&bootstrap).await;
    let wire=f.approve("partial",ControlAction::SetFolder(f.folder()));assert_eq!(call(&mut peer,wire.clone()).await["state"],"interrupted");
    assert_eq!(call(&mut peer,wire).await["error"],"prior_effect_not_replayed");f.assert_no_effects();drop(peer);drop(broker);tokio::task::yield_now().await;
});}
#[test] fn confirmation_and_folder_plaintext_never_enter_journal_or_reply(){run(async {
    let f=Fixture::new(Fault::None,false);let (broker,bootstrap)=f.broker().await;let mut peer=authenticated(&bootstrap).await;
    let wire=f.approve("private",ControlAction::SetFolder(f.folder()));let token=wire["body"]["confirmation_token"].as_str().unwrap().to_owned();let folder=wire["body"]["folder"].as_str().unwrap().to_owned();
    let answer=call(&mut peer,wire).await;let bytes=std::fs::read(f.directory.root.join("journal.bin")).unwrap();
    assert!(!bytes.windows(token.len()).any(|window|window==token.as_bytes()));assert!(!bytes.windows(folder.len()).any(|window|window==folder.as_bytes()));
    let reply=serde_json::to_string(&answer).unwrap();assert!(!reply.contains(&token));assert!(!reply.contains(&folder));assert_eq!(answer["state"],"committed");drop(peer);drop(broker);tokio::task::yield_now().await;
});}
#[test] fn journal_recovery_accepts_closed_control_facts_and_preserves_capsules(){run(async {
    let directory=Temporary::new();let log=directory.root.join("journal.bin");let folder=directory.root.join("recordings");std::fs::create_dir(&folder).unwrap();
    let events=Arc::new(Mutex::new(Vec::new()));let journal=Journal::open(&log,"replay-session",Clock(7)).unwrap();
    let shared=Arc::new(Mutex::new(ControlRuntime::open(journal,Clock(7),FileEffects{root:directory.root.clone(),events:events.clone(),fault:Fault::None},&[folder.clone()]).unwrap()));
    let broker=BrokerAuthority::start_control(&directory.root.join("rendezvous"),"127.0.0.1",7,Box::new(ControlBroker(shared.clone()))).await.unwrap();
    let bootstrap=private_bootstrap(&directory.root.join("rendezvous")).await;let mut peer=authenticated(&bootstrap).await;
    let wire=shared.lock().unwrap().confirm_for_operator("broker","recover",ControlAction::SetFolder(folder.clone()),Duration::from_secs(10)).unwrap().into_private_wire();
    let expected=call(&mut peer,wire.clone()).await;assert_eq!(expected["revision"],"2");drop(peer);drop(broker);tokio::task::yield_now().await;drop(shared);
    let journal=Journal::open(&log,"replay-session",Clock(7)).unwrap();assert_eq!(journal.records().len(),2);drop(journal);
    let recovered=journal_replay::recover(&log,&folder,"replay-session",Clock(7)).unwrap();assert_eq!(recovered.snapshot.revision,Clock(0));assert!(recovered.media.is_empty());
    let journal=Journal::open(&log,"replay-session",Clock(7)).unwrap();let runtime=ControlRuntime::open(journal,Clock(7),FileEffects{root:directory.root.clone(),events,fault:Fault::None},&[folder]).unwrap();assert_eq!(runtime.revision(),Clock(2));
});}

#[test] fn duplicate_json_keys_are_rejected_at_real_authenticated_frame_boundary(){run(async {
    use tokio::io::AsyncWriteExt;
    let f=Fixture::new(Fault::None,false);let (broker,bootstrap)=f.broker().await;let mut peer=authenticated(&bootstrap).await;
    peer.write_all(b"{\"epoch\":\"7\",\"capability\":\"control\",\"body\":{},\"body\":{}}\n").await.unwrap();
    assert!(read_frame(&mut peer).await.unwrap().is_none());f.assert_no_effects();drop(peer);drop(broker);tokio::task::yield_now().await;
});}
#[test] fn invalid_hmac_cannot_burn_valid_private_broker_bootstrap(){run(async {
    let f=Fixture::new(Fault::None,false);let (broker,bootstrap)=f.broker().await;let mut wrong=authenticate(&bootstrap,"broker",false).await;
    assert!(read_frame(&mut wrong).await.unwrap().is_none());drop(wrong);
    let mut peer=authenticated(&bootstrap).await;let wire=f.approve("after-bad-proof",ControlAction::SetFolder(f.folder()));
    assert_eq!(call(&mut peer,wire).await["state"],"committed");assert_eq!(f.events.lock().unwrap().len(),1);drop(peer);drop(broker);tokio::task::yield_now().await;
});}
#[test] fn transport_epoch_is_checked_before_control_handler(){run(async {
    let f=Fixture::new(Fault::None,false);let (broker,bootstrap)=f.broker().await;let mut peer=authenticated(&bootstrap).await;
    let wire=f.approve("bad-transport",ControlAction::SetFolder(f.folder()));
    write_frame(&mut peer,&json!({"epoch":"6","capability":"control","body":wire})).await.unwrap();
    assert_eq!(read_frame(&mut peer).await.unwrap().unwrap()["error"],"capability_denied");f.assert_no_effects();drop(peer);drop(broker);tokio::task::yield_now().await;
});}
#[test] fn request_identity_binding_and_missing_capsule_reject_before_effect(){run(async {
    let f=Fixture::new(Fault::None,false);let (broker,bootstrap)=f.broker().await;let mut peer=authenticated(&bootstrap).await;
    let mut wire=f.approve("original",ControlAction::SetFolder(f.folder()));wire["request_id"]=json!("changed");
    assert_eq!(call(&mut peer,wire).await["error"],"confirmation_binding_or_expiry");
    let mut wire=f.approve("capsule",ControlAction::SendToObs("cap-one".into()));wire["body"]["capsule_id"]=json!("missing");
    assert_eq!(call(&mut peer,wire).await["error"],"capsule_unavailable");f.assert_no_effects();drop(peer);drop(broker);tokio::task::yield_now().await;
});}
#[test] fn empty_journal_epoch_cannot_be_reinterpreted_by_control_runtime(){
    let directory=Temporary::new();let journal=Journal::open(&directory.root.join("journal.bin"),"epoch",Clock(8)).unwrap();
    let events=Arc::new(Mutex::new(Vec::new()));
    assert!(ControlRuntime::open(journal,Clock(7),FileEffects{root:directory.root.clone(),events,fault:Fault::None},&[directory.root.clone()]).is_err());
}
#[cfg(unix)]
#[test] fn symlinked_folder_is_not_an_operator_capability(){
    let f=Fixture::new(Fault::None,false);let link=f.folder().join("alias");std::os::unix::fs::symlink(&f.directory.root,&link).unwrap();
    assert!(f.runtime.lock().unwrap().confirm_for_operator("broker","symlink",ControlAction::SetFolder(link),Duration::from_secs(1)).is_err());
}

#[test] fn recording_without_operator_folder_is_blocked_without_a_durable_effect(){run(async {
    let f=Fixture::new(Fault::None,false);let (broker,bootstrap)=f.broker().await;let mut peer=authenticated(&bootstrap).await;
    let wire=f.approve("missing-folder",ControlAction::StartRecording("cap-one".into()));let answer=call(&mut peer,wire).await;
    assert_eq!(answer["state"],"blocked");assert_eq!(answer["error"],"recording_folder_required");assert_eq!(answer["receipt"],Value::Null);
    assert_eq!(f.runtime.lock().unwrap().revision(),Clock(3));f.assert_no_effects();drop(peer);drop(broker);tokio::task::yield_now().await;
});}
#[test] fn confirmation_cannot_be_reflected_into_request_id_or_journal(){run(async {
    let f=Fixture::new(Fault::None,false);let (broker,bootstrap)=f.broker().await;let mut peer=authenticated(&bootstrap).await;
    let mut wire=f.approve("reflection",ControlAction::SetFolder(f.folder()));let token=wire["body"]["confirmation_token"].as_str().unwrap().to_owned();wire["request_id"]=json!(token);
    let answer=call(&mut peer,wire).await;assert!(answer["request_id"]=="invalid","private identifier reflection");assert_eq!(answer["state"],"rejected");
    assert!(!serde_json::to_string(&answer).unwrap().contains(&token));let bytes=std::fs::read(f.directory.root.join("journal.bin")).unwrap();assert!(!bytes.windows(token.len()).any(|window|window==token.as_bytes()));
    f.assert_no_effects();drop(peer);drop(broker);tokio::task::yield_now().await;
});}

fn independent_digest(data:&[u8])->String {
    use sha2::{Digest,Sha256};Sha256::digest(data).iter().map(|byte|format!("{byte:02x}")).collect()
}
fn manual_record(kind:&str,payload:Value,sequence:u64,prior:&str,event:&str)->(Vec<u8>,String) {
    let mut record=json!({"schema":"cafe.journal.v1","session_id":"manual-control","authority_epoch":"7","sequence":sequence.to_string(),"event_id":event,"kind":kind,"payload":payload,"prior_digest":prior});
    let digest=independent_digest(&serde_json::to_vec(&record).unwrap());record["record_digest"]=json!(digest);
    let body=serde_json::to_vec(&record).unwrap();let length=u32::try_from(body.len()).unwrap();
    let mut frame=Vec::new();frame.extend_from_slice(&length.to_be_bytes());frame.extend_from_slice(&(!length).to_be_bytes());frame.extend(body);frame.extend_from_slice(b"CJOK");(frame,digest)
}
fn manual_intent(request:&str,args:&str)->Value {
    json!({"request_id":request,"identity_sha256":independent_digest(b"broker"),"operation":"set_folder","args_sha256":args,"capsule_id":null,"expected_revision":"0","effect_id":format!("ctl.{}",independent_digest(request.as_bytes()))})
}
#[test] fn replay_rejects_closed_control_corruption_even_with_recomputed_digests(){
    for case in 0..5 {
        let directory=Temporary::new();let log=directory.root.join("journal.bin");let mut intent=manual_intent("manual",&"a".repeat(64));
        match case {0=>intent["confirmation_token"]=json!("synthetic-private-confirmation"),1=>intent["expected_revision"]=json!("1"),2=>intent["effect_id"]=json!("wrong-effect"),_=>{}}
        let (mut bytes,prior)=manual_record("confirmed_control_intent",intent,1,&"0".repeat(64),"manual-intent");
        if case==3 || case==4 {
            let result=json!({"request_id":"manual","effect_id":if case==3{"wrong-effect".to_owned()}else{format!("ctl.{}",independent_digest(b"manual"))},"state":if case==4{"complete"}else{"committed"}});
            bytes.extend(manual_record("confirmed_control_result",result,2,&prior,"manual-result").0);
        }
        std::fs::write(&log,bytes).unwrap();assert!(matches!(Journal::open(&log,"manual-control",Clock(7)),Err(authority_journal::JournalError::Corrupt)));
    }
}
#[test] fn replay_rejects_result_without_intent(){
    let directory=Temporary::new();let log=directory.root.join("journal.bin");
    let payload=json!({"request_id":"missing","effect_id":"unknown","state":"committed"});
    std::fs::write(&log,manual_record("confirmed_control_result",payload,1,&"0".repeat(64),"orphan").0).unwrap();
    assert!(matches!(Journal::open(&log,"manual-control",Clock(7)),Err(authority_journal::JournalError::Corrupt)));
}
#[test] fn recovered_intent_without_result_is_interrupted_and_never_redispatched(){run(async {
    let directory=Temporary::new();let folder=directory.root.join("recordings");std::fs::create_dir(&folder).unwrap();let log=directory.root.join("journal.bin");
    let wire=json!({"schema":"cafe.control_intent.v1","kind":"control_intent","request_id":"pending","authority_epoch":"7","expected_revision":"0","body":{"op":"set_folder","folder":folder.to_str().unwrap(),"confirmation_token":"b".repeat(64)}});
    let fingerprint_input=json!({"schema":"cafe.control_intent.v1","kind":"control_intent","request_id":"pending","authority_epoch":"7","expected_revision":"0","body":{"op":"set_folder","folder":folder.canonicalize().unwrap().to_str().unwrap()}});
    let fingerprint=independent_digest(&serde_json::to_vec(&fingerprint_input).unwrap());
    let fact=manual_intent("pending",&fingerprint);std::fs::write(&log,manual_record("confirmed_control_intent",fact,1,&"0".repeat(64),"pending-event").0).unwrap();
    let events=Arc::new(Mutex::new(Vec::new()));let runtime=ControlRuntime::open(Journal::open(&log,"manual-control",Clock(7)).unwrap(),Clock(7),FileEffects{root:directory.root.clone(),events:events.clone(),fault:Fault::None},&[folder]).unwrap();
    let shared=Arc::new(Mutex::new(runtime));let rendezvous=directory.root.join("rendezvous");let broker=BrokerAuthority::start_control(&rendezvous,"127.0.0.1",7,Box::new(ControlBroker(shared.clone()))).await.unwrap();
    let bootstrap=private_bootstrap(&rendezvous).await;let mut peer=authenticated(&bootstrap).await;
    let answer=call(&mut peer,wire).await;assert_eq!(answer["state"],"interrupted");assert_eq!(answer["error"],"prior_effect_not_replayed");assert_eq!(answer["receipt"],Value::Null);assert!(events.lock().unwrap().is_empty());assert_eq!(shared.lock().unwrap().revision(),Clock(1));
    drop(peer);drop(broker);tokio::task::yield_now().await;drop(shared);
});}

fn actor_bindings()->Vec<capsule_pairing::Binding> {
    use windows_window_registry::{WindowRegistry,FixtureWindow,Rect};
    let mut windows=WindowRegistry::new(Clock(7));
    let views=windows.refresh_fixtures(&(0..2).map(|i|FixtureWindow{slot:format!("slot-{i}"),incarnation:1,process_id:42,process_birth:1,executable:"Discord.exe".into(),title:format!("Window-{i}"),class:"Discord".into(),rect:Rect{x:0,y:0,width:100,height:100},visible:true,minimized:false,layout:"fixture".into()}).collect::<Vec<_>>()).unwrap();
    views.into_iter().enumerate().map(|(i,view)|capsule_pairing::Binding{capsule_id:"cap-one".into(),person_label:"Guest".into(),feed_id:if i==0{"game-one".into()}else{"camera-one".into()},role:if i==0{capsule_registry::Role::Game}else{capsule_registry::Role::Camera},window:view.handle,window_generation:view.generation,layout_revision:view.layout_revision,crop:capsule_pairing::Crop{x:0,y:0,width:100,height:100},feed_generation:Clock(if i==0{2}else{3})}).collect()
}
fn actor_journal(root:&Path)->Journal {
    let mut journal=Journal::open(&root.join("journal.bin"),"actor-session",Clock(7)).unwrap();
    for (index,body) in [json!({"op":"create_capsule","capsule_id":"cap-one","person_label":"Guest","generation":"1"}),json!({"op":"add_feed","capsule_id":"cap-one","feed_id":"game-one","role":"game","generation":"2"}),json!({"op":"add_feed","capsule_id":"cap-one","feed_id":"camera-one","role":"camera","generation":"3"})].into_iter().enumerate() {
        let input=json!({"schema":"cafe.control.v1","kind":"intent","request_id":format!("actor-seed-{index}"),"authority_epoch":"7","expected_revision":index.to_string(),"body":body});
        journal.append_control(&format!("actor-event-{index}"),serde_json::from_value(input).unwrap()).unwrap();
    }
    journal
}
#[test] fn bounded_actor_queue_returns_pending_until_trusted_durable_completion(){run(async {
    let directory=Temporary::new();let folder=directory.root.join("recordings");std::fs::create_dir(&folder).unwrap();
    let (sender,mut receiver)=tokio::sync::mpsc::channel(1);
    let runtime=ControlRuntime::open(actor_journal(&directory.root),Clock(7),ControlCommandSender::new(sender,std::sync::Arc::new(std::sync::RwLock::new(actor_bindings()))),&[folder.clone()]).unwrap();
    let shared=Arc::new(Mutex::new(runtime));let rendezvous=directory.root.join("rendezvous");let broker=BrokerAuthority::start_control(&rendezvous,"127.0.0.1",7,Box::new(ControlBroker(shared.clone()))).await.unwrap();
    let bootstrap=private_bootstrap(&rendezvous).await;let mut peer=authenticated(&bootstrap).await;
    let wire=shared.lock().unwrap().confirm_for_operator("broker","actor-folder",ControlAction::SetFolder(folder.clone()),Duration::from_secs(10)).unwrap().into_private_wire();
    let pending=call(&mut peer,wire.clone()).await;assert_eq!(pending["state"],"pending");assert_eq!(pending["revision"],"4");assert_eq!(pending["error"],Value::Null);assert_eq!(pending["receipt"],Value::Null);
    assert_eq!(call(&mut peer,wire.clone()).await,pending);
    let job=receiver.recv().await.unwrap();assert_eq!(job.authority_epoch,Clock(7));assert!(receiver.try_recv().is_err());
    match &job.action {ActorControlAction::SetFolder(capability)=>{assert_eq!(capability.revalidate().unwrap(),folder.canonicalize().unwrap());},_=>panic!("unexpected actor job")}
    // A synthetic actor performs its real fixture file effect before the trusted completion.
    let mut artifact=std::fs::File::create(directory.root.join("actor-folder-applied")).unwrap();artifact.write_all(b"fixture accepted").unwrap();artifact.sync_all().unwrap();drop(artifact);
    let complete=shared.lock().unwrap().complete_from_actor(&job.effect_id,ActorCompletion::Applied);
    assert_eq!(complete.state,ControlState::Committed);assert_eq!(complete.revision,Clock(5));
    assert_eq!(call(&mut peer,wire).await,serde_json::to_value(&complete).unwrap());assert!(receiver.try_recv().is_err());
    assert_eq!(shared.lock().unwrap().complete_from_actor(&job.effect_id,ActorCompletion::Applied),complete);
    drop(peer);drop(broker);tokio::task::yield_now().await;drop(shared);
});}
#[test] fn actor_queue_full_is_interrupted_never_committed_or_requeued(){run(async {
    let directory=Temporary::new();let folder=directory.root.join("recordings");std::fs::create_dir(&folder).unwrap();
    let (sender,mut receiver)=tokio::sync::mpsc::channel(1);let runtime=ControlRuntime::open(actor_journal(&directory.root),Clock(7),ControlCommandSender::new(sender,std::sync::Arc::new(std::sync::RwLock::new(actor_bindings()))),&[folder.clone()]).unwrap();
    let shared=Arc::new(Mutex::new(runtime));let rendezvous=directory.root.join("rendezvous");let broker=BrokerAuthority::start_control(&rendezvous,"127.0.0.1",7,Box::new(ControlBroker(shared.clone()))).await.unwrap();let bootstrap=private_bootstrap(&rendezvous).await;let mut peer=authenticated(&bootstrap).await;
    let first=shared.lock().unwrap().confirm_for_operator("broker","queued",ControlAction::SetFolder(folder),Duration::from_secs(10)).unwrap().into_private_wire();assert_eq!(call(&mut peer,first).await["state"],"pending");
    let second=shared.lock().unwrap().confirm_for_operator("broker","full",ControlAction::SendToObs("cap-one".into()),Duration::from_secs(10)).unwrap().into_private_wire();
    let answer=call(&mut peer,second.clone()).await;assert_eq!(answer["state"],"interrupted");assert_eq!(answer["receipt"],Value::Null);
    assert_eq!(call(&mut peer,second).await["error"],"prior_effect_not_replayed");let _first=receiver.recv().await.unwrap();assert!(receiver.try_recv().is_err());
    assert_eq!(shared.lock().unwrap().revision(),Clock(6));drop(peer);drop(broker);tokio::task::yield_now().await;drop(shared);
});}
#[test] fn queued_capsule_and_feed_generations_are_frozen_for_actor_identity_checks(){run(async {
    let directory=Temporary::new();let folder=directory.root.join("recordings");std::fs::create_dir(&folder).unwrap();
    let (sender,mut receiver)=tokio::sync::mpsc::channel(1);let runtime=ControlRuntime::open(actor_journal(&directory.root),Clock(7),ControlCommandSender::new(sender,std::sync::Arc::new(std::sync::RwLock::new(actor_bindings()))),&[folder.clone()]).unwrap();
    let shared=Arc::new(Mutex::new(runtime));let rendezvous=directory.root.join("rendezvous");let broker=BrokerAuthority::start_control(&rendezvous,"127.0.0.1",7,Box::new(ControlBroker(shared.clone()))).await.unwrap();let bootstrap=private_bootstrap(&rendezvous).await;let mut peer=authenticated(&bootstrap).await;
    let wire=shared.lock().unwrap().confirm_for_operator("broker","freeze",ControlAction::SendToObs("cap-one".into()),Duration::from_secs(10)).unwrap().into_private_wire();assert_eq!(call(&mut peer,wire).await["state"],"pending");let job=receiver.recv().await.unwrap();
    let mut expected=capsule_registry::Capsule{capsule_id:"cap-one".into(),person_label:"Guest".into(),generation:Clock(1),feeds:std::collections::BTreeMap::from([("game-one".into(),capsule_registry::Feed{feed_id:"game-one".into(),role:capsule_registry::Role::Game,generation:Clock(2)}),("camera-one".into(),capsule_registry::Feed{feed_id:"camera-one".into(),role:capsule_registry::Role::Camera,generation:Clock(3)})])};
    assert!(job.matches_current_capsule(Clock(7),&expected));assert!(!job.matches_current_capsule(Clock(8),&expected));
    let mut bindings=actor_bindings();assert!(job.matches_current_bindings(&bindings));bindings[0].window_generation=Clock(99);assert!(!job.matches_current_bindings(&bindings));
    expected.feeds.get_mut("camera-one").unwrap().generation=Clock(99);assert!(!job.matches_current_capsule(Clock(7),&expected));
    let answer=shared.lock().unwrap().complete_from_actor(&job.effect_id,ActorCompletion::Interrupted);assert_eq!(answer.state,ControlState::Interrupted);assert_eq!(answer.receipt,None);
    drop(peer);drop(broker);tokio::task::yield_now().await;drop(shared);
});}

#[test] fn poisoned_runtime_returns_typed_redacted_error_without_reflecting_secrets(){run(async {
    let f=Fixture::new(Fault::None,false);let mut wire=f.approve("poison",ControlAction::SetFolder(f.folder()));let token=wire["body"]["confirmation_token"].as_str().unwrap().to_owned();wire["request_id"]=json!(token);
    let _=std::panic::catch_unwind(std::panic::AssertUnwindSafe(||{let _guard=f.runtime.lock().unwrap();panic!("synthetic actor failure");}));
    let (broker,bootstrap)=f.broker().await;let mut peer=authenticated(&bootstrap).await;let answer=call(&mut peer,wire).await;
    assert_eq!(answer["state"],"blocked");assert!(answer["request_id"]=="invalid","private identifier reflection");assert_eq!(answer["revision"],"3");assert_eq!(answer["error"],"control_runtime_unavailable");assert_eq!(answer["receipt"],Value::Null);assert!(!serde_json::to_string(&answer).unwrap().contains(&token));
    f.assert_no_effects();drop(peer);drop(broker);tokio::task::yield_now().await;
});}

#[test] fn authenticated_control_session_survives_idle_but_not_partial_frame_timeout(){run(async {
    use tokio::io::AsyncWriteExt;
    let f=Fixture::new(Fault::None,false);let (broker,bootstrap)=f.broker().await;let mut peer=authenticated(&bootstrap).await;
    tokio::time::sleep(Duration::from_secs(6)).await;
    let wire=f.approve("after-idle",ControlAction::SetFolder(f.folder()));assert_eq!(call(&mut peer,wire).await["state"],"committed");
    peer.write_all(b"{").await.unwrap();tokio::time::sleep(Duration::from_secs(6)).await;
    assert!(read_frame(&mut peer).await.unwrap().is_none());assert_eq!(f.events.lock().unwrap().len(),1);drop(peer);drop(broker);tokio::task::yield_now().await;
});}

#[test] fn replay_rejects_duplicate_valid_outcome_with_distinct_event_and_valid_hashes(){
    let directory=Temporary::new();let log=directory.root.join("journal.bin");let intent=manual_intent("duplicate",&"a".repeat(64));
    let (mut bytes,prior)=manual_record("confirmed_control_intent",intent,1,&"0".repeat(64),"intent");
    let result=json!({"request_id":"duplicate","effect_id":format!("ctl.{}",independent_digest(b"duplicate")),"state":"committed"});
    let (first,prior)=manual_record("confirmed_control_result",result.clone(),2,&prior,"first-result");bytes.extend(first);
    bytes.extend(manual_record("confirmed_control_result",result,3,&prior,"second-result").0);
    std::fs::write(&log,bytes).unwrap();assert!(matches!(Journal::open(&log,"manual-control",Clock(7)),Err(authority_journal::JournalError::Corrupt)));
}

#[test] fn offloaded_handler_allows_current_thread_actor_timer_and_real_kernel_progress(){run(async {
    let f=Fixture::new(Fault::None,false);let (sender,mut receiver)=tokio::sync::mpsc::channel::<ActorControlRequest>(1);
    let handler=ControlActorClient::new(sender,Duration::from_secs(4)).unwrap();let dir=f.directory.root.join("actor-rendezvous");
    let broker=BrokerAuthority::start_control(&dir,"127.0.0.1",7,Box::new(handler)).await.unwrap();let bootstrap=private_bootstrap(&dir).await;let mut peer=authenticated(&bootstrap).await;
    let wire=f.approve("current-thread",ControlAction::SetFolder(f.folder()));let shared=f.runtime.clone();let progressed=Arc::new(std::sync::atomic::AtomicBool::new(false));let flag=progressed.clone();
    let actor=tokio::spawn(async move {let request=receiver.recv().await.unwrap();tokio::time::sleep(Duration::from_millis(20)).await;flag.store(true,Ordering::SeqCst);request.execute_on(&mut *shared.lock().unwrap());});
    let answer=call(&mut peer,wire).await;assert_eq!(answer["state"],"committed");assert!(progressed.load(Ordering::SeqCst));actor.await.unwrap();assert_eq!(*f.events.lock().unwrap(),["set_folder"]);
    drop(peer);drop(broker);tokio::task::yield_now().await;
});}
#[test] fn timed_out_actor_admission_is_revoked_before_any_effect(){run(async {
    let f=Fixture::new(Fault::None,false);let (sender,mut receiver)=tokio::sync::mpsc::channel::<ActorControlRequest>(1);
    let handler=ControlActorClient::new(sender,Duration::from_millis(20)).unwrap();let dir=f.directory.root.join("actor-rendezvous");let broker=BrokerAuthority::start_control(&dir,"127.0.0.1",7,Box::new(handler)).await.unwrap();let bootstrap=private_bootstrap(&dir).await;let mut peer=authenticated(&bootstrap).await;
    let shared=f.runtime.clone();let actor=tokio::spawn(async move {let request=receiver.recv().await.unwrap();tokio::time::sleep(Duration::from_millis(80)).await;request.execute_on(&mut *shared.lock().unwrap());});
    let wire=f.approve("actor-timeout",ControlAction::SetFolder(f.folder()));let answer=call(&mut peer,wire).await;assert_eq!(answer,json!({"error":"control_actor_reply_unavailable"}));actor.await.unwrap();
    assert_eq!(f.runtime.lock().unwrap().revision(),Clock(3));f.assert_no_effects();drop(peer);drop(broker);tokio::task::yield_now().await;
});}
#[test] fn authority_drop_revokes_an_already_offloaded_but_unstarted_admission(){run(async {
    let f=Fixture::new(Fault::None,false);let (sender,mut receiver)=tokio::sync::mpsc::channel::<ActorControlRequest>(1);
    let handler=ControlActorClient::new(sender,Duration::from_secs(4)).unwrap();let dir=f.directory.root.join("actor-rendezvous");let broker=BrokerAuthority::start_control(&dir,"127.0.0.1",7,Box::new(handler)).await.unwrap();let bootstrap=private_bootstrap(&dir).await;let mut peer=authenticated(&bootstrap).await;
    let wire=f.approve("actor-revoked",ControlAction::SetFolder(f.folder()));write_frame(&mut peer,&json!({"epoch":"7","capability":"control","body":wire})).await.unwrap();let request=receiver.recv().await.unwrap();
    drop(broker);tokio::task::yield_now().await;request.execute_on(&mut *f.runtime.lock().unwrap());
    assert_eq!(f.runtime.lock().unwrap().revision(),Clock(3));f.assert_no_effects();drop(peer);tokio::task::yield_now().await;
});}
#[test] fn control_mode_waits_for_late_private_bootstrap_without_the_legacy_timeout(){run(async {
    let f=Fixture::new(Fault::None,false);let dir=f.directory.root.join("late-rendezvous");let broker=BrokerAuthority::start_control(&dir,"127.0.0.1",7,Box::new(ControlBroker(f.runtime.clone()))).await.unwrap();
    tokio::time::sleep(Duration::from_secs(31)).await;
    let bootstrap=private_bootstrap(&dir).await;let mut peer=authenticated(&bootstrap).await;let wire=f.approve("late-bootstrap",ControlAction::SetFolder(f.folder()));assert_eq!(call(&mut peer,wire).await["state"],"committed");
    drop(peer);drop(broker);tokio::task::yield_now().await;
});}
struct OversizedHandler;
impl authority_auth::ControlHandler for OversizedHandler {
    fn handle(&mut self,_peer:&authority_auth::AuthenticatedBrokerPeer,_intent:&Value)->Value{json!({"data":"a".repeat(authority_auth::MAX_LINE)})}
}
#[test] fn oversized_handler_reply_fails_closed_at_the_real_socket(){run(async {
    let f=Fixture::new(Fault::None,false);let dir=f.directory.root.join("oversized-rendezvous");let broker=BrokerAuthority::start_control(&dir,"127.0.0.1",7,Box::new(OversizedHandler)).await.unwrap();let bootstrap=private_bootstrap(&dir).await;let mut peer=authenticated(&bootstrap).await;
    write_frame(&mut peer,&json!({"epoch":"7","capability":"control","body":{}})).await.unwrap();assert!(read_frame(&mut peer).await.unwrap().is_none());f.assert_no_effects();drop(peer);drop(broker);tokio::task::yield_now().await;
});}
#[test] fn control_mode_never_returns_the_legacy_synthetic_snapshot_counter(){run(async {
    let f=Fixture::new(Fault::None,false);let (broker,bootstrap)=f.broker().await;let mut peer=authenticated(&bootstrap).await;
    write_frame(&mut peer,&json!({"epoch":"7","capability":"snapshot","body":{}})).await.unwrap();assert_eq!(read_frame(&mut peer).await.unwrap().unwrap(),json!({"error":"capability_denied"}));f.assert_no_effects();drop(peer);drop(broker);tokio::task::yield_now().await;
});}
#[test] fn authority_actor_borrows_the_same_writer_for_media_intent_without_a_mutex(){
    let directory=Temporary::new();let folder=directory.root.join("recordings");std::fs::create_dir(&folder).unwrap();let (sender,_receiver)=tokio::sync::mpsc::channel(1);
    let mut runtime=ControlRuntime::open(actor_journal(&directory.root),Clock(7),ControlCommandSender::new(sender,Arc::new(std::sync::RwLock::new(actor_bindings()))),&[folder]).unwrap();
    let receipt=runtime.journal_for_media_actor().append_media_intent("actor-media-intent","media-one","clip.mkv").unwrap();assert_eq!(receipt.state(),authority_journal::AppendState::Committed);assert_eq!(runtime.revision(),Clock(4));
    assert!(ControlActorClient::new(tokio::sync::mpsc::channel(1).0,Duration::from_secs(5)).is_err());
}

struct ExpiredScopedHandler { inner:ControlBroker<FileEffects> }
impl authority_auth::ControlHandler for ExpiredScopedHandler {
    fn handle(&mut self,peer:&authority_auth::AuthenticatedBrokerPeer,intent:&Value)->Value {
        let active=Arc::new(std::sync::atomic::AtomicBool::new(true));
        let scoped=peer.for_actor_request(active,std::time::Instant::now()+Duration::from_millis(1)).unwrap();
        std::thread::sleep(Duration::from_millis(10));
        authority_auth::ControlHandler::handle(&mut self.inner,&scoped,intent)
    }
}
#[test] fn scoped_monotonic_deadline_rejects_even_before_cancellation_thread_is_scheduled(){run(async {
    let f=Fixture::new(Fault::None,false);let dir=f.directory.root.join("deadline-rendezvous");let broker=BrokerAuthority::start_control(&dir,"127.0.0.1",7,Box::new(ExpiredScopedHandler{inner:ControlBroker(f.runtime.clone())})).await.unwrap();let bootstrap=private_bootstrap(&dir).await;let mut peer=authenticated(&bootstrap).await;
    let wire=f.approve("deadline-probe",ControlAction::SetFolder(f.folder()));let response=call(&mut peer,wire).await;assert_eq!(response["state"],"rejected");assert_eq!(response["error"],"control_identity_or_epoch_denied");f.assert_no_effects();assert_eq!(f.runtime.lock().unwrap().revision(),Clock(3));
    drop(peer);drop(broker);tokio::task::yield_now().await;
});}

// Observe only the sealed peer delivered by the real authenticated callback.
struct EofObservedActor {
    client: ControlActorClient,
    observed: Option<tokio::sync::oneshot::Sender<authority_auth::AuthenticatedBrokerPeer>>,
}
impl authority_auth::ControlHandler for EofObservedActor {
    fn handle(&mut self,peer:&authority_auth::AuthenticatedBrokerPeer,intent:&Value)->Value {
        if let Some(sender)=self.observed.take(){let _=sender.send(peer.clone());}
        authority_auth::ControlHandler::handle(&mut self.client,peer,intent)
    }
}
async fn assert_reader_revokes_queued_admission(kind:&str) {
    let f=Fixture::new(Fault::None,false);
    let (sender,mut receiver)=tokio::sync::mpsc::channel::<ActorControlRequest>(1);
    let (observe,observed)=tokio::sync::oneshot::channel();
    let client=ControlActorClient::new(sender,Duration::from_secs(4)).unwrap();
    let handler=EofObservedActor{client,observed:Some(observe)};
    let dir=f.directory.root.join("eof-rendezvous");
    let broker=BrokerAuthority::start_control(&dir,"127.0.0.1",7,Box::new(handler)).await.unwrap();
    let bootstrap=private_bootstrap(&dir).await;let mut peer=authenticated(&bootstrap).await;
    let wire=f.approve("reader-revoked",ControlAction::SetFolder(f.folder()));
    let frame=json!({"epoch":"7","capability":"control","body":wire});
    write_frame(&mut peer,&frame).await.unwrap();
    let request=tokio::time::timeout(Duration::from_secs(1),receiver.recv()).await.unwrap().unwrap();
    let sealed=tokio::time::timeout(Duration::from_secs(1),observed).await.unwrap().unwrap();
    assert!(sealed.is_live());
    match kind {
        "eof"=>{},
        "partial_eof"=>{tokio::io::AsyncWriteExt::write_all(&mut peer,b"{\"epoch\":").await.unwrap();},
        "invalid"=>{write_frame(&mut peer,&json!({"epoch":"7","capability":"snapshot","body":{}})).await.unwrap();},
        "overfull"=>{write_frame(&mut peer,&frame).await.unwrap();write_frame(&mut peer,&frame).await.unwrap();},
        _=>panic!("unknown fixture"),
    }
    // Invalid/overfull cases retain the socket: only reader validation can revoke.
    let retained=if kind=="eof" || kind=="partial_eof" {drop(peer);None}else{Some(peer)};
    // This is strictly earlier than the callback's four-second wait/deadline.
    // Observe real revocation; an arbitrary sleep or callback timeout cannot mask it.
    tokio::time::timeout(Duration::from_secs(1),async {
        while sealed.is_live(){tokio::task::yield_now().await;}
    }).await.expect("reader failed to revoke before callback deadline");
    request.execute_on(&mut *f.runtime.lock().unwrap());
    assert_eq!(f.runtime.lock().unwrap().revision(),Clock(3));f.assert_no_effects();
    drop(retained);drop(broker);tokio::task::yield_now().await;
}
#[test] fn judge_peer_eof_revokes_an_already_offloaded_but_unstarted_admission(){run(assert_reader_revokes_queued_admission("eof"));}
#[test] fn partial_frame_eof_revokes_queued_admission_before_callback_deadline(){run(assert_reader_revokes_queued_admission("partial_eof"));}
#[test] fn invalid_lookahead_revokes_queued_admission_before_callback_deadline(){run(assert_reader_revokes_queued_admission("invalid"));}
#[test] fn overfull_lookahead_revokes_queued_admission_before_callback_deadline(){run(assert_reader_revokes_queued_admission("overfull"));}

#[test] fn authority_drop_aborts_its_idle_connection_reader_task(){run(async {
    // Stable current-thread runtime metrics observe an actual task leak, not a mock.
    let metrics=tokio::runtime::Handle::current().metrics();let baseline=metrics.num_alive_tasks();
    let f=Fixture::new(Fault::None,false);let (broker,bootstrap)=f.broker().await;
    let mut peer=authenticated(&bootstrap).await;
    let response=call(&mut peer,f.approve("reader-task-cleanup",ControlAction::SetFolder(f.folder()))).await;
    assert_eq!(response["state"],"committed");
    drop(broker);
    // Keep the client write side open, so a leaked reader cannot finish by EOF.
    tokio::time::timeout(Duration::from_secs(1),async {
        while metrics.num_alive_tasks()!=baseline {tokio::task::yield_now().await;}
    }).await.expect("authority left an owned connection reader task alive");
    drop(peer);
});}

#[test] fn local_operator_uses_durable_handwritten_transitions_and_exact_deduplication(){
    let f=Fixture::new(Fault::None,false);
    let mut runtime=f.runtime.lock().unwrap();
    let first=runtime.handle_operator("local-once",Clock(7),Clock(3),ControlAction::SetFolder(f.folder()));
    assert_eq!(first.state,ControlState::Committed);assert_eq!(first.revision,Clock(5));
    let retry=runtime.handle_operator("local-once",Clock(7),Clock(3),ControlAction::SetFolder(f.folder()));
    assert_eq!(serde_json::to_value(&retry).unwrap(),serde_json::to_value(&first).unwrap());
    assert_eq!(*f.events.lock().unwrap(),["set_folder"]);
    let public=serde_json::to_string(&first).unwrap();assert!(!public.contains("confirmation_token"));assert!(!public.contains(f.folder().to_str().unwrap()));
}
#[test] fn local_operator_stale_epoch_and_revision_reject_before_any_effect(){
    let f=Fixture::new(Fault::None,false);let mut runtime=f.runtime.lock().unwrap();
    let wrong_epoch=runtime.handle_operator("local-epoch",Clock(8),Clock(3),ControlAction::SetFolder(f.folder()));
    assert_eq!(wrong_epoch.state,ControlState::Rejected);assert_eq!(wrong_epoch.error,Some("control_identity_or_epoch_denied"));
    let wrong_revision=runtime.handle_operator("local-revision",Clock(7),Clock(2),ControlAction::SetFolder(f.folder()));
    assert_eq!(wrong_revision.state,ControlState::Rejected);assert_eq!(wrong_revision.error,Some("stale_revision"));
    assert_eq!(runtime.revision(),Clock(3));f.assert_no_effects();
}
#[test] fn broker_confirmation_api_does_not_gain_a_remote_operator_issuer(){
    let f=Fixture::new(Fault::None,false);let mut runtime=f.runtime.lock().unwrap();
    for identity in ["operator","renderer"] {
        assert!(runtime.confirm_for_operator(identity,"issuer-probe",ControlAction::SetFolder(f.folder()),Duration::from_secs(1)).is_err());
    }
    assert_eq!(runtime.revision(),Clock(3));f.assert_no_effects();
}
#[test] fn authenticated_broker_cannot_replay_the_local_operator_receipt(){run(async {
    let f=Fixture::new(Fault::None,false);let wire=f.approve("cross-origin",ControlAction::SetFolder(f.folder()));
    {
        let mut runtime=f.runtime.lock().unwrap();
        let local=runtime.handle_operator("cross-origin",Clock(7),Clock(3),ControlAction::SetFolder(f.folder()));
        assert_eq!(local.state,ControlState::Committed);assert_eq!(local.revision,Clock(5));
    }
    let (broker,bootstrap)=f.broker().await;let mut peer=authenticated(&bootstrap).await;
    let denied=call(&mut peer,wire).await;assert_eq!(denied["state"],"rejected");assert_eq!(denied["error"],"request_id_conflict");
    assert_eq!(*f.events.lock().unwrap(),["set_folder"]);drop(peer);drop(broker);tokio::task::yield_now().await;
});}
#[test] fn local_operator_flush_failure_prevents_dispatch_and_commit(){
    let f=Fixture::new(Fault::None,true);let mut runtime=f.runtime.lock().unwrap();
    let blocked=runtime.handle_operator("local-flush",Clock(7),Clock(3),ControlAction::SetFolder(f.folder()));
    assert_eq!(blocked.state,ControlState::Blocked);assert_eq!(blocked.error,Some("intent_not_durable"));assert_eq!(runtime.revision(),Clock(3));f.assert_no_effects();
}

#[test]
fn bootstrap_authenticates_under_a_canonical_rendezvous_longer_than_unix_sun_len(){run(async{
 let fixture=Fixture::new(Fault::None,false);
 let parent=fixture.directory.root.join("canonical-private-root-".repeat(7));std::fs::create_dir(&parent).unwrap();
 let directory=parent.join("rendezvous");assert!(directory.as_os_str().as_encoded_bytes().len()>108);
 let authority=BrokerAuthority::start_control(&directory,"127.0.0.1",7,Box::new(ControlBroker(fixture.runtime.clone()))).await.unwrap();
 let bootstrap=private_bootstrap(&directory).await;
 let peer=authenticated(&bootstrap).await;drop(peer);drop(authority);fixture.assert_no_effects();
});}

#[test]
fn native_folder_selection_grants_only_the_chosen_canonical_directory(){
 let f=Fixture::new(Fault::None,false);let chosen=f.directory.root.join("chosen");std::fs::create_dir(&chosen).unwrap();
 let mut runtime=f.runtime.lock().unwrap();assert_eq!(runtime.revision(),Clock(3));
 assert_eq!(runtime.handle_operator("raw-before",Clock(7),Clock(3),ControlAction::SetFolder(chosen.clone())).state,ControlState::Rejected);
 let reply=runtime.handle_operator_folder("native-folder",Clock(7),Clock(3),chosen.clone());assert_eq!(reply.state,ControlState::Committed);assert_eq!(reply.revision,Clock(5));assert!(runtime.folder_configured());
 assert_eq!(runtime.handle_operator_folder("native-folder",Clock(7),Clock(3),chosen).state,ControlState::Committed);
 assert_eq!(runtime.revision(),Clock(5));assert_eq!(*f.events.lock().unwrap(),vec!["set_folder"]);
}
#[test]
fn stale_native_folder_requests_never_grant_a_new_root(){
 let f=Fixture::new(Fault::None,false);let chosen=f.directory.root.join("chosen");std::fs::create_dir(&chosen).unwrap();let mut runtime=f.runtime.lock().unwrap();
 assert_eq!(runtime.handle_operator_folder("wrong-epoch",Clock(8),Clock(3),chosen.clone()).state,ControlState::Rejected);
 assert_eq!(runtime.handle_operator_folder("wrong-revision",Clock(7),Clock(2),chosen.clone()).state,ControlState::Rejected);
 assert_eq!(runtime.handle_operator("raw-after",Clock(7),Clock(3),ControlAction::SetFolder(chosen)).state,ControlState::Rejected);
 assert_eq!(runtime.revision(),Clock(3));assert!(!runtime.folder_configured());f.assert_no_effects();
}
#[test]
fn native_folder_relative_paths_are_rejected_before_durable_intent(){
 let f=Fixture::new(Fault::None,false);let mut runtime=f.runtime.lock().unwrap();
 assert_eq!(runtime.handle_operator_folder("relative",Clock(7),Clock(3),PathBuf::from("relative")).state,ControlState::Rejected);
 assert_eq!(runtime.revision(),Clock(3));assert!(!runtime.folder_configured());f.assert_no_effects();
}
