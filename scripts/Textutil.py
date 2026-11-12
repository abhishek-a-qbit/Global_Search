import re

ACRONYMS = {"crm", "cpq", "seo", "sms", "pos", "dam", "lms", "ai", "b2b", "b2c"}   # tweak as needed
CONNECTORS = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "nor", "of", "on", "or", "per", "the", "to", "vs", "via", "with"}


def norm(s: str) -> str:
    """Lowercase, strip punctuation, collapse spaces."""
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def pretty(slug: str) -> str:
    """'6sense-revenue-ai-for-sales' -> '6sense Revenue AI for Sales'."""
    words = re.sub(r"-+", " ", slug).split()
    return " ".join(w.upper() if w in ACRONYMS else w.lower() if w in CONNECTORS and i else
                    w[0].upper() + w[1:] for i, w in enumerate(words))