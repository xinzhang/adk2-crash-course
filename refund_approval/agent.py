"""Example 3 — Dynamic workflow with human-in-the-loop (ADK 2.x).

A refund-processing workflow with a business rule enforced in code:

    START -> intake (LLM extracts the request) -> decide_and_process (@node)

- Refunds of $100 or less are auto-approved.
- Refunds over $100 PAUSE the workflow (RequestInput) and wait for a human
  manager to approve. When the human answers, the workflow resumes exactly
  where it left off and reads the answer from ctx.resume_inputs.

The three primitives from the video:
  1. @node            — turns a plain Python function into a workflow step,
                        so the business rule is a real `if`, not a prompt.
  2. RequestInput     — returned from a node, it interrupts the workflow and
                        shows a message to the user.
  3. ctx.resume_inputs — on resume, the human's answer is here, keyed by the
                        interrupt id. (The video calls this "resume data".)

Wrapped in an App with ResumabilityConfig so the pause/resume is durable and
checkpointed.
"""

from google.adk.agents import Agent
from google.adk.agents import Context
from google.adk.apps import App
from google.adk.apps import ResumabilityConfig
from google.adk.events.request_input import RequestInput
from google.adk.workflow import START
from google.adk.workflow import Workflow
from google.adk.workflow import node
from pydantic import BaseModel

MODEL = "gemini-2.5-flash"

AUTO_APPROVE_LIMIT = 100.00
APPROVAL_INTERRUPT_ID = "manager-approval"


class RefundRequest(BaseModel):
    customer_id: str
    amount: float
    reason: str


# --- Step 1: LLM extracts a structured refund request ---------------------

intake = Agent(
    model=MODEL,
    name="intake",
    description="Extracts a structured refund request from the user message.",
    instruction="""Extract the refund request from the user's message.
Return the customer_id, the refund amount as a number, and the reason.
If the reason is not stated, use "not specified".""",
    output_schema=RefundRequest,
)


# --- Step 2: business rule + human-in-the-loop, in plain Python -----------


def _answer_text(value) -> str:
    """Normalizes a resume answer (str, bool, dict, or Content) to text."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("response", "input", "text", "value"):
            if key in value:
                return _answer_text(value[key])
        return str(value)
    parts = getattr(value, "parts", None)
    if parts:
        return " ".join(p.text or "" for p in parts)
    return str(value)


def _is_approval(text: str) -> bool:
    return text.strip().lower() in {"yes", "y", "approve", "approved", "ok", "true"}


@node(rerun_on_resume=True)
def decide_and_process(ctx: Context, node_input: RefundRequest) -> dict:
    """Applies the refund policy; pauses for a manager above the limit."""
    req = node_input

    if req.amount <= AUTO_APPROVE_LIMIT:
        return {
            "decision": "auto_approved",
            "customer_id": req.customer_id,
            "amount": req.amount,
            "reason": req.reason,
            "detail": (
                f"Refund of ${req.amount:.2f} is within the "
                f"${AUTO_APPROVE_LIMIT:.0f} auto-approval limit. "
                "Processed successfully."
            ),
        }

    # Over the limit: has the manager already answered?
    answer = ctx.resume_inputs.get(APPROVAL_INTERRUPT_ID)
    if answer is None:
        # No — pause the workflow and ask a human.
        return RequestInput(
            interrupt_id=APPROVAL_INTERRUPT_ID,
            message=(
                f"⚠️ Manager approval required: refund of ${req.amount:.2f} "
                f"for customer {req.customer_id} "
                f"(reason: {req.reason}) exceeds the "
                f"${AUTO_APPROVE_LIMIT:.0f} auto-approval limit.\n"
                "Do you approve? (yes/no)"
            ),
            response_schema={"type": "string"},
        )

    # Yes — the workflow resumed with the human's answer.
    approved = _is_approval(_answer_text(answer))
    return {
        "decision": "approved" if approved else "rejected",
        "approved_by_human": True,
        "customer_id": req.customer_id,
        "amount": req.amount,
        "reason": req.reason,
        "detail": (
            f"Refund of ${req.amount:.2f} was "
            f"{'APPROVED' if approved else 'REJECTED'} by a manager. "
            + ("Processed successfully." if approved else "No refund issued.")
        ),
    }


# --- The workflow, made durable/resumable via the App wrapper -------------

workflow = Workflow(
    name="refund_workflow",
    edges=[
        (START, intake, decide_and_process),
    ],
)

app = App(
    name="refund_approval",
    root_agent=workflow,
    resumability_config=ResumabilityConfig(is_resumable=True),
)

root_agent = workflow
