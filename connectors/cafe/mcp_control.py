"""Local MCP authority-control plane: selection, confirmation consumer, loopback dispatch.

Stdlib-only. Four named tools (select_capsule, start_recording, stop_recording,
send_to_obs) over the frozen cafe.control_intent.v1 wire contract. This module
never mints confirmations, never reads approvals from MCP arguments or files,
and never claims live product control: without a trusted configured snapshot
provider, approval callback and rendezvous bridge every control attempt ends in
an honest blocked reply. The native Rust confirmation bridge is pending
(runtime_main advertises mcp_control_available=false), so default deployments
deny by default.

Provider protocols (all injected, trusted, in-process only, never files):
  snapshot_provider() -> None | {"authority_epoch": str, "revision": str | None,
      "capsules": {capsule_id: object}}
    Fresh trusted authority snapshot per call. revision None means unknown
    (never invented). Missing or malformed snapshots fail closed.
  approval_provider(request_id, op, capsule_id) -> None
      | (wire, issued_ns) | {"wire": wire, "issued_ns": issued_ns}
    Pre-issued authority confirmation wire (exact cafe.control_intent.v1
    object) with a monotonic issuance stamp in nanoseconds. Bare wires are rejected; issuance must be supplied by the trusted local approval source. None means not approved (yet). Expired
    (>30s), future-dated, mismatched or reused approvals are rejected.
  rendezvous_dir: private broker rendezvous directory. The ticket is fetched
    once via channel_auth.receive_ticket and cached in memory; the secret is
    never logged, never in argv, and loopback 127.0.0.1 is the only host.
    Optional ticket_fetcher/connector seams override only the dial mechanics
    for sandboxed tests; production passes neither.

Replies are closed ControlReply-shaped dicts
{request_id, state, revision, error, receipt}; select_capsule replies are
{capsule_id, state, error}. Unknown revisions are None, never "0".
Malformed caller identifiers are answered as "invalid", never reflected.

Audit JSONL lines carry fixed keys only:
{ts_ns, op, request_id_sha256, capsule_id_sha256, authority_epoch,
expected_revision, state, error, effect_id}. Request/capsule identifiers are
sha256 hashes, never verbatim caller strings. One admission line is fsynced
before any dispatch or local selection change; a dispatch that reaches the
broker appends one outcome line. Admission audit failure prevents dispatch. A failed outcome audit returns interrupted and disables further effects; it cannot undo an already committed authority effect.
"""
import argparse
from contextlib import contextmanager
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import socket
import stat
import threading
import time

from . import channel_auth

CONTROL_TOOLS = ('select_capsule', 'start_recording', 'stop_recording', 'send_to_obs')
AUTHORITY_OPS = frozenset({'start_recording', 'stop_recording', 'send_to_obs'})
INTENT_SCHEMA = 'cafe.control_intent.v1'
INTENT_KIND = 'control_intent'
RECEIPT_SCHEMA = 'cafe.control_receipt.v1'
LOOPBACK = '127.0.0.1'
BROKER_IDENTITY = 'broker'
_TTL_NS = 30_000_000_000
_U64MAX = (1 << 64) - 1
_SEEN_CAP = 1024
_TOKEN_CAP = 2048
_IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}')
_TOKEN = re.compile(r'[A-Za-z0-9_-]{32,256}')
_CLOCK = re.compile(r'0|[1-9][0-9]{0,19}')
_EFFECT = re.compile(r'ctl\.[0-9a-f]{64}')
_NONCE = re.compile(r'[0-9a-f]{64}')
CONTROL_STATES = frozenset({'pending', 'committed', 'rejected', 'interrupted', 'blocked'})
SUCCESS_STATES = frozenset({'committed', 'pending', 'selected'})
# Every error code this module emits. Fixed set, no exception text ever surfaces.
CONTROL_ERRORS = frozenset({
    'mcp_control_unavailable', 'confirmation_required',
    'confirmation_binding_or_expiry', 'invalid_control_intent',
    'request_id_conflict', 'capsule_unavailable',
    'control_identity_or_epoch_denied', 'stale_revision',
    'control_transport_error', 'uncertain_dispatch', 'audit_unavailable', 'confirmation_capacity',
})
# Error codes accepted in authority replies (Rust control_intents verbatim).
AUTHORITY_ERRORS = frozenset({
    'invalid_control_intent', 'authority_requires_recovery',
    'old_authority_epoch', 'stale_revision', 'folder_not_authorized',
    'folder_selection_capacity', 'capsule_unavailable', 'confirmation_denied',
    'confirmation_capacity', 'confirmation_entropy_unavailable',
    'confirmation_entropy_collision', 'request_id_conflict',
    'prior_effect_not_replayed', 'control_identity_or_epoch_denied',
    'confirmation_required', 'confirmation_binding_or_expiry',
    'recording_folder_required', 'control_peer_revoked', 'intent_not_durable',
    'folder_changed_after_admission', 'capability_denied', 'effect_interrupted',
    'result_not_durable', 'actor_completion_already_recorded',
    'unknown_actor_completion', 'control_response_unavailable',
})


