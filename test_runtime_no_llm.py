"""Runtime smoke tests that exercise ADK 2.x workflow mechanics WITHOUT an LLM.

Run:  .venv/bin/python tests/test_runtime_no_llm.py

Test 1 — graph routing: a code-only classifier stands in for the LLM and we
verify each category reaches the right handler from graph_router.
Test 2 — HITL pause/resume: a code-only intake feeds the REAL
decide_and_process node from refund_approval; we verify auto-approval under
$100 and the RequestInput interrupt + resume above $100.
Test 3 — nested workflows (front_desk): an outer router workflow routes into
nested inner workflows; we verify routing reaches a nested handler and that a
HITL interrupt raised two workflow levels deep pauses and resumes correctly.
"""

import asyncio
import sys

sys.path.insert(0, ".")

from google.adk.agents import Context
from google.adk.apps import App, ResumabilityConfig
from google.adk.runners import InMemoryRunner
from google.adk.workflow import START, Workflow, node
from google.adk.workflow.utils._workflow_hitl_utils import (
    create_request_input_response,
)
from google.genai import types

from graph_router.agent import handle_bug, handle_billing, handle_feature_request, router
from refund_approval.agent import RefundRequest, decide_and_process

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
failures = []


def check(label, cond, detail=""):
    print(f"  [{PASS if cond else FAIL}] {label}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(label)


async def run_turn(runner, user_id, session_id, text):
    """Sends one user message and returns the list of events."""
    events = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=text)]),
    ):
        events.append(event)
    return events


def outputs_of(events):
    out = []
    for e in events:
        if getattr(e, "output", None) is not None:
            out.append(e.output)
        elif e.content and e.content.parts:
            out.append(" ".join(p.text or "" for p in e.content.parts if p.text))
    return out


# --------------------------------------------------------------------------
# Test 1: graph routing with a code-only classifier
# --------------------------------------------------------------------------
async def test_graph_routing():
    print("\nTest 1: graph routing (code-only classifier stand-in)")

    for message, expected_handler, expected_snippet in [
        ("BUG", "handle_bug", "Bug report filed"),
        ("BILLING", "handle_billing", "Billing question routed"),
        ("FEATURE", "handle_feature_request", "Feature request logged"),
    ]:

        @node(name="fake_classifier")
        def fake_classifier(node_input) -> str:
            # Stands in for the LLM: echoes the user message as the category.
            parts = getattr(node_input, "parts", None)
            if parts:
                return " ".join(p.text or "" for p in parts if p.text)
            return str(node_input)

        wf = Workflow(
            name="test_router",
            edges=[
                (
                    START,
                    fake_classifier,
                    router,
                    {
                        "BUG": handle_bug,
                        "BILLING": handle_billing,
                        "FEATURE": handle_feature_request,
                    },
                ),
            ],
        )
        runner = InMemoryRunner(agent=wf, app_name="test_router")
        session = await runner.session_service.create_session(
            app_name="test_router", user_id="u1"
        )
        events = await run_turn(runner, "u1", session.id, message)
        text = " ".join(str(o) for o in outputs_of(events))
        check(
            f"'{message}' routes to {expected_handler}",
            expected_snippet in text,
            detail=f"got: {text[:60]}...",
        )


# --------------------------------------------------------------------------
# Test 2: HITL pause/resume with the real decide_and_process node
# --------------------------------------------------------------------------
async def test_hitl():
    print("\nTest 2: refund policy + human-in-the-loop (real decide_and_process node)")

    def make_app(amount):
        @node(name="fake_intake")
        def fake_intake(node_input) -> RefundRequest:
            return RefundRequest(
                customer_id="CUST-002", amount=amount, reason="test refund"
            )

        wf = Workflow(
            name="test_refund", edges=[(START, fake_intake, decide_and_process)]
        )
        return App(
            name="test_refund",
            root_agent=wf,
            resumability_config=ResumabilityConfig(is_resumable=True),
        )

    # --- Case A: $50 auto-approves, no human involved ---
    runner = InMemoryRunner(app=make_app(50.0))
    session = await runner.session_service.create_session(
        app_name="test_refund", user_id="u1"
    )
    events = await run_turn(runner, "u1", session.id, "refund please")
    text = " ".join(str(o) for o in outputs_of(events))
    check("$50 refund auto-approved", "auto_approved" in text, detail=text[:80])

    # --- Case B: $350 pauses with RequestInput, then resumes on 'yes' ---
    # The pause surfaces as a long-running `adk_request_input` function call
    # (this is what adk web renders as a question to the user); the resume is
    # a function_response wrapping the human's reply as {"result": ...}.
    runner = InMemoryRunner(app=make_app(350.0))
    session = await runner.session_service.create_session(
        app_name="test_refund", user_id="u1"
    )
    events = await run_turn(runner, "u1", session.id, "refund please")
    interrupt_id = None
    message = ""
    for e in events:
        if e.content and e.content.parts:
            for p in e.content.parts:
                if p.function_call and p.function_call.name == "adk_request_input":
                    interrupt_id = p.function_call.id
                    message = (p.function_call.args or {}).get("message", "")
    paused = interrupt_id is not None and "Manager approval required" in message
    check(
        "$350 refund pauses for manager approval",
        paused,
        detail=f"interrupt_id={interrupt_id}",
    )

    if paused:
        resume_part = create_request_input_response(interrupt_id, {"result": "yes"})
        events = []
        async for event in runner.run_async(
            user_id="u1",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[resume_part]),
        ):
            events.append(event)
        text = " ".join(str(o) for o in outputs_of(events))
        check(
            "workflow resumes and approves after human says 'yes'",
            "'decision': 'approved'" in text and "'approved_by_human': True" in text,
            detail=text[:100],
        )


