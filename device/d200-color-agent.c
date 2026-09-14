#define _DARWIN_C_SOURCE 1
#define _POSIX_C_SOURCE 200809L
#include <dlfcn.h>
#include <fcntl.h>
#include <errno.h>
#include <signal.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <turbojpeg.h>
#include <unistd.h>
#include "d200_video_stream.h"
#include "d200_color_decode.h"

#define WIDTH 540
#define HEIGHT 960

typedef int32_t mi_s32;
typedef uint32_t mi_u32;
typedef uint16_t mi_u16;
typedef mi_s32 mi_sys_buf_handle;
typedef uint8_t mi_bool;
typedef struct { mi_u32 module, device, channel, port; } channel_port;
typedef struct divp_context {
    void *sys, *divp, *disp;
    mi_s32 (*sys_init)(void);
    mi_s32 (*sys_exit)(void);
    mi_s32 (*disp_init)(const void *);
    mi_s32 (*disp_deinit)(void);
    mi_s32 (*create)(mi_u32, const void *);
    mi_s32 (*set_output)(mi_u32, const void *);
    mi_s32 (*start)(mi_u32);
    mi_s32 (*stop)(mi_u32);
    mi_s32 (*destroy)(mi_u32);
    mi_s32 (*deinit)(void);
    mi_s32 (*bind)(channel_port *, channel_port *, mi_u32, mi_u32);
    mi_s32 (*unbind)(struct divp_context *);
    mi_s32 (*get_buf)(channel_port *, void *, void *, mi_sys_buf_handle *, mi_s32);
    mi_s32 (*put_buf)(mi_sys_buf_handle, void *, mi_bool);
    int channel, sys_initialized, disp_initialized, created, started, bound, cleanup_failed;
    mi_sys_buf_handle held;
    int held_acquired;
    unsigned char info[272];
} divp_context;

static volatile sig_atomic_t running = 1;
static volatile sig_atomic_t signal_reason = D200_VS_RESULT_CANCELLED;
static channel_port divp_port = {12, 0, 0, 0};
static channel_port disp_port = {15, 0, 0, 0};
static void put16(unsigned char *p, uint16_t v) { p[0] = v; p[1] = v >> 8; }
static void put32(unsigned char *p, uint32_t v) { p[0] = v; p[1] = v >> 8; p[2] = v >> 16; p[3] = v >> 24; }
static uint16_t get16(const unsigned char *p) { return (uint16_t)p[0] | (uint16_t)p[1] << 8; }
static uint32_t get32(const unsigned char *p) { return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24; }
static uint64_t monotonic_ns(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return (uint64_t)t.tv_sec * 1000000000ULL + t.tv_nsec; }
static void stop_signal(int signo) { signal_reason = signo == SIGUSR1 ? D200_VS_SOURCE_FAILURE : D200_VS_RESULT_CANCELLED; running = 0; }

/* UNBIND_REQUEST_BEGIN: extracted separately with injected open/ioctl/close. */
typedef struct {
    channel_port source, destination;
    uint8_t reserved[16];
} unbind_payload;
typedef struct {
    uint32_t size, reserved;
    uint64_t pointer;
} unbind_request;
_Static_assert(sizeof(channel_port) == 16, "MI channel port size");
_Static_assert(offsetof(channel_port, module) == 0 && offsetof(channel_port, device) == 4 &&
               offsetof(channel_port, channel) == 8 && offsetof(channel_port, port) == 12,
               "MI channel port offsets");
_Static_assert(sizeof(unbind_payload) == 48, "MI unbind payload size");
_Static_assert(offsetof(unbind_payload, source) == 0 && offsetof(unbind_payload, destination) == 16 &&
               offsetof(unbind_payload, reserved) == 32,
               "MI unbind payload offsets");
_Static_assert(sizeof(unbind_request) == 16, "MI ioctl request size");
_Static_assert(offsetof(unbind_request, size) == 0 && offsetof(unbind_request, reserved) == 4 &&
               offsetof(unbind_request, pointer) == 8, "MI ioctl request offsets");
static int initialized_unbind(divp_context *d) {
    (void)d;
    /* MI_SYS_IOCTL_UnBindChnPort consumes only the two 16-byte ports.
     * Keep the ignored tail initialized; it cannot restore missing endpoints. */
    unbind_payload payload = {
        .source = divp_port, .destination = disp_port, .reserved = {0}
    };
    unbind_request request = {
        .size = sizeof(payload), .reserved = 0, .pointer = (uint64_t)(uintptr_t)&payload
    };
    int fd = open("/dev/mi_sys", O_RDWR | O_CLOEXEC);
    if (fd < 0) return -1;
    int rc = ioctl(fd, 0x40306904UL, &request);
    int saved_errno = errno;
    if (close(fd) && !rc) return -1;
    if (rc) errno = saved_errno;
    return rc;
}
/* UNBIND_REQUEST_END */

/* DIVP_OPEN_BEGIN: extracted with injected loader and vendor callbacks. */
/* Returned startup failures only. Stage strings are compile-time literals,
 * never loader text, credentials or payloads. Cleanup cannot replace this
 * record. A call that never returns produces no invented return value. */
