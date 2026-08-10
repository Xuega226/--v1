using System.Windows;

namespace Unnameko.Desktop;

public partial class App : System.Windows.Application
{
    private MainWindow? _window;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        _window = new MainWindow();
        MainWindow = _window;
        _window.Show();
    }
}
