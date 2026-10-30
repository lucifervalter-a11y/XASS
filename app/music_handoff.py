"""Fenced three-phase handoff. Missing receipts never prove silence or sound."""
from datetime import datetime, timezone
import secrets

from fastapi import HTTPException
from sqlalchemy import select, update

from app.models import AgentCommand, HeartbeatSource
from app.music_models import MusicSession
from app.music_playback_models import MusicTransfer


def reliable(transfer):
    return (transfer.target or {}).get("protocol") == 2


def changed(transfer, **values):
    transfer.target = {**(transfer.target or {}), **values}


def receipt(details, transfer, states):
    target = transfer.target
    return (isinstance(details, dict) and details.get("state") in states
            and details.get("handoff_id") == transfer.id
            and details.get("handoff_revision") == target.get("attempt_revision"))


class ReliableHandoff:
    def __init__(self, control, body, fail, playback_meta):
        self.control, self.body, self.fail, self.meta = control, body, fail, playback_meta

    async def phase(self, session, transfer, before, after):
        """A database compare-and-set fences polls from different workers."""
        result = await session.execute(update(MusicTransfer).where(MusicTransfer.id == transfer.id,
            MusicTransfer.status == before).values(status=after).execution_options(synchronize_session=False))
        await session.refresh(transfer, attribute_names=["status"])
        return result.rowcount == 1

    async def send(self, transfer, action, device, request, user, session):
        target = transfer.target
        sent = await self.control(self.body(source_name=device[6:], action=action,
            track_id=target["track_id"], output_id=target["output_id"], position_sec=transfer.position,
            volume=target["volume"], handoff_id=transfer.id, handoff_revision=target["attempt_revision"],
            expires_at=int(datetime.now(timezone.utc).timestamp()) + (150 if action == "prepare" else 25),
            lan_url=target.get("lan_url") if action == "prepare" else None), request, user, session)
        return sent["command_id"]

    async def initialize(self, transfer, audible, request, user, session):
        meta = await self.meta(session)
        meta.revision += 1
        changed(transfer, attempt_revision=meta.revision, source_audible=audible,
                phase_started=datetime.now(timezone.utc).timestamp())
        transfer.status = "preparing"
        await session.commit()
        if transfer.target["device"].startswith("agent:"):
            try:
                ident = await self.send(transfer, "prepare", transfer.target["device"], request, user, session)
                changed(transfer, prepare_command_id=ident)
                await session.commit()
            except HTTPException as error:
                await self.fail(session, transfer, str(error.detail))

    async def advance(self, transfer, request, user, session):
        if transfer.status in {"ready", "failed"}:
            return
        meta = await self.meta(session)
        await session.refresh(transfer)
        target = transfer.target
        if meta.transfer_id != transfer.id:
            await self.fail(session, transfer, "Переключение заменено новым действием")
            return
        elapsed = datetime.now(timezone.utc).timestamp() - target.get("phase_started", 0)
        if elapsed > (150 if transfer.status == "preparing" else 25):
            await self.fail(session, transfer, "Передача не подтверждена. Звук не возобновляется автоматически. Повторите передачу или явно выберите устройство.")
            return
        device = target["device"]
        # A worker can stop after persisting a phase but before storing its
        # command ID. The durable per-attempt command key makes recovery safe.
        try:
            if transfer.status == "preparing" and device.startswith("agent:") and not target.get("prepare_command_id"):
                changed(transfer, prepare_command_id=await self.send(transfer, "prepare", device, request, user, session))
                await session.commit()
                target = transfer.target
            elif transfer.status == "waiting" and transfer.source_device.startswith("agent:") and not transfer.stop_command_id:
                transfer.stop_command_id = await self.send(transfer, "pause", transfer.source_device, request, user, session)
                await session.commit()
            elif transfer.status == "starting" and device.startswith("agent:") and not transfer.start_command_id:
                transfer.start_command_id = await self.send(transfer, "start_prepared", device, request, user, session)
                await session.commit()
        except HTTPException as error:
            await self.fail(session, transfer, str(error.detail))
            return
        if transfer.status == "preparing":
            prepared = bool(target.get("local_prepared")) if device == "local" else False
            command = await session.get(AgentCommand, target.get("prepare_command_id")) if target.get("prepare_command_id") else None
            if device.startswith("agent:"):
                source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == device[6:]))
                details = (source.last_payload or {}).get("music_player") if source else {}
                prepared = receipt(details, transfer, {"prepared"})
                if command and command.status in {"failed", "cancelled"} or receipt(details, transfer, {"error"}):
                    await self.fail(session, transfer, "Целевой ПК не смог подготовить трек или аудиовыход")
                    return
            if not prepared:
                return
            if not await self.phase(session, transfer, "preparing", "waiting" if target["source_audible"] else "stopped"):
                return
            changed(transfer, phase_started=datetime.now(timezone.utc).timestamp())
            await session.commit()
            if transfer.status == "waiting" and transfer.source_device.startswith("agent:"):
                try:
                    transfer.stop_command_id = await self.send(transfer, "pause", transfer.source_device, request, user, session)
                    await session.commit()
                except HTTPException as error:
                    await self.fail(session, transfer, str(error.detail))
                    return
        if transfer.status == "waiting" and transfer.stop_command_id:
            command = await session.get(AgentCommand, transfer.stop_command_id)
            details = (command.result or {}).get("details") if command else {}
            if command and command.status == "completed" and (command.result or {}).get("ok") is True:
                if not receipt(details, transfer, {"paused", "stopped", "ended", "idle"}):
                    await self.fail(session, transfer, "Прежний ПК не подтвердил остановку именно этого переключения")
                    return
                if target.get("preserve_position"):
                    position = details.get("position_sec")
                    if isinstance(position, bool) or not isinstance(position, (int, float)) or not 0 <= position <= 86400:
                        await self.fail(session, transfer, "Прежний ПК не подтвердил точную позицию")
                        return
                    transfer.position = position
                if not await self.phase(session, transfer, "waiting", "stopped"):
                    return
            elif command and command.status in {"failed", "cancelled"}:
                await self.fail(session, transfer, "Не удалось подтвердить остановку прежнего ПК")
                return
        if transfer.status == "stopped":
            if not await self.phase(session, transfer, "stopped", "starting"):
                return
            item = await session.get(MusicSession, 1)
            if item is None:
                item = MusicSession(id=1)
                session.add(item)
            target = transfer.target
            item.track_id, item.device, item.session_key = target["track_id"], device, target["session_key"]
            item.position, item.state, item.updated_at = transfer.position, "loading", datetime.now(timezone.utc)
            meta.client_id, meta.output_id, meta.volume = target["client_id"], target["output_id"], target["volume"]
            if target.get("queue") is not None:
                meta.queue = list(dict.fromkeys(target["queue"]))
            meta.repeat_mode = target["repeat_mode"]
            meta.revision += 1
            changed(transfer, lease_revision=meta.revision, phase_started=datetime.now(timezone.utc).timestamp())
            if not target["autoplay"]:
                item.state = "paused"
                transfer.status, meta.transfer_id = "ready", ""
            await session.commit()
            if device.startswith("agent:") and target["autoplay"]:
                try:
                    transfer.start_command_id = await self.send(transfer, "start_prepared", device, request, user, session)
                    await session.commit()
                except HTTPException as error:
                    await self.fail(session, transfer, str(error.detail))
                    return
        if transfer.status == "starting" and device.startswith("agent:"):
            source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == device[6:]))
            details = (source.last_payload or {}).get("music_player") if source else {}
            command = await session.get(AgentCommand, transfer.start_command_id) if transfer.start_command_id else None
            # A command ACK is merely acceptance. Only the current fenced
            # player heartbeat can confirm that the audio engine is running.
            if (receipt(details, transfer, {"playing"}) and details.get("track_id") == target["track_id"]
                    and details.get("audio_started") is True):
                await self.finish(transfer, session)
            elif command and command.status in {"failed", "cancelled"} or receipt(details, transfer, {"error"}):
                await self.fail(session, transfer, "Целевой ПК не подтвердил воспроизведение. Прежнее устройство остаётся на паузе")

    async def finish(self, transfer, session):
        meta = await self.meta(session)
        item = await session.get(MusicSession, 1)
        if meta.transfer_id != transfer.id or not item or item.session_key != transfer.target["session_key"]:
            raise HTTPException(409, "Переключение заменено")
        item.state, item.updated_at = "playing", datetime.now(timezone.utc)
        transfer.status, meta.transfer_id = "ready", ""
        await session.commit()

    async def acknowledge_target(self, transfer, payload, session):
        target = transfer.target
        if (target["device"] != "local" or not secrets.compare_digest(payload.session_key, target["session_key"])
                or payload.revision != target["attempt_revision"]):
            raise HTTPException(409, "Подтверждение другого переключения")
        meta = await self.meta(session)
        await session.refresh(transfer)
        if transfer.status == "ready" and payload.state == "playing":
            return
        if meta.transfer_id != transfer.id or transfer.status == "failed":
            raise HTTPException(409, "Переключение истекло или отменено")
        if payload.state == "prepared" and transfer.status == "preparing":
            changed(transfer, local_prepared=True)
            await session.commit()
        elif payload.state == "playing" and transfer.status == "starting":
            await self.finish(transfer, session)
        else:
            raise HTTPException(409, "Подтверждение не соответствует этапу переключения")
