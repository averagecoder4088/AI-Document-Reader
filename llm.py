"""
llm.py -- the ONLY file that talks to an LLM provider (Groq).
Everything else calls call_llm(). To switch provider later, change this file only.
"""
import os
import time

from dotenv import load_dotenv
from groq import (APIConnectionError, BadRequestError, Groq, InternalServerError,
                  NotFoundError, RateLimitError)

load_dotenv()  # reads GROQ_API_KEY from a local .env file if one exists (never committed to git)

# Override with --model or the GROQ_MODEL environment variable.
# Groq retires/renames models often: run `python run_agent.py --list-models` to see what your key can use.
DEFAULT_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")


class JSONGenerationError(Exception):
    """The model failed to produce valid JSON (Groq 'json_validate_failed'). Callers may retry."""


_REPLACEMENTS = {
    "\u3010": "[", "\u3011": "]",      # fullwidth brackets -> [ ]
    "\u2011": "-",                      # non-breaking hyphen -> normal hyphen
    "\u202f": " ", "\u00a0": " ",       # narrow / non-breaking spaces -> normal space
}


def clean_text(text):
    """gpt-oss sometimes emits look-alike Unicode (what VS Code highlights in yellow).
    Normalise it so citations are plain [S3]."""
    for bad, good in _REPLACEMENTS.items():
        text = text.replace(bad, good)
    return text


def get_client():
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise SystemExit("GROQ_API_KEY not found. Create a .env file containing: GROQ_API_KEY=your-key  (see .env.example)")
    return Groq(api_key=key)


def list_models():
    return sorted(m.id for m in get_client().models.list().data)


def call_llm(system, user, model, temperature, json_mode=False, max_tokens=3500):
    """One chat call. Returns the model's text.
    - gpt-oss models are 'reasoning' models: they think before answering and that thinking uses up the
      token budget. We cap it (max_tokens) and ask for LOW reasoning effort on JSON tasks, so the
      answer is never starved of tokens (an empty answer was the cause of 'json_validate_failed').
    - Retries only on rate limits / network / server errors. A wrong model name fails loudly."""
    client = get_client()
    extra = {"max_completion_tokens": max_tokens}
    if json_mode:
        extra["response_format"] = {"type": "json_object"}
    if model.startswith("openai/gpt-oss"):
        extra["reasoning_effort"] = "low" if json_mode else "medium"

    for attempt in range(4):
        try:
            r = client.chat.completions.create(
                model=model,
                temperature=temperature,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                **extra,
            )
            print(f"[used model: {model}]")
            return clean_text(r.choices[0].message.content or "")
        except (RateLimitError, APIConnectionError, InternalServerError) as e:
            wait = 15 * 2 ** attempt  # 15, 30, 60, 120 s
            print(f"{type(e).__name__}: retrying in {wait}s")
            time.sleep(wait)
        except BadRequestError as e:
            if "json_validate_failed" in str(e):
                raise JSONGenerationError(str(e)[:200])
            raise SystemExit(f"Request rejected: {e}")
        except NotFoundError as e:
            raise SystemExit(f"Model '{model}' not found: {e}\n"
                             f"Run with --list-models to see valid names, then pass --model NAME.")
    raise RuntimeError("Groq kept failing after 4 attempts")
