using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

internal static class InteropChild
{
    [StructLayout(LayoutKind.Sequential)] private struct Startup
    {
        public int size; public IntPtr reserved, desktop, title;
        public uint x, y, width, height, charsX, charsY, fill, flags;
        public ushort show, reservedBytes;
        public IntPtr reservedData, input, output, error;
    }
    [StructLayout(LayoutKind.Sequential)] private struct ProcessInfo
    { public IntPtr process, thread; public int pid, tid; }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CreateProcess(string executable, StringBuilder command,
        IntPtr ps, IntPtr ts, bool inherit, uint flags, IntPtr env, string cwd,
        ref Startup startup, out ProcessInfo process);
    [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool TerminateProcess(IntPtr handle, uint code);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool GetProcessTimes(IntPtr handle, out long creation, out long exit, out long kernel, out long user);
    [DllImport("kernel32.dll")] private static extern uint WaitForSingleObject(IntPtr handle, uint timeout);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool GetProcessHandleCount(IntPtr handle, out uint count);
    private static int Main(string[] args)
    {
        if (args[0] == "terminate" || args[0] == "handles")
        {
            IntPtr handle = OpenProcess(0x101001, false, Int32.Parse(args[1]));
            if (handle == IntPtr.Zero) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            try
            {
                long creation, exit, kernel, user;
                if (!GetProcessTimes(handle, out creation, out exit, out kernel, out user)
                    || creation.ToString() != args[2] || WaitForSingleObject(handle, 0) != 258)
                    throw new InvalidOperationException("Test target identity changed");
                if (args[0] == "handles")
                {
                    uint count;
                    if (!GetProcessHandleCount(handle, out count)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                    Console.WriteLine(count); return 0;
                }
                if (!TerminateProcess(handle, 9)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                if (WaitForSingleObject(handle, 10000) != 0) throw new InvalidOperationException("Exact target did not exit");
                return 0;
            }
            finally { CloseHandle(handle); }
        }
        if (args[0] == "tail")
        {
            string executable = Process.GetCurrentProcess().MainModule.FileName;
            Startup startup = new Startup { size = Marshal.SizeOf(typeof(Startup)) };
            ProcessInfo child;
            if (!CreateProcess(executable, new StringBuilder("\"" + executable + "\" sleep " + args[1]),
                IntPtr.Zero, IntPtr.Zero, false, 0x08000000, IntPtr.Zero, null, ref startup, out child))
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            CloseHandle(child.thread); CloseHandle(child.process);
            Console.WriteLine(child.pid); Console.Out.Flush();
            return 0;
        }
        if (args[0] == "sleep") { Thread.Sleep(Int32.Parse(args[1])); return 0; }
        if (args[0] == "echo") { Console.Write(Console.In.ReadToEnd()); return 0; }
        throw new ArgumentException("Unknown test fixture mode");
    }
}
