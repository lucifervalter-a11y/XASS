"""Read-only process-bound capability guard for native/legacy coexistence."""
from __future__ import annotations
import json
import math
from pathlib import Path
import time

INCOMPATIBLE = "Работает прежний агент XASS. Закройте старое приложение XASS полностью, затем запустите агент в нативном окне. Локальная музыка временно недоступна, чтобы два плеера не играли одновременно."
UNVERIFIED = "Не удалось проверить совместимость работающего агента. Закройте прежний XASS и повторите проверку перед локальным воспроизведением."

def _object(path: Path):
    if not path.is_file() or path.stat().st_size > 65536: return {}
    value=json.loads(path.read_text(encoding="utf-8-sig"))
    return value if isinstance(value,dict) else {}

def probe_agent(data: Path, source: Path, *, now: float | None = None) -> dict:
    import psutil
    inactive={"active":False,"compatible":True,"pid":0,"revision":"","reason":""}
    try:
        report=_object(Path(data)/".agent-status.json")
        pid=report.get("process_id")
        if type(pid) is not int or not 0 < pid <= 2**31-1: return inactive
        process=psutil.Process(pid)
        if not process.is_running(): return inactive
        command=process.cmdline()
        legacy = "--agent" in command or any(Path(str(part)).name.lower()=="client_agent.py" for part in command)
        native = "--agent-child" in command and (("--role" in command and command[command.index("--role")+1:command.index("--role")+2]==["background-agent"])
                  or any(Path(str(part)).name.lower()=="background_agent.py" for part in command))
        if not (legacy or native): return inactive
        created=process.create_time(); updated=report.get("updated_at")
        if isinstance(updated,bool) or not isinstance(updated,(int,float)) or not math.isfinite(updated) or updated < created-1 or updated > (time.time() if now is None else now)+5:
            return inactive
        expected=str(_object(Path(source)/"build-info.json").get("revision") or "development")
        revision=str(report.get("native_agent_revision") or "")
        capable=type(report.get("native_audio_ownership")) is int and report["native_audio_ownership"]==1
        context_matches = False
        if native and "--source" in command and "--data" in command:
            context_matches = (Path(command[command.index("--source")+1]).resolve() == Path(source).resolve()
                and Path(command[command.index("--data")+1]).resolve() == Path(data).resolve())
        compatible = native and context_matches and capable and revision==expected
        return {"active":True,"compatible":compatible,"pid":pid,"revision":revision[:80],"reason":"" if compatible else INCOMPATIBLE}
    except psutil.NoSuchProcess:
        return inactive
    except (psutil.AccessDenied,psutil.ZombieProcess):
        return {"active":True,"compatible":False,"pid":0,"revision":"","reason":UNVERIFIED}
    except (OSError,ValueError,TypeError,IndexError):
        return inactive
