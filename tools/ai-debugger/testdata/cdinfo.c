/* CDINFO - shows what DOS sees of the CD in the first CD-ROM drive, through
 * the MSCDEX interface (INT 2Fh, AH=15h; SHSUCDX provides it): the drive
 * letter, the disc's tracks (data or audio, start in min:sec.frame), and,
 * for a disc with audio, whether the drive plays it: it plays the first
 * audio track for a second and checks that the play position moves.
 * Ends with "CDINFO: OK" when the drive answers (and plays, if it can).
 *
 * Build (Open Watcom): wcl -ms -bt=dos -0 -os cdinfo.c
 */

#include <dos.h>
#include <i86.h>
#include <stdio.h>
#include <string.h>

#pragma pack(1)
typedef struct {
    unsigned char  len, unit, cmd;
    unsigned short status;
    unsigned char  reserved[8];
} req_header;

typedef struct {
    req_header     h;
    unsigned char  media;
    void far      *buf;
    unsigned short count;
    unsigned short start;
    void far      *volume;
} ioctl_req;

typedef struct {
    req_header    h;
    unsigned char mode; /* 1: Red Book addresses */
    unsigned long start;
    unsigned long count;
} play_req;
#pragma pack()

#define STATUS_ERROR 0x8000
#define STATUS_BUSY  0x0200

static int drive; /* 0 = A: */

static unsigned short
request(void far *req)
{
    union REGS   r;
    struct SREGS s;

    segread(&s);
    r.x.ax = 0x1510;
    r.x.cx = drive;
    r.x.bx = FP_OFF(req);
    s.es   = FP_SEG(req);
    int86x(0x2F, &r, &r, &s);
    return ((req_header far *) req)->status;
}

static unsigned short
ioctl_in(unsigned char *cb, unsigned short n)
{
    ioctl_req q;

    memset(&q, 0, sizeof(q));
    q.h.len = sizeof(q);
    q.h.cmd = 3; /* IOCTL input */
    q.buf   = cb;
    q.count = n;
    return request(&q);
}

static unsigned long
ticks(void)
{
    return *(unsigned long far *) MK_FP(0x40, 0x6C);
}

static void
q_channel(unsigned char *q)
{
    memset(q, 0, 11);
    q[0] = 0x0C; /* Audio Q-channel info */
    ioctl_in(q, 11);
}

int
main(void)
{
    union REGS     r;
    unsigned char  cb[16], q1[11], q2[11];
    int            lo, hi, t, audio = 0;
    unsigned long  start = 0, t0;
    unsigned short st;
    play_req       p;
    req_header     stop;

    r.x.ax = 0x1500;
    r.x.bx = 0;
    int86(0x2F, &r, &r);
    if (r.x.bx == 0) {
        printf("CDINFO: no CD-ROM drive (no MSCDEX or SHSUCDX)\n");
        return 1;
    }
    drive = r.x.cx;
    printf("CDINFO: %u CD-ROM drive(s), the first is %c:\n", r.x.bx, 'A' + drive);

    /* After a disc change the driver answers "invalid disk change" (0Fh)
       once: ask again. */
    for (t = 0; t < 3; t++) {
        memset(cb, 0, sizeof(cb));
        cb[0] = 0x0A; /* Audio disk info: lowest and highest track, lead-out */
        st    = ioctl_in(cb, 7);
        if (!(st & STATUS_ERROR) || (st & 0xFF) != 0x0F)
            break;
    }
    if (st & STATUS_ERROR) {
        printf("CDINFO: the drive answers error %02Xh (no disc?)\n", st & 0xFF);
        return 1;
    }
    lo = cb[1];
    hi = cb[2];
    printf("Tracks %d-%d, lead-out at %02u:%02u.%02u\n", lo, hi, cb[5], cb[4], cb[3]);
    for (t = lo; t <= hi; t++) {
        memset(cb, 0, sizeof(cb));
        cb[0] = 0x0B; /* Audio track info */
        cb[1] = (unsigned char) t;
        st    = ioctl_in(cb, 7);
        if (st & STATUS_ERROR) {
            printf("  track %2d  error %02Xh\n", t, st & 0xFF);
            continue;
        }
        printf("  track %2d  %02u:%02u.%02u  %s\n", t, cb[4], cb[3], cb[2], (cb[6] & 0x40) ? "data" : "audio");
        if (!(cb[6] & 0x40) && !audio) {
            audio = t;
            start = *(unsigned long *) &cb[2];
        }
    }
    if (!audio) {
        printf("CDINFO: OK (no audio tracks)\n");
        return 0;
    }

    memset(&p, 0, sizeof(p));
    p.h.len = sizeof(p);
    p.h.cmd = 132; /* Play audio */
    p.mode  = 1;
    p.start = start;
    p.count = 75L * 10;
    st      = request(&p);
    if (st & STATUS_ERROR) {
        printf("CDINFO: playing track %d fails, error %02Xh\n", audio, st & 0xFF);
        return 1;
    }
    q_channel(q1);
    for (t0 = ticks(); ticks() - t0 < 18;)
        ;
    q_channel(q2);
    printf("Playing track %d: %02u:%02u.%02u then %02u:%02u.%02u into it\n", audio, q1[4], q1[5], q1[6], q2[4], q2[5],
           q2[6]);
    memset(&stop, 0, sizeof(stop));
    stop.len = sizeof(stop);
    stop.cmd = 133; /* Stop audio */
    request(&stop);
    if (memcmp(q1 + 4, q2 + 4, 3) == 0) {
        printf("CDINFO: the play position doesn't move\n");
        return 1;
    }
    printf("CDINFO: OK (audio plays)\n");
    return 0;
}
