# Portable native updater transport checks

Run from the repository root with the .NET 8 SDK:

```sh
dotnet run --project windows/NativeUpdate.Tests/NativeUpdate.Tests.csproj -c Release
```

This executable links the production `NativeUpdateClient` source and uses only
framework libraries, an in-memory HTTP handler, synthetic response streams and
a uniquely named temporary folder. It does not connect to GitHub, read user
configuration, run an installer or change the installed application.

Production transfer policy:

- One two-minute overall budget for release discovery and manifest retrieval.
- One fifteen-minute overall budget for an installer download.
- Thirty seconds without network progress terminates either operation.
- The overall deadline never resets when a read or redirect makes progress.
- User cancellation remains cancellation; deadline expiry is a retryable
  `TimeoutException` with a user-safe localized message.
- Every read/write observes the linked operation token. Responses/streams are
  disposed and `.download` files are removed before failure reaches the caller.
  Previously verified installer files are not replaced on failure.

Tests shorten those budgets and cover headers followed by a stalled body,
release-list/manifest/installer idle timeouts, trickle downloads bounded by the
overall deadline, the shared multi-request check budget, caller cancellation,
pre-canceled callers, cleanup, retry using the same client, healthy idle renewal,
size/SHA-256 rejection and existing HTTPS/host/redirect boundaries.

These are transport/service tests, not a Windows UI or real-network acceptance
claim. Verify the UI releases its busy state and offers retry after a timeout,
resets progress, and distinguishes cancellation on a Windows test installation.
