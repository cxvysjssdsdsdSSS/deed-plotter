"""AI parsing client.

Runs a Cursor agent (via the Cursor SDK, authenticated with a Cursor API
key) against the deed page images and returns structured boundary calls.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path

from cursor_sdk import CursorAgentError, CustomTool, LocalAgentOptions
from cursor_sdk.asyncio import AsyncClient

from cogo import (
    Call,
    curve_can_derive_chord,
    newly_derivable_copied_chord,
    normalize_call_type,
)
from cursor_bridge_patch import ensure_cursor_bridge_windows_patch
from chat_propose import chat_propose_prompt_section, wants_deed_proposal
from chat_propose_tools import (
    CHAT_PROPOSAL_RETRY_NUDGE,
    PROPOSE_TOOL_NAME,
    ProposalCollector,
    chat_has_reviewable_proposal,
    make_propose_tool,
    resolve_chat_proposal,
)
from agent_retry import (
    DEFAULT_MAX_ATTEMPTS,
    call_with_retry,
    is_retryable_run_status,
)

DEFAULT_MODEL = "composer-2.5"
CHAT_HISTORY_LIMIT = 12

CHAT_SYSTEM = """\
You are the Deed Plotter assistant — a land-surveyor-aware helper for a desktop
app that extracts and plots metes-and-bounds deeds.

You answer questions about:
- The current deed workspace (calls, ties, closure, POB, document info, notes)
- How to operate Deed Plotter (Open, Pages, Parse, edit, export, jobs)

Rules:
- Be concise and practical. Prefer numbers and call indices from the workspace.
- Do not invent bearings, distances, or calls that are not in the workspace.
- For curve edits, Radius plus distance is enough. Leave chord_length empty
  unless a chord is already stated; do not copy distance into chord_length.
- Do not claim you already changed the table or exported a file unless the user
  applied a proposal in the app.
- For how-to questions, use the app help in the context.
"""

INSTRUCTIONS = """You are an expert land surveyor's assistant. Read the deed /
legal description provided and extract the metes-and-bounds boundary calls in order.

Respond with ONLY a JSON object in this exact shape (no markdown fences, no commentary):
{
  "legal_description": "<the complete legal description transcribed from the document, cleaned up and properly formatted: fix OCR/scan artifacts, normalize bearings to the form N 45°30'15\\" E, keep the original wording otherwise, and break it into readable paragraphs with each THENCE call starting on its own line>",
  "document_info": {
    "document_type": "<e.g. Warranty Deed, Special Warranty Deed, Easement, Plat, County Line Survey, Control Line Survey, Road Survey, Survey Report>",
    "county": "", "state": "",
    "date": "<document/survey date as written>",
    "grantor": "", "grantee": "",
    "surveyor": "<surveyor name if shown>",
    "surveyor_license": "<RPLS/PLS number if shown>",
    "volume_page": "<recording volume/page or document number if shown>",
    "acreage_stated": "<acreage as stated in the deed, e.g. '16.715 acres'>",
    "basis_of_bearings": "<the survey's angular reference ONLY — e.g. 'Texas State Plane Coordinate System, Central Zone (4203), NAD83', 'magnetic bearings', or 'based on the north line of Lot 1'. Quote the deed verbatim. Empty if not stated. Do NOT put corner phrases like 'No bearings' / 'no bearing trees' here — those mean no witness trees at that corner, not a datum>"
  },
  "tie_calls": [
    // ONLY the commencement-to-POB run: courses from a point of commencement
    // to a DISTINCT, explicitly stated POB / "Beginning at…". Do NOT put
    // witness/reference monuments here ("iron rod bears N 33°06' W, 1.0 ft")
    // — those belong in that corner's "monument" field. Empty array when the
    // deed begins at the POB, or when Commencing at X is itself the place of
    // beginning of a closed figure (put every THENCE in "calls" instead).
  ],
  "point_of_beginning": "<text describing the POB, or empty string>",
  "pob_coordinates": {"northing": 0.0, "easting": 0.0},  // ONLY if the deed explicitly states grid/state-plane coordinates for the POB (e.g. "N=10,213,045.36, E=3,097,432.12"). Omit or null if not stated — never compute or invent coordinates.
  "pob_monument": "<the physical monument AT the point of beginning itself, e.g. '1/2\\" iron rod found', or empty if the POB is a calculated point located only by a tie from a commencing monument>",
  "general_notes": "<observations a surveyor must act on: ambiguous or conflicting calls, illegible text, missing closure language, OCR corrections you made. Do NOT warn about routine boilerplate (corners described relative to adjoiners, standard recital language). Do NOT compute or estimate misclosure yourself — the application checks closure mathematically>",
  "calls": [
    {
      "type": "line" | "curve",
      "bearing": "N 45°30'15\\" E",          // quadrant bearing; for curves put the chord bearing here too
      "distance": 123.45,                    // number only; for lines this is the line length; for curves this is the course magnitude as written (often the arc). Use null if unreadable.
      "units": "feet" | "chains" | "rods" | "varas" | "meters" | "links" | "poles",
      "curve_direction": "left" | "right",   // curves only
      "radius": 0.0,                         // curves only, same units; null if not given
      "arc_length": 0.0,                     // curves only, same units; null if not given
      "chord_bearing": "",                   // curves only
      "chord_length": 0.0,                   // curves only; null if not given
      "delta": "",                           // curves only, central angle e.g. "12°30'00\\""
      "monument": "<how the corner AT THE END of this call is monumented or witnessed. ALWAYS fill this when the deed gives ANY corner evidence: a monument at the corner ('1/2\\" iron rod found', 'concrete monument set', 'point in creek'), OR a witness/reference monument ('1/2\\" iron rod found bears N 33°06' W, 1.0 foot'), OR a descriptive corner ('southwest corner of Lot 1, Block A'). Empty ONLY if the deed says nothing at all about that corner>",
      "description": "<the call text, e.g. 'along the north line of Lot 4'>",
      "confidence": "high" | "medium" | "low"
    }
  ]
}

