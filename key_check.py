"""Shared debug callback: logs which API key is in use right before each LLM
call, so you can confirm .env / the shell env is loading the key you expect.
Wire it into an Agent via before_model_callback=log_api_key.
"""

import logging
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("adk_examples.key_check")


def _mask(key: str) -> str:
    return f"{key[:6]}...{key[-4:]}" if len(key) > 10 else "<missing>"


def log_api_key(callback_context, llm_request):
    env_var = "OPENAI_API_KEY" if "openai" in str(llm_request.model) else "GOOGLE_API_KEY"
    key = os.environ.get(env_var, "")
    logger.info("LLM call -> model=%s %s=%s", llm_request.model, env_var, _mask(key))
