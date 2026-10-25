#define _GNU_SOURCE
#include <dlfcn.h>
#include <unistd.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
// Test-only errno injection in this one subprocess; never fill a filesystem.
ssize_t write(int fd,const void *data,size_t count) {
 static ssize_t (*real_write)(int,const void*,size_t);static int partial;
 if (!real_write) real_write=dlsym(RTLD_NEXT,"write");
 char link[64],target[4096];snprintf(link,sizeof(link),"/proc/self/fd/%d",fd);ssize_t length=readlink(link,target,sizeof(target)-1);
 if (length>=0) {
  target[length]=0;size_t size=strlen(target);
  if (strstr(target,"/.local/share/pulsara/runtime/.install-")&&size>=12&&!strcmp(target+size-12,"/payload.tar")) {
   if (!partial) {partial=1;const char note[]="PBF_ENOSPC injected after actual prefix write\n";real_write(2,note,sizeof(note)-1);return real_write(fd,data,count>4096?4096:count);}
   errno=ENOSPC;return -1;
  }
 }
 return real_write(fd,data,count);
}
#include <fcntl.h>
ssize_t splice(int in,loff_t *offin,int out,loff_t *offout,size_t count,unsigned flags) {
 static ssize_t (*real_splice)(int,loff_t*,int,loff_t*,size_t,unsigned);static int partial;
 if (!real_splice) real_splice=dlsym(RTLD_NEXT,"splice");
 char link[64],target[4096];snprintf(link,sizeof(link),"/proc/self/fd/%d",out);ssize_t length=readlink(link,target,sizeof(target)-1);
 if(length>=0) {target[length]=0;size_t size=strlen(target);if(strstr(target,"/.local/share/pulsara/runtime/.install-")&&size>=12&&!strcmp(target+size-12,"/payload.tar")) {
  if(!partial){partial=1;const char note[]="PBF_ENOSPC injected after actual prefix write via splice\n";write(2,note,sizeof(note)-1);return real_splice(in,offin,out,offout,count>4096?4096:count,flags);}errno=ENOSPC;return -1;
 }}return real_splice(in,offin,out,offout,count,flags);
}
