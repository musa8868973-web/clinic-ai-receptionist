"""The brain of the bot: one LLM decides between FAQ answers and calendar actions.

Routing logic (how a message flows)
-----------------------------------
 user message
     |
     v
 LLM  (system prompt = rules + today's date + clinic_info.md content)
     |
     |-- question about the clinic ------> answers directly from the .md context   (FAQ)
     |
     |-- wants to see / book a time -----> returns a TOOL CALL
                                              |
                                              v
                                    we run check_available_slots / book_appointment
                                              |
                                              v
                                    result goes back to the LLM -> it writes the reply

The loop repeats until the LLM answers with plain text (no more tool calls).
Works with any OpenAI-compatible API: OpenAI, Groq, LM Studio, Ollama, etc.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from openai import OpenAI, OpenAIError

from src import calendar_agent
from src.faq_handler import FAQHandler

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 5   # safety net: stop endless tool-call loops
MAX_HISTORY = 24      # keep the last N messages so prompts stay small


class BotError(Exception):
    """Raised when the LLM provider cannot be reached or rejects the request."""


# ----------------------------------------------------------------------------- tools
# JSON schemas that tell the LLM which functions exist and how to call them.
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "check_available_slots",
            "description": (
                "Get the free appointment times for one date. Always call this before "
                "suggesting times or booking. Never guess availability."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD format."},
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": (
                "Book an appointment. Only call after the patient has confirmed their "
                "full name, the date and the time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_name": {"type": "string", "description": "Patient's full name."},
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD format."},
                    "time": {"type": "string", "description": "Start time in 24-hour HH:MM format."},
                },
                "required": ["patient_name", "date", "time"],
            },
        },
    },
]

# Maps a tool name from the LLM to the real Python function.
TOOL_REGISTRY = {
    "check_available_slots": calendar_agent.check_available_slots,
    "book_appointment": calendar_agent.book_appointment,
}


def run_tool(name: str, raw_arguments: str) -> str:
    """Execute one tool call and return a JSON string for the LLM. Never raises."""
    func = TOOL_REGISTRY.get(name)
    if func is None:
        return json.dumps({"error": f"Unknown tool '{name}'."})
    try:
        args = json.loads(raw_arguments or "{}")
        result = func(**args)
    except (json.JSONDecodeError, TypeError) as exc:
        result = {"error": f"Bad arguments for {name}: {exc}"}
    except Exception:  # e.g. Google API outage - tell the LLM, don't crash the chat
        logger.exception("Tool %s failed", name)
        result = {"error": "The calendar service is temporarily unavailable. Ask the patient to call the clinic."}
    return json.dumps(result)


# ----------------------------------------------------------------------------- prompt
def build_system_prompt(clinic_context: str) -> str:
    """Rules for the assistant + today's date + the clinic knowledge base."""
    now = calendar_agent.now_in_clinic_tz()
    return f"""You are the friendly virtual receptionist of the clinic described below.

TODAY: {now:%A, %Y-%m-%d}, current time {now:%H:%M} ({now.tzinfo}).
SCHEDULE: {calendar_agent.describe_schedule()}

HOW TO HELP
1. Clinic questions (hours, location, fees, services, doctors, policies):
   answer ONLY from CLINIC INFORMATION below. If the answer is not there, say you
   are not sure and suggest calling the clinic. Never invent facts.
2. Booking: collect the patient's full name, the date and the time.
   - Turn words like "tomorrow" or "next Monday" into YYYY-MM-DD using TODAY.
   - ALWAYS call check_available_slots before offering or confirming a time.
   - Before calling book_appointment, repeat name, date and time and wait for a clear "yes".
   - After booking, confirm the details and mention the arrival tip (10 minutes early).
   - If a booking fails, explain why and offer the free slots that the tool returned.
3. Never give medical advice or diagnoses. For emergencies, tell the person to call
   the emergency line or local emergency number immediately.
4. Style: warm, short (1-4 sentences), plain text. Show times in 12-hour format
   (e.g. 2:30 PM) but send 24-hour HH:MM to tools. Reply in the patient's language.

CLINIC INFORMATION
{clinic_context}
"""


# ----------------------------------------------------------------------------- bot
class ClinicBot:
    """One conversation. Create one instance per user/session."""

    def __init__(
        self,
        faq: FAQHandler | None = None,
        client: OpenAI | None = None,
        model: str | None = None,
    ) -> None:
        self.faq = faq or FAQHandler()
        self.model = model or os.getenv("LLM_MODEL", "gpt-4o-mini")
        self.client = client or self._make_client()
        self.history: list[dict[str, Any]] = []  # everything except the system prompt

    @staticmethod
    def _make_client() -> OpenAI:
        """Build the API client. Set OPENAI_BASE_URL to use Groq, LM Studio, Ollama..."""
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
        if not api_key:
            raise BotError("OPENAI_API_KEY is missing. Add it to your .env file.")
        return OpenAI(api_key=api_key, base_url=base_url, timeout=60.0)

    def reset(self) -> None:
        """Forget the conversation."""
        self.history.clear()

    def _trim_history(self) -> None:
        """Drop the oldest messages, but never start in the middle of a tool exchange."""
        if len(self.history) <= MAX_HISTORY:
            return
        self.history = self.history[-MAX_HISTORY:]
        while self.history and self.history[0]["role"] != "user":
            self.history.pop(0)

    def chat(self, user_message: str) -> str:
        """Send one user message, run any tools the LLM asks for, return the final reply."""
        checkpoint = len(self.history)  # lets us roll back cleanly if the API call fails
        self.history.append({"role": "user", "content": user_message})

        for _ in range(MAX_TOOL_ROUNDS):
            # Rebuilt every turn so the date/time and the .md content are always fresh.
            system = {"role": "system", "content": build_system_prompt(self.faq.get_context(user_message))}
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[system, *self.history],
                    tools=TOOLS,
                    tool_choice="auto",  # LLM decides: answer (FAQ) or call a tool (booking)
                    temperature=0.2,
                )
            except OpenAIError as exc:
                del self.history[checkpoint:]  # drop this unanswered turn
                logger.exception("LLM request failed")
                raise BotError(f"The AI service could not be reached: {exc}") from exc

            message = response.choices[0].message

            # No tool call -> this is the final answer for the user.
            if not message.tool_calls:
                reply = (message.content or "").strip() or "Sorry, I didn't catch that. Could you rephrase?"
                self.history.append({"role": "assistant", "content": reply})
                self._trim_history()
                return reply

            # Tool call(s) -> remember the request, run each tool, feed results back.
            self.history.append(
                {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.function.name, "arguments": call.function.arguments},
                        }
                        for call in message.tool_calls
                    ],
                }
            )
            for call in message.tool_calls:
                logger.info("Tool call: %s(%s)", call.function.name, call.function.arguments)
                self.history.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": run_tool(call.function.name, call.function.arguments),
                    }
                )

        fallback = "I'm having trouble completing that. Please call the clinic and we'll help you right away."
        self.history.append({"role": "assistant", "content": fallback})
        self._trim_history()
        return fallback
