# Windows 0.21.4: release and beta channels

This release changes the native Windows updater. It does not deploy the backend,
change the legacy `agent-latest` feed, or implement Discord voice playback.

## First installation from legacy XASS 0.21.0

Download `XASS-Native-Setup.exe` (release) or `XASS-Native-Test-Setup.exe` (beta)
from the matching GitHub release. These complete x64 installers include the
Python and .NET runtimes. Whisper model files are not included.

Exit the old XASS application and agent before starting the native application;
do not run two agents against the same profile. The legacy installer has a
different application ID, so its old Update button cannot migrate to native.
Keep the old installation until the new application has been checked. Do not
delete `%LOCALAPPDATA%\XASS` or `%LOCALAPPDATA%\XASS.Native`: these contain the
pairing, archive and preferences. Native beta uses this same real profile; it is
not a sandbox just because its release label is Beta.

The installers are not Authenticode-signed. SHA-256 verifies the published
download against its release manifest, not an independent publisher certificate.

## Channel selection

In the native application's Updates page, choose **Релиз** or **Бета** and
confirm the change. The selected channel is saved separately from the currently
installed package's identity. It applies to both manual update checks and
separately enabled automatic updates.

- Changing the selector does not itself install a package or enable automatic
  updates.
- A check/download/install captures its channel; another operation cannot
  replace that selection midway through the update.
- Stable and beta packages remain distinct, with strict tag, prerelease,
  filename, manifest, size and SHA-256 validation.
- Older versions are not installed. If the selected channel is behind, the
  current version remains installed until a compatible release is available.
- A same-version, same-commit transition between channels still verifies the
  target package's channel, including after installation and rollback.
- Native stable and beta share an Inno Setup application ID and use the existing
  installation directory. They are not separate side-by-side installations.

## Release verification

`windows-channel-release.yml` builds both distributions from the same immutable
commit. Its delivery gate requires the full isolated Python regression suite,
browser/PHP contracts, native tests and both installer jobs. Real installation,
native readiness, forced-failure rollback and transitions in both directions
run only on ephemeral GitHub Actions Windows runners, never on the user's PC.

Publication is a separate explicit action after successful checks; the workflow
does not push a release, modify production, or update installed applications.
Passing these tests does not establish physical iPhone/PC audio handoff, long VPN
sessions, or a live Discord voice performance.
