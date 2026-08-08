# travel_planner_remote_agent

A variant of [`../travel_planner`](../travel_planner/agent.py) that swaps the
two local sub-agents for **remote A2A agents**, and swaps the coordinator
pattern for an explicit `Workflow` graph. Same idea (check weather, book a
flight), different plumbing underneath — because the plumbing has to be
different once the sub-agents live on another server.

## The problem this solves

`travel_planner` gets a very convenient guarantee for free: `weather_checker`
(`mode="single_turn"`) and `flight_booker` (`mode="task"`) always **auto-return
control** to the coordinator when they're done, so `travel_planner` gets a
turn to combine their results into one reply. That guarantee comes from ADK
wrapping `mode`-tagged sub-agents as tools (`_SingleTurnAgentTool` /
`_TaskAgentTool`) — see `google/adk/agents/llm_agent.py`, the loop that
checks `isinstance(sub_agent, LlmAgent) and mode is None`.

`RemoteA2aAgent` (used to reach a sub-agent hosted on another server over
A2A) extends `BaseAgent` directly, not `LlmAgent` — **it has no `mode`
field**. So that `isinstance(..., LlmAgent)` check never fires for it, and
neither wrapper ever gets attached. Put a `RemoteA2aAgent` straight into
`travel_planner`'s `sub_agents=[...]` and it falls back to the old
`transfer_to_agent` control-transfer instead: whatever the remote agent says
becomes the final turn, and the coordinator never automatically gets a
chance to combine it with anything else — nor is there any guarantee it gets
a turn back at all, unless the remote agent is itself built to transfer back.

This example solves that by **not depending on the sub-agent's cooperation**.
Instead of a coordinator, `root_agent` here is a `Workflow` graph — control
return to the synthesis step is guaranteed by the graph's static edges, not
by an LLM's judgment call or a remote service transferring back correctly.

## What's actually different from `travel_planner`

| | `travel_planner` | `travel_planner_remote_agent` |
|---|---|---|
| Sub-agent type | Local `Agent` (`LlmAgent`) | `RemoteA2aAgent` (A2A, hosted elsewhere) |
| `root_agent` shape | One coordinator `Agent` with `sub_agents=[...]` | A `Workflow` graph with explicit nodes/edges |
| How a sub-agent is reached | `mode="single_turn"` / `mode="task"` → auto-wrapped as a tool | `ctx.run_node(remote_agent, ...)` inside a `@node` function, called directly |
| Who decides to combine results | The coordinator's own LLM, each turn, as a judgment call | A dedicated `synthesizer` node that the graph *always* routes to |
| Guarantee of getting a combined reply | Usually — but only as strong as the LLM's judgment (see caveat below) | Always — enforced by graph structure, independent of any agent's behavior |
| Multi-topic requests ("weather + flight") | Both sub-agents called, then coordinator's LLM decides to combine | Both gates run **concurrently** (`asyncio.gather`), then `join` waits for both before `synthesizer` runs |

The graph:

```
START -> capture_query -> classifier -> router -> {WEATHER: weather_gate, FLIGHT: flight_gate}
weather_gate -> {JOIN: join, DIRECT: synthesizer}
flight_gate  -> {JOIN: join, DIRECT: synthesizer}
join -> synthesizer
```

- `classifier` (LLM) tags the request with `WEATHER`, `FLIGHT`, both, or
  neither.
- `router` (`@node`, plain Python) turns those tags into which gate(s) fire —
  deterministic, not an LLM decision.
- `weather_gate` / `flight_gate` (`@node`, async) each call their remote
  agent via `await ctx.run_node(...)`, then pick their **own** downstream
  route: straight to `synthesizer` if they were the only topic requested, or
  to `join` if both topics are in play.
- `join` (`JoinNode`) waits for **all** of its wired predecessors before
  continuing.
- `synthesizer` (LLM) always runs last, and always produces one consolidated
  reply — never just relays a single gate's text verbatim.

### The gotcha this design works around

The first, more obvious design — router fans out conditionally, both gates
feed one shared `JoinNode` unconditionally — silently breaks for single-topic
requests. `JoinNode` waits for **all** of its *statically declared* graph
predecessors on every run, not just the ones the router actually triggered
that turn. Ask about weather only, and `flight_gate` never runs — the shared
join then waits forever for a predecessor that was never going to fire, and
the whole run just ends with no response, no error.

The fix: each gate decides its own outgoing route *after* calling its remote
agent, based on how many topics were requested (`ctx.state["travel_modes"]`,
set by `router`). `join`'s two predecessors are therefore only ever wired
together on the one path where both are guaranteed to fire together.

## How to run it

**Point the two `agent_card` URLs at real A2A services first.** Right now
they're placeholders:

```python
weather_checker = RemoteA2aAgent(
    name="weather_checker",
    agent_card="https://weather-service.example.com/.well-known/agent-card.json",
)
flight_booker = RemoteA2aAgent(
    name="flight_booker",
    agent_card="https://flight-service.example.com/.well-known/agent-card.json",
)
```

Swap those for the URL (or local file path) of a real agent card — an A2A
server exposing `get_weather`-style and `book_flight`-style agents. This repo
doesn't ship those servers; `weather_checker`/`flight_booker` in
`../travel_planner/agent.py` show the tool logic they'd need to wrap.

Once real agent cards are wired in:

```bash
cd adk2-examples
source .venv/bin/activate
.venv/bin/pip install "google-adk[a2a]"   # installs a2a-sdk; RemoteA2aAgent needs it
cp .env.example .env                       # then paste your API key
adk web                                    # pick travel_planner_remote_agent from the dropdown
```

or from the terminal:

```bash
adk run travel_planner_remote_agent
```

Try:
- `What's the weather in Paris?` → only `weather_gate` fires, routes `DIRECT`
  to `synthesizer`, `flight_gate`/`join` never run.
- `Book me a flight from SFO to Paris on 2026-09-01` → only `flight_gate`
  fires, same `DIRECT` path.
- `Check the weather in Paris and book me a flight from SFO to Paris on
  2026-09-01` → both gates fire concurrently, `join` waits for both, then
  `synthesizer` combines them into one reply.

### Verifying the graph without a live A2A server

Constructing `RemoteA2aAgent` doesn't touch the network — card resolution
happens lazily on first call — so you can confirm the workflow wires up
correctly even before pointing the agent cards at anything real:

```bash
OPENAI_API_KEY=test MODEL=gpt-4o .venv/bin/python -c "
import travel_planner_remote_agent.agent as m
print([n.name for n in m.root_agent.graph.nodes])
"
```

This should print all eight nodes (`__START__`, `capture_query`,
`classifier`, `router`, `weather_gate`, `flight_gate`, `join`, `synthesizer`)
with no `ValueError` from the graph validator — confirming the edges,
routing map, and join wiring are all structurally sound.