def _is_identifier(value):
    return type(value) is str and _IDENT.fullmatch(value) is not None


def _is_token(value):
    return type(value) is str and _TOKEN.fullmatch(value) is not None


def _is_clock(value):
    return (type(value) is str and _CLOCK.fullmatch(value) is not None
            and int(value) <= _U64MAX)


def _sha256_hex(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _strict_pairs(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValueError('duplicate_key')
        value[key] = child
    return value


def _reject_constant(value):
    raise ValueError('non_json_number')


def _loads_strict(raw):
    return json.loads(bytes(raw), object_pairs_hook=_strict_pairs,
                       parse_constant=_reject_constant)


def _frame_bytes(value):
    return (json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
            + b'\n')


def _read_frame(sock):
    data = bytearray()
    while True:
        if len(data) > channel_auth.MAX_LINE:
            raise ValueError('frame_too_large')
        try:
            chunk = sock.recv(4096)
        except socket.timeout as exc:
            raise ValueError('frame_timeout') from exc
        if not chunk:
            raise ValueError('frame_eof')
        data += chunk
        index = data.find(b'\n')
        if index != -1:
            if len(data) > index + 1:
                raise ValueError('frame_trailing')
            line = bytes(data)
            if len(line) > channel_auth.MAX_LINE:
                raise ValueError('frame_too_large')
            return _loads_strict(line)


def _write_frame(sock, value):
    data = _frame_bytes(value)
    if len(data) > channel_auth.MAX_LINE:
        raise ValueError('frame_too_large')
    sock.sendall(data)


class _Denied(Exception):
    def __init__(self, state, error):
        super().__init__(state)
        self.state = state
        self.error = error


class _MalformedApproval(Exception):
    pass


class _ProviderFailed(Exception):
    pass


class _NoBridge(Exception):
    pass


class _TransportError(Exception):
    def __init__(self, ambiguous):
        super().__init__('transport')
        self.ambiguous = ambiguous


def _default_fetch_ticket(rendezvous_dir):
    return asyncio.run(channel_auth.receive_ticket(rendezvous_dir))


def _default_connector(ticket):
    return socket.create_connection((LOOPBACK, ticket['port']), timeout=5)


@contextmanager
def _audit_file(path):
    """Anchor audit writes; Windows retains directory handles without delete sharing."""
    if path.parent.resolve() != path.parent.absolute():
        raise OSError('audit_path_changed')
    descriptors=[];native=[];fd=None
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            import msvcrt
            class Info(ctypes.Structure):
                _fields_=[('attributes',wintypes.DWORD),('created',wintypes.FILETIME),('accessed',wintypes.FILETIME),('written',wintypes.FILETIME),('volume',wintypes.DWORD),('size_high',wintypes.DWORD),('size_low',wintypes.DWORD),('links',wintypes.DWORD),('index_high',wintypes.DWORD),('index_low',wintypes.DWORD)]
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            create=kernel.CreateFileW;create.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE];create.restype=wintypes.HANDLE
            close=kernel.CloseHandle;close.argtypes=[wintypes.HANDLE];close.restype=wintypes.BOOL
            information=kernel.GetFileInformationByHandle;information.argtypes=[wintypes.HANDLE,ctypes.POINTER(Info)];information.restype=wintypes.BOOL
            invalid=ctypes.c_void_p(-1).value
            def opened(name,directory):
                handle=create(str(name),0x80 if directory else 0x40000080,3 if directory else 1,None,3 if directory else 4,0x02200000 if directory else 0x00200080,None)
                if handle==invalid:raise OSError('audit_handle_failed')
                info=Info()
                if not information(handle,ctypes.byref(info)) or info.attributes&0x400 or bool(info.attributes&0x10)!=directory or (not directory and info.links!=1):
                    close(handle);raise OSError('audit_handle_rejected')
                return handle
            for directory in reversed((path.parent,*path.parent.parents)):
                native.append(opened(directory,True))
            handle=opened(path,False)
            try:fd=msvcrt.open_osfhandle(handle,os.O_WRONLY|os.O_APPEND|os.O_BINARY)
            except BaseException:close(handle);raise
        else:
            required=getattr(os,'O_DIRECTORY',None);nofollow=getattr(os,'O_NOFOLLOW',None)
            if required is None or nofollow is None:raise OSError('anchored_audit_unavailable')
            directory=os.open(path.anchor,os.O_RDONLY|required);descriptors.append(directory)
            for part in path.parent.parts[1:]:
                directory=os.open(part,os.O_RDONLY|required|nofollow,dir_fd=directory);descriptors.append(directory)
            fd=os.open(path.name,os.O_WRONLY|os.O_CREAT|os.O_APPEND|nofollow,0o600,dir_fd=directory)
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise OSError('audit_file_rejected')
        yield fd
    finally:
        if fd is not None:os.close(fd)
        for descriptor in reversed(descriptors):os.close(descriptor)
        for handle in reversed(native):close(handle)


class ControlPlane:
    """Trusted local control plane. All steady state is in memory; audit is JSONL."""

    def __init__(self, audit_path, *, snapshot_provider=None,
                 approval_provider=None, approval_queue=None,
                 rendezvous_dir=None, clock=time.monotonic_ns,
                 ticket_fetcher=None, connector=None):
        """ticket_fetcher/connector override only the loopback dial mechanics.

        Production passes neither: the ticket comes from the private
        rendezvous directory and each dispatch dials 127.0.0.1. The seams
        exist so sandboxed tests can drive the real handshake, framing, HMAC
        and reply-validation logic over socketpair without network
        syscalls. Every seam failure fails closed like a missing bridge.
        """
        if approval_queue is not None:
            if approval_provider is not None:
                raise ValueError('ambiguous_approval_source')
            mapping = approval_queue

            def _from_queue(request_id, op, capsule_id, _mapping=mapping):
                try:
                    return _mapping.get((request_id, op, capsule_id))
                except Exception:
                    return None

            approval_provider = _from_queue
        if not callable(clock):
            raise ValueError('invalid_control_clock')
        self._audit_path = Path(audit_path).absolute()
        self._rendezvous_dir = (None if rendezvous_dir is None
                                else Path(rendezvous_dir).absolute())
        self._snapshot_provider = snapshot_provider
        self._approval_provider = approval_provider
        self._clock = clock
        self._ticket_fetcher = ticket_fetcher
        self._connector = connector
        self._selected = None
        self._ticket = None
        self._seen = {}
        self._consumed_tokens = set()
        self._uncertain = set()
        self._audit_failed = False
        self._lock = threading.Lock()

    @property
    def selected_capsule(self):
        return self._selected

    @property
    def uncertain_requests(self):
        return frozenset(self._uncertain)

    def execute(self, tool, args, now_ns=None):
        """Run one named control tool; always returns a closed MCP envelope."""
        if tool not in CONTROL_TOOLS:
            raise ValueError('unknown_control_tool')
        if type(args) is not dict:
            raise ValueError('invalid_control_arguments')
        now = self._clock() if now_ns is None else now_ns
        if type(now) is bool or type(now) is not int or now < 0:
            raise ValueError('invalid_control_clock')
        with self._lock:
            if tool == 'select_capsule':
                reply = self._select(args, now)
            else:
                reply = self._control(tool, args, now)
        text = json.dumps(reply, ensure_ascii=False, allow_nan=False,
                          separators=(',', ':'))
        return {'content': [{'type': 'text', 'text': text}],
                'structuredContent': reply,
                'isError': reply.get('state') not in SUCCESS_STATES}

    def _append_audit(self, *, ts_ns, op, request_hash, capsule_hash, epoch,
                      revision, state, error, effect_id):
        record = {'ts_ns': ts_ns, 'op': op,
                  'request_id_sha256': request_hash,
                  'capsule_id_sha256': capsule_hash,
                  'authority_epoch': epoch, 'expected_revision': revision,
                  'state': state, 'error': error, 'effect_id': effect_id}
        try:
            parent = self._audit_path.parent
            if (parent.is_symlink() or not parent.is_dir()
                    or parent.resolve() != parent.absolute()):
                return False
            line = (json.dumps(record, separators=(',', ':'), allow_nan=False)
                    .encode() + b'\n')
            with _audit_file(self._audit_path) as fd:
                offset = 0
                while offset < len(line):
                    written = os.write(fd, line[offset:])
                    if written <= 0:
                        return False
                    offset += written
                os.fsync(fd)
            return True
        except (OSError, ValueError, TypeError):
            return False

    def _load_snapshot(self):
        provider = self._snapshot_provider
        if provider is None or not callable(provider):
            return None
        try:
            snap = provider()
        except Exception:
            return None
        if type(snap) is not dict:
            return None
        epoch = snap.get('authority_epoch')
        revision = snap.get('revision')
        capsules = snap.get('capsules')
        if not _is_clock(epoch) or int(epoch) == 0:
            return None
        if revision is not None and not _is_clock(revision):
            return None
        if type(capsules) is not dict:
            return None
        return (epoch, revision, capsules)

    def _load_approval(self, request_id, op, capsule_id, now):
        try:
            raw = self._approval_provider(request_id, op, capsule_id)
        except Exception:
            raise _ProviderFailed from None
        if raw is None:
            return None
        if type(raw) is dict and set(raw) == {'wire', 'issued_ns'}:
            wire, issued = raw['wire'], raw['issued_ns']
        elif type(raw) in (tuple, list) and len(raw) == 2:
            wire, issued = raw
        else:
            raise _MalformedApproval
        if type(issued) is bool or type(issued) is not int or issued < 0:
            raise _MalformedApproval
        return (wire, issued)

    def _check_wire(self, wire, request_id, op, capsule_id, epoch, revision):
        if (type(wire) is not dict or set(wire) != {'schema', 'kind',
            'request_id', 'authority_epoch', 'expected_revision', 'body'}):
            raise _Denied('rejected', 'invalid_control_intent')
        if wire['schema'] != INTENT_SCHEMA or wire['kind'] != INTENT_KIND:
            raise _Denied('rejected', 'invalid_control_intent')
        if wire['request_id'] != request_id:
            raise _Denied('rejected', 'confirmation_binding_or_expiry')
        body = wire['body']
        if type(body) is not dict:
            raise _Denied('rejected', 'invalid_control_intent')
        if set(body) == {'op', 'confirmation_token', 'folder'}:
            raise _Denied('rejected', 'confirmation_binding_or_expiry')
        if set(body) != {'op', 'confirmation_token', 'capsule_id'}:
            raise _Denied('rejected', 'invalid_control_intent')
        if body['op'] != op:
            raise _Denied('rejected', 'confirmation_binding_or_expiry')
        token = body['confirmation_token']
        if not _is_token(token):
            raise _Denied('rejected', 'invalid_control_intent')
        bound_capsule = body['capsule_id']
        if type(bound_capsule) is not str:
            raise _Denied('rejected', 'invalid_control_intent')
        if bound_capsule != capsule_id:
            raise _Denied('rejected', 'confirmation_binding_or_expiry')
        if token in request_id or token in capsule_id:
            raise _Denied('rejected', 'invalid_control_intent')
        wire_epoch = wire['authority_epoch']
        if not _is_clock(wire_epoch):
            raise _Denied('rejected', 'invalid_control_intent')
        if wire_epoch != epoch:
            raise _Denied('rejected', 'control_identity_or_epoch_denied')
        wire_revision = wire['expected_revision']
        if not _is_clock(wire_revision):
            raise _Denied('rejected', 'invalid_control_intent')
        if revision is None or wire_revision != revision:
            raise _Denied('rejected', 'stale_revision')
        return token

    def _check_reply(self, raw, request_id, op):
        if (type(raw) is not dict or set(raw) != {'request_id', 'state',
                'revision', 'error', 'receipt'}):
            return None
        if raw['request_id'] != request_id:
            return None
        state = raw['state']
        if state not in CONTROL_STATES:
            return None
        revision = raw['revision']
        if not _is_clock(revision):
            return None
        error = raw['error']
        receipt = raw['receipt']
        if error is not None and (type(error) is not str
                                  or error not in AUTHORITY_ERRORS):
            return None
        if receipt is not None:
            if (type(receipt) is not dict
                    or set(receipt) != {'schema', 'op', 'effect_id'}):
                return None
            if (receipt['schema'] != RECEIPT_SCHEMA or receipt['op'] != op
                    or type(receipt['effect_id']) is not str
                    or _EFFECT.fullmatch(receipt['effect_id']) is None):
                return None
        if state == 'committed':
            if receipt is None or error is not None:
                return None
        elif state == 'pending':
            if receipt is not None or error is not None:
                return None
        elif receipt is not None or type(error) is not str:
            return None
        return {'request_id': request_id, 'state': state, 'revision': revision,
                'error': error,
                'receipt': None if receipt is None else dict(receipt)}

    def _ensure_ticket(self):
        if self._ticket is not None:
            return self._ticket
        if self._rendezvous_dir is None:
            raise _NoBridge
        fetcher = (self._ticket_fetcher if self._ticket_fetcher is not None
                   else _default_fetch_ticket)
        try:
            ticket = fetcher(self._rendezvous_dir)
        except Exception:
            raise _NoBridge from None
        if type(ticket) is not dict:
            raise _NoBridge
        self._ticket = ticket
        return ticket

    def _roundtrip(self, ticket, frame_bytes, request_id, op):
        if type(ticket) is not dict or ticket.get('host') != LOOPBACK:
            raise _TransportError(False)
        port = ticket.get('port')
        if type(port) is bool or type(port) is not int:
            raise _TransportError(False)
        connector = (self._connector if self._connector is not None
                     else _default_connector)
        try:
            sock = connector(ticket)
        except _TransportError:
            raise
        except Exception:
            raise _TransportError(False) from None
        try:
            sock.settimeout(5)
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            raise _TransportError(False) from None
        try:
            try:
                challenge = _read_frame(sock)
                if (type(challenge) is not dict
                        or set(challenge) != {'schema', 'identity', 'epoch',
                                              'nonce'}):
                    raise _TransportError(False)
                if (challenge['schema'] != 'cafe.broker.auth.v1'
                        or challenge['identity'] != BROKER_IDENTITY
                        or challenge['epoch'] != ticket['epoch']
                        or type(challenge['nonce']) is not str
                        or _NONCE.fullmatch(challenge['nonce']) is None):
                    raise _TransportError(False)
                message = ('cafe.broker.auth.v1\n' + BROKER_IDENTITY + '\n'
                           + ticket['epoch'] + '\n'
                           + challenge['nonce']).encode()
                proof = hmac.new(bytes.fromhex(ticket['secret']), message,
                                 hashlib.sha256).hexdigest()
                _write_frame(sock, {'identity': BROKER_IDENTITY,
                                    'epoch': ticket['epoch'], 'proof': proof})
                if _read_frame(sock) != {'authenticated': True}:
                    raise _TransportError(False)
            except _TransportError:
                raise
            except Exception:
                raise _TransportError(False) from None
            try:
                sock.sendall(frame_bytes)
            except Exception:
                raise _TransportError(True) from None
            try:
                raw_reply = _read_frame(sock)
            except Exception:
                raise _TransportError(True) from None
            reply = self._check_reply(raw_reply, request_id, op)
            if reply is None:
                raise _TransportError(True)
            return reply
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _consume(self, request_id, token_fingerprint):
        self._seen[request_id] = True
        self._consumed_tokens.add(token_fingerprint)

    def _select(self, args, now):
        raw = args.get('capsule_id')
        capsule_hash = _sha256_hex(raw) if type(raw) is str else None

        def terminal(state, error, echo, epoch=None, revision=None):
            if not self._append_audit(ts_ns=now, op='select_capsule',
                                      request_hash=None,
                                      capsule_hash=capsule_hash, epoch=epoch,
                                      revision=revision, state=state,
                                      error=error, effect_id=None):
                return {'capsule_id': echo, 'state': 'blocked',
                        'error': 'audit_unavailable'}
            return {'capsule_id': echo, 'state': state, 'error': error}

        if not _is_identifier(raw):
            return terminal('rejected', 'invalid_control_intent', 'invalid')
        snap = self._load_snapshot()
        if snap is None:
            return terminal('blocked', 'mcp_control_unavailable', raw)
        epoch, revision, capsules = snap
        if raw not in capsules:
            return terminal('rejected', 'capsule_unavailable', raw, epoch,
                            revision)
        if not self._append_audit(ts_ns=now, op='select_capsule',
                                  request_hash=None, capsule_hash=capsule_hash,
                                  epoch=epoch, revision=revision,
                                  state='selected', error=None, effect_id=None):
            return {'capsule_id': raw, 'state': 'blocked',
                    'error': 'audit_unavailable'}
        self._selected = raw
        return {'capsule_id': raw, 'state': 'selected', 'error': None}

    def _control(self, op, args, now):
        request_hash = capsule_hash = epoch = revision = None

        def audit_broken(echo):
            return {'request_id': echo, 'state': 'blocked', 'revision': None,
                    'error': 'audit_unavailable', 'receipt': None}

        def terminal(state, error, echo):
            if not self._append_audit(ts_ns=now, op=op,
                                      request_hash=request_hash,
                                      capsule_hash=capsule_hash, epoch=epoch,
                                      revision=revision, state=state,
                                      error=error, effect_id=None):
                return audit_broken(echo)
            return {'request_id': echo, 'state': state,
                    'revision': revision, 'error': error, 'receipt': None}

        request_id = args.get('request_id')
        if self._audit_failed:
            return audit_broken(request_id if _is_identifier(request_id) else 'invalid')
        if not _is_identifier(request_id):
            if type(request_id) is str:
                request_hash = _sha256_hex(request_id)
            if not self._append_audit(ts_ns=now, op=op,
                                      request_hash=request_hash,
                                      capsule_hash=None, epoch=None,
                                      revision=None, state='rejected',
                                      error='invalid_control_intent',
                                      effect_id=None):
                return audit_broken('invalid')
            return {'request_id': 'invalid', 'state': 'rejected',
                    'revision': None, 'error': 'invalid_control_intent',
                    'receipt': None}
        request_hash = _sha256_hex(request_id)
        raw_capsule = args.get('capsule_id')
        if raw_capsule is None:
            capsule_id = self._selected
        elif _is_identifier(raw_capsule):
            capsule_id = raw_capsule
        else:
            if type(raw_capsule) is str:
                capsule_hash = _sha256_hex(raw_capsule)
            if not self._append_audit(ts_ns=now, op=op,
                                      request_hash=request_hash,
                                      capsule_hash=capsule_hash, epoch=None,
                                      revision=None, state='rejected',
                                      error='invalid_control_intent',
                                      effect_id=None):
                return audit_broken(request_id)
            return {'request_id': request_id, 'state': 'rejected',
                    'revision': None, 'error': 'invalid_control_intent',
                    'receipt': None}
        if capsule_id is None:
            if not self._append_audit(ts_ns=now, op=op,
                                      request_hash=request_hash,
                                      capsule_hash=None, epoch=None,
                                      revision=None, state='rejected',
                                      error='capsule_unavailable',
                                      effect_id=None):
                return audit_broken(request_id)
            return {'request_id': request_id, 'state': 'rejected',
                    'revision': None, 'error': 'capsule_unavailable',
                    'receipt': None}
        capsule_hash = _sha256_hex(capsule_id)
        if request_id in self._seen:
            if not self._append_audit(ts_ns=now, op=op,
                                      request_hash=request_hash,
                                      capsule_hash=capsule_hash, epoch=None,
                                      revision=None, state='rejected',
                                      error='request_id_conflict',
                                      effect_id=None):
                return audit_broken(request_id)
            return {'request_id': request_id, 'state': 'rejected',
                    'revision': None, 'error': 'request_id_conflict',
                    'receipt': None}
        if len(self._seen) >= _SEEN_CAP or len(self._consumed_tokens) >= _TOKEN_CAP:
            return terminal('blocked', 'confirmation_capacity', request_id)
        snap = self._load_snapshot()
        if snap is None:
            if not self._append_audit(ts_ns=now, op=op,
                                      request_hash=request_hash,
                                      capsule_hash=capsule_hash, epoch=None,
                                      revision=None, state='blocked',
                                      error='mcp_control_unavailable',
                                      effect_id=None):
                return audit_broken(request_id)
            return {'request_id': request_id, 'state': 'blocked',
                    'revision': None, 'error': 'mcp_control_unavailable',
                    'receipt': None}
        epoch, revision, capsules = snap
        if capsule_id not in capsules:
            if not self._append_audit(ts_ns=now, op=op,
                                      request_hash=request_hash,
                                      capsule_hash=capsule_hash, epoch=epoch,
                                      revision=revision, state='rejected',
                                      error='capsule_unavailable',
                                      effect_id=None):
                return audit_broken(request_id)
            return {'request_id': request_id, 'state': 'rejected',
                    'revision': revision, 'error': 'capsule_unavailable',
                    'receipt': None}
        if self._approval_provider is None or not callable(
                self._approval_provider):
            if not self._append_audit(ts_ns=now, op=op,
                                      request_hash=request_hash,
                                      capsule_hash=capsule_hash, epoch=epoch,
                                      revision=revision, state='blocked',
                                      error='mcp_control_unavailable',
                                      effect_id=None):
                return audit_broken(request_id)
            return {'request_id': request_id, 'state': 'blocked',
                    'revision': revision, 'error': 'mcp_control_unavailable',
                    'receipt': None}
        try:
            approval = self._load_approval(request_id, op, capsule_id, now)
        except _ProviderFailed:
            return terminal('blocked', 'mcp_control_unavailable', request_id)
        except _MalformedApproval:
            return terminal('rejected', 'invalid_control_intent', request_id)
        if approval is None:
            if not self._append_audit(ts_ns=now, op=op,
                                      request_hash=request_hash,
                                      capsule_hash=capsule_hash, epoch=epoch,
                                      revision=revision, state='blocked',
                                      error='confirmation_required',
                                      effect_id=None):
                return audit_broken(request_id)
            return {'request_id': request_id, 'state': 'blocked',
                    'revision': revision, 'error': 'confirmation_required',
                    'receipt': None}
        wire, issued = approval
        possible_token = wire.get('body', {}).get('confirmation_token') if type(wire) is dict and type(wire.get('body')) is dict else None
        if type(possible_token) is str and _TOKEN.fullmatch(possible_token) and possible_token in request_id:
            return terminal('rejected', 'invalid_control_intent', 'invalid')
        try:
            token = self._check_wire(wire, request_id, op, capsule_id, epoch,
                                     revision)
        except _Denied as denied:
            return terminal(denied.state, denied.error, request_id)
        if issued > now or now - issued >= _TTL_NS:
            return terminal('rejected', 'confirmation_binding_or_expiry',
                            request_id)
        fingerprint = _sha256_hex(token)
        if fingerprint in self._consumed_tokens:
            return terminal('rejected', 'confirmation_binding_or_expiry',
                            request_id)
        try:
            ticket = self._ensure_ticket()
        except _NoBridge:
            return terminal('blocked', 'mcp_control_unavailable', request_id)
        if ticket.get('epoch') != epoch:
            return terminal('rejected', 'control_identity_or_epoch_denied',
                            request_id)
        frame = {'epoch': ticket['epoch'], 'capability': 'control',
                 'body': wire}
        try:
            frame_bytes = _frame_bytes(frame)
        except (ValueError, TypeError):
            return terminal('rejected', 'invalid_control_intent', request_id)
        if len(frame_bytes) > channel_auth.MAX_LINE:
            return terminal('rejected', 'invalid_control_intent', request_id)
        if not self._append_audit(ts_ns=now, op=op, request_hash=request_hash,
                                  capsule_hash=capsule_hash, epoch=epoch,
                                  revision=revision, state='pending',
                                  error=None, effect_id=None):
            return audit_broken(request_id)
        try:
            reply = self._roundtrip(ticket, frame_bytes, request_id, op)
        except _TransportError as failed:
            if failed.ambiguous:
                self._consume(request_id, fingerprint)
                self._uncertain.add(request_id)
                self._append_audit(ts_ns=now, op=op,
                                   request_hash=request_hash,
                                   capsule_hash=capsule_hash, epoch=epoch,
                                   revision=revision, state='interrupted',
                                   error='uncertain_dispatch', effect_id=None)
                return {'request_id': request_id, 'state': 'interrupted',
                        'revision': None, 'error': 'uncertain_dispatch',
                        'receipt': None}
            self._append_audit(ts_ns=now, op=op, request_hash=request_hash,
                               capsule_hash=capsule_hash, epoch=epoch,
                               revision=revision, state='blocked',
                               error='control_transport_error', effect_id=None)
            return {'request_id': request_id, 'state': 'blocked',
                    'revision': revision, 'error': 'control_transport_error',
                    'receipt': None}
        self._consume(request_id, fingerprint)
        receipt = reply['receipt']
        # Outcome line: expected_revision carries the authority's resulting
        # revision (the admission line already recorded the expectation).
        if not self._append_audit(ts_ns=now, op=op, request_hash=request_hash,
                           capsule_hash=capsule_hash, epoch=epoch,
                           revision=reply['revision'], state=reply['state'],
                           error=reply['error'],
                           effect_id=(None if receipt is None
                                      else receipt['effect_id'])):
            self._audit_failed = True
            self._uncertain.add(request_id)
            return {'request_id':request_id, 'state':'interrupted',
                    'revision':None, 'error':'audit_unavailable', 'receipt':None}
        return reply


_CLI_OPS = {'select': 'select_capsule', 'start': 'start_recording',
            'stop': 'stop_recording', 'send': 'send_to_obs'}


def main(argv=None):
    """Optional control CLI. No providers exist on this path: always blocked."""
    parser = argparse.ArgumentParser(
        prog='mcp_control', description=__doc__.splitlines()[0] if __doc__
        else 'cafe control CLI')
    parser.add_argument('--audit', required=True)
    parser.add_argument('--rendezvous', default=None)
    sub = parser.add_subparsers(dest='command', required=True)
    selected = sub.add_parser('select')
    selected.add_argument('--capsule-id', required=True)
    for name in ('start', 'stop', 'send'):
        command = sub.add_parser(name)
        command.add_argument('--request-id', required=True)
        command.add_argument('--capsule-id', default=None)
    args = parser.parse_args(argv)
    plane = ControlPlane(args.audit, rendezvous_dir=args.rendezvous)
    tool = _CLI_OPS[args.command]
    if tool == 'select_capsule':
        call = {'capsule_id': args.capsule_id}
    else:
        call = {'request_id': args.request_id}
        if args.capsule_id is not None:
            call['capsule_id'] = args.capsule_id
    reply = plane.execute(tool, call)['structuredContent']
    print(json.dumps(reply, separators=(',', ':')))
    return 0 if reply.get('state') in SUCCESS_STATES else 1


if __name__ == '__main__':
    raise SystemExit(main())

