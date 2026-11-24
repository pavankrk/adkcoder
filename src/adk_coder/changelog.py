from datetime import UTC, datetime

from .scrubber import scrub


def record(state, action, detail):
    with (state / "CHANGELOG.md").open("a", encoding="utf8") as stream:
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        stream.write(f"- {stamp} **{action}** {scrub(detail).replace(chr(10), ' ')}\n")
