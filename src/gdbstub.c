/*
 * 86Box    A hypervisor and IBM PC system emulator that specializes in
 *          running old operating systems and software designed for IBM
 *          PC systems and compatibles from 1981 through fairly recent
 *          system designs based on the PCI bus.
 *
 *          This file is part of the 86Box distribution.
 *
 *          GDB stub server for remote debugging.
 *
 * Authors: RichardG, <richardg867@gmail.com>
 *
 *          Copyright 2022 RichardG.
 */
#include <inttypes.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#    ifndef __clang__
#        include <unistd.h>
#    else
#        include <io.h>
#        define ssize_t           long
#        define strtok_r(a, b, c) strtok_s(a, b, c)
#    endif
#    include <winsock2.h>
#    include <ws2tcpip.h>
#else
#    include <unistd.h>
#    include <arpa/inet.h>
#    include <netinet/in.h>
#    include <netinet/tcp.h>
#    include <sys/socket.h>
#    include <errno.h>
#endif
#ifndef MSG_NOSIGNAL /* Windows doesn't signal; macOS uses SO_NOSIGPIPE instead */
#    define MSG_NOSIGNAL 0
#endif
#ifdef _WIN32
#    define GDBSTUB_SHUT_RDWR SD_BOTH
#else
#    define GDBSTUB_SHUT_RDWR SHUT_RDWR
#endif
#define HAVE_STDARG_H
#include <86box/86box.h>
#include "cpu.h"
#include "x86.h"
#include "x86seg.h"
#include "x86seg_common.h"
#include "x86_flags.h"
#include "x87_sf.h"
#include "x87.h"
#include "x87_ops_conv.h"
#include <86box/io.h>
#include <86box/mem.h>
#include <86box/plat.h>
#include <86box/thread.h>
#include <86box/gdbstub.h>
#include <86box/keyboard.h>
#include <86box/mouse.h>
#include <86box/video.h>

#define FAST_RESPONSE(s)         \
    strcpy(client->response, s); \
    client->response_pos = sizeof(s) - 1;
#define FAST_RESPONSE_HEX(s) gdbstub_client_respond_hex(client, (uint8_t *) s, sizeof(s));

enum {
    GDB_SIGINT  = 2,
    GDB_SIGTRAP = 5
};

enum {
    GDB_REG_EAX = 0,
    GDB_REG_ECX,
    GDB_REG_EDX,
    GDB_REG_EBX,
    GDB_REG_ESP,
    GDB_REG_EBP,
    GDB_REG_ESI,
    GDB_REG_EDI,
    GDB_REG_EIP,
    GDB_REG_EFLAGS,
    GDB_REG_CS,
    GDB_REG_SS,
    GDB_REG_DS,
    GDB_REG_ES,
    GDB_REG_FS,
    GDB_REG_GS,
#if 0
    GDB_REG_FS_BASE,
    GDB_REG_GS_BASE,
#endif
    GDB_REG_CR0,
    GDB_REG_CR2,
    GDB_REG_CR3,
    GDB_REG_CR4,
    GDB_REG_EFER,
    GDB_REG_ST0,
    GDB_REG_ST1,
    GDB_REG_ST2,
    GDB_REG_ST3,
    GDB_REG_ST4,
    GDB_REG_ST5,
    GDB_REG_ST6,
    GDB_REG_ST7,
    GDB_REG_FCTRL,
    GDB_REG_FSTAT,
    GDB_REG_FTAG,
    GDB_REG_FISEG,
    GDB_REG_FIOFF,
    GDB_REG_FOSEG,
    GDB_REG_FOOFF,
    GDB_REG_FOP,
    GDB_REG_MM0,
    GDB_REG_MM1,
    GDB_REG_MM2,
    GDB_REG_MM3,
    GDB_REG_MM4,
    GDB_REG_MM5,
    GDB_REG_MM6,
    GDB_REG_MM7,
    GDB_REG_MAX
};

enum {
    GDB_MODE_BASE10 = 0,
    GDB_MODE_HEX,
    GDB_MODE_OCT,
    GDB_MODE_BIN
};

typedef struct _gdbstub_client_ {
    int                socket;
    struct sockaddr_in addr;

    char    packet[16384], response[16384];
    volatile int gone; /* the connection is closing; nobody will acknowledge responses */
    uint8_t has_packet : 1;
    uint8_t first_packet_received : 1;
    uint8_t ida_mode : 1;
    uint8_t waiting_stop : 1;
    int     packet_pos;
    int     response_pos;

    event_t *processed_event;
    event_t *response_event;

    uint16_t last_io_base;
    uint16_t last_io_len;
    uint16_t last_io_value;

    struct _gdbstub_client_ *next;
} gdbstub_client_t;

typedef struct _gdbstub_breakpoint_ {
    uint32_t addr;
    union {
        uint8_t  orig_val;
        uint32_t end;
    };

    struct _gdbstub_breakpoint_ *next;
} gdbstub_breakpoint_t;

#ifdef ENABLE_GDBSTUB_LOG
int gdbstub_do_log = ENABLE_GDBSTUB_LOG;

static void
gdbstub_log(const char *fmt, ...)
{
    va_list ap;

    if (gdbstub_do_log) {
        va_start(ap, fmt);
        pclog_ex(fmt, ap);
        va_end(ap);
    }
}
#else
#    define gdbstub_log(fmt, ...)
#endif

static x86seg   *segment_regs[] = { &cpu_state.seg_cs, &cpu_state.seg_ss, &cpu_state.seg_ds, &cpu_state.seg_es, &cpu_state.seg_fs, &cpu_state.seg_gs };
static uint32_t *cr_regs[]      = { &cpu_state.CR0.l, &cr2, &cr3, &cr4 };
static void     *fpu_regs[]     = { &cpu_state.npxc, &cpu_state.npxs, NULL, &x87_pc_seg, &x87_pc_off, &x87_op_seg, &x87_op_off };
static char      target_xml[]   = /* QEMU gdb-xml/i386-32bit.xml with modifications (described in comments) */
    // clang-format off
    "<?xml version=\"1.0\"?>"
    "<!DOCTYPE target SYSTEM \"gdb-target.dtd\">"
    "<target>"
        "<!-- architecture tag goes here -->" /* <architecture> patched in here (length must be kept) */
        ""
        "<feature name=\"org.gnu.gdb.i386.core\">"
            "<flags id=\"i386_eflags\" size=\"4\">"
                "<field name=\"\" start=\"22\" end=\"31\"/>"
                "<field name=\"ID\" start=\"21\" end=\"21\"/>"
                "<field name=\"VIP\" start=\"20\" end=\"20\"/>"
                "<field name=\"VIF\" start=\"19\" end=\"19\"/>"
                "<field name=\"AC\" start=\"18\" end=\"18\"/>"
                "<field name=\"VM\" start=\"17\" end=\"17\"/>"
                "<field name=\"RF\" start=\"16\" end=\"16\"/>"
                "<field name=\"\" start=\"15\" end=\"15\"/>"
                "<field name=\"NT\" start=\"14\" end=\"14\"/>"
                "<field name=\"IOPL\" start=\"12\" end=\"13\"/>"
                "<field name=\"OF\" start=\"11\" end=\"11\"/>"
                "<field name=\"DF\" start=\"10\" end=\"10\"/>"
                "<field name=\"IF\" start=\"9\" end=\"9\"/>"
                "<field name=\"TF\" start=\"8\" end=\"8\"/>"
                "<field name=\"SF\" start=\"7\" end=\"7\"/>"
                "<field name=\"ZF\" start=\"6\" end=\"6\"/>"
                "<field name=\"\" start=\"5\" end=\"5\"/>"
                "<field name=\"AF\" start=\"4\" end=\"4\"/>"
                "<field name=\"PF\" start=\"2\" end=\"2\"/>"
                "<field name=\"\" start=\"1\" end=\"1\"/>"
                "<field name=\"CF\" start=\"0\" end=\"0\"/>"
            "</flags>"
            ""
            "<reg name=\"eax\" bitsize=\"32\" type=\"int32\" regnum=\"0\"/>"
            "<reg name=\"ecx\" bitsize=\"32\" type=\"int32\"/>"
            "<reg name=\"edx\" bitsize=\"32\" type=\"int32\"/>"
            "<reg name=\"ebx\" bitsize=\"32\" type=\"int32\"/>"
            "<reg name=\"esp\" bitsize=\"32\" type=\"data_ptr\"/>"
            "<reg name=\"ebp\" bitsize=\"32\" type=\"data_ptr\"/>"
            "<reg name=\"esi\" bitsize=\"32\" type=\"int32\"/>"
            "<reg name=\"edi\" bitsize=\"32\" type=\"int32\"/>"
            ""
            "<reg name=\"eip\" bitsize=\"32\" type=\"code_ptr\"/>"
            "<reg name=\"eflags\" bitsize=\"32\" type=\"i386_eflags\"/>"
            ""
            "<reg name=\"cs\" bitsize=\"16\" type=\"int32\"/>"
            "<reg name=\"ss\" bitsize=\"16\" type=\"int32\"/>"
            "<reg name=\"ds\" bitsize=\"16\" type=\"int32\"/>"
            "<reg name=\"es\" bitsize=\"16\" type=\"int32\"/>"
            "<reg name=\"fs\" bitsize=\"16\" type=\"int32\"/>"
            "<reg name=\"gs\" bitsize=\"16\" type=\"int32\"/>"
            ""
#if 0
            "<reg name=\"fs_base\" bitsize=\"32\" type=\"int32\"/>"
            "<reg name=\"gs_base\" bitsize=\"32\" type=\"int32\"/>"
#endif
            ""
            "<flags id=\"i386_cr0\" size=\"4\">"
                "<field name=\"PG\" start=\"31\" end=\"31\"/>"
                "<field name=\"CD\" start=\"30\" end=\"30\"/>"
                "<field name=\"NW\" start=\"29\" end=\"29\"/>"
                "<field name=\"AM\" start=\"18\" end=\"18\"/>"
                "<field name=\"WP\" start=\"16\" end=\"16\"/>"
                "<field name=\"NE\" start=\"5\" end=\"5\"/>"
                "<field name=\"ET\" start=\"4\" end=\"4\"/>"
                "<field name=\"TS\" start=\"3\" end=\"3\"/>"
                "<field name=\"EM\" start=\"2\" end=\"2\"/>"
                "<field name=\"MP\" start=\"1\" end=\"1\"/>"
                "<field name=\"PE\" start=\"0\" end=\"0\"/>"
            "</flags>"
            ""
            "<flags id=\"i386_cr3\" size=\"4\">"
                "<field name=\"PDBR\" start=\"12\" end=\"31\"/>"
                "<field name=\"PCID\" start=\"0\" end=\"11\"/>"
            "</flags>"
            ""
            "<flags id=\"i386_cr4\" size=\"4\">"
                "<field name=\"VME\" start=\"0\" end=\"0\"/>"
                "<field name=\"PVI\" start=\"1\" end=\"1\"/>"
                "<field name=\"TSD\" start=\"2\" end=\"2\"/>"
                "<field name=\"DE\" start=\"3\" end=\"3\"/>"
                "<field name=\"PSE\" start=\"4\" end=\"4\"/>"
                "<field name=\"PAE\" start=\"5\" end=\"5\"/>"
                "<field name=\"MCE\" start=\"6\" end=\"6\"/>"
                "<field name=\"PGE\" start=\"7\" end=\"7\"/>"
                "<field name=\"PCE\" start=\"8\" end=\"8\"/>"
                "<field name=\"OSFXSR\" start=\"9\" end=\"9\"/>"
                "<field name=\"OSXMMEXCPT\" start=\"10\" end=\"10\"/>"
                "<field name=\"UMIP\" start=\"11\" end=\"11\"/>"
                "<field name=\"LA57\" start=\"12\" end=\"12\"/>"
                "<field name=\"VMXE\" start=\"13\" end=\"13\"/>"
                "<field name=\"SMXE\" start=\"14\" end=\"14\"/>"
                "<field name=\"FSGSBASE\" start=\"16\" end=\"16\"/>"
                "<field name=\"PCIDE\" start=\"17\" end=\"17\"/>"
                "<field name=\"OSXSAVE\" start=\"18\" end=\"18\"/>"
                "<field name=\"SMEP\" start=\"20\" end=\"20\"/>"
                "<field name=\"SMAP\" start=\"21\" end=\"21\"/>"
                "<field name=\"PKE\" start=\"22\" end=\"22\"/>"
            "</flags>"
            ""
            "<flags id=\"i386_efer\" size=\"4\">"
                "<field name=\"TCE\" start=\"15\" end=\"15\"/>"
                "<field name=\"FFXSR\" start=\"14\" end=\"14\"/>"
                "<field name=\"LMSLE\" start=\"13\" end=\"13\"/>"
                "<field name=\"SVME\" start=\"12\" end=\"12\"/>"
                "<field name=\"NXE\" start=\"11\" end=\"11\"/>"
                "<field name=\"LMA\" start=\"10\" end=\"10\"/>"
                "<field name=\"LME\" start=\"8\" end=\"8\"/>"
                "<field name=\"SCE\" start=\"0\" end=\"0\"/>"
            "</flags>"
            ""
            "<reg name=\"cr0\" bitsize=\"32\" type=\"i386_cr0\"/>"
            "<reg name=\"cr2\" bitsize=\"32\" type=\"int32\"/>"
            "<reg name=\"cr3\" bitsize=\"32\" type=\"i386_cr3\"/>"
            "<reg name=\"cr4\" bitsize=\"32\" type=\"i386_cr4\"/>"
            "<reg name=\"efer\" bitsize=\"64\" type=\"i386_efer\"/>"
            ""
            "<reg name=\"st0\" bitsize=\"80\" type=\"i387_ext\"/>"
            "<reg name=\"st1\" bitsize=\"80\" type=\"i387_ext\"/>"
            "<reg name=\"st2\" bitsize=\"80\" type=\"i387_ext\"/>"
            "<reg name=\"st3\" bitsize=\"80\" type=\"i387_ext\"/>"
            "<reg name=\"st4\" bitsize=\"80\" type=\"i387_ext\"/>"
            "<reg name=\"st5\" bitsize=\"80\" type=\"i387_ext\"/>"
            "<reg name=\"st6\" bitsize=\"80\" type=\"i387_ext\"/>"
            "<reg name=\"st7\" bitsize=\"80\" type=\"i387_ext\"/>"
            ""
            "<reg name=\"fctrl\" bitsize=\"16\" type=\"int\" group=\"float\"/>"
            "<reg name=\"fstat\" bitsize=\"16\" type=\"int\" group=\"float\"/>"
            "<reg name=\"ftag\" bitsize=\"16\" type=\"int\" group=\"float\"/>"
            "<reg name=\"fiseg\" bitsize=\"16\" type=\"int\" group=\"float\"/>"
            "<reg name=\"fioff\" bitsize=\"32\" type=\"int\" group=\"float\"/>"
            "<reg name=\"foseg\" bitsize=\"16\" type=\"int\" group=\"float\"/>"
            "<reg name=\"fooff\" bitsize=\"32\" type=\"int\" group=\"float\"/>"
            "<reg name=\"fop\" bitsize=\"16\" type=\"int\" group=\"float\"/>"
            ""
            "<vector id=\"v8i8\" type=\"int8\" count=\"8\"/>"
            "<vector id=\"v8u8\" type=\"uint8\" count=\"8\"/>"
            "<vector id=\"v4i16\" type=\"int16\" count=\"4\"/>"
            "<vector id=\"v4u16\" type=\"uint16\" count=\"4\"/>"
            "<vector id=\"v2i32\" type=\"int32\" count=\"2\"/>"
            "<vector id=\"v2u32\" type=\"uint32\" count=\"2\"/>"
            "<union id=\"mmx\">"
                "<field name=\"uint64\" type=\"uint64\"/>"
                "<field name=\"v2_int32\" type=\"v2i32\"/>"
                "<field name=\"v4_int16\" type=\"v4i16\"/>"
                "<field name=\"v8_int8\" type=\"v8i8\"/>"
            "</union>"
            ""
            "<reg name=\"mm0\" bitsize=\"64\" type=\"mmx\" group=\"mmx\"/>"
            "<reg name=\"mm1\" bitsize=\"64\" type=\"mmx\" group=\"mmx\"/>"
            "<reg name=\"mm2\" bitsize=\"64\" type=\"mmx\" group=\"mmx\"/>"
            "<reg name=\"mm3\" bitsize=\"64\" type=\"mmx\" group=\"mmx\"/>"
            "<reg name=\"mm4\" bitsize=\"64\" type=\"mmx\" group=\"mmx\"/>"
            "<reg name=\"mm5\" bitsize=\"64\" type=\"mmx\" group=\"mmx\"/>"
            "<reg name=\"mm6\" bitsize=\"64\" type=\"mmx\" group=\"mmx\"/>"
            "<reg name=\"mm7\" bitsize=\"64\" type=\"mmx\" group=\"mmx\"/>"
        "</feature>"
    "</target>";
