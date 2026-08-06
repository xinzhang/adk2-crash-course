# Evaluating agents in this repo

There are two genuinely different things people mean by "eval an agent," and
they don't compete — they answer different questions:

| | **ADK-native eval** (`adk eval`) | **Langfuse** |
|---|---|---|
| What it is | Built into `google-adk` (`pip install "google-adk[eval]"`) | A separate, optional observability/eval platform (SaaS or self-hosted) |
| What it needs | Nothing extra — local JSON files | A Langfuse project + API keys, and wiring ADK's tracing to it |
| Unit of work | An `.evalset.json` file: recorded conversations + expected outputs | A "Dataset" of input/expected-output items, run as an "Experiment" |
| Scoring | Built-in metrics (`tool_trajectory_avg_score`, `response_match_score`, or LLM-as-judge) | Same idea (rule-based or LLM-as-judge scorers), configured in Langfuse |
| Where results live | Printed to your terminal / `.adk/eval_history/*.json` on disk | Langfuse's web UI, shared with your team, historical trend charts |
| Best for | Fast local regression checks while iterating on prompts/tools | Team-visible dashboards, production trace review, cross-run comparison |

**You do not need Langfuse to use `adk eval`.** They're independent. Part 1
below is 100% ADK-native and is what was actually run to produce the numbers
you see. Part 2 explains how you'd wire up Langfuse as an addition on top,
if/when you want shared dashboards — it's illustrative (no live Langfuse
instance was stood up to produce it), but the integration points are real
and accurate.

---

## Part 1 — `adk eval` (native, no external service)

### The process

1. **Create an eval set** — an empty JSON file that will hold recorded cases:
   ```bash
   adk eval_set create travel_planner travel_planner_evalset
   # -> travel_planner/travel_planner_evalset.evalset.json
   ```

2. **Record cases.** The easiest way is the web UI: `adk web` → open the
   agent → have a real conversation → **Evals** tab → save the session into
   an eval set. Under the hood this calls the same thing the CLI does:
   `EvalCase.conversation` is built from your session's events via
   `convert_session_to_eval_invocations`, capturing the user's message, the
   agent's actual tool calls, and its final response as the "expected"
   trajectory.

   You can do this from the command line too, against a running `adk web`
   server, without touching the UI — this is exactly how the 3 cases in
   `travel_planner/travel_planner_evalset.evalset.json` were generated:
   ```bash
   # 1. Create a session and send it a real message
   SID=$(curl -s -X POST "http://localhost:8000/apps/travel_planner/users/eval_user/sessions" \
     -H "Content-Type: application/json" -d '{}' | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")

   curl -s -X POST "http://localhost:8000/run_sse" -H "Content-Type: application/json" -d "{
     \"app_name\": \"travel_planner\", \"user_id\": \"eval_user\", \"session_id\": \"$SID\",
     \"new_message\": {\"role\": \"user\", \"parts\": [{\"text\": \"What is the weather in Tokyo?\"}]},
     \"streaming\": true
   }" > /dev/null

   # 2. Save that session into the eval set as a named case
   curl -s -X POST "http://localhost:8000/dev/apps/travel_planner/eval_sets/travel_planner_evalset/add_session" \
     -H "Content-Type: application/json" \
     -d "{\"eval_id\": \"weather_only\", \"session_id\": \"$SID\", \"user_id\": \"eval_user\"}"
   ```

3. **(Optional) Set thresholds** in a config file (`travel_planner/eval_config.json`
   in this repo):
   ```json
   {
     "criteria": {
       "tool_trajectory_avg_score": 1.0,
       "response_match_score": 0.6
     }
   }
   ```
   - `tool_trajectory_avg_score` — did it call the right tools, in the right
     order, with the right args (1.0 = exact match required)
   - `response_match_score` — ROUGE-based similarity between actual and
     expected final text (0.6 = 60% similarity required)
   - Default if you skip this file: `{"tool_trajectory_avg_score": 1.0,
     "response_match_score": 0.8}`

4. **Run it:**
   ```bash
   adk eval travel_planner travel_planner/travel_planner_evalset.evalset.json \
     --config_file_path travel_planner/eval_config.json \
     --print_detailed_results
   ```
   Run only specific cases with `evalset.json:case_a,case_b`.

### Real example in this repo

`travel_planner/travel_planner_evalset.evalset.json` has 3 real recorded
cases, captured exactly as above against the live `travel_planner` agent:

| eval_id | what it asked |
|---|---|
| `weather_only` | "What is the weather in Tokyo?" |
| `flight_only` | "Book me a flight from SFO to Paris on 2026-09-01" |
| `weather_and_flight` | Both, in one message |

Running `adk eval travel_planner travel_planner/travel_planner_evalset.evalset.json --print_detailed_results`
against it (unmodified output):

```
*********************************************************************
Eval Run Summary
travel_planner_evalset:
  Tests passed: 1
  Tests failed: 2
********************************************************************
Eval Set Id: travel_planner_evalset
Eval Id: weather_only
Overall Eval Status: PASSED
---------------------------------------------------------------------
Metric: tool_trajectory_avg_score, Status: PASSED, Score: 1.0, Threshold: 1.0
---------------------------------------------------------------------
Metric: response_match_score, Status: PASSED, Score: 0.952, Threshold: 0.8
---------------------------------------------------------------------
```
`flight_only` and `weather_and_flight` both come back `FAILED` — but not
because the agent did anything wrong. See the gotcha below.

