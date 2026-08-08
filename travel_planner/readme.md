### FAQ

* for the travel_planner agent, when I send a query to book a flight for next month                       
  it will ask me which day, then I replied..                                                              
  so how this is working? as I have the travel_planner agent                                              
  so when I send my answer (as which day to flight)                                                       
  how it will return to the previous agent (flight_booker) to continue do this?   


  Turn 1 — "book me a flight from SFO to Paris next month"
  1. travel_planner's LLM runs, decides to delegate, emits a flight_booker(request="...") tool call.
  2. Because flight_booker is mode="task", that "tool call" isn't a normal function call — ADK wraps it as
  _TaskAgentTool, which has _defers_response = True. Its run_async just returns None (confirmed in
  tools/agent_tool.py:423) — a signal meaning "don't synthesize a tool result, dispatch to the real agent
  instead."
  3. flight_booker's own LLM then runs directly, sees it's missing the date, and replies in plain text
  asking for it. This is the final visible event for the turn — no finish_task call yet, so the delegation
  is left open.

  What gets saved: all of this — travel_planner's tool-call event, and flight_booker's question — are
  appended to session.events, tagged with each event's author (travel_planner vs flight_booker).

  Turn 2 — "September 15th"
  This is the part your question is really about. I checked: travel_planner's LLM is not called again to
  "decide" anything.

  4. On every new turn, ADK re-runs the whole agent tree from the root. Before travel_planner's own LLM
  flow runs, its code calls self._get_subagent_to_resume(ctx) (llm_agent.py:853-896) — this is plain
  Python logic, not an LLM call. It reads session.events, finds the last event's author was flight_booker
  (not the user replying to travel_planner directly), and concludes: "we're mid-delegation to
  flight_booker, resume it."
  5. travel_planner._run_async_impl (llm_agent.py:536-545) sees that result and directly calls
  flight_booker.run_async(ctx) — skipping its own LLM entirely for this turn.
  6. flight_booker's LLM now runs with the full session history as context (including its own earlier
  question and your "September 15th" reply), parses the date, calls the real book_flight tool, then calls
  finish_task with a summary. That's what ends the delegation.
  7. Control returns to travel_planner, whose LLM runs one more time to produce the final consolidated
  reply you actually see.

  I verified this via trace spans — turn 2's LLM call order was flight_booker → flight_booker →
  travel_planner, with no travel_planner call before the first flight_booker call, unlike turn 1 which was
  travel_planner → flight_booker.

  To directly answer your question: yes — your reply is stored as an ordinary session message (with the
  sub-agent tracked via each event's author field), and it's that saved history that ADK's framework code
  inspects to deterministically route your answer back to flight_booker, not travel_planner's LLM
  re-deciding where to send it. The routing is a mechanical session-history lookup
  (_get_subagent_to_resume), not a fresh judgment call by the model.

###

⏺ Here's every internal ADK function/method I traced through or referenced while explaining the
  travel_planner → flight_booker multi-turn flow, with what each actually does (all confirmed against the
  installed google-adk 2.5.0 source, not from memory):

  Agent-tree resumption logic — google/adk/agents/llm_agent.py

  Function: LlmAgent._run_async_impl (:529)
  What it does: The per-turn entry point for every LlmAgent. Before running its own LLM, checks whether a
  sub-agent delegation is still open; if so, dispatches straight to that sub-agent and returns — this is 
  the code path that skips travel_planner's LLM on turn 2.
  ────────────────────────────────────────
  Function: LlmAgent._get_subagent_to_resume (:853) 
  What it does: Reads session.events, inspects the last event's author, and decides whether the current
  agent should hand off to a specific sub-agent to continue an in-progress delegation. Pure
  session-history lookup, no LLM call.
  ────────────────────────────────────────
  Function: LlmAgent.__get_transfer_to_agent_or_none (:939)
  What it does: Scans an event for a transfer_to_agent function-response and returns the target agent if
  found. Used by _get_subagent_to_resume to trace the transfer chain.
  ────────────────────────────────────────
  Function: LlmAgent.__get_agent_to_run (:898)
  What it does: Resolves an agent name (string) to the actual agent instance by searching the agent tree
  from root_agent. Raises a descriptive error if the name isn't found.

  Task-mode delegation tool — google/adk/tools/agent_tool.py

  ┌─────────────────────────────────────────┬─────────────────────────────────────────────────────────┐
  │                Function                 │                      What it does                       │
  ├─────────────────────────────────────────┼─────────────────────────────────────────────────────────┤
  │                                         │ Marks this tool as one whose result isn't produced by   │
  │ _TaskAgentTool.__init__ (:423, sets     │ the tool call itself — it's a "delegation marker"       │
  │ _defers_response = True)                │ telling the framework to run the real agent instead of  │
  │                                         │ treating this as a normal function call.                │
  ├─────────────────────────────────────────┼─────────────────────────────────────────────────────────┤
  │                                         │ Intentionally returns None. The actual work happens     │
  │ _TaskAgentTool.run_async                │ elsewhere (the framework dispatch loop) — this method   │
  │                                         │ exists only to satisfy the BaseTool interface.          │
  ├─────────────────────────────────────────┼─────────────────────────────────────────────────────────┤
  │ _SingleTurnAgentTool.run_async (for     │ Actually runs the sub-agent via ctx.run_node(...) and   │
  │ comparison — used by weather_checker)   │ returns its result directly as the tool's output —      │
  │                                         │ single call, no deferred dispatch.                      │
  └─────────────────────────────────────────┴─────────────────────────────────────────────────────────┘

  Function-call execution loop — google/adk/flows/llm_flows/functions.py

  Function: _execute_single_function_call_async (:492)
  What it does: Executes one tool call from the model's response. Contains the check tool.is_long_running 
  or tool._defers_response (:636) — when true and no response was produced, it skips auto-building a
  function-response event, leaving the delegation open for the framework to resume later.

  Session/invocation-state helpers — google/adk/agents/invocation_context.py and base_agent.py
  
  Function: InvocationContext._get_events (:414)
  What it does: Returns events from the session, optionally filtered to the current invocation and/or
  branch. Used by _get_subagent_to_resume to inspect recent history.
  ────────────────────────────────────────          
  Function: InvocationContext._find_matching_function_call (:535) 
  What it does: Given a function-response event, finds the original function-call event it answers — how
  the framework links "the date you replied with" back to the tool call that asked for it.            
  ────────────────────────────────────────
  Function: InvocationContext.should_pause_invocation (:483)
  What it does: Decides whether the whole invocation should pause after a given event (distinct from
  "ending" — a paused invocation can resume later; used for long-running tools and HITL-style flows like
  refund_approval's RequestInput).                  
  ────────────────────────────────────────
  Function: InvocationContext.set_agent_state (:307)
  What it does: Stores or clears a per-agent state marker for the current invocation — e.g. flags whether
  an agent has finished (end_of_agent) or should be resumed.
  ────────────────────────────────────────
  Function: BaseAgent._load_agent_state (:178)
  What it does: Loads a previously saved agent-state object from the invocation context, or None if the
  agent hasn't run before in this session.
  ────────────────────────────────────────
  Function: BaseAgent._create_agent_state_event (:197)
  What it does: Builds an Event carrying the current agent's state/end-of-agent flag, appended to the
  session so the next turn's _load_agent_state can read it back.

  How they chain together on turn 2: _run_async_impl calls _load_agent_state + _get_subagent_to_resume
  (which itself calls _get_events and __get_transfer_to_agent_or_none) to decide "resume flight_booker."
  It dispatches directly, skipping _execute_single_function_call_async's normal LLM-call-then-tool-call
  path entirely for that decision. Once flight_booker finishes and calls finish_task,
  set_agent_state/_create_agent_state_event record that the delegation ended, and control returns to
  travel_planner for its final synthesis pass.

  ### how to work with remote agent 


  What works automatically (no code needed)

  I found the code path in google/adk/flows/llm_flows/agent_transfer.py, _get_transfer_targets (:159-181):

  result.extend([
      sub_agent
      for sub_agent in agent.sub_agents
      if not hasattr(sub_agent, 'mode')
      or sub_agent.mode not in ('single_turn', 'task')
  ])

  RemoteA2aAgent extends BaseAgent directly — confirmed just now, BaseAgent has no mode field at all. That
  means not hasattr(sub_agent, 'mode') is True, so a RemoteA2aAgent placed in sub_agents=[...] is 
  automatically included as a valid transfer_to_agent target — you don't have to register it specially.
  travel_planner's LLM would see it in the transfer-instructions list alongside
  weather_checker/flight_booker and could call transfer_to_agent(agent_name="flight_booker_remote") on its
  own.

  And critically: _get_subagent_to_resume (the function I traced last time, that made turn 2 skip straight
  to flight_booker) is generic — it just scans for a transfer_to_agent function-response event pointing
  at some agent name, and that logic doesn't care whether the target is an LlmAgent or a RemoteA2aAgent.
  So yes, the same "your reply routes straight to the right sub-agent without the coordinator re-deciding"
  behavior would still work for a remote agent reached via transfer_to_agent.

  What's genuinely different (this is where you'd write your own code)

  This is exactly the gap we hit earlier building travel_planner_remote_agent: mode="task" gets you an
  auto-return contract — flight_booker calls finish_task, and the framework hands control back to
  travel_planner for synthesis, guaranteed. transfer_to_agent has no such contract. Once travel_planner
  transfers to the remote agent, that remote agent stays "in control" of the conversation indefinitely —
  every subsequent turn routes straight to it, and travel_planner never gets a turn back unless the remote
  agent itself explicitly calls transfer_to_agent back. That's code you'd have to write on the remote
  agent's side (outside this repo, since it's a separate A2A service) — nothing on the travel_planner side
  can force it.

  So to directly answer "do I have to write my own code":

  Behavior: Deliver the initial request to the remote agent
  Needs custom code?: No — put it in sub_agents=[...], ADK wires transfer_to_agent automatically
  ────────────────────────────────────────
  Behavior: Route your follow-up reply (e.g. "September 15th") to the same remote agent     
  Needs custom code?: No — _get_subagent_to_resume handles it via session history, same as flight_booker
  ────────────────────────────────────────
  Behavior: Get control back to travel_planner afterward, so it can synthesize a combined reply       
  Needs custom code?: Yes — either the remote agent must be built to call transfer_to_agent back to
  travel_planner when it's done, or you sidestep transfer_to_agent entirely and use the               
  travel_planner_remote_agent Workflow-graph pattern we already built, where control return is
  deterministic by graph structure instead of relying on the remote agent's cooperation
  
  That second row is exactly why travel_planner_remote_agent exists as a separate example — it trades the
  convenience of automatic multi-turn routing for a guarantee that synthesis always happens, instead of
  depending on a remote service you don't control to transfer back correctly.
