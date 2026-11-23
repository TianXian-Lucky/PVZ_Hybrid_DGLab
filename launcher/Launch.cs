using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Windows.Forms;

[assembly: AssemblyTitle("受伤反馈启动器")]
[assembly: AssemblyDescription("植物大战僵尸杂交版 · 独立桌面应用启动器")]
[assembly: AssemblyProduct("受伤反馈")]
[assembly: AssemblyVersion("1.0.0.6")]
[assembly: AssemblyFileVersion("1.0.0.6")]

internal static class Launch
{
    private const string Title = "受伤反馈";
    private const string InitialStep = "正在准备本机运行环境…";
    private const int SmokeTimeoutMs = 75000;

    [STAThread]
    private static int Main(string[] args)
    {
        bool smoke = args.Length == 1 && args[0] == "--desktop-smoke";
        bool prepare = args.Length == 1 && args[0] == "--prepare-only";
        bool headless = smoke || prepare;
        try
        {
            if (headless)
            {
                try { Console.OutputEncoding = new UTF8Encoding(false); }
                catch (IOException) { /* No attached console; inherited pipes still work. */ }
            }
            else
            {
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                if (args.Length != 0)
                {
                    ShowError("不支持这组启动参数。请直接双击「启动.exe」。");
                    return 2;
                }
            }

            string repoRoot = Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory);
            if (headless)
            {
                string executable = EnsureRuntime(repoRoot, delegate(string step)
                {
                    if (!String.IsNullOrWhiteSpace(step)) Console.Error.WriteLine(step);
                });
                return prepare ? 0 : OpenApp(executable, true);
            }

            using (var context = new ProgressContext(repoRoot))
            {
                Application.Run(context);
                return context.Result;
            }
        }
        catch (Exception error)
        {
            if (headless) Console.Error.WriteLine(ErrorText(error));
            else ShowError(ErrorText(error));
            return 1;
        }
    }

    private static string EnsureRuntime(string repoRoot, Action<string> report)
    {
        string executable = RuntimeInstaller.Ensure(repoRoot, report);
        if (String.IsNullOrWhiteSpace(executable) || !Path.IsPathRooted(executable) ||
            !String.Equals(Path.GetFileName(executable), "electron.exe", StringComparison.OrdinalIgnoreCase) ||
            !File.Exists(executable))
            throw new InvalidOperationException("运行环境没有准备完成。请确认目录可写，再启动一次。");

        string installed = Path.GetFullPath(executable);
        if (!File.Exists(Path.Combine(Path.GetDirectoryName(installed), "resources", "app", "package.json")))
            throw new InvalidOperationException("运行环境缺少桌面应用文件。请重新完整解压发布包，再双击「启动.exe」。");
        return installed;
    }

    private static int OpenApp(string executable, bool smoke)
    {
        string arguments = String.Empty;
        if (smoke)
        {
            string profile = Path.Combine(Path.GetTempPath(), "PVZ-Feedback-smoke-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(profile);
            arguments = "--user-data-dir=\"" + profile + "\" --disable-background-networking --desktop-smoke";
        }

        var start = new ProcessStartInfo
        {
            FileName = executable,
            WorkingDirectory = Path.GetDirectoryName(executable),
            Arguments = arguments,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = smoke,
            RedirectStandardError = smoke
        };
        if (smoke)
        {
            start.StandardOutputEncoding = Encoding.UTF8;
            start.StandardErrorEncoding = Encoding.UTF8;
        }

        var remove = new List<string>();
        foreach (string name in start.EnvironmentVariables.Keys)
        {
            if (name.StartsWith("ELECTRON_", StringComparison.OrdinalIgnoreCase) ||
                name.StartsWith("NODE_", StringComparison.OrdinalIgnoreCase) ||
                String.Equals(name, "CHROME_LOG_FILE", StringComparison.OrdinalIgnoreCase))
                remove.Add(name);
        }
        foreach (string name in remove) start.EnvironmentVariables.Remove(name);

        using (var app = new Process { StartInfo = start })
        {
            ManualResetEventSlim drained = null;
            if (smoke)
            {
                drained = new ManualResetEventSlim(false);
                int ended = 0;
                app.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e)
                {
                    if (e.Data != null) Console.Out.WriteLine(e.Data);
                    else if (Interlocked.Increment(ref ended) == 2) drained.Set();
                };
                app.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e)
                {
                    if (e.Data != null) Console.Error.WriteLine(e.Data);
                    else if (Interlocked.Increment(ref ended) == 2) drained.Set();
                };
            }
            if (!app.Start())
                throw new InvalidOperationException("桌面应用没有启动。请重新打开「启动.exe」。");

            if (!smoke) return 0; // Disposing the handle doesn't stop the desktop app.
            app.BeginOutputReadLine();
            app.BeginErrorReadLine();
            if (!app.WaitForExit(SmokeTimeoutMs))
            {
                // This object owns only the isolated smoke process we launched, never a player instance.
                try { if (!app.HasExited) app.Kill(); }
                catch (InvalidOperationException) { }
                catch (Win32Exception) { }
                Console.Error.WriteLine("桌面自检超过 75 秒，已停止等待。");
                return 124;
            }
            drained.Wait(1500); // Bound output draining independently of any inherited open pipe.
            return app.ExitCode;
        }
    }

    private static string ErrorText(Exception error)
    {
        var startError = error as Win32Exception;
        if (startError != null)
            return "无法打开桌面应用（Windows 错误 " + startError.NativeErrorCode +
                "）。请确认压缩包已完整解压、当前用户可访问该目录，并按 Windows 的提示处理后重试。";
        var accessError = error as UnauthorizedAccessException;
        if (accessError != null)
            return "当前用户无法写入运行环境目录。请将完整发布包解压到自己可写的文件夹，再双击「启动.exe」。";
        string message = error.Message;
        if (String.IsNullOrWhiteSpace(message)) message = "准备桌面应用时遇到错误。";
        return message + Environment.NewLine + Environment.NewLine +
            "请确认发布包已完整解压、当前账户可写入目录。首次启动若需下载，请检查能否访问发布渠道，然后重试。";
    }

    private static void ShowError(string text)
    {
        MessageBox.Show(text, Title + " · 启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
    }

    private sealed class ProgressContext : ApplicationContext
    {
        private readonly object gate = new object();
        private readonly Stopwatch elapsed = Stopwatch.StartNew();
        private readonly System.Windows.Forms.Timer ticker;
        private ProgressWindow window;
        private string step = InitialStep;
        private Exception error;
        private bool done;

        internal int Result { get; private set; }

        internal ProgressContext(string root)
        {
            ticker = new System.Windows.Forms.Timer { Interval = 90 };
            ticker.Tick += Tick;
            ticker.Start();

            var worker = new Thread(delegate()
            {
                Exception problem = null;
                try
                {
                    string executable = EnsureRuntime(root, delegate(string update)
                    {
                        if (!String.IsNullOrWhiteSpace(update)) SetStep(update);
                    });
                    SetStep("正在打开受伤反馈…");
                    OpenApp(executable, false);
                }
                catch (Exception failure) { problem = failure; }
                lock (gate)
                {
                    error = problem;
                    done = true;
                }
            });
            worker.IsBackground = true;
            worker.Start();
        }

        private void SetStep(string update)
        {
            lock (gate) step = update;
        }

        private void Tick(object sender, EventArgs args)
        {
            string activeStep;
            Exception failure;
            bool finished;
            lock (gate)
            {
                activeStep = step;
                failure = error;
                finished = done;
            }

            if (finished)
            {
                ticker.Stop();
                if (window != null) window.Finish();
                Result = failure == null ? 0 : 1;
                if (failure != null) ShowError(ErrorText(failure));
                ExitThread();
            }
            else if (elapsed.ElapsedMilliseconds >= 640)
            {
                if (window == null) window = new ProgressWindow();
                window.SetStep(activeStep);
                if (!window.Visible) window.Show();
            }
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                ticker.Dispose();
                if (window != null) window.Dispose();
            }
            base.Dispose(disposing);
        }
    }

    private sealed class ProgressWindow : Form
    {
        private readonly Label status;
        private bool finished;

        internal ProgressWindow()
        {
            Text = Title;
            ClientSize = new Size(430, 166);
            BackColor = Color.White;
            ForeColor = Color.FromArgb(42, 36, 45);
            Font = SystemFonts.MessageBoxFont;
            AutoScaleMode = AutoScaleMode.Dpi;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            StartPosition = FormStartPosition.CenterScreen;
            ControlBox = false;
            ShowInTaskbar = true;

            var heading = new Label
            {
                Text = "受伤反馈",
                Bounds = new Rectangle(22, 18, 382, 28),
                Font = new Font(Font.FontFamily, 14, FontStyle.Bold)
            };
            status = new Label
            {
                Text = InitialStep,
                Bounds = new Rectangle(22, 54, 382, 24),
                ForeColor = Color.FromArgb(111, 63, 84),
                AutoEllipsis = true
            };
            var bar = new ProgressBar
            {
                Bounds = new Rectangle(22, 86, 382, 10),
                Style = ProgressBarStyle.Marquee,
                MarqueeAnimationSpeed = 32
            };
            var hint = new Label
            {
                Text = "首次启动可能需要下载运行环境，完成后会自动打开。",
                Bounds = new Rectangle(22, 111, 382, 35),
                ForeColor = Color.FromArgb(94, 86, 98)
            };
            Controls.AddRange(new Control[] { heading, status, bar, hint });
        }

        internal void Finish()
        {
            finished = true;
            Close();
        }

        protected override void OnFormClosing(FormClosingEventArgs args)
        {
            if (!finished && args.CloseReason == CloseReason.UserClosing) args.Cancel = true;
            base.OnFormClosing(args);
        }

        internal void SetStep(string value)
        {
            if (status.Text != value) status.Text = value;
        }
    }
}

