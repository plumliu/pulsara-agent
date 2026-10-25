// Windows owns Job/process objects. Linux owns command admission, Unix relay,
// original process groups and result policy; this helper parses no WSL protocol.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Text;
using System.Threading;

[DataContract]
internal sealed class Frame
{
    [DataMember(EmitDefaultValue = false)] public string operation;
    [DataMember(EmitDefaultValue = false)] public string type;
    [DataMember(EmitDefaultValue = false)] public string distribution;
    [DataMember(EmitDefaultValue = false)] public string user;
    [DataMember(EmitDefaultValue = false)] public string python;
    [DataMember(EmitDefaultValue = false)] public string control;
    [DataMember(EmitDefaultValue = false)] public string job;
    [DataMember(EmitDefaultValue = false)] public string creation;
    [DataMember(EmitDefaultValue = false)] public string reason;
    [DataMember(EmitDefaultValue = false)] public int pid;
    [DataMember(EmitDefaultValue = false)] public int anchorPid;
    [DataMember(EmitDefaultValue = false)] public int monitorPid;
    [DataMember(EmitDefaultValue = false)] public string monitorCreation;
}

internal static class Native
{
    internal const uint Infinite = 0xffffffff, WaitTimeout = 258;
    internal const uint QueryProcess = 0x1000, Synchronize = 0x100000;
    internal const uint Suspended = 4, NoWindow = 0x08000000, ExtendedStartup = 0x00080000;
    internal const uint StdHandles = 0x100, KillOnJobClose = 0x2000;
    internal const uint Inherit = 1, DuplicateSameAccess = 2;
    [StructLayout(LayoutKind.Sequential)] internal struct FileTime { public uint low, high; }
    [StructLayout(LayoutKind.Sequential)] internal struct Security { public int size; public IntPtr descriptor; public int inherit; }
    [StructLayout(LayoutKind.Sequential)] internal struct Startup
    {
        public int size; public IntPtr reserved, desktop, title;
        public uint x, y, width, height, charsX, charsY, fill, flags;
        public ushort show, reservedBytes; public IntPtr reservedData, input, output, error;
    }
    [StructLayout(LayoutKind.Sequential)] internal struct StartupEx { public Startup startup; public IntPtr attributes; }
    [StructLayout(LayoutKind.Sequential)] internal struct ProcessInfo { public IntPtr process, thread; public int pid, tid; }
    [StructLayout(LayoutKind.Sequential)] internal struct BasicLimits
    {
        public long processTime, jobTime; public uint flags;
        public UIntPtr minWorkingSet, maxWorkingSet; public uint activeLimit;
        public UIntPtr affinity; public uint priority, scheduling;
    }
    [StructLayout(LayoutKind.Sequential)] internal struct IoCounters { public ulong a, b, c, d, e, f; }
    [StructLayout(LayoutKind.Sequential)] internal struct ExtendedLimits
    {
        public BasicLimits basic; public IoCounters io;
        public UIntPtr processMemory, jobMemory, peakProcessMemory, peakJobMemory;
    }
    [DllImport("kernel32.dll", SetLastError = true)] internal static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll")] internal static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll")] internal static extern IntPtr GetStdHandle(int kind);
    [DllImport("kernel32.dll", SetLastError = true)] internal static extern bool GetProcessTimes(IntPtr process, out FileTime creation, out FileTime exit, out FileTime kernel, out FileTime user);
    [DllImport("kernel32.dll", SetLastError = true)] internal static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
    [DllImport("kernel32.dll", SetLastError = true)] internal static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError = true)] internal static extern uint WaitForMultipleObjects(uint count, IntPtr[] handles, bool all, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError = true)] internal static extern bool GetExitCodeProcess(IntPtr process, out uint code);
    [DllImport("kernel32.dll", SetLastError = true)] internal static extern bool DuplicateHandle(IntPtr sourceProcess, IntPtr source, IntPtr targetProcess, out IntPtr target, uint access, bool inherit, uint options);
    [DllImport("kernel32.dll", SetLastError = true)] internal static extern bool CreatePipe(out IntPtr read, out IntPtr write, ref Security security, uint size);
    [DllImport("kernel32.dll", SetLastError = true)] internal static extern bool SetHandleInformation(IntPtr handle, uint mask, uint flags);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] internal static extern IntPtr CreateFile(string name, uint access, uint sharing, ref Security security, uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] internal static extern IntPtr CreateJobObject(IntPtr security, string name);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] internal static extern IntPtr OpenJobObject(uint access, bool inherit, string name);
    [DllImport("kernel32.dll", SetLastError = true)] internal static extern bool SetInformationJobObject(IntPtr job, int kind, ref ExtendedLimits value, uint size);
    [DllImport("kernel32.dll", SetLastError = true)] internal static extern bool QueryInformationJobObject(IntPtr job, int kind, IntPtr value, uint size, out uint returned);
    [DllImport("kernel32.dll", SetLastError = true)] internal static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", SetLastError = true)] internal static extern bool IsProcessInJob(IntPtr process, IntPtr job, out bool member);
    [DllImport("kernel32.dll", SetLastError = true)] internal static extern bool TerminateJobObject(IntPtr job, uint code);
    [DllImport("kernel32.dll", SetLastError = true)] internal static extern bool TerminateProcess(IntPtr process, uint code);
    [DllImport("kernel32.dll", SetLastError = true)] internal static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll", SetLastError = true)] internal static extern bool InitializeProcThreadAttributeList(IntPtr list, int count, uint flags, ref IntPtr size);
    [DllImport("kernel32.dll", SetLastError = true)] internal static extern bool UpdateProcThreadAttribute(IntPtr list, uint flags, IntPtr attribute, IntPtr value, IntPtr size, IntPtr previous, IntPtr returned);
    [DllImport("kernel32.dll")] internal static extern void DeleteProcThreadAttributeList(IntPtr list);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] internal static extern bool CreateProcess(string executable, StringBuilder command, IntPtr processSecurity, IntPtr threadSecurity, bool inherit, uint flags, IntPtr environment, string directory, ref StartupEx startup, out ProcessInfo process);

    internal static void Check(bool okay) { if (!okay) throw new Win32Exception(Marshal.GetLastWin32Error()); }
    internal static IntPtr Valid(IntPtr handle)
    {
        if (handle == IntPtr.Zero || handle == new IntPtr(-1)) throw new Win32Exception(Marshal.GetLastWin32Error());
        return handle;
    }
    internal static void Close(ref IntPtr handle) { if (handle != IntPtr.Zero) { CloseHandle(handle); handle = IntPtr.Zero; } }
    internal static string Creation(IntPtr handle)
    {
        FileTime created, exited, kernel, user;
        Check(GetProcessTimes(handle, out created, out exited, out kernel, out user));
        return (((ulong)created.high << 32) | created.low).ToString(System.Globalization.CultureInfo.InvariantCulture);
    }
    internal static IntPtr InheritedCopy(IntPtr handle)
    {
        IntPtr copy;
        Check(DuplicateHandle(GetCurrentProcess(), handle, GetCurrentProcess(), out copy, 0, true, DuplicateSameAccess));
        return copy;
    }
    internal static string Quote(string value)
    {
        if (value == null || value.IndexOf('\0') >= 0) throw new ArgumentException("Invalid process argument");
        if (value.Length > 0 && value.IndexOfAny(new char[] { ' ', '\t', '"' }) < 0) return value;
        StringBuilder result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char ch in value)
        {
            if (ch == '\\') { slashes++; continue; }
            if (ch == '"') result.Append('\\', slashes * 2 + 1);
            else result.Append('\\', slashes);
            slashes = 0; result.Append(ch);
        }
        result.Append('\\', slashes * 2); result.Append('"'); return result.ToString();
    }
    internal static ProcessInfo Launch(string executable, string[] args, IntPtr input, IntPtr output, IntPtr error, IntPtr[] handles, bool suspended)
    {
        StringBuilder command = new StringBuilder(Quote(executable));
        foreach (string arg in args) command.Append(' ').Append(Quote(arg));
        // CreateProcessW includes the terminating NUL in its 32767 character boundary.
        if (command.Length >= 32767) throw new ArgumentException("Windows process argument limit exceeded");
        IntPtr size = IntPtr.Zero, list = IntPtr.Zero, array = IntPtr.Zero;
        bool initialized = false;
        try
        {
            InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref size);
            list = Marshal.AllocHGlobal(size);
            Check(InitializeProcThreadAttributeList(list, 1, 0, ref size)); initialized = true;
            array = Marshal.AllocHGlobal(handles.Length * IntPtr.Size);
            Marshal.Copy(handles, 0, array, handles.Length);
            Check(UpdateProcThreadAttribute(list, 0, new IntPtr(0x20002), array, new IntPtr(handles.Length * IntPtr.Size), IntPtr.Zero, IntPtr.Zero));
            StartupEx startup = new StartupEx();
            startup.startup.size = Marshal.SizeOf(typeof(StartupEx)); startup.startup.flags = StdHandles;
            startup.startup.input = input; startup.startup.output = output; startup.startup.error = error; startup.attributes = list;
            ProcessInfo process;
            Check(CreateProcess(executable, command, IntPtr.Zero, IntPtr.Zero, true, NoWindow | ExtendedStartup | (suspended ? Suspended : 0), IntPtr.Zero, null, ref startup, out process));
            return process;
        }
        finally
        {
            if (initialized) DeleteProcThreadAttributeList(list);
            if (array != IntPtr.Zero) Marshal.FreeHGlobal(array);
            if (list != IntPtr.Zero) Marshal.FreeHGlobal(list);
        }
    }
    internal static int[] Members(IntPtr job)
    {
        int count = 16;
        while (true)
        {
            int size = checked(8 + count * IntPtr.Size);
            IntPtr buffer = Marshal.AllocHGlobal(size);
            try
            {
                uint returned;
                bool okay = QueryInformationJobObject(job, 3, buffer, (uint)size, out returned);
                int assigned = Marshal.ReadInt32(buffer), listed = Marshal.ReadInt32(buffer, 4);
                if (okay)
                {
                    int[] pids = new int[listed];
                    for (int index = 0; index < listed; index++) pids[index] = (int)Marshal.ReadIntPtr(buffer, 8 + index * IntPtr.Size);
                    return pids;
                }
                int code = Marshal.GetLastWin32Error();
                if (code != 234) throw new Win32Exception(code);
                count = Math.Max(checked(count * 2), assigned);
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }
    }
    internal static void JoinJob(IntPtr job, IntPtr owner, out bool ownerLost)
    {
        Dictionary<int, IntPtr> held = new Dictionary<int, IntPtr>();
        ownerLost = false;
        try
        {
            while (true)
            {
                List<int> exited = new List<int>();
                foreach (KeyValuePair<int, IntPtr> entry in held)
                {
                    uint state = WaitForSingleObject(entry.Value, 0);
                    if (state == 0) exited.Add(entry.Key);
                    else if (state != WaitTimeout) throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                foreach (int pid in exited) { CloseHandle(held[pid]); held.Remove(pid); }
                if (owner != IntPtr.Zero && WaitForSingleObject(owner, 0) == 0 && !ownerLost)
                {
                    ownerLost = true; Check(TerminateJobObject(job, 1));
                }
                int[] pids = Members(job);
                foreach (int pid in pids)
                {
                    IntPtr previous;
                    if (held.TryGetValue(pid, out previous))
                    {
                        uint state = WaitForSingleObject(previous, 0);
                        if (state == WaitTimeout) continue;
                        if (state != 0) throw new Win32Exception(Marshal.GetLastWin32Error());
                        CloseHandle(previous); held.Remove(pid);
                    }
                    IntPtr handle = OpenProcess(QueryProcess | Synchronize, false, pid);
                    if (handle == IntPtr.Zero)
                    {
                        // A short-lived member can exit before OpenProcess; an
                        // access failure is not evidence that it has exited.
                        if (Marshal.GetLastWin32Error() == 87) continue;
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                    }
                    bool member; Check(IsProcessInJob(handle, job, out member));
                    if (!member) { CloseHandle(handle); continue; }
                    held.Add(pid, handle);
                }
                if (pids.Length == 0) break;
                Thread.Sleep(5);
            }
            foreach (IntPtr handle in held.Values)
                if (WaitForSingleObject(handle, Infinite) != 0) throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        finally { foreach (IntPtr handle in held.Values) CloseHandle(handle); }
    }
}

internal static class Program
{
    private static readonly UTF8Encoding Utf8 = new UTF8Encoding(false, true);
    private static string ReadLine()
    {
        StringBuilder line = new StringBuilder();
        while (true)
        {
            int ch = Console.In.Read();
            if (ch == '\n') return line.ToString();
            if (ch < 0)
            {
                if (line.Length == 0) return null;
                throw new EndOfStreamException("Partial interop control frame");
            }
            line.Append((char)ch);
        }
    }
    private static Frame Read(string line)
    {
        if (line == null) return null;
        using (MemoryStream stream = new MemoryStream(Utf8.GetBytes(line)))
            return (Frame)new DataContractJsonSerializer(typeof(Frame)).ReadObject(stream);
    }
    private static void Send(Frame frame)
    {
        using (MemoryStream stream = new MemoryStream())
        {
            new DataContractJsonSerializer(typeof(Frame)).WriteObject(stream, frame);
            Console.Out.WriteLine(Utf8.GetString(stream.ToArray())); Console.Out.Flush();
        }
    }
    private static void Require(string value) { if (String.IsNullOrEmpty(value) || value.IndexOf('\0') >= 0) throw new ArgumentException("Missing scope target"); }

    private static int Probe()
    {
        Frame request = Read(ReadLine()); Require(request.job);
        IntPtr job = Native.Valid(Native.OpenJobObject(4, false, request.job));
        try
        {
            bool member; Native.Check(Native.IsProcessInJob(Native.GetCurrentProcess(), job, out member));
            if (!member) throw new InvalidOperationException("Diagnostic process is outside its scope Job");
            Send(new Frame { type = "probe", pid = System.Diagnostics.Process.GetCurrentProcess().Id, creation = Native.Creation(Native.GetCurrentProcess()) });
            Console.In.ReadToEnd(); return 0;
        }
        finally { Native.Close(ref job); }
    }

    private static int Monitor(string[] args)
    {
        IntPtr job = new IntPtr(Int64.Parse(args[1])), owner = new IntPtr(Int64.Parse(args[2]));
        // This finite worker retains the exact inherited Job/owner handles so a
        // scope-helper crash still has a physical settlement observer. It owns
        // no command admission, task registry, durable state or separate lifecycle.
        try
        {
            bool lost; Native.JoinJob(job, owner, out lost);
            Send(new Frame { type = "joined", reason = lost ? "owner-lost" : "settled" });
            return 0;
        }
        finally { Native.Close(ref owner); Native.Close(ref job); }
    }

    private static int Scope()
    {
        Frame config = Read(ReadLine());
        if (config == null) return 0;
        Require(config.distribution); Require(config.user); Require(config.python); Require(config.control);
        string name = "Local\\PulsaraInterop-" + Guid.NewGuid().ToString("N");
        IntPtr job = IntPtr.Zero, anchorWrite = IntPtr.Zero, anchorRead = IntPtr.Zero, nul = IntPtr.Zero;
        IntPtr inheritedJob = IntPtr.Zero, inheritedOwner = IntPtr.Zero, inheritedOutput = IntPtr.Zero, inheritedError = IntPtr.Zero;
        Native.ProcessInfo anchor = new Native.ProcessInfo(), monitor = new Native.ProcessInfo();
        bool assigned = false, joined = false;
        try
        {
            job = Native.Valid(Native.CreateJobObject(IntPtr.Zero, name));
            Native.ExtendedLimits limits = new Native.ExtendedLimits(); limits.basic.flags = Native.KillOnJobClose;
            Native.Check(Native.SetInformationJobObject(job, 9, ref limits, (uint)Marshal.SizeOf(typeof(Native.ExtendedLimits))));
            Native.Security security = new Native.Security { size = Marshal.SizeOf(typeof(Native.Security)), inherit = 1 };
            Native.Check(Native.CreatePipe(out anchorRead, out anchorWrite, ref security, 0));
            Native.Check(Native.SetHandleInformation(anchorWrite, Native.Inherit, 0));
            nul = Native.Valid(Native.CreateFile("NUL", 0xc0000000, 3, ref security, 3, 0, IntPtr.Zero));
            inheritedError = Native.InheritedCopy(Native.GetStdHandle(-12));
            string wsl = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "wsl.exe");
            anchor = Native.Launch(wsl, new string[] { "-d", config.distribution, "-u", config.user, "--exec", config.python, "-m", "pulsara_agent.windows_interop.anchor", config.control }, anchorRead, nul, inheritedError, new IntPtr[] { anchorRead, nul, inheritedError }, true);
            Native.Check(Native.AssignProcessToJobObject(job, anchor.process)); assigned = true;
            inheritedJob = Native.InheritedCopy(job);
            inheritedOwner = Native.InheritedCopy(Native.GetCurrentProcess());
            inheritedOutput = Native.InheritedCopy(Native.GetStdHandle(-11));
            string executable = System.Diagnostics.Process.GetCurrentProcess().MainModule.FileName;
            monitor = Native.Launch(executable, new string[] { "--monitor", inheritedJob.ToInt64().ToString(), inheritedOwner.ToInt64().ToString() }, nul, inheritedOutput, inheritedError, new IntPtr[] { inheritedJob, inheritedOwner, inheritedOutput, inheritedError, nul }, false);
            Native.Close(ref inheritedJob); Native.Close(ref inheritedOwner); Native.Close(ref inheritedOutput); Native.Close(ref anchorRead);
            if (Native.ResumeThread(anchor.thread) == 0xffffffff) throw new Win32Exception(Marshal.GetLastWin32Error());
            Send(new Frame { type = "ready", job = name,
                pid = System.Diagnostics.Process.GetCurrentProcess().Id,
                creation = Native.Creation(Native.GetCurrentProcess()),
                anchorPid = anchor.pid, monitorPid = monitor.pid,
                monitorCreation = Native.Creation(monitor.process) });
            Queue<string> input = new Queue<string>();
            using (AutoResetEvent available = new AutoResetEvent(false))
            {
                Thread reader = new Thread(delegate()
                {
                    try
                    {
                        while (true)
                        {
                            string line = ReadLine();
                            lock (input) input.Enqueue(line);
                            available.Set(); if (line == null) return;
                        }
                    }
                    catch { lock (input) input.Enqueue(null); try { available.Set(); } catch (ObjectDisposedException) { } }
                });
                reader.IsBackground = true; reader.Start();
                while (true)
                {
                    uint signaled = Native.WaitForMultipleObjects(2, new IntPtr[] { monitor.process, available.SafeWaitHandle.DangerousGetHandle() }, false, Native.Infinite);
                    if (signaled == 0)
                    {
                        uint exit; Native.Check(Native.GetExitCodeProcess(monitor.process, out exit));
                        if (exit != 0)
                        {
                            Native.Check(Native.TerminateJobObject(job, 1)); bool unused; Native.JoinJob(job, IntPtr.Zero, out unused);
                            Send(new Frame { type = "joined", reason = "monitor-lost" });
                        }
                        joined = true; return 0;
                    }
                    if (signaled != 1) throw new Win32Exception(Marshal.GetLastWin32Error());
                    while (true)
                    {
                        string line;
                        lock (input) { if (input.Count == 0) break; line = input.Dequeue(); }
                        Frame request = Read(line);
                        if (request == null)
                        {
                            Send(new Frame { type = "failed", reason = "control-eof" });
                            Native.Check(Native.TerminateJobObject(job, 1));
                        }
                        else if (request.operation == "cancel") Native.Check(Native.TerminateJobObject(job, 1));
                        else if (request.operation == "finish") Native.Close(ref anchorWrite);
                        else if (request.operation == "verify")
                        {
                            IntPtr process = Native.Valid(Native.OpenProcess(Native.QueryProcess | Native.Synchronize, false, request.pid));
                            try
                            {
                                bool member; Native.Check(Native.IsProcessInJob(process, job, out member));
                                if (!member || Native.Creation(process) != request.creation || Native.WaitForSingleObject(process, 0) != Native.WaitTimeout)
                                    throw new InvalidOperationException("Diagnostic Windows identity does not match this live Job");
                                Send(new Frame { type = "verified", pid = request.pid, creation = request.creation });
                            }
                            finally { Native.Close(ref process); }
                        }
                        else throw new ArgumentException("Unsupported scope command");
                    }
                }
            }
        }
        finally
        {
            Native.Close(ref anchorWrite); Native.Close(ref anchorRead);
            if (job != IntPtr.Zero && !joined) Native.TerminateJobObject(job, 1);
            if (anchor.process != IntPtr.Zero && !assigned) Native.TerminateProcess(anchor.process, 1);
            if (anchor.process != IntPtr.Zero) Native.WaitForSingleObject(anchor.process, Native.Infinite);
            if (monitor.process != IntPtr.Zero) Native.WaitForSingleObject(monitor.process, Native.Infinite);
            Native.Close(ref anchor.thread); Native.Close(ref anchor.process); Native.Close(ref monitor.thread); Native.Close(ref monitor.process);
            Native.Close(ref inheritedJob); Native.Close(ref inheritedOwner); Native.Close(ref inheritedOutput); Native.Close(ref inheritedError); Native.Close(ref nul); Native.Close(ref job);
        }
    }

    private static int Main(string[] args)
    {
        Console.InputEncoding = Utf8; Console.OutputEncoding = Utf8;
        try
        {
            if (args.Length == 1 && args[0] == "--version") { Console.WriteLine("pulsara-interop 1"); return 0; }
            if (args.Length == 1 && args[0] == "--probe") return Probe();
            if (args.Length == 1 && args[0] == "--scope") return Scope();
            if (args.Length == 3 && args[0] == "--monitor") return Monitor(args);
            throw new ArgumentException("Unsupported interop helper mode");
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error.GetType().Name + ": " + error.Message); return 1;
        }
    }
}
