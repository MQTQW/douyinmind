"""Pure media time formatting; no recognition, transport or model SDK."""


def timestamp(seconds: float) -> str:
    value = max(0, int(seconds))
    hours, remaining = divmod(value, 3600)
    minutes, secs = divmod(remaining, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"
