#define _GNU_SOURCE
#include <dlfcn.h>
#include <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <string.h>
// Test-only allocation failure for this subprocess's private installer output.
static int reject(const char *p) {
 size_t n=strlen(p);return strstr(p,"/.local/share/pulsara/runtime/.install-")&&n>=12&&!strcmp(p+n-12,"/payload.tar");
}
int open64(const char *p,int flags,...) {
 static int (*real_open)(const char*,int,...);if(!real_open)real_open=dlsym(RTLD_NEXT,"open64");
 if(reject(p)){const char note[]="PBF_ENOSPC injected payload open allocation\n";write(2,note,sizeof(note)-1);errno=ENOSPC;return -1;}
 mode_t mode=0;if(flags&O_CREAT){va_list ap;va_start(ap,flags);mode=va_arg(ap,mode_t);va_end(ap);}return real_open(p,flags,mode);
}
int open(const char *p,int flags,...) {
 static int (*real_open)(const char*,int,...);if(!real_open)real_open=dlsym(RTLD_NEXT,"open");
 if(reject(p)){const char note[]="PBF_ENOSPC injected payload open allocation\n";write(2,note,sizeof(note)-1);errno=ENOSPC;return -1;}
 mode_t mode=0;if(flags&O_CREAT){va_list ap;va_start(ap,flags);mode=va_arg(ap,mode_t);va_end(ap);}return real_open(p,flags,mode);
}
