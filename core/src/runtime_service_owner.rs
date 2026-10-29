//! Private Python service child owned by the authority, never by a renderer.
use std::{path::{Path,PathBuf,Component},io::Read,time::{Duration,Instant}};
use serde_json::{json,Value};
use sha2::{Digest,Sha256};
use crate::{authority_auth::make_frame,python_connector_supervisor::ConnectorSupervisor};

const PYTHON_ARCHIVE:&str="7aba3985d4cee1c7cacb963ca6aa08a5d3e695d7f9609d9fabc56f22ddcf5a06";
const WEBSOCKETS_WHEEL:&str="6aa59f0ef92e796b2db6f5f26550c4713c0e4036899fadf02f55e2ed4db0b7ae";
const BOOTSTRAP:&str="import sys;sys.path.insert(0,sys.argv[1]);from cafe.runtime_service_host import main;sys.argv=[sys.argv[0],sys.argv[2]];main()";

fn no_links(path:&Path)->bool {
    path.ancestors().all(|p|std::fs::symlink_metadata(p).is_ok_and(|m|{
        if m.file_type().is_symlink(){return false;}
        #[cfg(windows)]{use std::os::windows::fs::MetadataExt;if m.file_attributes()&0x400!=0{return false;}}
        true
    }))
}
fn regular(root:&Path,name:&str)->Result<PathBuf,&'static str>{
    if name.is_empty()||name.contains(['\\',':','\0'])||Path::new(name).components().any(|p|!matches!(p,Component::Normal(_))) {return Err("service_resource_rejected");}
    let path=root.join(name);
    if !no_links(&path)||!path.is_file()||std::fs::canonicalize(&path).ok().as_ref()!=Some(&path){return Err("service_resource_rejected");}
    Ok(path)
}
fn document(root:&Path,name:&str)->Result<Value,&'static str>{
    let path=regular(root,name)?;
    if std::fs::metadata(&path).map_err(|_|"service_manifest_rejected")?.len()>8*1024*1024{return Err("service_manifest_rejected");}
    let bytes=std::fs::read(path).map_err(|_|"service_manifest_rejected")?;
    serde_json::from_slice(&bytes).map_err(|_|"service_manifest_rejected")
}
fn pinned_file(root:&Path,name:&str,expected:&str,bytes:Option<u64>)->Result<(),&'static str>{
    if expected.len()!=64||!expected.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b)){return Err("service_pin_rejected");}
    let path=regular(root,name)?;let mut file=std::fs::File::open(path).map_err(|_|"service_pin_rejected")?;
    let size=file.metadata().map_err(|_|"service_pin_rejected")?.len();
    if size>64*1024*1024||bytes.is_some_and(|n|n!=size){return Err("service_pin_rejected");}
    let mut hash=Sha256::new();let mut buffer=[0u8;65536];let mut total=0u64;
    loop{let count=file.read(&mut buffer).map_err(|_|"service_pin_rejected")?;if count==0{break;}total=total.checked_add(count as u64).ok_or("service_pin_rejected")?;if total>size{return Err("service_pin_rejected");}hash.update(&buffer[..count]);}
    if total!=size||hash.finalize().iter().map(|byte|format!("{byte:02x}")).collect::<String>()!=expected{return Err("service_pin_rejected");}Ok(())
}
struct Program {python:PathBuf,connectors:PathBuf}
impl Program {
    fn at(resources:&Path)->Result<Self,&'static str>{
        if !resources.is_absolute()||!no_links(resources){return Err("service_resource_rejected");}
        #[cfg(windows)]{use std::path::Prefix;if !matches!(resources.components().next(),Some(Component::Prefix(p)) if matches!(p.kind(),Prefix::Disk(_)|Prefix::VerbatimDisk(_))){return Err("service_resource_rejected");}}
        let root=std::fs::canonicalize(resources).map_err(|_|"service_resource_rejected")?;
        let manifest=document(&root,"runtime-manifest.json")?;
        if manifest["schema"]!="cafe.private-python.v1"||manifest["version"]!="3.13.16+20261013"
            ||manifest["archive_bytes"]!=21987516_u64||manifest["archive_sha256"]!=PYTHON_ARCHIVE
            ||manifest["websockets"]["version"]!="17.2"||manifest["websockets"]["wheel_sha256"]!=WEBSOCKETS_WHEEL{return Err("service_runtime_pin_rejected");}
        let mut total=0u64;let mut count=0usize;
        for group in [&manifest["files"],&manifest["websockets"]["files"]] {
            let files=group.as_object().ok_or("service_manifest_rejected")?;
            for (name,pin) in files {
                if !name.starts_with("python/"){return Err("service_resource_rejected");}
                count+=1;let bytes=pin["bytes"].as_u64().ok_or("service_manifest_rejected")?;
                total=total.checked_add(bytes).ok_or("service_manifest_rejected")?;
                if count>16384||total>384*1024*1024{return Err("service_manifest_rejected");}
                pinned_file(&root,name,pin["sha256"].as_str().ok_or("service_manifest_rejected")?,Some(bytes))?;
            }
        }
        for principal in ["python/python.exe","python/python3.dll","python/python313.dll"] {
            if manifest["files"].get(principal).is_none(){return Err("service_manifest_rejected");}
        }
        let receipt=document(&root,"cafe-build.json")?;
        if receipt["schema"]!="cafe.unsigned-windows-app.v1"{return Err("service_manifest_rejected");}
        let sources=receipt["source_files"].as_object().ok_or("service_manifest_rejected")?;
        for required in ["runtime_service_host","runtime_service_auth","runtime_services","channel_auth","obs_auth","obs_bridge","twitch_chat","twitch_storage","windows_auth"] {
            if !sources.contains_key(&format!("connectors/cafe/{required}.py")){return Err("service_source_pin_rejected");}
        }
        for (name,pin) in sources.iter().filter(|(name,_)|name.starts_with("connectors/")) {
            if !name.ends_with(".py"){return Err("service_source_pin_rejected");}
            pinned_file(&root,name,pin.as_str().ok_or("service_source_pin_rejected")?,None)?;
        }
        Ok(Self{python:regular(&root,"python/python.exe")?,connectors:root.join("connectors")})
    }
    fn packaged()->Result<Self,&'static str>{
        let binary=std::env::current_exe().map_err(|_|"service_resource_rejected")?;
        let resources=binary.parent().and_then(Path::parent).ok_or("service_resource_rejected")?;
        Self::at(resources)
    }
    fn args(&self,root:&Path)->Result<Vec<String>,&'static str>{
        Ok(vec!["-I".into(),"-B".into(),"-c".into(),BOOTSTRAP.into(),self.connectors.to_str().ok_or("service_resource_rejected")?.into(),root.to_str().ok_or("service_resource_rejected")?.into()])
    }
}

