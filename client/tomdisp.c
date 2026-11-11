/* tomdisp - framebuffer client/diagnostic for TomTom (OpenTom) */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <linux/fb.h>
#include <linux/input.h>
#include <sys/time.h>
#include <sys/select.h>
#include <sys/sysmacros.h>
#include <termios.h>
#include <time.h>
#include <linux/soundcard.h>

/* 2.6.13 headers lack these */
#ifndef EVIOCGABS
struct input_absinfo { int value; int minimum; int maximum; int fuzz; int flat; };
#define EVIOCGABS(abs) _IOR('E', 0x40 + (abs), struct input_absinfo)
#endif
#ifndef BTN_TOUCH
#define BTN_TOUCH 0x14a
#endif

static int ffd;
static struct fb_var_screeninfo v;
static struct fb_fix_screeninfo f;
static unsigned char *fbmem;
static long fbsize;
static long yoff = 0;

static int open_fb(void){
    if (access("/dev/fb0", F_OK) != 0)
        mknod("/dev/fb0", S_IFCHR|0600, makedev(29,0));
    ffd = open("/dev/fb0", O_RDWR);
    if (ffd < 0){ perror("open /dev/fb0"); return -1; }
    if (ioctl(ffd, FBIOGET_VSCREENINFO, &v)) { perror("FBIOGET_VSCREENINFO"); return -1; }
    if (ioctl(ffd, FBIOGET_FSCREENINFO, &f)) { perror("FBIOGET_FSCREENINFO"); return -1; }
    /* force the visible page to page 0 */
    v.yoffset = 0;
    v.activate = FB_ACTIVATE_NOW;
    ioctl(ffd, FBIOPUT_VSCREENINFO, &v);
    ioctl(ffd, FBIOPAN_DISPLAY, &v);
    ioctl(ffd, FBIOGET_VSCREENINFO, &v);
    yoff = v.yoffset;
    fprintf(stderr, "[fb] yoffset=%ld yres_virtual=%u line=%u\n",
            yoff, v.yres_virtual, f.line_length);
    fbsize = f.line_length * v.yres_virtual;
    fbmem = mmap(0, fbsize, PROT_READ|PROT_WRITE, MAP_SHARED, ffd, 0);
    if (fbmem == MAP_FAILED){ perror("mmap"); fbmem=NULL; return -1; }
    return 0;
}

static void blit(const unsigned char *src, long srclen){
    long y, row = f.line_length;
    long want = (long)row * v.yres;
    if (srclen < want) return;
    for (y=0; y<v.yres; y++)
        memcpy(fbmem + (yoff + y)*row, src + y*row, row);
}

static void blit_rect(int x, int y, int w, int h, const unsigned char *src){
    int r;
    long row = f.line_length;
    for (r=0; r<h && (y+r)<(int)v.yres; r++)
        memcpy(fbmem + (yoff + y + r)*row + (long)x*2, src + (long)r*w*2, w*2);
}

/* ---- touchscreen (evdev event0) ---------------------------------------- */
static int touch_fd = -1;
/* calibrated raw touch extents for this panel (both axes inverted) */
static int axlo=100, axhi=930, aylo=137, ayhi=874;

static int open_touch(void){
    mkdir("/dev/input", 0755);
    if (access("/dev/input/event0", F_OK) != 0)
        mknod("/dev/input/event0", S_IFCHR|0600, makedev(13,64));
    int fd = open("/dev/input/event0", O_RDONLY|O_NONBLOCK);
    if (fd < 0){ perror("open event0"); return -1; }
    const char *e;
    if ((e=getenv("TT_XLO"))) axlo=atoi(e);
    if ((e=getenv("TT_XHI"))) axhi=atoi(e);
    if ((e=getenv("TT_YLO"))) aylo=atoi(e);
    if ((e=getenv("TT_YHI"))) ayhi=atoi(e);
    printf("touch: event0 calibrated x[%d..%d] y[%d..%d] (inverted)\n", axlo, axhi, aylo, ayhi);
    fflush(stdout);
    return fd;
}

static void send_tap(int cs, int rx, int ry){
    unsigned int msg[3];
    int sx = (axhi - rx) * ((int)v.xres - 1) / (axhi - axlo);
    int sy = (ayhi - ry) * ((int)v.yres - 1) / (ayhi - aylo);
    if (sx < 0) sx = 0; if (sx >= (int)v.xres) sx = (int)v.xres - 1;
    if (sy < 0) sy = 0; if (sy >= (int)v.yres) sy = (int)v.yres - 1;
    printf("tap raw %d,%d -> %d,%d\n", rx, ry, sx, sy); fflush(stdout);
    msg[0] = 0x43554F54u;            /* "TOUC" */
    msg[1] = (unsigned int)sx;
    msg[2] = (unsigned int)sy;
    write(cs, msg, sizeof(msg));
}

