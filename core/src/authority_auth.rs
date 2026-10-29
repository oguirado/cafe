//! Bounded private connector frames and one-use broker HMAC admission.
//! Owner-only Unix sockets or Windows named pipes deliver the one-use secret.
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use std::sync::{Arc, atomic::{AtomicBool, AtomicU64, Ordering}};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::sync::Mutex;
use tokio::task::JoinHandle;
pub const MAX_LINE: usize = 16384;
const DEADLINE: Duration = Duration::from_secs(5);
pub(crate) const CONTROL_REPLY_DEADLINE: Duration = DEADLINE;

fn keys(v: &Value, expected: &[&str]) -> bool {
    v.as_object().is_some_and(|o| o.len()==expected.len() && expected.iter().all(|k| o.contains_key(*k)))
}
fn clock(v: &Value) -> Option<u64> {
    let s=v.as_str()?;
    if s.is_empty() || (s.len()>1 && s.starts_with('0')) || !s.bytes().all(|b| b.is_ascii_digit()) {return None;}
    s.parse().ok()
}
// Decode recursively without silently replacing duplicate object keys.
struct UniqueJson(Value);
impl<'de> serde::Deserialize<'de> for UniqueJson {
    fn deserialize<D:serde::Deserializer<'de>>(deserializer:D)->Result<Self,D::Error>{
        struct Visitor;
        impl<'de> serde::de::Visitor<'de> for Visitor {
            type Value=UniqueJson;
            fn expecting(&self,f:&mut std::fmt::Formatter<'_>)->std::fmt::Result{f.write_str("unambiguous JSON")}
            fn visit_bool<E:serde::de::Error>(self,v:bool)->Result<Self::Value,E>{Ok(UniqueJson(Value::Bool(v)))}
            fn visit_i64<E:serde::de::Error>(self,v:i64)->Result<Self::Value,E>{Ok(UniqueJson(Value::Number(v.into())))}
            fn visit_u64<E:serde::de::Error>(self,v:u64)->Result<Self::Value,E>{Ok(UniqueJson(Value::Number(v.into())))}
            fn visit_f64<E:serde::de::Error>(self,v:f64)->Result<Self::Value,E>{serde_json::Number::from_f64(v).map(|v|UniqueJson(Value::Number(v))).ok_or_else(||E::custom("invalid_json_number"))}
            fn visit_str<E:serde::de::Error>(self,v:&str)->Result<Self::Value,E>{Ok(UniqueJson(Value::String(v.into())))}
            fn visit_string<E:serde::de::Error>(self,v:String)->Result<Self::Value,E>{Ok(UniqueJson(Value::String(v)))}
            fn visit_none<E:serde::de::Error>(self)->Result<Self::Value,E>{Ok(UniqueJson(Value::Null))}
            fn visit_unit<E:serde::de::Error>(self)->Result<Self::Value,E>{Ok(UniqueJson(Value::Null))}
            fn visit_seq<A:serde::de::SeqAccess<'de>>(self,mut a:A)->Result<Self::Value,A::Error>{let mut values=Vec::new();while let Some(v)=a.next_element::<UniqueJson>()?{values.push(v.0);}Ok(UniqueJson(Value::Array(values)))}
            fn visit_map<A:serde::de::MapAccess<'de>>(self,mut a:A)->Result<Self::Value,A::Error>{
                let mut values=serde_json::Map::new();
                while let Some(key)=a.next_key::<String>()?{
                    if values.contains_key(&key){return Err(serde::de::Error::custom("duplicate_json_key"));}
                    values.insert(key,a.next_value::<UniqueJson>()?.0);
                }
                Ok(UniqueJson(Value::Object(values)))
            }
        }
        deserializer.deserialize_any(Visitor)
    }
}
pub async fn read_frame<R: AsyncRead+Unpin>(r: &mut R) -> Result<Option<Value>,String> {
    tokio::time::timeout(DEADLINE, async {
        let mut data=Vec::with_capacity(256); let mut byte=[0u8;1];
        loop {
            let n=r.read(&mut byte).await.map_err(|_|"read_failed".to_owned())?;
            if n==0 {return if data.is_empty(){Ok(None)}else{Err("incomplete_frame".into())};}
            data.push(byte[0]);
            if data.len()>MAX_LINE {return Err("frame_too_large".into());}
            if byte[0]==b'\n' {return serde_json::from_slice::<UniqueJson>(&data).map(|v|Some(v.0)).map_err(|_|"invalid_json".into());}
        }
    }).await.map_err(|_|"read_timeout".to_owned())?
}
// Authenticated sessions may be idle; once a frame starts its body remains deadline-bounded.
async fn read_authenticated_frame<R:AsyncRead+Unpin>(r:&mut R)->Result<Option<Value>,String>{
    let mut first=[0u8;1];
    let n=r.read(&mut first).await.map_err(|_|"read_failed".to_owned())?;
    if n==0 {return Ok(None);}
    tokio::time::timeout(DEADLINE,async {
        let mut data=vec![first[0]];let mut byte=[0u8;1];
        loop {
            if data.len()>MAX_LINE {return Err("frame_too_large".into());}
            if data.last()==Some(&b'\n') {return serde_json::from_slice::<UniqueJson>(&data).map(|v|Some(v.0)).map_err(|_|"invalid_json".into());}
            let n=r.read(&mut byte).await.map_err(|_|"read_failed".to_owned())?;
            if n==0 {return Err("incomplete_frame".into());}
            data.push(byte[0]);
        }
    }).await.map_err(|_|"read_timeout".to_owned())?
}
pub async fn write_frame<W: AsyncWrite+Unpin>(w: &mut W, value: &Value) -> Result<(),String> {
    let mut data=serde_json::to_vec(value).map_err(|_|"encode_failed".to_owned())?;
    if data.len()+1>MAX_LINE {return Err("frame_too_large".into());} data.push(b'\n');
    tokio::time::timeout(DEADLINE,async {w.write_all(&data).await?;w.flush().await}).await.map_err(|_|"write_timeout".to_owned())?.map_err(|_|"write_failed".into())
}
pub fn make_frame(kind: &str, epoch: u64, body: Value) -> Value {
    json!({"schema":"cafe.connector.v1","kind":kind,"request_id":"channel","authority_epoch":epoch.to_string(),"expected_revision":"0","body":body})
}
fn validate_base(v: &Value, epoch:u64)->Result<(),String>{
    if epoch==0 || !keys(v,&["schema","kind","request_id","authority_epoch","expected_revision","body"]) || v["schema"]!="cafe.connector.v1" || v["request_id"]!="channel" || v["expected_revision"]!="0" || clock(&v["authority_epoch"])!=Some(epoch) || !v["body"].is_object(){return Err("invalid_connector_frame".into());} Ok(())
}
pub fn validate_authority_frame(v:&Value, epoch:u64)->Result<(),String>{
    validate_base(v,epoch)?;let b=&v["body"];
    let valid=match v["kind"].as_str(){
        Some("connector_start")=>keys(b,&[]),
        Some("approved_effect")=>keys(b,&["action"]) && b["action"].as_str().is_some_and(|s|!s.is_empty()&&s.len()<=64),
        Some("token_lease")=>keys(b,&["token","expires_unix_ms"]) && b["token"].as_str().is_some_and(|s|!s.is_empty()&&s.len()<=2048) && clock(&b["expires_unix_ms"]).is_some_and(|t|now_ms().is_ok_and(|n|t>n&&t-n<=5000)),
        _=>false};if valid{Ok(())}else{Err("connector_capability_denied".into())}
}
pub fn validate_connector_frame(v:&Value,epoch:u64)->Result<(),String>{
    validate_base(v,epoch)?;let valid=match v["kind"].as_str(){Some("observation")=>keys(&v["body"],&["ready"])&&v["body"]["ready"].is_boolean(),Some("effect_result")=>keys(&v["body"],&["accepted"])&&v["body"]["accepted"].is_boolean(),_=>false};if valid{Ok(())}else{Err("connector_capability_denied".into())}
}
fn now_ms()->Result<u64,String>{SystemTime::now().duration_since(UNIX_EPOCH).ok().and_then(|d|u64::try_from(d.as_millis()).ok()).ok_or_else(||"clock_unavailable".into())}
pub fn lease_frame(epoch:u64,token:&str,ttl_ms:u64,sink:&str)->Result<Value,String>{
    if sink!="private" || !(1..=5000).contains(&ttl_ms) || token.is_empty() || token.len()>2048{return Err("lease_route_denied".into());}
    let expiry=now_ms()?.checked_add(ttl_ms).ok_or("clock_overflow")?;
    let frame=make_frame("token_lease",epoch,json!({"token":token,"expires_unix_ms":expiry.to_string()}));validate_authority_frame(&frame,epoch)?;Ok(frame)
}
fn hex(bytes:&[u8])->String{bytes.iter().map(|b|format!("{b:02x}")).collect()}
fn unhex(s:&str)->Option<Vec<u8>>{if s.len()!=64 || !s.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b)){return None;} (0..s.len()).step_by(2).map(|i|u8::from_str_radix(&s[i..i+2],16).ok()).collect()}
pub fn hmac_sha256(key:&[u8],message:&[u8])->[u8;32]{
    let mut k=[0u8;64];if key.len()>64{k[..32].copy_from_slice(&Sha256::digest(key));}else{k[..key.len()].copy_from_slice(key);}
    let mut inner=Sha256::new();let mut outer=Sha256::new();let mut ipad=[0u8;64];let mut opad=[0u8;64];for i in 0..64{ipad[i]=k[i]^0x36;opad[i]=k[i]^0x5c;}inner.update(ipad);inner.update(message);outer.update(opad);outer.update(inner.finalize());k.fill(0);ipad.fill(0);opad.fill(0);outer.finalize().into()
}
fn equal(a:&[u8],b:&[u8])->bool{if a.len()!=b.len(){return false;}a.iter().zip(b).fold(0u8,|x,(a,b)|x|(a^b))==0}
// The same provider contract is exercised by real and simulated OS calls.
fn entropy_using(fill: fn(&mut [u8;32])->Result<(),String>)->Result<[u8;32],String>{
    let mut bytes=[0;32];
    if let Err(error)=fill(&mut bytes){bytes.fill(0);return Err(error);}
    Ok(bytes)
}
#[cfg(unix)]
fn fill_entropy(bytes:&mut [u8;32])->Result<(),String>{
    use std::io::Read;
    std::fs::File::open("/dev/urandom").and_then(|mut f|f.read_exact(bytes))
        .map_err(|_|"entropy_unavailable".to_owned())
}
#[cfg(windows)]
fn fill_entropy(bytes:&mut [u8;32])->Result<(),String>{
    use windows::Win32::Security::Cryptography::{BCryptGenRandom,BCRYPT_USE_SYSTEM_PREFERRED_RNG};
    // SAFETY: a writable fixed buffer and NULL provider match the system-RNG flag.
    unsafe { BCryptGenRandom(None,bytes,BCRYPT_USE_SYSTEM_PREFERRED_RNG) }.ok()
        .map_err(|_|"entropy_unavailable".to_owned())
}
#[cfg(not(any(unix,windows)))]
fn fill_entropy(_bytes:&mut [u8;32])->Result<(),String>{Err("entropy_unsupported".into())}
pub(crate) fn entropy()->Result<[u8;32],String>{entropy_using(fill_entropy)}
#[cfg(test)]
pub(crate) fn test_entropy()->Result<[u8;32],String>{entropy()}
#[cfg(test)]
pub(crate) fn test_failed_entropy()->Result<[u8;32],String>{
    fn failed(bytes:&mut [u8;32])->Result<(),String>{bytes.fill(7);Err("simulated_os_failure".into())}
    entropy_using(failed)
}
#[cfg(windows)]
#[path="windows_auth.rs"]
pub(crate) mod windows_auth;

