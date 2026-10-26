//! Bounded private connector frames and one-use broker HMAC admission.
//! Linux rendezvous is owner-only; Windows installation validation is pending.
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use std::sync::{Arc, atomic::{AtomicU64, Ordering}};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::sync::Mutex;
use tokio::task::JoinHandle;
pub const MAX_LINE: usize = 16384;
const DEADLINE: Duration = Duration::from_secs(5);

fn keys(v: &Value, expected: &[&str]) -> bool {
    v.as_object().is_some_and(|o| o.len()==expected.len() && expected.iter().all(|k| o.contains_key(*k)))
}
fn clock(v: &Value) -> Option<u64> {
    let s=v.as_str()?;
    if s.is_empty() || (s.len()>1 && s.starts_with('0')) || !s.bytes().all(|b| b.is_ascii_digit()) {return None;}
    s.parse().ok()
}
pub async fn read_frame<R: AsyncRead+Unpin>(r: &mut R) -> Result<Option<Value>,String> {
    tokio::time::timeout(DEADLINE, async {
        let mut data=Vec::with_capacity(256); let mut byte=[0u8;1];
        loop {
            let n=r.read(&mut byte).await.map_err(|_|"read_failed".to_owned())?;
            if n==0 {return if data.is_empty(){Ok(None)}else{Err("incomplete_frame".into())};}
            data.push(byte[0]);
            if data.len()>MAX_LINE {return Err("frame_too_large".into());}
            if byte[0]==b'\n' {return serde_json::from_slice(&data).map(Some).map_err(|_|"invalid_json".into());}
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
#[cfg(unix)]
fn entropy()->Result<[u8;32],String>{use std::io::Read;let mut b=[0;32];std::fs::File::open("/dev/urandom").and_then(|mut f|f.read_exact(&mut b)).map_err(|_|"entropy_unavailable".to_owned())?;Ok(b)}
#[cfg(not(unix))]
fn entropy()->Result<[u8;32],String>{Err("windows_verification_pending".into())}

/// Owns listeners and only its private installation rendezvous directory.
pub struct BrokerAuthority {port:u16,epoch:u64,effects:Arc<AtomicU64>,tasks:Vec<JoinHandle<()>>,dir:PathBuf}
impl BrokerAuthority {
    #[cfg(unix)]
    pub async fn start(dir:&Path,bind:&str,epoch:u64)->Result<Self,String>{
        use std::os::unix::fs::{PermissionsExt, DirBuilderExt};
        use std::os::fd::AsRawFd;
        if bind!="127.0.0.1" || epoch==0{return Err("loopback_only".into());}
        let listener=tokio::net::TcpListener::bind(("127.0.0.1",0)).await.map_err(|_|"bind_failed".to_owned())?;
        let port=listener.local_addr().map_err(|_|"address_failed")?.port();
        std::fs::DirBuilder::new().mode(0o700).create(dir).map_err(|_|"rendezvous_create_failed".to_owned())?;
        let socket=dir.join("broker.sock");
        // Linux fd-relative pathname avoids sockaddr_un limits on long private roots.
        let directory=std::fs::File::open(dir).map_err(|_|"rendezvous_open_failed".to_owned())?;
        let bind_path=format!("/proc/self/fd/{}/broker.sock",directory.as_raw_fd());
        let uds=tokio::net::UnixListener::bind(&bind_path).map_err(|_|"rendezvous_bind_failed".to_owned())?;
        std::fs::set_permissions(&socket,std::fs::Permissions::from_mode(0o600)).map_err(|_|"rendezvous_acl_failed".to_owned())?;
        let mut secret=entropy()?;let key=Arc::new(Mutex::new(Some(secret)));let effects=Arc::new(AtomicU64::new(0));
        let bootstrap=tokio::spawn(async move{
            if let Ok(Ok((mut stream,_)))=tokio::time::timeout(Duration::from_secs(30),uds.accept()).await {
                let value=json!({"schema":"cafe.rendezvous.v1","host":"127.0.0.1","port":port,"epoch":epoch.to_string(),"identity":"broker","secret":hex(&secret)});
                let _=write_frame(&mut stream,&value).await;
            }secret.fill(0);
        });
        let count=effects.clone();let serving=tokio::spawn(async move{
            loop {let Ok((mut stream,peer))=listener.accept().await else{break;};if peer.ip()!=std::net::Ipv4Addr::LOCALHOST{continue;}
                let Ok(nonce)=entropy() else{continue;};let nonce=hex(&nonce);let challenge=json!({"schema":"cafe.broker.auth.v1","identity":"broker","epoch":epoch.to_string(),"nonce":nonce});
                if write_frame(&mut stream,&challenge).await.is_err(){continue;}
                let Ok(Some(proof))=read_frame(&mut stream).await else{continue;};
                if !keys(&proof,&["identity","epoch","proof"]) || proof["identity"]!="broker" || clock(&proof["epoch"])!=Some(epoch){continue;}
                let Some(received)=proof["proof"].as_str().and_then(unhex) else{continue;};
                let Some(mut secret)=key.lock().await.take() else{continue;};let input=format!("cafe.broker.auth.v1\nbroker\n{epoch}\n{nonce}");let expected=hmac_sha256(&secret,input.as_bytes());secret.fill(0);
                if !equal(&expected,&received){continue;}
                if write_frame(&mut stream,&json!({"authenticated":true})).await.is_err(){continue;}
                let Ok(Some(request))=read_frame(&mut stream).await else{continue;};
                let valid=keys(&request,&["epoch","capability","body"]) && clock(&request["epoch"])==Some(epoch) && keys(&request["body"],&[]) && matches!(request["capability"].as_str(),Some("snapshot"|"propose"));
                if !valid{let _=write_frame(&mut stream,&json!({"error":"capability_denied"})).await;continue;}
                let n=count.fetch_add(1,Ordering::SeqCst)+1;let _=write_frame(&mut stream,&json!({"accepted":true,"effects":n})).await;
            }
        });
        Ok(Self{port,epoch,effects,tasks:vec![bootstrap,serving],dir:dir.to_path_buf()})
    }
    #[cfg(not(unix))]
    pub async fn start(_dir:&Path,_bind:&str,_epoch:u64)->Result<Self,String>{Err("windows_verification_pending".into())}
    pub fn status(&self)->Value{json!({"host":"127.0.0.1","port":self.port,"epoch":self.epoch.to_string(),"effects":self.effects.load(Ordering::SeqCst),"rendezvous_ready":true})}
}
impl Drop for BrokerAuthority {fn drop(&mut self){for t in &self.tasks{t.abort();}let _=std::fs::remove_file(self.dir.join("broker.sock"));let _=std::fs::remove_dir(&self.dir);}}