Rules:
- Preserve the exact order of calls as written in the description.
- Point of beginning (POB): the point where the BOUNDARY traverse both starts and
  returns ("Beginning at…", "place of beginning", "to the place of beginning").
  Do NOT treat an intermediate named corner along the first course (e.g. "to the
  N.E. corner of League 44") as the POB just because that course ends there.
- "calls" holds only boundary calls of the primary described tract, from the POB
  through closure. Exclude easements, less-and-except strips, reserved areas,
  and non-boundary appurtenances unless the deed explicitly makes them part of
  the parcel boundary.
- "tie_calls": ONLY a commencement-to-POB run — courses that walk from a point
  of commencement to a DISTINCT, explicitly stated POB / "Beginning at…", after
  which the deed then bounds the tract. Witness/reference monuments ("iron rod
  bears N 33°06' W, 1.0 ft") belong in that corner's "monument" field, NEVER in
  "tie_calls". Empty "tie_calls" if the deed begins at the POB.
- Closed-figure field notes (common in Texas league / abstract surveys): if the
  deed says Commencing at X, then THENCE … THENCE … back to the place of
  beginning, and X is that place of beginning (no separate "true point of
  beginning" / mid-description POB), put EVERY THENCE in "calls" and leave
  "tie_calls" empty. The first THENCE is boundary call 1, not a tie.
- For curves: set curve_direction to "left" or "right" and include the radius
  when the deed gives it. Radius plus "distance" is enough when the deed gives
  a curve magnitude ("a distance of", arc length in the course). Leave
  arc_length, delta, and chord_length null/empty unless the deed explicitly
  states those values. Do not copy distance into chord_length or arc_length.
  Include the chord bearing whenever the deed gives one. Copy numbers exactly
  as written; never derive missing curve values yourself.
- "distance" is the numeric magnitude only; put the unit in "units". Interpret
  archaic phrasing (chains, poles, varas, links) and set units accordingly —
  never pre-convert to feet. For compound distances like "10 chains 5 links",
  convert to a decimal of the primary unit (10.05 chains).
- Transcribe faithfully: if the deed misspells or uses archaic wording, keep the
  meaning; if a bearing/distance is illegible or ambiguous, still include the
  call with your best reading, mark confidence "low", use null for unreadable
  numbers (do not invent 0), and note the issue in description.
- Never invent a call to force closure.
- If the source is an open control, county, or road line (no return to a place of
  beginning), still put every course with a bearing and/or distance in "calls"
  in order. Do not invent a closing course — same rule as closed deeds.
- confidence: "high" = verbatim and unambiguous in the source; "medium" =
  normalized abbreviations or assumed units; "low" = ambiguous wording, poor
  scan quality, or a guessed reading.
- "basis_of_bearings" is the survey's angular reference (grid/datum, true or
  magnetic, or "based on … line"). Corner language like "No bearings",
  "whence no bearings", or "no bearing trees" means that corner has no
  witness/bearing trees — leave "basis_of_bearings" empty; do not invent a
  pob_monument from that phrase alone.
- Do not create, modify, or delete any files. Only read the inputs and answer.
"""

PAGE_SCOPE = """
Per-page scope (this request is ONE page of a multipage deed):
- Extract ONLY content that appears on THIS page image.
- Do NOT restate or re-emit calls, ties, or legal paragraphs that prior pages
  already covered (see prior-pages summary if provided).
- If the prior-pages summary shows the POB or boundary calls already exist,
  treat continuing THENCE courses on this page as boundary "calls" — NEVER as
  "tie_calls" — and leave "point_of_beginning" empty unless a POB statement is
  printed on this page.
- If a call or sentence begins on a prior page and continues here, include only
  the portion visible on this page (or the complete call if it finishes here).
  If this page only completes a curve begun on the prior page, emit ONE curve
  call with the values visible here and note the continuation in description.
- If this page contains only a LESS AND EXCEPT / save-and-except tract,
  easement, or reservation (not the primary boundary), return "calls": [] and
  transcribe the text into "legal_description" and note it in "general_notes".
- If this page has no boundary calls, return "calls": [] and still transcribe
  any legal description text visible on the page into "legal_description".
- Prefer empty arrays/fields over inventing content from memory of prior pages.
"""


class AIClientError(Exception):
    pass


# Near-exact junk the model sometimes puts in basis_of_bearings when the deed
# only said a corner has no witness/bearing trees. Whole-string match only —
# real statements like "Bearings based on …" or longer notes are kept.
_BASIS_JUNK_RE = re.compile(
    r"""^\s*
    (?:
        n/?a|none|unknown|not\s+stated|not\s+given|
        (?:whence\s+)?no\s+bearings?(?:\s+trees?)?(?:\s+found)?
    )
    \s*[-–—.]?\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def sanitize_basis_of_bearings(value: str) -> str:
    """Keep real basis statements; clear corner 'no bearings' false positives."""
    text = (value or "").strip()
    if not text:
        return ""
    if _BASIS_JUNK_RE.match(text):
        return ""
    return text


def _balanced_objects(text: str) -> list[str]:
    """All top-level balanced {...} spans in the text, in order."""
    spans: list[str] = []
    depth = 0
    in_str = False
    escape = False
    start = -1
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            if depth > 0:
                in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    spans.append(text[start:i + 1])
                    start = -1
    return spans


def _extract_json_object(text: str) -> str:
    """Pull the parse-result {...} object out of agent output.

    The agent sometimes writes prose (occasionally with a small example
    object) around the answer, so prefer the largest decodable object that
    has a "calls" key, then any decodable object, largest first.
    """
    if "{" not in text:
        raise AIClientError(f"Agent did not return JSON. Response began: {text[:300]}")
    decodable: list[str] = []
    for span in _balanced_objects(text):
        try:
            data = json.loads(span)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            decodable.append(span)
    if decodable:
        with_calls = [s for s in decodable if '"calls"' in s]
        pool = with_calls or decodable
        return max(pool, key=len)
    raise AIClientError(f"Agent JSON object was truncated. Response began: {text[:300]}")


def _coord_float(value) -> float:
    """Parse a POB northing/easting; strip thousands separators like call distances."""
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value).strip().replace(",", ""))


