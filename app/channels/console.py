"""Console channel — talk to Mochi from a terminal (demo mode).

Implements the Channel seam (channels/base.py) so the agent runs identically
to the Telegram transport, minus Telegram: no bot token, no phone — type
messages at the prompt. This is the demo-mode transport (docs/demo-mode.md);
production still uses Telegram.

Turns mirror the Telegram flow: stream the graph, surface an approval
interrupt as a prompt (Approve/Reject), resume with Command, log the turn, and
run the post-turn fact sweep. Proactive work is driven by slash commands
instead of the JobQueue:

  /scan            run the email-signal scan now, print any approval asks
  /approve <id>    approve a detected signal (creates its reminder)
  /reject <id>     dismiss a detected signal
  /tick            fire any due reminders now
  /briefing        print the daily briefing
  /drafts          list drafts created in this demo session
  /events          list demo calendar events for the next 7 days
  /help            list commands
  /quit            exit

Run it with: `uv run python -m app.main` and DEMO_MODE=true (or `docker compose up`).
"""

import logging
from datetime import UTC, datetime

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command
from sqlmodel import Session

from app.agent.graph import build_agent
from app.channels.base import Channel
from app.channels.render import render_choice, render_proposal
from app.config import settings
from app.integrations import demo as demo_provider
from app.integrations import google_calendar
from app.memory import store
from app.memory.db import get_engine
from app.memory.models import EmailSignal, SignalStatus
from app.proactive import briefing, jobs, reminders

log = logging.getLogger(__name__)

_THREAD_ID = "demo-console"


class _PrintBot:
    """Stand-in for the Telegram Bot: jobs only need `send_message`, and here
    it prints. Reply markup (buttons) is ignored — the console answers asks
    with /approve and /reject instead."""

    async def send_message(self, chat_id=None, text=None, reply_markup=None, **kw):  # noqa: ARG002
        # Signature mirrors telegram.Bot.send_message (callers pass chat_id and
        # reply_markup as keywords); only the text is printed here.
        print(f"\n[Mochi] {text}")


