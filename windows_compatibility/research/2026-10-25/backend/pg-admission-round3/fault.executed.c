#define _GNU_SOURCE
#include "postgres.h"
#include "fmgr.h"
#include <dlfcn.h>
#include <fcntl.h>
#include <signal.h>
#include <stdlib.h>
#include <unistd.h>
#include <stdio.h>
#include <string.h>
#undef snprintf
PG_MODULE_MAGIC;
static int is_exe(const char *name) {
 char buf[4096]; ssize_t n=readlink("/proc/self/exe",buf,sizeof(buf)-1);
 if(n<0)return 0;buf[n]=0;char *base=strrchr(buf,'/');return strcmp(base?base+1:buf,name)==0;
}
static void gate(const char *stage) {
 const char *wanted=getenv("G0_HOOK_STAGE"), *home=getenv("G0_HOOK_HOME");
 if(!wanted||!home||strcmp(wanted,stage))return;
 char path[4096], line[160];snprintf(path,sizeof(path),"%s/gate",home);
 int fd=open(path,O_WRONLY|O_CREAT|O_EXCL,0600);
 if(fd<0)return;
 int n=snprintf(line,sizeof(line),"%s %d %d\n",stage,getpid(),getppid());write(fd,line,n);close(fd);
 raise(SIGSTOP);
}
pid_t fork(void) {
 static pid_t (*real_fork)(void);if(!real_fork)real_fork=dlsym(RTLD_NEXT,"fork");
 if(is_exe("pg_ctl"))gate("pgctl-before-fork");
 return real_fork();
}
pid_t setsid(void) {
 pid_t (*real_setsid)(void)=dlsym(RTLD_NEXT,"setsid");pid_t result=real_setsid();
 if(result>=0&&is_exe("pg_ctl"))gate("pgctl-child-after-setsid");
 return result;
}
__attribute__((constructor)) static void loaded(void) {
 /* pg_ctl also invokes postgres -V; pause only its actual private -D process. */
 char argv[8192];int fd=open("/proc/self/cmdline",O_RDONLY);ssize_t n=fd<0?0:read(fd,argv,sizeof(argv));if(fd>=0)close(fd);
 const char *home=getenv("G0_HOOK_HOME");
 if(is_exe("postgres")&&home&&n>0&&memmem(argv,n,home,strlen(home)))gate("postgres-exec");
 if(is_exe("initdb")&&home&&n>0&&memmem(argv,n,home,strlen(home)))gate("initdb-exec");
 const char *pgdata=getenv("PGDATA");
 if(is_exe("postgres")&&home&&pgdata&&strstr(pgdata,home)==pgdata&&n>0&&memmem(argv,n,"--boot",6))gate("initdb-bootstrap");
 if(is_exe("pg_ctl")&&home&&n>0&&memmem(argv,n,home,strlen(home))&&memmem(argv,n,"stop",4))gate("pgctl-stop-exec");
}
void _PG_init(void) {if(is_exe("postgres"))gate("postmaster-before-ready");}
__attribute__((noreturn)) void exit(int status) {
 if(status==0&&is_exe("pg_ctl"))gate("pgctl-after-ready");
 void (*real_exit)(int)=dlsym(RTLD_NEXT,"exit");real_exit(status);__builtin_unreachable();
}