static void handle_touch(int cs){
    struct input_event ev;
    static int down=0, sx=0, sy=0;
    static struct timeval tp;
    while (read(touch_fd, &ev, sizeof(ev)) == (ssize_t)sizeof(ev)){
        if (ev.type != EV_ABS) continue;
        if (ev.code == ABS_X) sx = ev.value;
        else if (ev.code == ABS_Y) sy = ev.value;
        else if (ev.code == 24 /* ABS_PRESSURE */){
            if (ev.value > 0){
                if (!down){ down = 1; gettimeofday(&tp, NULL); }
            } else if (down){
                struct timeval tn; double dt;
                down = 0; gettimeofday(&tn, NULL);
                dt = (tn.tv_sec - tp.tv_sec) + (tn.tv_usec - tp.tv_usec)/1000000.0;
                if (dt < 0.8) send_tap(cs, sx, sy);
            }
        }
    }
}

static int write_bright(int val){
    int fd = open("/sys/class/backlight/s3c/brightness", O_WRONLY);
    if (fd < 0) return -1;
    char b[8]; int l = snprintf(b, sizeof(b), "%d", val);
    int r = write(fd, b, l); close(fd); return r;
}

static int read_full(int fd, unsigned char *buf, long n){
    long got=0; int r;
    while (got<n){ r=read(fd, buf+got, n-got); if(r<=0) return -1; got+=r; }
    return 0;
}

