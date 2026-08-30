"""
Skylark Drones — monday.com Business Intelligence Agent
A conversational agent that answers founder-level BI questions by querying
monday.com boards (Deal Funnel + Work Order Tracker) LIVE, over MCP.

LLM backend: Google Gemini API (genuinely free tier, no credit card).
monday.com integration: direct MCP client (the official `mcp` Python SDK)
connected to monday.com's own hosted MCP server at https://mcp.monday.com/mcp,
passed to Gemini as a tool so Gemini can call monday.com's tools itself.

IMPORTANT session-lifetime rule (this is what fixes the "client has been
closed" bug): every single call that needs the monday.com MCP connection
opens a brand-new session and closes it when done. No MCP session, MCP
ClientSession, or its underlying HTTP client is EVER cached in
st.session_state or reused across calls. Only plain values (strings, ints,
dicts) derived FROM a session are cached. Keep this rule if you extend the
file — reusing a closed async context is exactly what causes that error.

IMPORTANT function-calling rule (this is what fixes the
"TypeError: cannot pickle '_asyncio.Future' object" bug): we do NOT use
google-genai's built-in `automatic_function_calling` with a raw MCP
ClientSession passed as a tool. That combination is still experimental in
the SDK and its internal loop deep-copies the GenerateContentConfig on each
turn — and a live ClientSession carries anyio/asyncio internals (cancel
scopes, Futures) that cannot be deep-copied, which is exactly the pickle
error. Instead we drive the tool-calling loop ourselves: list the MCP
tools, hand Gemini plain function declarations, and when it asks for a
function call we call `session.call_tool(...)` directly and feed the
result back. Automatic function calling stays disabled everywhere.

Run locally:   streamlit run app.py
Deploy:        Streamlit Community Cloud (see README.md)
"""

import os
import re
import time
import json
import asyncio
import traceback
import streamlit as st
from google import genai
from google.genai import types
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

APP_TITLE = "Skylark Drones — BI Agent"
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
MONDAY_MCP_URL = "https://mcp.monday.com/mcp"
MAX_TOOL_CALLS_PER_TURN = 10

SAMPLE_QUESTIONS = [
    "How's our pipeline looking for the mining sector?",
    "What's our revenue and collections this quarter?",
    "Which projects are stuck or blocked right now?",
]


def get_secret(name: str):
    """Read from Streamlit secrets first, then environment variables."""
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name)


GEMINI_API_KEY = get_secret("GEMINI_API_KEY")
MONDAY_API_TOKEN = get_secret("MONDAY_API_TOKEN")
DEAL_BOARD_NAME = get_secret("DEAL_BOARD_NAME") or "Deal Funnel"
WO_BOARD_NAME = get_secret("WO_BOARD_NAME") or "Work Order Tracker"

# --------------------------------------------------------------------------
# System prompt — this is where "data resilience" and "query understanding"
# mostly live. It gives the model a data dictionary + the known messiness of
# these two specific boards, so it interprets founder questions and
# incomplete/inconsistent data sensibly instead of guessing blindly.
# --------------------------------------------------------------------------