// clang-format on

#ifdef _WIN32
static WSADATA wsa;
#endif
static int      gdbstub_socket = -1;
static int      stop_reason_len = 0;
static int      in_gdbstub = 0;
static uint32_t watch_addr;
static char     stop_reason[2048];

static gdbstub_client_t *first_client = NULL;
static gdbstub_client_t *last_client = NULL;
static mutex_t          *client_list_mutex;

/* The last frame the first monitor's output completed, kept while a client is
   connected so that it can be fetched as a screenshot, and the RGB snapshot
   of it that "fb" serves. Like client packets, these are only touched from
   the emulation thread. */
static uint32_t *frame_last;
static int       frame_last_w;
static int       frame_last_h;
static int       frame_last_size;
static uint32_t  frame_last_seq;
static uint8_t  *frame_snap;
static int       frame_snap_size;
static int       frame_snap_len;

/* Keep a paused CPU paused when the last client disconnects ("hold 1"),
   so that tools which connect once per command don't let the guest run
   between commands. */
static int hold_on_disconnect;

/* Whether the CPU was stopped at the end of the last time slice. */
static int was_stopped;

/* Set by the client thread when the last client leaves; acted on by the
   emulation thread, which owns the breakpoint lists and input state. */
static volatile int cleanup_pending;

/* Keys pressed with "kd" and not yet released, to release on disconnect. */
static uint16_t held_keys[32];
static int      held_keys_count;

/* Software interrupt log ("tv"/"tl"), interrupt catchpoints ("ca") and the
   execution range catch ("xr"), all driven from the emulation thread.
   A record holds the registers at the INT instruction, the bytes at DS:(E)DX,
   DS:(E)SI and ES:(E)DI then, and the registers and the bytes at the first
   and last of those addresses when the call returns. The layout is part of
   the "tl" output, so fields are only ever appended. */
#define INTLOG_SIZE    4096 /* records, power of 2 */
#define INTLOG_BYTES   64
#define INTLOG_PENDING 64
#define INTLOG_HASH    1024

enum {
    INTLOG_PENDING_CALL = 0, /* not returned yet */
    INTLOG_RETURNED,
    INTLOG_NO_RETURN /* an outer call returned first (exit, longjmp...) */
};

typedef struct {
    uint32_t seq;
    uint32_t info; /* vector | mode << 8 (0 real, 1 V86, 2 PM16, 3 PM32) | depth << 16 | status << 24 */
    uint32_t tsc_lo, tsc_hi;
    uint32_t sel_cs, eip, sel_ss, esp, sel_ds, sel_es;
    uint32_t in[6];  /* EAX EBX ECX EDX ESI EDI */
    uint32_t out[9]; /* EAX EBX ECX EDX ESI EDI EFLAGS DS ES */
    uint32_t lin[3]; /* linear DS:(E)DX, DS:(E)SI, ES:(E)DI at the call */
    uint32_t lens;   /* bytes captured: DX | SI << 8 | DI << 16 | DX on return << 24 */
    uint32_t lens2;  /* DI on return */
    uint32_t base_cs;
    uint32_t count; /* identical consecutive calls this record stands for */
    uint8_t  bytes[5][INTLOG_BYTES]; /* DX, SI, DI at the call; DX, DI on return */
} gdbstub_intlog_t;

typedef struct {
    uint32_t ret, stack_sel, stack_ptr, seq;
    uint8_t  vector, ah, al;
} gdbstub_intpending_t;

typedef struct {
    int vector, ah, al, when; /* ah/al -1 for any; when: 1 call, 2 return */
} gdbstub_catch_t;

static gdbstub_intlog_t    *intlog;
static uint32_t             intlog_next = 1;
static uint32_t             intlog_first = 1;
static uint8_t              intlog_vectors[256];
static int                  intlog_on;
static gdbstub_intpending_t int_pending[INTLOG_PENDING];
static int                  int_pending_count;
static uint8_t              int_ret_hash[INTLOG_HASH];
static gdbstub_catch_t      catches[16];
static int                  catch_count;
static int                  catch_stop_vector;
static int                  catch_stop_return;
static uint32_t             catch_stop_seq;
static uint32_t             xrange_lo[8], xrange_hi[8];
static int                  xrange_count;
static uint32_t             xrange_hit;

#define INT_RET_HASH(a) ((((a) >> 10) ^ (a)) & (INTLOG_HASH - 1))

static void gdbstub_int_clear(void);
static int  gdbstub_peek(uint32_t addr, uint8_t *buf, int len);


static void (*cpu_exec_shadow)(int32_t cycs);

/* NEC V20/V30 core: has already fetched the next opcode when the stub runs. */
extern int biu_queue_preload;

/* Linear address of the next instruction to execute. */
static uint32_t
gdbstub_pc(void)
{
    if (cpu_exec_shadow == execvx0)
        return cs + ((cpu_state.pc - biu_queue_preload) & 0xffff);
    return cs + cpu_state.pc;
}

/* Only the 286+ interpreters compute arithmetic flags lazily; on the 8086
   cores flags_op is never maintained and may be stale from another machine. */
static void
gdbstub_flags_rebuild(void)
{
    if (is286)
        flags_rebuild();
}
static gdbstub_breakpoint_t *first_swbreak = NULL;
static gdbstub_breakpoint_t *first_hwbreak = NULL;
static gdbstub_breakpoint_t *first_rwatch = NULL;
static gdbstub_breakpoint_t *first_wwatch = NULL;
static gdbstub_breakpoint_t *first_awatch = NULL;

int      gdbstub_step = 0;
int      gdbstub_next_asap = 0;
uint64_t gdbstub_watch_pages[(((uint32_t) -1) >> (MEM_GRANULARITY_BITS + 6)) + 1];

static void
gdbstub_break(void)
{
    /* Pause CPU execution as soon as possible. */
    if (gdbstub_step <= GDBSTUB_EXEC)
        gdbstub_step = GDBSTUB_BREAK;
}

static void
gdbstub_jump(uint32_t new_pc)
{
    cpu_state.pc = new_pc - cs;
    flushmmucache();
}

static inline int
gdbstub_hex_decode(int c)
{
    if ((c >= '0') && (c <= '9'))
        return c - '0';
    else if ((c >= 'A') && (c <= 'F'))
        return c - 'A' + 10;
    else if ((c >= 'a') && (c <= 'f'))
        return c - 'a' + 10;
    else
        return 0;
}

static inline int
gdbstub_hex_encode(int c)
{
    if (c < 10)
        return c + '0';
    else
        return c - 10 + 'a';
}

/* Parse a hex number (with or without 0x) filling all 32 bits; 0 if invalid. */
static int
gdbstub_parse_hex(const char *p, uint32_t *dest)
{
    char *end;
    if (!p || !*p)
        return 0;
    *dest = (uint32_t) strtoul(p, &end, 16);
    return !*end;
}

static int
gdbstub_num_decode(char *p, int *dest, int mode)
{
    /* Stop if the pointer is invalid. */
    if (!p)
        return 0;

    /* Read sign. */
    int sign = 1;
    if ((p[0] == '+') || (p[0] == '-')) {
        if (p[0] == '-')
            sign = -1;
        p++;
    }

    /* Read type identifer if present (0x/0o/0b/0n) */
    if (p[0] == '0') {
        switch (p[1]) {
            case 'x':
                mode = GDB_MODE_HEX;
                break;

            case '0' ... '7':
                p -= 1;
                /* fall-through */

            case 'o':
                mode = GDB_MODE_OCT;
                break;

            case 'b':
                mode = GDB_MODE_BIN;
                break;

            case 'n':
                mode = GDB_MODE_BASE10;
                break;

            default:
                p -= 2;
                break;
        }
        p += 2;
    }

    /* Parse each character. */
    *dest = 0;
    while (*p) {
        switch (mode) {
            case GDB_MODE_BASE10:
                if ((*p >= '0') && (*p <= '9'))
                    *dest = ((*dest) * 10) + ((*p) - '0');
                else
                    return 0;
                break;

            case GDB_MODE_HEX:
                if (((*p >= '0') && (*p <= '9')) || ((*p >= 'A') && (*p <= 'F')) || ((*p >= 'a') && (*p <= 'f')))
                    *dest = ((*dest) << 4) | gdbstub_hex_decode(*p);
                else
                    return 0;
                break;

            case GDB_MODE_OCT:
                if ((*p >= '0') && (*p <= '7'))
                    *dest = ((*dest) << 3) | ((*p) - '0');
                else
                    return 0;
                break;

            case GDB_MODE_BIN:
                if ((*p == '0') || (*p == '1'))
                    *dest = ((*dest) << 1) | ((*p) - '0');
                else
                    return 0;
                break;

            default:
                break;
        }
        p++;
    }

    /* Apply sign. */
    if (sign < 0)
        *dest = -(*dest);

    /* Return success. */
    return 1;
}

static int
gdbstub_client_read_word(gdbstub_client_t *client, int *dest)
{
    const char *p = &client->packet[client->packet_pos];
    const char *q = p;
    while (((*p >= '0') && (*p <= '9')) || ((*p >= 'A') && (*p <= 'F')) || ((*p >= 'a') && (*p <= 'f')))
        *dest = ((*dest) << 4) | gdbstub_hex_decode(*p++);
    return p - q;
}

static int
gdbstub_client_read_hex(gdbstub_client_t *client, uint8_t *buf, int size)
{
    int pp = client->packet_pos;
    while (size-- && (pp < (sizeof(client->packet) - 2))) {
        *buf = gdbstub_hex_decode(client->packet[pp++]) << 4;
        *buf++ |= gdbstub_hex_decode(client->packet[pp++]);
    }
    return pp - client->packet_pos;
}

static int
gdbstub_client_read_string(gdbstub_client_t *client, char *buf, int size, char terminator)
{
    int  pp = client->packet_pos;
    char c;
    while (size-- && (pp < (sizeof(client->packet) - 1))) {
        c = client->packet[pp];
        if ((c == terminator) || (c == '\0')) {
            *buf = '\0';
            break;
        }
        pp++;
        *buf++ = c;
    }
    return pp - client->packet_pos;
}

