"""Pure, bounded control proposals awaiting a separate native decision.

Integration is deliberately absent: no wire tool, IPC route, approval issuer,
effect adapter, or file-backed provider is registered here. The parent must
inject a reliable synchronous ``snapshot_provider()`` identity projection and
a trusted monotonic ``clock_ns``. This callback differs from the read-model
provider ``(kind, arguments)``. The read-model ``list_capsules`` rows are
``{capsule_id, slot, generation, person_label, feeds:[{feed_id, role,
generation}]}``; the adapter must project the closed role-keyed ControlView
without treating ``slot-N`` aliases as authority identifiers. Recorder rows
``{capsule_id, state, feeds:[{feed_id, state, written_frames, backpressure}]}``
and their string counters remain in the read-model host. Workspace events
``choose-window({capsule_id, role})``, ``record({capsule_id})``,
``stop({capsule_id})``, ``obs({capsule_id})`` and ``choose-folder()`` remain
in the parent controller. The chosen folder never enters a proposal.

The parent must revalidate the human gesture, epoch, revision and identities
before a native decision, and the native executor must revalidate again before
any effect. Native bind_window support is absent at the base; a synthetic view
and a consumed proposal do not supply that missing integration. Pending means
queue admission only, and consumption means a private handoff only.

One serialized owner calls this library. Reentrant callbacks fail closed; no
threading primitive or wall clock is used. All retained request tombstones are
session scoped and are never evicted. A new session requires a new queue and
parent-owned fencing of obsolete handlers.
"""

from dataclasses import dataclass
import json
import re

__all__ = ("ProposalQueue",)

_SCHEMA = "cafe.control_proposal.v1"
_TTL_NS = 30_000_000_000
_NS_PER_MS = 1_000_000
_MAX_PENDING = 64
_MAX_SEEN = 1024
# Existing native bounds: runtime_main capsule admission and
# windows_window_registry enumeration. A window page is not a global list.
_MAX_CAPSULES = 2
_MAX_WINDOWS = 4096
_ROLES = frozenset({"game", "camera"})
_OPS = frozenset({"bind_window", "start_recording", "stop_recording", "send_to_obs"})
# Frozen control identifier rule from mcp_control._is_identifier. Importing
# that effect-bearing host is unnecessary for this standalone pure library.
_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}")
_CLOCK = re.compile(r"0|[1-9][0-9]{0,19}")
_U64_MAX = "18446744073709551615"
_ARG_KEYS = frozenset({"request_id", "op", "capsule_id", "authority_epoch",
                       "expected_revision"})
_WINDOW_KEYS = frozenset({"token", "generation", "layout_revision"})


def _identifier(value):
    return type(value) is str and _IDENT.fullmatch(value) is not None


def _clock(value):
    # Length/lexicographic comparison preserves the exact string, even above
    # the range that floating-point or JavaScript numbers can represent.
    return (type(value) is str and _CLOCK.fullmatch(value) is not None
            and (len(value) < len(_U64_MAX) or value <= _U64_MAX))


def _window_token(value):
    if type(value) is not str or not value or len(value) > 128 or "\0" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= 128
    except UnicodeError:
        return False


def _closed(value, keys):
    return (type(value) is dict and len(value) == len(keys)
            and all(type(key) is str for key in value) and set(value) == keys)


@dataclass(frozen=True, slots=True, repr=False)
class _FeedIdentity:
    feed_id: str
    role: str
    generation: str


@dataclass(frozen=True, slots=True, repr=False)
class _CapsuleIdentity:
    capsule_id: str
    generation: str
    feeds: tuple


@dataclass(frozen=True, slots=True, repr=False)
class _WindowIdentity:
    token: str
    generation: str
    layout_revision: str


@dataclass(frozen=True, slots=True, repr=False)
class _WindowSelection:
    identity: _WindowIdentity
    role: str


@dataclass(frozen=True, slots=True, repr=False)
class _Request:
    request_id: str
    op: str
    capsule_id: str
    authority_epoch: str
    revision: str
    window: _WindowSelection | None
    body: bytes


