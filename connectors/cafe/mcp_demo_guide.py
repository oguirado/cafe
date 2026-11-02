"""Static demo contract proposed for later MCP registration; no live effects.

This module does not connect the recorder, native confirmation provider, or
runtime data to MCP. All observations remain pending. The unchanged server owns
initialization, notifications, JSON-RPC framing, and response limits; a future
integration must frame these flat objects there, without an inner envelope.
"""

_STEPS = (
    (
        "Invite a guest to a Discord call and silence music before isolation.",
        "A quiet, clean call is ready to isolate; live verification is pending.",
        "The operator sees the invited guest in the call and checks the audio "
        "is quiet with music silenced; retain redacted acceptance evidence.",
    ),
    (
        "Pop out the call window and select it through the host selector.",
        "The host holds the selected window token; a title or executable name "
        "alone is not its identity. Live verification is pending.",
        "The operator sees the intended pop-out selected in the host selector; "
        "keep the window token inside the host and omit private window lists.",
    ),
    (
        "Establish the capsule identity and label game and camera feeds only "
        "where those feeds are real.",
        "The capsule identifier and generation are known, with real feeds "
        "labelled game and camera. Live verification is pending.",
        "The operator sees the capsule tile, its identifier and generation, "
        "and the corresponding game or camera feed labels.",
    ),
    (
        "When the native recording path is available, start recording, stop "
        "recording, and verify the file in the operator-chosen folder. An AI "
        "request is only a proposal requiring Confirm inside Café.",
        "An operator-confirmed recording produces a verifiable file; this "
        "guide supplies no recorder connection or completed effect.",
        "The operator sees the file entry after stopping and opens it to "
        "verify the intended content. File and native recording acceptance "
        "remain pending; a queued proposal is not a completed recording.",
    ),
    (
        "When native OBS integration is available, send the same capsule "
        "window to OBS twice and inspect the source list. An AI request "
        "requires Confirm inside Café before any effect.",
        "A single Window Capture target is reused with no duplicate source; "
        "live verification is pending.",
        "The operator sees one intended Window Capture source before and "
        "after the second send and verifies its pixels in OBS. Configuration "
        "readback or a fixture alone does not establish native acceptance.",
    ),
    (
        "During a separate live check, receive a Twitch message and inspect "
        "its stored JSONL row using the existing chat event validator.",
        "A typed chat row is validated against cafe.chat_event.v1; live "
        "arrival and durable storage remain pending.",
        "The operator sees the corresponding JSONL line and its schema "
        "validation result, preserving canonical clock strings and unknown "
        "null fields. This guide never reads or aggregates JSONL.",
    ),
    (
        "Initialize the existing MCP session, send notifications/initialized, "
        "list tools with tools/list, and perform typed read calls using the "
        "six existing chat tools. Native control remains disabled without "
        "a native provider.",
        "The assistant receives tools/list and typed read results within "
        "existing bounds. Live data access remains pending; this static "
        "guide is not registered in the unchanged server.",
        "The operator sees the tool list and bounded typed read result with "
        "any scan limitations or closed error shown honestly. Tests cannot "
        "establish live capture, recording, OBS, broadcast, or MCP control.",
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
    """Describe only the proposed read-only demo_guide tool, without registering it."""
    return [{
        "name": "demo_guide",
        "description": "Static seven-step demo checklist; all live evidence is pending.",
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
    """Describe the proposed static resource; this does not add a live endpoint."""
    return [{
        "uri": "cafe://demo/guide",
        "name": "demo_guide",
        "description": "Static demo guide with human confirmation and pending live checks.",
        "mimeType": "application/json",
    }]


def read_resource(uri: str) -> dict:
    """Return the full guide only for the exact URI, otherwise empty unknown_resource."""
    if type(uri) is not str or uri != "cafe://demo/guide":
        return _reply((), "unknown_resource")
    return demo_guide({})