enum color_startup_domain {
    COLOR_STARTUP_NONE, COLOR_STARTUP_LOADER, COLOR_STARTUP_VENDOR,
    COLOR_STARTUP_RESOURCE, COLOR_STARTUP_INTERNAL
};
struct color_startup {
    const char *stage;
    enum color_startup_domain domain;
    int32_t vendor_return;
    int error, ready, emitted;
    uint64_t observed_ns;
};
#define COLOR_SUMMARY_CAPACITY 1536u
static uint64_t monotonic_ns(void);
static void color_startup_fail(struct color_startup *startup, const char *stage,
                               enum color_startup_domain domain, int32_t vendor_return, int error) {
    if (startup->stage) return;
    startup->stage = stage;
    startup->domain = domain;
    startup->vendor_return = vendor_return;
    startup->error = error;
    startup->observed_ns = monotonic_ns();
}
static int color_startup_vendor(struct color_startup *startup, const char *stage, mi_s32 result) {
    if (!result) return 0;
    /* Vendor wrappers may overwrite errno while logging. It is not a
     * trustworthy syscall error at this boundary. Preserve only raw MI rc. */
    color_startup_fail(startup, stage, COLOR_STARTUP_VENDOR, result, 0);
    return -1;
}
static int divp_open(divp_context *d, uint32_t fps_n, uint32_t fps_d, int channel,
                     struct color_startup *startup) {
    unsigned char channel_attributes[32] = {0};
    unsigned char output_attributes[16] = {0};
    memset(d, 0, sizeof(*d));
    d->unbind = initialized_unbind;
    d->channel = channel;
    divp_port.channel = (mi_u32)channel;
    d->sys = dlopen("/lib/libmi_sys.so", RTLD_NOW | RTLD_GLOBAL);
    if (!d->sys) color_startup_fail(startup, "loader.sys", COLOR_STARTUP_LOADER, 0, 0);
    d->divp = dlopen("/lib/libmi_divp.so", RTLD_NOW | RTLD_GLOBAL);
    if (!d->divp) color_startup_fail(startup, "loader.divp", COLOR_STARTUP_LOADER, 0, 0);
    if (startup->stage) goto failed;
    /* Preserve the complete resolution batch even on a missing symbol: its
     * acquired cleanup callbacks are part of the existing lifetime contract. */
#define LOAD(where, member, name) do { \
    d->member = dlsym(d->where, name); \
    if (!d->member) color_startup_fail(startup, "symbol." name, COLOR_STARTUP_LOADER, 0, 0); \
} while (0)
    LOAD(sys, sys_init, "MI_SYS_Init"); LOAD(sys, sys_exit, "MI_SYS_Exit");
    LOAD(divp, create, "MI_DIVP_CreateChn"); LOAD(divp, set_output, "MI_DIVP_SetOutputPortAttr");
    LOAD(divp, start, "MI_DIVP_StartChn"); LOAD(divp, stop, "MI_DIVP_StopChn"); LOAD(divp, destroy, "MI_DIVP_DestroyChn");
    LOAD(divp, deinit, "MI_DIVP_DeInitDev");
    LOAD(sys, bind, "MI_SYS_BindChnPort");
    LOAD(sys, get_buf, "MI_SYS_ChnInputPortGetBuf"); LOAD(sys, put_buf, "MI_SYS_ChnInputPortPutBuf");
    if (startup->stage) goto failed;
    if (color_startup_vendor(startup, "vendor.MI_SYS_Init", d->sys_init())) goto failed;
    d->sys_initialized = 1;
    d->disp = dlopen("/lib/libmi_disp.so", RTLD_NOW | RTLD_GLOBAL);
    if (!d->disp) {
        color_startup_fail(startup, "loader.disp", COLOR_STARTUP_LOADER, 0, 0);
        goto failed;
    }
    LOAD(disp, disp_init, "MI_DISP_InitDev");
    LOAD(disp, disp_deinit, "MI_DISP_DeInitDev");
#undef LOAD
    if (startup->stage) goto failed;
    if (color_startup_vendor(startup, "vendor.MI_DISP_InitDev", d->disp_init(NULL))) goto failed;
    d->disp_initialized = 1;
    if (color_startup_vendor(startup, "vendor.MI_DIVP_CreateChn",
                              d->create((mi_u32)channel, channel_attributes))) goto failed;
    d->created = 1;
    put32(output_attributes, WIDTH); put32(output_attributes + 4, HEIGHT);
    if (color_startup_vendor(startup, "vendor.MI_DIVP_SetOutputPortAttr",
                              d->set_output((mi_u32)channel, output_attributes))) goto failed;
    if (color_startup_vendor(startup, "vendor.MI_DIVP_StartChn", d->start((mi_u32)channel))) goto failed;
    d->started = 1;
    mi_u32 fps = (mi_u32)(((uint64_t)fps_n + fps_d / 2) / fps_d);
    if (!fps) fps = 1;
    if (color_startup_vendor(startup, "vendor.MI_SYS_BindChnPort",
                              d->bind(&divp_port, &disp_port, fps, fps))) goto failed;
    d->bound = 1;
    return 0;
failed:
    return -1;
}
/* DIVP_OPEN_END */