def _optional_float(
    value, *, field: str, index: int, warnings: list[str], kind: str = "Call",
) -> float:
    """Parse a numeric field; null/missing/blank → 0 with a warning (no inventing)."""
    if value is None or value == "":
        warnings.append(
            f"{kind} {index}: {field} missing/unreadable "
            "(left blank — fill in before trusting closure)."
        )
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        warnings.append(
            f"{kind} {index}: {field} missing/unreadable "
            "(left blank — fill in before trusting closure)."
        )
        return 0.0
    try:
        return float(text)
    except ValueError:
        warnings.append(f"{kind} {index}: {field} not numeric ({value!r}) — left blank.")
        return 0.0


def _curve_optional(
    item: dict, field: str, index: int, warnings: list[str], *, kind: str = "Call",
) -> float:
    if field not in item:
        return 0.0
    raw = item.get(field)
    if raw is None or raw == "":
        # Optional curve fields: warn only when explicitly null (AI couldn't read).
        warnings.append(f"{kind} {index}: {field} missing/unreadable.")
        return 0.0
    return _optional_float(raw, field=field, index=index, warnings=warnings, kind=kind)


def _item_has_curve_data(item: dict) -> bool:
    for key in ("radius", "arc_length", "chord_length", "delta", "curve_direction"):
        raw = item.get(key)
        if raw is not None and raw != "":
            return True
    return False


