"""Command-line chat with the Clinic AI Receptionist.

Run:  python main.py
Commands inside the chat:  /reset  start over   |   /quit  exit
"""

from __future__ import annotations

import logging
import sys

from dotenv import load_dotenv

load_dotenv()  # must run before importing src.* so settings are available

from src import calendar_agent  # noqa: E402
from src.bot_engine import BotError, ClinicBot  # noqa: E402

CYAN, GREEN, DIM, RESET = "\033[36m", "\033[32m", "\033[2m", "\033[0m"


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    try:
        bot = ClinicBot()
    except (BotError, FileNotFoundError) as exc:
        print(f"Setup problem: {exc}")
        return 1

    mode = "DEMO calendar (local file)" if calendar_agent.is_mock() else "Google Calendar"
    print(f"\n{GREEN}Green Valley Family Clinic - AI Receptionist{RESET}")
    print(f"{DIM}Model: {bot.model}  |  Calendar: {mode}")
    print(f"Type /reset to start over, /quit to exit.{RESET}\n")
    print(f"{CYAN}Bot:{RESET} Hello! I can answer questions about the clinic or book an appointment. How can I help?\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            return 0

        if not user_input:
            continue
        if user_input.lower() in {"/quit", "/exit", "quit", "exit"}:
            print("Goodbye!")
            return 0
        if user_input.lower() == "/reset":
            bot.reset()
            print(f"{DIM}Conversation cleared.{RESET}\n")
            continue

        try:
            reply = bot.chat(user_input)
        except BotError as exc:
            print(f"{DIM}[error] {exc}{RESET}\n")
            continue
        print(f"\n{CYAN}Bot:{RESET} {reply}\n")


if __name__ == "__main__":
    sys.exit(main())