static uint8_t clamp8(int value) { return value < 0 ? 0 : value > 255 ? 255 : (uint8_t)value; }
static void initialize_range_tables(uint8_t y_table[256], uint8_t c_table[256]) {
    for (int value = 0; value < 256; value++) {
        int centered = value - 128;
        int rounding = centered >= 0 ? 127 : -127;
        y_table[value] = (uint8_t)(16 + (value * 219 + 127) / 255);
        c_table[value] = clamp8(128 + (centered * 224 + rounding) / 255);
    }
}
static int present_planes(divp_context *d, const uint8_t *y_plane,
                          const uint8_t *u_plane, const uint8_t *v_plane) {
    unsigned char config[48] = {0};
    put32(config, 1); put32(config + 0x04, 0x90000000U);
    put16(config + 0x10, WIDTH); put16(config + 0x12, HEIGHT);
    put32(config + 0x18, 11);
    mi_s32 get_rc = -1;
    for (int attempt = 0; attempt < 4 && running; attempt++) {
        memset(d->info, 0, sizeof(d->info)); d->held = -1;
        get_rc = d->get_buf(&divp_port, config, d->info, &d->held, 500);
        if (!get_rc) {
            d->held_acquired = 1;
            break;
        }
    }
    if (get_rc || !d->held_acquired) return -2;
    uint32_t y_stride = get32(d->info + 0x60), uv_stride = get32(d->info + 0x64);
    uint8_t *y_destination = (uint8_t *)(uintptr_t)get32(d->info + 0x3c);
    uint8_t *uv_destination = (uint8_t *)(uintptr_t)get32(d->info + 0x40);
    if (get32(d->info + 0x10) != 1 || get32(d->info + 0x34) != 2 ||
        get16(d->info + 0x38) != WIDTH || get16(d->info + 0x3a) != HEIGHT ||
        !y_destination || !uv_destination || y_stride < WIDTH || uv_stride < WIDTH ||
        get32(d->info + 0x6c) < (uint64_t)y_stride * HEIGHT + (uint64_t)uv_stride * (HEIGHT / 2)) {
        if (d->put_buf(d->held, d->info, 1)) d->cleanup_failed = 1;
        d->held_acquired = 0; return -3;
    }
    static uint8_t y_table[256], c_table[256];
    static int tables_initialized;
    if (!tables_initialized) { initialize_range_tables(y_table, c_table); tables_initialized = 1; }
    for (int row = 0; row < HEIGHT; row++) {
        uint8_t *destination = y_destination + (size_t)row * y_stride;
        const uint8_t *source = y_plane + (size_t)row * WIDTH;
        for (int column = 0; column < WIDTH; column++) destination[column] = y_table[source[column]];
    }
    for (int row = 0; row < HEIGHT / 2; row++) {
        uint8_t *destination = uv_destination + (size_t)row * uv_stride;
        const uint8_t *u = u_plane + (size_t)row * (WIDTH / 2);
        const uint8_t *v = v_plane + (size_t)row * (WIDTH / 2);
        for (int column = 0; column < WIDTH / 2; column++) {
            destination[column * 2] = c_table[u[column]];
            destination[column * 2 + 1] = c_table[v[column]];
        }
    }
    mi_s32 rc = d->put_buf(d->held, d->info, 0);
    d->held_acquired = 0;
    if (rc) d->cleanup_failed = 1;
    return rc ? -5 : 0;
}

/* STREAM_CORE_BEGIN: production core extracted by the host-only fixture. */
static int divp_cleanup(divp_context *d) {
    int failed = d->cleanup_failed;
    if (d->held_acquired) {
        if (!d->put_buf || d->put_buf(d->held, d->info, 1)) failed = 1;
        d->held_acquired = 0;
    }
    if (d->bound) { if (!d->unbind || d->unbind(d)) failed = 1; d->bound = 0; }
    if (d->started) { if (!d->stop || d->stop((mi_u32)d->channel)) failed = 1; d->started = 0; }
    if (d->created) { if (!d->destroy || d->destroy((mi_u32)d->channel)) failed = 1; d->created = 0; }
    if (d->deinit && d->deinit()) failed = 1;
    /* Retain our display reference through all bound-video teardown. */
    if (d->disp_initialized) { if (!d->disp_deinit || d->disp_deinit()) failed = 1; d->disp_initialized = 0; }
    if (d->sys_initialized) { if (!d->sys_exit || d->sys_exit()) failed = 1; d->sys_initialized = 0; }
    if (d->divp && dlclose(d->divp)) failed = 1;
    if (d->disp && dlclose(d->disp)) failed = 1;
    if (d->sys && dlclose(d->sys)) failed = 1;
    d->divp = d->disp = d->sys = NULL; d->deinit = NULL;
    d->cleanup_failed = failed;
    return failed ? -1 : 0;
}

struct color_stream {
    d200_vs_state wire;
    struct color_startup startup;
    uint8_t *slots[2];
    uint32_t sizes[2];
    uint8_t input[D200_VS_HEADER_SIZE + D200_VS_MAX_PAYLOAD];
    uint8_t output[64];
    size_t have, need, out_size, out_sent;
    uint64_t record_started, deadline, phase, period, remainder, progress, eos_started;
    int pacing, started;
    uint64_t ready_ns, first_submission_ns, submissions, jpeg_bytes;
    uint64_t queue_highwater, late_submissions, presentation_ns, presentation_max_ns;
    uint64_t presentation_attempts, cleanup_ns;
    int ready_observed, submission_observed, cleanup_observed, metrics_saturated;
};