/// Created only inside the successful HMAC branch; request data never constructs it.
#[derive(Clone)]
pub(crate) struct AuthenticatedBrokerPeer { epoch: u64, live:Arc<AtomicBool>, connection_live:Arc<AtomicBool>, request_live:Option<Arc<AtomicBool>>, request_deadline:Option<Instant> }
impl AuthenticatedBrokerPeer {
    pub(crate) fn identity(&self) -> &'static str { "broker" }
    pub(crate) fn epoch(&self) -> u64 { self.epoch }
    pub(crate) fn is_live(&self)->bool { self.live.load(Ordering::Acquire) && self.connection_live.load(Ordering::Acquire) && self.request_live.as_ref().is_none_or(|flag|flag.load(Ordering::Acquire)) && self.request_deadline.is_none_or(|deadline|Instant::now()<deadline) }
    pub(crate) fn for_actor_request(&self,active:Arc<AtomicBool>,deadline:Instant)->Option<Self> {
        if !self.is_live() || self.request_live.is_some(){return None;}
        Some(Self{epoch:self.epoch,live:self.live.clone(),connection_live:self.connection_live.clone(),request_live:Some(active),request_deadline:Some(deadline)})
    }
}

/// A real authenticated request consumer; implementations own typed effect dispatch.
pub(crate) trait ControlHandler: Send {
    fn handle(&mut self, peer: &AuthenticatedBrokerPeer, intent: &Value) -> Value;
}

