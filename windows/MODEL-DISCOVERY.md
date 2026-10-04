# Existing local Whisper model discovery

On each native window's initial configuration, an empty assistant model field is
filled from a complete existing Hugging Face snapshot when one can be found.
There are no downloads, recursive disk scans, saved configuration writes, or
microphone starts. New machines still need a separately installed local model.

## Selection and boundaries

- Any nonempty `XASS_ASSISTANT_MODEL`, including an invalid path, wins unchanged.
  A prefilled UI value also wins. Any model-field edit while the probe is running,
  including entering and clearing text, cancels application of the result.
- The probe runs on `Task.Run`, once during initialization. It checks close,
  cancellation, edits, active requests and background-listening state before
  filling the field. The UI wait is capped at three seconds.
- Exact known roots only: `HF_HUB_CACHE`, `HUGGINGFACE_HUB_CACHE`, `HF_HOME/hub`,
  `XDG_CACHE_HOME/huggingface/hub`, and `<user>/.cache/huggingface/hub`.
- XASS's existing song-transcription caches are included under `XASS_DATA_ROOT`
  and `%LOCALAPPDATA%/XASS`: `transcription/models/whisper` and
  `transcription/models/hf/hub`. The former is the explicit `cache_dir` used by
  `transcribe_runner.py`; the latter follows its `HF_HOME`.
- Traversal is limited to nine roots, 128 immediate repository entries per root
  plus two direct known-small-repository probes, 16 immediate snapshot entries
  per repository plus a bounded direct `refs/main` lookup, and 256 total snapshot
  probes. There is a two-second cooperative budget between filesystem operations.
  A slow filesystem call itself cannot be interrupted; it stays off the UI thread
  and the result is abandoned when the UI deadline expires.
- Only `models--...whisper.../snapshots/<40-or-64-hex-revision>` shapes are searched.
  Complete multilingual small models are preferred, then base, tiny, medium,
  and other multilingual Whisper conversions. Cache order breaks equal-model
  ties. Within a repository `refs/main` is preferred; otherwise the most recently
  modified complete snapshot within the enumerated bound wins.
- Candidates require readable, nonempty `model.bin`, `config.json`, and
  `tokenizer.json`. Config is bounded to 1 MiB and must parse as an object with
  multiple Whisper language token IDs. `.en` repositories are not auto-selected
  for XASS's Russian recognition.
- Standard snapshot-file symlinks into the same repository's local `blobs`
  directory are accepted. Directory reparse points, chained/external file links,
  UNC/device paths and Windows mapped network drives are skipped. Those uncommon
  layouts remain available through an explicit manual model selection.
- This verifies the local folder and required files, not the integrity of all
  weights, full tokenizer semantics, or successful model inference. Python
  remains the final model loader and reports incompatible/corrupt model errors.

## Explicit-path offline guard

`pc_client/voice_capture.py` now rejects any absolute explicit model folder missing
one of the same three nonempty files before importing or constructing WhisperModel.
It does not switch to a different cache/model. `local_files_only=True` remains set.
This matters because faster-whisper's tokenizer fallback independently calls
`Tokenizer.from_pretrained` when `tokenizer.json` is missing.

## Sources and validation

Official implementation/documentation consulted:

- https://huggingface.co/docs/huggingface_hub/en/guides/manage-cache
- https://huggingface.co/docs/huggingface_hub/en/package_reference/environment_variables
- https://github.com/SYSTRAN/faster-whisper/blob/master/faster_whisper/transcribe.py
  (`WhisperModel.__init__`, local directory and tokenizer fallback)
- https://github.com/SYSTRAN/faster-whisper/blob/master/faster_whisper/utils.py
  (model alias mapping and downloaded files)
- https://huggingface.co/Systran/faster-whisper-small/blob/main/config.json

Portable deterministic fixture tests (no network or model inference):

```sh
dotnet run --project windows/ModelDiscovery.Tests/ModelDiscovery.Tests.csproj
python -m unittest discover -s windows/tests -p test_voice_capture.py -v
python -m unittest discover -s windows/tests -p test_voice_ui_contract.py -v
```

The .NET harness covers cache roots, private XASS caches, ranking, revision
fallback, missing/empty/invalid files, English-only exclusion, no recursive scans,
root/snapshot bounds, cancellation and HF symlinks. On Windows, symlink fixture
cases require Developer Mode/admin and `XASS_TEST_SYMLINKS=1`; they run on Linux
without special configuration. UI tests are source contracts, not runtime tests.
Windows UI acceptance, the actual user's cached path, microphone behavior and
real inference still require a Windows run.
