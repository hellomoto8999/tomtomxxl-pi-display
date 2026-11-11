/* TomTom ttsystem packer - kernel first (matches original layout) */
#include <stdint.h>
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/types.h>
#include <sys/stat.h>
#include "md5.h"
#include "blowfish.h"

unsigned char magic[4]       = {0x54, 0x54, 0x42, 0x4c};
unsigned char rootaddr[4]    = {0x00, 0x00, 0x00, 0x31}; /* 0x31000000 */
unsigned char kerneladdr[4]  = {0x00, 0x00, 0x70, 0x31}; /* 0x31700000 */
unsigned char startseq[12]   = {0x00,0x00,0x00,0x00, 0x00,0x00,0x70,0x31, 0x00,0x00,0x00,0x30};
unsigned char ttkey[16]      = {0xD8,0x88,0xd3,0x13, 0xed,0x83,0xba,0xad,
                                0x9c,0xf4,0x1b,0x50, 0xb3,0x43,0xfa,0xdd};

static unsigned int filesize(char *n){struct stat s; if(stat(n,&s)){perror(n);exit(1);} return s.st_size;}
static char *readfile(char *n,unsigned int sz){FILE*f=fopen(n,"rb");char*b=malloc(sz);
  if(!f||!b){perror(n);exit(1);} if(fread(b,1,sz,f)!=sz){fprintf(stderr,"read err %s\n",n);exit(1);} fclose(f); return b;}

static void sign(char *buf,unsigned int size,char *sig){
  struct MD5Context md5; BLOWFISH_CTX bf;
  MD5Init(&md5); MD5Update(&md5,(unsigned char*)buf,size); MD5Final((unsigned char*)sig,&md5);
  Blowfish_Init(&bf,ttkey,16);
  Blowfish_Encrypt(&bf,&((uint32_t*)sig)[0],&((uint32_t*)sig)[1]);
  Blowfish_Encrypt(&bf,&((uint32_t*)sig)[2],&((uint32_t*)sig)[3]);
}

int main(int argc,char**argv){
  if(argc!=3){fprintf(stderr,"usage: %s initramfs.gz zImage > ttsystem\n",argv[0]);return 1;}
  uint32_t rootsize=filesize(argv[1]), kernelsize=filesize(argv[2]);
  char *root=readfile(argv[1],rootsize), *kernel=readfile(argv[2],kernelsize);
  unsigned char rootsig[16], kernelsig[16];
  sign(root,rootsize,(char*)rootsig); sign(kernel,kernelsize,(char*)kernelsig);

  write(STDOUT_FILENO, magic, 4);
  /* kernel first */
  write(STDOUT_FILENO,&kernelsize,4);
  write(STDOUT_FILENO,kerneladdr,4);
  write(STDOUT_FILENO,kernel,kernelsize);
  write(STDOUT_FILENO,kernelsig,16);
  /* root/initramfs second */
  write(STDOUT_FILENO,&rootsize,4);
  write(STDOUT_FILENO,rootaddr,4);
  write(STDOUT_FILENO,root,rootsize);
  write(STDOUT_FILENO,rootsig,16);
  write(STDOUT_FILENO,startseq,12);
  return 0;
}
