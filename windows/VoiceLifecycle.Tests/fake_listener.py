"""Synthetic protocol fixture: never opens a microphone or imports an STT model."""
import json
import sys
import time

request = json.loads(sys.stdin.readline())
mode = request["model_path"]
def emit(value):
    print(json.dumps(value, ensure_ascii=True), flush=True)
if mode == "bad_json":
    print("{invalid", flush=True)
elif mode == "long_line":
    print("x" * 9000, flush=True)
elif mode == "error":
    emit({"type": "error", "message": "PRIVATE DIAGNOSTIC MUST NOT ESCAPE"})
else:
    emit({"type": "state", "state": "ready"})
    for line in sys.stdin:
        command = json.loads(line)
        if command["operation"] == "resume":
            emit({"type": "state", "state": "listening"})
            if mode == "command":
                emit({"type": "command", "epoch": command["epoch"], "text": "Джарвис, открой ютуб"})
            if mode == "ambient":
                emit({"type": "command", "epoch": command["epoch"], "text": "открой ютуб"})
        if command["operation"] == "pause" and mode != "no_pause":
            emit({"type": "state", "state": "paused", "id": command["id"]})
    time.sleep(60)  # Parent cancellation must kill a worker even if EOF is ignored.
