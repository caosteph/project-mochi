"""Shared fuzzy text matching for de-duplication (reminders + email signals).

Two items are "the same thing" if their text is identical after normalization, or they
share a distinguishing content word — a word that isn't a generic filler/action/email
term. So "yoga class" ≡ "go to yoga class", and six "Palantir Offer …" titles collapse to
one, but "submit the form" stays distinct from "submit health insurance claims".
"""

# Generic words that don't distinguish one task/signal from another.
STOP_WORDS = {
    # articles / prepositions / pronouns / aux
    "the", "a", "an", "to", "my", "for", "of", "on", "at", "and", "in", "with", "from", "is",
    "are", "was", "has", "have", "your", "you", "me", "up", "about", "re", "fwd", "it", "this",
    "that", "by", "as", "or",
    # generic actions
    "do", "go", "get", "got", "take", "submit", "prep", "prepare", "respond", "reply", "attend",
    "call", "please", "remind", "reminder", "set", "make", "send",
    # generic email / notification nouns
    "email", "message", "meeting", "offer", "chat", "discussion", "follow", "followup", "update",
    "notification", "alert", "due", "payment", "found", "available", "new", "confirm",
    "confirmation", "order", "package", "seats", "row", "info", "details", "class",
}


def norm_text(text: str) -> str:
    return " ".join((text or "").lower().split())


def content_words(text: str) -> set[str]:
    words = {w.strip(".,:;!?()[]-\"'") for w in norm_text(text).split()}
    return {w for w in words if len(w) > 2 and w not in STOP_WORDS}


def same_thing(a: str, b: str) -> bool:
    """True if a and b refer to the same task/signal (identical, or share a content word)."""
    if norm_text(a) == norm_text(b):
        return True
    return bool(content_words(a) & content_words(b))