SYSTEM_PROMPT = f"""You are Skylark Drones' internal Business Intelligence agent. Founders and
executives ask you plain-language questions about the sales pipeline and project
execution, and you answer by querying monday.com LIVE via the connected tools.
Never fabricate numbers — every figure you state must come from a tool call you
actually made in this conversation.

## Your data sources (two monday.com boards)

1. **{DEAL_BOARD_NAME}** — the sales pipeline / deals board. One row per deal.
   Key columns and what they mean:
   - Deal Name: an internal codename for the deal (NOT the real client name — these
     are intentionally masked, e.g. "Naruto", "Sasuke"). Use it to cross-reference
     the Work Order board (its "Deal name masked" column uses the same codenames).
   - Owner code: anonymized sales rep identifier (e.g. OWNER_003).
   - Client Code: anonymized client identifier (e.g. COMPANY089).
   - Deal Status: Open / Won / Dead / On Hold. This is the fast way to answer
     "how many deals did we win/lose."
   - Deal Stage: a lettered pipeline funnel, roughly in order: A. Lead Generated →
     B. Sales Qualified Leads → C. Demo Done → D. Feasibility → E. Proposal/
     Commercials Sent → F. Negotiations → G. Project Won → H. Work Order Received →
     I. POC → J. Invoice sent → K. Amount Accrued → L. Project Lost →
     M. Projects On Hold → N./O. Not relevant. Treat G/H/J/K as "won-side" stages,
     L as lost, M as on hold, N/O as disqualified/irrelevant — don't count N/O
     against pipeline health.
   - Closure Probability: High / Medium / Low. Frequently blank — do not assume
     blank means "Low"; say the probability is unrecorded instead.
   - Masked Deal value: deal size in INR, anonymized/scaled but still directionally
     useful for relative comparisons. Often blank, especially for dead/lost deals —
     do not silently treat blank as zero when asked about "value of the pipeline";
     flag how many records lack a value.
   - Tentative Close Date / Close Date (A) / Created Date: dates. Close Date (A) is
     mostly blank (it's only filled once a deal actually closes) — don't treat that
     as a data error, it's expected for open deals.
   - Sector/service: the vertical, e.g. Mining, Renewables, Railways, Powerline,
     Construction, DSP, Tender, Manufacturing, Aviation, Security and Surveillance,
     Others. A small fraction of rows have this blank — report them as "sector
     unspecified" rather than dropping them silently from totals.
   - Product deal: what was sold (Pure Service, Spectra, Dock, DMO, Hardware,
     combinations) — very frequently blank, especially for early-stage deals.

2. **{WO_BOARD_NAME}** — project execution & billing board. One row per work order.
   Key columns:
   - Deal name masked: same codename system as the Deal board's "Deal Name" — this
     is your join key across the two boards. Note: several deal codenames repeat
     across multiple work orders (a single deal can spawn multiple work orders,
     e.g. recurring monthly contracts), so joins are one-to-many, not one-to-one.
   - Customer Name Code / BD/KAM Personnel code: anonymized identifiers.
   - Nature of Work: One time Project / Monthly Contract / Proof of Concept / etc.
   - Execution Status: Completed / Not Started / Executed until current month / etc.
   - Sector, Type of Work: operational categorization — Sector here should roughly
     match Sector/service on the Deal board for the same deal name, but don't
     assume perfect consistency; if they conflict, say so rather than picking one.
   - Amount in Rupees (Excl/Incl of GST) (Masked): the contracted/order value.
   - Billed Value / Collected Amount / Amount to be billed / Amount Receivable
     (all "(Masked)" in Rupees): use these together to reason about billing and
     collections health — e.g. Amount Receivable > 0 with an old Last invoice date
     may indicate a stuck receivable, worth flagging as a caveat, not a hard claim.
   - Quantities as per PO / Quantity billed (till date) / Balance in quantity:
     execution progress in units (varies by project — HA, count, etc; the unit
     isn't always explicit, so don't invent a unit if the source field doesn't
     have one).
   - Invoice Status / Billing Status / Collection status / WO Status (billed):
     several of these are heavily blank in this dataset — when most of a column
     is blank, say so plainly rather than reporting a count as if it were complete
     ("18 of 176 work orders have a recorded Billing Status; of those, ...").

## How to work

1. On your first tool call in a conversation, find the two boards by name
   ("{DEAL_BOARD_NAME}" and "{WO_BOARD_NAME}") and note their board IDs — don't
   assume fixed IDs, they can change if boards are re-imported.
2. Query only what you need for the question — use monday.com's filtering/query
   tools rather than pulling every column of every item when a narrower query
   will do, especially on the 38-column Work Order board.
3. When a founder's question is ambiguous (e.g. "this quarter" — which quarter,
   based on which date field? "pipeline" — open deals only, or including on-hold?
   "revenue" — booked/contracted value, billed value, or collected value?), ask a
   short clarifying question before running a big query, UNLESS a reasonable
   default is obvious, in which case state the assumption you're making inline
   and proceed (e.g. "I'm using Tentative Close Date and treating 'this quarter'
   as the current calendar quarter — let me know if you meant something else").
4. Always ground numeric answers in what you actually retrieved. If a metric is
   undermined by missing data (e.g. 70% of deal values are blank), lead with the
   number but explicitly caveat the coverage gap — founders need to know how much
   to trust a figure.
5. Cross-board questions (e.g. "which won deals haven't generated a work order
   yet?", "what's the collection rate for the mining sector?") require querying
   both boards and joining on the deal codename — do this rather than answering
   from one board alone.
6. Prefer clear, founder-readable prose and short tables over dumping raw rows.
   Lead with the headline number/insight, then supporting detail, then caveats.
7. If asked to "prepare a leadership update" or similar, produce a concise
   executive-brief in Markdown: 3-5 headline metrics, notable movers (biggest
   wins/losses/stuck items), sector breakdown, and a short "data quality notes"
   section listing anything materially incomplete that leadership should know
   about. Do not pad it with filler.
8. Never expose the raw anonymized codes as if they were real client names, and
   never invent a real company name for a masked one.
9. You only have read access to monday.com — never claim to have changed,
   created, or deleted anything there.
"""