def call_from_ai_dict(
    item: dict,
    index: int,
    warnings: list[str],
    *,
    kind: str = "Call",
) -> Call:
    raw_type = item.get("type", "")
    stripped = str(raw_type or "").strip().lower()
    call_type = normalize_call_type(
        raw_type, has_curve_data=_item_has_curve_data(item),
    )
    if stripped and stripped not in ("line", "curve"):
        warnings.append(
            f"{kind} {index}: type {raw_type!r} treated as {call_type}."
        )
    elif not stripped and call_type == "curve":
        warnings.append(
            f"{kind} {index}: missing type treated as curve "
            "(radius/delta/arc/chord present)."
        )

    distance_raw = item.get("distance")
    distance_missing = distance_raw is None or distance_raw == ""
    # Lines need distance. Curves often omit it and use chord/arc/radius instead —
    # do not treat a null curve distance as low-confidence by itself.
    if distance_missing:
        if call_type == "line":
            warnings.append(
                f"{kind} {index}: distance missing/unreadable "
                "(left blank — fill in before trusting closure)."
            )
        distance = 0.0
    else:
        distance = _optional_float(
            distance_raw, field="distance", index=index, warnings=warnings, kind=kind,
        )

    confidence = str(item.get("confidence", "") or "").lower()
    if distance_missing and call_type == "line":
        confidence = "low"
    elif confidence not in ("high", "medium", "low", ""):
        confidence = "medium"

    bearing = str(item.get("bearing", "") or "")
    radius = _curve_optional(item, "radius", index, warnings, kind=kind) if call_type == "curve" else 0.0
    arc_length = _curve_optional(item, "arc_length", index, warnings, kind=kind) if call_type == "curve" else 0.0
    chord_length = _curve_optional(item, "chord_length", index, warnings, kind=kind) if call_type == "curve" else 0.0
    delta = str(item.get("delta", "") or "")
    if call_type == "curve" and newly_derivable_copied_chord(
        curve_can_derive_chord(radius, delta, arc_length),
        False,
        distance,
        chord_length,
    ):
        # Same Dist-then-Radius as Chat add_call: Dist==Chord + R/Δ is an
        # echoed live copy, not a stated chord.
        chord_length = 0.0

    return Call(
        call_type=call_type,
        bearing=bearing,
        distance=distance,
        units=str(item.get("units", "feet") or "feet"),
        curve_direction=str(item.get("curve_direction", "") or ""),
        radius=radius,
        arc_length=arc_length,
        chord_bearing=str(item.get("chord_bearing", "") or "") or (bearing if call_type == "curve" else ""),
        chord_length=chord_length,
        delta=delta,
        monument=str(item.get("monument", "") or ""),
        description=str(item.get("description", "") or ""),
        confidence=confidence,
    )


