"""Identity of the recap format permitted for current presentation."""

RECAP_HASH_PREFIX = "rx8:"


def current_recap_hash(value: str | None) -> bool:
    return bool(value and value.startswith(RECAP_HASH_PREFIX))
