import json
import secrets
from datetime import datetime
from pathlib import Path

HISTORY_PATH = Path(__file__).parent / ".volto_history.json"
MAX_ENTRIES = 10


def generate_id() -> str:
    return secrets.token_hex(2)  # 4 hex chars


def load() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    try:
        with open(HISTORY_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save(entries: list[dict]) -> None:
    with open(HISTORY_PATH, "w") as f:
        json.dump(entries, f, indent=2)


def append_entry(prompt: str, command: str, environment: str, retry_of: str | None = None, error: str | None = None) -> str:
    """Appends entry, evicts oldest if over limit. Returns the new entry's id."""
    entries = load()
    entry_id = generate_id()
    entries.append({
        "id": entry_id,
        "timestamp": datetime.now().isoformat(),
        "prompt": prompt,
        "command": command,
        "environment": environment,
        "retry_of": retry_of,
        "error": error,
    })
    if len(entries) > MAX_ENTRIES:
        entries = entries[-MAX_ENTRIES:]
    save(entries)
    return entry_id


def get_entry(index: int) -> dict | None:
    """
    Get entry by 1-based recency index over ROOT entries only (retry_of is None).
    index=1 is most recent root, index=10 is oldest root.
    Returns None if index is out of range.
    """
    entries = load()
    roots = [e for e in entries if e.get("retry_of") is None]
    if not roots:
        return None
    if index < 1 or index > len(roots):
        return None
    return roots[-index]


def get_chain(entry: dict) -> list[dict]:
    """
    Walk retry_of pointers from the given entry back to the root.
    Returns list ordered root -> entry. Stops at broken links.
    """
    entries = load()
    by_id = {e["id"]: e for e in entries if "id" in e}

    chain = [entry]
    current = entry
    while current.get("retry_of") is not None:
        parent = by_id.get(current["retry_of"])
        if parent is None:
            break  # broken link — stop here
        chain.append(parent)
        current = parent

    chain.reverse()
    return chain