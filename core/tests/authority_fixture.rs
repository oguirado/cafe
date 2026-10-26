//! Synthetic test authority, not the production entrypoint or an MCP service.
#[path = "../src/authority_auth.rs"] mod authority_auth;
#[path = "../src/python_connector_supervisor.rs"] mod python_connector_supervisor;
use authority_auth::{BrokerAuthority,make_frame,validate_connector_frame};
use python_connector_supervisor::ConnectorSupervisor;
use serde_json::{Value,json};
use std::io::{Read,Write};
use std::path::PathBuf;

fn emit(value:Value){let mut out=std::io::stdout().lock();if let Ok(mut data)=serde_json::to_vec(&value){data.push(b'\n');let _=out.write_all(&data);let _=out.flush();}}
async fn serve(args:Vec<String>)->Result<(),String>{
    if args.len()!=4{return Err("fixture_arguments".into());}
    let work=PathBuf::from(&args[3]);let mut connector=ConnectorSupervisor::spawn(&args[1],&[args[2].clone()],1).await?;
    let (tx,mut rx)=tokio::sync::mpsc::channel::<Value>(8);
    tokio::task::spawn_blocking(move||{let mut stdin=std::io::stdin().lock();loop{let mut data=Vec::new();let mut byte=[0];loop{match stdin.read(&mut byte){Ok(0)=>return,Ok(_)=>{},Err(_)=>return}data.push(byte[0]);if data.len()>authority_auth::MAX_LINE{return;}if byte[0]==b'\n'{break;}}let Ok(v)=serde_json::from_slice(&data)else{return;};if tx.blocking_send(v).is_err(){return;}}});
    let mut broker:Option<BrokerAuthority>=None;let mut serial=0u64;let mut admitted=0u64;
    emit(json!({"ready":true,"connector":connector.status()}));
    while let Some(v)=rx.recv().await{
        let result:Result<Value,String>=match v["op"].as_str(){
            Some("status")=>Ok(json!({"connector":connector.status(),"broker":broker.as_ref().map(|b|b.status()),"admitted":admitted})),
            Some("effect")=>{let status=connector.status();let epoch=status["authority_epoch"].as_str().and_then(|s|s.parse().ok()).ok_or("invalid_epoch")?;let action=v["action"].as_str().unwrap_or("");connector.exchange(make_frame("approved_effect",epoch,json!({"action":action}))).await},
            Some("accept_connector")=>{let status=connector.status();let epoch=status["authority_epoch"].as_str().and_then(|s|s.parse().ok()).ok_or("invalid_epoch")?;match validate_connector_frame(&v["frame"],epoch){Ok(())=>{admitted+=1;Ok(json!({"accepted":true,"admitted":admitted}))},Err(e)=>Err(e)}},
            Some("lease")=>connector.lease(v["token"].as_str().unwrap_or(""),v["ttl_ms"].as_u64().unwrap_or(0),v["sink"].as_str().unwrap_or("")).await,
            Some("restart")=>connector.restart().await.map(|()|connector.status()),
            Some("broker_start")=>{
                serial+=1;let dir=work.join(format!("rendezvous-{serial}"));match BrokerAuthority::start(&dir,v["bind"].as_str().unwrap_or(""),1).await{Ok(b)=>{let result=b.status();broker=Some(b);Ok(result)},Err(e)=>Err(e)}
            },
            Some("shutdown")=>{connector.stop().await;emit(json!({"stopped":true}));return Ok(());},
            _=>Err("unknown_operation".into())
        };
        match result{Ok(value)=>emit(json!({"ok":value})),Err(_)=>emit(json!({"error":"request_rejected"}))}
    }
    connector.stop().await;Ok(())
}
fn main(){let args=std::env::args().collect();let runtime=tokio::runtime::Builder::new_current_thread().enable_all().build();match runtime{Ok(rt)=>{let result=rt.block_on(serve(args));rt.shutdown_background();if result.is_err(){emit(json!({"error":"fixture_failed"}));std::process::exit(2);}},Err(_)=>std::process::exit(2)}}