CONNECTION_TEST_PROMPT = f"""Using the connected monday.com tools, do exactly this:
1. Find the current authenticated user's name.
2. Find the board ID of the board named exactly "{DEAL_BOARD_NAME}".
3. Find the board ID of the board named exactly "{WO_BOARD_NAME}".
If a board isn't found, use null for its id. Respond with ONLY compact JSON,
no markdown fences, no extra text, in exactly this shape:
{{"user": "<name or null>", "deal_board_id": <int or null>, "wo_board_id": <int or null>}}"""

# --------------------------------------------------------------------------
# Gemini + MCP plumbing
#
# Rule 1: every function here opens its OWN fresh MCP session and closes it
# before returning. Nothing MCP-related is ever stored in st.session_state.
#
# Rule 2: we NEVER pass a raw MCP ClientSession into
# `automatic_function_calling`. We list its tools ourselves, hand Gemini
# plain function declarations, and execute any function calls it requests
# by hand. See the big comment at the top of the file for why.
# --------------------------------------------------------------------------

def get_client() -> genai.Client:
    return genai.Client(api_key=GEMINI_API_KEY)


def _extract_retry_delay_seconds(error_text: str, default: float = 5.0) -> float:
    """Parse Gemini's 'retryDelay': '1s' hint out of a 429 error string, if
    present, else fall back to a small default backoff."""
    match = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", error_text)
    if match:
        return max(float(match.group(1)), 1.0)
    return default


async def _generate_with_retry(client, **kwargs):
    """Call generate_content, retrying once after a short backoff if we hit
    a 429 RESOURCE_EXHAUSTED — free-tier per-minute quotas can be brief
    bursts, and one retry avoids failing the whole turn over a transient
    spike instead of always making the user manually try again."""
    try:
        return await client.aio.models.generate_content(**kwargs)
    except Exception as e:
        text = format_error(e)
        if "RESOURCE_EXHAUSTED" not in text and "429" not in text:
            raise
        delay = _extract_retry_delay_seconds(text)
        await asyncio.sleep(delay)
        return await client.aio.models.generate_content(**kwargs)


def format_error(e: BaseException) -> str:
    """Unwrap ExceptionGroup / anyio TaskGroup errors to show the real cause
    instead of the generic 'unhandled errors in a TaskGroup' wrapper."""
    excs = getattr(e, "exceptions", None)
    if excs:
        return " | ".join(format_error(sub) for sub in excs)
    return f"{type(e).__name__}: {e}"


