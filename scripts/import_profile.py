"""Seed Mochi's long-term memory from a profile exported by another agent.

Reads a JSON profile (see docs/profile-extraction-prompt.md for the exact shape and the prompt that
produces it) and stores each durable fact as a `Fact` row with `provenance="imported"`, deduped against
what Mochi already knows. Optional `goals` become active `Goal` rows (which surface in the daily
briefing).

Everything is local: embeddings run on Ollama (localhost), nothing leaves the machine, and the input
file lives in git-ignored data/.

Safe by default — with no flag this is a DRY RUN: it prints a full preview + a by-category summary and
writes nothing. Pass --commit to actually store.

    uv run python scripts/import_profile.py                     # dry-run preview of data/profile_import.json
    uv run python scripts/import_profile.py --input p.json      # a different file
    uv run python scripts/import_profile.py --commit            # actually store
"""

import argparse
import json
import sys
from datetime import UTC, datetime

from sqlmodel import Session, select
from tzlocal import get_localzone

from app.config import settings
from app.memory import store
from app.memory.db import get_engine
from app.memory.models import Goal, GoalStatus, Provenance
from app.proactive import text_match

DEFAULT_INPUT = "data/profile_import.json"


def _extract_json(raw: str) -> dict:
    """Parse the profile object, tolerating a Markdown ```json fence or stray prose around it —
    LLMs often wrap JSON that way. Falls back to slicing from the first '{' to the last '}'."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found in the input file")
    return json.loads(raw[start : end + 1])


def _clamp_confidence(value) -> float:
    try:
        c = float(value)
    except (TypeError, ValueError):
        return 0.8  # sensible default when the source omits/garbles it
    return max(0.0, min(1.0, c))


def _parse_target_date(value) -> datetime | None:
    """'YYYY-MM-DD' (or any ISO date/datetime) → tz-aware UTC; None/'' → None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=get_localzone())
    return dt.astimezone(UTC)


def _norm_facts(profile: dict) -> list[dict]:
    """Normalize the facts array into {text, category, confidence}, dropping empties."""
    out = []
    for raw in profile.get("facts", []) or []:
        if isinstance(raw, str):
            raw = {"text": raw}
        text = (raw.get("text") or "").strip()
        if not text:
            continue
        out.append({
            "text": text[:300],  # match the sweep's per-fact bound
            "category": (raw.get("category") or "uncategorized").strip().lower(),
            "confidence": _clamp_confidence(raw.get("confidence", 0.8)),
        })
    return out


def _norm_goals(profile: dict) -> list[dict]:
    out = []
    for raw in profile.get("goals", []) or []:
        if isinstance(raw, str):
            raw = {"text": raw}
        text = (raw.get("text") or "").strip()
        if not text:
            continue
        out.append({"text": text[:300], "target_date": _parse_target_date(raw.get("target_date"))})
    return out


def _dup_of(session: Session, text: str) -> str | None:
    """The text of an existing near-duplicate fact (>= the same dedup bar the post-turn sweep uses),
    or None. Reuses the real hybrid recall so imports and organic capture stay consistent."""
    hits = store.recall(session, query=text, k=1)
    if hits and hits[0].similarity >= settings.fact_dedup_similarity:
        return hits[0].fact.text
    return None


def _goal_exists(session: Session, text: str) -> bool:
    existing = session.exec(select(Goal).where(Goal.status == GoalStatus.ACTIVE.value)).all()
    return any(text_match.same_thing(text, g.text) for g in existing)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default=DEFAULT_INPUT, help=f"profile JSON path (default: {DEFAULT_INPUT})")
    ap.add_argument("--commit", action="store_true", help="actually store (default is a dry-run preview)")
    ap.add_argument("--dry-run", action="store_true", help="explicit dry run (the default; writes nothing)")
    args = ap.parse_args()
    commit = args.commit and not args.dry_run

    try:
        with open(args.input, encoding="utf-8") as f:
            profile = _extract_json(f.read())
    except FileNotFoundError:
        print(f"! no file at {args.input!r}. Save your agent's JSON reply there first "
              f"(see docs/profile-extraction-prompt.md).")
        return 1
    except ValueError as e:
        print(f"! couldn't parse {args.input!r}: {e}")
        return 1

    facts = _norm_facts(profile)
    goals = _norm_goals(profile)
    if not facts and not goals:
        print(f"! {args.input!r} parsed but had no facts or goals.")
        return 1

    mode = "COMMIT — storing" if commit else "DRY RUN — nothing will be written"
    print(f"=== import_profile [{mode}] ===")
    print(f"parsed: {len(facts)} fact(s), {len(goals)} goal(s) from {args.input!r}\n")

    stored = deduped = 0
    by_cat_store: dict[str, int] = {}
    engine = get_engine()
    with Session(engine) as session:
        print("facts:")
        for fct in facts:
            dup = _dup_of(session, fct["text"])
            if dup is not None:
                deduped += 1
                print(f"  =  [dup] {fct['text']!r}  (~ already know: {dup!r})")
                continue
            by_cat_store[fct["category"]] = by_cat_store.get(fct["category"], 0) + 1
            stored += 1
            marker = "+ " if commit else "+ [would] "
            print(f"  {marker}({fct['category']}, conf={fct['confidence']}) {fct['text']!r}")
            if commit:
                store.remember_fact(session, text=fct["text"], confidence=fct["confidence"],
                                    provenance=Provenance.IMPORTED.value)

        goal_stored = goal_dup = 0
        if goals:
            print("\ngoals:")
            for g in goals:
                if _goal_exists(session, g["text"]):
                    goal_dup += 1
                    print(f"  =  [dup] {g['text']!r}")
                    continue
                goal_stored += 1
                when = f" (target {g['target_date'].date()})" if g["target_date"] else ""
                print(f"  {'+ ' if commit else '+ [would] '}{g['text']!r}{when}")
                if commit:
                    store.add_goal(session, text=g["text"], target_date=g["target_date"])

    verb = "stored" if commit else "would store"
    print("\n=== summary ===")
    print(f"facts: {stored} {verb}, {deduped} deduped (of {len(facts)})")
    if by_cat_store:
        print("  by category: " + ", ".join(f"{c}={n}" for c, n in sorted(by_cat_store.items())))
    if goals:
        print(f"goals: {goal_stored} {verb}, {goal_dup} deduped (of {len(goals)})")
    if not commit:
        print("\n(dry run — re-run with --commit to store)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
