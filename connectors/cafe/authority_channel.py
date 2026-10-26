"""Private Rust-supervised connector child (P08).

Bounded single-line JSONL over anonymous stdio pipes. All framing and
validation helpers come from the Codex-owned `.channel_auth` module; this
module only streams bounded lines, tracks the private in-memory lease, and
dispatches `approved_effect` bodies to the injected handler. Never logs,
echoes, or exposes token material on stdout, stderr, or exceptions.
"""

import asyncio
import json
import sys
import time

from .channel_auth import MAX_LINE, make_frame, validate_frame, validate_lease


def _write_stdout_frame(frame):
    """Serialize one frame to stdout; used only via a single awaited writer."""
    data = json.dumps(frame, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(data) > MAX_LINE:
        raise ValueError("frame_too_large")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


async def serve_connector(handler=None):
    """Run the connector loop until EOF; return a fixed process exit code.

    The token lease lives only in function-local memory and is cleared in
    `finally` on every exit path. Every frame is validated before any state
    change or handler call. Handler `Exception`s fail closed; `BaseException`
    (e.g. handler-initiated `SystemExit`) propagates after cleanup.
    """
    active_token = None
    active_expiry_ms = 0
    epoch = None
    try:
        while True:
            raw = await asyncio.to_thread(sys.stdin.buffer.readline, MAX_LINE + 1)
            if raw == b"":
                return 0 if epoch is not None else 1
            if len(raw) > MAX_LINE or not raw.endswith(b"\n"):
                return 2
            try:
                frame = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError):
                return 2
            if not isinstance(frame, dict):
                return 2
            if epoch is None:
                try:
                    epoch = validate_frame(frame, None, True)
                except ValueError:
                    return 2
                await asyncio.to_thread(
                    _write_stdout_frame, make_frame("observation", epoch, {"ready": True})
                )
                continue
            if frame.get("kind") == "connector_start":
                return 2
            try:
                validate_frame(frame, epoch, True)
            except ValueError:
                return 2
            if frame["kind"] == "token_lease":
                try:
                    validate_lease(frame["body"])
                except ValueError:
                    active_token = None
                    active_expiry_ms = 0
                    await asyncio.to_thread(
                        _write_stdout_frame,
                        make_frame("effect_result", epoch, {"accepted": False}),
                    )
                    continue
                expiry_ms = int(frame["body"]["expires_unix_ms"])
                remaining_ms = expiry_ms - time.time_ns() // 1_000_000
                if not 0 < remaining_ms <= 5000:
                    active_token = None
                    active_expiry_ms = 0
                    await asyncio.to_thread(
                        _write_stdout_frame,
                        make_frame("effect_result", epoch, {"accepted": False}),
                    )
                    continue
                active_token = frame["body"]["token"]
                active_expiry_ms = expiry_ms
                await asyncio.to_thread(
                    _write_stdout_frame,
                    make_frame("effect_result", epoch, {"accepted": True}),
                )
            elif frame["kind"] == "approved_effect":
                if handler is None:
                    accepted = False
                else:
                    try:
                        accepted = bool(await handler(frame["body"]))
                    except Exception:
                        return 2
                await asyncio.to_thread(
                    _write_stdout_frame,
                    make_frame("effect_result", epoch, {"accepted": accepted}),
                )
            else:
                return 2
    finally:
        active_token = None
        active_expiry_ms = 0


if __name__ == "__main__":
    try:
        _code = asyncio.run(serve_connector())
    except SystemExit:
        raise
    except BaseException:
        _code = 2
    if not isinstance(_code, int):
        _code = 2
    raise SystemExit(_code)