static int
gdbstub_client_write_reg(int index, uint8_t *buf)
{
    int width = 4;
    switch (index) {
        case GDB_REG_EAX ... GDB_REG_EDI:
            cpu_state.regs[index - GDB_REG_EAX].l = *((uint32_t *) buf);
            break;

        case GDB_REG_EIP:
            gdbstub_jump(*((uint32_t *) buf));
            break;

        case GDB_REG_EFLAGS:
            gdbstub_flags_rebuild(); /* drop pending lazy flags, which would override the new ones */
            cpu_state.flags  = AS_U16(buf[0]);
            cpu_state.eflags = AS_U16(buf[2]);
            break;

        case GDB_REG_CS ... GDB_REG_GS:
            width = 2;
            loadseg(*((uint16_t *) buf), segment_regs[index - GDB_REG_CS]);
            flushmmucache();
            break;

#if 0
        case GDB_REG_FS_BASE ... GDB_REG_GS_BASE:
            /* Do what qemu does and just load the base. */
            segment_regs[(index - 16) + (GDB_REG_FS - GDB_REG_CS)]->base = *((uint32_t *) buf);
            break;
#endif

        case GDB_REG_CR0 ... GDB_REG_CR4:
            *cr_regs[index - GDB_REG_CR0] = *((uint32_t *) buf);
            flushmmucache();
            break;

        case GDB_REG_EFER:
            msr.amd_efer = *((uint64_t *) buf);
            break;

        case GDB_REG_ST0 ... GDB_REG_ST7:
            width           = 10;
            x87_conv_t conv = {
                .eind  = { .ll = AS_U64(buf[0]) },
                .begin = AS_U16(buf[8])
            };
            cpu_state.ST[(cpu_state.TOP + (index - GDB_REG_ST0)) & 7] = x87_from80(&conv);
            break;

        case GDB_REG_FCTRL:
        case GDB_REG_FISEG:
        case GDB_REG_FOSEG:
            width                                           = 2;
            *((uint16_t *) fpu_regs[index - GDB_REG_FCTRL]) = *((uint16_t *) buf);
            if (index >= GDB_REG_FISEG)
                flushmmucache();
            break;

        case GDB_REG_FSTAT:
        case GDB_REG_FOP:
            width = 2;
            break;

        case GDB_REG_FTAG:
            width = 2;
            x87_settag(*((uint16_t *) buf));
            break;

        case GDB_REG_FIOFF:
        case GDB_REG_FOOFF:
            *((uint32_t *) fpu_regs[index - GDB_REG_FCTRL]) = *((uint32_t *) buf);
            break;

        case GDB_REG_MM0 ... GDB_REG_MM7:
            width                               = 8;
            cpu_state.MM[index - GDB_REG_MM0].q = *((uint64_t *) buf);
            break;

        default:
            width = 0;
    }

#ifdef ENABLE_GDBSTUB_LOG
    char logbuf[256], *p = logbuf + sprintf(logbuf, "GDB Stub: Setting register %d to ", index);
    for (int i = width - 1; i >= 0; i--)
        p += sprintf(p, "%02X", buf[i]);
    sprintf(p, "\n");
    gdbstub_log(logbuf);
#endif

    return width;
}

static void
gdbstub_client_respond(gdbstub_client_t *client)
{
    /* Calculate checksum. */
    int checksum = 0;
    int i;
    for (i = 0; i < client->response_pos; i++)
        checksum += client->response[i];

    /* Send response packet. */
    client->response[client->response_pos] = '\0';
#ifdef ENABLE_GDBSTUB_LOG
    i                     = client->response[994]; /* pclog_ex buffer too small */
    client->response[994] = '\0';
    gdbstub_log("GDB Stub: Sending response: %s\n", client->response);
    client->response[994] = i;
#endif
    send(client->socket, "$", 1, MSG_NOSIGNAL);
    send(client->socket, client->response, client->response_pos, MSG_NOSIGNAL);
    char response_cksum[3] = { '#', gdbstub_hex_encode((checksum >> 4) & 0x0f), gdbstub_hex_encode(checksum & 0x0f) };
    send(client->socket, response_cksum, sizeof(response_cksum), MSG_NOSIGNAL);
}

static void
gdbstub_client_respond_partial(gdbstub_client_t *client)
{
    /* Send response. */
    gdbstub_client_respond(client);

    /* Wait for the response to be acknowledged. */
    thread_wait_event(client->response_event, -1);
    thread_reset_event(client->response_event);
}

static void
gdbstub_client_respond_hex(gdbstub_client_t *client, uint8_t *buf, int size)
{
    while (size-- && (client->response_pos < (sizeof(client->response) - 2))) {
        client->response[client->response_pos++] = gdbstub_hex_encode((*buf) >> 4);
        client->response[client->response_pos++] = gdbstub_hex_encode((*buf++) & 0x0f);
    }
}

static int
gdbstub_client_read_reg(int index, uint8_t *buf)
{
    int width = 4;
    switch (index) {
        case GDB_REG_EAX ... GDB_REG_EDI:
            *((uint32_t *) buf) = cpu_state.regs[index].l;
            break;

        case GDB_REG_EIP:
            *((uint32_t *) buf) = cs + cpu_state.pc;
            break;

        case GDB_REG_EFLAGS:
            gdbstub_flags_rebuild(); /* the interpreter computes arithmetic flags lazily */
            AS_U16(buf[0]) = cpu_state.flags;
            AS_U16(buf[2]) = cpu_state.eflags;
            break;

        case GDB_REG_CS ... GDB_REG_GS:
            *((uint16_t *) buf) = segment_regs[index - GDB_REG_CS]->seg;
            break;

#if 0
        case GDB_REG_FS_BASE ... GDB_REG_GS_BASE:
            *((uint32_t *) buf) = segment_regs[(index - 16) + (GDB_REG_FS - GDB_REG_CS)]->base;
            break;
#endif

        case GDB_REG_CR0 ... GDB_REG_CR4:
            *((uint32_t *) buf) = *cr_regs[index - GDB_REG_CR0];
            break;

        case GDB_REG_EFER:
            *((uint64_t *) buf) = msr.amd_efer;
            break;

        case GDB_REG_ST0 ... GDB_REG_ST7:
            width = 10;
            x87_conv_t conv;
            x87_to80(cpu_state.ST[(cpu_state.TOP + (index - GDB_REG_ST0)) & 7], &conv);
            AS_U64(buf[0]) = conv.eind.ll;
            AS_U16(buf[8]) = conv.begin;
            break;

        case GDB_REG_FCTRL ... GDB_REG_FSTAT:
        case GDB_REG_FISEG:
        case GDB_REG_FOSEG:
            width               = 2;
            *((uint16_t *) buf) = *((uint16_t *) fpu_regs[index - GDB_REG_FCTRL]);
            break;

        case GDB_REG_FTAG:
            width               = 2;
            *((uint16_t *) buf) = x87_gettag();
            break;

        case GDB_REG_FIOFF:
        case GDB_REG_FOOFF:
            *((uint32_t *) buf) = *((uint32_t *) fpu_regs[index - GDB_REG_FCTRL]);
            break;

        case GDB_REG_FOP:
            width               = 2;
            *((uint16_t *) buf) = 0; /* we don't store the FPU opcode */
            break;

        case GDB_REG_MM0 ... GDB_REG_MM7:
            width               = 8;
            *((uint64_t *) buf) = cpu_state.MM[index - GDB_REG_MM0].q;
            break;

        default:
            width = 0;
    }

    return width;
}

