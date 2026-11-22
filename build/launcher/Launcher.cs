// TIA MCP 控制台 启动器
//
// 背景：部分电脑（含装火绒等安全软件的机器）上 Chromium 沙箱初始化会直接失败，
// 进程在加载 main.js 之前就以 0x80000003 中止，表现为“双击没有任何反应”。
// Windows 无法给 exe 预置命令行参数，所以在 exe 与 Electron 之间放一个极小的启动器：
// 由它设置 ELECTRON_DISABLE_SANDBOX 并附带 --no-sandbox 启动真正的控制台，
// 保证双击（无参数、无环境变量）也能正常打开。

using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;

[assembly: AssemblyTitle("TIA MCP 控制台")]
[assembly: AssemblyDescription("TIA MCP 控制台启动器")]
[assembly: AssemblyProduct("TIA MCP 控制台")]
[assembly: AssemblyCompany("机器人协会")]
[assembly: AssemblyCopyright("Copyright © 2026 机器人协会（仅限本版修改）")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

internal static class Program
{
    private const string HostExeName = "tia-mcp-console.exe";

    [STAThread]
    private static int Main(string[] args)
    {
        string baseDir = AppDomain.CurrentDomain.BaseDirectory;
        string hostExe = Path.Combine(baseDir, HostExeName);
        if (!File.Exists(hostExe))
        {
            return 2;
        }

        var commandLine = new StringBuilder();
        bool hasNoSandbox = false;
        foreach (string arg in args)
        {
            if (string.Equals(arg, "--no-sandbox", StringComparison.OrdinalIgnoreCase))
            {
                hasNoSandbox = true;
            }
            commandLine.Append('"').Append(arg.Replace("\"", "\\\"")).Append("\" ");
        }
        if (!hasNoSandbox)
        {
            commandLine.Append("--no-sandbox");
        }

        var startInfo = new ProcessStartInfo(hostExe, commandLine.ToString().Trim())
        {
            UseShellExecute = false,
            WorkingDirectory = baseDir,
        };
        startInfo.EnvironmentVariables["ELECTRON_DISABLE_SANDBOX"] = "1";
        // 部分终端工具会把 ELECTRON_RUN_AS_NODE=1 写进全局环境（例如某些 AI 工具链），
        // 那样 Electron 会把自己当成 Node 运行、窗口根本不出现，这里强制清掉。
        startInfo.EnvironmentVariables.Remove("ELECTRON_RUN_AS_NODE");

        try
        {
            Process.Start(startInfo);
            return 0;
        }
        catch
        {
            return 1;
        }
    }
}
