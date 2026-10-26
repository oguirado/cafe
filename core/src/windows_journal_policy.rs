//! P06W safe NTFS metadata policy; unsafe OS calls are confined to this module.
//! This is option (b): successful file flushes, checked NTFS metadata journaling,
//! and same-volume write-through publication. It is not a directory-fsync claim.
//! Power-loss qualification remains pending on actual Windows/storage hardware.

use std::fs::OpenOptions;
use std::os::windows::fs::OpenOptionsExt;
use std::os::windows::io::AsRawHandle;
use std::os::windows::ffi::OsStrExt;
use std::path::Path;
use windows::core::PCWSTR;
use windows::Win32::Foundation::HANDLE;
use windows::Win32::Storage::FileSystem::{
    GetVolumeInformationByHandleW, MoveFileExW, FILE_FLAG_BACKUP_SEMANTICS,
    MOVEFILE_WRITE_THROUGH,
};
use crate::authority_journal::JournalError;

pub(crate) fn require_ntfs_directory(parent: &Path) -> Result<(), JournalError> {
    // Metadata query only: this read-only directory handle is NEVER flushed.
    let directory = OpenOptions::new().read(true)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS.0).open(parent)
        .map_err(|_| JournalError::Io)?;
    if !directory.metadata().map_err(|_| JournalError::Io)?.is_dir() {
        return Err(JournalError::Io);
    }
    let mut filesystem = [0u16; 32];
    // SAFETY: the live File owns the handle; buffers remain valid for this call.
    unsafe { GetVolumeInformationByHandleW(
        HANDLE(directory.as_raw_handle()), None, None, None, None,
        Some(&mut filesystem),
    ) }.map_err(|_| JournalError::Io)?;
    let end = filesystem.iter().position(|&v| v == 0).ok_or(JournalError::Io)?;
    let name = String::from_utf16(&filesystem[..end]).map_err(|_| JournalError::Io)?;
    if !name.eq_ignore_ascii_case("NTFS") { return Err(JournalError::Io); }
    Ok(())
}

pub(crate) fn publish_no_replace(source: &Path, target: &Path) -> Result<(), JournalError> {
    let mut from: Vec<u16> = source.as_os_str().encode_wide().collect();
    let mut to: Vec<u16> = target.as_os_str().encode_wide().collect();
    if from.contains(&0) || to.contains(&0) { return Err(JournalError::Io); }
    from.push(0); to.push(0);
    // No COPY_ALLOWED or REPLACE_EXISTING: same-volume operation and no clobber.
    // SAFETY: both UTF-16 vectors have a final NUL and live throughout the call.
    unsafe { MoveFileExW(PCWSTR(from.as_ptr()), PCWSTR(to.as_ptr()),
        MOVEFILE_WRITE_THROUGH) }.map_err(|_| JournalError::Io)
}
