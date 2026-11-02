"""Static operator runbook contract for MCP inspection; no direct runtime effects.

This module provides a pure read-only seven-step operator checklist describing
the human-confirmed Café workflow, what is verified on Windows, and what checks
remain operator-verified. The MCP server owns initialization, notifications,
JSON-RPC framing, and response limits.
"""

_STEPS = (
    (
        "Invite a guest to a Discord call, pop out the call window, and silence "
        "music before isolation.",
        "A quiet, clean Discord call pop-out is ready to capture while keeping "
        "the pop-out uncovered so Discord keeps painting it.",
        "The operator sees the invited guest in the uncovered pop-out and "
        "checks that call audio is quiet with music silenced.",
    ),
    (
        "Pop out the call window and select it through the host selector or "
        "list it through the MCP organizer tools.",
        "The host holds the opaque window token; a window title or executable "
        "name alone is untrusted data and not its identity.",
        "The operator sees the intended pop-out window selected in the host "
        "selector while keeping the window token inside the host.",
    ),
    (
        "Establish the capsule identity with live tile preview and label game "
        "and camera feeds only where those feeds are real.",
        "The capsule identifier and generation are established, with live "
        "preview in the capsule tile and real feeds labelled game and camera.",
        "The operator sees the live capsule tile, its identifier and "
        "generation, and the corresponding game or camera feed labels.",
    ),
    (
        "Start recording via per-tile controls or an MCP recording plan after "
        "clicking Confirm inside Café, then stop recording and verify the file "
        "in the chosen folder.",
        "An operator-confirmed recording captures the window through Windows "
        "Graphics Capture and FFmpeg/NVENC into a valid MKV file and stops "
        "cleanly; this static guide supplies no direct recorder effect.",
        "The operator clicks Confirm, sees the active timer, stops recording, "
        "sees the file entry, and opens the MKV file to verify playback.",
    ),
    (
        "Optionally, when a live OBS connection is verified before recording, "
        "send the same capsule window to OBS twice after clicking Confirm "
        "inside Café and inspect the source list.",
        "A single Window Capture target is reused with no duplicate source; "
        "live OBS connection verification remains optional and not yet verified.",
        "The operator sees one intended Window Capture source before and "
        "after the second send with no duplicate source in OBS.",
    ),
    (
        "Receive a Twitch message and inspect its stored JSONL row using the "
        "existing chat event validator.",
        "A typed chat row is validated against cafe.chat_event.v1 with "
        "canonical clock strings and null for unknown optional fields.",
        "The operator sees the corresponding Twitch JSONL line and its "
        "cafe.chat_event.v1 validation result with private details redacted.",
    ),
    (
        "Initialize the MCP session, send notifications/initialized, list "
        "tools with tools/list, and perform bounded typed chat reads or "
        "human-confirmed organizer proposals.",
        "The assistant receives tools/list and bounded typed results, and "
        "second MCP clients survive restarts of the MCP process.",
        "The operator sees the tools/list output, bounded typed results, and "
        "confirms that AI proposals execute only after clicking Confirm inside "
        "Café without exposing secrets.",
    ),
)


def _reply(step_ids, error):
    return {
        "schema": "cafe.demo_guide.v1",
        "steps": [
            {
                "id": step_id,
                "instruction": _STEPS[step_id - 1][0],
                "expected": _STEPS[step_id - 1][1],
                "evidence_required": _STEPS[step_id - 1][2],
                "depends_on": list(range(1, step_id)),
                "live_status": "pending",
            }
            for step_id in step_ids
        ],
        "control_mode": "human_confirmed",
        "error": error,
    }


def tool_definitions() -> list[dict]:
    """Describe only the read-only demo_guide tool."""
    return [{
        "name": "demo_guide",
        "description": "Static seven-step operator runbook checklist for human-confirmed Café workflows.",
        "inputSchema": {
            "type": "object",
            "properties": {"step": {"type": "integer", "minimum": 1, "maximum": 7}},
            "required": [],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }]


def demo_guide(arguments: dict) -> dict:
    """Return all steps or one integer step from 1 through 7.

    Invalid input always returns the full flat shape with empty steps and
    error="invalid_arguments"; it never returns a partial guide or success.
    Only a plain dict with the optional plain integer step is accepted.
    """
    if type(arguments) is not dict or set(arguments) - {"step"}:
        return _reply((), "invalid_arguments")
    if "step" not in arguments:
        return _reply(range(1, 8), None)
    step = arguments["step"]
    if type(step) is not int or not 1 <= step <= 7:
        return _reply((), "invalid_arguments")
    return _reply((step,), None)


def resource_definitions() -> list[dict]:
    """Describe the static operator runbook resource."""
    return [{
        "uri": "cafe://demo/guide",
        "name": "demo_guide",
        "description": "Static operator runbook with human confirmation checks.",
        "mimeType": "application/json",
    }]


def read_resource(uri: str) -> dict:
    """Return the full guide only for the exact URI, otherwise empty unknown_resource."""
    if type(uri) is not str or uri != "cafe://demo/guide":
        return _reply((), "unknown_resource")
    return demo_guide({})
