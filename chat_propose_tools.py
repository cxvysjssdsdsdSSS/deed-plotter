"""Cursor SDK custom tool for deed chat action proposals.

Structured tool calls are more reliable than hoping the model pastes JSON in
prose. The tool only *queues* actions for the app's review dialog — it does
not mutate the workspace.
"""

from __future__ import annotations

from typing import Any

from cursor_sdk import CustomTool, CustomToolContext

from chat_propose import split_chat_proposal

PROPOSE_TOOL_NAME = "propose_deed_actions"

PROPOSE_TOOL_DESCRIPTION = """\
Submit proposed deed workspace changes for the user to review in the app.

Call this when the user asks to change calls, ties, document info, re-parse,
or export — including informal phrasing like "make the longer sides curves".
Do NOT call for advice-only / how-to questions.

call_index must be a 1-based row from the CURRENT Call Table before this batch
(updates/deletes run before adds — never use post-insert indices). after_index
for add_call: 0=before first, N=after call N, omit/-1=append. Several adds with
the same after_index keep proposal order. Prefer add_call to insert new courses.

The app shows a review table; nothing is applied until the user clicks Apply.
Pass the same action objects documented in the chat proposal schema.
For curve add/update: do not copy distance into chord_length. Radius plus
distance is enough; omit chord_length unless a chord is already stated.
"""

PROPOSE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "description": "Proposed workspace actions (update_call, add_call, …).",
            "items": {"type": "object"},
        },
        "skipped": {
            "type": "array",
            "description": "Items you could not propose, with reasons (strings or objects).",
            "items": {},
        },
    },
    "required": ["actions"],
}

CHAT_PROPOSAL_RETRY_NUDGE = (
    "You did not submit reviewable deed actions. Call the "
    f"{PROPOSE_TOOL_NAME} tool now with an actions array (and optional skipped), "
    "OR append the fenced JSON proposal block from the instructions. "
    "Do not only describe the change in prose."
)


class ProposalCollector:
    """Thread-safe-enough bag filled when the model invokes propose_deed_actions."""

    def __init__(self) -> None:
        self.called = False
        self.actions: list[dict] = []
        self.skipped: list[Any] = []

    def record(self, actions: Any, skipped: Any = None) -> dict[str, Any]:
        self.called = True
        cleaned: list[dict] = []
        if isinstance(actions, list):
            cleaned = [a for a in actions if isinstance(a, dict)]
        self.actions = cleaned
        if isinstance(skipped, list):
            self.skipped = list(skipped)
        else:
            self.skipped = []
        return {
            "ok": True,
            "accepted": len(cleaned),
            "message": (
                "Queued for user review in Deed Plotter. "
                "Do not claim the changes were applied."
            ),
        }

    def as_proposal(self) -> dict | None:
        if not self.called:
            return None
        return {"actions": list(self.actions), "skipped": list(self.skipped)}


def make_propose_tool(collector: ProposalCollector) -> CustomTool:
    def execute(args: dict[str, Any], _ctx: CustomToolContext) -> Any:
        return collector.record(args.get("actions"), args.get("skipped"))

    return CustomTool(
        execute=execute,
        description=PROPOSE_TOOL_DESCRIPTION,
        input_schema=PROPOSE_TOOL_SCHEMA,
    )


def resolve_chat_proposal(
    text: str,
    collector: ProposalCollector | None = None,
) -> tuple[str, dict | None]:
    """Return (display_prose, proposal_dict_or_None).

    Prefers a non-empty tool-collected proposal. If the tool was called with an
    empty actions list, fall back to fenced/bare JSON in the assistant text so
    an accidental empty tool call cannot hide a valid legacy proposal.
    """
    prose, fenced = split_chat_proposal(text or "")
    display = prose or (text or "").strip()
    tool_prop = collector.as_proposal() if collector is not None else None
    fenced_actions = (
        fenced.get("actions")
        if isinstance(fenced, dict) and isinstance(fenced.get("actions"), list)
        else None
    )
    if tool_prop is not None:
        tool_actions = tool_prop.get("actions") if isinstance(tool_prop.get("actions"), list) else []
        if tool_actions:
            return (
                display or "(Proposed actions — see review.)",
                tool_prop,
            )
        if fenced_actions:
            return prose or display, fenced
        # Tool called with nothing useful — still return it so skipped-only
        # proposals can surface in the UI.
        return display or "(Proposed actions — see review.)", tool_prop
    return prose, fenced


def chat_has_reviewable_proposal(
    text: str,
    collector: ProposalCollector | None = None,
) -> bool:
    """True when tool or fenced JSON produced a non-empty actions list."""
    _prose, prop = resolve_chat_proposal(text, collector)
    if not prop:
        return False
    actions = prop.get("actions")
    return isinstance(actions, list) and len(actions) > 0
