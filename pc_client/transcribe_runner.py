"""Song transcription runner: Demucs (htdemucs vocals) -> faster-whisper large-v3.

Runs inside the separate transcription runtime (its own Python venv with
torch/demucs/faster-whisper), never inside the XASS client process. The client
starts it at BELOW_NORMAL/IDLE priority with limited threads and reads
"PROGRESS <stage> <fraction>" lines from stdout.

    python transcribe_runner.py --input song.mp3 --output result.json \
        --language ru --device cuda --threads 4 --models <dir> --workdir <dir>
    python transcribe_runner.py --warmup --device cuda --models <dir>

Output JSON: {"lines": [{"start", "end", "text"}], "language", "model", "device"}.
Heavy libraries are imported lazily so the line builder is unit-testable.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Iterable

MODEL = "large-v3"
DEMUCS_MODEL = "htdemucs"
MAX_LINE_CHARS = 48
MIN_BREAK_CHARS = 18
GAP_BREAK_SEC = 0.9
MAX_LINES = 2000
GPU_ERRORS = (RuntimeError, OSError, ValueError)  # CUDA OOM, missing cuDNN DLLs, unsupported float16
# Well-known Whisper hallucinations on music/silence (mostly Russian subtitles credits).
_HALLUCINATIONS = re.compile(
    r"(субтитр|продолжение следует|редактор субтитров|dimatorzok|спасибо за просмотр|"
    r"подписывайтесь на канал|thanks for watching|amara\.org|♪+$)", re.I)


def progress(stage: str, fraction: float) -> None:
    print(f"PROGRESS {stage} {max(0.0, min(1.0, float(fraction))):.3f}", flush=True)


def _get(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def _line(words: list[tuple[float, float, str]]) -> dict | None:
    text = " ".join("".join(word for _, _, word in words).split())
    if not text:
        return None
    return {"start": round(words[0][0], 3), "end": round(max(words[-1][1], words[0][0] + 0.2), 3), "text": text[:500]}


def build_lines(segments: Iterable[Any]) -> list[dict]:
    """Whisper segments (with optional word timestamps) -> short karaoke lines."""
    out: list[dict] = []
    for segment in segments:
        words = [(float(_get(w, "start", 0) or 0), float(_get(w, "end", 0) or 0), str(_get(w, "word", "") or ""))
                 for w in (_get(segment, "words") or [])]
        words = [w for w in words if w[2].strip()]
        if not words:
            text = " ".join(str(_get(segment, "text", "") or "").split())
            if text:
                start = float(_get(segment, "start", 0) or 0)
                out.append({"start": round(start, 3), "end": round(max(float(_get(segment, "end", 0) or 0), start + 0.2), 3),
                            "text": text[:500]})
            continue
        current: list[tuple[float, float, str]] = []
        for word in words:
            if current:
                chars = len("".join(w for _, _, w in current).strip())
                gap = word[0] - current[-1][1]
                sentence_end = current[-1][2].rstrip().endswith((".", "!", "?", "…"))
                if gap > GAP_BREAK_SEC or chars + len(word[2]) > MAX_LINE_CHARS or (sentence_end and chars >= MIN_BREAK_CHARS):
                    line = _line(current)
                    if line:
                        out.append(line)
                    current = []
            current.append(word)
        line = _line(current)
        if line:
            out.append(line)
    return clean_lines(out)


def clean_lines(lines: list[dict]) -> list[dict]:
    result: list[dict] = []
    repeats = 0
    for line in sorted(lines, key=lambda item: item["start"]):
        if _HALLUCINATIONS.search(line["text"]):
            continue
        if result and result[-1]["text"].casefold() == line["text"].casefold():
            repeats += 1
            if repeats >= 3:  # Whisper repetition loops; real choruses rarely repeat a line 4x back-to-back.
                continue
        else:
            repeats = 0
        result.append(line)
        if len(result) >= MAX_LINES:
            break
    return result


def separate_vocals(source: Path, workdir: Path, device: str, threads: int) -> Path:
    import torch
    torch.set_num_threads(max(1, threads))
    from demucs.separate import main as demucs_main
    args = ["-n", DEMUCS_MODEL, "--two-stems", "vocals", "-d", device, "-o", str(workdir), str(source)]
    demucs_main(args)
    vocals = workdir / DEMUCS_MODEL / source.stem / "vocals.wav"
    if not vocals.is_file():
        raise RuntimeError("demucs produced no vocals stem")
    return vocals


def load_whisper(device: str, threads: int, models: Path):
    if device == "cuda":
        # On Windows importing torch first puts its bundled cuDNN/cuBLAS DLLs on
        # the search path, which CTranslate2 (faster-whisper) then reuses.
        import torch  # noqa: F401
    from faster_whisper import WhisperModel
    compute_type = "float16" if device == "cuda" else "int8"
    return WhisperModel(MODEL, device=device, compute_type=compute_type, cpu_threads=max(1, threads),
                        download_root=str(models / "whisper"))


def transcribe(vocals: Path, language: str, device: str, threads: int, models: Path, duration_hint: float = 0,
               vad_filter: bool = True, model=None) -> tuple[list[dict], str]:
    model = model or load_whisper(device, threads, models)
    segments, info = model.transcribe(str(vocals), language=None if language in {"", "auto"} else language,
                                      word_timestamps=True, vad_filter=vad_filter, beam_size=5,
                                      condition_on_previous_text=False)
    total = float(getattr(info, "duration", 0) or duration_hint or 0)
    collected = []
    for segment in segments:
        collected.append(segment)
        if total > 0:
            progress("transcribe", min(0.99, float(_get(segment, "end", 0) or 0) / total))
    return build_lines(collected), str(getattr(info, "language", "") or language)


def hear(source: Path, vocals: Path, language: str, device: str, threads: int, models: Path) -> tuple[list[dict], str]:
    """Vocals first. A sung or tuned vocal often disappears into the mix or the VAD."""
    found_language = language
    model = load_whisper(device, threads, models)
    for target, vad in ((vocals, True), (vocals, False), (source, False)):
        lines, found_language = transcribe(target, language, device, threads, models, vad_filter=vad, model=model)
        if lines:
            return lines, found_language
    return [], found_language


def run(args) -> dict:
    started = time.monotonic()
    models = Path(args.models)
    os.environ.setdefault("TORCH_HOME", str(models / "torch"))
    os.environ.setdefault("HF_HOME", str(models / "hf"))
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    device = args.device
    progress("separate", 0.0)
    try:
        vocals = separate_vocals(Path(args.input), workdir, device, args.threads)
    except GPU_ERRORS as exc:
        if device != "cuda":
            raise
        print(f"cuda separation failed, falling back to cpu: {exc}", file=sys.stderr, flush=True)
        device = "cpu"
        vocals = separate_vocals(Path(args.input), workdir, device, args.threads)
    progress("transcribe", 0.0)
    try:
        lines, language = hear(Path(args.input), vocals, args.language, device, args.threads, models)
    except GPU_ERRORS as exc:
        if device != "cuda":
            raise
        print(f"cuda transcription failed, falling back to cpu: {exc}", file=sys.stderr, flush=True)
        device = "cpu"
        lines, language = hear(Path(args.input), vocals, args.language, device, args.threads, models)
    return {"lines": lines, "language": language, "model": MODEL, "device": device,
            "elapsed_sec": round(time.monotonic() - started, 1)}


WHISPER_REPO = "Systran/faster-whisper-large-v3"
WHISPER_FILES = ("config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.json", "vocabulary.txt")
WHISPER_FALLBACK_BYTES = 3_090_000_000
DEMUCS_SHARE = 0.03  # htdemucs is ~80 MB of the ~3.2 GB first download


def _tree_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def whisper_total_bytes() -> int:
    try:
        from huggingface_hub import HfApi
        info = HfApi().model_info(WHISPER_REPO, files_metadata=True)
        total = sum(int(item.size or 0) for item in info.siblings or [] if item.rfilename in WHISPER_FILES)
        return total or WHISPER_FALLBACK_BYTES
    except Exception:
        return WHISPER_FALLBACK_BYTES


def watch_download(directory: Path, total: int, start: float, share: float, interval: float = 1.0):
    """Report bytes on disk (including partial files) as "PROGRESS models f"."""
    import threading
    stop = threading.Event()
    baseline = _tree_bytes(directory)

    def loop():
        while not stop.wait(interval):
            done = max(0, _tree_bytes(directory) - baseline)
            progress("models", min(0.99, start + share * min(1.0, done / max(1, total))))

    thread = threading.Thread(target=loop, name="model-download-progress", daemon=True)
    thread.start()
    return stop


def warmup(args) -> dict:
    """Download both models once (with progress) so the first job does not hit its deadline."""
    models = Path(args.models)
    os.environ.setdefault("TORCH_HOME", str(models / "torch"))
    os.environ.setdefault("HF_HOME", str(models / "hf"))
    progress("models", 0.0)
    from demucs.pretrained import get_model
    get_model(DEMUCS_MODEL)
    progress("models", DEMUCS_SHARE)
    target = models / "whisper"
    target.mkdir(parents=True, exist_ok=True)
    stop = watch_download(target, whisper_total_bytes(), DEMUCS_SHARE, 0.99 - DEMUCS_SHARE)
    try:
        from faster_whisper.utils import download_model
        download_model(MODEL, cache_dir=str(target))
    finally:
        stop.set()
    progress("models", 1.0)
    return {"ok": True, "model": MODEL, "device": args.device}


def _parent_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) != 0  # 0 = signaled/exited
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def watch_parent(pid: int, interval: float = 5.0) -> None:
    """Exit when the XASS agent goes away so no orphan keeps the GPU busy."""
    import threading

    def loop():
        while True:
            time.sleep(interval)
            if not _parent_alive(pid):
                os._exit(3)

    if pid > 0:
        threading.Thread(target=loop, name="parent-watchdog", daemon=True).start()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="XASS song transcription runner")
    parser.add_argument("--input", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--language", default="ru")
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cpu")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--models", required=True)
    parser.add_argument("--workdir", default="")
    parser.add_argument("--warmup", action="store_true")
    parser.add_argument("--parent-pid", type=int, default=0)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    watch_parent(args.parent_pid)
    try:
        result = warmup(args) if args.warmup else run(args)
    except Exception as exc:  # reported to the client, which reports it to the server
        print(f"ERROR {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr, flush=True)
        return 2
    if args.output:
        tmp = Path(args.output).with_suffix(".tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, args.output)
    progress("upload", 1.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