static void color_counter(struct color_stream *s, uint64_t *counter, uint64_t amount)
{
    if (UINT64_MAX - *counter < amount) { *counter = UINT64_MAX; s->metrics_saturated = 1; }
    else *counter += amount;
}

static void color_cleanup_finished(struct color_stream *s, uint64_t started)
{
    s->cleanup_ns = monotonic_ns() - started;
    s->cleanup_observed = 1;
}

static int color_emit(struct color_stream *s, uint8_t kind, const uint8_t *p, uint32_t n)
{
    d200_vs_header h = {0};
    if (s->out_size) return -1;
    h.kind = kind; h.payload_length = n; h.epoch = 1;
    memcpy(h.session, s->wire.session, 16); h.sequence = s->wire.sequence[1];
    if (!d200_vs_encode_record(s->output, sizeof(s->output), &h, p) ||
        !d200_vs_state_accept(&s->wire, &h, p, D200_VS_CONSUMER)) return -1;
    s->out_size = 40 + n; s->out_sent = 0;
    return 0;
}

static int color_ready(struct color_stream *s, const uint8_t session[16], uint32_t n, uint32_t d)
{
    uint8_t p[24];
    d200_vs_state_init(&s->wire, session, n, d, NULL);
    s->need = 40; s->period = UINT64_C(1000000000) * d / n;
    s->remainder = UINT64_C(1000000000) * d % n;
    s->progress = monotonic_ns();
    d200_vs_put_u32(p, n); d200_vs_put_u32(p + 4, d);
    d200_vs_put_u32(p + 8, 2); d200_vs_put_u32(p + 12, D200_VS_MAX_JPEG);
    d200_vs_put_u32(p + 16, D200_VS_WINDOW_BYTES); d200_vs_put_u32(p + 20, D200_VS_MAX_PAYLOAD);
    int result = color_emit(s, D200_VS_READY, p, sizeof(p));
    if (!result) {
        s->ready_ns = monotonic_ns(); s->ready_observed = 1;
        s->startup.ready = 1;
    } else color_startup_fail(&s->startup, "ready", COLOR_STARTUP_INTERNAL, 0, 0);
    return result;
}

/* One nonblocking receive, bounded by the current record, per invocation.
 * Receive stays enabled during pacing, even with both reservations occupied. */
static int color_receive(struct color_stream *s, int fd)
{
    d200_vs_header h;
    ssize_t n = recv(fd, s->input + s->have, s->need - s->have, MSG_DONTWAIT);
    if (!n) return D200_VS_DISCONNECTED;
    if (n < 0) return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR ? 0 : D200_VS_DISCONNECTED;
    if (!s->have) s->record_started = monotonic_ns();
    s->have += (size_t)n;
    if (s->have != s->need) return 0;
    if (s->need == 40) {
        if (!d200_vs_decode_header(s->input, 40, &h)) return D200_VS_PROTOCOL;
        s->need += h.payload_length;
        return 0;
    }
    if (!d200_vs_decode_record(s->input, s->need, D200_VS_PRODUCER, &h) ||
        !d200_vs_state_accept(&s->wire, &h, s->input + 40, D200_VS_PRODUCER)) return D200_VS_PROTOCOL;
    const uint8_t *p = s->input + 40;
    if (h.kind == D200_VS_FRAME) {
        unsigned slot = (unsigned)(d200_vs_get_u64(p) & 1);
        memcpy(s->slots[slot] + d200_vs_get_u32(p + 12), p + 16, h.payload_length - 16);
        s->sizes[slot] = d200_vs_get_u32(p + 8);
        color_counter(s, &s->jpeg_bytes, h.payload_length - 16);
        uint64_t queued = s->wire.received - s->wire.consumed + (s->wire.partial_total ? 1 : 0);
        if (queued > s->queue_highwater) s->queue_highwater = queued;
    } else if (h.kind == D200_VS_CANCEL || h.kind == D200_VS_ERROR) {
        return d200_vs_get_u32(p);
    } else if (h.kind == D200_VS_EOS) {
        if (!s->wire.eos) return D200_VS_EMPTY_SOURCE;
        s->eos_started = monotonic_ns();
    }
    s->have = 0; s->need = 40; s->record_started = 0; s->progress = monotonic_ns();
    return 0;
}

static int color_flush(struct color_stream *s, int fd)
{
    if (!s->out_size) return 0;
    ssize_t n = send(fd, s->output + s->out_sent, s->out_size - s->out_sent, MSG_DONTWAIT | MSG_NOSIGNAL);
    if (n < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) return 0;
    if (n <= 0) return -1;
    s->out_sent += (size_t)n;
    if (s->out_sent == s->out_size) { s->out_size = s->out_sent = 0; s->progress = monotonic_ns(); }
    return 0;
}

/* Presenter returns 0 after submission, 1 while decode is pending, or a
 * negative error. Pending work changes neither pacing nor wire credits. */
