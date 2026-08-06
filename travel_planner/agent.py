"""Example 2 — Collaborative agent modes (ADK 2.x).

A travel coordinator with two specialist sub-agents:

    travel_planner (coordinator)
      ├── weather_checker   mode="single_turn"  (video calls this "singleton")
      └── flight_booker     mode="task"

- single_turn: the sub-agent runs once, returns its result, and control
  auto-returns to the coordinator. No chatting with the user.
- task: the sub-agent MAY chat with the user to finish its job (e.g. ask
  "what date?"), and control auto-returns the moment the task completes.

In ADK 1.x, once a coordinator transferred to a sub-agent, getting control
back reliably was the hard part — the sub-agent had to remember to transfer
back, and often didn't. In 2.x the mode makes the handoff contract explicit:
ADK wraps the sub-agent as a tool and returns control automatically.
"""

import os

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

from key_check import log_api_key

MODEL = LiteLlm(model="openai/" + os.environ.get("MODEL", "gpt-4o"))

# --- Mock tools (stand-ins for real APIs) ---------------------------------

_MOCK_WEATHER = {
    "paris": {"condition": "partly cloudy", "temp_c": 18, "wind_kph": 12},
    "london": {"condition": "light rain", "temp_c": 14, "wind_kph": 20},
    "tokyo": {"condition": "sunny", "temp_c": 24, "wind_kph": 8},
    "san francisco": {"condition": "foggy", "temp_c": 16, "wind_kph": 15},
}


def get_weather(city: str) -> dict:
    """Returns current weather for a city.

    Args:
        city: City name, e.g. "Paris".
    """
    report = _MOCK_WEATHER.get(
        city.strip().lower(),
        {"condition": "clear", "temp_c": 21, "wind_kph": 10},
    )
    return {"city": city, **report}


def book_flight(origin: str, destination: str, date: str) -> dict:
    """Books a flight and returns the confirmation.

    Args:
        origin: Departure airport code or city, e.g. "SFO".
        destination: Arrival airport code or city, e.g. "CDG".
        date: Travel date, e.g. "2026-04-28".
    """
    return {
        "status": "confirmed",
        "confirmation_code": "AD2K88",
        "origin": origin,
        "destination": destination,
        "date": date,
        "airline": "Gemini Air",
        "price_usd": 642.00,
    }


# --- Sub-agent 1: runs once, auto-returns ---------------------------------

weather_checker = Agent(
    model=MODEL,
    before_model_callback=log_api_key,
    name="weather_checker",
    mode="single_turn",
    description="Checks the current weather for a given city.",
    instruction=(
        "Use the get_weather tool to look up the weather for the requested "
        "city, then return a one-sentence summary of the conditions."
    ),
    tools=[get_weather],
)

# --- Sub-agent 2: may ask clarifying questions, auto-returns when done ----

flight_booker = Agent(
    model=MODEL,
    before_model_callback=log_api_key,
    name="flight_booker",
    mode="task",
    description="Books flights between two cities.",
    instruction=(
        "You book flights. You need an origin, a destination, and a travel "
        "date. If the travel date (or any other required detail) is missing, "
        "ask the user for it. Once you have everything, call book_flight and "
        "report the confirmation code and price, then finish the task."
    ),
    tools=[book_flight],
)

# --- The coordinator ------------------------------------------------------

root_agent = Agent(
    model=MODEL,
    before_model_callback=log_api_key,
    name="travel_planner",
    description="Travel planning coordinator.",
    instruction=(
        "You are a travel coordinator. Delegate weather questions to "
        "weather_checker and flight bookings to flight_booker. For complex "
        "requests (e.g. 'check the weather and book me a flight'), use both "
        "as appropriate, then give the user one consolidated answer."
    ),
    sub_agents=[weather_checker, flight_booker],
)
