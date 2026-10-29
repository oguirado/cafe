//! Owner-only persistent runtime directories; no secret values are logged.
use std::path::Path;
use std::os::windows::ffi::OsStrExt;
use windows::core::{PCWSTR,PWSTR};
use windows::Win32::Foundation::{HLOCAL,LocalFree};
use windows::Win32::Security::{PSECURITY_DESCRIPTOR,SECURITY_ATTRIBUTES,OWNER_SECURITY_INFORMATION,DACL_SECURITY_INFORMATION};
use windows::Win32::Security::Authorization::{ConvertStringSecurityDescriptorToSecurityDescriptorW,ConvertSecurityDescriptorToStringSecurityDescriptorW,GetNamedSecurityInfoW,SE_FILE_OBJECT};
use windows::Win32::Storage::FileSystem::CreateDirectoryW;
struct Allocation(*mut core::ffi::c_void);
impl Drop for Allocation {fn drop(&mut self){unsafe{let _=LocalFree(Some(HLOCAL(self.0)));}}}
fn wide(path:&Path)->Result<Vec<u16>,&'static str>{let mut out:Vec<u16>=path.as_os_str().encode_wide().collect();if out.contains(&0){return Err("runtime_directory_rejected");}out.push(0);Ok(out)}
fn desired()->Result<(Allocation,PSECURITY_DESCRIPTOR),&'static str>{
 let sid=crate::authority_auth::windows_auth::owner_sid().map_err(|_|"runtime_owner_unavailable")?;
 let sddl:Vec<u16>=format!("O:{sid}D:P(A;OICI;FA;;;{sid})").encode_utf16().chain(Some(0)).collect();let mut descriptor=PSECURITY_DESCRIPTOR::default();
 unsafe{ConvertStringSecurityDescriptorToSecurityDescriptorW(PCWSTR(sddl.as_ptr()),1,&mut descriptor,None)}.map_err(|_|"runtime_acl_unavailable")?;
 Ok((Allocation(descriptor.0),descriptor))
}
fn text(descriptor:PSECURITY_DESCRIPTOR)->Result<String,&'static str>{
 let mut out=PWSTR::null();unsafe{ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor,1,OWNER_SECURITY_INFORMATION|DACL_SECURITY_INFORMATION,&mut out,None)}.map_err(|_|"runtime_acl_unavailable")?;
 let _allocation=Allocation(out.0.cast());unsafe{out.to_string()}.map_err(|_|"runtime_acl_unavailable")
}
fn policy_equal(actual:&str,expected:&str)->bool{
 let Some((actual_owner,actual_acl))=actual.split_once("D:") else{return false;};let Some((owner,acl))=expected.split_once("D:") else{return false;};
 if actual_owner!=owner{return false;}
 let Some(at)=actual_acl.find('(') else{return false;};let Some(want)=acl.find('(') else{return false;};
 // AI/AR only describe inheritance bookkeeping. P remains mandatory, and
 // owner plus the complete canonical ACE list must match exactly.
 let Some(mut flags)=actual_acl[..at].strip_prefix('P') else{return false;};let mut ai=false;let mut ar=false;
 while !flags.is_empty(){if let Some(rest)=flags.strip_prefix("AI"){if ai{return false;}ai=true;flags=rest;}else if let Some(rest)=flags.strip_prefix("AR"){if ar{return false;}ar=true;flags=rest;}else{return false;}}
 actual_acl[at..]==acl[want..]
}
pub(crate) fn create(dir:&Path)->Result<(),&'static str>{
 let (_guard,descriptor)=desired()?;let attributes=SECURITY_ATTRIBUTES{nLength:size_of::<SECURITY_ATTRIBUTES>() as u32,lpSecurityDescriptor:descriptor.0,bInheritHandle:false.into()};let path=wide(dir)?;
 unsafe{CreateDirectoryW(PCWSTR(path.as_ptr()),Some(&attributes))}.map_err(|_|"runtime_directory_create_failed")?;verify(dir)
}
pub(crate) fn verify(dir:&Path)->Result<(),&'static str>{
 let path=wide(dir)?;let mut actual=PSECURITY_DESCRIPTOR::default();
 let result=unsafe{GetNamedSecurityInfoW(PCWSTR(path.as_ptr()),SE_FILE_OBJECT,OWNER_SECURITY_INFORMATION|DACL_SECURITY_INFORMATION,None,None,None,None,&mut actual)};
 if result.0!=0 || actual.0.is_null(){return Err("runtime_acl_rejected");}let _actual=Allocation(actual.0);let (_wanted_guard,wanted)=desired()?;
 if !policy_equal(&text(actual)?,&text(wanted)?){return Err("runtime_acl_rejected");}Ok(())
}