static int color_step(struct color_stream *s, int (*present)(void *, const uint8_t *, size_t), void *context)
{
    uint64_t now = monotonic_ns();
    if (s->pacing && now >= s->deadline && !s->out_size) {
        uint8_t p[8];
        d200_vs_put_u64(p, s->wire.consumed);
        if (color_emit(s, D200_VS_CONSUMED, p, sizeof(p))) return D200_VS_PROTOCOL;
        s->pacing = 0; s->progress = now;
    }
    if (!s->pacing && !s->out_size && s->wire.consumed < s->wire.received) {
        unsigned slot = (unsigned)(s->wire.consumed & 1);
        uint64_t presentation_started = monotonic_ns();
        int result = present(context, s->slots[slot], s->sizes[slot]);
        /* Pending decode is not a presentation attempt and grants no credit. */
        if (result == 1) return 0;
        if (!s->started) { s->deadline = now; s->started = 1; }
        uint64_t finished = monotonic_ns(), duration = finished - presentation_started;
        color_counter(s, &s->presentation_attempts, 1);
        color_counter(s, &s->presentation_ns, duration);
        if (duration > s->presentation_max_ns) s->presentation_max_ns = duration;
        if (result) return D200_VS_PRESENTATION;
        if (!s->submission_observed) { s->first_submission_ns = finished; s->submission_observed = 1; }
        color_counter(s, &s->submissions, 1);
        if (presentation_started > s->deadline) color_counter(s, &s->late_submissions, 1);
        if (UINT64_MAX - s->deadline <= s->period) return D200_VS_RESOURCE_LIMIT;
        s->deadline += s->period;
        s->phase += s->remainder;
        if (s->phase >= s->wire.fps_n) { s->deadline++; s->phase -= s->wire.fps_n; }
        s->pacing = 1;
    }
    return 0;
}

static int color_poll_timeout(const struct color_stream *s)
{
    if (!s->pacing) return 10;
    uint64_t now = monotonic_ns();
    if (now >= s->deadline) return 0;
    uint64_t remaining = s->deadline - now;
    if (remaining >= UINT64_C(10000000)) return 10;
    return (int)((remaining + UINT64_C(999999)) / UINT64_C(1000000));
}

static int color_timeout(const struct color_stream *s)
{
    uint64_t now = monotonic_ns();
    uint64_t progress = s->period > UINT64_C(10000000000) ? s->period * 3 : UINT64_C(30000000000);
    uint64_t drain = s->period * 2 + UINT64_C(5000000000);
    if (drain < UINT64_C(10000000000)) drain = UINT64_C(10000000000);
    return (s->have && now - s->record_started >= UINT64_C(30000000000)) ||
        (s->wire.has_eos && now - s->eos_started >= drain) || now - s->progress >= progress;
}

static int color_terminal(struct color_stream *s, uint32_t reason, int cleanup_failed)
{
    uint8_t payload[20] = {0};
    d200_vs_header h = {0};
    if (s->out_size) return -1;
    if (cleanup_failed) reason = D200_VS_CLEANUP_FAILED;
    if (!reason && (!s->wire.has_eos || !s->wire.eos || s->wire.eos != s->wire.consumed || s->pacing)) return -1;
    h.kind = reason ? (d200_vs_reason(reason) ? D200_VS_CANCELLED : D200_VS_ERROR) : D200_VS_DONE;
    h.payload_length = reason ? 4 : 20; h.epoch = 1;
    h.sequence = s->wire.sequence[1]; memcpy(h.session, s->wire.session, 16);
    if (reason) d200_vs_put_u32(payload, reason);
    else { d200_vs_put_u64(payload, s->wire.eos); d200_vs_put_u64(payload + 8, s->wire.consumed); }
    if (!d200_vs_encode_record(s->output, sizeof(s->output), &h, payload)) return -1;
    s->out_size = 40 + h.payload_length; s->out_sent = 0;
    return 0;
}
/* One immediate ordered ERROR or an explicit disconnect before vendor cleanup.
 * Shutdown starts the proxy watchdog; it never establishes cleanup proof. */
static int color_failure_notice(struct color_stream *s, int fd, uint32_t reason)
{
    if (!s->out_size && reason && !d200_vs_reason(reason) &&
        !color_terminal(s, reason, 0) && !color_flush(s, fd) && !s->out_size)
        return 1;
    (void)shutdown(fd, SHUT_RDWR);
    return -1;
}
static const char *color_optional_ns(char out[32], int observed, uint64_t value)
{
    if (!observed) return "null";
    (void)snprintf(out, 32, "%llu", (unsigned long long)value);
    return out;
}

/* Fixed vocabulary and numeric fields only; raw loader error strings are
 * intentionally excluded. Vendor numbers are null for every nonvendor failure.
 * 'ready' means READY queued locally, never received or displayed. */
static int color_startup_json(const struct color_startup *startup, char *out, size_t capacity)
{
    const char *domain = "null";
    char stage[96], vendor[32], vendor_u32[32], error[32], observed[32];
    const char *stage_json = "null", *vendor_json = "null", *unsigned_json = "null", *errno_json = "null";
    if (startup->stage) {
        int n = snprintf(stage, sizeof(stage), "\"%s\"", startup->stage);
        if (n < 0 || (size_t)n >= sizeof(stage)) return -1;
        stage_json = stage;
        switch (startup->domain) {
        case COLOR_STARTUP_LOADER: domain = "\"loader\""; break;
        case COLOR_STARTUP_VENDOR: domain = "\"vendor\""; break;
        case COLOR_STARTUP_RESOURCE: domain = "\"resource\""; break;
        case COLOR_STARTUP_INTERNAL: domain = "\"internal\""; break;
        default: return -1;
        }
        if (startup->domain == COLOR_STARTUP_VENDOR) {
            (void)snprintf(vendor, sizeof(vendor), "%ld", (long)startup->vendor_return);
            (void)snprintf(vendor_u32, sizeof(vendor_u32), "%lu", (unsigned long)(uint32_t)startup->vendor_return);
            vendor_json = vendor; unsigned_json = vendor_u32;
        }
        if (startup->error) {
            (void)snprintf(error, sizeof(error), "%d", startup->error);
            errno_json = error;
        }
    }
    return snprintf(out, capacity,
        "{\"outcome\":\"%s\",\"stage\":%s,\"domain\":%s,\"vendorReturn\":%s,"
        "\"vendorReturnU32\":%s,\"errno\":%s,\"observedNs\":%s}",
        startup->stage ? "failed" : startup->ready ? "ready" : "not-ready",
        stage_json, domain, vendor_json, unsigned_json, errno_json,
        color_optional_ns(observed, startup->stage != NULL, startup->observed_ns));
}

