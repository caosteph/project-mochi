"""Calibrate memory consolidation's grouping threshold on Stephanie's REAL facts — read-only.

Consolidation (docs/14 #1(c), step C) will only ask the local model about pairs of facts whose
embeddings are at least THRESHOLD similar; every pair above it costs one model call, every related
pair below it is a duplicate or contradiction that never gets fixed. The threshold can't be guessed:
a contradiction like "lives in Queens" / "moved to Brooklyn" shares no words and may not embed close
at all. So this measures it on her own memory:

  1. loads every current fact + its stored embedding, computes all pairwise cosine similarities,
     and prints the distribution and the most similar pairs
  2. `--label N`: shows N pairs sampled ACROSS similarity bands (not just the top — the low bands
     are exactly where wordless contradictions would hide) and asks, per pair, whether they're about
     the same thing (one duplicates or updates the other). Answers persist in
     data/consolidation_labels.json, so labelling can be spread over sessions
  3. for each candidate threshold: how many related pairs it catches, and how many model calls a
     run would cost — then recommends the HIGHEST threshold that still reaches --target-recall
     (fewest model calls for that recall), flagging it if it exceeds --max-candidates

READ-ONLY BY CONSTRUCTION: the connection is opened with `default_transaction_read_only=on`, so
Postgres itself rejects any write — this runs against the live DB (it has to: the point is her real
facts), and a bug here must not be able to touch it. Nothing leaves the machine; labels stay in data/.

    uv run python scripts/calibrate_consolidation.py                # report only
    uv run python scripts/calibrate_consolidation.py --label 40     # label 40 more pairs, then report
"""

import argparse
import json
import random
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import psycopg

ROOT = Path(__file__).resolve().parent.parent
LABELS_PATH = ROOT / "data" / "consolidation_labels.json"
BANDS = [(0.9, 1.01), (0.8, 0.9), (0.7, 0.8), (0.6, 0.7), (0.5, 0.6), (-1.0, 0.5)]
# Labelling effort per band, highest first: related pairs are rarer as similarity drops, but the
# lower bands still get real weight because that's where a wordless contradiction would be.
BAND_WEIGHTS = [0.25, 0.2, 0.2, 0.15, 0.1, 0.1]
THRESHOLDS = [round(t, 2) for t in np.arange(0.40, 0.96, 0.05)]
_SNIP = 110


@dataclass(frozen=True)
class FactRow:
    id: int
    text: str
    pinned: bool


@dataclass(frozen=True)
class Pair:
    a: FactRow
    b: FactRow
    sim: float

    @property
    def key(self) -> str:
        return pair_key(self.a.id, self.b.id)


def pair_key(x: int, y: int) -> str:
    return f"{min(x, y)}-{max(x, y)}"


# --- reading (Postgres-enforced read-only) ---------------------------------------------


def connect_read_only(url: str) -> psycopg.Connection:
    url = url.replace("postgresql+psycopg://", "postgresql://", 1)
    return psycopg.connect(url, options="-c default_transaction_read_only=on")


def load_facts(conn: psycopg.Connection) -> tuple[list[FactRow], np.ndarray]:
    """Current facts and their embeddings (rows L2-normalised). Works before and after the
    supersession columns exist (step A's migration runs on the bot's next start)."""
    has_col = conn.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = 'fact' AND column_name = 'superseded_by'"
    ).fetchone()
    where = "WHERE superseded_by IS NULL AND embedding IS NOT NULL" if has_col else "WHERE embedding IS NOT NULL"
    rows = conn.execute(f"SELECT id, text, pinned, embedding::text FROM fact {where} ORDER BY id").fetchall()
    facts = [FactRow(r[0], r[1], bool(r[2])) for r in rows]
    if not rows:
        return facts, np.zeros((0, 0))
    vecs = np.array([json.loads(r[3]) for r in rows], dtype=np.float64)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return facts, vecs / np.where(norms == 0, 1, norms)


# --- pure analysis (unit-tested) -------------------------------------------------------------


