# Manual Windows acceptance receipts

No live Discord acceptance receipt has been recorded. Portable fixtures, Windows type checks and a hosted synthetic window are separate evidence and cannot replace these observations.

After running the actual native scenarios, add a redacted receipt describing restored and occluded Discord pop-outs, same-window title changes, close/recreate and handle reuse, person/crop selection, resize/DPI/layout, permission and lifecycle cleanup, source-clock observations, and measured profile budgets. Record failures and unavailable scenarios explicitly. Never infer source quality or performance from window dimensions.

Template (empty fields are pending):

```json
{
  "machine_class": "",
  "os_build": "",
  "steps": [],
  "result": "pending",
  "date": "",
  "evidence_sha256": []
}
```

Use generic hardware classes and redacted evidence. No usernames, machine names, credentials, personal media, raw native handles or private paths. This template is not a pass receipt. See the [project README](../../README.md) for CI coverage.


## Journal durability contract

The journal uses option (b) on Windows: successful regular-file flushes and
checked NTFS metadata journaling, with same-volume publication through
`MoveFileExW` and `MOVEFILE_WRITE_THROUGH`. Other filesystems or metadata-query
errors are rejected. There is no directory-fsync claim: the directory handle
uses backup semantics for the NTFS query only and is never flushed. Both media
file flushes use handles opened with write access. Publication does not replace
an existing target, and a failed required flush, metadata check or rename cannot
return a committed receipt. Unix retains its directory fsync.

This contract provides successful OS file barriers, checked namespace publication
and recoverable journal framing. It does not establish that every device cache,
file creation or rename survives power removal. A failed commit poisons its
writer, retains the pending in-memory stage and returns no receipt; bytes may
already have reached a cache, so no successful physical rollback is claimed.
Recovery continues to validate the committed prefix and reject corruption.

Implementation: `core/src/authority_journal.rs` and
`core/src/windows_journal_policy.rs`. Native Windows runtime acceptance is the
Windows CI journal suite after the operator commits. Power-loss qualification:
`windows_verification: pending`; test real storage, abrupt termination and power
removal at creation, rename, file flush and receipt boundaries before claiming
that hardware guarantee.

API references: [file flush rights](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers),
[write-through move](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw),
[NTFS metadata recovery](https://learn.microsoft.com/en-us/troubleshoot/windows-client/backup-and-storage/fat-hpfs-and-ntfs-file-systems) and
[caching limits](https://learn.microsoft.com/en-us/windows/win32/fileio/file-caching).

## Journal writer-lock lifecycle

A writer first creates or opens the complete journal pathname plus `.lock` and
holds its exclusive `try_lock` handle until its data handle closes. The lock
file persists after success, error, recovery and process exit; it is never
unlinked by a writer. Its presence alone does not mean an owner is alive: the
OS handle lock is the admission authority. Administrators must not unlink or
replace it while a writer may be alive. All cooperating writers use the same
stable journal pathname. The journal data file itself is not locked, so separate
read-only handles can observe bytes on Windows and Unix. Observers still need
to validate framing and committed trailers; seeing bytes is not a receipt.
Repairing replay takes the writer lock because it can quarantine/truncate data.
The file and Unix directory durability barriers are unchanged.

## Private Windows authentication rendezvous

Windows secrets and nonces come exclusively from `BCryptGenRandom` with the
system-preferred provider. A failed system call returns an error without a
fallback or partial bytes. Unix retains its operating-system random source.
The broker binds only loopback and delivers its one-use HMAC key over a local
named pipe. Both the pipe and its fresh rendezvous directory have a protected
DACL granting full access only to the current account SID. Metadata inherits
that ACL and contains only a random pipe name and authority process identity,
never the key. The Python receiver rejects reparse points, wrong ownership,
additional ACEs, an unprotected directory DACL or a different pipe server.
The pipe rejects remote clients and duplicate server instances. The receiver
uses identification-only security quality of service, reads a bounded ticket
and acknowledges receipt before the server handle closes. Later TCP admission
still requires the existing one-use, epoch-bound HMAC proof.

This is account isolation, not protection against another hostile process
already running as the same account. Runtime Windows verification of the
rendezvous, CSPRNG and all CI stages remains pending until the operator's next
commit and hosted run. Native Discord and physical power-loss acceptance remain
separate pending checks.

API references: [mandatory locks](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex),
[system RNG](https://learn.microsoft.com/en-us/windows/win32/api/bcrypt/nf-bcrypt-bcryptgenrandom) and
[pipe ACLs](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights).