static void
gdbstub_client_packet(gdbstub_client_t *client)
{
    gdbstub_breakpoint_t *breakpoint;
    gdbstub_breakpoint_t *prev_breakpoint = NULL;
    gdbstub_breakpoint_t **first_breakpoint = NULL;

#ifdef GDBSTUB_CHECK_CHECKSUM /* msys2 gdb 11.1 transmits qSupported and H with invalid checksum... */
    uint8_t rcv_checksum = 0, checksum = 0;
#endif
    int     i;
    int     j = 0;
    int     k = 0;
    int     l;
    uint8_t buf[10] = { 0 };
    char   *p;

    int     orig_cpu_abrt        = cpu_state.abrt;
    int     orig_cpu_abrt_reason = abrt_error;
    uint32_t orig_cr2            = cr2;

    /* Validate checksum. */
    client->packet_pos -= 2;
#ifdef GDBSTUB_CHECK_CHECKSUM
    gdbstub_client_read_hex(client, &rcv_checksum, 1);
#endif
    AS_U16(client->packet[--client->packet_pos]) = 0;
#ifdef GDBSTUB_CHECK_CHECKSUM
    for (i = 0; i < client->packet_pos; i++)
        checksum += client->packet[i];

    if (checksum != rcv_checksum) {
        /* Send negative acknowledgement. */
#    ifdef ENABLE_GDBSTUB_LOG
        i                   = client->packet[953]; /* pclog_ex buffer too small */
        client->packet[953] = '\0';
        gdbstub_log("GDB Stub: Received packet with invalid checksum (expected %02X got %02X): %s\n", checksum, rcv_checksum, client->packet);
        client->packet[953] = i;
#    endif
        send(client->socket, "-", 1, MSG_NOSIGNAL);
        return;
    }
#endif

    /* Send positive acknowledgement. */
#ifdef ENABLE_GDBSTUB_LOG
    i                   = client->packet[996]; /* pclog_ex buffer too small */
    client->packet[996] = '\0';
    gdbstub_log("GDB Stub: Received packet: %s\n", client->packet);
    client->packet[996] = i;
#endif
    send(client->socket, "+", 1, MSG_NOSIGNAL);

    /* Block other responses from being written while this one (if any is produced) isn't acknowledged. */
    if ((client->packet[0] != 'c') && (client->packet[0] != 's') && (client->packet[0] != 'v')) {
        thread_wait_event(client->response_event, -1);
        thread_reset_event(client->response_event);
    }
    client->response_pos = 0;
    client->packet_pos   = 1;

    /* Handle IDA-specific hacks. */
    if (!client->first_packet_received) {
        client->first_packet_received = 1;
        if (!strcmp(client->packet, "qSupported:xmlRegisters=i386,arm,mips")) {
            gdbstub_log("GDB Stub: Enabling IDA mode\n");
            client->ida_mode = 1;
        }
    }

    /* Parse command. */
    switch (client->packet[0]) {
        case '?': /* stop reason */
            /* Respond with a stop reply packet if one is present. */
            if (stop_reason_len) {
                strcpy(client->response, stop_reason);
                client->response_pos = strlen(client->response);
            }
            break;

        case 'c': /* continue */
        case 's': /* step */
            /* Flag that the client is waiting for a stop reason. */
            client->waiting_stop = 1;

            /* Jump to address if specified. */
            if (client->packet[1] && gdbstub_client_read_word(client, &j))
                gdbstub_jump(j);

            /* Resume CPU. */
            gdbstub_step = gdbstub_next_asap = (client->packet[0] == 's') ? GDBSTUB_SSTEP : GDBSTUB_EXEC;
            return;

        case 'D': /* detach */
            /* Resume emulation. */
            gdbstub_step = GDBSTUB_EXEC;

            /* Respond positively. */
ok:
            FAST_RESPONSE("OK");
            break;

        case 'g': /* read all registers */
            /* Output the values of all registers. */
            for (i = 0; i < GDB_REG_MAX; i++)
                gdbstub_client_respond_hex(client, buf, gdbstub_client_read_reg(i, buf));
            break;

        case 'G': /* write all registers */
            /* Write the values of all registers. */
            for (i = 0; i < GDB_REG_MAX; i++) {
                if (i == GDB_REG_MAX)
                    goto e22;
                if (!gdbstub_client_read_hex(client, buf, sizeof(buf)))
                    break;
                client->packet_pos += gdbstub_client_write_reg(i, buf) << 1;
            }

            /* Respond positively. */
            goto ok;

        case 'H': /* set thread */
            /* Read operation type and thread ID. */
            if ((client->packet[1] == '\0') || (client->packet[2] == '\0')) {
e22:
                FAST_RESPONSE("E22");
                break;
            }

            /* Respond positively only on thread 1. */
            if ((client->packet[2] == '1') && !client->packet[3])
                goto ok;
            else
                goto e22;

        case 'm': /* read memory */
            /* Read address and length. */
            if (!(i = gdbstub_client_read_word(client, &j)))
                goto e22;
            client->packet_pos += i + 1;
            gdbstub_client_read_word(client, &k);
            if (!k)
                goto e22;

            /* Clamp length. */
            if (k >= (sizeof(client->response) >> 1))
                k = (sizeof(client->response) >> 1) - 1;

            /* Read by qwords, then by dwords, then by words, then by bytes. */
            i = 0;
            orig_cr2     = cr2;
            cpl_override = 1;
            if (is386) {
                for (; i < (k & ~7); i += 8) {
                    orig_cpu_abrt        = cpu_state.abrt;
                    orig_cpu_abrt_reason = abrt_error;
                    *((uint64_t *) buf) = readmemql(j);
                    if (cpu_state.abrt != orig_cpu_abrt) {
                        if (cpu_state.abrt == ABRT_PF) {
                            cpu_state.abrt = orig_cpu_abrt;
                            abrt_error     = orig_cpu_abrt_reason;
                            goto mem_read_fault;
                        }
                    }
                    j += 8;
                    gdbstub_client_respond_hex(client, buf, 8);
                }
                for (; i < (k & ~3); i += 4) {
                    orig_cpu_abrt        = cpu_state.abrt;
                    orig_cpu_abrt_reason = abrt_error;
                    *((uint32_t *) buf) = readmemll(j);
                    if (cpu_state.abrt != orig_cpu_abrt) {
                        if (cpu_state.abrt == ABRT_PF) {
                            cpu_state.abrt = orig_cpu_abrt;
                            abrt_error     = orig_cpu_abrt_reason;
                            goto mem_read_fault;
                        }
                    }
                    j += 4;
                    gdbstub_client_respond_hex(client, buf, 4);
                }
            }
            for (; i < (k & ~1); i += 2) {
                orig_cpu_abrt        = cpu_state.abrt;
                orig_cpu_abrt_reason = abrt_error;
                *((uint16_t *) buf) = readmemwl(j);
                if (cpu_state.abrt != orig_cpu_abrt) {
                    if (cpu_state.abrt == ABRT_PF) {
                        cpu_state.abrt = orig_cpu_abrt;
                        abrt_error     = orig_cpu_abrt_reason;
                        goto mem_read_fault;
                    }
                }
                j += 2;
                gdbstub_client_respond_hex(client, buf, 2);
            }
            for (; i < k; i++) {
                orig_cpu_abrt        = cpu_state.abrt;
                orig_cpu_abrt_reason = abrt_error;
                buf[0] = readmembl(j++);
                if (cpu_state.abrt != orig_cpu_abrt) {
                    if (cpu_state.abrt == ABRT_PF) {
                        cpu_state.abrt = orig_cpu_abrt;
                        abrt_error     = orig_cpu_abrt_reason;
                        goto mem_read_fault;
                    }
                }
                gdbstub_client_respond_hex(client, buf, 1);
            }
            cpl_override = 0;
            cr2          = orig_cr2;
            break;

mem_read_fault:
            /* Return what was read before the fault, as GDB allows, or an error
               if nothing was. The guest's CR2 must not see debugger accesses. */
            cpl_override = 0;
            cr2          = orig_cr2;
            if (!client->response_pos) {
                FAST_RESPONSE("E06");
            }
            break;

        case 'M': /* write memory */
        case 'X': /* write memory binary */
            /* Read address and length. */
            if (!(i = gdbstub_client_read_word(client, &j)))
                goto e22;
            client->packet_pos += i + 1;
            client->packet_pos += gdbstub_client_read_word(client, &k) + 1;
            if (!k)
                goto e22;

            /* Clamp length. */
            if (k >= ((sizeof(client->response) >> 1) - client->packet_pos))
                k = (sizeof(client->response) >> 1) - client->packet_pos - 1;

            /* Decode the data. */
            if (client->packet[0] == 'M') { /* hex encoded */
                gdbstub_client_read_hex(client, (uint8_t *) client->packet, k);
            } else { /* binary encoded */
                i = 0;
                while (i < k) {
                    if (client->packet[client->packet_pos] == '}') {
                        client->packet_pos++;
                        client->packet[i++] = client->packet[client->packet_pos++] ^ 0x20;
                    } else {
                        client->packet[i++] = client->packet[client->packet_pos++];
                    }
                }
            }

            /* Write by qwords, then by dwords, then by words, then by bytes. */
            p = client->packet;
            i = 0;
            orig_cr2     = cr2;
            cpl_override = 1;
            if (is386) {
                for (; i < (k & ~7); i += 8) {
                    orig_cpu_abrt        = cpu_state.abrt;
                    orig_cpu_abrt_reason = abrt_error;
                    writememql(j, *((uint64_t *) p));
                    if (cpu_state.abrt != orig_cpu_abrt) {
                        if (cpu_state.abrt == ABRT_PF) {
                            cpu_state.abrt = orig_cpu_abrt;
                            abrt_error     = orig_cpu_abrt_reason;
                            goto mem_write_fault;
                        }
                    }
                    j += 8;
                    p += 8;
                }
                for (; i < (k & ~3); i += 4) {
                    orig_cpu_abrt        = cpu_state.abrt;
                    orig_cpu_abrt_reason = abrt_error;
                    writememll(j, *((uint32_t *) p));
                    if (cpu_state.abrt != orig_cpu_abrt) {
                        if (cpu_state.abrt == ABRT_PF) {
                            cpu_state.abrt = orig_cpu_abrt;
                            abrt_error     = orig_cpu_abrt_reason;
                            goto mem_write_fault;
                        }
                    }
                    j += 4;
                    p += 4;
                }
            }
            for (; i < (k & ~1); i += 2) {
                orig_cpu_abrt        = cpu_state.abrt;
                orig_cpu_abrt_reason = abrt_error;
                writememwl(j, *((uint16_t *) p));
                if (cpu_state.abrt != orig_cpu_abrt) {
                    if (cpu_state.abrt == ABRT_PF) {
                        cpu_state.abrt = orig_cpu_abrt;
                        abrt_error     = orig_cpu_abrt_reason;
                        goto mem_write_fault;
                    }
                }
                j += 2;
                p += 2;
            }
            for (; i < k; i++) {
                orig_cpu_abrt        = cpu_state.abrt;
                orig_cpu_abrt_reason = abrt_error;
                writemembl(j++, p[0]);
                if (cpu_state.abrt != orig_cpu_abrt) {
                    if (cpu_state.abrt == ABRT_PF) {
                        cpu_state.abrt = orig_cpu_abrt;
                        abrt_error     = orig_cpu_abrt_reason;
                        goto mem_write_fault;
                    }
                }
                p++;
            }
            cpl_override = 0;
            cr2          = orig_cr2;

            /* Respond positively. */
            goto ok;

mem_write_fault:
            cpl_override = 0;
            cr2          = orig_cr2;
            FAST_RESPONSE("E06");
            break;

        case 'p': /* read register */
            /* Read register index. */
            if (!gdbstub_client_read_word(client, &j)) {
e14:
                FAST_RESPONSE("E14");
                break;
            }

            /* Read the register's value. */
            if (!(i = gdbstub_client_read_reg(j, buf)))
                goto e14;

            /* Return value. */
            gdbstub_client_respond_hex(client, buf, i);
            break;

        case 'P': /* write register */
            /* Read register index and value. */
            if (!(i = gdbstub_client_read_word(client, &j)))
                goto e14;
            client->packet_pos += i + 1;
            if (!gdbstub_client_read_hex(client, buf, sizeof(buf)))
                goto e14;

            /* Write the value to the register. */
            if (!gdbstub_client_write_reg(j, buf))
                goto e14;

            /* Respond positively. */
            goto ok;

        case 'q': /* query */
            /* Erase response, as we'll use it as a scratch buffer. */
            memset(client->response, 0, sizeof(client->response));

            /* Read the query type. */
            client->packet_pos += gdbstub_client_read_string(client, client->response, sizeof(client->response) - 1,
                                                             (client->packet[1] == 'R') ? ',' : ':')
                + 1;

            /* Perform the query. */
            if (!strcmp(client->response, "Supported")) {
                /* Go through the feature list and negate ones we don't support. */
                while ((client->response_pos < (sizeof(client->response) - 1)) && (i = gdbstub_client_read_string(client, &client->response[client->response_pos], sizeof(client->response) - client->response_pos - 1, ';'))) {
                    client->packet_pos += i + 1;
                    if (strncmp(&client->response[client->response_pos], "PacketSize", 10) && strcmp(&client->response[client->response_pos], "swbreak") && strcmp(&client->response[client->response_pos], "hwbreak") && strncmp(&client->response[client->response_pos], "xmlRegisters", 12) && strcmp(&client->response[client->response_pos], "qXfer:features:read")) {
                        gdbstub_log("GDB Stub: Feature \"%s\" is not supported\n", &client->response[client->response_pos]);
                        client->response_pos += i;
                        client->response[client->response_pos++] = '-';
                        client->response[client->response_pos++] = ';';
                    } else {
                        gdbstub_log("GDB Stub: Feature \"%s\" is supported\n", &client->response[client->response_pos]);
                    }
                }

                /* Add our supported features to the end. */
                if (client->response_pos < (sizeof(client->response) - 1))
                    client->response_pos += snprintf(&client->response[client->response_pos], sizeof(client->response) - client->response_pos,
                                                     "PacketSize=%X;swbreak+;hwbreak+;qXfer:features:read+", (int) (sizeof(client->packet) - 1));
                break;
            } else if (!strcmp(client->response, "Xfer")) {
                /* Read the transfer object. */
                client->packet_pos += gdbstub_client_read_string(client, client->response, sizeof(client->response) - 1, ':') + 1;
                if (!strcmp(client->response, "features")) {
                    /* Read the transfer operation. */
                    client->packet_pos += gdbstub_client_read_string(client, client->response, sizeof(client->response) - 1, ':') + 1;
                    if (!strcmp(client->response, "read")) {
                        /* Read the transfer annex. */
                        client->packet_pos += gdbstub_client_read_string(client, client->response, sizeof(client->response) - 1, ':') + 1;
                        if (!strcmp(client->response, "target.xml")) {
                            /* Patch architecture for IDA. */
                            p = strstr(target_xml, "<!-- architecture tag goes here -->");
                            if (p) {
                                if (client->ida_mode)
                                    memcpy(p, "<architecture>i386</architecture>  ", 35); /* make IDA not complain about i8086 being unknown */
                                else
                                    memcpy(p, "<architecture>i8086</architecture> ", 35); /* start in 16-bit mode to work around known GDB bug preventing 32->16 switching */
                            }

                            /* Send target XML. */
                            p = target_xml;
                        } else {
                            p = NULL;
                        }

                        /* Stop if the file wasn't found. */
                        if (!p) {
e00:
                            FAST_RESPONSE("E00");
                            break;
                        }

                        /* Read offset and length. */
                        if (!(i = gdbstub_client_read_word(client, &j)))
                            goto e22;
                        client->packet_pos += i + 1;
                        client->packet_pos += gdbstub_client_read_word(client, &k) + 1;
                        if (!k)
                            goto e22;

                        /* Check if the offset is valid. */
                        l = strlen(p);
                        if (j > l)
                            goto e00;
                        p += j;

                        /* Return the more/less flag while also clamping the length. */
                        if (k >= ((sizeof(client->response) >> 1) - 2))
                            k = (sizeof(client->response) >> 1) - 3;
                        if (k < (l - j)) {
                            client->response[client->response_pos++] = 'm';
                        } else {
                            client->response[client->response_pos++] = 'l';
                            k                                        = l - j;
                        }

                        /* Encode the data. */
                        while (k--) {
                            i = *p++;
                            if ((i == '\0') || (i == '#') || (i == '$') || (i == '*') || (i == '}')) {
                                client->response[client->response_pos++] = '}';
                                client->response[client->response_pos++] = i ^ 0x20;
                            } else {
                                client->response[client->response_pos++] = i;
                            }
                        }
                        break;
                    }
                }
            } else if (!strncmp(client->response, "Attached", 8)) {
                FAST_RESPONSE("1");
            } else if (!strcmp(client->response, "C")) {
                FAST_RESPONSE("QC1");
            } else if (!strcmp(client->response, "fThreadInfo")) {
                FAST_RESPONSE("m1");
            } else if (!strcmp(client->response, "sThreadInfo")) {
                FAST_RESPONSE("l");
            } else if (!strcmp(client->response, "Rcmd")) {
                /* Read and decode command in-place. */
                i                 = gdbstub_client_read_hex(client, (uint8_t *) client->packet, strlen(client->packet) - client->packet_pos);
                client->packet[i] = 0;
                gdbstub_log("GDB Stub: Monitor command: %s\n", client->packet);

                /* Parse the command name. */
                char *strtok_save;
                p = strtok_r(client->packet, " ", &strtok_save);
                if (!p)
                    goto ok;
                i = strlen(p) - 1; /* get last character offset */

                /* Interpret the command. */
                if (!strcmp(p, "fi")) {
                    /* Freeze the last completed frame as RGB for "fb" and report its size. */
                    frame_snap_len = frame_last_w * frame_last_h * 3;
                    if (frame_snap_len > frame_snap_size) {
                        free(frame_snap);
                        frame_snap      = (uint8_t *) malloc(frame_snap_len);
                        frame_snap_size = frame_snap ? frame_snap_len : 0;
                    }
                    if (!frame_snap)
                        frame_snap_len = 0;
                    for (i = 0; i < (frame_snap_len / 3); i++) {
                        uint32_t pixel          = video_color_transform(frame_last[i]);
                        frame_snap[(i * 3)]     = (pixel >> 16) & 0xff;
                        frame_snap[(i * 3) + 1] = (pixel >> 8) & 0xff;
                        frame_snap[(i * 3) + 2] = pixel & 0xff;
                    }
                    if (frame_snap_len)
                        client->packet_pos = sprintf(client->packet, "%d %d %u\n", frame_last_w, frame_last_h, frame_last_seq);
                    else
                        client->packet_pos = sprintf(client->packet, "0 0 0\n");
                    client->response_pos = 0;
                    gdbstub_client_respond_hex(client, (uint8_t *) client->packet, client->packet_pos);
                    break;
                } else if (!strcmp(p, "fb")) {
                    /* Stream the frozen RGB frame as partial responses, which
                       each take one round trip instead of one CPU time slice. */
                    if (!frame_snap_len)
                        goto e22;
                    k = ((sizeof(client->response) - 2) >> 1) - 1;
                    for (j = 0; j < frame_snap_len; j += k) {
                        client->response_pos                     = 0;
                        client->response[client->response_pos++] = 'O';
                        gdbstub_client_respond_hex(client, &frame_snap[j], MIN(k, frame_snap_len - j));
                        gdbstub_client_respond_partial(client);
                        if (client->gone) /* nobody will acknowledge any more */
                            break;
                    }
                } else if (!strcmp(p, "kd") || !strcmp(p, "ku")) {
                    /* Press or release a key by its set 1 scan code (E0xx for extended keys). */
                    l = (p[1] == 'd');
                    if (!(p = strtok_r(NULL, " ", &strtok_save)) || !gdbstub_num_decode(p, &j, GDB_MODE_HEX) || (j < 0) || (j > 0xffff))
                        goto e22;
                    keyboard_input_injected(l, j);
                    for (i = 0; (i < held_keys_count) && (held_keys[i] != j); i++)
                        ;
                    if (l && (i == held_keys_count) && (held_keys_count < (int) (sizeof(held_keys) / sizeof(held_keys[0]))))
                        held_keys[held_keys_count++] = j;
                    else if (!l && (i < held_keys_count))
                        held_keys[i] = held_keys[--held_keys_count];
                } else if (!strcmp(p, "mm")) {
                    /* Move the mouse by a relative amount, optionally turning the wheel. */
                    if (!(p = strtok_r(NULL, " ", &strtok_save)) || !gdbstub_num_decode(p, &j, GDB_MODE_BASE10) || !(p = strtok_r(NULL, " ", &strtok_save)) || !gdbstub_num_decode(p, &k, GDB_MODE_BASE10))
                        goto e22;
                    mouse_injected = 1;
                    mouse_scale(j, k);
                    if ((p = strtok_r(NULL, " ", &strtok_save)) && gdbstub_num_decode(p, &j, GDB_MODE_BASE10))
                        mouse_wheel_clicks(j);
                } else if (!strcmp(p, "sg")) {
                    /* Report the segment descriptor caches and descriptor table registers,
                       which the register packets don't carry, for protected mode debugging. */
                    static const char *seg_names[] = { "cs", "ss", "ds", "es", "fs", "gs" };
                    client->packet_pos = 0;
                    for (i = 0; i < 6; i++)
                        client->packet_pos += sprintf(&client->packet[client->packet_pos], "%s sel=%04X base=%08X limit=%08X access=%02X flags=%02X\n",
                                                      seg_names[i], segment_regs[i]->seg, segment_regs[i]->base, segment_regs[i]->limit,
                                                      segment_regs[i]->access, segment_regs[i]->ar_high);
                    client->packet_pos += sprintf(&client->packet[client->packet_pos], "gdt base=%08X limit=%08X\nidt base=%08X limit=%08X\n",
                                                  gdt.base, gdt.limit, idt.base, idt.limit);
                    client->packet_pos += sprintf(&client->packet[client->packet_pos], "ldt sel=%04X base=%08X limit=%08X\ntr sel=%04X base=%08X limit=%08X\n",
                                                  ldt.seg, ldt.base, ldt.limit, tr.seg, tr.base, tr.limit);
                    client->packet_pos += sprintf(&client->packet[client->packet_pos], "cpu use32=%d stack32=%d cpl=%d\n",
                                                  !!use32, !!stack32, CPL);
                    client->response_pos = 0;
                    gdbstub_client_respond_hex(client, (uint8_t *) client->packet, client->packet_pos);
                    break;
                } else if (!strcmp(p, "bl")) {
                    /* List breakpoints and watchpoints: type (Z packet number), address, length. */
                    static gdbstub_breakpoint_t **lists[] = { &first_swbreak, &first_hwbreak, &first_wwatch, &first_rwatch, &first_awatch };
                    client->packet_pos = 0;
                    for (l = 0; l < 5; l++) {
                        for (breakpoint = *lists[l]; breakpoint && (client->packet_pos < 4000); breakpoint = breakpoint->next)
                            client->packet_pos += sprintf(&client->packet[client->packet_pos], "%d %08X %X\n", l, breakpoint->addr,
                                                          (l < 2) ? 1 : (breakpoint->end - breakpoint->addr));
                    }
                    client->response_pos = 0;
                    if (client->packet_pos)
                        gdbstub_client_respond_hex(client, (uint8_t *) client->packet, client->packet_pos);
                    else {
                        FAST_RESPONSE("OK");
                    }
                    break;
                } else if (!strcmp(p, "tv") || !strcmp(p, "ts")) {
                    /* Set the INT vectors to log ("tv 21 31 33", "tv off"), and show the log state. */
                    if ((p[1] == 'v') && (p = strtok_r(NULL, " ", &strtok_save))) {
                        uint8_t  vectors[256] = { 0 };
                        uint32_t v;
                        if (strcmp(p, "off")) {
                            do {
                                if (!gdbstub_parse_hex(p, &v) || (v > 0xff))
                                    goto e22;
                                vectors[v] = 1;
                            } while ((p = strtok_r(NULL, " ", &strtok_save)));
                            if (!intlog && !(intlog = (gdbstub_intlog_t *) calloc(INTLOG_SIZE, sizeof(gdbstub_intlog_t))))
                                goto e22;
                        }
                        memcpy(intlog_vectors, vectors, sizeof(intlog_vectors));
                        for (intlog_on = 0, i = 0; i < 256; i++)
                            intlog_on |= intlog_vectors[i];
                    }
                    client->packet_pos = sprintf(client->packet, "vectors");
                    for (i = 0; i < 256; i++) {
                        if (intlog_vectors[i])
                            client->packet_pos += sprintf(&client->packet[client->packet_pos], " %02X", i);
                    }
                    client->packet_pos += sprintf(&client->packet[client->packet_pos], "\nlog first=%X next=%X size=%X record=%X\npending %d\n",
                                                  intlog_first, intlog_next, INTLOG_SIZE, (int) sizeof(gdbstub_intlog_t), int_pending_count);
                    for (i = 0; i < catch_count; i++)
                        client->packet_pos += sprintf(&client->packet[client->packet_pos], "catch %02X %02X %02X %d\n", catches[i].vector,
                                                      catches[i].ah & 0x1ff, catches[i].al & 0x1ff, catches[i].when);
                    for (i = 0; i < xrange_count; i++)
                        client->packet_pos += sprintf(&client->packet[client->packet_pos], "xrange %08X %08X\n", xrange_lo[i], xrange_hi[i]);
                    client->packet_pos += sprintf(&client->packet[client->packet_pos], "tsc %" PRIX64 " hz %d\n", tsc, cpu_s->rspeed);
                    client->response_pos = 0;
                    gdbstub_client_respond_hex(client, (uint8_t *) client->packet, client->packet_pos);
                    break;
                } else if (!strcmp(p, "tl")) {
                    /* Stream log records from a sequence number on, as raw records. */
                    uint32_t from = intlog_first;
                    uint32_t max  = INTLOG_SIZE;
                    if ((p = strtok_r(NULL, " ", &strtok_save)) && !gdbstub_parse_hex(p, &from))
                        goto e22;
                    if ((p = strtok_r(NULL, " ", &strtok_save)) && !gdbstub_parse_hex(p, &max))
                        goto e22;
                    if ((int32_t) (from - intlog_first) < 0)
                        from = intlog_first;
                    k = (((sizeof(client->response) - 2) >> 1) - 1) / sizeof(gdbstub_intlog_t);
                    while (intlog && max && ((int32_t) (intlog_next - from) > 0)) {
                        client->response_pos                     = 0;
                        client->response[client->response_pos++] = 'O';
                        for (j = 0; (j < k) && max && ((int32_t) (intlog_next - from) > 0); j++, max--, from++)
                            gdbstub_client_respond_hex(client, (uint8_t *) &intlog[from & (INTLOG_SIZE - 1)], sizeof(gdbstub_intlog_t));
                        gdbstub_client_respond_partial(client);
                        if (client->gone)
                            break;
                    }
                } else if (!strcmp(p, "mr")) {
                    /* Read RAM and ROM without side effects or faults, for memory snapshots:
                       records of {u8 readable, u32 address, u32 length} with the bytes
                       after readable ones; unreadable runs (device memory, unmapped pages)
                       carry no bytes. */
                    uint32_t addr, len;
                    uint8_t  hdr[9];
                    if (!(p = strtok_r(NULL, " ", &strtok_save)) || !gdbstub_parse_hex(p, &addr) || !(p = strtok_r(NULL, " ", &strtok_save)) || !gdbstub_parse_hex(p, &len))
                        goto e22;
                    k = (((sizeof(client->response) - 2) >> 1) - 1 - sizeof(hdr)) & ~0xfff;
                    while (len && !client->gone) {
                        uint32_t n = MIN(len, (uint32_t) k);
                        n          = MIN(n, 0x1000 - (addr & 0xfff)); /* stay within a page */
                        l          = gdbstub_peek(addr, (uint8_t *) client->packet, n);
                        hdr[0]     = !!l;
                        if (!l) {
                            /* Skip whole unreadable pages in one record. */
                            uint8_t probe;
                            while ((n < len) && !gdbstub_peek(addr + n, &probe, 1))
                                n = MIN(len, n + 0x1000);
                        } else {
                            n = l;
                            /* Coalesce further readable pages into this record. */
                            while ((n < len) && (n < (uint32_t) k)) {
                                j = gdbstub_peek(addr + n, (uint8_t *) &client->packet[n], MIN(MIN(len - n, (uint32_t) k - n), 0x1000));
                                if (!j)
                                    break;
                                n += j;
                            }
                        }
                        memcpy(&hdr[1], &addr, 4);
                        memcpy(&hdr[5], &n, 4);
                        client->response_pos                     = 0;
                        client->response[client->response_pos++] = 'O';
                        gdbstub_client_respond_hex(client, hdr, sizeof(hdr));
                        if (l)
                            gdbstub_client_respond_hex(client, (uint8_t *) client->packet, n);
                        gdbstub_client_respond_partial(client);
                        addr += n;
                        len -= n;
                    }
                } else if (!strcmp(p, "tc")) {
                    /* Clear the log. */
                    intlog_first = intlog_next;
                } else if (!strcmp(p, "ca")) {
                    /* Add an INT catchpoint: vector [AH|*] [AL|*] [call|ret|both]. */
                    gdbstub_catch_t c = { 0, -1, -1, 1 };
                    uint32_t        v;
                    if (!(p = strtok_r(NULL, " ", &strtok_save)) || !gdbstub_parse_hex(p, &v) || (v > 0xff) || (catch_count >= (int) (sizeof(catches) / sizeof(catches[0]))))
                        goto e22;
                    c.vector = v;
                    for (i = 0; i < 2; i++) {
                        if (!(p = strtok_r(NULL, " ", &strtok_save)))
                            break;
                        if (!strcmp(p, "call") || !strcmp(p, "ret") || !strcmp(p, "both"))
                            break;
                        if (strcmp(p, "*")) {
                            if (!gdbstub_parse_hex(p, &v) || (v > 0xff))
                                goto e22;
                            if (i)
                                c.al = v;
                            else
                                c.ah = v;
                        }
                        p = NULL;
                    }
                    if (p || (p = strtok_r(NULL, " ", &strtok_save))) {
                        if (!strcmp(p, "ret"))
                            c.when = 2;
                        else if (!strcmp(p, "both"))
                            c.when = 3;
                        else if (strcmp(p, "call"))
                            goto e22;
                    }
                    catches[catch_count++] = c;
                } else if (!strcmp(p, "cx")) {
                    /* Remove all INT catchpoints. */
                    catch_count = 0;
                } else if (!strcmp(p, "xr")) {
                    /* Stop when execution enters one of up to 8 linear ranges (start end, end exclusive);
                       no ranges clears them. The ranges are dropped when one is entered. */
                    uint32_t lo[8], hi[8];
                    for (l = 0; (p = strtok_r(NULL, " ", &strtok_save)); l++) {
                        if ((l >= 8) || !gdbstub_parse_hex(p, &lo[l]) || !(p = strtok_r(NULL, " ", &strtok_save)) || !gdbstub_parse_hex(p, &hi[l]))
                            goto e22;
                    }
                    memcpy(xrange_lo, lo, sizeof(lo));
                    memcpy(xrange_hi, hi, sizeof(hi));
                    xrange_count = l;
                } else if (!strcmp(p, "state")) {
                    /* Report whether the CPU is running, for clients that connect while holding. */
                    client->packet_pos   = sprintf(client->packet, "running %d\n", gdbstub_step == GDBSTUB_EXEC);
                    client->response_pos = 0;
                    gdbstub_client_respond_hex(client, (uint8_t *) client->packet, client->packet_pos);
                    break;
                } else if (!strcmp(p, "hold")) {
                    /* Set or show whether a paused CPU stays paused after the last client leaves. */
                    if ((p = strtok_r(NULL, " ", &strtok_save))) {
                        if (!gdbstub_num_decode(p, &j, GDB_MODE_BASE10))
                            goto e22;
                        hold_on_disconnect = !!j;
                    }
                    client->packet_pos   = sprintf(client->packet, "hold %d\n", hold_on_disconnect);
                    client->response_pos = 0;
                    gdbstub_client_respond_hex(client, (uint8_t *) client->packet, client->packet_pos);
                    break;
                } else if (!strcmp(p, "mb")) {
                    /* Set the mouse buttons held (bit 0 left, bit 1 right, bit 2 middle). */
                    if (!(p = strtok_r(NULL, " ", &strtok_save)) || !gdbstub_num_decode(p, &j, GDB_MODE_HEX))
                        goto e22;
                    mouse_injected = 1;
                    mouse_set_buttons_ex(j);
                } else if (p[0] == 'i') {
                    /* Read I/O operation width. */
                    l = (i < 1) ? '\0' : p[i];

                    /* Read optional I/O port. */
                    if (!(p = strtok_r(NULL, " ", &strtok_save)) || !gdbstub_num_decode(p, &j, GDB_MODE_HEX) || (j < 0) || (j >= 65536))
                        j = client->last_io_base;
                    else
                        client->last_io_base = j;

                    /* Read optional length. */
                    if (!(p = strtok_r(NULL, " ", &strtok_save)) || !gdbstub_num_decode(p, &k, GDB_MODE_BASE10))
                        k = client->last_io_len;
                    else
                        client->last_io_len = k;

                    /* Clamp length. */
                    if (k < 1)
                        k = 1;
                    if (k > (65536 - j))
                        k = 65536 - j;

                    /* Read ports. */
                    i = 0;
                    while (i < k) {
                        if ((i % 16) == 0) {
                            if (i) {
                                client->packet[client->packet_pos++] = '\n';

                                /* Provide partial response with the last line. */
                                client->response_pos                     = 0;
                                client->response[client->response_pos++] = 'O';
                                gdbstub_client_respond_hex(client, (uint8_t *) client->packet, client->packet_pos);
                                gdbstub_client_respond_partial(client);
                            }
                            client->packet_pos = sprintf(client->packet, "%04X:", j + i);
                        }
                        /* Act according to I/O operation width. */
                        switch (l) {
                            case 'd':
                            case 'l':
                                client->packet_pos += sprintf(&client->packet[client->packet_pos], " %08X", inl(j + i));
                                i += 4;
                                break;

                            case 'w':
                                client->packet_pos += sprintf(&client->packet[client->packet_pos], " %04X", inw(j + i));
                                i += 2;
                                break;

                            case 'b':
                            case '\0':
                                client->packet_pos += sprintf(&client->packet[client->packet_pos], " %02X", inb(j + i));
                                i++;
                                break;

                            default:
                                goto unknown;
                        }
                    }
                    client->packet[client->packet_pos++] = '\n';

                    /* Respond with the final line. */
                    client->response_pos = 0;
                    gdbstub_client_respond_hex(client, (uint8_t *) &client->packet, client->packet_pos);
                    break;
                } else if (p[0] == 'o') {
                    /* Read I/O operation width. */
                    l = (i < 1) ? '\0' : p[i];

                    /* Read optional I/O port. */
                    if (!(p = strtok_r(NULL, " ", &strtok_save)) || !gdbstub_num_decode(p, &j, GDB_MODE_HEX) || (j < 0) || (j >= 65536))
                        j = -1;

                    /* Read optional value. */
                    if (!(p = strtok_r(NULL, " ", &strtok_save)) || !gdbstub_num_decode(p, &k, GDB_MODE_HEX)) {
                        if (j == -1)
                            k = client->last_io_value;
                        else
                            k = j; /* only one specified = treat as value on last port */
                        j = -1;
                    }
                    if (j == -1)
                        j = client->last_io_base;
                    else
                        client->last_io_base = j;
                    client->last_io_value = k;

                    /* Write port. */
                    switch (l) {
                        case 'd':
                        case 'l':
                            outl(j, k);
                            break;

                        case 'w':
                            outw(j, k);
                            break;

                        case 'b':
                        case 't':
                        case '\0':
                            outb(j, k);
                            break;

                        default:
                            goto unknown;
                    }
                } else if (p[0] == 'r') {
                    pc_reset_hard();
                } else if ((p[0] == '?') || !strcmp(p, "help")) {
                    FAST_RESPONSE_HEX(
                        "Commands:\n"
                        "- ib/iw/il [port [length]] - Read {length} (default 1) I/O ports starting from {port} (default last)\n"
                        "- ob/ow/ol [[port] value] - Write {value} to I/O {port} (both default last)\n"
                        "- r - Hard reset the emulated machine\n"
                        "- fi - Freeze the last frame for fb; prints {width} {height} {frame number}\n"
                        "- fb - Read the frozen frame as RGB bytes\n"
                        "- kd/ku scancode - Press/release a key (hex set 1 scan code, E0xx if extended)\n"
                        "- mm dx dy [dz] - Move the mouse (decimal, relative)\n"
                        "- mb buttons - Set the mouse buttons held (hex mask: 1 left, 2 right, 4 middle)\n"
                        "- sg - Show segment descriptor caches, descriptor tables and code/stack size\n"
                        "- hold [0|1] - Keep the CPU's run state and breakpoints when the last client disconnects,\n"
                        "  and don't pause it when the next one connects\n"
                        "- state - Show whether the CPU is running\n"
                        "- bl - List breakpoints and watchpoints: {Z type} {address} {length}\n"
                        "- tv [vector...|off] - Log INT calls to these hex vectors; shows the log state (also ts)\n"
                        "- tl [from [count]] - Read log records from sequence number {from} as raw bytes\n"
                        "- tc - Clear the INT log\n"
                        "- mr address length - Read RAM/ROM without side effects: {readable} {address} {length} [bytes] records\n"
                        "- ca vector [ah|*] [al|*] [call|ret|both] - Stop before matching INT calls and/or at their returns\n"
                        "- cx - Remove all INT catchpoints\n"
                        "- xr [start end]... - Stop when execution enters a linear range (end exclusive); none clears\n");
                    break;
                } else {
unknown:
                    FAST_RESPONSE_HEX("Unknown command\n");
                    break;
                }

                goto ok;
            }
            break;

        case 'z': /* remove break/watchpoint */
        case 'Z': /* insert break/watchpoint */

            /* Parse breakpoint type. */
            switch (client->packet[1]) {
                case '0': /* software breakpoint */
                    first_breakpoint = &first_swbreak;
                    break;

                case '1': /* hardware breakpoint */
                    first_breakpoint = &first_hwbreak;
                    break;

                case '2': /* write watchpoint */
                    first_breakpoint = &first_wwatch;
                    break;

                case '3': /* read watchpoint */
                    first_breakpoint = &first_rwatch;
                    break;

                case '4': /* access watchpoint */
                    first_breakpoint = &first_awatch;
                    break;

                default:                      /* unknown type */
                    client->packet[2] = '\0'; /* force address check to fail */
                    break;
            }

            /* Read address. */
            if (client->packet[2] != ',')
                break;
            client->packet_pos = 3;
            if (!(i = gdbstub_client_read_word(client, &j)))
                break;
            client->packet_pos += i;
            if (client->packet[client->packet_pos++] == ',')
                gdbstub_client_read_word(client, &k);
            else
                k = 1;

            /* Test writability of software breakpoint. */
            if (client->packet[1] == '0') {
                buf[0] = readmembl(j);
                writemembl(j, 0xcc);
                buf[1] = readmembl(j);
                writemembl(j, buf[0]);
                if (buf[1] != 0xcc)
                    goto end;
            }

            /* Find an existing breakpoint with this address. */
            breakpoint = *first_breakpoint;
            while (breakpoint) {
                if (breakpoint->addr == j)
                    break;
                prev_breakpoint = breakpoint;
                breakpoint      = breakpoint->next;
            }

            /* Check if the breakpoint is already present (when inserting) or not found (when removing). */
            if ((!!breakpoint) ^ (client->packet[0] == 'z'))
                goto e22;

            /* Insert or remove the breakpoint. */
            if (client->packet[0] != 'z') {
                /* Allocate a new breakpoint. */
                breakpoint       = calloc(1, sizeof(gdbstub_breakpoint_t));
                breakpoint->addr = j;
                breakpoint->end  = j + k;
                breakpoint->next = NULL;

                /* Add the new breakpoint to the list. */
                if (!(*first_breakpoint))
                    *first_breakpoint = breakpoint;
                else if (prev_breakpoint)
                    prev_breakpoint->next = breakpoint;
            } else {
                /* Remove breakpoint from the list. */
                if (breakpoint == *first_breakpoint)
                    *first_breakpoint = breakpoint->next;
                else if (prev_breakpoint)
                    prev_breakpoint->next = breakpoint->next;

                /* De-allocate breakpoint. */
                free(breakpoint);
            }

            /* Update the page watchpoint map if we're dealing with a watchpoint. */
            if (client->packet[1] >= '2') {
                /* Clear this watchpoint's corresponding page map groups,
                   as everything is going to be recomputed soon anyway. */
                memset(&gdbstub_watch_pages[((uint32_t) j) >> (MEM_GRANULARITY_BITS + 6)], 0,
                       (((((uint32_t) j + (uint32_t) k - 1) >> (MEM_GRANULARITY_BITS + 6))
                         - (((uint32_t) j) >> (MEM_GRANULARITY_BITS + 6))) + 1) * sizeof(gdbstub_watch_pages[0]));

                /* Go through all watchpoint lists. */
                l          = 0;
                breakpoint = first_rwatch;
                while (1) {
                    if (breakpoint) {
                        /* Flag this watchpoint's corresponding pages as having a watchpoint. */
                        k = (breakpoint->end - 1) >> MEM_GRANULARITY_BITS;
                        for (i = breakpoint->addr >> MEM_GRANULARITY_BITS; i <= k; i++)
                            gdbstub_watch_pages[i >> 6] |= (1ULL << (i & 63));

                        breakpoint = breakpoint->next;
                    } else {
                        /* Jump from list to list as a shortcut. */
                        if (l == 0)
                            breakpoint = first_wwatch;
                        else if (l == 1)
                            breakpoint = first_awatch;
                        else
                            break;
                        l++;
                    }
                }

                /* Drop cached translations, so that watched pages stop
                   bypassing the checks through the MMU lookup caches. */
                flushmmucache();
            }

            /* Respond positively. */
            goto ok;
    }
end:
    /* Send response. */
    gdbstub_client_respond(client);
}