class ConsoleChannel(Channel):
    def __init__(self) -> None:
        self.agent = build_agent()
        self.chat_id = settings.telegram_chat_id
        self._bot = _PrintBot()

    def _config(self) -> dict:
        return {"configurable": {"thread_id": _THREAD_ID}}

    # --- one agent turn -----------------------------------------------------

    def _stream_turn(self, graph_input) -> tuple[dict | None, str | None]:
        """Stream one graph run; return (interrupt_payload, reply text)."""
        interrupt_payload = None
        reply = None
        try:
            for update in self.agent.stream(graph_input, self._config(), stream_mode="updates"):
                if "__interrupt__" in update:
                    interrupt_payload = update["__interrupt__"][0].value
                    continue
                for node, payload in update.items():
                    if node not in ("agent", "repair"):
                        continue
                    msgs = (payload or {}).get("messages") or []
                    if not msgs:
                        continue
                    msg = msgs[-1]
                    if (
                        isinstance(msg, AIMessage)
                        and isinstance(msg.content, str)
                        and msg.content.strip()
                        and not getattr(msg, "tool_calls", None)
                    ):
                        reply = msg.content
        except Exception:
            log.exception("console turn failed")
            return None, "⚠️ Something went wrong on my end — mind trying again?"
        return interrupt_payload, reply

    def _handle_interrupt(self, payload: dict) -> str | None:
        """Print an interrupt proposal and ask. Returns the resumed reply text."""
        if payload.get("type") == "choice":
            options = payload.get("options") or []
            print(f"\n[Mochi] {render_choice(payload.get('question', ''))}")
            for i, opt in enumerate(options):
                print(f"  [{i}] {opt}")
            try:
                choice = int(input("Pick one: ").strip())
            except (ValueError, EOFError):
                choice = -1
            resume = {"choice": choice}
        else:
            print(f"\n[Mochi] {render_proposal(payload.get('action', ''), payload.get('details', {}))}")
            try:
                answer = input("Approve? [y/n]: ").strip().lower()
            except EOFError:
                answer = "n"
            resume = {"approved": answer in ("y", "yes")}
        _, reply = self._stream_turn(Command(resume=resume))
        return reply

    def _turn(self, text: str) -> None:
        interrupt_payload, reply = self._stream_turn({"messages": [HumanMessage(text)]})
        if interrupt_payload is not None:
            reply = self._handle_interrupt(interrupt_payload)
        print(f"\n[Mochi] {reply or 'Done.'}")
        self._log_turn(text, reply)
        if settings.fact_sweep_enabled:
            self._fact_sweep(text)

    def _log_turn(self, user_text: str, reply: str | None) -> None:
        try:
            with Session(get_engine()) as session:
                store.log_message(session, chat_id=self.chat_id, role="user", text=user_text)
                if reply:
                    store.log_message(session, chat_id=self.chat_id, role="assistant", text=reply)
        except Exception:
            log.exception("console turn logging failed; ignoring")

    def _fact_sweep(self, text: str) -> None:
        """Sync version of TelegramChannel._fact_sweep: extract durable facts the
        user stated and store the new ones. Error-isolated — never affects the turn."""
        from app.agent.graph import fact_extractor
        from app.memory import extract

        try:
            with Session(get_engine()) as session:
                stored = extract.sweep_and_store(session, text, extractor=fact_extractor())
            if stored:
                log.info("fact sweep stored %d new fact(s)", len(stored))
        except Exception:
            log.exception("fact sweep failed; ignoring")

    # --- slash commands -----------------------------------------------------

    def _cmd_scan(self) -> None:
        import asyncio

        print("\nScanning the demo inbox…")
        with Session(get_engine()) as session:
            asyncio.run(jobs.run_signal_ingest_tick(self._bot, session, self.chat_id))
        print("Scan done. Approve a signal with /approve <id>, or dismiss with /reject <id>.")

    def _cmd_approve(self, arg: str) -> None:
        try:
            signal_id = int(arg)
        except ValueError:
            print("Usage: /approve <signal id>")
            return
        with Session(get_engine()) as session:
            signal = session.get(EmailSignal, signal_id)
            if signal is None:
                print("No signal with that id.")
                return
            reminder = reminders.create_from_signal(session, signal)
            print(
                f"✅ I'll remind you — {reminder.text} "
                f"({reminder.due_at.astimezone():%a %b %-d, %-I:%M %p})."
            )

    def _cmd_reject(self, arg: str) -> None:
        try:
            signal_id = int(arg)
        except ValueError:
            print("Usage: /reject <signal id>")
            return
        with Session(get_engine()) as session:
            signal = session.get(EmailSignal, signal_id)
            if signal is None:
                print("No signal with that id.")
                return
            signal.status = SignalStatus.DISMISSED.value
            session.add(signal)
            session.commit()
            print("👍 Skipped — I won't set that one.")

    def _cmd_tick(self) -> None:
        import asyncio

        with Session(get_engine()) as session:
            sent = asyncio.run(jobs.run_reminder_tick(self._bot, session, self.chat_id))
        print(f"Tick done — {sent} reminder(s) fired." if sent else "Tick done — nothing due.")

    def _cmd_briefing(self) -> None:
        with Session(get_engine()) as session:
            print(f"\n{briefing.build_briefing(session, now=datetime.now(UTC))}")

    def _cmd_drafts(self) -> None:
        drafts = demo_provider.list_drafts()
        if not drafts:
            print("No drafts created yet in this demo session.")
            return
        for d in drafts:
            print(f"- {d['id']}: to {d['to']} — {d['subject']}")

    def _cmd_events(self) -> None:
        events = google_calendar.list_events()
        if not events:
            print("Nothing on the demo calendar for the next 7 days.")
            return
        for e in events:
            print(google_calendar.format_event(e))

    def _cmd_help(self) -> None:
        print(
            "\nCommands:\n"
            "  /scan            scan the demo inbox for returns/bills/appointments\n"
            "  /approve <id>    approve a detected signal (creates its reminder)\n"
            "  /reject <id>     dismiss a detected signal\n"
            "  /tick            fire any due reminders now\n"
            "  /briefing        print the daily briefing\n"
            "  /drafts          list drafts created in this demo session\n"
            "  /events          list demo calendar events for the next 7 days\n"
            "  /help            this list\n"
            "  /quit            exit\n"
            "\nOr just type a message to chat with Mochi."
        )

    def _dispatch(self, text: str) -> bool:
        """Handle a slash command. Returns True if the line was a command."""
        if not text.startswith("/"):
            return False
        parts = text[1:].split(None, 1)
        cmd, arg = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
        if cmd == "quit":
            raise EOFError
        if cmd == "help":
            self._cmd_help()
        elif cmd == "scan":
            self._cmd_scan()
        elif cmd == "approve":
            self._cmd_approve(arg)
        elif cmd == "reject":
            self._cmd_reject(arg)
        elif cmd == "tick":
            self._cmd_tick()
        elif cmd == "briefing":
            self._cmd_briefing()
        elif cmd == "drafts":
            self._cmd_drafts()
        elif cmd == "events":
            self._cmd_events()
        else:
            print(f"Unknown command /{cmd} — try /help.")
        return True

    # --- main loop ----------------------------------------------------------

    def run(self) -> None:
        print(
            "Mochi (demo mode) — a local-first personal agent running on fictional data.\n"
            "Type a message to chat, or /help for demo commands. /quit to exit.\n"
            "Try: \"what's on my calendar this week?\" or \"what did the security email say?\""
        )
        while True:
            try:
                text = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nBye!")
                return
            if not text:
                continue
            try:
                if self._dispatch(text):
                    continue
            except EOFError:
                print("\nBye!")
                return
            self._turn(text)
