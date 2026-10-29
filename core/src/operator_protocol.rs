//! Closed private-stdin operator protocol. No remote confirmation issuer.
#![forbid(unsafe_code)]
use serde::{Deserialize,Deserializer};
use crate::capsule_registry::{Clock,Role};

pub(crate) const MAX_OPERATOR_BYTES:usize=16_384;
/// Deliberately neither Debug nor Serialize; secrets never enter projections.
pub(crate) struct Secret(String);
impl Secret {pub(crate) fn expose(&self)->&str{&self.0}}
impl<'de> Deserialize<'de> for Secret {
    fn deserialize<D:Deserializer<'de>>(deserializer:D)->Result<Self,D::Error>{
        let value=String::deserialize(deserializer)?;
        if value.is_empty() || value.len()>1024 || value.contains('\0'){return Err(serde::de::Error::custom("secret_rejected"));}
        Ok(Self(value))
    }
}
#[derive(Deserialize)]
#[serde(tag="mode",rename_all="snake_case",deny_unknown_fields)]
pub(crate) enum PasswordChoice {Absent{},Provided{value:Secret}}
#[derive(Clone,Copy,Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct WireCrop{pub x:u32,pub y:u32,pub width:u32,pub height:u32}
#[derive(Deserialize)]
#[serde(tag="op",rename_all="snake_case",deny_unknown_fields)]
pub(crate) enum OperatorBody {
    Snapshot{},
    ListWindows{offset:Option<u32>},
    CreateCapsule{capsule_id:String,person_label:String},
    PairWindow{capsule_id:String,person_label:String,feed_id:String,role:Role,window_token:String,window_generation:Clock,layout_revision:Clock,crop:WireCrop},
    SetFolder{folder:String},
    StartRecording{capsule_id:String},
    StopRecording{capsule_id:String},
    SendToObs{capsule_id:String},
    ConfigureFfmpeg{path:String,sha256:String},
    ConfigureObs{port:u16,password:PasswordChoice},
    ConfigureTwitch{channel:String},
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct OperatorRequest {
    pub schema:String,pub kind:String,pub request_id:String,
    pub authority_epoch:Clock,pub expected_revision:Clock,pub body:OperatorBody,
}
fn identifier(value:&str)->bool{!value.is_empty() && value.len()<=96 && value.as_bytes()[0].is_ascii_alphanumeric() && value.bytes().all(|b|b.is_ascii_alphanumeric()||b"_.:-".contains(&b))}
fn bounded(value:&str,max:usize)->bool{!value.is_empty() && value.len()<=max && !value.contains('\0')}
impl OperatorBody {
    fn valid(&self)->bool{match self {
        Self::Snapshot{}=>true,
        Self::ListWindows{offset}=>offset.is_none_or(|value|value<=1_000_000),
        Self::CreateCapsule{capsule_id,person_label}=>identifier(capsule_id)&&bounded(person_label,128),
        Self::PairWindow{capsule_id,person_label,feed_id,window_token,window_generation,layout_revision,crop,..}=>identifier(capsule_id)&&identifier(feed_id)&&bounded(person_label,128)&&bounded(window_token,128)&&window_generation.0>0&&layout_revision.0>0&&crop.width>0&&crop.height>0&&crop.x.checked_add(crop.width).is_some()&&crop.y.checked_add(crop.height).is_some(),
        Self::SetFolder{folder}=>bounded(folder,4096),
        Self::StartRecording{capsule_id}|Self::StopRecording{capsule_id}|Self::SendToObs{capsule_id}=>identifier(capsule_id),
        Self::ConfigureFfmpeg{path,sha256}=>bounded(path,4096)&&sha256.len()==64&&sha256.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b)),
        Self::ConfigureObs{port,..}=>*port>0,
        Self::ConfigureTwitch{channel}=>!channel.is_empty()&&channel.len()<=25&&channel.bytes().all(|b|b.is_ascii_lowercase()||b.is_ascii_digit()||b==b'_'),
    }}
}
/// Only closed syntax/length is checked here. Main MUST check epoch/revision,
/// known opaque-window lookup, identity/crop/folder and actual executable pins.
pub(crate) fn decode_operator(bytes:&[u8])->Result<OperatorRequest,&'static str>{
    if bytes.is_empty()||bytes.len()>MAX_OPERATOR_BYTES{return Err("operator_frame_rejected");}
    let value:OperatorRequest=serde_json::from_slice(bytes).map_err(|_|"operator_frame_rejected")?;
    if value.schema!="cafe.operator.v1"||value.kind!="operator_intent"||!identifier(&value.request_id)||!value.body.valid(){return Err("operator_frame_rejected");}
    Ok(value)
}