class DeedParserClient:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL):
        self.api_key = api_key.strip()
        self.model = model.strip() or DEFAULT_MODEL

    def parse_images(self, images_png: list[bytes], extra_text: str = "") -> dict:
        """Parse deed page images. Returns dict with 'calls' (list[Call]),
        'point_of_beginning', and 'general_notes'."""
        if not self.api_key:
            raise AIClientError("No API key configured. Open Settings and enter your Cursor API key.")
        with tempfile.TemporaryDirectory(prefix="deed_pages_") as tmp:
            tmp_path = Path(tmp)
            names = []
            for i, png in enumerate(images_png, start=1):
                name = f"page_{i:02d}.png"
                (tmp_path / name).write_bytes(png)
                names.append(name)
            prompt = (
                INSTRUCTIONS
                + f"\nThe deed consists of {len(names)} page image(s) in the current "
                + f"directory, in order: {', '.join(names)}. Read every page image "
                + "carefully before answering."
            )
            if extra_text:
                prompt += f"\n\nAdditional context from the user:\n{extra_text}"
            return self._run(prompt, cwd=str(tmp_path))

    def parse_page(
        self,
        page_png: bytes,
        *,
        page_number: int,
        total_pages: int,
        document_page: int | None = None,
        prior_context: str = "",
        extra_text: str = "",
    ) -> dict:
        """Parse a single deed page image (one API call / agent hang-up).

        Extract only what appears on this page. Prior pages are summarized in
        ``prior_context`` so the model continues the traverse without redoing them.

        ``page_number`` / ``total_pages`` are the position within this parse run
        (selected pages). ``document_page`` is the 1-based page in the source PDF
        when that differs (e.g. parsing only pages 3 and 5).
        """
        if not self.api_key:
            raise AIClientError("No API key configured. Open Settings and enter your Cursor API key.")
        if page_number < 1 or total_pages < 1 or page_number > total_pages:
            raise AIClientError(
                f"Invalid page_number={page_number} for total_pages={total_pages}."
            )
        doc_page = document_page if document_page is not None else page_number
        with tempfile.TemporaryDirectory(prefix="deed_page_") as tmp:
            tmp_path = Path(tmp)
            name = "page.png"
            (tmp_path / name).write_bytes(page_png)
            prompt = (
                INSTRUCTIONS
                + PAGE_SCOPE
                + f"\nThis is document page {doc_page} "
                + f"(image {page_number} of {total_pages} in this parse run). "
                + f"The only image in the current directory is {name}. "
                + "Read it carefully and extract ONLY what appears on this page."
            )
            if prior_context.strip():
                prompt += (
                    "\n\nPrior pages already parsed (do NOT repeat their calls/"
                    "legal text; continue from where they left off):\n"
                    + prior_context.strip()
                )
            if extra_text.strip():
                prompt += f"\n\nAdditional context from the user:\n{extra_text.strip()}"
            return self._parse_run(prompt, cwd=str(tmp_path), structure_qa=False, document_finalize=False)

    def parse_text(self, deed_text: str) -> dict:
        if not self.api_key:
            raise AIClientError("No API key configured. Open Settings and enter your Cursor API key.")
        # No page images — this text is the only source. It may be a full legal
        # description, OCR/hints notes, or both (paste box + Settings append).
        prompt = (
            INSTRUCTIONS
            + "\n\nThe following text was provided by the user in place of deed "
            + "page images. Treat it as the deed / legal description when it "
            + "contains metes-and-bounds; if it is mostly notes or hints, extract "
            + "what you can and record gaps in general_notes.\n\n"
            + deed_text
        )
        with tempfile.TemporaryDirectory(prefix="deed_text_") as tmp:
            return self._parse_run(prompt, cwd=tmp)

    def chat(
        self,
        deed_context: str,
        history: list[dict],
        user_message: str,
    ) -> dict:
        """Free-form chat about the current deed / app.

        Returns ``{"text": str, "proposal": dict|None}``. ``proposal`` comes from
        the propose_deed_actions tool when called, else from a fenced JSON block
        in the assistant text (legacy).
        """
        if not self.api_key:
            raise AIClientError("No API key configured. Open Settings and enter your Cursor API key.")
        convo = ""
        for turn in history[-CHAT_HISTORY_LIMIT:]:
            role = "User" if turn.get("role") == "user" else "Assistant"
            convo += f"\n{role}: {turn.get('content', '')}"
        prompt = (
            CHAT_SYSTEM
            + "\n\n=== CONTEXT ===\n" + deed_context
            + "\n\n" + chat_propose_prompt_section()
            + "\n\n=== CONVERSATION SO FAR ===" + (convo or "\n(none)")
            + f"\n\nUser: {user_message}\n\nAnswer as the assistant, directly."
        )
        with tempfile.TemporaryDirectory(prefix="deed_chat_") as tmp:
            text, collector = self._run_chat_agent(prompt, tmp, user_message)
        _prose, proposal = resolve_chat_proposal(text, collector)
        return {"text": text, "proposal": proposal}

    def _parse_run(
        self,
        prompt: str,
        cwd: str,
        *,
        structure_qa: bool = True,
        document_finalize: bool = True,
    ) -> dict:
        raw = self._run_agent(prompt, cwd)
        return self._parse_response(
            raw,
            structure_qa=structure_qa,
            document_finalize=document_finalize,
        )

    def _run(self, prompt: str, cwd: str) -> dict:
        # Back-compat alias used by parse_images / parse_text.
        return self._parse_run(prompt, cwd)

    def _run_chat_agent(
        self,
        prompt: str,
        cwd: str,
        user_message: str,
    ) -> tuple[str, ProposalCollector]:
        """Run chat with propose_deed_actions tool; one nudge retry if needed."""
        ensure_cursor_bridge_windows_patch()
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

        def _once() -> tuple[str, ProposalCollector]:
            try:
                return asyncio.run(
                    self._run_chat_async(prompt, cwd, user_message)
                )
            except CursorAgentError:
                raise

        try:
            return call_with_retry(_once, max_attempts=DEFAULT_MAX_ATTEMPTS)
        except CursorAgentError as exc:
            raise AIClientError(f"Agent failed to start: {exc}") from exc

    async def _run_chat_async(
        self,
        prompt: str,
        cwd: str,
        user_message: str,
    ) -> tuple[str, ProposalCollector]:
        collector = ProposalCollector()
        tools: dict[str, CustomTool] = {
            PROPOSE_TOOL_NAME: make_propose_tool(collector),
        }
        async with await AsyncClient.launch_bridge(workspace=cwd) as client:
            async with await client.create_agent(
                api_key=self.api_key,
                model=self.model,
                local=LocalAgentOptions(cwd=cwd, custom_tools=tools),
            ) as agent:
                run = await agent.send(prompt)
                result = await run.wait()
                text = self._chat_result_text(result)
                if wants_deed_proposal(user_message) and not chat_has_reviewable_proposal(
                    text, collector
                ):
                    run2 = await agent.send(CHAT_PROPOSAL_RETRY_NUDGE)
                    result2 = await run2.wait()
                    try:
                        text2 = self._chat_result_text(result2)
                    except AIClientError:
                        text2 = ""
                    if chat_has_reviewable_proposal(text2, collector):
                        # Keep first-turn prose when the nudge reply is empty but
                        # the tool queued actions.
                        if text2.strip():
                            text = text2
                    elif text2.strip() and not text.strip():
                        text = text2
                return text, collector

    @staticmethod
    def _chat_result_text(result: object) -> str:
        status = getattr(result, "status", "") or ""
        raw = getattr(result, "result", None) or ""
        if status == "finished" and str(raw).strip():
            return str(raw)
        detail = str(raw).strip() or status or "unknown"
        err = AIClientError(f"Agent run ended with status '{status}': {detail}")
        if (
            status == "finished" and not str(raw).strip()
        ) or is_retryable_run_status(status, detail):
            err.is_retryable = True  # type: ignore[attr-defined]
        raise err

    def _run_agent(self, prompt: str, cwd: str) -> str:
        ensure_cursor_bridge_windows_patch()
        # The SDK's sync bridge launcher is broken on Windows (it selects on
        # a pipe, which Windows only allows for sockets), so use the async
        # surface, which manages the bridge subprocess via asyncio.
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

        def _once() -> str:
            try:
                result = asyncio.run(self._run_async(prompt, cwd))
            except CursorAgentError:
                raise
            return DeedParserClient._chat_result_text(result)

        try:
            return call_with_retry(_once, max_attempts=DEFAULT_MAX_ATTEMPTS)
        except CursorAgentError as exc:
            raise AIClientError(f"Agent failed to start: {exc}") from exc

    async def _run_async(self, prompt: str, cwd: str):
        async with await AsyncClient.launch_bridge(workspace=cwd) as client:
            async with await client.create_agent(
                api_key=self.api_key,
                model=self.model,
                local=LocalAgentOptions(cwd=cwd),
            ) as agent:
                run = await agent.send(prompt)
                return await run.wait()

    @staticmethod
    def _parse_response(
        raw: str,
        *,
        structure_qa: bool = True,
        document_finalize: bool = True,
    ) -> dict:
        text = raw.strip()
        fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        if not text.startswith("{"):
            text = _extract_json_object(text)
        else:
            # Still brace-balance in case of trailing commentary.
            try:
                json.loads(text)
            except json.JSONDecodeError:
                text = _extract_json_object(text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AIClientError(f"Could not decode agent JSON: {exc}") from exc

        warnings: list[str] = []
        raw_calls = data.get("calls") or []
        raw_ties = data.get("tie_calls") or []
        calls = []
        for i, item in enumerate(raw_calls, start=1):
            if not isinstance(item, dict):
                warnings.append(f"Call {i}: skipped — not a JSON object.")
                continue
            calls.append(call_from_ai_dict(item, i, warnings))
        tie_calls = []
        for i, item in enumerate(raw_ties, start=1):
            if not isinstance(item, dict):
                warnings.append(f"Tie call {i}: skipped — not a JSON object.")
                continue
            tie_calls.append(call_from_ai_dict(item, i, warnings, kind="Tie call"))

        info = data.get("document_info") or {}
        pob_xy = data.get("pob_coordinates")
        pob_coordinates = None
        if isinstance(pob_xy, dict):
            try:
                n_raw, e_raw = pob_xy.get("northing"), pob_xy.get("easting")
                if n_raw is not None and e_raw is not None and n_raw != "" and e_raw != "":
                    n, e = _coord_float(n_raw), _coord_float(e_raw)
                    # Reject only the schema's (0, 0) placeholder — a single
                    # zero axis is a legitimate grid coordinate.
                    if n != 0.0 or e != 0.0:
                        pob_coordinates = {"northing": n, "easting": e}
            except (TypeError, ValueError):
                warnings.append("POB coordinates present but not numeric — ignored.")

        document_info: dict[str, str] = {}
        if isinstance(info, dict):
            for k, v in info.items():
                if not v:
                    continue
                text_v = str(v)
                if k == "basis_of_bearings":
                    text_v = sanitize_basis_of_bearings(text_v)
                    if not text_v:
                        continue
                document_info[k] = text_v

        from parse_structure_qa import (
            apply_stub_and_open_line_finalize,
            warn_possible_misfiled_boundary_tie,
        )

        notes = data.get("general_notes", "") or ""
        calls, tie_calls, notes, warnings = apply_stub_and_open_line_finalize(
            calls=calls,
            tie_calls=tie_calls,
            document_info=document_info,
            general_notes=notes,
            warnings=warnings,
            document_finalize=document_finalize,
        )

        if structure_qa:
            warnings.extend(
                warn_possible_misfiled_boundary_tie(
                    data.get("legal_description", "") or "",
                    calls,
                    tie_calls,
                    document_info=document_info,
                )
            )

        return {
            "calls": calls,
            "tie_calls": tie_calls,
            "document_info": document_info,
            "legal_description": data.get("legal_description", "") or "",
            "point_of_beginning": data.get("point_of_beginning", "") or "",
            "pob_coordinates": pob_coordinates,
            "pob_monument": data.get("pob_monument", "") or "",
            "general_notes": notes,
            "parse_warnings": warnings,
        }
