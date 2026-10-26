"""Isolated bounded delivery. Only started after explicit send opt-in in the CLI."""

import json
import signal
import sys

from cripto_core.validation import require_object

from cripto_bot.adapters.continuous_config import NotificationInput
from cripto_bot.adapters.notifications.http import LocalSender


def main() -> int:
    def timeout(signum: int, frame: object) -> None:
        raise TimeoutError

    signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, 10)
    try:
        body = sys.stdin.buffer.read(80001)
        if len(body) > 80000:
            raise ValueError
        value = require_object(json.loads(body))
        config = NotificationInput.model_validate(value["settings"])
        outcome = LocalSender(config).send(
            require_object(value["event"]),
            str(value["channel"]),
            duplicate_possible=value.get("duplicate_possible") is True,
        )
    except TimeoutError:
        outcome = "UNKNOWN"
    except Exception:
        outcome = "FAILED"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    print(json.dumps({"outcome": outcome}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