static void
gdbstub_release_input(void)
{
    /* Release keys and mouse buttons a client left held, and return the mouse to the host. */
    while (held_keys_count)
        keyboard_input_injected(0, held_keys[--held_keys_count]);
    if (mouse_injected) {
        mouse_set_buttons_ex(0);
        mouse_injected = 0;
    }
}

static void
gdbstub_clear_points(void)
{
    gdbstub_breakpoint_t **lists[] = { &first_swbreak, &first_hwbreak, &first_rwatch, &first_wwatch, &first_awatch };
    gdbstub_breakpoint_t  *breakpoint;

    for (int l = 0; l < 5; l++) {
        while ((breakpoint = *lists[l])) {
            *lists[l] = breakpoint->next;
            free(breakpoint);
        }
    }
    memset(gdbstub_watch_pages, 0, sizeof(gdbstub_watch_pages));
    flushmmucache();
}

static void
gdbstub_cpu_exec(int32_t cycs)
{
    /* Flag that we're now in the debugger context to avoid triggering watchpoints. */
    in_gdbstub = 1;

    /* Clean up after the last client left. */
    if (cleanup_pending) {
        cleanup_pending = 0;
        gdbstub_release_input();
        if (!hold_on_disconnect) {
            gdbstub_clear_points();
            gdbstub_int_clear();
        }
    }

    /* Handle CPU execution if it isn't paused. */
    int ran = (gdbstub_step <= GDBSTUB_SSTEP);
    if (ran) {
        /* Swap in any software breakpoints. */
        gdbstub_breakpoint_t *swbreak = first_swbreak;
        while (swbreak) {
            /* Swap the INT 3 opcode into the address. */
            swbreak->orig_val = readmembl(swbreak->addr);
            writemembl(swbreak->addr, 0xcc);
            swbreak = swbreak->next;
        }

        /* Call the original cpu_exec function outside the debugger context. */
        if ((gdbstub_step == GDBSTUB_SSTEP) && ((cycles + cycs) <= 0))
            cycs += -(cycles + cycs) + 1;
        in_gdbstub = 0;
        cpu_exec_shadow(cycs);
        in_gdbstub = 1;

        /* Swap out any software breakpoints. */
        swbreak = first_swbreak;
        while (swbreak) {
            if (readmembl(swbreak->addr) == 0xcc)
                writemembl(swbreak->addr, swbreak->orig_val);
            swbreak = swbreak->next;
        }
    }

    /* Populate the stop reason when the CPU has just stopped, and keep it while
       the CPU stays stopped, so that a client connecting later can still ask
       ("?") why. */
    if (gdbstub_step <= GDBSTUB_EXEC) {
        stop_reason_len = 0;
        was_stopped     = 0;
    } else if (!ran && was_stopped) {
        gdbstub_step = GDBSTUB_BREAK;
    } else {
        was_stopped     = 1;
        stop_reason_len = 0;
        /* Assemble stop reason manually, avoiding sprintf and friends for performance. */
        stop_reason[stop_reason_len++] = 'T';
        stop_reason[stop_reason_len++] = '0';
        stop_reason[stop_reason_len++] = '0' + ((gdbstub_step == GDBSTUB_BREAK) ? GDB_SIGINT : GDB_SIGTRAP);

        /* Add extended break reason. Catchpoints and range catches are
           reported with keys GDB ignores, on top of a plain SIGTRAP. */
        if (gdbstub_step == GDBSTUB_BREAK_CATCH) {
            stop_reason_len += sprintf(&stop_reason[stop_reason_len], "%s:%X;seq:%X;", catch_stop_return ? "intret" : "intcall",
                                       catch_stop_vector, catch_stop_seq);
        } else if (gdbstub_step == GDBSTUB_BREAK_RANGE) {
            stop_reason_len += sprintf(&stop_reason[stop_reason_len], "xrange:%X;", xrange_hit);
        } else if (gdbstub_step >= GDBSTUB_BREAK_RWATCH) {
            if (gdbstub_step != GDBSTUB_BREAK_WWATCH)
                stop_reason[stop_reason_len++] = (gdbstub_step == GDBSTUB_BREAK_RWATCH) ? 'r' : 'a';
            stop_reason[stop_reason_len++] = 'w';
            stop_reason[stop_reason_len++] = 'a';
            stop_reason[stop_reason_len++] = 't';
            stop_reason[stop_reason_len++] = 'c';
            stop_reason[stop_reason_len++] = 'h';
            stop_reason[stop_reason_len++] = ':';
            stop_reason_len += sprintf(&stop_reason[stop_reason_len], "%X;", watch_addr);
        } else if (gdbstub_step >= GDBSTUB_BREAK_SW) {
            stop_reason[stop_reason_len++] = (gdbstub_step == GDBSTUB_BREAK_SW) ? 's' : 'h';
            stop_reason[stop_reason_len++] = 'w';
            stop_reason[stop_reason_len++] = 'b';
            stop_reason[stop_reason_len++] = 'r';
            stop_reason[stop_reason_len++] = 'e';
            stop_reason[stop_reason_len++] = 'a';
            stop_reason[stop_reason_len++] = 'k';
            stop_reason[stop_reason_len++] = ':';
            stop_reason[stop_reason_len++] = ';';
        }

        /* Add register dump. */
        uint8_t buf[10] = { 0 };
        int     j;
        for (int i = 0; i < GDB_REG_MAX; i++) {
            if (i >= 0x10)
                stop_reason[stop_reason_len++] = gdbstub_hex_encode(i >> 4);
            stop_reason[stop_reason_len++] = gdbstub_hex_encode(i & 0x0f);
            stop_reason[stop_reason_len++] = ':';
            j                              = gdbstub_client_read_reg(i, buf);
            for (int k = 0; k < j; k++) {
                stop_reason[stop_reason_len++] = gdbstub_hex_encode(buf[k] >> 4);
                stop_reason[stop_reason_len++] = gdbstub_hex_encode(buf[k] & 0x0f);
            }
            stop_reason[stop_reason_len++] = ';';
        }
        stop_reason[stop_reason_len] = '\0';

        /* Don't execute the CPU any further if single-stepping. */
        gdbstub_step = GDBSTUB_BREAK;
    }

    /* Return the framerate to normal. */
    gdbstub_next_asap = 0;

    /* Process client packets. */
    thread_wait_mutex(client_list_mutex);
    gdbstub_client_t *client = first_client;
    while (client) {
        /* Report stop reason if the client is waiting for one. */
        if (client->waiting_stop && stop_reason_len) {
            client->waiting_stop = 0;

            /* Wait for any pending responses to be acknowledged. */
            if (!thread_wait_event(client->response_event, -1)) {
                /* Block other responses from being written while this one isn't acknowledged. */
                thread_reset_event(client->response_event);

                /* Write stop reason response. */
                strcpy(client->response, stop_reason);
                client->response_pos = stop_reason_len;
                gdbstub_client_respond(client);
            } else {
                gdbstub_log("GDB Stub: Timed out waiting for client %s:%d\n", inet_ntoa(client->addr.sin_addr), client->addr.sin_port);
            }
        }

        if (client->has_packet) {
            gdbstub_client_packet(client);
            client->has_packet = client->packet_pos = 0;
            thread_set_event(client->processed_event);
        }

#ifdef GDBSTUB_ALLOW_MULTI_CLIENTS
        client = client->next;
#else
        break;
#endif
    }
    thread_release_mutex(client_list_mutex);

    /* Flag that we're now out of the debugger context. */
    in_gdbstub = 0;
}