/* Fixed-size terminal diagnostic only; native submission is not pixel proof.
 * Milestones absent because startup/presentation never reached them are null. */
static int color_summary(const struct color_stream *s, char *out, size_t capacity, uint32_t reason)
{
    char session[33], ready[32], first[32], duration[32], maximum[32], cleanup[32];
    char startup[384];
    int startup_size = color_startup_json(&s->startup, startup, sizeof(startup));
    if (startup_size < 0 || (size_t)startup_size >= sizeof(startup)) return -1;
    for (unsigned i = 0; i < 16; i++) (void)snprintf(session + i * 2, 3, "%02x", s->wire.session[i]);
    return snprintf(out, capacity,
        "{\"event\":\"native-video-terminal\",\"session\":\"%s\",\"epoch\":1,\"clock\":\"device-monotonic\","
        "\"readyNs\":%s,\"firstSubmissionNs\":%s,\"framesReceived\":%llu,\"framesConsumed\":%llu,"
        "\"successfulSubmissions\":%llu,\"jpegBytesReceived\":%llu,\"queueHighwaterFrames\":%llu,"
        "\"lateSubmissions\":%llu,\"presentationAttempts\":%llu,\"presentationTotalNs\":%s,"
        "\"presentationMaxNs\":%s,\"cleanupDurationNs\":%s,\"terminalCode\":%u,"
        "\"countersSaturated\":%s,\"pixelProof\":false,\"startup\":%s}\n",
        session, color_optional_ns(ready, s->ready_observed, s->ready_ns),
        color_optional_ns(first, s->submission_observed, s->first_submission_ns),
        (unsigned long long)s->wire.received, (unsigned long long)s->wire.consumed,
        (unsigned long long)s->submissions, (unsigned long long)s->jpeg_bytes,
        (unsigned long long)s->queue_highwater, (unsigned long long)s->late_submissions,
        (unsigned long long)s->presentation_attempts,
        color_optional_ns(duration, s->presentation_attempts != 0, s->presentation_ns),
        color_optional_ns(maximum, s->presentation_attempts != 0, s->presentation_max_ns),
        color_optional_ns(cleanup, s->cleanup_observed, s->cleanup_ns), reason,
        s->metrics_saturated ? "true" : "false", startup);
}
/* Diagnostics cannot delay owned-child exit when its stderr pipe is full.
 * Temporarily use nonblocking mode on the existing diagnostic description;
 * an unavailable sink loses this optional record, never cleanup evidence. */
static int color_write_summary(int fd, const char *data, size_t length)
{
    if (!length || length >= COLOR_SUMMARY_CAPACITY) return -1;
    int flags = fcntl(fd, F_GETFL);
    if (flags < 0 || (! (flags & O_NONBLOCK) && fcntl(fd, F_SETFL, flags | O_NONBLOCK))) return -1;
    ssize_t written = write(fd, data, length);
    if (!(flags & O_NONBLOCK)) (void)fcntl(fd, F_SETFL, flags);
    return written == (ssize_t)length ? 0 : -1;
}

/* Stack-only path also works before color_stream allocation. One attempted
 * write per failure, even if stderr is full or cleanup subsequently wedges.
 * Public wire session/epoch are correlation, not capability/owner authority. */
static int color_startup_failure(struct color_startup *startup, const uint8_t session[16],
                                  int fd, uint32_t reason)
{
    if (!startup->stage || startup->emitted) return 0;
    startup->emitted = 1;
    char public_session[33], first[384], summary[640];
    for (unsigned i = 0; i < 16; i++) (void)snprintf(public_session + i * 2, 3, "%02x", session[i]);
    int n = color_startup_json(startup, first, sizeof(first));
    if (n < 0 || (size_t)n >= sizeof(first)) return -1;
    n = snprintf(summary, sizeof(summary),
        "{\"event\":\"native-startup-failure\",\"session\":\"%s\",\"epoch\":1,"
        "\"clock\":\"device-monotonic\",\"terminalCode\":%u,\"pixelProof\":false,\"startup\":%s}\n",
        public_session, reason, first);
    if (n < 0 || (size_t)n >= sizeof(summary)) return -1;
    return color_write_summary(fd, summary, (size_t)n);
}
/* STREAM_CORE_END */