int main(int argc, char **argv){
    const char *mode = argc>1 ? argv[1] : "info";
    int need_fb = !strcmp(mode,"info") || !strcmp(mode,"test") || !strcmp(mode,"serve");
    if (need_fb && open_fb()) return 1;

    if (!strcmp(mode,"info")){
        printf("xres=%u yres=%u bpp=%u line=%u vxres=%u vyres=%u red=%u/%u green=%u/%u blue=%u/%u\n",
            v.xres, v.yres, v.bits_per_pixel, f.line_length, v.xres_virtual, v.yres_virtual,
            v.red.offset, v.red.length, v.green.offset, v.green.length, v.blue.offset, v.blue.length);
        printf("fix: smem_start=%lx smem_len=%u type=%u visual=%u\n",
            (unsigned long)f.smem_start, f.smem_len, f.type, f.visual);
        return 0;
    }

    if (!strcmp(mode,"test")){
        unsigned long x,y;
        for (y=0;y<v.yres;y++)
            for (x=0;x<v.xres;x++){
                if (v.bits_per_pixel==16){
                    unsigned short c;
                    if (x < v.xres/3) c = 0xF800;       /* red */
                    else if (x < 2*v.xres/3) c = 0x07E0; /* green */
                    else c = 0x001F;                     /* blue */
                    c = (y/24)%2 ? (unsigned short)~c : c;
                    *((unsigned short*)(fbmem + y*f.line_length) + x) = c;
                } else if (v.bits_per_pixel==32){
                    unsigned int c;
                    if (x < v.xres/3) c=0x00FF0000;
                    else if (x < 2*v.xres/3) c=0x0000FF00;
                    else c=0x000000FF;
                    *((unsigned int*)(fbmem + y*f.line_length) + x) = c;
                } else {
                    *((unsigned char*)(fbmem + y*f.line_length) + x) = (unsigned char)(x & 0xFF);
                }
            }
        printf("test pattern written (%ux%u %ubpp)\n", v.xres, v.yres, v.bits_per_pixel);
        return 0;
    }

    if (!strcmp(mode,"bright")){
        int val = argc>2 ? atoi(argv[2]) : 50;
        int fd = open("/sys/class/backlight/s3c/brightness", O_WRONLY);
        if (fd < 0){ perror("open brightness"); return 1; }
        char b[8]; int l = snprintf(b, sizeof(b), "%d", val);
        if (write(fd, b, l) < 0) perror("write brightness");
        close(fd);
        printf("brightness=%d\n", val);
        return 0;
    }

    if (!strcmp(mode,"decode")){
        int fd = open(argv[2], O_RDONLY);
        if (fd < 0){ perror("open"); return 1; }
        struct input_event ev;
        while (read(fd, &ev, sizeof(ev)) == (ssize_t)sizeof(ev))
            printf("%u %u %d\n", ev.type, ev.code, ev.value);
        close(fd);
        return 0;
    }

    if (!strcmp(mode,"gpsprobe")){
        int baud = argc>2 ? atoi(argv[2]) : 115200;
        speed_t sp = baud==4800 ? B4800 : baud==9600 ? B9600 :
                     baud==38400 ? B38400 : B115200;
        if (access("/dev/ttySAC1", F_OK) != 0)
            mknod("/dev/ttySAC1", S_IFCHR|0600, makedev(204,65));
        int fd = open("/dev/ttySAC1", O_RDONLY|O_NONBLOCK|O_NOCTTY);
        if (fd < 0){ perror("open ttySAC1"); return 1; }
        struct termios t;
        if (tcgetattr(fd, &t)==0){
            cfmakeraw(&t); cfsetispeed(&t, sp); cfsetospeed(&t, sp);
            tcsetattr(fd, TCSANOW, &t);
        }
        printf("gpsprobe %d baud, 5s...\n", baud); fflush(stdout);
        time_t end = time(NULL) + 5; int total = 0;
        unsigned char buf[256];
        while (time(NULL) < end){
            int n = read(fd, buf, sizeof(buf));
            int i;
            if (n > 0){ total += n;
                for (i=0;i<n;i++){ unsigned char c=buf[i];
                    if (c=='\n'||c=='\r'||(c>=32&&c<127)) putchar(c); }
                fflush(stdout);
            }
            usleep(100000);
        }
        printf("\n[gpsprobe] %d bytes\n", total);
        close(fd);
        return 0;
    }

    if (!strcmp(mode,"beep")){
        int hz = argc>2 ? atoi(argv[2]) : 880;
        /* buzzer first */
        if (access("/dev/buz", F_OK) != 0)
            mknod("/dev/buz", S_IFCHR|0600, makedev(252,0));
        int b = open("/dev/buz", O_WRONLY);
        if (b >= 0){
            printf("buz ok\n"); fflush(stdout);
            write(b, "\1", 1); usleep(150000); write(b, "\0", 1);
            close(b); return 0;
        }
        /* OSS dsp fallback */
        if (access("/dev/dsp", F_OK) != 0)
            mknod("/dev/dsp", S_IFCHR|0600, makedev(14,3));
        int d = open("/dev/dsp", O_WRONLY);
        if (d < 0){ perror("open /dev/dsp"); return 1; }
        int fmt = AFMT_S16_LE, ch = 1, rate = 22050;
        ioctl(d, SNDCTL_DSP_SETFMT, &fmt);
        ioctl(d, SNDCTL_DSP_CHANNELS, &ch);
        ioctl(d, SNDCTL_DSP_SPEED, &rate);
        int n = rate / 2, i, half = rate / (2 * hz);
        if (half < 1) half = 1;
        short *buf = malloc(n * sizeof(short));
        for (i=0;i<n;i++) buf[i] = ((i / half) & 1) ? 7000 : -7000;
        if (write(d, buf, n*sizeof(short)) < 0) perror("write dsp");
        close(d); free(buf);
        printf("dsp beep %dHz\n", hz);
        return 0;
    }

    if (!strcmp(mode,"send")){
        const char *file = argv[2], *host = argv[3];
        int port = argc>4 ? atoi(argv[4]) : 9000;
        int fd = open(file, O_RDONLY);
        if (fd < 0){ perror("open"); return 1; }
        int so = socket(AF_INET, SOCK_STREAM, 0);
        struct sockaddr_in sa; memset(&sa, 0, sizeof(sa));
        sa.sin_family = AF_INET; sa.sin_port = htons(port);
        inet_aton(host, &sa.sin_addr);
        if (connect(so, (struct sockaddr*)&sa, sizeof(sa))){ perror("connect"); return 1; }
        char buf[4096]; ssize_t n; long total = 0;
        while ((n = read(fd, buf, sizeof(buf))) > 0){ write(so, buf, n); total += n; }
        close(so); close(fd);
        printf("sent %ld bytes\n", total);
        return 0;
    }

    if (!strcmp(mode,"play")){
        int port = argc>2 ? atoi(argv[2]) : 5556;
        if (access("/dev/dsp", F_OK) != 0)
            mknod("/dev/dsp", S_IFCHR|0600, makedev(14,3));
        int ls = socket(AF_INET, SOCK_STREAM, 0);
        int one = 1; setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
        struct sockaddr_in sa; memset(&sa, 0, sizeof(sa));
        sa.sin_family = AF_INET; sa.sin_addr.s_addr = INADDR_ANY; sa.sin_port = htons(port);
        if (bind(ls,(struct sockaddr*)&sa,sizeof(sa))){ perror("bind"); return 1; }
        listen(ls,1);
        printf("audio listening on %d\n", port); fflush(stdout);
        unsigned char *buf = malloc(16384);
        while (1){
            int cs = accept(ls, NULL, NULL);
            if (cs < 0) continue;
            int d = open("/dev/dsp", O_WRONLY);
            if (d < 0){ perror("open /dev/dsp"); close(cs); continue; }
            int fmt = AFMT_S16_LE, ch = 2, rate = 44100;
            ioctl(d, SNDCTL_DSP_SETFMT, &fmt);
            ioctl(d, SNDCTL_DSP_CHANNELS, &ch);
            ioctl(d, SNDCTL_DSP_SPEED, &rate);
            printf("audio client: fmt=%d ch=%d rate=%d\n", fmt, ch, rate); fflush(stdout);
            int n;
            while ((n = recv(cs, buf, 16384, 0)) > 0)
                if (write(d, buf, n) < 0) break;
            close(d); close(cs);
            printf("audio client done\n"); fflush(stdout);
        }
    }

    if (!strcmp(mode,"serve")){
        int port = argc>2 ? atoi(argv[2]) : 5555;
        int ls = socket(AF_INET, SOCK_STREAM, 0);
        int one=1; setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
        struct sockaddr_in sa; memset(&sa,0,sizeof(sa));
        sa.sin_family=AF_INET; sa.sin_addr.s_addr=INADDR_ANY; sa.sin_port=htons(port);
        if (bind(ls,(struct sockaddr*)&sa,sizeof(sa))){ perror("bind"); return 1; }
        listen(ls,1);
        printf("listening on %d, fb %ux%u %ubpp line=%u\n", port, v.xres, v.yres, v.bits_per_pixel, f.line_length);
        fflush(stdout);
        touch_fd = open_touch();
        long want = (long)f.line_length * v.yres;
        unsigned char *buf = malloc(want);
        while (1){
            int cs = accept(ls, NULL, NULL);
            if (cs < 0) continue;
            /* handshake: send geometry */
            unsigned short hdr[6] = { v.xres, v.yres, v.bits_per_pixel, f.line_length, 0, 0 };
            write(cs, hdr, sizeof(hdr));
            while (1){
                fd_set rf; FD_ZERO(&rf); FD_SET(cs, &rf);
                int maxfd = cs;
                if (touch_fd >= 0){ FD_SET(touch_fd, &rf); if (touch_fd > maxfd) maxfd = touch_fd; }
                if (select(maxfd+1, &rf, NULL, NULL, NULL) < 0){
                    if (errno == EINTR) continue;
                    break;
                }
                if (touch_fd >= 0 && FD_ISSET(touch_fd, &rf)) handle_touch(cs);
                if (!FD_ISSET(cs, &rf)) continue;
                unsigned int tag;
                if (read_full(cs, (unsigned char*)&tag, 4)) break;
                if (tag == 0xFFFFFFFFu){            /* control */
                    unsigned char ctl[2];
                    if (read_full(cs, ctl, 2)) break;
                    if (ctl[0] == 1) write_bright(ctl[1]);
                    continue;
                }
                if (tag == 0xEEEEEEEEu){            /* dirty rectangles */
                    unsigned short nrect, i;
                    if (read_full(cs, (unsigned char*)&nrect, 2)) break;
                    for (i=0; i<nrect; i++){
                        unsigned short hh[4];       /* x,y,w,h */
                        if (read_full(cs, (unsigned char*)hh, 8)) break;
                        int w = hh[2], h = hh[3];
                        if (w<=0 || h<=0 || (long)w*(long)h*2 > want) break;
                        if (read_full(cs, buf, (long)w*h*2)) break;
                        blit_rect(hh[0], hh[1], w, h, buf);
                    }
                    continue;
                }
                if (tag==0 || tag>want) break;      /* full frame */
                if (read_full(cs, buf, tag)) break;
                blit(buf, tag);
            }
            close(cs);
            printf("client disconnected\n"); fflush(stdout);
        }
    }
    fprintf(stderr,"usage: %s info|test|serve [port]\n", argv[0]);
    return 0;
}
