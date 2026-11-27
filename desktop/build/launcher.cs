// MagicPro.exe: starts the app with the embedded Python (python\pythonw.exe -m desktop).
// Built by desktop/build/build.ps1 with the C# compiler that ships with Windows (.NET Framework 4).
using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Windows.Forms;

[assembly: System.Reflection.AssemblyTitle("Magic Pro")]
[assembly: System.Reflection.AssemblyProduct("Magic Pro")]

static class Program
{
    [STAThread]
    static int Main(string[] args)
    {
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        string py = Path.Combine(dir, "python", "pythonw.exe");
        if (!File.Exists(py))
        {
            MessageBox.Show("Magic Pro is not installed correctly. Please run the installer again.", "Magic Pro",
                            MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        string extra = string.Join(" ", args.Select(a => "\"" + a.Replace("\"", "") + "\"").ToArray());
        var psi = new ProcessStartInfo(py, "-m desktop " + extra);
        psi.WorkingDirectory = dir;
        psi.UseShellExecute = false;
        psi.EnvironmentVariables.Remove("PYTHONPATH");
        psi.EnvironmentVariables.Remove("PYTHONHOME");
        Process.Start(psi);
        return 0;
    }
}
