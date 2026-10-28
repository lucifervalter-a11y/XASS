"""One bounded local request, JSON-line progress, one result. No server or keys."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE if (HERE / "voice_assistant.py").is_file() else HERE.parent / "pc_client"))
from voice_assistant import AssistantError, LABELS, LocalExecutor, Settings, validate_plan


def emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=True, allow_nan=False), flush=True)


def handle(request: object, *, executor=None, progress=lambda state: None) -> dict:
    if not isinstance(request, dict) or set(request) - {"operation", "settings", "text", "plan", "confirmed"}:
        raise AssistantError("Неверный запрос помощника.")
    operation = request.get("operation")
    if operation not in {"plan", "record", "execute"}:
        raise AssistantError("Неизвестное действие помощника.")
    settings = Settings.from_dict(request.get("settings", {}))
    text = request.get("text", "")
    if operation == "record":
        from voice_capture import WhisperTranscriber, record_pcm
        progress("loading_model")
        # Load/check dependencies before acquiring microphone; never download.
        transcriber = WhisperTranscriber(settings.stt_model_path)
        progress("recording")
        pcm = record_pcm()
        progress("transcribing")
        try:
            text = transcriber.transcribe(pcm)
        finally:
            del pcm
    if operation in {"plan", "record"}:
        progress("planning")
        try:
            plan = settings.make_planner().plan(text)
        except AssistantError as error:
            # Keep recognized text editable even if it was outside the grammar.
            return {"state": "rejected", "message": str(error), "text": text, "plan": None}
        return {"state": "ready", "text": text, "plan": plan.to_dict(), "label": LABELS[plan.action],
                "message": "Проверьте команду и нажмите «Выполнить»."}
    plan = validate_plan(request.get("plan"), text)
    if type(request.get("confirmed", False)) is not bool:
        raise AssistantError("Неверное подтверждение.")
    progress("executing")
    return asdict((executor or LocalExecutor()).execute(plan, text, settings,
                                                       confirmed=request.get("confirmed", False)))


def main() -> None:
    try:
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise AssistantError("Запрос помощника слишком большой.")
        request = json.loads(raw.decode("utf-8"))
        result = handle(request, progress=lambda state: emit({"type": "progress", "state": state}))
        emit({"type": "result", "ok": True, "result": result})
    except Exception as error:
        emit({"type": "result", "ok": False, "error": str(error) if isinstance(error, AssistantError)
              else "Помощник не выполнил действие. Проверьте локальные настройки и зависимости."})


if __name__ == "__main__":
    main()
