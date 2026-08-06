# Running Book

Operational how-to for starting each agent in this repo. For what each
example teaches, see [README.md](README.md) — this doc is just "how do I run
it."

## One-time setup

```bash
cd adk2-examples
source .venv/bin/activate      # venv with google-adk 2.5.0 already created
cp .env.example .env           # then paste your GOOGLE_API_KEY (aistudio.google.com/apikey)
```

Every command below is run from this directory (`adk2-examples/`) so `adk`
picks up `.env` automatically.

## Quick reference

| Agent folder | Entry point | Needs API key | Human-in-the-loop |
|---|---|---|---|
| `graph_router` | `root_agent` (Workflow) | yes | no |
| `travel_planner` | `root_agent` (Agent) | yes | no |
| `refund_approval` | `root_agent` / `app` | yes | yes (refunds > $100) |
| `front_desk` | `root_agent` / `app` (nests the other three) | yes | yes (via REFUND branch) |

Two ways to run any of them: `adk web` (browser UI, one server for all four)
or `adk run <folder>` (terminal, one agent at a time). `refund_approval` and
`front_desk` pause for human input, so they're easiest to drive interactively
in `adk web`; use `adk run` for the other two if you just want a terminal.

## Option A — `adk web` (all four, browser UI)

```bash
adk web
```

Opens the dev UI (default `http://127.0.0.1:8000`). Pick the agent from the
dropdown in the top-left. Switching agents starts a fresh session. The Trace
tab is useful for watching graph routing in `graph_router` and `front_desk`.

## Option B — `adk run` (one agent, terminal)

```bash
adk run graph_router                              # interactive REPL
adk run graph_router "My dashboard shows 500 errors"   # single-turn, then exit
```

Works the same for `travel_planner`. For `refund_approval` and `front_desk`,
prefer interactive mode (`adk run <folder>` with no query) since the workflow
may pause mid-conversation to ask you a yes/no question — reply directly at
the prompt.

## Per-agent run notes

### `graph_router`

```bash
adk run graph_router
```
Try: `hi` (no route, classifier just greets you) · `My dashboard shows 500
errors on the analytics page` (→ BUG) · `How much does the Pro plan cost?`
(→ BILLING) · `It would be great to have dark mode` (→ FEATURE).

### `travel_planner`

```bash
adk run travel_planner
```
Try: `What's the weather in Paris today?` (single_turn sub-agent, runs once)
· `Book me a flight from SFO to CDG` (task sub-agent, will ask you for a
date — answer it in the same session) · `Check the weather in Paris and book
me a flight from SFO to CDG` (both).

### `refund_approval`

```bash
adk run refund_approval
```
Try: `Customer 001 wants a $50 refund for a duplicate charge` (auto-approved,
no pause) · `Customer 002 wants a $350 refund, product didn't work` (workflow
**pauses** and asks for manager approval — reply `yes` or `no` at the prompt
and it resumes from exactly where it paused).

To resume the pause programmatically instead of interactively, send a
`function_response` part with `id="manager-approval"` — see
[test_runtime_no_llm.py](test_runtime_no_llm.py) for the exact call shape.

### `front_desk`

```bash
adk run front_desk
```
One entry point that triages into the other three (imported, not
duplicated). Try: `My dashboard shows 500 errors` (→ nested `graph_router`) ·
`What's the weather in Paris?` (→ `travel_planner` team) · `Customer 002
wants a $350 refund` (→ nested `refund_approval`, still pauses for approval —
the pause propagates up through both workflow levels; reply `yes`/`no`).

## Running the test suite (no API key needed)

```bash
python test_runtime_no_llm.py
```

Swaps the LLM nodes for code stand-ins and exercises real runtime mechanics:
all three `graph_router` routes, the $50 auto-approval, the $350 pause +
resume cycle, and nested `front_desk` routing including HITL propagation
through two workflow levels.

## Troubleshooting

- **`adk: command not found`** — the venv isn't active: `source
  .venv/bin/activate`.
- **401 / auth errors** — `.env` is missing or `GOOGLE_API_KEY` is unset;
  copy `.env.example` → `.env` and paste a real key from
  aistudio.google.com/apikey.
- **`adk web` port already in use** — pass `--port <n>` to pick another one.
- **`refund_approval` / `front_desk` don't seem to pause** — you're likely
  using `adk run <folder> "<query>"` (single-shot, exits immediately). Use
  interactive mode (`adk run <folder>` with no query, or `adk web`) so you
  can answer the follow-up prompt in the same session.
