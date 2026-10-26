"""Owner-only Windows ticket reception, using system APIs through stdlib ctypes.

No secret is stored in rendezvous metadata. ACL, owner, reparse-point and server
identity failures reject admission; there is no alternate secret transport.
"""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import stat
import time


def _api():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    security = ctypes.WinDLL('advapi32', use_last_error=True)
    signatures = {
        'GetCurrentProcess': ([], wintypes.HANDLE),
        'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
        'LocalFree': ([ctypes.c_void_p], ctypes.c_void_p),
        'WaitNamedPipeW': ([wintypes.LPCWSTR, wintypes.DWORD], wintypes.BOOL),
        'CreateFileW': ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE),
        'GetNamedPipeServerProcessId': ([wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)], wintypes.BOOL),
        'PeekNamedPipe': ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
        'WriteFile': ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
        'ReadFile': ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    signatures = {
        'OpenProcessToken': ([wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)], wintypes.BOOL),
        'GetTokenInformation': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        'GetNamedSecurityInfoW': ([wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], wintypes.DWORD),
        'EqualSid': ([ctypes.c_void_p, ctypes.c_void_p], wintypes.BOOL),
        'GetAclInformation': ([ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int], wintypes.BOOL),
        'GetAce': ([ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)], wintypes.BOOL),
        'GetSecurityDescriptorControl': ([ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(security, name)
        function.argtypes, function.restype = args, result
    return kernel, security


def _owner(kernel, security):
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ValueError('rendezvous_owner_failed')
    try:
        needed = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not 0 < needed.value <= 65536:
            raise ValueError('rendezvous_owner_failed')
        data = ctypes.create_string_buffer(needed.value)
        if not security.GetTokenInformation(token, 1, data, needed, ctypes.byref(needed)):
            raise ValueError('rendezvous_owner_failed')
        # TOKEN_USER begins with SID_AND_ATTRIBUTES; its first member is PSID.
        return data, ctypes.c_void_p.from_buffer(data).value
    finally:
        kernel.CloseHandle(token)


def _check_acl(path, directory, kernel, security, sid):
    info = Path(path).lstat()
    if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError('insecure_rendezvous')
    if (directory and not stat.S_ISDIR(info.st_mode)) or (not directory and not stat.S_ISREG(info.st_mode)):
        raise ValueError('insecure_rendezvous')
    owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    # SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION.
    status = security.GetNamedSecurityInfoW(str(path), 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if status:
        raise ValueError('rendezvous_acl_failed')
    try:
        if not owner.value or not dacl.value or not security.EqualSid(owner, sid):
            raise ValueError('insecure_rendezvous')
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not security.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ValueError('rendezvous_acl_failed')
        if directory and not control.value & 0x1000:  # SE_DACL_PROTECTED
            raise ValueError('insecure_rendezvous')
        # ACL_SIZE_INFORMATION: AceCount, AclBytesInUse, AclBytesFree.
        acl_info = (wintypes.DWORD * 3)()
        if not security.GetAclInformation(dacl, acl_info, ctypes.sizeof(acl_info), 2) or acl_info[0] != 1:
            raise ValueError('insecure_rendezvous')
        ace = ctypes.c_void_p()
        if not security.GetAce(dacl, 0, ctypes.byref(ace)):
            raise ValueError('rendezvous_acl_failed')
        header = (ctypes.c_ubyte * 4).from_address(ace.value)
        # ACCESS_ALLOWED_ACE only; not INHERIT_ONLY, full file access for owner.
        mask = wintypes.DWORD.from_address(ace.value + 4).value
        if header[0] != 0 or header[1] & 0x08 or mask != 0x001f01ff or not security.EqualSid(ace.value + 8, sid):
            raise ValueError('insecure_rendezvous')
    finally:
        kernel.LocalFree(descriptor)


def _receive_ticket(directory, limit):
    kernel, security = _api()
    storage, sid = _owner(kernel, security)
    _check_acl(directory, True, kernel, security, sid)
    metadata = Path(directory) / 'broker.pipe'
    _check_acl(metadata, False, kernel, security, sid)
    if metadata.stat().st_size > 1024:
        raise ValueError('invalid_rendezvous_metadata')
    value = json.loads(metadata.read_bytes())
    if not isinstance(value, dict) or set(value) != {'schema', 'pipe_name', 'authority_pid'} or value['schema'] != 'cafe.rendezvous.endpoint.v1':
        raise ValueError('invalid_rendezvous_metadata')
    name, pid = value['pipe_name'], value['authority_pid']
    if not isinstance(name, str) or not name.startswith('cafe-broker-') or len(name) != 76 or any(c not in '0123456789abcdef' for c in name[12:]):
        raise ValueError('invalid_rendezvous_metadata')
    if type(pid) is not int or not 1 < pid <= 0xffffffff or pid == os.getpid():
        raise ValueError('invalid_rendezvous_metadata')
    address = '{0}{0}.{0}pipe{0}{1}'.format(chr(92), name)
    if not kernel.WaitNamedPipeW(address, 5000):
        raise ValueError('rendezvous_unavailable')
    # SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION: no server impersonation.
    handle = kernel.CreateFileW(address, 0xc0000000, 0, None, 3, 0x00110000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ValueError('rendezvous_unavailable')
    try:
        actual_pid = wintypes.ULONG()
        if not kernel.GetNamedPipeServerProcessId(handle, ctypes.byref(actual_pid)) or actual_pid.value != pid:
            raise ValueError('rendezvous_server_changed')
        deadline, data = time.monotonic() + 5, bytearray()
        while time.monotonic() < deadline:
            available = wintypes.DWORD()
            if not kernel.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
                raise ValueError('incomplete_ticket')
            if not available.value:
                time.sleep(0.005)
                continue
            buffer = ctypes.create_string_buffer(min(available.value, limit + 1 - len(data)))
            received = wintypes.DWORD()
            if not kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(received), None) or not received.value:
                raise ValueError('incomplete_ticket')
            data.extend(buffer.raw[:received.value])
            if len(data) > limit:
                raise ValueError('ticket_too_large')
            if b'\n' in data:
                if not data.endswith(b'\n') or data.count(b'\n') != 1:
                    raise ValueError('invalid_ticket')
                ticket = json.loads(data)
                ack = b'{"received":true}\n'
                written = wintypes.DWORD()
                if not kernel.WriteFile(handle, ack, len(ack), ctypes.byref(written), None) or written.value != len(ack):
                    raise ValueError('rendezvous_ack_failed')
                return ticket
        raise ValueError('rendezvous_timeout')
    finally:
        kernel.CloseHandle(handle)