// Connection cancellation is independent of authority lifetime and request deadlines.
struct RevokeConnection(Arc<AtomicBool>);
impl Drop for RevokeConnection {
    fn drop(&mut self) { self.0.store(false,Ordering::Release); }
}
struct ConnectionReader {
    live: Arc<AtomicBool>,
    task: JoinHandle<()>,
}
impl Drop for ConnectionReader {
    fn drop(&mut self) {
        self.live.store(false,Ordering::Release);
        self.task.abort(); // Only the Tokio task created for this connection.
    }
}
enum ControlFrame { Request(Value), Denied }
async fn serve_control_connection(stream:tokio::net::TcpStream,peer:AuthenticatedBrokerPeer,handler:Arc<std::sync::Mutex<Box<dyn ControlHandler>>>) {
    let (mut input,mut output)=stream.into_split();
    // Exactly one lookahead frame; overflow revokes instead of waiting unboundedly.
    let (sender,mut receiver)=tokio::sync::mpsc::channel(1);
    let reader_live=peer.connection_live.clone();let epoch=peer.epoch;
    let task=tokio::spawn(async move {
        let _revoke=RevokeConnection(reader_live);
        loop {
            let request=match read_authenticated_frame(&mut input).await {Ok(Some(v))=>v,_=>break};
            let valid=keys(&request,&["epoch","capability","body"]) && clock(&request["epoch"])==Some(epoch)
                && request["capability"]=="control" && request["body"].is_object();
            if !valid { let _=sender.try_send(ControlFrame::Denied); break; }
            if sender.try_send(ControlFrame::Request(request)).is_err(){break;}
        }
    });
    // Dropped on ordinary exit, connection future cancellation and authority abort.
    let _reader=ConnectionReader{live:peer.connection_live.clone(),task};
    while let Some(frame)=receiver.recv().await {
        let request=match frame {
            ControlFrame::Denied=>{let _=write_frame(&mut output,&json!({"error":"capability_denied"})).await;break;},
            ControlFrame::Request(value)=>value,
        };
        if !peer.is_live(){break;}
        let handler=handler.clone();let owned_peer=peer.clone();let body=request["body"].clone();
        // Only this loop starts callbacks, serially. The reader remains active on EOF.
        let work=tokio::task::spawn_blocking(move ||{
            if !owned_peer.is_live(){return None;}
            let mut handler=handler.lock().ok()?;
            if !owned_peer.is_live(){return None;}
            let response=handler.handle(&owned_peer,&body);
            if serde_json::to_vec(&response).ok()?.len()+1>MAX_LINE{return None;}
            Some(response)
        });
        let response=match tokio::time::timeout(CONTROL_REPLY_DEADLINE,work).await {Ok(Ok(Some(response)))=>response,_=>break};
        if !peer.is_live(){break;}
        if write_frame(&mut output,&response).await.is_err(){break;}
    }
}

