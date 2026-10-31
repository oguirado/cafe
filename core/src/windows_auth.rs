//! Private Windows rendezvous: protected owner-only DACL and local-only named pipe.
use std::path::Path;
use std::os::windows::ffi::OsStrExt;
use std::os::windows::io::FromRawHandle;
use windows::core::{PCWSTR,PWSTR};
use windows::Win32::Foundation::{CloseHandle,HANDLE,HLOCAL,LocalFree,GENERIC_WRITE};
use windows::Win32::Security::{GetTokenInformation,TokenUser,TOKEN_QUERY,TOKEN_USER,SECURITY_ATTRIBUTES,PSECURITY_DESCRIPTOR};
use windows::Win32::Security::Authorization::{ConvertSidToStringSidW,ConvertStringSecurityDescriptorToSecurityDescriptorW};
use windows::Win32::System::Threading::{GetCurrentProcess,OpenProcessToken};
use windows::Win32::Storage::FileSystem::{CreateDirectoryW,CreateFileW,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,FILE_SHARE_READ};
use tokio::net::windows::named_pipe::{NamedPipeServer,ServerOptions};

struct Allocation(*mut core::ffi::c_void);
impl Drop for Allocation {fn drop(&mut self){unsafe{let _=LocalFree(Some(HLOCAL(self.0)));}}}
struct Token(HANDLE);
impl Drop for Token {fn drop(&mut self){unsafe{let _=CloseHandle(self.0);}}}
pub(crate) fn owner_sid()->Result<String,String>{
    let mut handle=HANDLE::default();
    // SAFETY: valid owned output handle; RAII closes every successful open.
    unsafe{OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&mut handle)}.map_err(|_|"rendezvous_owner_failed")?;
    let token=Token(handle);let mut length=0;
    unsafe{let _=GetTokenInformation(token.0,TokenUser,None,0,&mut length);}
    if length==0 || length>65536{return Err("rendezvous_owner_failed".into());}
    // Machine-word storage supplies TOKEN_USER alignment, including its SID tail.
    let mut storage=vec![0usize;(length as usize).div_ceil(size_of::<usize>())];
    unsafe{GetTokenInformation(token.0,TokenUser,Some(storage.as_mut_ptr().cast()),length,&mut length)}.map_err(|_|"rendezvous_owner_failed")?;
    let account=unsafe{&*storage.as_ptr().cast::<TOKEN_USER>()};let mut text=PWSTR::null();
    unsafe{ConvertSidToStringSidW(account.User.Sid,&mut text)}.map_err(|_|"rendezvous_owner_failed")?;
    let _allocation=Allocation(text.0.cast());
    unsafe{text.to_string()}.map_err(|_|"rendezvous_owner_failed".into())
}

pub(crate) fn rendezvous(dir:&Path,nonce:&str)->Result<NamedPipeServer,String>{
    let sid=owner_sid()?;
    // Protected DACL: current account only, full access, inherited by metadata.
    let sddl:Vec<u16>=format!("O:{sid}D:P(A;OICI;FA;;;{sid})").encode_utf16().chain(Some(0)).collect();
    let mut descriptor=PSECURITY_DESCRIPTOR::default();
    unsafe{ConvertStringSecurityDescriptorToSecurityDescriptorW(PCWSTR(sddl.as_ptr()),1,&mut descriptor,None)}.map_err(|_|"rendezvous_acl_failed")?;
    let _allocation=Allocation(descriptor.0);
    let mut attrs=SECURITY_ATTRIBUTES{nLength:size_of::<SECURITY_ATTRIBUTES>() as u32,lpSecurityDescriptor:descriptor.0,bInheritHandle:false.into()};
    let mut path:Vec<u16>=dir.as_os_str().encode_wide().collect();
    if path.contains(&0){return Err("rendezvous_create_failed".into());}path.push(0);
    unsafe{CreateDirectoryW(PCWSTR(path.as_ptr()),Some(&attrs))}.map_err(|_|"rendezvous_create_failed")?;
    let result=(||{
        if nonce.len()!=64 || !nonce.bytes().all(|b|b.is_ascii_hexdigit()){return Err("rendezvous_nonce_failed".into());}
        let name=format!("cafe-broker-{nonce}");
        let address=format!("{0}{0}.{0}pipe{0}{name}",'\\');
        // SAFETY: descriptor/attributes outlive CreateNamedPipeW inside Tokio.
        // First instance prevents impersonating an already-created server name.
        let pipe=unsafe{ServerOptions::new().first_pipe_instance(true).reject_remote_clients(true).max_instances(1)
            .create_with_security_attributes_raw(&address,(&mut attrs as *mut SECURITY_ATTRIBUTES).cast())}
            .map_err(|_|"rendezvous_bind_failed")?;
        let metadata=serde_json::json!({"schema":"cafe.rendezvous.endpoint.v1","pipe_name":name,"authority_pid":std::process::id()});
        // Inherited DACLs do not carry a protected flag or an explicit file owner.
        // Create metadata with the same owner-only descriptor as the directory,
        // including under an elevated token whose default owner is Administrators.
        let metadata_path:Vec<u16>=dir.join("broker.pipe").as_os_str().encode_wide().chain(Some(0)).collect();
        let handle=unsafe{CreateFileW(PCWSTR(metadata_path.as_ptr()),GENERIC_WRITE.0,FILE_SHARE_READ,Some(&attrs),CREATE_NEW,FILE_ATTRIBUTE_NORMAL,None)}.map_err(|_|"rendezvous_create_failed")?;
        // SAFETY: successful CREATE_NEW returns one owned handle, transferred once.
        let mut file=unsafe{std::fs::File::from_raw_handle(handle.0)};
        use std::io::Write;
        file.write_all(serde_json::to_string(&metadata).map_err(|_|"rendezvous_encode_failed")?.as_bytes()).map_err(|_|"rendezvous_write_failed")?;
        Ok(pipe)
    })();
    if result.is_err(){let _=std::fs::remove_file(dir.join("broker.pipe"));let _=std::fs::remove_dir(dir);}
    result
}