pub(crate) struct ServiceOwner {
    program:Option<Program>,root:PathBuf,epoch:u64,child:Option<ConnectorSupervisor>,
    body:Option<Value>,last_poll:Instant,
}
impl ServiceOwner {
    pub(crate) fn packaged(root:PathBuf,epoch:u64)->Self {
        Self{program:Program::packaged().ok(),root,epoch,child:None,body:None,last_poll:Instant::now()}
    }
    pub(crate) fn configurable(&self)->bool{self.program.is_some()}
    pub(crate) fn obs_ready(&self)->bool{self.body.as_ref().is_some_and(|b|b["obs"]=="configured"&&!b["closed"].as_bool().unwrap_or(true))}
    pub(crate) fn chat_running(&self)->bool{self.body.as_ref().is_some_and(|b|b["chat"]=="running")}
    pub(crate) fn chat_ready(&self)->bool{self.body.as_ref().is_some_and(|b|b["chat"]=="running"&&b["chat_connected"]==true&&b["messages"].as_str().and_then(|v|v.parse::<u64>().ok()).is_some_and(|n|n>0))}
    pub(crate) fn projection(&self)->Value{
        let b=self.body.as_ref();json!({"available":self.child.is_some()&&b.is_some(),"obs":b.map(|v|v["obs"].clone()),"chat":b.map(|v|v["chat"].clone()),"messages":b.map(|v|v["messages"].clone()),"connections":b.map(|v|v["connections"].clone())})
    }
    async fn ensure(&mut self)->Result<(),&'static str>{
        if let Some(child)=self.child.as_mut(){
            if child.status()["available"]==true{return Ok(());}
            self.body=None;
            return child.restart().await.map_err(|_|"service_restart_failed");
        }
        let program=self.program.as_ref().ok_or("private_python_unavailable")?;
        let args=program.args(&self.root)?;
        let child=ConnectorSupervisor::spawn(program.python.to_str().ok_or("service_resource_rejected")?,&args,self.epoch).await.map_err(|_|"service_start_failed")?;
        self.child=Some(child);Ok(())
    }
    pub(crate) async fn request(&mut self,body:Value)->Result<(),&'static str>{
        crate::authority_auth::validate_authority_frame(&make_frame("service_request",self.epoch,body.clone()),self.epoch).map_err(|_|"service_request_rejected")?;
        self.ensure().await?;
        let child=self.child.as_mut().ok_or("service_unavailable")?;
        let epoch=child.status()["authority_epoch"].as_str().and_then(|s|s.parse::<u64>().ok()).ok_or("service_epoch_rejected")?;
        let response=match child.exchange(make_frame("service_request",epoch,body.clone())).await {
            Ok(value)=>value,Err(_)=>{self.body=None;child.stop().await;return Err("service_exchange_failed");}
        };
        if response["kind"]!="service_result"{self.body=None;child.stop().await;return Err("service_reply_rejected");}
        let state=response["body"].clone();let accepted=state["accepted"]==true;
        if (body["op"]=="configure_obs"&&accepted&&state["obs"]!="configured")
            ||(body["op"]=="start_chat"&&accepted&&state["chat"]!="running") {
            self.body=None;child.stop().await;return Err("service_reply_rejected");
        }
        self.body=Some(state);
        if accepted{Ok(())}else{Err("service_operation_rejected")}
    }
    pub(crate) async fn poll(&mut self)->bool{
        if self.child.is_none()||self.last_poll.elapsed()<Duration::from_secs(1){return false;}
        self.last_poll=Instant::now();let prior=self.body.clone();let _=self.request(json!({"op":"status"})).await;
        prior!=self.body
    }
    pub(crate) async fn close(&mut self){
        if self.child.is_none(){return;}
        if self.child.as_mut().is_some_and(|child|child.status()["available"]==true){let _=self.request(json!({"op":"close"})).await;}
        if let Some(child)=self.child.as_mut(){child.stop().await;}
        self.body=None;
    }
}