def all_pairs(facts: list[FactRow], unit_vecs: np.ndarray) -> list[Pair]:
    """Every unordered pair with its cosine similarity, most similar first."""
    sims = unit_vecs @ unit_vecs.T if len(facts) else np.zeros((0, 0))
    out = [Pair(facts[i], facts[j], float(sims[i, j]))
           for i in range(len(facts)) for j in range(i + 1, len(facts))]
    return sorted(out, key=lambda p: p.sim, reverse=True)


def histogram(pairs: list[Pair]) -> list[tuple[str, int]]:
    return [(f"{lo:.1f}–{min(hi, 1.0):.1f}" if lo >= 0 else f"<{hi:.1f}",
             sum(lo <= p.sim < hi for p in pairs)) for lo, hi in BANDS]


def stratified_sample(pairs: list[Pair], n: int, labelled: set[str], *, seed: int = 0) -> list[Pair]:
    """Up to n not-yet-labelled pairs, spread across BANDS by BAND_WEIGHTS (a band with too few
    pairs donates its share to the others). Deterministic for a given seed and label set."""
    rng = random.Random(seed)
    pools = [[p for p in pairs if lo <= p.sim < hi and p.key not in labelled] for lo, hi in BANDS]
    for pool in pools:
        rng.shuffle(pool)
    quota = [round(n * w) for w in BAND_WEIGHTS]
    picked: list[Pair] = []
    for pool, q in zip(pools, quota, strict=True):
        picked += pool[:q]
        del pool[:q]
    leftovers = [p for pool in pools for p in pool]
    picked += leftovers[: max(0, n - len(picked))]
    return sorted(picked[:n], key=lambda p: p.sim, reverse=True)


@dataclass(frozen=True)
class ThresholdRow:
    threshold: float
    candidates: int          # pairs at/above the threshold = model calls per full run
    related_caught: int      # labelled-related pairs at/above it
    related_total: int

    @property
    def recall(self) -> float | None:
        return self.related_caught / self.related_total if self.related_total else None


def threshold_table(pairs: list[Pair], labels: dict[str, bool]) -> list[ThresholdRow]:
    sim_by_key = {p.key: p.sim for p in pairs}
    related = [sim_by_key[k] for k, v in labels.items() if v and k in sim_by_key]
    return [ThresholdRow(t, sum(p.sim >= t for p in pairs), sum(s >= t for s in related), len(related))
            for t in THRESHOLDS]


def recommend(rows: list[ThresholdRow], *, target_recall: float, max_candidates: int,
              min_related: int = 5) -> tuple[ThresholdRow | None, str]:
    """The HIGHEST threshold that still reaches target_recall (fewest model calls for that recall),
    if it fits the call budget. Refuses to recommend on too few related labels — a recall measured
    over 2 pairs is noise."""
    total = rows[0].related_total if rows else 0
    if total < min_related:
        return None, f"only {total} pair(s) labelled related — label more (need ≥ {min_related}) before trusting a threshold"
    meeting = [r for r in rows if r.recall is not None and r.recall >= target_recall]
    if not meeting:
        return None, f"no threshold reaches {target_recall:.0%} recall — related facts embed far apart; similarity alone can't group them"
    best = max(meeting, key=lambda r: r.threshold)
    if best.candidates > max_candidates:
        return best, (f"{best.threshold:.2f} reaches {best.recall:.0%} recall but costs {best.candidates} model calls "
                      f"per run (budget {max_candidates}) — raise the budget or accept lower recall")
    return best, f"use {best.threshold:.2f}: catches {best.related_caught}/{best.related_total} related pairs with {best.candidates} model calls per run"


# --- labels + interaction --------------------------------------------------------------------


def load_labels(path: Path) -> dict[str, bool]:
    try:
        return {k: bool(v) for k, v in json.loads(path.read_text()).items()}
    except (OSError, ValueError):
        return {}


def save_labels(path: Path, labels: dict[str, bool]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(labels, indent=1, sort_keys=True))


