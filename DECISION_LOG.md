# Decision Log — Skylark Drones BI Agent

## Key assumptions

- **Board naming.** The agent finds boards by name ("Deal Funnel", "Work
  Order Tracker") rather than hardcoded board IDs, since IDs are only known
  after import. If boards are renamed, the two `*_BOARD_NAME` config values
  need to match.
- **Join key.** The two boards share no formal relation in monday.com; they
  join informally on the masked deal codename (`Deal Name` on the Deal
  board, `Deal name masked` on the Work Order board). I assumed this is a
  reliable, if informal, join key based on inspecting the sample data — and
  that it's one-to-many (one deal can have several work orders, e.g.
  recurring monthly contracts), not one-to-one.
- **Structural artifacts vs. real messiness.** Two things in the raw files
  looked like spreadsheet export artifacts rather than intentional "messy
  real-world data": (1) two stray rows in the Deal Funnel sheet where every
  column except `Deal Name` literally contained the column header text
  again (an apparent copy/paste or frozen-header artifact) — I removed
  these two rows; (2) the Work Order sheet's real header was on row 2, not
  row 1 (row 1 was blank) — I re-read with the correct header row. Every
  other form of messiness (nulls, inconsistent casing, sparse columns,
  masked values) was left as-is and handled by the agent at query time
  rather than cleaned up front, since the assignment explicitly wants the
  agent to demonstrate handling messy data, not to receive pre-cleaned data.
- **"Revenue"/"pipeline value" is ambiguous by design.** Founders will ask
  loosely ("how's revenue?") without specifying booked vs. billed vs.
  collected. Rather than picking one silently, the agent is instructed to
  state which figure it's using and offer to compute the others, or ask
  when the ambiguity is consequential.
- **Read-only scope.** The assignment specifies "Monday.com — Read only," so
  the agent's system prompt explicitly forbids claiming any write action,
  and in practice it's never asked to do anything that would require one.
- **Zero-budget constraint.** I assumed the prototype should run entirely on
  free tiers — no credit card, no paid API usage — since that was a hard
  constraint for this build, not just a preference. This shaped the LLM
  choice below.

## Trade-offs chosen and why