#[derive(Clone,PartialEq,Eq,PartialOrd,Ord)]
pub(crate) enum ServiceKey {Configuration(String),Effect(String)}
struct Work {key:ServiceKey,body:Value}
enum Update {State(Option<Value>),Done(ServiceKey,bool)}
pub(crate) struct ServicePermit(tokio::sync::mpsc::OwnedPermit<Work>);
pub(crate) struct ServiceClient {
    requests:tokio::sync::mpsc::Sender<Work>,updates:tokio::sync::mpsc::Receiver<Update>,
    pending:std::collections::BTreeSet<ServiceKey>,state:Option<Value>,available:bool,
    task:tokio::task::JoinHandle<()>,stop:Option<tokio::sync::oneshot::Sender<()>>,
}
impl ServiceClient {
    pub(crate) fn packaged(root:PathBuf,epoch:u64)->Self {
        let owner=ServiceOwner::packaged(root,epoch);Self::spawn(owner)
    }
    fn spawn(mut owner:ServiceOwner)->Self{
        let available=owner.configurable();
        let (requests,mut rx)=tokio::sync::mpsc::channel::<Work>(8);
        let (tx,updates)=tokio::sync::mpsc::channel(16);
        let (stop,mut stopped)=tokio::sync::oneshot::channel();
        let task=tokio::spawn(async move{
            loop{
                if stopped.try_recv().is_ok(){break;}
                match rx.try_recv(){
                    Ok(work)=>{
                        let applied=owner.request(work.body).await.is_ok();
                        if tx.send(Update::State(owner.body.clone())).await.is_err(){break;}
                        if tx.send(Update::Done(work.key,applied)).await.is_err(){break;}
                    },
                    Err(tokio::sync::mpsc::error::TryRecvError::Disconnected)=>break,
                    Err(tokio::sync::mpsc::error::TryRecvError::Empty)=>{
                        if owner.poll().await&&tx.send(Update::State(owner.body.clone())).await.is_err(){break;}
                        tokio::time::sleep(Duration::from_millis(5)).await;
                    },
                }
            }
            owner.close().await;
        });
        Self{requests,updates,pending:std::collections::BTreeSet::new(),state:None,available,task,stop:Some(stop)}
    }
    pub(crate) fn configurable(&self)->bool{self.available&&!self.task.is_finished()}
    pub(crate) fn obs_ready(&self)->bool{self.state.as_ref().is_some_and(|b|b["obs"]=="configured"&&b["closed"]==false)}
    pub(crate) fn chat_running(&self)->bool{self.state.as_ref().is_some_and(|b|b["chat"]=="running")}
    pub(crate) fn chat_ready(&self)->bool{self.state.as_ref().is_some_and(|b|b["chat"]=="running"&&b["chat_connected"]==true&&b["messages"].as_str().and_then(|s|s.parse::<u64>().ok()).is_some_and(|n|n>0))}
    pub(crate) fn projection(&self)->Value{
        let b=self.state.as_ref();json!({"available":b.is_some(),"obs":b.map(|v|v["obs"].clone()),"chat":b.map(|v|v["chat"].clone()),"messages":b.map(|v|v["messages"].clone()),"connections":b.map(|v|v["connections"].clone())})
    }
    pub(crate) fn reserve(&self,key:&ServiceKey)->Result<ServicePermit,&'static str>{
        if !self.configurable()||self.pending.len()>=8||self.pending.contains(key){return Err("service_queue_unavailable");}
        self.requests.clone().try_reserve_owned().map(ServicePermit).map_err(|_|"service_queue_unavailable")
    }
    pub(crate) fn submit(&mut self,permit:ServicePermit,key:ServiceKey,body:Value){
        self.pending.insert(key.clone());permit.0.send(Work{key,body});
    }
    pub(crate) fn drain(&mut self)->(Vec<(ServiceKey,bool)>,bool){
        let mut done=Vec::new();let mut changed=false;
        for _ in 0..16{
            match self.updates.try_recv(){
                Ok(Update::State(value))=>{changed|=self.state!=value;self.state=value;},
                Ok(Update::Done(key,applied))=>{if self.pending.remove(&key){done.push((key,applied));}else{self.state=None;changed=true;}},
                Err(_)=>break,
            }
        }
        if self.updates.is_closed()||self.task.is_finished(){
            self.state=None;changed=true;
            done.extend(std::mem::take(&mut self.pending).into_iter().map(|key|(key,false)));
        }
        (done,changed)
    }
    pub(crate) async fn close(&mut self){
        let Some(stop)=self.stop.take()else{return;};let _=stop.send(());
        if tokio::time::timeout(Duration::from_secs(12),&mut self.task).await.is_err(){self.task.abort();let _=(&mut self.task).await;}
        self.state=None;
    }
}
impl Drop for ServiceClient{fn drop(&mut self){self.task.abort();}}