/// Owns listeners and only its private installation rendezvous directory.
pub struct BrokerAuthority {port:u16,epoch:u64,effects:Arc<AtomicU64>,tasks:Vec<JoinHandle<()>>,dir:PathBuf,live:Arc<AtomicBool>}
impl BrokerAuthority {
    #[cfg(any(unix,windows))]
    pub async fn start(dir:&Path,bind:&str,epoch:u64)->Result<Self,String>{
        Self::start_inner(dir,bind,epoch,None).await
    }
    #[cfg(any(unix,windows))]
    pub(crate) async fn start_control(dir:&Path,bind:&str,epoch:u64,handler:Box<dyn ControlHandler>)->Result<Self,String>{
        Self::start_inner(dir,bind,epoch,Some(Arc::new(std::sync::Mutex::new(handler)))).await
    }
    #[cfg(any(unix,windows))]
    async fn start_inner(dir:&Path,bind:&str,epoch:u64,control:Option<Arc<std::sync::Mutex<Box<dyn ControlHandler>>>>)->Result<Self,String>{
        if bind!="127.0.0.1" || epoch==0{return Err("loopback_only".into());}
        let mut secret=entropy()?;
        let listener=tokio::net::TcpListener::bind(("127.0.0.1",0)).await.map_err(|_|"bind_failed".to_owned())?;
        let port=listener.local_addr().map_err(|_|"address_failed")?.port();
        #[cfg(unix)]
        let uds={
            use std::os::unix::fs::{PermissionsExt,DirBuilderExt};
            use std::os::fd::AsRawFd;
            std::fs::DirBuilder::new().mode(0o700).create(dir).map_err(|_|"rendezvous_create_failed".to_owned())?;
            let directory=std::fs::File::open(dir).map_err(|_|"rendezvous_open_failed")?;
            let bind_path=format!("/proc/self/fd/{}/broker.sock",directory.as_raw_fd());
            let uds=tokio::net::UnixListener::bind(&bind_path).map_err(|_|"rendezvous_bind_failed")?;
            std::fs::set_permissions(dir.join("broker.sock"),std::fs::Permissions::from_mode(0o600)).map_err(|_|"rendezvous_acl_failed")?;
            uds
        };
        #[cfg(windows)]
        let mut pipe=windows_auth::rendezvous(dir,&hex(&entropy()?))?;
        let key=Arc::new(Mutex::new(Some(secret)));let effects=Arc::new(AtomicU64::new(0));
        let live=Arc::new(AtomicBool::new(true));let control_mode=control.is_some();
        let bootstrap=tokio::spawn(async move{
            let value=json!({"schema":"cafe.rendezvous.v1","host":"127.0.0.1","port":port,"epoch":epoch.to_string(),"identity":"broker","secret":hex(&secret)});
            #[cfg(unix)]
            {
                let accepted=if control_mode{uds.accept().await.ok()}else{tokio::time::timeout(Duration::from_secs(30),uds.accept()).await.ok().and_then(Result::ok)};
                if let Some((mut stream,_))=accepted {let _=write_frame(&mut stream,&value).await;}
            }
            #[cfg(windows)]
            {
                let connected=if control_mode{pipe.connect().await.is_ok()}else{matches!(tokio::time::timeout(Duration::from_secs(30),pipe.connect()).await,Ok(Ok(())))};
                if connected && write_frame(&mut pipe,&value).await.is_ok(){
                    // Keep the server handle alive until the receiver has read.
                    let _=read_frame(&mut pipe).await;
                }
            }
            secret.fill(0);
        });
        let count=effects.clone();let serving_live=live.clone();let serving=tokio::spawn(async move{
            loop {let Ok((mut stream,peer))=listener.accept().await else{break;};if peer.ip()!=std::net::Ipv4Addr::LOCALHOST{continue;}
                let Ok(nonce)=entropy() else{continue;};let nonce=hex(&nonce);let challenge=json!({"schema":"cafe.broker.auth.v1","identity":"broker","epoch":epoch.to_string(),"nonce":nonce});
                if write_frame(&mut stream,&challenge).await.is_err(){continue;}
                let Ok(Some(proof))=read_frame(&mut stream).await else{continue;};
                if !keys(&proof,&["identity","epoch","proof"]) || proof["identity"]!="broker" || clock(&proof["epoch"])!=Some(epoch){continue;}
                let Some(received)=proof["proof"].as_str().and_then(unhex) else{continue;};
                let mut key_guard=key.lock().await;
                let Some(secret)=key_guard.as_ref() else{continue;};let input=format!("cafe.broker.auth.v1\nbroker\n{epoch}\n{nonce}");let expected=hmac_sha256(secret,input.as_bytes());
                if !equal(&expected,&received){continue;}
                let Some(mut secret)=key_guard.take() else{continue;};secret.fill(0);drop(key_guard);
                if write_frame(&mut stream,&json!({"authenticated":true})).await.is_err(){continue;}
                let peer=AuthenticatedBrokerPeer{epoch,live:serving_live.clone(),connection_live:Arc::new(AtomicBool::new(true)),request_live:None,request_deadline:None};
                if let Some(handler)=control.as_ref(){
                    serve_control_connection(stream,peer,handler.clone()).await;
                    continue;
                }
                loop {
                    let Ok(Some(request))=read_frame(&mut stream).await else{break;};
                    let base=keys(&request,&["epoch","capability","body"]) && clock(&request["epoch"])==Some(epoch);
                    let valid=control.is_none() && base && keys(&request["body"],&[]) && matches!(request["capability"].as_str(),Some("snapshot"|"propose"));
                    if !valid{let _=write_frame(&mut stream,&json!({"error":"capability_denied"})).await;break;}
                    let n=count.fetch_add(1,Ordering::SeqCst)+1;let _=write_frame(&mut stream,&json!({"accepted":true,"effects":n})).await;break;
                }
                peer.live.store(false,Ordering::Release);
            }
        });
        Ok(Self{port,epoch,effects,tasks:vec![bootstrap,serving],dir:dir.to_path_buf(),live})
    }
    #[cfg(not(any(unix,windows)))]
    pub async fn start(_dir:&Path,_bind:&str,_epoch:u64)->Result<Self,String>{Err("rendezvous_unsupported".into())}
    pub fn status(&self)->Value{json!({"host":"127.0.0.1","port":self.port,"epoch":self.epoch.to_string(),"effects":self.effects.load(Ordering::SeqCst),"rendezvous_ready":true})}
}
impl Drop for BrokerAuthority {fn drop(&mut self){self.live.store(false,Ordering::Release);for t in &self.tasks{t.abort();}let _=std::fs::remove_file(self.dir.join("broker.sock"));let _=std::fs::remove_file(self.dir.join("broker.pipe"));let _=std::fs::remove_dir(&self.dir);}}
