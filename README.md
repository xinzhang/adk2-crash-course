# ADK 2.x Examples — the three features from the video

Reconstructions of the three demos from the YouTube video on Google ADK 2.0
(the presenter shared no repo, so these are rebuilt from what's shown on
screen, verified against the real `google-adk` 2.5.0 APIs).

| Example | ADK 2.x feature | Key API |
|---|---|---|
| [graph_router](graph_router/agent.py) | Graph-based workflows | `Workflow`, edges, `ctx.route` |
| [travel_planner](travel_planner/agent.py) | Collaborative agent modes | `mode="single_turn"` / `mode="task"` |
| [refund_approval](refund_approval/agent.py) | Dynamic workflows + human-in-the-loop | `@node`, `RequestInput`, `ctx.resume_inputs` |
| [front_desk](front_desk/agent.py) | Nested workflows (composes the other three) | workflows & agent trees as graph nodes, `clone()` |

## Setup

```bash
cd adk2-examples
# venv with google-adk 2.5.0 is already created at .venv/
cp .env.example .env       # then paste your GOOGLE_API_KEY
source .venv/bin/activate
adk web                    # opens the dev UI; pick an agent in the dropdown
```

## What to try in adk web

**graph_router** — watch the Trace tab: classifier → router → handler, deterministically.
- `hi` → the classifier greets you (no route matches, graph ends)
- `My dashboard shows 500 errors on the analytics page` → BUG handler
- `How much does the Pro plan cost?` → BILLING handler
- `It would be great to have dark mode` → FEATURE handler

**travel_planner** — watch control return to the coordinator after each sub-agent.
- `What's the weather in Paris today?` → weather_checker (single_turn: runs once, auto-returns)
- `Book me a flight from SFO to CDG` → flight_booker (task: asks you for the date, then auto-returns)
- `Check the weather in Paris and book me a flight from SFO to CDG` → both

**refund_approval** — the business rule is an `if` statement, not a prompt.
- `Customer 001 wants a $50 refund for a duplicate charge` → auto-approved
- `Customer 002 wants a $350 refund, product didn't work` → workflow **pauses**
  and asks you to approve; reply `yes` (or `no`) and it resumes exactly where
  it left off

**front_desk** — one entry point that triages to the other three examples.
- `My dashboard shows 500 errors` → routed to the nested graph_router workflow
  (its own classifier picks BUG inside)
- `What's the weather in Paris?` → routed to the travel team (coordinator +
  its two sub-agents run as one graph node)
- `Customer 002 wants a $350 refund` → routed into the nested refund
  workflow, which pauses for your approval — the pause propagates up through
  both workflow levels, and your `yes` resumes the inner node

## How each one works (the learning notes)

### 1. Graph-based workflows (`graph_router`)

In ADK 1.x you put routing rules in one big prompt and hoped the LLM followed
them. In 2.x the control flow is a graph declared in code:

```python
root_agent = Workflow(
    name="graph_router",
    edges=[
        (START, classifier, router, {
            "BUG": handle_bug,
            "BILLING": handle_billing,
            "FEATURE": handle_feature_request,
        }),
    ],
)
```

- A tuple is a **chain**: START → classifier → router.
- A dict is a **routing map**: which edge fires depends on the route the
  previous node emitted.
- The `router` function node reads the classifier's text (via its
  `node_input` parameter — that name is special and receives the upstream
  node's output) and sets `ctx.route = "BUG"`. Only the matching edge fires.
- The LLM only classifies. It *cannot* skip steps or call the wrong handler,
  because it doesn't do the calling — the graph does.

### 2. Collaborative agent modes (`travel_planner`)

The 1.x pain: after `transfer_to_agent`, the sub-agent had to remember to
transfer back. Often it didn't, and the coordinator lost control. In 2.x the
handoff contract is a field:

```python
weather_checker = Agent(..., mode="single_turn")  # run once, auto-return
flight_booker  = Agent(..., mode="task")          # may chat with the user,
                                                  # auto-returns when done
```

Under the hood ADK wraps these sub-agents as tools of the coordinator
(`_SingleTurnAgentTool` / `_TaskAgentTool`), so returning control is just a
tool call returning — automatic and guaranteed. (`mode="chat"` is the old
transfer behavior; the video calls `single_turn` "singleton".) A task-mode
agent also gets a `finish_task` tool so it can signal completion.

### 3. Dynamic workflows + human-in-the-loop (`refund_approval`)

The business rule lives in plain Python, and the human pause is a first-class
workflow primitive:

```python
@node(rerun_on_resume=True)
def decide_and_process(ctx: Context, node_input: RefundRequest):
    if node_input.amount <= 100:
        return {...}                          # auto-approve: just an `if`

    answer = ctx.resume_inputs.get("manager-approval")
    if answer is None:
        return RequestInput(                  # PAUSE: ask a human
            interrupt_id="manager-approval",
            message="Approve this $350 refund? (yes/no)",
            response_schema={"type": "string"},
        )
    ...                                       # RESUMED: answer is here
```

- Returning a `RequestInput` interrupts the workflow. In adk web it surfaces
  as a question; programmatically it's a long-running `adk_request_input`
  function call.
- `rerun_on_resume=True` means the node re-runs after the human answers —
  and this time `ctx.resume_inputs[interrupt_id]` contains the reply.
  (The video calls this "resume data".)
- The `App(..., resumability_config=ResumabilityConfig(is_resumable=True))`
  wrapper makes the pause durable: state is checkpointed in the session, so
  the workflow can resume even across restarts (with a persistent session
  service).
- To resume programmatically, send a `function_response` part with
  `id=<interrupt_id>` and `response={"result": <the answer>}` — see
  [test_runtime_no_llm.py](test_runtime_no_llm.py).

### 4. Nested workflows — composition (`front_desk`)

Everything in ADK 2.x is "node-like", so a routing map can mix a plain
function, an entire agent tree, and a whole other workflow:

```python
workflow = Workflow(
    name="front_desk",
    edges=[
        (START, front_desk_classifier, front_desk_router, {
            "SUPPORT": support_workflow,   # nested Workflow (graph_router)
            "TRAVEL": travel_team,         # LlmAgent + its sub-agents
            "REFUND": refund_workflow,     # nested Workflow with HITL
        }),
    ],
)
```

- The nested pieces are **imported from the sibling folders** — nothing is
  rewritten. `graph_router`'s whole classifier→router→handlers graph runs
  inside the SUPPORT branch.
- The travel coordinator is **cloned** (`travel_root.clone()`) because an
  agent instance tracks its parent and can't belong to two trees at once —
  and it's still the root of its own standalone app in the same process.
- The refund branch's human-approval **interrupt propagates up** through both
  workflow levels: the outer workflow pauses, and the resume finds its way
  back to the inner `decide_and_process` node. Verified in Test 3 of the
  test file.

## Tests (no API key needed)

```bash
.venv/bin/python test_runtime_no_llm.py
```

Replaces the LLM nodes with code stand-ins and exercises the real runtime
mechanics: all three graph routes, the $50 auto-approval, the $350 pause, the
resume-with-"yes" cycle (using the actual `decide_and_process` node), and the
nested front_desk routing including HITL propagation through two workflow
levels.

## Version note

Built and verified against `google-adk==2.5.0`. The workflow APIs
(`google.adk.workflow`) and `ResumabilityConfig` are marked experimental
upstream, so details may shift between 2.x releases.