@dataclass(frozen=True, slots=True, repr=False)
class _ControlView:
    authority_epoch: str
    revision: str
    capsules: tuple
    windows: tuple


@dataclass(frozen=True, slots=True, repr=False)
class _BoundProposal:
    """Private immutable snapshot, neither consent nor a durable receipt."""

    proposal_id: str
    request_id: str
    op: str
    authority_epoch: str
    revision: str
    capsule: _CapsuleIdentity
    window: _WindowSelection | None
    expires_at_ns: int


@dataclass(slots=True, repr=False)
class _Entry:
    request: _Request
    proposal_id: str | None = None
    capsule: _CapsuleIdentity | None = None
    expires_at_ns: int | None = None
    state: str = "blocked"
    error: str | None = "confirmation_required"


def _window_identity(value):
    if (not _closed(value, _WINDOW_KEYS)
            or not _window_token(value["token"])
            or not _clock(value["generation"])
            or not _clock(value["layout_revision"])):
        return None
    return _WindowIdentity(value["token"], value["generation"],
                           value["layout_revision"])


def _request(value):
    if type(value) is not dict:
        return None
    op = value.get("op")
    if type(op) is not str or op not in _OPS:
        return None
    keys = _ARG_KEYS | {"window"} if op == "bind_window" else _ARG_KEYS
    if (not _closed(value, keys) or not _identifier(value["request_id"])
            or not _identifier(value["capsule_id"])
            or not _clock(value["authority_epoch"])
            or not _clock(value["expected_revision"])):
        return None
    window = None
    if op == "bind_window":
        selected = value["window"]
        if (not _closed(selected, _WINDOW_KEYS | {"role"})
                or type(selected["role"]) is not str
                or selected["role"] not in _ROLES):
            return None
        identity = _window_identity({key: selected[key] for key in _WINDOW_KEYS})
        if identity is None:
            return None
        window = _WindowSelection(identity, selected["role"])
    # A dict API has no original wire bytes. Byte identity therefore means
    # this fixed UTF-8 JSON encoding; key order is immaterial, strings are
    # never normalized, and the complete validated body is retained exactly.
    body = json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")
    return _Request(value["request_id"], op, value["capsule_id"],
                    value["authority_epoch"], value["expected_revision"],
                    window, body)


def _view(value):
    if (not _closed(value, {"authority_epoch", "revision", "capsules", "windows"})
            or not _clock(value["authority_epoch"])
            or not _clock(value["revision"])):
        return None
    capsules, windows = value["capsules"], value["windows"]
    if (type(capsules) is not dict or len(capsules) > _MAX_CAPSULES
            or type(windows) is not list or len(windows) > _MAX_WINDOWS):
        return None
    capsule_ids, feed_ids, window_tokens = set(), set(), set()
    frozen_capsules, frozen_windows = [], []
    for key, capsule in capsules.items():
        if (not _identifier(key)
                or not _closed(capsule, {"capsule_id", "generation", "feeds"})
                or not _identifier(capsule["capsule_id"])
                or capsule["capsule_id"] != key or key in capsule_ids
                or not _clock(capsule["generation"])):
            return None
        capsule_ids.add(key)
        feeds = capsule["feeds"]
        if type(feeds) is not dict or len(feeds) > len(_ROLES):
            return None
        frozen_feeds = []
        for role, feed in feeds.items():
            if (type(role) is not str or role not in _ROLES
                    or not _closed(feed, {"feed_id", "role", "generation"})
                    or not _identifier(feed["feed_id"])
                    or feed["feed_id"] in feed_ids
                    or type(feed["role"]) is not str or feed["role"] != role
                    or not _clock(feed["generation"])):
                return None
            feed_ids.add(feed["feed_id"])
            frozen_feeds.append((role, _FeedIdentity(feed["feed_id"], role,
                                                     feed["generation"])))
        frozen_capsules.append(_CapsuleIdentity(key, capsule["generation"],
                                                tuple(sorted(frozen_feeds))))
    for window in windows:
        frozen = _window_identity(window)
        if frozen is None or frozen.token in window_tokens:
            return None
        window_tokens.add(frozen.token)
        frozen_windows.append(frozen)
    return _ControlView(value["authority_epoch"], value["revision"],
                        tuple(frozen_capsules), tuple(frozen_windows))


