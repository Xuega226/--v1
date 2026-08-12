using System.IO.Pipes;
using System.IO;
using System.Text;
using System.Text.Json;

namespace Unnameko.Desktop;

internal sealed class PipeClient : IAsyncDisposable
{
    private readonly string _pipeName;
    private readonly SemaphoreSlim _writeLock = new(1, 1);
    private readonly CancellationTokenSource _stop = new();
    private NamedPipeClientStream? _stream;
    private System.IO.StreamReader? _reader;
    private System.IO.StreamWriter? _writer;

    public event Action<JsonElement>? MessageReceived;
    public event Action<bool>? ConnectionChanged;

    public bool IsConnected => _stream?.IsConnected == true;

    public PipeClient(string pipeName = "unnameko-agent-v1")
    {
        _pipeName = pipeName;
    }

    public async Task RunAsync()
    {
        var nextLaunchAttempt = DateTimeOffset.MinValue;
        while (!_stop.IsCancellationRequested)
        {
            try
            {
                await ConnectAsync(_stop.Token);
                ConnectionChanged?.Invoke(true);
                nextLaunchAttempt = DateTimeOffset.MinValue;
                await SendAsync(new { type = "hello", request_id = Guid.NewGuid().ToString("N") });
                await ReadLoopAsync(_stop.Token);
            }
            catch (OperationCanceledException) when (_stop.IsCancellationRequested)
            {
                break;
            }
            catch
            {
                ConnectionChanged?.Invoke(false);
                DisposeStream();
                if (DateTimeOffset.UtcNow >= nextLaunchAttempt)
                {
                    CoreLauncher.TryStart();
                    nextLaunchAttempt = DateTimeOffset.UtcNow.AddSeconds(5);
                }
                try
                {
                    await Task.Delay(1200, _stop.Token);
                }
                catch (OperationCanceledException)
                {
                    break;
                }
            }
        }
        ConnectionChanged?.Invoke(false);
    }

    public async Task SendAsync(object payload)
    {
        if (_writer is null || !IsConnected)
        {
            throw new IOException("桌面核心尚未连接");
        }
        var json = JsonSerializer.Serialize(payload);
        await _writeLock.WaitAsync(_stop.Token);
        try
        {
            await _writer.WriteLineAsync(json);
            await _writer.FlushAsync(_stop.Token);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    private async Task ConnectAsync(CancellationToken cancellationToken)
    {
        DisposeStream();
        _stream = new NamedPipeClientStream(
            ".", _pipeName, PipeDirection.InOut, PipeOptions.Asynchronous);
        await _stream.ConnectAsync(900, cancellationToken);
        _reader = new StreamReader(_stream, new UTF8Encoding(false), false, 8192, leaveOpen: true);
        _writer = new StreamWriter(_stream, new UTF8Encoding(false), 8192, leaveOpen: true)
        {
            AutoFlush = true,
        };
    }

    private async Task ReadLoopAsync(CancellationToken cancellationToken)
    {
        while (!cancellationToken.IsCancellationRequested && _reader is not null)
        {
            var line = await _reader.ReadLineAsync(cancellationToken);
            if (line is null)
            {
                throw new EndOfStreamException();
            }
            using var document = JsonDocument.Parse(line);
            MessageReceived?.Invoke(document.RootElement.Clone());
        }
    }

    private void DisposeStream()
    {
        _reader?.Dispose();
        _writer?.Dispose();
        _stream?.Dispose();
        _reader = null;
        _writer = null;
        _stream = null;
    }

    public ValueTask DisposeAsync()
    {
        _stop.Cancel();
        DisposeStream();
        _writeLock.Dispose();
        _stop.Dispose();
        return ValueTask.CompletedTask;
    }
}