struct presenter {
    divp_context display;
    tjhandle decoder;
    struct d200_decode_queue queue;
    uint8_t *planes[2];
    uint64_t submitted, released, current;
};
static int color_decode(void *context, unsigned slot, const uint8_t *jpeg, size_t size) {
    struct presenter *p = context;
    if (tj3DecompressHeader(p->decoder, jpeg, size) ||
        tj3Get(p->decoder, TJPARAM_JPEGWIDTH) != WIDTH ||
        tj3Get(p->decoder, TJPARAM_JPEGHEIGHT) != HEIGHT ||
        tj3Get(p->decoder, TJPARAM_SUBSAMP) != TJSAMP_420) return -1;
    uint8_t *y = p->planes[slot];
    unsigned char *planes[3] = {y, y + WIDTH * HEIGHT, y + WIDTH * HEIGHT * 5 / 4};
    int strides[3] = {WIDTH, WIDTH / 2, WIDTH / 2};
    return tj3DecompressToYUVPlanes8(p->decoder, jpeg, size, planes, strides) ? -1 : 0;
}
static int color_present(void *context, const uint8_t *jpeg, size_t size) {
    struct presenter *p = context;
    (void)jpeg; (void)size;
    int result = 0;
    int status = d200_decode_status(&p->queue, p->current, &result);
    if (status == D200_DECODE_PENDING) return 1;
    if (status != D200_DECODE_DONE || result) return -1;
    const uint8_t *y = p->planes[p->current & 1];
    return present_planes(&p->display, y, y + WIDTH * HEIGHT, y + WIDTH * HEIGHT * 5 / 4);
}
static int worker(const uint8_t session[16], uint32_t fps_n, uint32_t fps_d, int fd) {
    struct color_startup startup = {0};
    struct color_stream *s = calloc(1, sizeof(*s));
    if (!s) color_startup_fail(&startup, "allocation.stream", COLOR_STARTUP_RESOURCE, 0, 0);
    struct presenter *p = calloc(1, sizeof(*p));
    if (!p) color_startup_fail(&startup, "allocation.presenter", COLOR_STARTUP_RESOURCE, 0, 0);
    uint32_t reason = D200_VS_START_FAILED;
    int cleanup_failed = 0, sent = 0, terminal_queued = 0, notice_failed = 0;
    if (startup.stage) {
        (void)color_startup_failure(&startup, session, STDERR_FILENO, D200_VS_RESOURCE_LIMIT);
        free(s); free(p); return D200_VS_RESOURCE_LIMIT;
    }
    d200_vs_state_init(&s->wire, session, fps_n, fps_d, NULL);
    p->planes[0] = malloc((size_t)WIDTH * HEIGHT * 3 / 2);
    if (!p->planes[0]) color_startup_fail(&s->startup, "allocation.planes.0", COLOR_STARTUP_RESOURCE, 0, 0);
    p->planes[1] = malloc((size_t)WIDTH * HEIGHT * 3 / 2);
    if (!p->planes[1]) color_startup_fail(&s->startup, "allocation.planes.1", COLOR_STARTUP_RESOURCE, 0, 0);
    s->slots[0] = malloc(D200_VS_MAX_JPEG);
    if (!s->slots[0]) color_startup_fail(&s->startup, "allocation.jpeg.0", COLOR_STARTUP_RESOURCE, 0, 0);
    s->slots[1] = malloc(D200_VS_MAX_JPEG);
    if (!s->slots[1]) color_startup_fail(&s->startup, "allocation.jpeg.1", COLOR_STARTUP_RESOURCE, 0, 0);
    p->decoder = tj3Init(TJINIT_DECOMPRESS);
    if (!p->decoder) color_startup_fail(&s->startup, "decoder.init", COLOR_STARTUP_RESOURCE, 0, 0);
    /* Complete the existing allocation batch before its gate. Earlier NULL
     * results must not change which resources are subsequently cleaned up. */
    if (s->startup.stage) goto done;
    if (divp_open(&p->display, fps_n, fps_d, 0, &s->startup)) goto done;
    if (d200_decode_init(&p->queue, color_decode, p)) {
        /* Queue init also uses pthread return codes and performs rollback;
         * do not mislabel its synthesized errno as a direct syscall error. */
        color_startup_fail(&s->startup, "queue.init", COLOR_STARTUP_RESOURCE, 0, 0);
        goto done;
    }
    if (color_ready(s, session, fps_n, fps_d)) goto done;
    reason = 0;
    while (running) {
        while (p->released < s->wire.consumed) {
            if (d200_decode_release(&p->queue, p->released)) { reason = D200_VS_PRESENTATION; goto done; }
            p->released++;
        }
        if (color_flush(s, fd)) { reason = D200_VS_DISCONNECTED; break; }
        reason = color_receive(s, fd);
        if (reason) break;
        while (p->submitted < s->wire.received) {
            unsigned slot = (unsigned)(p->submitted & 1);
            if (d200_decode_submit(&p->queue, p->submitted, s->slots[slot], s->sizes[slot])) {
                reason = D200_VS_PRESENTATION; goto done;
            }
            p->submitted++;
        }
        if (d200_decode_drain_notifications(&p->queue)) { reason = D200_VS_PRESENTATION; break; }
        p->current = s->wire.consumed;
        reason = color_step(s, color_present, p);
        if (reason) break;
        if (s->wire.has_eos && s->wire.consumed == s->wire.eos && !s->out_size && !s->have) break;
        if (color_timeout(s)) {
            reason = D200_VS_TIMEOUT; break;
        }
        struct pollfd pollfds[2] = {
            {fd, POLLIN | (s->out_size ? POLLOUT : 0), 0},
            {d200_decode_notify_fd(&p->queue), POLLIN, 0}
        };
        if (poll(pollfds, 2, color_poll_timeout(s)) < 0 && errno != EINTR) { reason = D200_VS_DISCONNECTED; break; }
    }
    /* The loop's own cause wins. An out-of-band signal addresses the process, not
     * its diagnosis: replacing an observed failure with the signal's reason would
     * report a transport disconnect as a clean cancel and skip the owned ERROR
     * record below. The signal supplies a reason only while none was reached. */
    if (!running && !reason) reason = (uint32_t)signal_reason;
done:
    (void)color_startup_failure(&s->startup, session, STDERR_FILENO, reason);
    /* An owned failure record lets the proxy start its absolute cleanup/reap
     * budget even if a subsequent vendor cleanup call wedges. ERROR is never
     * exposed as cleanup proof until this process has exited successfully. */
    if (reason && !d200_vs_reason(reason)) {
        int notice = color_failure_notice(s, fd, reason);
        terminal_queued = notice == 1;
        notice_failed = notice < 0;
    }
    uint64_t cleanup_started = monotonic_ns();
    int decode_cleanup_failed = d200_decode_destroy(&p->queue) != 0;
    cleanup_failed = divp_cleanup(&p->display) != 0 || decode_cleanup_failed;
    /* A failed join must never leave the callback referencing freed storage.
     * The worker process exits with cleanup failure and reclaims it then. */
    if (!decode_cleanup_failed) {
        if (p->decoder) tj3Destroy(p->decoder);
        free(p->planes[0]); free(p->planes[1]); free(p);
        free(s->slots[0]); free(s->slots[1]);
    }
    color_cleanup_finished(s, cleanup_started);
    if (cleanup_failed) reason = D200_VS_CLEANUP_FAILED;
    /* Finish an already partially written progress record before terminal.
     * No replacement/truncation of a record on this ordered byte stream. */
    uint64_t end = monotonic_ns() + UINT64_C(2000000000);
    while (!notice_failed && s->out_size && monotonic_ns() < end) {
        if (color_flush(s, fd)) break;
        struct pollfd pollfd = {fd, POLLOUT, 0}; (void)poll(&pollfd, 1, 10);
    }
    if (notice_failed) sent = 0;
    else if (terminal_queued) sent = !s->out_size;
    else if (!color_terminal(s, reason, cleanup_failed)) {
        while (s->out_size && monotonic_ns() < end) {
            if (color_flush(s, fd)) break;
            struct pollfd pollfd = {fd, POLLOUT, 0}; (void)poll(&pollfd, 1, 10);
        }
        sent = !s->out_size;
    }
    if (close(fd)) cleanup_failed = 1;
    char summary[COLOR_SUMMARY_CAPACITY];
    int summary_size = color_summary(s, summary, sizeof(summary),
        sent && !cleanup_failed ? reason : D200_VS_CLEANUP_FAILED);
    if (summary_size > 0 && (size_t)summary_size < sizeof(summary))
        (void)color_write_summary(STDERR_FILENO, summary, (size_t)summary_size);
    free(s);
    /* Exit zero attests delivery plus checked cleanup, not playback success. */
    return sent && !cleanup_failed ? 0 : D200_VS_CLEANUP_FAILED;
}
static int parse_u32(const char *text, uint32_t *value) {
    char *end; unsigned long long n;
    if (!text[0]) return -1;
    for (const char *p = text; *p; p++) if (*p < '0' || *p > '9') return -1;
    errno = 0; n = strtoull(text, &end, 10);
    if (errno || *end || n > UINT32_MAX) return -1;
    *value = (uint32_t)n; return 0;
}
int main(int argc, char **argv) {
    uint8_t session[16]; uint32_t epoch, n, d, fd;
    if (argc != 7 || strcmp(argv[1], "--stream") || strlen(argv[2]) != 32 ||
        parse_u32(argv[3], &epoch) || epoch != 1 || parse_u32(argv[4], &n) ||
        parse_u32(argv[5], &d) || !n || !d || n > UINT64_C(240) * d ||
        parse_u32(argv[6], &fd) || fd < 3 || fd > INT32_MAX) return 2;
    for (unsigned i = 0; i < 16; i++) {
        unsigned value = 0;
        for (unsigned j = 0; j < 2; j++) {
            char c = argv[2][i * 2 + j];
            if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return 2;
            value = value * 16 + (unsigned)(c <= '9' ? c - '0' : c - 'a' + 10);
        }
        session[i] = (uint8_t)value;
    }
    int type; socklen_t size = sizeof(type);
    if (getsockopt((int)fd, SOL_SOCKET, SO_TYPE, &type, &size) || type != SOCK_STREAM) return 2;
    signal(SIGTERM, stop_signal); signal(SIGINT, stop_signal); signal(SIGUSR1, stop_signal); signal(SIGPIPE, SIG_IGN);
    sigset_t unblock;
    sigemptyset(&unblock); sigaddset(&unblock, SIGTERM); sigaddset(&unblock, SIGUSR1);
    if (sigprocmask(SIG_UNBLOCK, &unblock, NULL)) return 2;
    return worker(session, n, d, (int)fd);
}