static void
gdbstub_client_thread(void *priv)
{
    gdbstub_client_t *client = (gdbstub_client_t *) priv;
    uint8_t           buf[256];
    ssize_t           bytes_read;

    gdbstub_log("GDB Stub: New connection from %s:%d\n", inet_ntoa(client->addr.sin_addr), client->addr.sin_port);

    /* Allow packets to be processed. */
    thread_set_event(client->processed_event);

    /* Read data from client. */
    while ((bytes_read = recv(client->socket, (char *) buf, sizeof(buf), 0)) > 0) {
        for (ssize_t i = 0; i < bytes_read; i++) {
            switch (buf[i]) {
                case '$': /* packet start */
                    /* Wait for any existing packets to be processed. */
                    thread_wait_event(client->processed_event, -1);
                    thread_set_event(client->processed_event);

                    client->packet_pos = 0;
                    break;

                case '-': /* negative acknowledgement */
                    /* Retransmit the current response. */
                    gdbstub_client_respond(client);
                    break;

                case '+': /* positive acknowledgement */
                    /* Allow another response to be written. */
                    thread_set_event(client->response_event);
                    break;

                case 0x03: /* break */
                    /* Wait for any existing packets to be processed. */
                    thread_wait_event(client->processed_event, -1);
                    thread_set_event(client->processed_event);

                    /* Break immediately. */
                    gdbstub_log("GDB Stub: Break requested\n");
                    gdbstub_break();
                    break;

                default:
                    /* Wait for any existing packets to be processed, just in case. */
                    thread_wait_event(client->processed_event, -1);
                    thread_set_event(client->processed_event);

                    if (client->packet_pos < (sizeof(client->packet) - 1)) {
                        /* Append byte to the packet. */
                        client->packet[client->packet_pos++] = buf[i];

                        /* Check if we're at the end of a packet. */
                        if ((client->packet_pos >= 3) && (client->packet[client->packet_pos - 3] == '#')) { /* packet checksum start */
                            /* Small hack to speed up IDA instruction trace mode. */
                            if (*((uint32_t *) client->packet) == ('H' | ('c' << 8) | ('1' << 16) | ('#' << 24))) {
                                /* Send pre-computed response. */
                                send(client->socket, "+$OK#9A", 7, MSG_NOSIGNAL);

                                /* Skip processing. */
                                continue;
                            }

                            /* Flag that a packet should be processed. */
                            client->packet[client->packet_pos] = '\0';
                            thread_reset_event(client->processed_event);
                            gdbstub_next_asap = client->has_packet = 1;
                        }
                    }
                    break;
            }
        }
    }

    gdbstub_log("GDB Stub: Connection with %s:%d broken\n", inet_ntoa(client->addr.sin_addr), client->addr.sin_port);

    /* Unblock anyone waiting on the response event. */
    client->gone = 1;
    thread_set_event(client->response_event);

    /* Close the socket and remove this client from the list. The socket is
       closed under the list mutex so that the server thread, which kicks out
       the previous client when a new one connects, can't act on a descriptor
       that has already been closed and reused for the new connection. */
    thread_wait_mutex(client_list_mutex);
    if (client->socket != -1) {
        close(client->socket);
        client->socket = -1;
    }
#ifdef GDBSTUB_ALLOW_MULTI_CLIENTS
    if (client == first_client) {
#endif
        first_client = client->next;
        if (first_client == NULL) {
            last_client  = NULL;
            /* Unless asked to hold, unpause the CPU when all clients are
               disconnected, and drop their breakpoints and watchpoints so
               the guest can't stop with nobody attached. Injected keys and
               mouse buttons are released either way. */
            if (!hold_on_disconnect)
                gdbstub_step = GDBSTUB_EXEC;
            cleanup_pending = 1;
        }
#ifdef GDBSTUB_ALLOW_MULTI_CLIENTS
    } else {
        other_client = first_client;
        while (other_client) {
            if (other_client->next == client) {
                if (last_client == client)
                    last_client = other_client;
                other_client->next = client->next;
                break;
            }
            other_client = other_client->next;
        }
    }
#endif

    free(client);
    thread_release_mutex(client_list_mutex);
}

