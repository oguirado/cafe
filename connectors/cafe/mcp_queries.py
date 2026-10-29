"""Stdlib-only readonly MCP queries over validated chat events.

Consumed by ``connectors.cafe.mcp_server``: the server validates tool names
and arguments, confines data reads, bounds scans, and frames JSON-RPC.
This module only transforms the validated single-pass ``events`` iterable
into deterministic JSON-compatible results. No I/O, no output, no state.
"""

from datetime import datetime, timezone
import heapq
import re
from typing import Any, Dict, Iterable, List

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
TOP_CHATTER_LIMIT = 200
MAX_BUCKETS = 1000
MAX_MESSAGE_RATE_BUCKETS = 1000

_UTC_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d{1,9}))?(Z|\+00:00)$"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _utc_to_ns(value: str) -> int:
    """Convert a UTC instant string to integer epoch nanoseconds (exact)."""
    match = _UTC_RE.match(value)
    if match is None:
        raise ValueError("invalid_utc")
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    moment = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    delta = moment - _EPOCH
    fraction = match.group(7) or ""
    nanos = int(fraction.ljust(9, "0")) if fraction else 0
    return (delta.days * 86400 + delta.seconds) * 1000000000 + nanos


def _utc_to_ms(value: str) -> int:
    """Convert a UTC instant string to integer epoch milliseconds (no float)."""
    return _utc_to_ns(value) // 1000000


def _recv_ns(event: Dict[str, Any]) -> int:
    return _utc_to_ns(event["received_at"]["utc"])


def _recv_ms(event: Dict[str, Any]) -> int:
    return _recv_ns(event) // 1000000


def _mono_ns(event: Dict[str, Any]) -> int:
    return int(event["received_at"]["monotonic_ns"])


def _msg_key(event: Dict[str, Any]) -> str:
    message_id = event["message_id"]
    return "" if message_id is None else message_id


class _Candidate:
    """Orderable event wrapper; monotonic time only breaks ties in-session."""

    __slots__ = ("event", "recv_ns", "mono_ns", "msg", "session", "seq")

    def __init__(self, event, recv_ns, mono_ns, msg, session, seq):
        self.event = event
        self.recv_ns = recv_ns
        self.mono_ns = mono_ns
        self.msg = msg
        self.session = session
        self.seq = seq

    def __lt__(self, other):
        # Establish a total order: session precedes monotonic time so clocks
        # are compared only inside the same session, never in a cycle.
        return (self.recv_ns, self.session, self.mono_ns, self.msg, self.seq) < (
            other.recv_ns, other.session, other.mono_ns, other.msg, other.seq)


def _bounded_newest(events: Iterable[_Candidate], limit: int) -> List[Dict[str, Any]]:
    heap: List[_Candidate] = []
    for candidate in events:
        heapq.heappush(heap, candidate)
        if len(heap) > limit:
            heapq.heappop(heap)
    heap.sort()
    return [candidate.event for candidate in heap]


def _list_channels_and_days(events: Iterable[Dict[str, Any]]) -> Any:
    found: Dict[str, set] = {}
    for event in events:
        found.setdefault(event["channel"], set()).add(event["received_at"]["utc"][:10])
    return [{"channel": channel, "days": sorted(days)} for channel, days in sorted(found.items())]


def _recent_messages(arguments: Dict[str, Any], events: Iterable[Dict[str, Any]]) -> Any:
    channel = arguments["channel"]
    limit = arguments.get("limit", DEFAULT_LIMIT)
    since = arguments.get("since")
    since_ns = _utc_to_ns(since) if since is not None else None

    def candidates():
        seq = 0
        for event in events:
            if event["kind"] != "message" or event["channel"] != channel:
                continue
            recv_ns = _recv_ns(event)
            if since_ns is not None and recv_ns < since_ns:
                continue
            yield _Candidate(event, recv_ns, _mono_ns(event), _msg_key(event), event["session_id"], seq)
            seq += 1

    return _bounded_newest(candidates(), limit)


