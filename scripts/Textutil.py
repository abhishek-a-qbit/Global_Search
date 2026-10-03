import re

ACRONYMS = {"crm", "cpq", "seo", "sms", "pos", "dam", "lms", "ai"}   # tweak as needed


def norm(s: str) -> str:
    """Lowercase, strip punctuation, collapse spaces."""
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def pretty(slug: str) -> str:
    """'6sense-revenue-ai-for-sales' -> '6sense Revenue AI For Sales'."""
    words = re.sub(r"-+", " ", slug).split()
    return " ".join(w.upper() if w in ACRONYMS else w[0].upper() + w[1:] for w in words)