static void
gdbstub_server_thread(void *priv)
{
    /* Listen on GDB socket. */
    listen(gdbstub_socket, 1);

    /* Accept connections. */
    gdbstub_client_t *client;
    socklen_t         sl = sizeof(struct sockaddr_in);
    while (1) {
        /* Allocate client structure. */
        client = calloc(1, sizeof(gdbstub_client_t));
        memset(client, 0, sizeof(gdbstub_client_t));
        client->processed_event = thread_create_event();
        client->response_event  = thread_create_event();

        /* Accept connection. */
        client->socket = accept(gdbstub_socket, (struct sockaddr *) &client->addr, &sl);
        if (client->socket < 0)
            break;

        /* Responses are written in several small pieces; don't let Nagle's
           algorithm hold each one back until the client's delayed ACK. */
        int nodelay = 1;
        setsockopt(client->socket, IPPROTO_TCP, TCP_NODELAY,
#ifdef _WIN32
                   (const char *) &nodelay,
#else
                   &nodelay,
#endif
                   sizeof(nodelay));
#ifdef SO_NOSIGPIPE
        setsockopt(client->socket, SOL_SOCKET, SO_NOSIGPIPE, &nodelay, sizeof(nodelay));
#endif

        /* Add to client list. */
        thread_wait_mutex(client_list_mutex);
        if (first_client) {
#ifdef GDBSTUB_ALLOW_MULTI_CLIENTS
            last_client->next = client;
            last_client       = client;
#else
            first_client->next = last_client = client;
            if (first_client->socket != -1) /* its thread closes it */
                shutdown(first_client->socket, GDBSTUB_SHUT_RDWR);
#endif
        } else {
            first_client = last_client = client;
        }
        thread_release_mutex(client_list_mutex);

        /* Pause CPU execution, as GDB expects on attach, unless the last
           client asked to hold the CPU's state across connections. */
        if (!hold_on_disconnect)
            gdbstub_break();

        /* Start client thread. */
        thread_create(gdbstub_client_thread, client);
    }

    /* Deallocate the redundant client structure. */
    thread_destroy_event(client->processed_event);
    thread_destroy_event(client->response_event);
    free(client);
}

void
gdbstub_cpu_init(void)
{
    /* Replace cpu_exec with our own function if the GDB stub is active. */
    if ((gdbstub_socket != -1) && (cpu_exec != gdbstub_cpu_exec)) {
        cpu_exec_shadow = cpu_exec;
        cpu_exec        = gdbstub_cpu_exec;
    }
}

/* Copy guest memory at a linear address without side effects: only RAM and
   ROM are read (never device memory such as VGA, whose reads change state),
   through the page tables without faulting, and outside the MMU caches.
   Returns how many bytes could be read. */
static int
gdbstub_peek(uint32_t addr, uint8_t *buf, int len)
{
    int i = 0;
    while (i < len) {
        uint64_t phys = addr;
        if (cr0 >> 31) {
            int old_cpl_override = cpl_override;
            cpl_override         = 1;
            phys                 = mmutranslate_noabrt(addr, 0);
            cpl_override         = old_cpl_override;
            if (phys > 0xffffffffULL)
                break;
        }
        phys &= rammask;
        const uint8_t *page = _mem_exec[phys >> MEM_GRANULARITY_BITS];
        if (!page)
            break;
        int n = MIN(len - i, 0x1000 - (int) (addr & 0xfff));
        memcpy(&buf[i], &page[phys & MEM_GRANULARITY_MASK], n);
        i += n;
        addr += n;
    }
    return i;
}

static int
gdbstub_catch_match(int vector, int ah, int al, int when)
{
    for (int i = 0; i < catch_count; i++) {
        if ((catches[i].vector == vector) && (catches[i].when & when) && ((catches[i].ah < 0) || (catches[i].ah == ah)) && ((catches[i].al < 0) || (catches[i].al == al)))
            return 1;
    }
    return 0;
}

static void
gdbstub_int_pop(int index, int status)
{
    gdbstub_intpending_t *pending = &int_pending[index];
    int_ret_hash[INT_RET_HASH(pending->ret)]--;
    if (intlog && (status == INTLOG_NO_RETURN)) {
        gdbstub_intlog_t *rec = &intlog[pending->seq & (INTLOG_SIZE - 1)];
        if (rec->seq == pending->seq)
            rec->info = (rec->info & 0x00ffffff) | (INTLOG_NO_RETURN << 24);
    }
    memmove(pending, pending + 1, (int_pending_count - index - 1) * sizeof(gdbstub_intpending_t));
    int_pending_count--;
}

static void
gdbstub_int_clear(void)
{
    intlog_on = int_pending_count = catch_count = xrange_count = 0;
    memset(intlog_vectors, 0, sizeof(intlog_vectors));
    memset(int_ret_hash, 0, sizeof(int_ret_hash));
}

/* Called by INT n instructions with EIP already past the instruction: logs
   the call and remembers where it returns to. */
void
gdbstub_int(uint8_t vector)
{
    if (!intlog_on && !catch_count)
        return;

    uint32_t ip = use32 ? cpu_state.pc : (cpu_state.pc & 0xffff);

    int track = intlog_vectors[vector] || (catch_count && gdbstub_catch_match(vector, AH, AL, 2));
    if (!track)
        return;

    int      mode = !(msw & 1) ? 0 : ((cpu_state.eflags & VM_FLAG) ? 1 : (use32 ? 3 : 2));
    uint32_t sp   = stack32 ? ESP : SP;
    uint32_t seq  = 0;

    if (intlog_vectors[vector] && intlog) {
        int               old_in_gdbstub = in_gdbstub;
        uint32_t          mask           = (mode == 3) ? 0xffffffff : 0xffff;
        gdbstub_intlog_t *rec;

        seq = intlog_next++;
        if (!seq) /* 0 means "not logged" in the pending list */
            seq = intlog_next++;
        if ((intlog_next - intlog_first) > INTLOG_SIZE)
            intlog_first = intlog_next - INTLOG_SIZE;
        rec = &intlog[seq & (INTLOG_SIZE - 1)];
        memset(rec, 0, sizeof(gdbstub_intlog_t));
        rec->seq     = seq;
        rec->count   = 1;
        rec->info    = vector | (mode << 8) | (MIN(int_pending_count, 255) << 16) | (INTLOG_PENDING_CALL << 24);
        rec->tsc_lo  = (uint32_t) tsc;
        rec->tsc_hi  = (uint32_t) (tsc >> 32);
        rec->sel_cs  = CS;
        rec->base_cs = cs;
        rec->eip     = (ip - 2) & (use32 ? 0xffffffff : 0xffff);
        rec->sel_ss  = SS;
        rec->esp     = sp;
        rec->sel_ds  = DS;
        rec->sel_es  = ES;
        rec->in[0]   = EAX;
        rec->in[1]   = EBX;
        rec->in[2]   = ECX;
        rec->in[3]   = EDX;
        rec->in[4]   = ESI;
        rec->in[5]   = EDI;
        rec->lin[0]  = ds + (EDX & mask);
        rec->lin[1]  = ds + (ESI & mask);
        rec->lin[2]  = es + (EDI & mask);
        in_gdbstub   = 1;
        rec->lens    = gdbstub_peek(rec->lin[0], rec->bytes[0], INTLOG_BYTES) | (gdbstub_peek(rec->lin[1], rec->bytes[1], INTLOG_BYTES) << 8) | (gdbstub_peek(rec->lin[2], rec->bytes[2], INTLOG_BYTES) << 16);
        in_gdbstub   = old_in_gdbstub;
    }

    /* Remember where the call returns to, to log its results and check return catchpoints. */
    if (int_pending_count == INTLOG_PENDING)
        gdbstub_int_pop(0, INTLOG_NO_RETURN);
    gdbstub_intpending_t *pending = &int_pending[int_pending_count++];
    pending->ret                  = cs + ip;
    pending->stack_sel            = SS;
    pending->stack_ptr            = sp;
    pending->seq                  = seq;
    pending->vector               = vector;
    pending->ah                   = AH;
    pending->al                   = AL;
    int_ret_hash[INT_RET_HASH(pending->ret)]++;
}

