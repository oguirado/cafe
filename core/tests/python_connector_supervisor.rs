#[path = "../src/authority_auth.rs"] mod authority_auth;
use authority_auth::*;
use serde_json::json;
#[test] fn hmac_independent_fixed_vector(){
    // RFC 4231 test case one, independently specified expected digest.
    let got=hmac_sha256(&[0x0b;20],b"Hi There");
    let expected:[u8;32]=[0xb0,0x34,0x4c,0x61,0xd8,0xdb,0x38,0x53,0x5c,0xa8,0xaf,0xce,0xaf,0x0b,0xf1,0x2b,0x88,0x1d,0xc2,0x00,0xc9,0x83,0x3d,0xa7,0x26,0xe9,0x37,0x6c,0x2e,0x32,0xcf,0xf7];assert_eq!(got,expected);
}
#[test] fn wrong_identity_before_effect(){assert!(validate_connector_frame(&make_frame("approved_effect",1,json!({"action":"effect"})),1).is_err());}
#[test] fn stale_epoch_before_effect(){assert!(validate_connector_frame(&make_frame("observation",1,json!({"ready":true})),2).is_err());}
#[test] fn secret_lease_forbidden_sink_before_serialization(){for sink in ["journal","log","broker",""]{assert!(lease_frame(1,"synthetic",100,sink).is_err());}}
#[test] fn closed_frame_unknown_key(){let mut f=make_frame("observation",1,json!({"ready":true}));f["extra"]=json!(true);assert!(validate_connector_frame(&f,1).is_err());}
#[test] fn unsafe_clock_rejected(){let mut f=make_frame("observation",1,json!({"ready":true}));f["authority_epoch"]=json!(1);assert!(validate_connector_frame(&f,1).is_err());}
#[test] fn wildcard_before_listener(){let rt=tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();let result=rt.block_on(BrokerAuthority::start(std::path::Path::new("unused-rendezvous"),"0.0.0.0",1));assert!(result.is_err());assert!(!std::path::Path::new("unused-rendezvous").exists());}

#[test] fn system_entropy_and_os_failure(){
    let first=authority_auth::test_entropy().unwrap();
    let second=authority_auth::test_entropy().unwrap();
    assert_eq!(first.len(),32); assert_eq!(second.len(),32);
    assert!(first!=second,"independent system CSPRNG calls must differ");
    assert!(matches!(authority_auth::test_failed_entropy(),Err(e) if e=="simulated_os_failure"));
}

#[test]fn service_request_surface_is_closed_before_private_pipe_write(){
    let valid=[json!({"op":"status"}),json!({"op":"close"}),json!({"op":"configure_obs","port":4455,"password":{"mode":"absent"}}),json!({"op":"configure_obs","port":4455,"password":{"mode":"provided","value":"SYNTHETIC_ONLY"}}),json!({"op":"start_chat","channel":"synthetic_channel"}),json!({"op":"send_capsule","capsule_id":"guest","feeds":{"game":"Game:Fixture:app.exe"},"crops":{"game":{"x":0,"y":0,"width":1280,"height":720}}})];
    for body in valid {
        assert!(validate_authority_frame(&make_frame("service_request",7,body.clone()),7).is_ok());
        let mut extra=body;extra["unknown"]=json!(true);assert!(validate_authority_frame(&make_frame("service_request",7,extra),7).is_err());
    }
    for body in [json!({"op":"configure_obs","host":"0.0.0.0","port":4455,"password":{"mode":"absent"}}),json!({"op":"configure_obs","port":true,"password":{"mode":"absent"}}),json!({"op":"configure_obs","port":4455,"password":{"mode":"absent","value":"SYNTHETIC_ONLY"}}),json!({"op":"start_chat","channel":"bad\r\nJOIN #other"}),json!({"op":"start_chat","channel":"synthetic","destination":"elsewhere"}),json!({"op":"send_capsule","capsule_id":"guest","feeds":{"game":"Game:Fixture:app.exe"}})] {
        assert!(validate_authority_frame(&make_frame("service_request",7,body),7).is_err());
    }
    let frame=make_frame("service_request",7,json!({"op":"status"}));assert!(validate_authority_frame(&frame,8).is_err());assert!(validate_connector_frame(&frame,7).is_err());
}
#[test]fn service_crops_and_roles_never_wrap_or_fall_back_to_whole_window(){
    let base=json!({"op":"send_capsule","capsule_id":"guest","feeds":{"camera":"Call:Fixture:app.exe"},"crops":{"camera":{"x":1,"y":2,"width":320,"height":240}}});
    for (key,value) in [("width",json!(0)),("x",json!(4294967295_u64)),("width",json!(-1)),("x",json!(true)),("height",json!(1.5))] {
        let mut body=base.clone();body["crops"]["camera"][key]=value;assert!(validate_authority_frame(&make_frame("service_request",7,body),7).is_err());
    }
    let mut body=base.clone();body["crops"]["game"]=body["crops"]["camera"].clone();assert!(validate_authority_frame(&make_frame("service_request",7,body),7).is_err());
}
#[test]fn service_results_cannot_reflect_secrets_or_unsafe_json_counter_numbers(){
    let base=json!({"accepted":true,"closed":false,"obs":"configured","obs_code":null,"chat":"running","chat_code":null,"chat_connected":null,"messages":"9007199254740993","malformed":null,"reconnects":null,"connections":null});
    assert!(validate_connector_frame(&make_frame("service_result",7,base.clone()),7).is_ok());
    for (key,value) in [("password",json!("SYNTHETIC_ONLY")),("obs_code",json!("SYNTHETIC_ONLY")),("messages",json!(9007199254740993_u64)),("messages",json!("01")),("messages",json!("18446744073709551616")),("chat",json!("connected"))] {
        let mut body=base.clone();body[key]=value;assert!(validate_connector_frame(&make_frame("service_result",7,body),7).is_err());
    }
}
