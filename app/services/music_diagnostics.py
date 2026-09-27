"""Stable handoff failure categories, never raw agent messages or identifiers."""

TRANSFER_ERROR_CODES = frozenset({
    "source_timeout", "target_timeout", "source_stop_failed", "target_start_failed",
    "target_unavailable", "transfer_cancelled", "replaced", "agent_command_failed", "unknown",
})

_EXACT_REASONS = {
    "ПК не подтвердил остановку звука": "source_stop_failed",
    "Не удалось остановить прежний ПК": "source_stop_failed",
    "Запуск был прерван. Повторите переключение": "target_start_failed",
    "ПК не подтвердил запуск музыки": "target_start_failed",
    "ПК не запустил музыку": "target_start_failed",
    "Переключение отменено": "transfer_cancelled",
    "Переключение заменено новым действием": "replaced",
}
_TIMEOUT_REASONS = frozenset({
    "Время переключения истекло",
    "Время переключения истекло. Повторите действие",
    "Предыдущее устройство не подтвердило переключение. Музыка не запущена повторно; попробуйте ещё раз",
})
_UNAVAILABLE_REASONS = frozenset({
    "Компьютер не в сети",
    "ПК недоступен для музыки. Проверьте подключение и версию агента",
    "Перепривяжите ПК с индивидуальным ключом",
    "Обновите агент XASS до версии 0.16.0 или новее",
})


def classify_transfer_failure(detail, *, phase="") -> str:
    """Classify exact server-owned messages; never parse/export arbitrary text."""
    if not isinstance(detail, str) or not detail:
        return "unknown"
    if detail in _EXACT_REASONS:
        return _EXACT_REASONS[detail]
    if detail in _TIMEOUT_REASONS:
        return "target_timeout" if phase in {"starting", "stopped", "ready"} else "source_timeout"
    if detail in _UNAVAILABLE_REASONS:
        return "source_stop_failed" if phase == "waiting" else "target_unavailable"
    return "agent_command_failed"


def transfer_failure_code(transfer) -> str:
    """Read persisted codes, with a conservative fallback for pre-update rows."""
    target = transfer.target if isinstance(transfer.target, dict) else {}
    saved = target.get("failure_code")
    if isinstance(saved, str) and saved in TRANSFER_ERROR_CODES:
        return saved
    phase = "starting" if transfer.start_command_id else "waiting"
    return classify_transfer_failure(transfer.detail, phase=phase)
