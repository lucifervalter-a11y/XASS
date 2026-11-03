# Windows client updates and rollback

The native WinUI client and the legacy Python agent are separate update targets.
The legacy agent's authenticated server manifest must never be used to replace
an installed native client. Both native release channels instead fetch manifests and SHA-256-verified
installers from the official XASS GitHub Releases origin;
source commits, arbitrary links and the legacy server feed are not installers.

## Channel and local updates

**Релиз** selects `native`; **Бета** selects `native-test`. The saved selection
controls manual and already-opted-in automatic native update checks. Changing the
selection does not install a version. A version/channel transition remains a
separate, locally confirmed installer action. Older/unknown versions are refused.
Changing from Beta back to Release does not silently downgrade.

## Remote requests

An owner can request a native-channel check, a native-channel install, or a return
to the last successful native installation from the existing authenticated PC
control channel. This sends a bounded operation/channel intent only, never an EXE,
URL or new credential. A live, compatible native PC presents it in **Обновления**;
the owner must explicitly review it there. The requested channel is named on that
screen; an install still requires a second confirmation showing the exact version,
commit and download size. The UI obtains and validates the official release before
showing/installing it. An offline PC retains the request for up to 24 hours. It is
not evidence that a check or install completed.

The old generic update operation maps to the native selected-channel request only
after this native command handler is installed. Until then, do not assume that an
old installed app supports the remote channel selector.

## Rollback

The automatic updater already keeps a byte-verified copy and restores it if the
new package fails health checks. The manual **Вернуться к предыдущей установке**
action is separate: it accepts only the verified backup of the immediately
preceding *successful* native update, first makes and verifies a safety copy of
the currently installed program, and checks the restored native UI/helper before
reporting success. A bad/changed backup or failed restored health check does not
leave the target version installed. Automatic update is switched off before a
confirmed manual rollback so it cannot immediately install the new package again.

Rollback changes the application files only. It does **not** roll back the owner
profile, keys, archive, music or settings. Therefore an old binary may be
incompatible with data changed by the newer version; test a recovery on the
isolated synthetic fixture before relying on it for older schema transitions.

## XASS AI is a separate runtime

The native installer includes neither XASS AI/Hermes nor its model. The XASS
integration currently pins and validates Hermes Agent 0.22.2 / commit
`d923a5eb487a17c0a68801bba9ef0f56c81040a8`; it does not launch an updater. The
upstream published v0.22.5 on October 22, 2026. Its update path depends on the
external installation type (CLI, desktop installer, Store or Docker), and XASS
must revalidate its authenticated WebSocket health/session contract before
changing the strict version pin. Ask the owner to establish the actual local
installation type/version first. Do not run a guessed installer command, alter a
model/profile, or send credentials to the backend.
