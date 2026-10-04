using System.IO.Pipes;
using System.Text.Json;
using System.Text;

namespace Xass.Native.Services;

// Current-user-only pipe; another local account cannot inject profile imports.
// The first process retains the mutex and activation pipe until final exit.
public sealed class NativeInstance : IDisposable
{
    private readonly Mutex mutex;
    private readonly CancellationTokenSource stop = new();
    private const string Name = "XASS.Native.Test.Activation";
    public bool Primary { get; }
    public NativeInstance()
    { mutex = new Mutex(true, "Local\\" + Name, out bool primary); Primary = primary; }
    public async Task ForwardAsync(string[] args)
    {
        using var pipe = new NamedPipeClientStream(".", Name, PipeDirection.Out, PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await pipe.ConnectAsync(timeout.Token);
        using var writer = new StreamWriter(pipe);
        await writer.WriteLineAsync(JsonSerializer.Serialize(args.Take(8)));
    }
    public async Task ListenAsync(Action<string[]> activated)
    {
        while (!stop.IsCancellationRequested)
        {
            try
            {
                using var pipe = new NamedPipeServerStream(Name, PipeDirection.In, 1, PipeTransmissionMode.Byte,
                    PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
                await pipe.WaitForConnectionAsync(stop.Token);
                using var reader = new StreamReader(pipe);
                var input = new StringBuilder(); var chars = new char[1024]; bool complete = false;
                using var deadline = CancellationTokenSource.CreateLinkedTokenSource(stop.Token);
                deadline.CancelAfter(TimeSpan.FromSeconds(5));
                while (!complete)
                {
                    int count = await reader.ReadAsync(chars.AsMemory(), deadline.Token);
                    if (count == 0) break;
                    int newline = Array.IndexOf(chars, '\n', 0, count);
                    int take = newline < 0 ? count : newline;
                    if (input.Length + take > 32768) throw new IOException("Activation too large");
                    input.Append(chars, 0, take); complete = newline >= 0;
                }
                if (!complete) continue;
                string[]? args = JsonSerializer.Deserialize<string[]>(input.ToString());
                if (args is { Length: <= 8 }) activated(args);
            }
            catch (OperationCanceledException) { if (stop.IsCancellationRequested) break; }
            catch (IOException) { }
            catch (JsonException) { }
        }
    }
    public void Dispose()
    { stop.Cancel(); if (Primary) mutex.ReleaseMutex(); mutex.Dispose(); stop.Dispose(); }
}
