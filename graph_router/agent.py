"""Example 1 — Graph-based workflow (ADK 2.x).

A support-ticket triage graph:

    START -> classifier (LLM) -> router (code) -> one of three handlers (code)

The LLM does ONE thing: classify the message. All routing lives in code as
graph edges, so it is deterministic — the LLM cannot skip steps, call the
wrong handler, or answer in prose instead of routing.

In ADK 1.x you would have crammed "first classify, then call the right
handler, never skip steps..." into one big instruction prompt and hoped the
model followed it.
"""

import os

from google.adk.agents import Agent
from google.adk.agents import Context
from google.adk.models.lite_llm import LiteLlm
from google.adk.workflow import START
from google.adk.workflow import Workflow
from google.adk.workflow import node

from key_check import log_api_key

MODEL = LiteLlm(model="openai/" + os.environ.get("MODEL", "gpt-4o"))

CATEGORIES = ("BUG", "BILLING", "FEATURE")

# --- The only LLM in the graph: a tiny classifier -------------------------

classifier = Agent(
    model=MODEL,
    before_model_callback=log_api_key,
    name="classifier",
    description="Classifies incoming customer support messages.",
    instruction="""You are the intake classifier for a software company's
support desk.

If the user just greets you or asks what you can do, greet them briefly and
explain that you can classify and route support requests (bug reports,
billing questions, feature requests).

Otherwise, classify their message into exactly ONE of these categories:
- BUG: something is broken or erroring
- BILLING: pricing, plans, invoices, payments
- FEATURE: a request for new functionality

Respond with ONLY the category word in uppercase (BUG, BILLING, or FEATURE).
No punctuation, no explanation.""",
)


# --- Deterministic routing, in code ---------------------------------------


@node
def router(ctx: Context, node_input: str) -> str:
    """Reads the classifier's output and emits a route for the graph edges."""
    # Exact match, not substring containment: a greeting reply like "I can
    # classify bug reports, billing questions, or feature requests" contains
    # all three category words, so `category in text` would misroute it.
    text = str(node_input).strip().upper()
    if text in CATEGORIES:
        ctx.route = text
        return f"routed to {text}"
    # Conversational message (e.g. a greeting): no route matches, the branch
    # ends here and the classifier's reply is what the user sees.
    return "no support category detected — nothing to route"


# --- Plain-Python handlers (no LLM involved) ------------------------------


@node
def handle_bug(node_input: str) -> str:
    return (
        "🐛 Bug report filed with the engineering team.\n"
        "Ticket: ENG-4211 (priority: high)\n"
        "You'll receive a status update within 24 hours. If you have logs or "
        "a screenshot, reply here and we'll attach them to the ticket."
    )


@node
def handle_billing(node_input: str) -> str:
    return (
        "💳 Billing question routed to the billing team.\n"
        "Quick answer: the Pro plan is $29/user/month and includes unlimited "
        "dashboards, analytics history, priority support, and SSO.\n"
        "A billing specialist will follow up within 1 business day."
    )


@node
def handle_feature_request(node_input: str) -> str:
    return (
        "✨ Feature request logged on the product roadmap.\n"
        "Reference: PROD-1187\n"
        "Thanks for the suggestion — the product team reviews requests every "
        "sprint and we'll notify you if it ships."
    )


# --- The graph itself: nodes + edges, declared in code --------------------

root_agent = Workflow(
    name="graph_router",
    edges=[
        (
            START,
            classifier,
            router,
            {
                "BUG": handle_bug,
                "BILLING": handle_billing,
                "FEATURE": handle_feature_request,
            },
        ),
    ],
)