/* Check whether the instruction about to run is where a pending INT call
   returns to. Returns 1 if a return catchpoint stops the CPU (only when
   may_stop: another stop reason raised by the same instruction wins). */
static int
gdbstub_int_returned(uint32_t addr, int may_stop)
{
    uint32_t sp = stack32 ? ESP : SP;
    for (int i = int_pending_count - 1; i >= 0; i--) {
        gdbstub_intpending_t *pending = &int_pending[i];
        if ((pending->ret != addr) || (pending->stack_sel != SS))
            continue;
        /* INT 25h/26h return with the flags still on the stack. */
        if ((pending->stack_ptr != sp) && !(((pending->vector == 0x25) || (pending->vector == 0x26)) && (((pending->stack_ptr - 2) & (stack32 ? 0xffffffff : 0xffff)) == sp)))
            continue;

        /* Calls made since this one never returned. */
        while (int_pending_count > (i + 1))
            gdbstub_int_pop(int_pending_count - 1, INTLOG_NO_RETURN);

        if (intlog && pending->seq) {
            gdbstub_intlog_t *rec = &intlog[pending->seq & (INTLOG_SIZE - 1)];
            if (rec->seq == pending->seq) {
                int old_in_gdbstub = in_gdbstub;
                rec->info          = (rec->info & 0x00ffffff) | (INTLOG_RETURNED << 24);
                rec->out[0]        = EAX;
                rec->out[1]        = EBX;
                rec->out[2]        = ECX;
                rec->out[3]        = EDX;
                rec->out[4]        = ESI;
                rec->out[5]        = EDI;
                gdbstub_flags_rebuild();
                rec->out[6] = cpu_state.flags | ((uint32_t) cpu_state.eflags << 16);
                rec->out[7] = DS;
                rec->out[8] = ES;
                in_gdbstub  = 1;
                rec->lens |= gdbstub_peek(rec->lin[0], rec->bytes[3], INTLOG_BYTES) << 24;
                rec->lens2  = gdbstub_peek(rec->lin[2], rec->bytes[4], INTLOG_BYTES);
                in_gdbstub  = old_in_gdbstub;

                /* Fold a call identical to the one before it, in and out, into that
                   one, so polling loops (INT 16h AH=01h...) don't flood the log. */
                gdbstub_intlog_t *prev = &intlog[(pending->seq - 1) & (INTLOG_SIZE - 1)];
                if ((pending->seq == (intlog_next - 1)) && ((int32_t) (pending->seq - intlog_first) > 0) && (prev->seq == (pending->seq - 1)) && (prev->info == rec->info) && (prev->count < 0xffffffff) && !memcmp(&prev->sel_cs, &rec->sel_cs, offsetof(gdbstub_intlog_t, count) - offsetof(gdbstub_intlog_t, sel_cs)) && !memcmp(prev->bytes, rec->bytes, sizeof(rec->bytes))) {
                    prev->count++;
                    intlog_next--;
                    pending->seq--;
                }
            }
        }

        int vector = pending->vector, ah = pending->ah, al = pending->al;
        uint32_t seq = pending->seq;
        gdbstub_int_pop(i, INTLOG_RETURNED);
        if (may_stop && catch_count && gdbstub_catch_match(vector, ah, al, 2)) {
            catch_stop_vector = vector;
            catch_stop_return = 1;
            catch_stop_seq    = seq;
            gdbstub_step      = GDBSTUB_BREAK_CATCH;
            return 1;
        }
        return 0;
    }
    return 0;
}

/* Check whether the instruction about to run is an INT matching a call
   catchpoint, and stop before it if so. */
static int
gdbstub_catch_call(uint32_t addr)
{
    uint8_t buf[8];
    int     n = gdbstub_peek(addr, buf, sizeof(buf));
    int     i;

    for (i = 0; (i < n) && ((buf[i] == 0x26) || (buf[i] == 0x2e) || (buf[i] == 0x36) || (buf[i] == 0x3e) || (buf[i] == 0x64) ||
                            (buf[i] == 0x65) || (buf[i] == 0x66) || (buf[i] == 0x67) || (buf[i] == 0xf0) || (buf[i] == 0xf2) || (buf[i] == 0xf3));
         i++)
        ;
    if (((i + 1) >= n) || (buf[i] != 0xcd) || !gdbstub_catch_match(buf[i + 1], AH, AL, 1))
        return 0;
    catch_stop_vector = buf[i + 1];
    catch_stop_return = 0;
    catch_stop_seq    = 0;
    gdbstub_step      = GDBSTUB_BREAK_CATCH;
    return 1;
}

/* Called after every instruction (and interrupt delivery), before the next
   one runs. Nonzero stops the CPU. */
int
gdbstub_instruction(void)
{
    /* A stop raised during the instruction (watchpoint, INT 3) wins over the checks below. */
    int stopped = (gdbstub_step >= GDBSTUB_BREAK_SW);

    if (int_pending_count | xrange_count | catch_count) {
        uint32_t addr = gdbstub_pc();
        /* INT calls returning; their results are logged even when stopping for something else. */
        if (int_pending_count && int_ret_hash[INT_RET_HASH(addr)] && gdbstub_int_returned(addr, !stopped))
            return 1;
        if (!stopped) {
            /* Execution entering a watched range. */
            for (int i = 0; i < xrange_count; i++) {
                if ((addr >= xrange_lo[i]) && (addr < xrange_hi[i])) {
                    gdbstub_log("GDB Stub: Execution entered range at %08X\n", addr);
                    xrange_hit   = addr;
                    xrange_count = 0;
                    gdbstub_step = GDBSTUB_BREAK_RANGE;
                    return 1;
                }
            }
            /* An INT about to be called. The instruction a resume starts on isn't
               checked, so resuming at a caught INT runs it. */
            if (catch_count && gdbstub_catch_call(addr))
                return 1;
        }
    }
    if (stopped)
        return 1;

    /* Check hardware breakpoints if any are present. */
    gdbstub_breakpoint_t *breakpoint = first_hwbreak;
    if (breakpoint) {
        /* Calculate the current instruction's address. */
        uint32_t wanted_addr = gdbstub_pc();

        /* Go through the list of software breakpoints. */
        do {
            /* Check if the breakpoint coincides with this address. */
            if (breakpoint->addr == wanted_addr) {
                gdbstub_log("GDB Stub: Hardware breakpoint at %08X\n", wanted_addr);

                /* Flag that we're in a hardware breakpoint. */
                gdbstub_step = GDBSTUB_BREAK_HW;

                /* Pause execution. */
                return 1;
            }

            breakpoint = breakpoint->next;
        } while (breakpoint);
    }

    /* No breakpoint found, continue execution or stop if execution is paused. */
    return gdbstub_step - GDBSTUB_EXEC;
}

int
gdbstub_int3(void)
{
    /* Check software breakpoints if any are present. */
    gdbstub_breakpoint_t *breakpoint = first_swbreak;
    if (breakpoint) {
        /* Calculate the breakpoint instruction's address. */
        uint32_t new_pc = cpu_state.pc - 1;
        if (cpu_state.op32)
            new_pc &= 0xffff;
        uint32_t wanted_addr = cs + new_pc;

        /* Go through the list of software breakpoints. */
        do {
            /* Check if the breakpoint coincides with this address. */
            if (breakpoint->addr == wanted_addr) {
                gdbstub_log("GDB Stub: Software breakpoint at %08X\n", wanted_addr);

                /* Move EIP back to where the break instruction was. */
                cpu_state.pc = new_pc;

                /* Flag that we're in a software breakpoint. */
                gdbstub_step = GDBSTUB_BREAK_SW;

                /* Abort INT 3 execution. */
                return 1;
            }

            breakpoint = breakpoint->next;
        } while (breakpoint);
    }

    /* No breakpoint found, continue INT 3 execution as normal. */
    return 0;
}

void
gdbstub_mem_access(uint32_t *addrs, int access)
{
    /* Stop if we're in the debugger context. */
    if (in_gdbstub)
        return;

    int width = access & (GDBSTUB_MEM_WRITE - 1);
    int i;

    /* Go through the lists of watchpoints for this type of access. */
    gdbstub_breakpoint_t *watchpoint = (access & GDBSTUB_MEM_WRITE) ? first_wwatch : first_rwatch;
    while (1) {
        if (watchpoint) {
            /* Check if any component of this address is within the breakpoint's range. */
            for (i = 0; i < width; i++) {
                if ((addrs[i] >= watchpoint->addr) && (addrs[i] < watchpoint->end)) {
                    watch_addr = addrs[i];
                    break;
                }
            }
            if (i < width) {
                gdbstub_log("GDB Stub: %s watchpoint at %08X\n", (access & GDBSTUB_MEM_AWATCH) ? "Access" : ((access & GDBSTUB_MEM_WRITE) ? "Write" : "Read"), watch_addr);

                /* Flag that we're in a read/write watchpoint. */
                gdbstub_step = (access & GDBSTUB_MEM_AWATCH) ? GDBSTUB_BREAK_AWATCH : ((access & GDBSTUB_MEM_WRITE) ? GDBSTUB_BREAK_WWATCH : GDBSTUB_BREAK_RWATCH);

                /* Stop looking. */
                return;
            }

            watchpoint = watchpoint->next;
        } else {
            /* Jump from list to list as a shortcut. */
            if (access & GDBSTUB_MEM_AWATCH) {
                break;
            } else {
                watchpoint = first_awatch;
                access |= GDBSTUB_MEM_AWATCH;
            }
        }
    }
}

void
gdbstub_frame_blit(int monitor_index, int x, int y, int w, int h)
{
    const bitmap_t *buf = monitors[monitor_index].target_buffer;

    /* Keep every frame, so that a client connecting to a paused machine sees
       what is on screen. Clip to the buffer as the frontends do. */
    if ((monitor_index != 0) || !buf)
        return;
    if (x < 0) {
        w += x;
        x = 0;
    }
    if (y < 0) {
        h += y;
        y = 0;
    }
    if ((x + w) > buf->w)
        w = buf->w - x;
    if ((y + h) > buf->h)
        h = buf->h - y;
    if ((w <= 0) || (h <= 0))
        return;

    if ((w * h) > frame_last_size) {
        free(frame_last);
        frame_last      = (uint32_t *) malloc(w * h * sizeof(uint32_t));
        frame_last_size = frame_last ? (w * h) : 0;
        if (!frame_last) {
            frame_last_w = frame_last_h = 0;
            return;
        }
    }

    for (int row = 0; row < h; row++)
        memcpy(&frame_last[row * w], &buf->line[y + row][x], w * sizeof(uint32_t));
    frame_last_w = w;
    frame_last_h = h;
    frame_last_seq++;
}

void
gdbstub_init(void)
{
#ifdef _WIN32
    WSAStartup(MAKEWORD(2, 2), &wsa);
#endif

    /* Create GDB server socket. */
    if ((gdbstub_socket = socket(AF_INET, SOCK_STREAM, 0)) == -1) {
        pclog("GDB Stub: Failed to create socket\n");
        return;
    }

    int yes = 1;
    if (setsockopt(gdbstub_socket, SOL_SOCKET, SO_REUSEADDR,
#ifdef _WIN32
                   (const char *) &yes,
#else
                   &yes,
#endif
                   sizeof(yes)) == -1) {
        pclog("GDB Stub: setsockopt SO_REUSEADDR failed\n");
        return;
    }

#ifdef _WIN32
    if (setsockopt(gdbstub_socket, SOL_SOCKET, SO_EXCLUSIVEADDRUSE, (const char *) &yes, sizeof(yes)) == -1) {
        pclog("GDB Stub: setsockopt SO_EXCLUSIVEADDRUSE failed\n");
    }
#endif

    /* Bind GDB server socket. */
    int                port      = gdbstub_port;
    struct sockaddr_in bind_addr = {
        .sin_family = AF_INET,
        .sin_addr   = { .s_addr = INADDR_ANY },
        .sin_port   = htons(port)
    };
    if (bind(gdbstub_socket, (struct sockaddr *) &bind_addr, sizeof(bind_addr)) == -1) {
        pclog("GDB Stub: Failed to bind on port %d (%d)\n", port,
#ifdef _WIN32
              WSAGetLastError()
#else
              errno
#endif
        );
        gdbstub_socket = -1;
        return;
    }

    /* Create client list mutex. */
    client_list_mutex = thread_create_mutex();

    /* Clear watchpoint page map. */
    memset(gdbstub_watch_pages, 0, sizeof(gdbstub_watch_pages));

    /* Start server thread. */
    pclog("GDB Stub: Listening on port %d\n", port);
    thread_create(gdbstub_server_thread, NULL);

    /* Start the CPU paused. */
    gdbstub_step = GDBSTUB_BREAK;
}

void
gdbstub_close(void)
{
    /* Stop if the GDB server hasn't initialized. */
    if (gdbstub_socket < 0)
        return;

    /* Close GDB server socket. */
    close(gdbstub_socket);

    /* Clear client list. */
    thread_wait_mutex(client_list_mutex);
    gdbstub_client_t *client = first_client;
    int               socket;
    while (client) {
        socket         = client->socket;
        if (client->waiting_stop) {
            FAST_RESPONSE("W00");
            gdbstub_client_respond(client);
        }
        client->socket = -1;
        close(socket);
        client = client->next;
    }
    thread_release_mutex(client_list_mutex);
    thread_close_mutex(client_list_mutex);
}
