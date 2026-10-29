//! Host-selected private storage, exclusive process lease and collision-checked epochs.
#![forbid(unsafe_code)]
use std::{fs::{File,OpenOptions},path::{Path,PathBuf}};
use crate::capsule_registry::Clock;
pub(crate) struct RuntimeStorage {pub root:PathBuf,pub session:PathBuf,pub recordings:PathBuf,pub epoch:Clock,pub session_id:String,_writer:File}
fn links(path:&Path)->bool{path.ancestors().any(|part|match std::fs::symlink_metadata(part){Ok(meta)=>{
 #[cfg(windows)]{use std::os::windows::fs::MetadataExt;if meta.file_attributes()&0x400!=0{return true;}}
 meta.file_type().is_symlink()},Err(_)=>false})}
fn create_private(path:&Path)->Result<(),&'static str>{
 #[cfg(unix)]{use std::os::unix::fs::DirBuilderExt;std::fs::DirBuilder::new().mode(0o700).create(path).map_err(|_|"runtime_directory_create_failed")?;}
 #[cfg(windows)]{crate::runtime_windows_security::create(path)?;}
 #[cfg(not(any(unix,windows)))]{return Err("runtime_platform_unsupported");}
 verify_private(path)
}
fn verify_private(path:&Path)->Result<(),&'static str>{
 let meta=std::fs::symlink_metadata(path).map_err(|_|"runtime_directory_rejected")?;if !meta.is_dir()||links(path){return Err("runtime_directory_rejected");}
 #[cfg(target_os="linux")]{use std::os::unix::fs::MetadataExt;let owner=std::fs::metadata("/proc/self").map_err(|_|"runtime_owner_unavailable")?.uid();if meta.uid()!=owner || meta.mode()&0o077!=0{return Err("runtime_acl_rejected");}}
 #[cfg(windows)]{crate::runtime_windows_security::verify(path)?;}
 #[cfg(not(any(target_os="linux",windows)))]{return Err("runtime_platform_unsupported");}
 Ok(())
}
impl RuntimeStorage {
 /// Root is supplied only by the native Electron host, never a renderer or
 /// MCP request. Persistent session directories are not removed on shutdown.
 pub(crate) fn open(requested:&Path)->Result<Self,&'static str>{
  if !requested.is_absolute()||requested.components().any(|p|matches!(p,std::path::Component::ParentDir))||links(requested){return Err("runtime_directory_rejected");}
  #[cfg(windows)]{use std::path::{Component,Prefix};if !matches!(requested.components().next(),Some(Component::Prefix(p)) if matches!(p.kind(),Prefix::Disk(_)|Prefix::VerbatimDisk(_))){return Err("runtime_directory_rejected");}}
  if !requested.exists(){create_private(requested)?;}else{verify_private(requested)?;}
  let root=requested.canonicalize().map_err(|_|"runtime_directory_rejected")?;
  let lease=root.join("authority.lock");if links(&lease){return Err("runtime_lease_rejected");}
  let mut options=OpenOptions::new();options.read(true).write(true).create(true);
  #[cfg(unix)]{use std::os::unix::fs::OpenOptionsExt;options.mode(0o600);}
  let writer=options.open(&lease).map_err(|_|"runtime_lease_rejected")?;
  if links(&lease)||!writer.metadata().map_err(|_|"runtime_lease_rejected")?.is_file(){return Err("runtime_lease_rejected");}
  writer.try_lock().map_err(|_|"authority_already_running")?;
  let mut entropy=crate::authority_auth::entropy().map_err(|_|"runtime_entropy_unavailable")?;let mut head=[0u8;8];head.copy_from_slice(&entropy[..8]);let epoch=Clock(u64::from_be_bytes(head));entropy.fill(0);
  if epoch.0==0{return Err("runtime_epoch_rejected");}
  let session_id=format!("session-{}",epoch.0);let session=root.join(&session_id);
  // Exclusive create fails on a retained epoch collision; never reuse it.
  create_private(&session)?;let recordings=session.join("recordings");create_private(&recordings)?;
  Ok(Self{root,session,recordings,epoch,session_id,_writer:writer})
 }
}
#[cfg(test)]mod tests{
 use super::*;use std::sync::atomic::{AtomicU64,Ordering};static NEXT:AtomicU64=AtomicU64::new(0);
 struct Scratch(PathBuf);impl Scratch{fn new()->Self{let base=std::env::temp_dir().canonicalize().unwrap().join(format!("cafe-runtime-store-{}-{}",std::process::id(),NEXT.fetch_add(1,Ordering::SeqCst)));std::fs::create_dir(&base).unwrap();Self(base)}}impl Drop for Scratch{fn drop(&mut self){std::fs::remove_dir_all(&self.0).unwrap();}}
 #[test]fn exclusive_runtime_lease_does_not_hide_the_journal(){let tmp=Scratch::new();let root=tmp.0.join("private");let live=RuntimeStorage::open(&root).unwrap();assert!(RuntimeStorage::open(&root).is_err());std::fs::write(live.session.join("readable.journal"),b"fixture").unwrap();assert_eq!(std::fs::read(live.session.join("readable.journal")).unwrap(),b"fixture");}
 #[test]fn retained_sessions_prevent_epoch_reuse_and_lockfile_is_not_deleted(){let tmp=Scratch::new();let root=tmp.0.join("private");let first=RuntimeStorage::open(&root).unwrap();let epoch=first.epoch;let prior=first.session.clone();drop(first);assert!(root.join("authority.lock").is_file());let second=RuntimeStorage::open(&root).unwrap();assert_ne!(epoch,second.epoch);assert!(prior.is_dir());}
 #[test]fn relative_runtime_root_is_never_guessed(){assert!(RuntimeStorage::open(Path::new("relative-runtime")).is_err());}
 #[test]#[cfg(unix)]fn permissive_existing_runtime_root_is_rejected(){use std::os::unix::fs::PermissionsExt;let tmp=Scratch::new();let root=tmp.0.join("unsafe");std::fs::create_dir(&root).unwrap();std::fs::set_permissions(&root,std::fs::Permissions::from_mode(0o755)).unwrap();assert!(RuntimeStorage::open(&root).is_err());}
}