# --------------------------------------------------------------------------
# Test 3: nested workflows — front_desk routing + HITL through two levels
# --------------------------------------------------------------------------
async def test_nested():
    print("\nTest 3: nested workflows (real front_desk_router + nested real nodes)")

    from front_desk.agent import front_desk_router

    @node(name="fake_intake_nested")
    def fake_intake_nested(node_input) -> RefundRequest:
        return RefundRequest(customer_id="CUST-9", amount=350.0, reason="nested")

    inner_refund = Workflow(
        name="inner_refund", edges=[(START, fake_intake_nested, decide_and_process)]
    )

    @node(name="fake_support_classifier")
    def fake_support_classifier(node_input) -> str:
        return "BUG"

    inner_support = Workflow(
        name="inner_support",
        edges=[
            (
                START,
                fake_support_classifier,
                router,
                {
                    "BUG": handle_bug,
                    "BILLING": handle_billing,
                    "FEATURE": handle_feature_request,
                },
            ),
        ],
    )

    @node(name="fake_front_classifier")
    def fake_front_classifier(node_input) -> str:
        parts = getattr(node_input, "parts", None)
        if parts:
            return " ".join(p.text or "" for p in parts if p.text)
        return str(node_input)

    outer = Workflow(
        name="outer_front_desk",
        edges=[
            (
                START,
                fake_front_classifier,
                front_desk_router,
                {"SUPPORT": inner_support, "REFUND": inner_refund},
            ),
        ],
    )
    app = App(
        name="outer_front_desk",
        root_agent=outer,
        resumability_config=ResumabilityConfig(is_resumable=True),
    )
    runner = InMemoryRunner(app=app)

    # SUPPORT routes into the nested support workflow, reaching handle_bug.
    session = await runner.session_service.create_session(
        app_name="outer_front_desk", user_id="u1"
    )
    events = await run_turn(runner, "u1", session.id, "SUPPORT")
    text = " ".join(str(o) for o in outputs_of(events))
    check("SUPPORT reaches nested bug handler", "Bug report filed" in text)

    # REFUND routes into the nested refund workflow and pauses for approval.
    session = await runner.session_service.create_session(
        app_name="outer_front_desk", user_id="u1"
    )
    events = await run_turn(runner, "u1", session.id, "REFUND")
    interrupt_id = None
    for e in events:
        if e.content and e.content.parts:
            for p in e.content.parts:
                if p.function_call and p.function_call.name == "adk_request_input":
                    interrupt_id = p.function_call.id
    check(
        "HITL interrupt propagates up two workflow levels",
        interrupt_id is not None,
        detail=f"interrupt_id={interrupt_id}",
    )

    if interrupt_id:
        resume_part = create_request_input_response(interrupt_id, {"result": "yes"})
        events = []
        async for event in runner.run_async(
            user_id="u1",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[resume_part]),
        ):
            events.append(event)
        text = " ".join(str(o) for o in outputs_of(events))
        check(
            "resume reaches the nested node and approves",
            "'decision': 'approved'" in text,
            detail=text[:90],
        )


async def main():
    await test_graph_routing()
    await test_hitl()
    await test_nested()
    print()
    if failures:
        print(f"{len(failures)} test(s) failed: {failures}")
        sys.exit(1)
    print("All runtime tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