def _search_messages(arguments: Dict[str, Any], events: Iterable[Dict[str, Any]]) -> Any:
    channel = arguments["channel"]
    query = arguments["query"]
    limit = arguments.get("limit", DEFAULT_LIMIT)

    def candidates():
        seq = 0
        for event in events:
            if event["kind"] != "message" or event["channel"] != channel:
                continue
            if query not in event["text"]:
                continue
            yield _Candidate(event, _recv_ns(event), _mono_ns(event), _msg_key(event), event["session_id"], seq)
            seq += 1

    return _bounded_newest(candidates(), limit)


def _top_chatters(arguments: Dict[str, Any], events: Iterable[Dict[str, Any]], now_ns: int) -> Any:
    channel = arguments["channel"]
    start_ns = now_ns - arguments["window_minutes"] * 60000000000
    counts: Dict[str, int] = {}
    for event in events:
        if event["kind"] != "message" or event["channel"] != channel:
            continue
        recv_ns = _recv_ns(event)
        if recv_ns < start_ns or recv_ns > now_ns:
            continue
        login = event["user_login"]
        if not login:
            continue
        counts[login] = counts.get(login, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [{"user_login": login, "messages": total} for login, total in ranked[:TOP_CHATTER_LIMIT]]


def _message_rate(arguments: Dict[str, Any], events: Iterable[Dict[str, Any]]) -> Any:
    channel = arguments["channel"]
    bucket_ms = arguments["bucket_seconds"] * 1000
    counts: Dict[int, int] = {}
    for event in events:
        if event["kind"] != "message" or event["channel"] != channel:
            continue
        bucket = (_recv_ms(event) // bucket_ms) * bucket_ms
        if bucket not in counts:
            if len(counts) >= MAX_MESSAGE_RATE_BUCKETS:
                raise ValueError("message_rate_bucket_bound_exceeded")
            counts[bucket] = 0
        counts[bucket] += 1
    return [{"bucket_start_ms": bucket, "messages": counts[bucket]} for bucket in sorted(counts)]


def _session_summary(arguments: Dict[str, Any], events: Iterable[Dict[str, Any]]) -> Any:
    channel = arguments["channel"]
    day = arguments["day"]
    messages = 0
    chatters = set()
    minutes: Dict[int, int] = {}
    first_ms = None
    last_ms = None
    for event in events:
        if event["kind"] != "message" or event["channel"] != channel:
            continue
        if event["received_at"]["utc"][:10] != day:
            continue
        recv_ms = _recv_ms(event)
        messages += 1
        login = event["user_login"]
        if login:
            chatters.add(login)
        minute = recv_ms // 60000
        minutes[minute] = minutes.get(minute, 0) + 1
        if first_ms is None or recv_ms < first_ms:
            first_ms = recv_ms
        if last_ms is None or recv_ms > last_ms:
            last_ms = recv_ms
    return {
        "messages": messages,
        "unique_chatters": len(chatters),
        "peak_per_minute": max(minutes.values()) if minutes else 0,
        "first_message_ms": first_ms,
        "last_message_ms": last_ms,
    }


def execute(name: str, arguments: dict, events: Iterable[dict], now_ms: int, *, now_ns=None) -> Any:
    """Run one validated readonly query over a single-pass event iterable."""
    if type(arguments) is not dict:
        raise ValueError("invalid_arguments")
    if now_ns is None:
        # Explicit legacy API receives integer milliseconds, not float time.
        if type(now_ms) is not int:
            raise ValueError("invalid_query_clock")
        now_ns = now_ms * 1000000
    if type(now_ns) is not int:
        raise ValueError("invalid_query_clock")
    try:
        if name == "list_channels_and_days":
            return _list_channels_and_days(events)
        if name == "recent_messages":
            return _recent_messages(arguments, events)
        if name == "search_messages":
            return _search_messages(arguments, events)
        if name == "top_chatters":
            return _top_chatters(arguments, events, now_ns)
        if name == "message_rate":
            return _message_rate(arguments, events)
        if name == "session_summary":
            return _session_summary(arguments, events)
    except KeyError as exc:
        raise ValueError("invalid_event") from exc
    raise ValueError("unknown_tool")