#[cfg(test)]
mod tests {
 use super::*;
 fn wire(body:&str)->String{format!("{{\"schema\":\"cafe.operator.v1\",\"kind\":\"operator_intent\",\"request_id\":\"ui-one\",\"authority_epoch\":\"7\",\"expected_revision\":\"3\",\"body\":{body}}}")}
 #[test]fn private_operator_known_query_has_exact_clocks(){let v=decode_operator(wire("{\"op\":\"snapshot\"}").as_bytes()).unwrap();assert_eq!(v.authority_epoch,Clock(7));assert_eq!(v.expected_revision,Clock(3));}
 #[test]fn unknown_outer_and_body_fields_are_rejected(){
  assert!(decode_operator(wire("{\"op\":\"snapshot\",\"extra\":true}").as_bytes()).is_err());
  assert!(decode_operator(wire("{\"op\":\"snapshot\"}").replacen("\"body\":","\"extra\":true,\"body\":",1).as_bytes()).is_err());
 }
 #[test]fn duplicated_keys_never_select_an_origin_or_action(){
  for raw in [wire("{\"op\":\"snapshot\",\"op\":\"list_windows\"}"),wire("{\"op\":\"snapshot\"}").replacen("\"request_id\":","\"request_id\":\"other\",\"request_id\":",1),wire("{\"op\":\"configure_obs\",\"port\":4455,\"port\":4456,\"password\":{\"mode\":\"absent\"}}")]{assert!(decode_operator(raw.as_bytes()).is_err());}
 }
 #[test]fn clocks_are_decimal_strings_and_never_unsafe_json_numbers(){for clock in ["7","\"18446744073709551616\"","\"-1\"","\"07\""]{let raw=wire("{\"op\":\"snapshot\"}").replace("\"authority_epoch\":\"7\"",&format!("\"authority_epoch\":{clock}"));assert!(decode_operator(raw.as_bytes()).is_err());}}
 #[test]fn secret_errors_and_password_presence_are_explicit_and_closed(){
  assert!(decode_operator(wire("{\"op\":\"configure_obs\",\"port\":4455,\"password\":{\"mode\":\"absent\"}}").as_bytes()).is_ok());
  assert!(decode_operator(wire("{\"op\":\"configure_obs\",\"port\":4455,\"password\":{\"mode\":\"absent\",\"extra\":true}}").as_bytes()).is_err());
  let bad=wire("{\"op\":\"configure_obs\",\"port\":4455,\"password\":{\"mode\":\"provided\",\"value\":\"canary-not-a-password\",\"extra\":true}}");
  assert_eq!(decode_operator(bad.as_bytes()).err(),Some("operator_frame_rejected"));
  assert!(decode_operator(wire("{\"op\":\"configure_obs\",\"port\":4455,\"host\":\"remote.invalid\",\"password\":{\"mode\":\"absent\"}}").as_bytes()).is_err());
 }
 #[test]fn malformed_geometry_and_oversized_frames_reject(){
  assert!(decode_operator(&vec![b' ';MAX_OPERATOR_BYTES+1]).is_err());
  let raw=wire("{\"op\":\"pair_window\",\"capsule_id\":\"cap\",\"person_label\":\"Guest\",\"feed_id\":\"game\",\"role\":\"game\",\"window_token\":\"opaque\",\"window_generation\":\"1\",\"layout_revision\":\"1\",\"crop\":{\"x\":4294967295,\"y\":0,\"width\":2,\"height\":1}}");assert!(decode_operator(raw.as_bytes()).is_err());
 }
}

#[cfg(test)]mod page_tests {
 use super::*;
 fn wire(body:&str)->String{format!("{{\"schema\":\"cafe.operator.v1\",\"kind\":\"operator_intent\",\"request_id\":\"list-one\",\"authority_epoch\":\"7\",\"expected_revision\":\"0\",\"body\":{body}}}")}
 #[test]fn bounded_window_page_syntax_is_closed(){
  assert!(decode_operator(wire("{\"op\":\"list_windows\"}").as_bytes()).is_ok());
  assert!(decode_operator(wire("{\"op\":\"list_windows\",\"offset\":3}").as_bytes()).is_ok());
  for body in ["{\"op\":\"list_windows\",\"offset\":-1}","{\"op\":\"list_windows\",\"offset\":1000001}","{\"op\":\"list_windows\",\"offset\":0,\"extra\":true}"]{assert!(decode_operator(wire(body).as_bytes()).is_err());}
 }
}
