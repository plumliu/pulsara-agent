using System;
using System.ComponentModel;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.IO;

internal static class Fault
{
    [DllImport("kernel32.dll",SetLastError=true)] static extern IntPtr OpenProcess(uint access,bool inherit,int pid);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetProcessTimes(IntPtr p,out long c,out long e,out long k,out long u);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool TerminateProcess(IntPtr p,uint code);
    [DllImport("kernel32.dll",SetLastError=true)] static extern uint WaitForSingleObject(IntPtr p,uint ms);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr p);
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr OpenJobObject(uint a,bool i,string n);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool QueryInformationJobObject(IntPtr j,int k,IntPtr b,uint s,out uint r);
    static IntPtr Open(int pid,string expected)
    {
        IntPtr p=OpenProcess(0x101001,false,pid);
        if(p==IntPtr.Zero)throw new Win32Exception(Marshal.GetLastWin32Error());
        long c,e,k,u;
        if(!GetProcessTimes(p,out c,out e,out k,out u)||WaitForSingleObject(p,0)!=258||(expected!="-"&&c.ToString()!=expected))throw new Exception("Exact process identity changed");
        return p;
    }
    static int[] Members(IntPtr job)
    {
        int count=16;
        while(true){int size=checked(8+count*IntPtr.Size);IntPtr b=Marshal.AllocHGlobal(size);
            try{uint returned;bool okay=QueryInformationJobObject(job,3,b,(uint)size,out returned);
                int assigned=Marshal.ReadInt32(b),listed=Marshal.ReadInt32(b,4);
                if(okay){int[] p=new int[listed];for(int i=0;i<listed;i++)p[i]=(int)Marshal.ReadIntPtr(b,8+i*IntPtr.Size);return p;}
                int error=Marshal.GetLastWin32Error();if(error!=234)throw new Win32Exception(error);count=Math.Max(checked(count*2),assigned);
            }finally{Marshal.FreeHGlobal(b);}}
    }
    static int Main(string[] a)
    {
        if(a[0]=="watch"){
            IntPtr job=OpenJobObject(4,false,a[1]);if(job==IntPtr.Zero)throw new Win32Exception(Marshal.GetLastWin32Error());
            var held=new List<IntPtr>();var identity=new List<string>();
            try{foreach(int pid in Members(job)){IntPtr p=Open(pid,"-");held.Add(p);long c,e,k,u;GetProcessTimes(p,out c,out e,out k,out u);identity.Add("{\"pid\":"+pid+",\"creation\":\""+c+"\"}");}}
            finally{CloseHandle(job);}
            // PROCESS handles do not retain an extra user Job handle.
            File.WriteAllText(a[2],"{\"held\":["+String.Join(",",identity)+"],\"jobHandleClosedBeforeFault\":true}");
            var waits=new List<string>();
            try{foreach(IntPtr p in held){uint wait=WaitForSingleObject(p,60000);waits.Add(wait.ToString());if(wait!=0)throw new Exception("Exact scope member did not exit");}
                File.WriteAllText(a[3],"{\"waits\":["+String.Join(",",waits)+"],\"signalSent\":false}");return 0;}
            finally{foreach(IntPtr p in held)CloseHandle(p);}}
        if(a[0]=="members"){
            IntPtr job=OpenJobObject(4,false,a[1]);if(job==IntPtr.Zero)throw new Win32Exception(Marshal.GetLastWin32Error());
            try{Console.WriteLine("["+String.Join(",",Members(job))+"]");return 0;}finally{CloseHandle(job);}}
        if(a[0]=="pair"){
            IntPtr first=Open(Int32.Parse(a[1]),a[2]),second=Open(Int32.Parse(a[3]),a[4]),child=Open(Int32.Parse(a[5]),"-");
            try{long c,e,k,u;GetProcessTimes(child,out c,out e,out k,out u);
                if(!TerminateProcess(first,9)||!TerminateProcess(second,9))throw new Win32Exception(Marshal.GetLastWin32Error());
                uint one=WaitForSingleObject(first,10000),two=WaitForSingleObject(second,10000),tail=WaitForSingleObject(child,10000);
                Console.WriteLine("{\"ownerWait\":"+one+",\"monitorWait\":"+two+",\"childPid\":"+a[5]+",\"childCreation\":\""+c+"\",\"childWait\":"+tail+"}");
                return one==0&&two==0&&tail==0?0:2;
            }finally{CloseHandle(child);CloseHandle(second);CloseHandle(first);}}
        throw new ArgumentException("Unknown exact fault mode");
    }
}
