"""Example 4 — Composing the other three examples (nested workflows).

One front desk that triages every incoming message and hands it to the right
team — where each "team" is one of the previous examples, plugged in whole:

    START -> front_desk_classifier -> front_desk_router
                 ├── SUPPORT -> graph_router workflow   (a NESTED workflow:
                 │              its own classifier+router+handlers run inside)
                 ├── TRAVEL  -> travel_planner coordinator (an LlmAgent with
                 │              its own two sub-agents, used as a graph node)
                 └── REFUND  -> refund workflow          (nested workflow with
                                human-in-the-loop — the pause propagates up)

The point: in ADK 2.x everything is "node-like". An entire Workflow, an agent
tree, or a plain function can all sit on the same routing map. The nested
pieces are imported from the sibling example folders — nothing is rewritten.
"""

import os

from google.adk.agents import Agent
from google.adk.agents import Context
from google.adk.apps import App
from google.adk.apps import ResumabilityConfig
from google.adk.models.lite_llm import LiteLlm
from google.adk.workflow import START
from google.adk.workflow import Workflow
from google.adk.workflow import node

from graph_router.agent import root_agent as support_workflow
from refund_approval.agent import workflow as refund_workflow
from travel_planner.agent import root_agent as travel_root

from key_check import log_api_key

MODEL = LiteLlm(model="openai/" + os.environ.get("MODEL", "gpt-4o"))

DEPARTMENTS = ("SUPPORT", "TRAVEL", "REFUND")

# The travel coordinator is also the root agent of its own app; clone it so
# this workflow attaches to a private copy (agents track their parent, and
# one instance can't belong to two trees in the same adk web process).
# mode="single_turn" because it sits as a graph node after front_desk_router,
# which can't feed a chat-mode agent (chat mode expects session history, not
# a direct node input).
travel_team = travel_root.clone({"mode": "single_turn"})

# --- Front-desk triage: one small LLM + one routing function --------------

front_desk_classifier = Agent(
    model=MODEL,
    before_model_callback=log_api_key,
    name="front_desk_classifier",
    description="Triage: decides which department a message belongs to.",
    instruction="""You are the front desk of a software company. Triage the
user's message into exactly ONE department:
- SUPPORT: bug reports, billing questions, feature requests
- TRAVEL: weather questions, flight bookings, trip planning
- REFUND: refund requests

If the message is a follow-up in an ongoing conversation (for example,
answering a question an agent just asked, like a travel date or a refund
approval), return the SAME department as that conversation.

If the user just greets you or asks what you can do, greet them briefly,
list the three departments, and return no department.

Otherwise respond with ONLY the department word in uppercase.""",
)


@node
def front_desk_router(ctx: Context, node_input: str) -> str:
    text = str(node_input).strip().upper()
    for dept in DEPARTMENTS:
        if dept in text:
            ctx.route = dept
            return f"routed to {dept}"
    return "no department detected — nothing to route"


# --- The composition: whole examples as routing targets -------------------

workflow = Workflow(
    name="front_desk",
    edges=[
        (
            START,
            front_desk_classifier,
            front_desk_router,
            {
                "SUPPORT": support_workflow,  # nested Workflow
                "TRAVEL": travel_team,        # agent tree as a node
                "REFUND": refund_workflow,    # nested Workflow with HITL
            },
        ),
    ],
)

# Resumable so the refund branch's human-approval pause survives the turn.
app = App(
    name="front_desk",
    root_agent=workflow,
    resumability_config=ResumabilityConfig(is_resumable=True),
)

root_agent = workflow
