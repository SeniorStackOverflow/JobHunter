// SPDX-License-Identifier: GPL-2.0-only
// Derived from SeniorStackOverflow/a51-kvm probes/docker/launcher.c at
// ac9f151387808a836b684972e3a3af323641110b (GPL-2.0-only).
// Bind the complete native root onto the namespace root before chroot. This
// makes setns(CLONE_NEWNS), and therefore runc exec/healthchecks, enter that
// root rather than Android's enclosing rootfs. Changes are namespace-local.
#define _GNU_SOURCE
#include <sched.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/wait.h>
static void ck(int r,const char*s){if(r<0){perror(s);exit(1);}}
static void dir(const char*p){if(mkdir(p,0755)<0&&errno!=EEXIST){perror(p);exit(1);}}
static int runsh(const char *cmd){pid_t p=fork();ck(p,"fork shell");if(!p){execl("/system/bin/sh","sh","-c",cmd,NULL);_exit(127);}int st;ck(waitpid(p,&st,0),"wait shell");return !WIFEXITED(st)||WEXITSTATUS(st);}
static void parents(const char *p){char path[1024];snprintf(path,sizeof(path),"%s",p);for(char *s=path+1;*s;s++)if(*s=='/'){*s=0;dir(path);*s='/';}dir(path);}
static void bindroot(const char *root,const char *path,int rec){char target[1024];snprintf(target,sizeof(target),"%s%s",root,path);parents(target);ck(mount(path,target,NULL,MS_BIND|(rec?MS_REC:0),NULL),path);}
static void rootfile(const char *root,const char *name,const char *data){char p[1024];snprintf(p,sizeof(p),"%s%s",root,name);int f=open(p,O_CREAT|O_TRUNC|O_WRONLY,0600);ck(f,p);size_t n=strlen(data);if(write(f,data,n)!=(ssize_t)n){perror(p);exit(1);}close(f);}
int main(int argc,char**argv){
 if(argc<2){fprintf(stderr,"usage: launcher command [args...]\n");return 2;}
 ck(unshare(CLONE_NEWNS|CLONE_NEWNET),"unshare mount+network");
 ck(mount(NULL,"/",NULL,MS_PRIVATE|MS_REC,NULL),"private mount propagation");
 ck(mount("tmpfs","/sys/fs/cgroup","tmpfs",MS_NOSUID|MS_NODEV|MS_NOEXEC,"mode=755,size=1m"),"private cgroup mount");
 const char*names[]={"cpu","cpuacct","cpuset","memory","freezer","devices","pids","blkio"};
 const char*android[]={"/dev/cpuctl","/acct","/dev/cpuset","/dev/memcg","/dev/freezer",NULL,NULL,NULL};
 for(int i=0;i<8;i++){char p[256];snprintf(p,sizeof(p),"/sys/fs/cgroup/%s",names[i]);dir(p);if(android[i])ck(mount(android[i],p,NULL,MS_BIND|MS_REC,NULL),names[i]);else ck(mount("cgroup",p,"cgroup",0,names[i]),names[i]);}
 // Docker owns a private netns; the host-side supervisor adds a scoped uplink.
 setenv("PATH","/data/local/tmp/codex-a51-docker-bin:/system/bin:/system/xbin",1);
 setenv("DOCKER_TMPDIR","/data/local/tmp/codex-a51-docker/tmp",1);
 // Go's Linux defaults cannot discover Android's CA directory in this layout.
 setenv("SSL_CERT_DIR","/system/etc/security/cacerts:/apex/com.android.conscrypt/cacerts",1);
 dir("/data/local/tmp/codex-a51-docker");dir("/data/local/tmp/codex-a51-docker/tmp");
 if(runsh("/system/bin/ip link set lo up"))return 1;
 dir("/data/local/tmp/codex-a51-docker/data");
 if(runsh("/system/bin/losetup -j /data/local/tmp/codex-a51-docker-storage.ext4 | /system/bin/grep -q /dev/")) {
  int attached=0;
  /* Android ueventd may create a newly allocated loop node asynchronously. */
  for(int attempt=0;attempt<5;attempt++) {
   if(!runsh("/system/bin/losetup -f /data/local/tmp/codex-a51-docker-storage.ext4")){attached=1;break;}
   usleep(250000);
  }
  if(!attached)return 1;
 }
 int pipefd[2];ck(pipe(pipefd),"pipe");pid_t child=fork();ck(child,"fork loop lookup");
 if(!child){dup2(pipefd[1],STDOUT_FILENO);close(pipefd[0]);close(pipefd[1]);execl("/system/bin/losetup","losetup","-j","/data/local/tmp/codex-a51-docker-storage.ext4",NULL);_exit(127);}
 close(pipefd[1]);FILE *loops=fdopen(pipefd[0],"r");
 if(!loops){perror("loop lookup");return 1;}
 char line[512];if(!fgets(line,sizeof(line),loops))return 1;fclose(loops);int st;ck(waitpid(child,&st,0),"wait loop lookup");if(st!=0)return 1;
 char *colon=strchr(line,':');if(!colon)return 1;*colon=0;
 ck(mount(line,"/data/local/tmp/codex-a51-docker/data","ext4",MS_NOATIME,NULL),"encrypted-file-backed ext4 Docker data");
 fprintf(stderr,"Docker data loop: %s\n",line);
 const char *root="/data/local/tmp/codex-a51-docker-rootfs";
 parents(root);
 ck(mount(root,root,NULL,MS_BIND|MS_REC,NULL),"Linux root mount boundary");
 const char *dirs[]={"/proc","/sys","/dev","/system","/apex","/vendor","/linkerconfig","/data/local/tmp/codex-a51-docker-bin","/data/local/tmp/codex-a51-docker"};
 for(unsigned i=0;i<sizeof(dirs)/sizeof(dirs[0]);i++)bindroot(root,dirs[i],1);
 const char *local[]={"/bin","/etc","/run","/tmp","/var","/var/run"};
 for(unsigned i=0;i<sizeof(local)/sizeof(local[0]);i++){char p[1024];snprintf(p,sizeof(p),"%s%s",root,local[i]);parents(p);}
 char link[1024];snprintf(link,sizeof(link),"%s/bin/sh",root);if(symlink("/system/bin/sh",link)<0&&errno!=EEXIST){perror("shell link");return 1;}
 rootfile(root,"/etc/passwd","root:x:0:0:root:/root:/bin/sh\n");rootfile(root,"/etc/group","root:x:0:\n");
 rootfile(root,"/etc/resolv.conf","nameserver 1.1.1.1\n");
 rootfile(root,"/etc/nsswitch.conf","passwd: files\ngroup: files\nhosts: files dns\n");
 int rootns=open("/proc/self/ns/mnt",O_RDONLY|O_CLOEXEC);ck(rootns,"open native mount namespace");
 ck(mount(root,"/",NULL,MS_BIND|MS_REC,NULL),"native namespace root boundary");
 ck(setns(rootns,CLONE_NEWNS),"adopt namespace filesystem root");close(rootns);
 ck(chroot("/"),"native Linux filesystem layout");ck(chdir("/"),"chroot cwd");
 ck(execvp(argv[1],argv+1),"exec daemon");return 1;
}
