"""Example 2b — Explicit synthesis over remote (A2A) sub-agents (ADK 2.x).

Variant of ../travel_planner that swaps the two local sub-agents for remote
A2A agents, and swaps the coordinator (`sub_agents=[...]` + `mode=`) pattern
for an explicit graph `Workflow`.

Why this needs a different shape than travel_planner:
`RemoteA2aAgent` extends `BaseAgent` directly, not `LlmAgent` — it has no
`mode` field. The `mode="single_turn"/"task"` auto-return-to-coordinator
trick (weather_checker/flight_booker in ../travel_planner) only fires for
`isinstance(sub_agent, LlmAgent)`, so a remote sub-agent placed in
`sub_agents=[...]` falls back to the old ADK 1.x `transfer_to_agent`
control-transfer: whatever the remote agent says becomes the final turn,
and the root never automatically gets a chance to combine it with anything
else.

So instead of a coordinator, this is a `Workflow` graph:

    START -> classifier (LLM: which topics apply?)
          -> router (@node: decide WEATHER / FLIGHT / both, in code)
          -> weather_gate / flight_gate (@node: call the remote agent via
             ctx.run_node, then route onward)
          -> join (only reached when BOTH topics apply — waits for both
             gates)
          -> synthesizer (LLM: always runs, always produces one
             consolidated reply)

Gotcha this design works around: `JoinNode` waits for ALL of its
statically-declared predecessors on every run, not just the ones the router
actually triggered. Feeding a conditionally-skippable branch straight into
a shared join silently stalls the workflow (it just ends with no response)
whenever only one branch fires. The fix here: each gate node decides its
own outgoing route (JOIN vs DIRECT-to-synthesizer) based on whether both
topics were requested, so `join`'s two predecessors are only ever wired
together on the path where both are guaranteed to fire together.
"""

import os

from google.adk.agents import Agent
from google.adk.agents import Context
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.workflow import START
from google.adk.workflow import JoinNode
from google.adk.workflow import Workflow
from google.adk.workflow import node

from key_check import log_api_key

MODEL = LiteLlm(model="openai/" + os.environ.get("MODEL", "gpt-4o"))

# --- Remote sub-agents (A2A) ------------------------------------------------
# Placeholder agent cards — point these at real A2A services to make this
# example runnable end to end. Constructing RemoteA2aAgent does not touch
# the network; card resolution happens lazily on first call.

weather_checker = RemoteA2aAgent(
    name="weather_checker",
    description="Checks the current weather for a given city.",
    agent_card="https://weather-service.example.com/.well-known/agent-card.json",
)

flight_booker = RemoteA2aAgent(
    name="flight_booker",
    description="Books flights between two cities.",
    agent_card="https://flight-service.example.com/.well-known/agent-card.json",
)

# --- Step 1: classify which topics the request touches ---------------------

classifier = Agent(
    model=MODEL,
    before_model_callback=log_api_key,
    name="classifier",
    description="Decides which travel topics a request touches.",
    instruction="""Classify which topics the user's message touches.

Respond with the applicable tags, comma-separated, using ONLY these words:
WEATHER, FLIGHT

Examples:
- "what's the weather in Paris?" -> WEATHER
- "book me a flight to Tokyo" -> FLIGHT
- "check the weather and book me a flight to Paris" -> WEATHER,FLIGHT

If neither applies, respond with NONE.""",
)


# --- Step 2: turn the classification into a route, in code -----------------


@node
def router(ctx: Context, node_input: str) -> str:
    """Reads the classifier's tags and decides which gate(s) to trigger."""
    text = str(node_input).strip().upper()
    modes = [m for m in ("WEATHER", "FLIGHT") if m in text]
    ctx.state["travel_modes"] = modes
    ctx.route = modes
    if not modes:
        return "no travel topic detected — nothing to route"
    return ctx.state.get("original_query", node_input)


@node
def capture_query(ctx: Context, node_input: str) -> str:
    """Stashes the original user message for the gates to use later — by
    the time router/weather_gate/flight_gate run, node_input has already
    been rewritten by the classifier into a WEATHER/FLIGHT tag."""
    ctx.state["original_query"] = node_input
    return node_input


# --- Step 3: gates — call the remote agent, then route onward --------------
# Each gate calls its remote agent via ctx.run_node (the same mechanism ADK
# uses internally for mode="single_turn" sub-agents), then decides its OWN
# outgoing route: straight to the synthesizer if it's the only topic, or to
# the join if both topics are in play (in which case the other gate is
# guaranteed to also route to the join on this same run).


@node
async def weather_gate(ctx: Context, node_input: str) -> str:
    query = ctx.state.get("original_query", node_input)
    result = await ctx.run_node(weather_checker, node_input=query)
    ctx.route = "JOIN" if len(ctx.state.get("travel_modes", [])) > 1 else "DIRECT"
    return result


@node
async def flight_gate(ctx: Context, node_input: str) -> str:
    query = ctx.state.get("original_query", node_input)
    result = await ctx.run_node(flight_booker, node_input=query)
    ctx.route = "JOIN" if len(ctx.state.get("travel_modes", [])) > 1 else "DIRECT"
    return result


# --- Step 4: join only ever waits on a pair that always fires together -----

join = JoinNode(name="join")

# --- Step 5: synthesis — always runs, always produces one final reply ------

synthesizer = Agent(
    model=MODEL,
    before_model_callback=log_api_key,
    name="synthesizer",
    description="Combines the gathered travel results into one final reply.",
    instruction="""You will receive either a single result string, or a JSON
object keyed by which gate produced it (e.g. "weather_gate", "flight_gate").
Write ONE consolidated, friendly response covering everything you were
given. Never just repeat a single field verbatim if more than one topic was
requested — always combine them into a single coherent answer.""",
)


root_agent = Workflow(
    name="travel_planner_remote_agent",
    edges=[
        (
            START,
            capture_query,
            classifier,
            router,
            {"WEATHER": weather_gate, "FLIGHT": flight_gate},
        ),
        (weather_gate, {"JOIN": join, "DIRECT": synthesizer}),
        (flight_gate, {"JOIN": join, "DIRECT": synthesizer}),
        (join, synthesizer),
    ],
)