#[cfg(test)]mod resource_tests{
    use super::*;
    use std::sync::atomic::{AtomicU64,Ordering};
    static NEXT:AtomicU64=AtomicU64::new(0);
    struct Scratch(PathBuf);
    impl Scratch{fn new()->Self{
        let path=std::env::temp_dir().canonicalize().unwrap().join(format!("cafe-service-pins-{}-{}",std::process::id(),NEXT.fetch_add(1,Ordering::SeqCst)));
        std::fs::create_dir(&path).unwrap();Self(path)
    }}
    impl Drop for Scratch{fn drop(&mut self){std::fs::remove_dir_all(&self.0).unwrap();}}
    #[test]fn explicit_resource_paths_and_stream_hashes_fail_closed(){
        let root=Scratch::new();std::fs::write(root.0.join("sample"),b"abc").unwrap();
        const HASH:&str="ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
        assert!(pinned_file(&root.0,"sample",HASH,Some(3)).is_ok());
        for name in ["../sample","/sample","C:sample","bad\\sample",""]{assert!(regular(&root.0,name).is_err());}
        assert!(pinned_file(&root.0,"sample",HASH,Some(4)).is_err());
        std::fs::write(root.0.join("sample"),b"abd").unwrap();assert!(pinned_file(&root.0,"sample",HASH,Some(3)).is_err());
        assert!(Program::at(&root.0).is_err());
    }
}
