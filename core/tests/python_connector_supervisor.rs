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
