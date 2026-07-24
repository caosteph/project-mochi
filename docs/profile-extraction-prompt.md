# Profile-extraction prompt (seed Mochi's memory from another agent)

Paste the block below into an AI agent that already knows Stephanie well. It produces a JSON profile
that `scripts/import_profile.py` ingests into Mochi's long-term memory (as `Fact` rows,
`provenance="imported"`, deduped against what's already there).

**Workflow:** run the prompt → save the JSON reply to `data/profile_import.json` (git-ignored) →
`uv run python scripts/import_profile.py` (dry-run preview, writes nothing) → review →
`... --commit` to store.

---

You have gotten to know me well through our work together. I'm setting up a new personal assistant and
want to transfer everything you know about me into its long-term memory, so it starts out already
understanding me instead of learning from scratch.

Produce a comprehensive profile of me as **JSON only** (no prose before or after), in exactly this shape:

```json
{
  "facts": [
    {"text": "<one durable fact about me, third person, self-contained>", "category": "<category>", "confidence": 0.0}
  ],
  "goals": [
    {"text": "<an objective I'm actively working toward>", "target_date": "YYYY-MM-DD or null"}
  ]
}
```

Rules:

1. **Only what you actually know.** Extract facts you've genuinely learned about me — never invent,
   guess, or pad to hit a number. If you're unsure, lower the `confidence`; if you don't know, leave it
   out.
2. **Atomic + self-contained.** One fact per entry, written in the **third person** and understandable
   on its own. Resolve every pronoun and name people/pets explicitly — write
   `"Stephanie's sister Lilian is a senior at Cornell"`, not `"her sister goes there"`. Use my name
   ("Stephanie") for facts about me. Each fact will be stored and retrieved independently, so it must
   make sense with no surrounding context.
3. **Durable only.** Stable traits, relationships, preferences, routines, history, and ongoing context —
   not fleeting state ("busy this week") and not one-off to-dos.
4. **Be exhaustive.** Cover every category you have real information for (skip ones you don't):
   - `identity` — name, age/birthday, where I live, languages I speak
   - `relationships` — family, partner, close friends, pets: names and key details
   - `work` — employer, role, field, current projects, expertise, career direction
   - `education` — schools, degrees, subjects, credentials
   - `health` — conditions, allergies, dietary needs, fitness habits, medications (general — never
     record numbers)
   - `preferences` — food, drink, music, style/aesthetic, brands, hobbies, interests I like
   - `dislikes` — things to avoid, pet peeves, boundaries
   - `routines` — recurring activities, weekly rhythm, standing commitments, schedule patterns
   - `dates` — birthdays, anniversaries, other recurring important dates
   - `places` — where I live/work, spots I frequent, travel
   - `values` — what matters to me, how I think, my temperament and personality
   - `communication` — how I like to be talked to: tone, level of detail, what earns my trust, what
     annoys me in an assistant
   - `context` — what's going on in my life right now that's ongoing (a move, a job search, a trip)
5. **No secrets or credentials.** Never include passwords, full card/account/SSN numbers, or security
   answers — those are liabilities, not useful facts. General financial context is fine
   ("budgets carefully", "has a Chase credit card").
6. **Confidence bands.** `0.9–1.0` = certain; `0.6–0.8` = fairly sure; `< 0.6` = a guess (include only
   if genuinely useful).
7. **Goals** (optional): things I'm actively working toward, with a `target_date` if you know one
   (else `null`).

Aim for breadth and specificity — if you know me well, 40–150 facts is a good target. Concrete beats
vague. Output **only** the JSON object.
