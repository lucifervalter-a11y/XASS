"""Exercise the installed helper's real fixed roles without network, audio or credentials."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import wave


def exercise(helper: Path, source: Path, data: Path, role: str, requests: list[dict]) -> list[dict]:
    arguments = [str(helper), '--role', role]
    if role != 'assistant':
        arguments += ['--source', str(source), '--data', str(data)]
    process = subprocess.run(arguments, input=''.join(json.dumps(value) + '\n' for value in requests),
                             encoding='utf-8', capture_output=True, timeout=30,
                             env=dict(os.environ, XASS_DATA_ROOT=str(data), PYTHONIOENCODING='utf-8'))
    if process.returncode != 0:
        raise RuntimeError(f'Installed {role} failed with exit {process.returncode}; raw output omitted')
    rows = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
    if not rows:
        raise RuntimeError(f'Installed {role} returned no protocol data')
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--installed', type=Path, required=True)
    args = parser.parse_args()
    root = args.installed.resolve(strict=True)
    helper, source = root / 'runtime/XASS.NativeHelper.exe', root / 'pc_client'
    with tempfile.TemporaryDirectory(prefix='xass-frozen-smoke-') as temp:
        data = Path(temp)
        bridge = exercise(helper, source, data, 'agent-bridge', [{'action': 'desktop_status', 'version': 1}])
        if len(bridge) != 1 or bridge[0].get('ok') is not True:
            raise RuntimeError('Installed native desktop bridge failed')
        # mutagen discovers codec parsers dynamically. Exercise an actual file
        # through the frozen bridge so missing frozen parsers cannot pass merely
        # because importing the top-level mutagen package succeeds.
        library = data / 'synthetic-library'; library.mkdir()
        with wave.open(str(library / 'untagged.wav'), 'wb') as audio:
            audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(8000)
            audio.writeframes(b'\x00\x00' * 800)
        scan = exercise(helper, source, data, 'agent-bridge',
                        [{'action': 'desktop_curation_start', 'version': 1, 'folder': str(library)}])
        tracks = scan[0].get('result', {}).get('tracks', [])
        if scan[0].get('ok') is not True or len(tracks) != 1:
            raise RuntimeError('Installed local-library scan failed')
        inspected = exercise(helper, source, data, 'agent-bridge',
                             [{'action': 'desktop_curation_inspect', 'version': 1, 'id': tracks[0]['id']}])
        if inspected[0].get('ok') is not True or not inspected[0].get('result', {}).get('tags'):
            raise RuntimeError('Installed audio-tag parser is missing or failed')
        assistant = exercise(helper, source, data, 'assistant',
                             [{'operation': 'plan', 'settings': {}, 'text': 'открой ютуб'}])
        if not any(row.get('type') == 'result' and row.get('result', {}).get('state') == 'ready' for row in assistant):
            raise RuntimeError('Installed text-only assistant planning failed')
        music = exercise(helper, source, data, 'desktop-music',
                         [{'version': 1, 'id': 1, 'action': 'snapshot'}, {'version': 1, 'id': 2, 'action': 'shutdown'}])
        if len(music) != 2 or any(row.get('ok') is not True for row in music):
            raise RuntimeError('Installed local music protocol failed')
        host = exercise(helper, source, data, 'background-agent',
                        [{'version': 1, 'id': 1, 'action': 'host_status'}, {'version': 1, 'id': 2, 'action': 'host_quit'}])
        if len(host) != 2 or any(row.get('ok') is not True for row in host):
            raise RuntimeError('Installed background host protocol failed')
        if (data / 'config.json').exists():
            raise RuntimeError('Read-only installed smoke unexpectedly created pairing configuration')
    print('Installed roles passed: desktop bridge, local-library codec parsing, text planning, idle music and unpaired background host. No microphone, model, player or network action requested.')


if __name__ == '__main__':
    main()