def _snip(text: str) -> str:
    return text if len(text) <= _SNIP else text[: _SNIP - 1] + "…"


def _show(p: Pair) -> str:
    pin = lambda f: "📌 " if f.pinned else ""  # noqa: E731
    return f"  [{p.sim:.2f}]  A: {pin(p.a)}{_snip(p.a.text)}\n          B: {pin(p.b)}{_snip(p.b.text)}"


def label_interactively(sample: list[Pair], labels: dict[str, bool], path: Path, ask=None) -> int:
    ask = ask or input  # resolved per call, not bound at definition time
    print("\nFor each pair: are A and B about the SAME thing — one duplicates, updates, or contradicts the other?")
    print("  y = yes, same thing   n = no, unrelated   s = skip   q = quit (answers so far are saved)\n")
    done = 0
    for i, p in enumerate(sample, 1):
        print(f"{i}/{len(sample)}\n{_show(p)}")
        while (ans := ask("  same thing? [y/n/s/q] ").strip().lower()[:1]) not in ("y", "n", "s", "q"):
            pass
        if ans == "q":
            break
        if ans in ("y", "n"):
            labels[p.key] = ans == "y"
            save_labels(path, labels)  # after every answer: quitting or a crash loses nothing
            done += 1
    return done


def report(pairs: list[Pair], labels: dict[str, bool], *, top: int, target_recall: float, max_candidates: int) -> None:
    print(f"\n=== {len(pairs)} fact pairs ===\nsimilarity distribution:")
    for band, count in histogram(pairs):
        print(f"  {band:>9}  {count:6d}  {'█' * min(60, count // max(1, len(pairs) // 300))}")
    print(f"\nmost similar {top} pairs (📌 = pinned; consolidation never auto-changes these):")
    for p in pairs[:top]:
        flag = {True: " ← you said: same", False: " ← you said: unrelated"}.get(labels.get(p.key), "")
        print(_show(p) + flag)
    rows = threshold_table(pairs, labels)
    n_rel, n_unrel = sum(labels.values()), sum(not v for v in labels.values())
    print(f"\nthresholds (from your {len(labels)} labels: {n_rel} same, {n_unrel} unrelated):")
    print("  threshold  model-calls/run  related caught")
    for r in rows:
        rec = "   -" if r.recall is None else f"{r.related_caught}/{r.related_total} ({r.recall:.0%})"
        print(f"     {r.threshold:.2f}        {r.candidates:6d}       {rec}")
    _best, why = recommend(rows, target_recall=target_recall, max_candidates=max_candidates)
    print(f"\nrecommendation: {why}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Calibrate consolidation's grouping threshold (read-only).")
    ap.add_argument("--label", type=int, default=0, metavar="N", help="label N more pairs before reporting")
    ap.add_argument("--top", type=int, default=15, help="how many of the most similar pairs to show")
    ap.add_argument("--target-recall", type=float, default=0.9)
    ap.add_argument("--max-candidates", type=int, default=200, help="model-call budget per consolidation run")
    ap.add_argument("--labels", type=Path, default=LABELS_PATH)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)

    from scripts._verify_lib import bootstrap_env

    bootstrap_env()
    from app.config import settings

    with connect_read_only(settings.database_url) as conn:
        facts, vecs = load_facts(conn)
    if len(facts) < 2:
        print(f"Only {len(facts)} current fact(s) — nothing to calibrate.")
        return 0
    pairs = all_pairs(facts, vecs)
    labels = load_labels(args.labels)
    if args.label:
        sample = stratified_sample(pairs, args.label, set(labels), seed=args.seed + len(labels))
        n = label_interactively(sample, labels, args.labels)
        print(f"\nlabelled {n} pair(s); {len(labels)} total saved to {args.labels}")
    report(pairs, labels, top=args.top, target_recall=args.target_recall, max_candidates=args.max_candidates)
    return 0


if __name__ == "__main__":
    sys.exit(main())