async def _with_fresh_mcp_session(coro_fn):
    """
    Opens a brand-new monday.com MCP connection, runs coro_fn(session) with
    it, and guarantees the connection is closed afterwards — never reused.
    """
    async with streamablehttp_client(
        MONDAY_MCP_URL,
        headers={"Authorization": f"Bearer {MONDAY_API_TOKEN}"},
    ) as (read_stream, write_stream, _get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            return await coro_fn(session)


_ALLOWED_SCHEMA_KEYS = {
    "type", "format", "description", "nullable", "enum",
    "items", "properties", "required", "title",
}


def _clean_json_schema(schema):
    """monday.com's MCP tool schemas are plain JSON-Schema and can contain
    things Gemini's FunctionDeclaration.parameters doesn't understand:
    extra keywords (e.g. $schema, additionalProperties, anyOf), and nullable
    fields expressed as "type": ["string", "null"] instead of a separate
    nullable flag. Normalize to a safe subset, recursively."""
    if not isinstance(schema, dict):
        return schema
    cleaned = {}
    is_nullable = False
    for key, value in schema.items():
        if key not in _ALLOWED_SCHEMA_KEYS:
            continue
        if key == "type" and isinstance(value, list):
            # e.g. ["string", "null"] -> type "string", nullable True.
            non_null = [t for t in value if t != "null"]
            is_nullable = len(non_null) < len(value)
            cleaned[key] = non_null[0] if non_null else "string"
        elif key == "properties" and isinstance(value, dict):
            cleaned[key] = {k: _clean_json_schema(v) for k, v in value.items()}
        elif key == "items":
            cleaned[key] = _clean_json_schema(value)
        else:
            cleaned[key] = value
    if is_nullable:
        cleaned["nullable"] = True
    return cleaned


_WRITE_VERBS = (
    "create", "update", "delete", "remove", "archive", "duplicate",
    "move", "change", "add", "set", "write", "insert", "clear",
    "upload", "invite", "assign",
)


async def _mcp_tools_to_gemini_tool(session):
    """List tools on the monday.com MCP server and convert them into a
    single Gemini `Tool` made of plain function declarations. Only
    read-style tools are exposed to Gemini: this agent is read-only by
    design (see SYSTEM_PROMPT), so there's no reason to even declare
    write-capable tools to the model — it shrinks the payload resent on
    every loop turn (helping with the free-tier token/minute quota) and
    removes any chance of the model invoking a mutating tool."""
    result = await session.list_tools()
    declarations = []
    for tool in result.tools:
        name_lower = tool.name.lower()
        if any(verb in name_lower for verb in _WRITE_VERBS):
            continue
        declarations.append(
            types.FunctionDeclaration(
                name=tool.name,
                description=tool.description or "",
                parameters=_clean_json_schema(tool.inputSchema) if tool.inputSchema else None,
            )
        )
    return types.Tool(function_declarations=declarations)


_MAX_TOOL_RESULT_CHARS = 12000


def _mcp_result_to_text(result):
    """Flatten an MCP CallToolResult into plain text for Gemini to read.
    Capped in size — a broad monday.com query can return a lot of rows, and
    every tool result gets re-sent on each subsequent loop turn, so an
    uncapped result can blow through the free-tier tokens-per-minute quota
    within a single question."""
    parts_text = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts_text.append(text)
        else:
            try:
                parts_text.append(json.dumps(block.model_dump(), default=str))
            except Exception:
                parts_text.append(str(block))
    joined = "\n".join(parts_text) if parts_text else "(empty tool result)"
    if getattr(result, "isError", False):
        joined = f"ERROR: {joined}"
    if len(joined) > _MAX_TOOL_RESULT_CHARS:
        joined = (
            joined[:_MAX_TOOL_RESULT_CHARS]
            + f"\n...[truncated, {len(joined) - _MAX_TOOL_RESULT_CHARS} more characters omitted — "
              f"narrow the query with filters/specific columns if you need the rest]"
        )
    return joined


async def _run_agent_loop(session, contents, tool_log, system_instruction=None):
    """
    Manual function-calling loop, replacing google-genai's
    automatic_function_calling for MCP sessions:
      1. Ask Gemini, with the monday.com tools described as plain function
         declarations (built from session.list_tools()).
      2. If Gemini asks for a function call, execute it ourselves against
         the live session via session.call_tool(...) and feed the result
         back as a function_response.
      3. Repeat until Gemini returns a plain text answer, or we hit
         MAX_TOOL_CALLS_PER_TURN.
    `contents` is mutated in place so callers can inspect/reuse history.
    `tool_log` is a plain list of dicts appended to for UI transparency —
    safe to store/display since it holds no live session objects.
    """
    client = get_client()
    gemini_tool = await _mcp_tools_to_gemini_tool(session)

    base_config = types.GenerateContentConfig(
        tools=[gemini_tool],
        system_instruction=system_instruction,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    response = None
    for _ in range(MAX_TOOL_CALLS_PER_TURN):
        response = await _generate_with_retry(
            client,
            model=MODEL,
            contents=contents,
            config=base_config,
        )

        candidate = response.candidates[0] if response.candidates else None
        parts = candidate.content.parts if (candidate and candidate.content) else []
        function_calls = [p.function_call for p in parts if getattr(p, "function_call", None) is not None]

        if not function_calls:
            return response

        # Gemini's function-call turn goes into history before we respond to it.
        contents.append(candidate.content)

        response_parts = []
        for fc in function_calls:
            args = dict(fc.args or {})
            tool_log.append({"call": fc.name, "args": args})
            try:
                result = await session.call_tool(fc.name, args)
                result_text = _mcp_result_to_text(result)
            except Exception as e:
                result_text = f"ERROR calling {fc.name}: {format_error(e)}"
            tool_log.append({"response": result_text})

            response_parts.append(
                types.Part.from_function_response(
                    name=fc.name,
                    response={"result": result_text},
                )
            )

        contents.append(types.Content(role="user", parts=response_parts))

    # Ran out of tool-call budget — ask once more, tools disabled, for a
    # final text answer instead of silently returning the last raw response.
    return await _generate_with_retry(
        client,
        model=MODEL,
        contents=contents,
        config=types.GenerateContentConfig(system_instruction=system_instruction),
    )


async def ask_agent(user_text: str, history: list):
    """Send one turn to Gemini using a fresh monday.com MCP session.
    Returns (response, tool_log) — tool_log is a plain list, safe to pass
    around / display without touching the (already-closed) MCP session."""

    contents = []
    for message in history:
        role = "model" if message["role"] == "assistant" else "user"
        contents.append(types.Content(role=role, parts=[types.Part(text=message["content"])]))
    contents.append(types.Content(role="user", parts=[types.Part(text=user_text)]))

    tool_log = []

    async def _run(session):
        response = await _run_agent_loop(session, contents, tool_log, system_instruction=SYSTEM_PROMPT)
        return response, tool_log

    return await _with_fresh_mcp_session(_run)


async def test_connection():
    """
    One-off health check using a fresh Gemini request
    and a fresh monday.com MCP session.
    """
    contents = [types.Content(role="user", parts=[types.Part(text=CONNECTION_TEST_PROMPT)])]
    tool_log = []

    async def _run(session):
        return await _run_agent_loop(session, contents, tool_log, system_instruction=SYSTEM_PROMPT)

    response = await _with_fresh_mcp_session(_run)

    text = (response.text or "").strip()

    # Remove markdown code fences if Gemini adds them.
    if text.startswith("```"):
        text = text.strip("`")

        if "\n" in text:
            text = text.split("\n", 1)[1]

    return json.loads(text)


def render_tool_history(tool_log, container):
    """Show the monday.com tool calls Gemini made this turn, for transparency.
    `tool_log` is the plain list built by _run_agent_loop — no live session
    objects, safe to render directly."""
    for entry in tool_log:
        if "call" in entry:
            with container.expander(f"🔧 monday.com tool call: `{entry['call']}`", expanded=False):
                st.json(entry.get("args", {}))
        elif "response" in entry:
            with container.expander("📋 monday.com response", expanded=False):
                st.code(entry["response"][:4000])


# --------------------------------------------------------------------------
# UI — dark / glass theme
# --------------------------------------------------------------------------

st.set_page_config(page_title=APP_TITLE, page_icon="🛸", layout="wide")

CUSTOM_CSS = """
<style>
:root {
    --accent: #6366F1;
    --accent-2: #22D3EE;
    --success: #10B981;
    --glass-bg: rgba(255,255,255,0.04);
    --glass-border: rgba(255,255,255,0.09);
}
.stApp {
    background:
        radial-gradient(ellipse 900px 500px at 15% -10%, rgba(99,102,241,0.20), transparent 60%),
        radial-gradient(ellipse 700px 500px at 100% 0%, rgba(34,211,238,0.12), transparent 55%),
        #0B0F19;
}
section[data-testid="stSidebar"] {
    background: #0E1220;
    border-right: 1px solid var(--glass-border);
}
.hero-title {
    font-size: 2.4rem;
    font-weight: 800;
    background: linear-gradient(90deg, #EEF2FF 0%, #A5B4FC 60%, #67E8F9 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin-bottom: 0.1rem;
}
.hero-sub {
    color: #9CA3AF;
    font-size: 1.02rem;
    margin-bottom: 1.6rem;
}
.glass-card {
    background: var(--glass-bg);
    border: 1px solid var(--glass-border);
    border-radius: 14px;
    padding: 0.9rem 1rem;
    margin-bottom: 0.7rem;
    backdrop-filter: blur(6px);
}
.badge-row { display:flex; align-items:center; justify-content:space-between; margin-bottom:0.35rem;}
.badge {
    display:inline-flex; align-items:center; gap:6px;
    font-size:0.78rem; font-weight:600; padding:2px 10px; border-radius:999px;
}
.badge-ok { background: rgba(16,185,129,0.15); color:#34D399; border:1px solid rgba(16,185,129,0.35);
    box-shadow: 0 0 10px rgba(16,185,129,0.25);}
.badge-off { background: rgba(239,68,68,0.12); color:#F87171; border:1px solid rgba(239,68,68,0.3);}
.dot { width:7px; height:7px; border-radius:50%; background:#34D399; box-shadow:0 0 6px #34D399; display:inline-block;}
.alert-banner {
    background: rgba(239,68,68,0.08);
    border: 1px solid rgba(239,68,68,0.35);
    border-left: 3px solid #F87171;
    border-radius: 10px;
    padding: 0.75rem 1rem;
    color: #FCA5A5;
    backdrop-filter: blur(6px);
    margin: 0.5rem 0 1rem 0;
}
.connected-banner {
    background: rgba(16,185,129,0.08);
    border: 1px solid rgba(16,185,129,0.35);
    border-left: 3px solid #34D399;
    border-radius: 10px;
    padding: 0.6rem 0.9rem;
    color: #6EE7B7;
    margin: 0.4rem 0 0.8rem 0;
}
div[data-testid="stChatMessage"] {
    border-radius: 14px;
    border: 1px solid var(--glass-border);
    background: var(--glass-bg);
    backdrop-filter: blur(6px);
}
.stButton>button {
    border-radius: 10px !important;
    border: 1px solid var(--glass-border) !important;
    background: rgba(255,255,255,0.03) !important;
    transition: all 0.15s ease-in-out;
}
.stButton>button:hover {
    border-color: var(--accent) !important;
    box-shadow: 0 0 12px rgba(99,102,241,0.35);
    color: #C7D2FE !important;
}
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

missing = [n for n, v in [("GEMINI_API_KEY", GEMINI_API_KEY), ("MONDAY_API_TOKEN", MONDAY_API_TOKEN)] if not v]

with st.sidebar:
    st.markdown("### ⚙️ Data Connections")
    st.markdown('<div class="glass-card">', unsafe_allow_html=True)
    st.markdown(
        f'<div class="badge-row"><span>monday.com token</span>'
        f'<span class="badge {"badge-ok" if MONDAY_API_TOKEN else "badge-off"}">'
        f'<span class="dot"></span>{"Connected" if MONDAY_API_TOKEN else "Missing"}</span></div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<div class="badge-row"><span>Gemini API key</span>'
        f'<span class="badge {"badge-ok" if GEMINI_API_KEY else "badge-off"}">'
        f'<span class="dot"></span>{"Connected" if GEMINI_API_KEY else "Missing"}</span></div>',
        unsafe_allow_html=True,
    )
    st.markdown("</div>", unsafe_allow_html=True)

    if missing:
        st.markdown(
            f'<div class="alert-banner">⚠️ Missing: {", ".join(missing)}. '
            f"Set these in <code>Settings → Secrets</code> (deployed) or "
            f"<code>.streamlit/secrets.toml</code> (local). See README.md.</div>",
            unsafe_allow_html=True,
        )
        st.stop()

    if st.button("🔌 Test monday.com connection", use_container_width=True):
        with st.spinner("Pinging monday.com..."):
            try:
                st.session_state["conn_test"] = asyncio.run(test_connection())
                st.session_state["conn_test_error"] = None
            except Exception as e:
                st.session_state["conn_test"] = None
                st.session_state["conn_test_error"] = format_error(e)
                print("=== TEST CONNECTION FULL TRACEBACK ===")
                traceback.print_exc()
                print("=== END TRACEBACK ===")

    conn_test = st.session_state.get("conn_test")
    conn_err = st.session_state.get("conn_test_error")
    if conn_test:
        st.markdown(
            f'<div class="connected-banner">✅ Connected as <b>{conn_test.get("user") or "unknown user"}</b></div>',
            unsafe_allow_html=True,
        )
        st.markdown(f"**Deals board ID:** `{conn_test.get('deal_board_id')}`")
        st.markdown(f"**Work Orders board ID:** `{conn_test.get('wo_board_id')}`")
    elif conn_err:
        st.markdown(f'<div class="alert-banner">⚠️ Connection test failed: {conn_err}</div>', unsafe_allow_html=True)

    if st.button("🗑️ Reset conversation", use_container_width=True):
        st.session_state.pop("messages", None)
        st.session_state.pop("pending", None)
        st.rerun()
# --------------------------------------------------------------------------
# Main panel
# --------------------------------------------------------------------------

st.markdown(f'<div class="hero-title">🛸 {APP_TITLE}</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="hero-sub">Ask founder-level questions about pipeline and delivery, sourced live from monday.com.</div>',
    unsafe_allow_html=True,
)

if "messages" not in st.session_state:
    st.session_state["messages"] = []

if not st.session_state["messages"]:
    st.markdown("**Sample questions:**")
    cols = st.columns(len(SAMPLE_QUESTIONS))
    for col, q in zip(cols, SAMPLE_QUESTIONS):
        with col:
            if st.button(q, use_container_width=True, key=f"chip_{q}"):
                st.session_state["messages"].append({"role": "user", "content": q})
                st.session_state["pending"] = True
                st.rerun()

for msg in st.session_state["messages"]:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

user_input = st.chat_input("Ask a business or pipeline question...")

if user_input:
    st.session_state["messages"].append({"role": "user", "content": user_input})
    st.session_state["pending"] = True

if st.session_state.get("pending"):
    st.session_state["pending"] = False
    with st.chat_message("user"):
        st.markdown(st.session_state["messages"][-1]["content"])

    with st.chat_message("assistant"):
        status_container = st.container()

    with st.spinner("Querying monday.com..."):
        try:
            history = st.session_state["messages"][:-1]

            response, tool_log = asyncio.run(
                ask_agent(
                    st.session_state["messages"][-1]["content"],
                    history,
                )
            )

            render_tool_history(tool_log, status_container)

            answer = response.text or (
                "(No response text — the model may have only made tool calls.)"
            )

        except Exception as e:
            err_text = format_error(e)
            st.markdown(
                f'<div class="alert-banner">⚠️ Connection Alert: '
                f'{err_text}</div>',
                unsafe_allow_html=True,
            )

            if "RESOURCE_EXHAUSTED" in err_text or "429" in err_text:
                answer = (
                    "Hit Gemini's free-tier rate limit (tokens-per-minute quota). "
                    "This usually clears within a minute — wait briefly and ask again. "
                    "If it keeps happening, try narrower questions (specific sectors/date "
                    "ranges rather than 'everything'), or check your quota/billing plan at "
                    "https://ai.dev/rate-limits."
                )
            else:
                answer = (
                    "Something went wrong talking to monday.com or Gemini. "
                    "Please check your API credentials and board access."
                )

    status_container.markdown(answer)

    st.session_state["messages"].append(
        {"role": "assistant", "content": answer}
    )