def _reply(entry=None, *, state="rejected", error="invalid_proposal", now=None):
    remaining = None
    if entry is not None:
        state, error = entry.state, entry.error
        if state == "pending":
            # Integer ceiling: a live sub-millisecond proposal displays 1 ms.
            remaining = (entry.expires_at_ns - now + _NS_PER_MS - 1) // _NS_PER_MS
    return {"schema": _SCHEMA,
            "proposal_id": None if entry is None else entry.proposal_id,
            "request_id": None if entry is None else entry.request.request_id,
            "state": state, "error": error, "expires_in_ms": remaining}


def _terminal(entry, state, error):
    entry.state, entry.error = state, error
    entry.capsule = None


class ProposalQueue:
    """Bounded session queue; all effects and native decisions remain outside."""

    def __init__(self, snapshot_provider=None, *, clock_ns, max_pending=64):
        if (not callable(clock_ns) or type(max_pending) is not int
                or not 1 <= max_pending <= _MAX_PENDING):
            raise ValueError("invalid_proposal")
        self._snapshot_provider = snapshot_provider
        self._clock_ns = clock_ns
        self._max_pending = max_pending
        self._requests = {}
        self._proposals = {}
        self._next_id = 1
        self._last_ns = None
        self._clock_failed = False
        self._active = False
        self._reentered = False

    def _known_request(self, arguments):
        if type(arguments) is dict:
            identity = arguments.get("request_id")
            if _identifier(identity):
                return self._requests.get(identity)
        return None

    def _known_proposal(self, proposal_id):
        return self._proposals.get(proposal_id) if _identifier(proposal_id) else None

    def _enter(self, entry):
        if self._active:
            self._reentered = True
            if entry is not None and entry.state == "pending":
                _terminal(entry, "blocked", "confirmation_required")
            return False
        self._active = True
        self._reentered = False
        return True

    def _now(self):
        if self._clock_failed:
            return None
        try:
            now = self._clock_ns()
        except Exception:
            now = None
        if (type(now) is not int or now < 0 or self._reentered
                or (self._last_ns is not None and now < self._last_ns)):
            self._clock_failed = True
            for entry in self._proposals.values():
                if entry.state == "pending":
                    _terminal(entry, "blocked", "provider_unavailable")
            return None
        self._last_ns = now
        for entry in self._proposals.values():
            if entry.state == "pending" and now >= entry.expires_at_ns:
                _terminal(entry, "expired", "proposal_expired")
        return now

    def _snapshot(self):
        if not callable(self._snapshot_provider):
            return None
        try:
            value = self._snapshot_provider()
            return None if self._reentered else _view(value)
        except Exception:
            return None

    @staticmethod
    def _binding(request, view, prior=None):
        if (view.authority_epoch != request.authority_epoch
                or view.revision != request.revision):
            return None, "stale_revision"
        capsule = next((item for item in view.capsules
                        if item.capsule_id == request.capsule_id), None)
        if capsule is None:
            return None, "confirmation_required"
        if prior is not None and capsule != prior:
            return None, "stale_revision"
        if request.window is not None:
            selected = request.window.identity
            window = next((item for item in view.windows
                           if item.token == selected.token), None)
            if window is None:
                # A partial list cannot prove global absence or authorize a
                # replacement. The chosen identity must be present now.
                return None, "confirmation_required"
            if window != selected:
                return None, "stale_revision"
        return capsule, None

    def propose(self, arguments: dict) -> dict:
        """Admit an inert proposal, or return one closed refusal."""
        known = self._known_request(arguments)
        if not self._enter(known):
            return _reply(known, state="blocked", error="confirmation_required")
        try:
            request = _request(arguments)
            if request is None:
                result = _reply(state="rejected", error="invalid_proposal")
                if known is not None:
                    result["request_id"] = known.request.request_id
                    result["proposal_id"] = known.proposal_id
                return result
            if known is not None and known.request.body != request.body:
                result = _reply(state="rejected", error="request_id_conflict")
                result["request_id"], result["proposal_id"] = (
                    known.request.request_id, known.proposal_id)
                return result
            now = self._now()
            if known is not None:
                return _reply(known, now=now)
            if (len(self._requests) >= _MAX_SEEN
                    or sum(item.state == "pending" for item in self._proposals.values())
                    >= self._max_pending):
                return _reply(error="proposal_capacity")
            entry = _Entry(request)
            self._requests[request.request_id] = entry
            if now is None:
                _terminal(entry, "blocked", "provider_unavailable")
                return _reply(entry)
            view = self._snapshot()
            after = self._now()
            if view is None or after is None:
                _terminal(entry, "blocked", "provider_unavailable")
                return _reply(entry)
            capsule, error = self._binding(request, view)
            if error is not None:
                _terminal(entry, "blocked", error)
                return _reply(entry)
            # The original sample starts the TTL; a slow provider cannot
            # extend it or return a new proposal after the deadline.
            entry.expires_at_ns = now + _TTL_NS
            if after >= entry.expires_at_ns:
                _terminal(entry, "expired", "proposal_expired")
                return _reply(entry)
            entry.proposal_id = "proposal-" + str(self._next_id)
            self._next_id += 1
            entry.capsule = capsule
            entry.state, entry.error = "pending", None
            self._proposals[entry.proposal_id] = entry
            return _reply(entry, now=after)
        finally:
            self._active = False

    def inspect(self, proposal_id: str) -> dict:
        """Return stored state and display lifetime, never native consent."""
        entry = self._known_proposal(proposal_id)
        if not self._enter(entry):
            return _reply(entry, state="blocked", error="confirmation_required")
        try:
            if entry is None:
                return _reply()
            now = self._now()
            return _reply(entry, now=now)
        finally:
            self._active = False

    def reject(self, proposal_id: str) -> dict:
        """Permanently dismiss a known proposal without emitting an effect."""
        entry = self._known_proposal(proposal_id)
        if not self._enter(entry):
            return _reply(entry, state="blocked", error="confirmation_required")
        try:
            if entry is None:
                return _reply()
            self._now()
            if entry.state == "pending":
                _terminal(entry, "rejected", "confirmation_required")
            return _reply(entry)
        finally:
            self._active = False

    def consume_for_native_decision(self, proposal_id: str, *,
                                    authority_epoch: str, revision: str):
        """Hand one frozen snapshot to the native decider, at most once.

        This method is not an approval route. Its return value cannot bypass
        the parent's native confirmation and the executor's independent checks.
        A consumed entry exposes only rejected/proposal_consumed thereafter.
        """
        entry = self._known_proposal(proposal_id)
        if not self._enter(entry):
            return None
        try:
            if entry is None:
                return None
            now = self._now()
            if entry.state != "pending" or now is None:
                return None
            if not _clock(authority_epoch) or not _clock(revision):
                _terminal(entry, "blocked", "invalid_proposal")
                return None
            request = entry.request
            if (authority_epoch != request.authority_epoch
                    or revision != request.revision):
                _terminal(entry, "blocked", "stale_revision")
                return None
            view = self._snapshot()
            after = self._now()
            if entry.state != "pending":
                return None
            if view is None or after is None:
                _terminal(entry, "blocked", "provider_unavailable")
                return None
            capsule, error = self._binding(request, view, entry.capsule)
            if error is not None:
                _terminal(entry, "blocked", error)
                return None
            bound = _BoundProposal(entry.proposal_id, request.request_id,
                                   request.op, request.authority_epoch,
                                   request.revision, capsule, request.window,
                                   entry.expires_at_ns)
            _terminal(entry, "rejected", "proposal_consumed")
            return bound
        finally:
            self._active = False
