"""A lightweight structural check on generated single-file sites — the "verify / validate the code"
step Stephanie asked for. Not a full HTML validator or a renderer (a real visual check needs a headless
browser, deferred to the Mac mini); it catches the failure modes that actually make a page look broken:
missing doc, missing the Tailwind/Alpine CDNs it's supposed to use, a truncated <script>, or a stub
that's too small to be the real thing. Pure, so it's unit-testable and runs offline.
"""


def validate_index_html(content: str) -> tuple[bool, str]:
    """Return (ok, note). `note` is a short human-readable reason when something's off, '' when clean."""
    if not content or not content.strip():
        return False, "the page came back empty"
    low = content.lower()

    problems: list[str] = []
    if "<!doctype html" not in low and "<html" not in low:
        problems.append("no HTML document")
    if "cdn.tailwindcss.com" not in low:
        problems.append("Tailwind isn't loaded")
    if "alpinejs" not in low and "alpine.js" not in low:
        problems.append("Alpine isn't loaded")
    if low.count("<script") != low.count("</script>"):
        problems.append("a <script> tag looks unclosed/truncated")
    if len(content) < 800:
        problems.append("the page is suspiciously small")

    if problems:
        return False, "; ".join(problems)
    return True, ""