### A real gotcha: task-mode sub-agents break eval re-inference

`adk eval` doesn't replay your recorded conversation verbatim — it re-runs
the agent fresh against the recorded user message(s), then compares the new
trajectory to what was recorded. For `flight_only` and `weather_and_flight`
(both route through `flight_booker`, which is `mode="task"`), that fresh run
crashes during evaluation:

```
ValueError: Function call not found for function response ids: {'call_IbBKNfXMgtiplpoIiebUfY28'}.
  File ".../google/adk/runners.py", line 788, in _resolve_invocation_id_from_fr
```

Root cause, traced through the ADK source: when `flight_booker` (task mode)
finishes, its result is delivered back to `travel_planner` as a
`function_response` event — but that event's `author` is recorded as
`"user"`, not `"flight_booker"` or `"travel_planner"` (a framework quirk of
how task-delegation results get threaded back into the parent's session).
`convert_session_to_eval_invocations` (the same function both the CLI and
the web UI's "save as eval case" button use) doesn't capture that
`author="user"` function-response event as part of the recorded trajectory.
So the eval case ends up missing the link between `travel_planner`'s
`flight_booker` tool call and its result — and when `adk eval` tries to
resolve that link during fresh re-inference, it can't find it and throws.

**Takeaway:** `adk eval` works reliably for `mode="single_turn"` sub-agents
(confirmed: `weather_only` passes cleanly with real scores). For
`mode="task"` sub-agents, recording and re-running eval cases currently
hits this ADK 2.5.0 bug — worth knowing before you invest in building out a
large eval suite around task-mode flows specifically.

---

## Part 2 — Langfuse (separate, optional, illustrative)

Langfuse is not an ADK feature — it's a third-party LLM observability
platform (cloud or self-hosted) that happens to also offer dataset-based
evaluation with a shared web UI. The integration is two independent pieces:

### 2a. Send ADK's traces to Langfuse (tracing/observability)

ADK has built-in OpenTelemetry instrumentation (`google/adk/telemetry/`).
Langfuse exposes a standard OTLP endpoint, so wiring them together is just
environment variables — no ADK-specific Langfuse code needed:

```bash
# .env (illustrative — not wired up in this repo)
OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <base64(public_key:secret_key)>
```

With that set, every `adk web` / `adk run` session's spans (agent runs,
tool calls, LLM calls) show up as traces in Langfuse's UI automatically —
this is the "observability" half, independent of eval.

### 2b. Run a Langfuse Dataset "Experiment" against the agent (eval)

This is Langfuse's equivalent of an `.evalset.json`: a **Dataset** of
input → expected-output items, run through your agent, scored, and
compared across runs in the UI.

```python
# illustrative — requires `pip install langfuse` and a real Langfuse project
from langfuse import Langfuse
from google.adk.runners import InMemoryRunner
from google.genai import types
from travel_planner.agent import root_agent

langfuse = Langfuse()  # reads LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST

# 1. Define the dataset once (mirrors the 3 cases in travel_planner_evalset.evalset.json)
dataset_name = "travel_planner_eval"
langfuse.create_dataset(name=dataset_name)
for item in [
    {"input": "What is the weather in Tokyo?", "expected_output": "sunny, 24C, 8kph"},
    {"input": "Book me a flight from SFO to Paris on 2026-09-01", "expected_output": "confirmation code AD2K88"},
]:
    langfuse.create_dataset_item(dataset_name=dataset_name, **item)

# 2. Run the agent against every item, linking each run to the dataset item
runner = InMemoryRunner(agent=root_agent, app_name="travel_planner")
dataset = langfuse.get_dataset(dataset_name)

for item in dataset.items:
    with item.run(run_name="gpt-4o-baseline") as root_span:
        session = await runner.session_service.create_session(
            app_name="travel_planner", user_id="langfuse_eval"
        )
        final_text = ""
        async for event in runner.run_async(
            user_id="langfuse_eval", session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part.from_text(text=item.input)]),
        ):
            if event.content and event.content.parts:
                final_text = event.content.parts[0].text or final_text

        root_span.update_trace(output=final_text)
        # Score it — either programmatically, or leave it to a Langfuse
        # LLM-as-judge evaluator configured in the UI to score automatically.
        root_span.score_trace(name="exact_match", value=float(item.expected_output in final_text))
```

After this runs, Langfuse's **Datasets → travel_planner_eval → Experiments**
view shows the `gpt-4o-baseline` run with per-item traces (full tool-call
detail, since tracing from 2a is active) and aggregate scores — and you can
run it again after a prompt change and diff the two runs side by side.

### When to reach for which

- Iterating locally on a prompt/tool and want a fast pass/fail signal →
  **`adk eval`**. Zero setup, already works in this repo.
- Want a shared dashboard, historical trend lines across many runs, or
  need non-engineers to review agent outputs → **add Langfuse** on top,
  it doesn't replace `adk eval`, it complements it.
- Hit the task-mode eval bug above and need `flight_booker`-style flows
  evaluated → for now, Langfuse's programmatic runner approach (2b) sidesteps
  the issue entirely, since it doesn't depend on ADK's session-replay/
  invocation-resolution machinery — it just calls the agent and scores
  whatever text comes back.
