namespace Xass.Native.Services;
// DTO only: no network or installer starts in these portable tests.
public sealed record NativeUpdate(string Version, string Revision, string Tag, Uri Download, long Size, string Sha256);
