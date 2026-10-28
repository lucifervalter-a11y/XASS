"""Private bounded JSON-lines control for the opt-in local microphone worker."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import json
import logging
from pathlib import Path
import sys
import threading

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE if (HERE / "background_voice.py").is_file() else HERE.parent / "pc_client"))
from background_voice import BackgroundVoiceWorker, ListenerError

MAX_REQUEST_BYTES = 16384
MAX_EVENT_BYTES = 4096


class JsonLineWriter:
    def __init__(self, stream):
        self.stream = stream
        self.lock = threading.Lock()

    def __call__(self, event: dict) -> None:
        line = json.dumps(event, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n"
        if len(line) > MAX_EVENT_BYTES:
            raise ListenerError("worker_failed")
        with self.lock:
            self.stream.write(line)
            self.stream.flush()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ListenerError("invalid_request")
        result[key] = value
    return result


def run(input_stream, output_stream, *, worker_factory=BackgroundVoiceWorker) -> None:
    worker = worker_factory(JsonLineWriter(output_stream))
    try:
        # stdin lives on this thread; a blocked model loader/inference never
        # prevents processing pause/stop/EOF. The parent enforces its kill timer.
        while True:
            raw = input_stream.readline(MAX_REQUEST_BYTES + 1)
            if not raw:
                break
            if len(raw) > MAX_REQUEST_BYTES:
                raise ListenerError("invalid_request")
            try:
                request = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
            except (ValueError, UnicodeError, RecursionError):
                raise ListenerError("invalid_request") from None
            worker.handle(request)
            if isinstance(request, dict) and request.get("operation") == "stop":
                break
    except (BrokenPipeError, OSError):
        # No logging fallback: a vanished parent must never leave a microphone.
        try:
            worker.stop()
        except Exception:
            pass
    except Exception as error:
        worker.fail(str(error) if isinstance(error, ListenerError) else "worker_failed")
    finally:
        try:
            worker.stop()
        except Exception:
            # The process is exiting and the parent's timeout is the last guard
            # against a driver stuck inside a native call. Never falsely ack.
            pass


class _QuietOutput:
    """Discard dependency diagnostics in memory; never persist ambient text."""
    def write(self, value):
        return len(value)

    def flush(self):
        pass

    def isatty(self):
        return False


def main() -> None:
    # Save the protocol handle before silencing incidental dependency output.
    output = sys.stdout
    logging.disable(logging.CRITICAL)
    with redirect_stdout(_QuietOutput()), redirect_stderr(_QuietOutput()):
        run(sys.stdin.buffer, output)


if __name__ == "__main__":
    main()
