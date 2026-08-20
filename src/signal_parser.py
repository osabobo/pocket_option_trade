import re
from datetime import datetime, timezone
from .models import Signal, Direction

EXPIRY_RE = re.compile(r"Expires?\s+in\s+(\d+)\s*(second|seconds|sec|s|minute|minutes|min|m)\b", re.I)
TIME_RE = re.compile(r"\b(\d{2}:\d{2}:\d{2})\b")
TZ_RE = re.compile(r"\b(UTC[+-]\d{1,2}(?::\d{2})?)\b", re.I)
MG_RE = re.compile(r"Max\s+(\d+)\s+martingale", re.I)

def parse_signal(text: str, message_id: str | None = None) -> Signal | None:
    upper = text.upper()
    if re.search(r"\b(UP|CALL|HIGH)\b", upper):
        direction = Direction.UP
    elif re.search(r"\b(DOWN|PUT|LOW)\b", upper):
        direction = Direction.DOWN
    else:
        return None

    expiry_match = EXPIRY_RE.search(text)
    if not expiry_match:
        return None

    n = int(expiry_match.group(1))
    unit = expiry_match.group(2).lower()
    expiry_seconds = n * 60 if unit.startswith(("minute", "min", "m")) else n

    asset = None
    for token in re.findall(r"\b[A-Z][A-Z0-9_-]{4,11}\b", upper):
        if token in {"TRADER", "SIGNALS", "EXPIRES", "MINUTE", "MARTINGALE"}:
            continue
        if "OTC" in token or len(token.replace("_", "").replace("-", "")) == 6:
            asset = token
            break
    if not asset:
        return None

    tm = TIME_RE.search(text)
    tz = TZ_RE.search(text)
    mg = MG_RE.search(text)

    return Signal(
        provider=text.splitlines()[0].strip() if text.strip() else None,
        asset=asset,
        direction=direction,
        expiry_seconds=expiry_seconds,
        signal_time=tm.group(1) if tm else None,
        timezone=tz.group(1).upper() if tz else None,
        max_martingale=int(mg.group(1)) if mg else 0,
        telegram_message_id=str(message_id) if message_id is not None else None,
        received_at=datetime.now(timezone.utc),
    )
