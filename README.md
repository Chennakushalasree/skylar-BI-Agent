# Skylark Drones — monday.com Business Intelligence Agent

A conversational agent that answers founder-level BI questions ("How's our
pipeline looking for energy sector this quarter?") by querying live
monday.com data across two boards: **Deal Funnel** (sales pipeline) and
**Work Order Tracker** (project execution & billing).

**Live demo:** _https://skylar-bi-agent-mtxw3zhadx9dranffadiwg.streamlit.app/_


Built end-to-end on **free tiers only** — Google Gemini API (no credit
card), monday.com free plan, GitHub, and Streamlit Community Cloud.

---

## Architecture

```
 ┌──────────────┐   chat message    ┌───────────────────────┐
 │  Streamlit   │ ─────────────────▶│  Gemini API (Google)   │
 │  chat UI     │                   │  (google-genai SDK)    │
 │  (app.py)    │◀───────────────── │                        │
 └──────────────┘  founder-readable └───────────┬────────────┘
                        answer                   │ Gemini's automatic
                                                  │ function calling —
                                                  │ Gemini decides which
                                                  │ monday.com tool to call
                                                  ▼
                                   ┌──────────────────────────────┐
                                   │  MCP client session           │
                                   │  (official `mcp` Python SDK,   │
                                   │  opened fresh each chat turn)  │
                                   └───────────────┬────────────────┘
                                                    │ Streamable HTTP,
                                                    │ Bearer token
                                                    ▼
                                   ┌──────────────────────────────┐
                                   │  monday.com hosted MCP server │
                                   │  https://mcp.monday.com/mcp   │
                                   └───────────────┬────────────────┘
                                                    │
                                                    ▼
                                   ┌──────────────────────────────┐
                                   │  monday.com boards:            │
                                   │  • Deal Funnel                 │
                                   │  • Work Order Tracker          │
                                   └──────────────────────────────┘
```

**Why this shape:** the assignment requires connecting to monday.com "via MCP
or API" without hardcoding CSV data. monday.com ships an official, maintained
**hosted MCP server** (`https://mcp.monday.com/mcp`) exposing 60+ tools for
reading/writing boards. Rather than hand-writing a GraphQL wrapper, this app
opens a real MCP client session to that server (using the official `mcp`
Python SDK) and hands the live session to Gemini as a callable tool. Gemini's
SDK then automatically discovers monday.com's tools and calls them as needed
— every answer is backed by a real API call at question time, nothing is
cached or hardcoded. See `DECISION_LOG.md` for why Gemini + a direct MCP
client was chosen over other options (in short: it's the only fully free
combination that still uses a real MCP connection, not a workaround).

All the "make sense of messy business data" logic lives in the **system
prompt** in `app.py`: a data dictionary for both boards (what each column
means, which ones are usually blank and why, how the two boards join on the
masked deal codename) plus explicit instructions to caveat incomplete data
rather than silently drop or misrepresent it.

---

## Repo contents

| File | Purpose |
|---|---|
| `app.py` | Streamlit chat app + system prompt + Gemini↔MCP call loop |
| `requirements.txt` | Python dependencies |
| `.streamlit/secrets.toml.example` | Template for API keys (copy → `secrets.toml`, don't commit the real one) |
| `Deal_Funnel_cleaned.csv` | Deal board source data, ready to import into monday.com |
| `Work_Order_Tracker_cleaned.csv` | Work order board source data, ready to import into monday.com |
| `DECISION_LOG.md` | Assumptions, trade-offs, what to do with more time |

---

## Full setup, from absolute zero

You'll create four free accounts and connect them. None require a credit
card. Total time: roughly 30–40 minutes the first time.

### Part 1 — monday.com (where the business data lives)

1. Go to [monday.com](https://monday.com) → **Get started** → sign up with
   your email (free plan, no card).
2. Once inside, create a new board: click **+ Add** in the left sidebar →
   **New board** → name it exactly `Deal Funnel`.
3. Import the data into it: open the board → click the **⋮** (three dots)
   next to the board name → **Import data** → **Excel/CSV File** → upload
   `Deal_Funnel_cleaned.csv` from this repo. monday.com will show you a
   preview and auto-suggest a column type for each column (Status for
   `Deal Status`, Date for the date columns, Numbers for `Masked Deal
   value`, Text/Dropdown for the rest) — accept the suggestions and finish
   the import.
4. Repeat steps 2–3 for a second board named exactly `Work Order Tracker`,
   importing `Work_Order_Tracker_cleaned.csv`.
5. Get your **personal API token** (this is how the agent authenticates,
   read-only in practice since the agent never sends write requests):
   click your **profile picture** (bottom-left) → **Developers** →
   **My Access Tokens** → **Show**/**Generate** → copy the long token
   string. Save it somewhere for a moment — this is your `MONDAY_API_TOKEN`.

### Part 2 — Google Gemini API (the agent's "brain," genuinely free)

1. Go to [Google AI Studio](https://aistudio.google.com/) and sign in with
   any Google account.
2. Click **Get API key** (left sidebar) → **Create API key**. No billing
   setup, no credit card — this is a real, permanent free tier.
3. Copy the key. This is your `GEMINI_API_KEY`.

### Part 3 — GitHub (to host the code so Streamlit can deploy it)

1. If you don't already have one, sign up free at
   [github.com](https://github.com).
2. Create a **new repository** (name it e.g. `skylark-bi-agent`), then
   upload the contents of this folder to it. Easiest way if you're not
   comfortable with git commands: on the repo page, click
   **Add file → Upload files**, and drag in everything from this folder
   *except* `.streamlit/secrets.toml` (it shouldn't exist yet if you
   haven't created it — only `secrets.toml.example` should be uploaded).

   If you do want to use git from a terminal instead:
   ```bash
   cd skylark-bi-agent
   git init
   git add .
   git commit -m "Skylark Drones BI agent"
   git branch -M main
   git remote add origin https://github.com/<your-username>/skylark-bi-agent.git
   git push -u origin main
   ```

### Part 4 — Streamlit Community Cloud (free hosting, gives you a public link)

1. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with
   your GitHub account (free, no card).
2. Click **Create app** → **From existing repo** → pick your
   `skylark-bi-agent` repo → branch `main` → main file path `app.py` →
   **Deploy**.
3. While it's building, click **Advanced settings → Secrets** (or, after
   deploying, go to **⋮ → Settings → Secrets**) and paste in:
   ```toml
   GEMINI_API_KEY = "paste your key here"
   MONDAY_API_TOKEN = "paste your token here"
   ```
4. Save. The app rebuilds automatically. After ~1 minute you'll have a
   public URL like `https://your-app-name.streamlit.app` — that's your
   hosted prototype link.

### Running it locally instead (optional, for testing before you deploy)

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# edit .streamlit/secrets.toml and paste in your two real keys
streamlit run app.py
```
This opens the chat app in your browser at `http://localhost:8501`.

---

## Using the agent

Type a question in the chat box, e.g.:
- "How's our pipeline looking for the Mining sector this quarter?"
- "Which won deals don't have a work order yet?"
- "What's our collection rate — how much billed value is still uncollected?"
- "Give me a leadership update" (or click the sidebar button)

Expand the "🔧 monday.com tool call" / "📋 monday.com response" blocks under
any answer to see exactly which board(s) and query the agent ran — useful
for trusting (or debugging) an answer.

---

## Known limitations

- Gemini's built-in MCP support is an experimental feature of the
  `google-genai` SDK; behavior may change as it matures — see
  `DECISION_LOG.md`.
- Read-only by design: the agent never creates/edits/deletes monday.com
  items, per the assignment's "Read: All data" scope. It's simply never
  given a reason to call a write tool, and the system prompt explicitly
  tells it not to claim to have written anything.
- A fresh MCP connection is opened per chat turn rather than kept open for
  the whole session — simpler and more robust for a same-day build, at a
  small latency cost per message. See Decision Log.
- No conversation persistence across browser sessions (in-memory only) —
  acceptable for a prototype; see Decision Log for what a v2 would add.
- Gemini's free tier has per-minute/per-day rate limits — fine for a demo
  or grading session, not for production volume.