| Decision | Chosen | Alternative considered | Why |
|---|---|---|---|
| LLM backend | **Google Gemini API** (Gemini 2.5 Flash via Google AI Studio) | Anthropic Claude API / OpenAI API | Claude and OpenAI have no standing free API tier as of this build — both require adding billing. Gemini via Google AI Studio is the one major-lab API with a genuine, permanent, no-card-required free tier, which was a hard requirement here. Trade-off: Gemini's tool-use quality and its MCP support are both slightly less battle-tested than Claude's equivalent for this specific job, and the free tier has tighter rate limits — acceptable for a demo/prototype. |
| monday.com integration | A **direct MCP client** (the official `mcp` Python SDK) connecting to monday.com's own **hosted MCP server**, with the live session handed to Gemini as a tool | Anthropic-style built-in "MCP connector" API parameter / hand-written GraphQL wrapper | Gemini's SDK supports passing an MCP `ClientSession` object straight into `tools=[...]` with automatic function calling, which is the free-tier-compatible equivalent of a provider-native MCP connector. This still means a *real* MCP connection is doing the integration work (satisfying "connect via MCP or API" literally) without writing custom GraphQL query/mutation code by hand. Cost: this is documented as an *experimental* feature of `google-genai`, and the `mcp` package's API has changed across recent versions, so the exact client code may need small updates if either SDK changes — I pinned both dependencies in `requirements.txt` to versions I verified work together. |
| Data resilience strategy | Push data-quality knowledge into the **system prompt** (data dictionary + explicit "flag, don't hide, missing data" instructions), and let the model reason over live, still-messy data | Pre-clean/normalize the data heavily before import, or add a code layer that normalizes at query time | A prompt-level data dictionary is faster to build and, more importantly, is what actually generalizes to *new* messiness the boards will accumulate later — a hardcoded cleaning layer would only work for today's snapshot. Trade-off: answer quality depends more on the model correctly following the caveat instructions than on a strictly deterministic cleaning step. |
| UI | Streamlit, single-file app, with custom CSS for a dark/glass visual theme | Flask + custom HTML chat widget, or a separate React/Tailwind/shadcn frontend | Streamlit's `st.chat_message`/`st.chat_input` gave a working conversational interface in a fraction of the code, and Streamlit Community Cloud gives a free hosted, testable-without-local-setup link in a few clicks — both matter directly for a 6-hour, "must be hosted, no budget" deliverable. A fully separate React frontend was considered for a more polished look, but was rejected: it would require its own build/hosting step and a backend API layer, adding deployment risk for a same-day submission with no visual requirement in the grading criteria. Custom CSS on top of Streamlit gets most of the visual polish (dark theme, glass panels, glowing status badges, styled chat bubbles) without that risk. |
| API keys in the running app | Read only from `st.secrets`/environment variables, never typed into an in-app form | Sleek in-app input fields with show/hide toggles for pasting keys | Typing API keys into a web form means they travel through the browser and app server on every use; reading them from Streamlit's secrets store (set once at deploy time) is the safer default, which mattered here given the security-relevant nature of the assignment. |
| MCP connection lifetime | Open a **fresh MCP session per chat turn** | Keep one long-lived MCP session open for the whole app session | Simpler and more robust against idle-connection drops for a same-day build; costs a small amount of extra latency (one new handshake per message) which is acceptable for a conversational BI tool where answers already take a few seconds. |
| Board discovery | Look boards up **by name** each session | Hardcode board IDs in config | IDs aren't known until after the grader imports the CSVs into their own monday.com account; name-based lookup makes the repo usable without editing IDs. |
| Conversation state | In-memory (`st.session_state`), no database | Persist chat history in a database | Out of scope for a same-day prototype; flagged as a "with more time" item below. |

## What I'd do differently with more time

- **Automated tests / eval set** — a small fixed set of founder questions
  with expected-shape answers (e.g. "does the answer mention the sector
  breakdown and a data-quality caveat"), run against the live boards, to
  catch regressions instead of manually re-testing by hand.
- **Persistent MCP session** — reuse one open MCP connection across a chat
  session instead of reconnecting every turn, once I'd handled reconnection
  logic for dropped/idle connections properly.
- **Structured "leadership update" export** — currently the leadership
  update is a chat answer in Markdown; with more time I'd add a one-click
  export to a formatted doc/slide rather than copy-paste from chat.
- **Sector/label normalization pass** — surface a review step where the
  agent proposes a canonical mapping for near-duplicate labels (e.g. sector
  names that differ only in casing or a stray space) and asks a human to
  confirm before using it in aggregates, rather than deciding silently.
- **Revisit the LLM choice once budget allows** — swap in Claude or GPT-5
  behind the same MCP-session-as-tool pattern (both support it) and compare
  answer quality/tool-use reliability against Gemini on the same question
  set, now that the architecture isn't provider-locked.
- **Auth beyond a personal token** — for a real multi-user deployment, move
  from a single personal API token in `secrets.toml` to per-user OAuth
  (monday.com's hosted MCP server supports this) so the agent runs with
  each founder's own monday.com permissions.

## How I interpreted "prepare data for leadership updates"

I treated this as: the agent should be able to produce, on request (chat
message or one sidebar button), a **self-contained executive brief** —
headline pipeline/execution/billing metrics, notable movers (biggest
wins/losses/stuck items), a sector breakdown, and an explicit "data quality
notes" section — rather than just answering one-off questions. The
instruction for this lives in the system prompt (see `app.py`), and the
sidebar's "Prepare leadership update" button sends a canned prompt that
triggers it, so a founder doesn't need to know how to phrase the request. I
scoped it to Markdown output in the chat (see "with more time" above for a
proper doc/slide export) since a polished export pipeline felt like more
than the optional requirement warranted within the time budget.
