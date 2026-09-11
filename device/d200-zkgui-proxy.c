#define _GNU_SOURCE
#include <arpa/inet.h>
#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <sys/un.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>
#include "d200_video_stream.h"

#define MAX_PAYLOAD (64U * 1024U)
#define CAPABILITY_MAX 128U
#define WAIT_MS 5000
#define RECORD_MS 30000
#define KILL_WAIT_MS 2000
#define HEARTBEAT_TIMEOUT_MS 120000

enum packet_kind {
    HELLO = 1, READY, OUTPUT0, OUTPUT1, INPUT0, INPUT1, STOP, ERROR,
    RESTORED, OUTPUT_ACK, BOOTSTRAP, RESTORING, PING, PONG
};
struct packet {
    uint8_t kind;
    uint32_t length;
    uint32_t sequence;
    unsigned char data[MAX_PAYLOAD];
};
struct pending { bool used; struct packet packet; };
struct control_output {
    unsigned char bytes[16 + MAX_PAYLOAD];
    size_t size, sent;
    uint64_t started;
};
/* VIDEO_DECL_BEGIN */
struct video_record {
    unsigned char bytes[40 + D200_VS_MAX_PAYLOAD];
    size_t have, need, sent;
    uint64_t started;
    bool complete;
    bool recv_observed;
    uint64_t recv_ms;
    size_t recv_requested;
    ssize_t recv_result;
    int recv_errno;
};
struct video_control_observation {
    bool received, enqueued, sent;
    uint32_t sequence, reply_sequence;
    uint64_t received_ms, enqueued_ms, sent_ms;
};
struct video {
    int listener, data_fd, child_fd;
    pid_t pid;
    uint16_t port;
    uint32_t state, reason, initial_reason, fps_n, fps_d;
    uint8_t session[16], capability[32];
    d200_vs_state external, internal;
    struct video_record up, down;
    uint64_t opened, failed_at, progress, drain_started, last_tick, max_tick_gap;
    bool exists, failed, killed, reaped, checked, terminal_seen, data_eof, fd_failed;
    int exit_status;
    bool exit_status_known, wait_reported;
    uint8_t terminal_kind;
    uint32_t terminal_reason;
    bool cancel_pending;
    uint8_t cancel_prefix[36];
    /* Observation only: no field below participates in flow control. */
    struct state *diagnostic_owner;
    bool data_recv_observed, data_bytes_saturated;
    uint64_t data_recv_ms, data_received;
    size_t data_recv_requested;
    ssize_t data_recv_result;
    int data_recv_errno;
    struct video_control_observation control_observation[2]; /* STATUS, CANCEL */
};
/* VIDEO_DECL_END */
struct state {
    int lock_fd, listener[2], peer[2];
    char session[PATH_MAX], sockpath[2][PATH_MAX], preload[PATH_MAX];
    pid_t child;
    uint32_t rx_seq, tx_seq;
    uint64_t last_rx_ms;
    struct video video;
    uint64_t video_generation;
    bool generation_known, generation_on_connection;
    bool stdout_ok, hello, lock_owned, listener_owned[2];
    struct pending pending[2];
    struct control_output output[4];
    struct control_output input;
    unsigned output_head, output_count;
    bool rx_exhausted, tx_exhausted;
    const char *exit_origin, *control_loss;
    uint64_t exit_observed_ms, control_loss_ms;
    int stock_wait_status;
    bool stock_wait_known, control_eof;
};

static int send_output_ack(struct state *s, uint32_t sequence);
static int send_packet(struct state *s, uint8_t kind, const void *payload,
                       uint32_t length);
static uint64_t monotonic_ms(void);
static int video_open(struct video *v, const struct packet *packet);
static void video_socket_buffers(int fd);

/* LIFECYCLE_BEGIN: actual production diagnostic helpers, host-effect tested.
 * Callers supply only fixed literals, never command/capability/error text.
 * A control boundary is not a loop exit. Unhandled fatal signals may emit nothing.
 * Observation must preserve errno and never retry, wait, or affect protocol IO. */
static void lifecycle_write(const char *data, size_t length)
{
    int saved = errno;
    if (length > 0 && length < 768) {
        int fd = open("/proc/self/fd/2", O_WRONLY | O_NONBLOCK | O_CLOEXEC | O_APPEND);
        if (fd >= 0) {
            (void)write(fd, data, length);
            (void)close(fd);
        }
    }
    errno = saved;
}

static void lifecycle_status(char *out, size_t size, bool known, int status)
{
    char exited[24] = "null", signaled[24] = "null";
    if (!known) { (void)snprintf(out, size, "null"); return; }
    if (WIFEXITED(status)) (void)snprintf(exited, sizeof(exited), "%d", WEXITSTATUS(status));
    if (WIFSIGNALED(status)) (void)snprintf(signaled, sizeof(signaled), "%d", WTERMSIG(status));
    (void)snprintf(out, size, "{\"raw\":%d,\"exitCode\":%s,\"signal\":%s}", status, exited, signaled);
}

static void lifecycle_emit(struct state *s, const char *event)
{
    int saved = errno;
    char agent[96], stock[96], origin[64] = "null", loss[64] = "null", out[768];
    char exit_time[32] = "null", loss_time[32] = "null";
    lifecycle_status(agent, sizeof(agent), s->video.exit_status_known, s->video.exit_status);
    lifecycle_status(stock, sizeof(stock), s->stock_wait_known, s->stock_wait_status);
    if (s->exit_origin) (void)snprintf(origin, sizeof(origin), "\"%s\"", s->exit_origin);
    if (s->control_loss) (void)snprintf(loss, sizeof(loss), "\"%s\"", s->control_loss);
    if (s->exit_origin) (void)snprintf(exit_time, sizeof(exit_time), "%llu", (unsigned long long)s->exit_observed_ms);
    if (s->control_loss) (void)snprintf(loss_time, sizeof(loss_time), "%llu", (unsigned long long)s->control_loss_ms);
    int n = snprintf(out, sizeof(out),
        "{\"event\":\"proxyLifecycle\",\"phase\":\"%s\",\"clock\":\"device-monotonic-ms\","
        "\"observedMs\":%llu,\"firstExitOrigin\":%s,\"exitObservedMs\":%s,"
        "\"firstControlLoss\":%s,\"controlLossMs\":%s,\"agentWait\":%s,\"stockWait\":%s,\"pixelProof\":false}\n",
        event, (unsigned long long)monotonic_ms(), origin, exit_time,
        loss, loss_time, agent, stock);
    if (n > 0 && (size_t)n < sizeof(out)) lifecycle_write(out, (size_t)n);
    errno = saved;
}

static void lifecycle_exit(struct state *s, const char *reason)
{
    int saved = errno;
    if (!s->exit_origin) {
        s->exit_origin = reason; s->exit_observed_ms = monotonic_ms();
        lifecycle_emit(s, "loop-exit");
    }
    errno = saved;
}

static void lifecycle_control_loss(struct state *s, const char *reason)
{
    int saved = errno;
    if (!s->control_loss) {
        s->control_loss = reason; s->control_loss_ms = monotonic_ms();
        lifecycle_emit(s, "control-loss");
    }
    errno = saved;
}
static void lifecycle_agent_wait(struct state *s)
{
    if (s->video.reaped && !s->video.wait_reported) {
        s->video.wait_reported = true;
        lifecycle_emit(s, "agent-wait");
    }
}
/* LIFECYCLE_END */

/* VIDEO_CORE_BEGIN: allowlisted production code for effect-injected fixtures. */
static int video_close_fd(int *fd)
{
    int result = *fd >= 0 ? close(*fd) : 0;
    *fd = -1;
    return result;
}

static void video_reset(struct video *v)
{
    memset(v, 0, sizeof(*v));
    v->listener = v->data_fd = v->child_fd = -1;
    v->pid = -1;
    v->up.need = v->down.need = 40;
}

static void video_fail(struct video *v, uint32_t reason)
{
    if (!v->exists || v->checked || v->failed) return;
    int saved_errno = errno;
    uint64_t observed = monotonic_ms();
    char trace[2560], last_recv[256] = "null", control[2][256], queue[256] = "null";
    char public_session[33], pending_json[32] = "null";
    for (unsigned i = 0; i < 16; i++)
        (void)snprintf(public_session + i * 2, 3, "%02x", v->session[i]);
    if (v->data_recv_observed) {
        char error[32] = "null";
        if (v->data_recv_result < 0) (void)snprintf(error, sizeof(error), "%d", v->data_recv_errno);
        (void)snprintf(last_recv, sizeof(last_recv),
            "{\"observedMs\":%llu,\"requested\":%zu,\"result\":%lld,\"errno\":%s}",
            (unsigned long long)v->data_recv_ms, v->data_recv_requested,
            (long long)v->data_recv_result, error);
    }
    for (unsigned i = 0; i < 2; i++) {
        const struct video_control_observation *o = &v->control_observation[i];
        if (!o->received) { (void)snprintf(control[i], sizeof(control[i]), "null"); continue; }
        char enqueued[32] = "null", sent[32] = "null", reply_sequence[32] = "null";
        if (o->enqueued) {
            (void)snprintf(enqueued, sizeof(enqueued), "%llu", (unsigned long long)o->enqueued_ms);
            (void)snprintf(reply_sequence, sizeof(reply_sequence), "%u", o->reply_sequence);
        }
        if (o->sent) (void)snprintf(sent, sizeof(sent), "%llu", (unsigned long long)o->sent_ms);
        (void)snprintf(control[i], sizeof(control[i]),
            "{\"sequence\":%u,\"receivedMs\":%llu,\"replySequence\":%s,\"replyEnqueuedMs\":%s,\"replySentMs\":%s}",
            o->sequence, (unsigned long long)o->received_ms, reply_sequence, enqueued, sent);
    }
    const struct state *owner = v->diagnostic_owner;
    if (owner != NULL) {
        if (owner->output_count) {
            const struct control_output *head = &owner->output[owner->output_head];
            (void)snprintf(queue, sizeof(queue),
                "{\"count\":%u,\"headKind\":%u,\"headSequence\":%u,\"headSent\":%zu,\"headSize\":%zu,\"headAgeMs\":%llu}",
                owner->output_count, (unsigned)head->bytes[5], d200_vs_get_u32(head->bytes + 12),
                head->sent, head->size, (unsigned long long)(observed >= head->started ? observed - head->started : 0));
        } else (void)snprintf(queue, sizeof(queue),
            "{\"count\":0,\"headKind\":null,\"headSequence\":null,\"headSent\":null,\"headSize\":null,\"headAgeMs\":null}");
    }
    /* Snapshot only, before closing the data fd. FIONREAD neither consumes
     * data nor clears SO_ERROR. Unsupported/failed queries remain unknown. */
#ifdef FIONREAD
    if (v->data_fd >= 0) {
        int pending = -1;
        if (ioctl(v->data_fd, FIONREAD, &pending) == 0 && pending >= 0)
            (void)snprintf(pending_json, sizeof(pending_json), "%d", pending);
    }
#endif
    errno = saved_errno;
    int length = snprintf(trace, sizeof(trace),
        "{\"event\":\"videoRelayFailure\",\"session\":\"%s\",\"epoch\":1,\"clock\":\"device-monotonic-ms\","
        "\"observedMs\":%llu,\"reason\":%u,\"state\":%u,"
        "\"externalReceived\":%llu,\"externalConsumed\":%llu,\"internalReceived\":%llu,\"internalConsumed\":%llu,"
        "\"partialBytes\":%u,\"upHave\":%zu,\"upNeed\":%zu,\"upSent\":%zu,\"upComplete\":%u,"
        "\"downHave\":%zu,\"downNeed\":%zu,\"downSent\":%zu,\"downComplete\":%u,"
        "\"progressAgeMs\":%llu,\"upAgeMs\":%llu,\"maxTickGapMs\":%llu,"
        "\"lastDataRecv\":%s,\"dataReceivedBytes\":%llu,\"dataBytesSaturated\":%s,"
        "\"dataFdPresent\":%s,\"dataReadEligible\":%s,\"failedBefore\":%s,\"checkedBefore\":%s,\"cancelPending\":%s,"
        "\"dataPendingBytes\":%s,\"lastStatus\":%s,\"lastCancel\":%s,\"controlQueue\":%s,\"pixelProof\":false}\n",
        public_session, (unsigned long long)observed, reason, v->state,
        (unsigned long long)v->external.received, (unsigned long long)v->external.consumed,
        (unsigned long long)v->internal.received, (unsigned long long)v->internal.consumed,
        v->internal.partial_total, v->up.have, v->up.need, v->up.sent, (unsigned)v->up.complete,
        v->down.have, v->down.need, v->down.sent, (unsigned)v->down.complete,
        (unsigned long long)(observed >= v->progress ? observed - v->progress : 0),
        (unsigned long long)(v->up.have && observed >= v->up.started ? observed - v->up.started : 0),
        (unsigned long long)v->max_tick_gap, last_recv, (unsigned long long)v->data_received,
        v->data_bytes_saturated ? "true" : "false", v->data_fd >= 0 ? "true" : "false",
        v->data_fd >= 0 && !v->failed && !v->checked && !v->up.complete ? "true" : "false",
        v->failed ? "true" : "false", v->checked ? "true" : "false", v->cancel_pending ? "true" : "false",
        pending_json, control[0], control[1], queue);
    /* Optional diagnostics must never block cancellation on a full sink. */
    int flags = fcntl(STDERR_FILENO, F_GETFL);
    if (length > 0 && (size_t)length < sizeof(trace) && flags >= 0 &&
        ((flags & O_NONBLOCK) || !fcntl(STDERR_FILENO, F_SETFL, flags | O_NONBLOCK))) {
        (void)write(STDERR_FILENO, trace, (size_t)length);
        if (!(flags & O_NONBLOCK)) (void)fcntl(STDERR_FILENO, F_SETFL, flags);
    }
    errno = saved_errno;
    v->failed = true; v->failed_at = monotonic_ms(); v->reason = v->initial_reason = reason;
    v->state = D200_VS_CANCELLING;
    video_close_fd(&v->listener);
    video_close_fd(&v->data_fd); v->data_eof = true;
    /* Signals address only the stored child. The handler cooperatively cleans
     * DIVP and emits checked terminal bytes on the inherited socket. */
    if (v->pid > 0 && !v->reaped)
        (void)kill(v->pid, reason == D200_VS_SOURCE_FAILURE ? SIGUSR1 : SIGTERM);
}

/* Neither helper loops, polls, or waits for a complete record. */
static int video_read(int fd, struct video_record *r)
{
    d200_vs_header h;
    if (r->complete) return 0;
    ssize_t n = recv(fd, r->bytes + r->have, r->need - r->have, MSG_DONTWAIT);
    int saved_errno = errno;
    r->recv_observed = true;
    r->recv_ms = monotonic_ms();
    r->recv_requested = r->need - r->have;
    r->recv_result = n;
    r->recv_errno = n < 0 ? saved_errno : 0;
    errno = saved_errno;
    if (n < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) return 0;
    if (n <= 0) return -1;
    if (!r->have) r->started = monotonic_ms();
    r->have += (size_t)n;
    if (r->have == 40 && r->need == 40) {
        if (!d200_vs_decode_header(r->bytes, 40, &h)) return -2;
        r->need += h.payload_length;
    } else if (r->have == r->need) r->complete = true;
    return 0;
}

static int video_write(int fd, struct video_record *r)
{
    if (!r->complete) return 0;
    ssize_t n = send(fd, r->bytes + r->sent, r->need - r->sent, MSG_NOSIGNAL | MSG_DONTWAIT);
    if (n < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) return 0;
    if (n <= 0) return -1;
    r->sent += (size_t)n;
    if (r->sent == r->need) { memset(r, 0, sizeof(*r)); r->need = 40; }
    return 0;
}

static int video_owner(const struct video *v, const uint8_t *q)
{
    return v->exists && !memcmp(v->session, q + 8, 16) &&
        d200_vs_get_u32(q + 24) == 1 && !memcmp(v->capability, q + 28, 32);
}

static int video_generation(struct state *s, uint64_t generation)
{
    if (s->generation_on_connection) return generation == s->video_generation;
    if (s->generation_known && generation <= s->video_generation) return 0;
    s->generation_known = s->generation_on_connection = true;
    s->video_generation = generation;
    return 1;
}

static void video_prefix(uint8_t *r, const struct packet *p, uint32_t epoch, uint32_t result)
{
    memcpy(r, p->data, 8); d200_vs_put_u32(r + 8, p->sequence);
    memcpy(r + 12, p->data + 8, 16);
    d200_vs_put_u32(r + 28, epoch); d200_vs_put_u32(r + 32, result);
}

static int video_cancel_reply(struct state *s)
{
    struct video *v = &s->video;
    uint8_t reply[44] = {0};
    if (!v->cancel_pending || !s->hello || !s->generation_on_connection) return 0;
    if (!v->checked && !(v->failed && monotonic_ms() - v->failed_at >= 5000)) return 0;
    memcpy(reply, v->cancel_prefix, 36);
    if (v->checked) {
        d200_vs_put_u32(reply + 36, v->state); d200_vs_put_u32(reply + 40, v->reason);
    } else d200_vs_put_u32(reply + 32, D200_VS_CLEANUP_FAILED);
    v->cancel_pending = false;
    return send_packet(s, D200_VS_VIDEO_CANCEL_RESULT, reply, sizeof(reply));
}

static int video_control(struct state *s, const struct packet *p)
{
    struct video *v = &s->video;
    v->diagnostic_owner = s;
    if (p->kind == D200_VS_VIDEO_STATUS_REQUEST || p->kind == D200_VS_VIDEO_CANCEL_REQUEST) {
        int saved_errno = errno;
        unsigned index = p->kind == D200_VS_VIDEO_STATUS_REQUEST ? 0 : 1;
        v->control_observation[index] = (struct video_control_observation){
            .received = true, .sequence = p->sequence, .received_ms = monotonic_ms()
        };
        errno = saved_errno;
    }
    uint8_t reply[72] = {0};
    uint32_t result = 0, epoch = p->kind == D200_VS_VIDEO_OPEN_REQUEST ? 0 : d200_vs_get_u32(p->data + 24);
    uint32_t length = d200_vs_control_length(p->kind + 1);
    if (p->kind == D200_VS_VIDEO_OPEN_REQUEST && p->data[28] != D200_VS_VERSION) result = D200_VS_UNSUPPORTED_VERSION;
    else if (!d200_vs_validate_control_payload(p->kind, p->data, p->length)) result = D200_VS_INVALID_PARAMETERS;
    else if (!video_generation(s, d200_vs_get_u64(p->data))) result = D200_VS_UNAUTHORIZED;
    else if (p->kind == D200_VS_VIDEO_OPEN_REQUEST) {
        uint32_t n = d200_vs_get_u32(p->data + 32), d = d200_vs_get_u32(p->data + 36);
        if (n > UINT64_C(240) * d) result = D200_VS_INVALID_PARAMETERS;
        else if (v->exists && (!v->checked || !memcmp(v->session, p->data + 8, 16))) result = D200_VS_BUSY;
        else if (video_open(v, p)) result = D200_VS_START_FAILED;
        else {
            epoch = 1; reply[36] = 1; d200_vs_put_u16(reply + 38, v->port);
            memcpy(reply + 40, v->capability, 32);
        }
    } else if (!video_owner(v, p->data)) result = D200_VS_UNAUTHORIZED;
    else if (p->kind == D200_VS_VIDEO_CANCEL_REQUEST) {
        if (v->cancel_pending) result = D200_VS_BUSY;
        else {
            video_prefix(v->cancel_prefix, p, 1, 0); v->cancel_pending = true;
            video_fail(v, d200_vs_get_u32(p->data + 60));
            return video_cancel_reply(s);
        }
    } else {
        d200_vs_put_u32(reply + 36, v->state);
        d200_vs_put_u64(reply + 40, v->internal.received);
        d200_vs_put_u64(reply + 48, v->internal.consumed);
        d200_vs_put_u64(reply + 56, v->internal.has_eos ? v->internal.eos : UINT64_MAX);
        d200_vs_put_u32(reply + 64, v->reason);
        d200_vs_put_u32(reply + 68, v->internal.ready);
    }
    video_prefix(reply, p, epoch, result);
    return send_packet(s, p->kind + 1, reply, length);
}

static void video_up(struct video *v)
{
    struct video_record *r = &v->up;
    d200_vs_header h;
    if (r->complete) { if (video_write(v->child_fd, r)) video_fail(v, D200_VS_DISCONNECTED); return; }
    int rc = video_read(v->data_fd, r);
    v->data_recv_observed = r->recv_observed;
    v->data_recv_ms = r->recv_ms;
    v->data_recv_requested = r->recv_requested;
    v->data_recv_result = r->recv_result;
    v->data_recv_errno = r->recv_errno;
    if (r->recv_result > 0) {
        uint64_t bytes = (uint64_t)r->recv_result;
        if (UINT64_MAX - v->data_received < bytes) {
            v->data_received = UINT64_MAX; v->data_bytes_saturated = true;
        } else v->data_received += bytes;
    }
    if (rc) { v->data_eof = true; video_fail(v, rc == -2 ? D200_VS_PROTOCOL : D200_VS_DISCONNECTED); return; }
    if (!r->complete) return;
    uint8_t *p = r->bytes + 40;
    if (!d200_vs_decode_record(r->bytes, r->need, D200_VS_PRODUCER, &h) ||
        !d200_vs_state_accept(&v->external, &h, p, D200_VS_PRODUCER)) { video_fail(v, D200_VS_PROTOCOL); return; }
    if (h.kind == D200_VS_ATTACH) {
        video_close_fd(&v->listener); v->state = D200_VS_ATTACHING;
        memset(r, 0, sizeof(*r)); r->need = 40;
        return;
    }
    if (h.kind == D200_VS_CANCEL || h.kind == D200_VS_ERROR) {
        video_fail(v, d200_vs_get_u32(p)); return;
    }
    h.sequence = v->internal.sequence[0];
    if (!d200_vs_state_accept(&v->internal, &h, p, D200_VS_PRODUCER) ||
        !d200_vs_encode_header(r->bytes, 40, &h)) { video_fail(v, D200_VS_PROTOCOL); return; }
    if (h.kind == D200_VS_EOS) { v->state = D200_VS_DRAINING; v->drain_started = monotonic_ms(); }
    else v->state = D200_VS_STREAMING;
    v->progress = monotonic_ms();
    if (video_write(v->child_fd, r)) video_fail(v, D200_VS_DISCONNECTED);
}

static void video_down(struct video *v)
{
    struct video_record *r = &v->down;
    d200_vs_header h;
    if (r->complete) {
        if (v->failed && !v->terminal_seen) { memset(r, 0, sizeof(*r)); r->need = 40; return; }
        if (v->terminal_seen || !v->external.attached) return;
        if (v->data_fd >= 0 && video_write(v->data_fd, r)) video_fail(v, D200_VS_DISCONNECTED);
        return;
    }
    int rc = video_read(v->child_fd, r);
    if (rc) {
        video_close_fd(&v->child_fd);
        if (!v->terminal_seen) video_fail(v, rc == -2 ? D200_VS_PROTOCOL : D200_VS_DISCONNECTED);
        return;
    }
    if (!r->complete) return;
    uint8_t *p = r->bytes + 40;
    if (!d200_vs_decode_record(r->bytes, r->need, D200_VS_CONSUMER, &h)) { video_fail(v, D200_VS_PROTOCOL); return; }
    /* SIGTERM/SIGUSR1 is an out-of-band owned CANCEL on the private hop. */
    if (h.kind == D200_VS_CANCELLED && v->failed) v->internal.cancel_reason = d200_vs_get_u32(p);
    if (!d200_vs_state_accept(&v->internal, &h, p, D200_VS_CONSUMER)) { video_fail(v, D200_VS_PROTOCOL); return; }
    if (h.kind == D200_VS_DONE || h.kind == D200_VS_CANCELLED || h.kind == D200_VS_ERROR) {
        v->terminal_seen = true; v->terminal_kind = h.kind;
        v->terminal_reason = h.kind == D200_VS_DONE ? 0 : d200_vs_get_u32(p);
        if (h.kind == D200_VS_ERROR) video_fail(v, v->terminal_reason);
        return;
    }
    if (v->failed) { memset(r, 0, sizeof(*r)); r->need = 40; return; }
    h.sequence = v->external.sequence[1];
    /* READY may precede ATTACH internally: hold it until authentication. */
    if (h.kind == D200_VS_READY && !v->external.attached) {
        if (!d200_vs_encode_header(r->bytes, 40, &h)) video_fail(v, D200_VS_PROTOCOL);
        return;
    }
    if (!d200_vs_state_accept(&v->external, &h, p, D200_VS_CONSUMER) ||
        !d200_vs_encode_header(r->bytes, 40, &h)) { video_fail(v, D200_VS_PROTOCOL); return; }
    v->progress = monotonic_ms();
    if (h.kind == D200_VS_READY) v->state = D200_VS_STATE_READY;
    if (v->data_fd >= 0 && video_write(v->data_fd, r)) video_fail(v, D200_VS_DISCONNECTED);
}

static void video_terminal(struct video *v)
{
    struct video_record *r = &v->down;
    d200_vs_header h = {0};
    uint8_t p[20] = {0};
    if (!v->reaped || v->checked) return;
    int close_failed = video_close_fd(&v->listener);
    if (video_close_fd(&v->child_fd)) close_failed = -1;
    if (close_failed) v->fd_failed = true;
    if (v->fd_failed || !v->terminal_seen || v->killed || !WIFEXITED(v->exit_status) || WEXITSTATUS(v->exit_status) ||
        v->terminal_reason == D200_VS_CLEANUP_FAILED) {
        v->state = D200_VS_FAILED; v->reason = D200_VS_CLEANUP_FAILED;
        return;
    }
    v->checked = true;
    if (!v->failed && v->terminal_kind == D200_VS_DONE) {
        v->state = D200_VS_STATE_DONE; v->reason = 0;
        h.kind = D200_VS_DONE; h.payload_length = 20;
        d200_vs_put_u64(p, v->internal.eos); d200_vs_put_u64(p + 8, v->internal.consumed);
    } else {
        v->reason = v->initial_reason ? v->initial_reason : v->terminal_reason;
        v->state = d200_vs_reason(v->reason) ? D200_VS_STATE_CANCELLED : D200_VS_FAILED;
        h.kind = d200_vs_reason(v->reason) ? D200_VS_CANCELLED : D200_VS_ERROR;
        h.payload_length = 4; d200_vs_put_u32(p, v->reason);
    }
    h.epoch = 1; h.sequence = v->external.sequence[1]; memcpy(h.session, v->session, 16);
    /* No old down record remains: terminal was retained in this same slot. */
    memset(r, 0, sizeof(*r)); r->need = 40 + h.payload_length;
    if (v->external.attached && !v->data_eof && d200_vs_encode_record(r->bytes, sizeof(r->bytes), &h, p)) {
        r->have = r->need; r->complete = true; r->started = monotonic_ms();
    }
}

static unsigned video_pollfds(const struct video *v, struct pollfd *fds)
{
    unsigned count = 0;
    short data_events = 0, child_events = 0;
    if (!v->exists) return 0;
    if (v->listener >= 0 && v->data_fd < 0 && !v->failed && !v->checked)
        fds[count++] = (struct pollfd){ .fd = v->listener, .events = POLLIN };
    if (!v->failed && !v->checked && !v->up.complete) data_events |= POLLIN;
    if (v->down.complete && (v->checked || (v->external.attached && !v->terminal_seen)))
        data_events |= POLLOUT;
    if (v->data_fd >= 0 && data_events)
        fds[count++] = (struct pollfd){ .fd = v->data_fd, .events = data_events };
    if (!v->checked && !v->terminal_seen && !v->down.complete) child_events |= POLLIN;
    if (!v->checked && !v->failed && v->up.complete) child_events |= POLLOUT;
    if (v->child_fd >= 0 && child_events)
        fds[count++] = (struct pollfd){ .fd = v->child_fd, .events = child_events };
    return count;
}

static void video_tick(struct state *s)
{
    struct video *v = &s->video;
    v->diagnostic_owner = s;
    uint64_t now = monotonic_ms();
    if (!v->exists) return;
    if (v->last_tick && now >= v->last_tick && now - v->last_tick > v->max_tick_gap)
        v->max_tick_gap = now - v->last_tick;
    v->last_tick = now;
    if (v->checked) {
        if (v->down.complete && v->data_fd >= 0 && video_write(v->data_fd, &v->down)) video_close_fd(&v->data_fd);
        if (!v->down.complete || now - v->down.started >= 5000) video_close_fd(&v->data_fd);
        (void)video_cancel_reply(s); return;
    }
    if (v->listener >= 0 && v->data_fd < 0 && !v->failed) {
        v->data_fd = accept4(v->listener, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
        if (v->data_fd < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) video_fail(v, D200_VS_DISCONNECTED);
        if (v->data_fd >= 0) video_socket_buffers(v->data_fd);
    }
    if (v->data_fd >= 0 && !v->failed) video_up(v);
    /* Authorize a READY held while external ATTACH was pending. */
    if (v->down.complete && !v->terminal_seen && v->external.attached && !v->external.ready) {
        d200_vs_header h;
        if (!d200_vs_decode_record(v->down.bytes, v->down.need, D200_VS_CONSUMER, &h) ||
            !d200_vs_state_accept(&v->external, &h, v->down.bytes + 40, D200_VS_CONSUMER)) video_fail(v, D200_VS_PROTOCOL);
        else { v->state = D200_VS_STATE_READY; v->progress = now; }
    }
    if (v->child_fd >= 0 && !v->terminal_seen) video_down(v);
    if (!v->reaped && v->pid > 0) {
        pid_t result = waitpid(v->pid, &v->exit_status, WNOHANG);
        if (result == v->pid) { v->reaped = true; v->exit_status_known = true; }
        else if (result < 0 && errno != EINTR) { v->reaped = true; v->exit_status = 1 << 8; }
    }
    /* Read buffered terminal bytes before treating an observed exit as loss. */
    if (v->reaped && (v->terminal_seen || v->child_fd < 0)) video_terminal(v);
    /* I/O above may stamp progress after this tick's initial clock sample. */
    now = monotonic_ms();
    uint64_t period = UINT64_C(1000) * v->fps_d / v->fps_n;
    uint64_t progress_budget = period > 10000 ? period * 3 : 30000;
    uint64_t drain_budget = period * 2 + 5000;
    if (drain_budget < 10000) drain_budget = 10000;
    if (!v->failed && !v->checked &&
        ((!v->external.ready && now - v->opened >= 10000) ||
         (v->up.have && now - v->up.started >= RECORD_MS) ||
         (v->down.have && !v->terminal_seen && (!v->down.complete || v->external.attached) && now - v->down.started >= RECORD_MS) ||
         (v->drain_started && now - v->drain_started >= drain_budget) ||
         now - v->progress >= progress_budget)) video_fail(v, D200_VS_TIMEOUT);
    /* video_fail can establish a newer cancellation deadline. */
    now = monotonic_ms();
    if (v->failed && !v->reaped && now - v->failed_at >= 3000 && !v->killed) {
        v->killed = true; (void)kill(v->pid, SIGKILL);
    }
    if (v->failed && !v->checked && now - v->failed_at >= 5000) {
        v->state = D200_VS_FAILED; v->reason = D200_VS_CLEANUP_FAILED;
    }
    (void)video_cancel_reply(s);
}
/* VIDEO_CORE_END */

static void connection_boundary(struct state *s)
{
    memset(s->pending, 0, sizeof(s->pending));
    memset(s->output, 0, sizeof(s->output));
    memset(&s->input, 0, sizeof(s->input));
    s->output_head = s->output_count = 0;
    s->rx_exhausted = s->tx_exhausted = false;
    s->generation_on_connection = false;
    s->video.cancel_pending = false;
    s->hello = false;
    s->stdout_ok = false;
}

static volatile sig_atomic_t stop_requested;
static volatile sig_atomic_t stop_signo;
static struct termios original_terminal;
static int terminal_changed;

static void signal_handler(int signo) { if (!stop_signo) stop_signo = signo; stop_requested = 1; }

static int configure_raw_terminal(void)
{
    struct termios raw;
    if (!isatty(STDIN_FILENO)) return 0;
    if (tcgetattr(STDIN_FILENO, &original_terminal)) return -1;
    raw = original_terminal;
    cfmakeraw(&raw);
    if (tcsetattr(STDIN_FILENO, TCSANOW, &raw)) return -1;
    terminal_changed = 1;
    return 0;
}

static void restore_terminal(void)
{
    if (terminal_changed)
        (void)tcsetattr(STDIN_FILENO, TCSANOW, &original_terminal);
    terminal_changed = 0;
}

static uint64_t monotonic_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000U + (uint64_t)ts.tv_nsec / 1000000U;
}

static int safe_tmp_absolute(const char *path)
{
    const char *p;
    if (path == NULL || strncmp(path, "/tmp/", 5) != 0 || path[5] == '\0') return 0;
    for (p = path + 5; *p; ++p)
        if (p[0] == '/' && p[1] == '.' && p[2] == '.' && (p[3] == '/' || p[3] == '\0')) return 0;
    return strstr(path, "/../") == NULL && strcmp(path + strlen(path) - 3, "/..") != 0;
}


static int valid_packet_length(uint8_t kind, uint32_t length)
{
    if (length > MAX_PAYLOAD) return 0;
    switch (kind) {
    case HELLO: return length >= 1 && length <= CAPABILITY_MAX;
    case OUTPUT0: case OUTPUT1: case INPUT0: case INPUT1: case ERROR:
        return 1;
    case 21: case 22: case 23: case 24: case 25: case 26:
        return length == d200_vs_control_length(kind);
    case OUTPUT_ACK: return length == 4;
    case READY: case STOP: case RESTORED: case BOOTSTRAP: case RESTORING:
    case PING: case PONG: return length == 0;
    default: return 0;
    }
}

/* Control replies share one bounded ordered queue with HID output, so a
 * partially sent video reply cannot interleave or block the Studio loop. */
static int control_flush(struct state *s)
{
    if (!s->stdout_ok) return -1;
    if (!s->output_count) return 0;
    struct control_output *o = &s->output[s->output_head];
    if (monotonic_ms() - o->started >= WAIT_MS) { s->stdout_ok = false; errno = ETIMEDOUT; return -1; }
    ssize_t n = send(STDOUT_FILENO, o->bytes + o->sent, o->size - o->sent, MSG_NOSIGNAL | MSG_DONTWAIT);
    if (n < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) return 0;
    if (n <= 0) { s->stdout_ok = false; return -1; }
    o->sent += (size_t)n;
    if (o->sent == o->size) {
        if (o->bytes[5] == D200_VS_VIDEO_STATUS_RESULT || o->bytes[5] == D200_VS_VIDEO_CANCEL_RESULT) {
            unsigned index = o->bytes[5] == D200_VS_VIDEO_STATUS_RESULT ? 0 : 1;
            struct video_control_observation *observation = &s->video.control_observation[index];
            if (observation->received && observation->enqueued &&
                observation->sequence == d200_vs_get_u32(o->bytes + 24) &&
                observation->reply_sequence == d200_vs_get_u32(o->bytes + 12)) {
                int saved_errno = errno;
                observation->sent = true; observation->sent_ms = monotonic_ms();
                errno = saved_errno;
            }
        }
        memset(o, 0, sizeof(*o)); s->output_head = (s->output_head + 1) % 4; s->output_count--;
    }
    return 0;
}

static int send_packet(struct state *s, uint8_t kind, const void *payload, uint32_t length)
{
    if (!s->stdout_ok || !valid_packet_length(kind, length) || s->tx_exhausted) return -1;
    if (s->output_count == 4) { s->stdout_ok = false; errno = ENOBUFS; return -1; }
    struct control_output *o = &s->output[(s->output_head + s->output_count) % 4];
    memcpy(o->bytes, "D2PX", 4); o->bytes[4] = 1; o->bytes[5] = kind; o->bytes[6] = o->bytes[7] = 0;
    d200_vs_put_u32(o->bytes + 8, length); d200_vs_put_u32(o->bytes + 12, s->tx_seq);
    if (s->tx_seq == UINT32_MAX) s->tx_exhausted = true;
    else s->tx_seq++;
    if (length) memcpy(o->bytes + 16, payload, length);
    o->size = 16 + length; o->sent = 0; o->started = monotonic_ms(); s->output_count++;
    if (kind == D200_VS_VIDEO_STATUS_RESULT || kind == D200_VS_VIDEO_CANCEL_RESULT) {
        unsigned index = kind == D200_VS_VIDEO_STATUS_RESULT ? 0 : 1;
        struct video_control_observation *observation = &s->video.control_observation[index];
        if (observation->received && observation->sequence == d200_vs_get_u32(o->bytes + 24)) {
            observation->enqueued = true; observation->enqueued_ms = o->started;
            observation->reply_sequence = d200_vs_get_u32(o->bytes + 12);
        }
    }
    return control_flush(s);
}

static int receive_packet(struct state *s, struct packet *packet)
{
    struct control_output *in = &s->input;
    if (stop_requested) { errno = ECANCELED; return -1; }
    if (in->sent && monotonic_ms() - in->started >= WAIT_MS) { errno = ETIMEDOUT; return -1; }
    if (!in->size) in->size = 16;
    ssize_t n = recv(STDIN_FILENO, in->bytes + in->sent, in->size - in->sent, MSG_DONTWAIT);
    if (n < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) return 1;
    if (n <= 0) { if (n == 0) s->control_eof = true; errno = ECONNRESET; return -1; }
    if (!in->sent) in->started = monotonic_ms();
    in->sent += (size_t)n;
    if (in->sent < in->size) return 1;
    uint32_t length = d200_vs_get_u32(in->bytes + 8), sequence = d200_vs_get_u32(in->bytes + 12);
    if (memcmp(in->bytes, "D2PX", 4) || in->bytes[4] != 1 || in->bytes[6] || in->bytes[7] ||
        !valid_packet_length(in->bytes[5], length) || s->rx_exhausted || sequence != s->rx_seq) { errno = EPROTO; return -1; }
    if (in->size == 16 && length) { in->size += length; return 1; }
    if (s->rx_seq == UINT32_MAX) s->rx_exhausted = true;
    else s->rx_seq++;
    packet->kind = in->bytes[5]; packet->length = length; packet->sequence = sequence;
    if (length) memcpy(packet->data, in->bytes + 16, length);
    memset(in, 0, sizeof(*in));
    s->last_rx_ms = monotonic_ms();
    return 0;
}

static void send_error(struct state *s, const char *message)
{
    size_t length = strlen(message);
    if (length > MAX_PAYLOAD) length = MAX_PAYLOAD;
    (void)send_packet(s, ERROR, message, (uint32_t)length);
}

static int send_output_ack(struct state *s, uint32_t sequence)
{
    uint32_t encoded = htonl(sequence);
    return send_packet(s, OUTPUT_ACK, &encoded, sizeof(encoded));
}

static int run_service(const char *command)
{
    pid_t pid = fork(); int status;
    if (pid < 0) return -1;
    if (pid == 0) {
        int nullfd = open("/dev/null", O_RDWR);
        if (nullfd < 0 || dup2(nullfd, STDIN_FILENO) < 0 ||
            dup2(nullfd, STDOUT_FILENO) < 0 || dup2(nullfd, STDERR_FILENO) < 0)
            _exit(127);
        if (nullfd > STDERR_FILENO) close(nullfd);
        execl("/bin/sh", "sh", "-c", command, (char *)NULL);
        _exit(127);
    }
    while (waitpid(pid, &status, 0) < 0) if (errno != EINTR) return -1;
    return WIFEXITED(status) && WEXITSTATUS(status) == 0 ? 0 : -1;
}

static int is_zkgui_pid(const char *name)
{
    char path[PATH_MAX], cmd[PATH_MAX]; int fd; ssize_t n;
    if (snprintf(path, sizeof(path), "/proc/%s/cmdline", name) >= (int)sizeof(path)) return 0;
    fd = open(path, O_RDONLY | O_CLOEXEC); if (fd < 0) return 0;
    n = read(fd, cmd, sizeof(cmd) - 1); close(fd);
    if (n <= 0) return 0;
    cmd[n] = '\0';
    return strcmp(cmd, "/bin/zkgui") == 0;
}

static int count_zkgui(pid_t *only)
{
    DIR *dir = opendir("/proc"); struct dirent *entry; int count = 0;
    if (!dir) return -1;
    while ((entry = readdir(dir)) != NULL) {
        char *end; long p = strtol(entry->d_name, &end, 10);
        if (*entry->d_name && *end == '\0' && p > 1 && is_zkgui_pid(entry->d_name)) { count++; if (only) *only = (pid_t)p; }
    }
    closedir(dir); return count;
}

static int tty_s1_owned_by_zkgui(void)
{
    DIR *dir = opendir("/proc"); struct dirent *entry; int owners = 0;
    if (!dir) return 0;
    while ((entry = readdir(dir)) != NULL) {
        char *end; long pid = strtol(entry->d_name, &end, 10); DIR *fds; struct dirent *fdent; char fdpath[PATH_MAX], target[PATH_MAX];
        if (!*entry->d_name || *end || pid <= 1) continue;
        if (snprintf(fdpath, sizeof(fdpath), "/proc/%s/fd", entry->d_name) >= (int)sizeof(fdpath)) continue;
        fds = opendir(fdpath); if (!fds) continue;
        while ((fdent = readdir(fds)) != NULL) {
            ssize_t n;
            if (fdent->d_name[0] == '.') continue;
            if (snprintf(fdpath, sizeof(fdpath), "/proc/%ld/fd/%s", pid, fdent->d_name) >= (int)sizeof(fdpath)) continue;
            n = readlink(fdpath, target, sizeof(target) - 1);
            if (n > 0) { target[n] = '\0'; if (strcmp(target, "/dev/ttyS1") == 0) owners++; }
        }
        closedir(fds);
    }
    closedir(dir); return owners;
}

static int zkgui_owns_tty(pid_t pid)
{
    DIR *fds;
    struct dirent *fdent;
    char fdpath[PATH_MAX], target[PATH_MAX];
    if (snprintf(fdpath, sizeof(fdpath), "/proc/%ld/fd", (long)pid) >= (int)sizeof(fdpath)) return 0;
    fds = opendir(fdpath);
    if (!fds) return 0;
    while ((fdent = readdir(fds)) != NULL) {
        ssize_t n;
        if (fdent->d_name[0] == '.') continue;
        if (snprintf(fdpath, sizeof(fdpath), "/proc/%ld/fd/%s", (long)pid, fdent->d_name) >= (int)sizeof(fdpath)) continue;
        n = readlink(fdpath, target, sizeof(target) - 1);
        if (n > 0) {
            target[n] = '\0';
            if (strcmp(target, "/dev/ttyS1") == 0) { closedir(fds); return 1; }
        }
    }
    closedir(fds);
    return 0;
}

static int wait_no_zkgui_and_tty(void)
{
    uint64_t end = monotonic_ms() + WAIT_MS;
    while (monotonic_ms() < end) {
        if (count_zkgui(NULL) == 0 && tty_s1_owned_by_zkgui() == 0) return 0;
        usleep(50000);
    }
    errno = ETIMEDOUT; return -1;
}

static int wait_restored(void)
{
    uint64_t end = monotonic_ms() + WAIT_MS;
    while (monotonic_ms() < end) {
        pid_t pid = -1;
        if (count_zkgui(&pid) == 1 && tty_s1_owned_by_zkgui() == 1 && zkgui_owns_tty(pid)) return 0;
        usleep(50000);
    }
    errno = ETIMEDOUT; return -1;
}

static int make_listener(const char *path)
{
    struct sockaddr_un addr; int fd; size_t len = strlen(path);
    if (len >= sizeof(addr.sun_path)) { errno = ENAMETOOLONG; return -1; }
    fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK | SOCK_CLOEXEC, 0); if (fd < 0) return -1;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    if (path[0] == '@') {
        addr.sun_path[0] = '\0';
        memcpy(addr.sun_path + 1, path + 1, len - 1);
    } else {
        memcpy(addr.sun_path, path, len + 1);
    }
    if (bind(fd, (struct sockaddr *)&addr,
             offsetof(struct sockaddr_un, sun_path) + len +
                 (path[0] == '@' ? 0 : 1))) {
        int e = errno;
        close(fd);
        errno = e;
        return -1;
    }
    if ((path[0] != '@' && chmod(path, 0600)) || listen(fd, 4)) {
        int e = errno;
        close(fd);
        if (path[0] != '@') unlink(path);
        errno = e;
        return -1;
    }
    return fd;
}

static int make_host_listener(uint16_t port)
{
    struct sockaddr_in addr;
    int fd;
    int enabled = 1;
    fd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0)
        return -1;
    (void)setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &enabled, sizeof(enabled));
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons(port);
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) ||
        listen(fd, 1)) {
        int saved = errno;
        close(fd);
        errno = saved;
        return -1;
    }
    return fd;
}

static void video_socket_buffers(int fd)
{
    int size = 65576, send_size = -1, receive_size = -1, nodelay = 1;
    socklen_t n = sizeof(int);
    (void)setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));
    (void)setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &size, sizeof(size));
    (void)setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &size, sizeof(size));
    (void)getsockopt(fd, SOL_SOCKET, SO_SNDBUF, &send_size, &n);
    n = sizeof(int);
    (void)getsockopt(fd, SOL_SOCKET, SO_RCVBUF, &receive_size, &n);
    char message[128];
    int length = snprintf(message, sizeof(message), "video socket buffers send=%d receive=%d\n", send_size, receive_size);
    if (length <= 0 || (size_t)length >= sizeof(message)) return;
    /* Linux proc reopens the sink with an independent file description, so
     * nonblocking telemetry never races another process restoring stderr flags.
     * Unavailable/unsupported/full diagnostic sinks simply lose this record. */
    int diagnostic = open("/proc/self/fd/2", O_WRONLY | O_NONBLOCK | O_CLOEXEC | O_APPEND);
    if (diagnostic < 0) return;
    (void)write(diagnostic, message, (size_t)length);
    close(diagnostic);
}

static int video_open(struct video *previous, const struct packet *packet)
{
    struct video candidate;
    struct video *v = &candidate;
    int pair[2] = {-1, -1}, random_fd = -1;
    struct sockaddr_in address;
    socklen_t address_size = sizeof(address);
    sigset_t blocked, original;
    video_reset(v);
    v->fps_n = d200_vs_get_u32(packet->data + 32); v->fps_d = d200_vs_get_u32(packet->data + 36);
    memcpy(v->session, packet->data + 8, 16);
    random_fd = open("/dev/urandom", O_RDONLY | O_CLOEXEC | O_NONBLOCK);
    if (random_fd < 0) goto failed;
    ssize_t random_bytes = read(random_fd, v->capability, sizeof(v->capability));
    video_close_fd(&random_fd);
    if (random_bytes != (ssize_t)sizeof(v->capability)) goto failed;
    v->listener = make_host_listener(0);
    if (v->listener < 0 || fcntl(v->listener, F_SETFL, O_NONBLOCK) ||
        getsockname(v->listener, (struct sockaddr *)&address, &address_size) ||
        socketpair(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0, pair)) goto failed;
    v->port = ntohs(address.sin_port);
    for (int i = 0; i < 2; i++) {
        video_socket_buffers(pair[i]);
    }
    sigemptyset(&blocked); sigaddset(&blocked, SIGTERM); sigaddset(&blocked, SIGUSR1);
    if (sigprocmask(SIG_BLOCK, &blocked, &original)) goto failed;
    v->pid = fork();
    if (!v->pid) {
        char session[33], n[16], d[16];
        for (unsigned i = 0; i < 16; i++) snprintf(session + i * 2, 3, "%02x", v->session[i]);
        snprintf(n, sizeof(n), "%u", v->fps_n); snprintf(d, sizeof(d), "%u", v->fps_d);
        if (dup2(pair[1], 3) < 0 || fcntl(3, F_SETFD, 0)) _exit(127);
        long limit = sysconf(_SC_OPEN_MAX);
        if (limit < 4) _exit(127);
        for (long fd = 4; fd < limit; fd++) close((int)fd);
        int nullfd = open("/dev/null", O_RDWR);
        /* Keep the proxy's diagnostic stderr, never the D2PX stdout socket. */
        if (nullfd < 0 || dup2(nullfd, 0) < 0 || dup2(nullfd, 1) < 0) _exit(127);
        if (nullfd > 3) close(nullfd);
        unsetenv("LD_PRELOAD"); unsetenv("D200_HIDG0_SOCKET"); unsetenv("D200_HIDG1_SOCKET");
        execl("/tmp/d200-color-agent", "/tmp/d200-color-agent", "--stream", session, "1", n, d, "3", (char *)NULL);
        _exit(127);
    }
    (void)sigprocmask(SIG_SETMASK, &original, NULL);
    if (v->pid < 0) goto failed;
    close(pair[1]); v->child_fd = pair[0];
    v->exists = true; v->state = D200_VS_OPENING;
    v->opened = v->progress = monotonic_ms();
    d200_vs_state_init(&v->external, v->session, v->fps_n, v->fps_d, v->capability);
    d200_vs_state_init(&v->internal, v->session, v->fps_n, v->fps_d, NULL);
    video_close_fd(&previous->listener); video_close_fd(&previous->data_fd); video_close_fd(&previous->child_fd);
    *previous = *v;
    return 0;
failed:
    video_close_fd(&random_fd); video_close_fd(&pair[0]); video_close_fd(&pair[1]);
    video_close_fd(&v->listener); video_reset(v);
    return -1;
}

static int child_alive(struct state *s)
{
    int status; pid_t r;
    if (s->child <= 0) return 0;
    do r = waitpid(s->child, &status, WNOHANG); while (r < 0 && errno == EINTR);
    if (r == 0) return 1;
    if (r == s->child) {
        s->stock_wait_status = status; s->stock_wait_known = true;
        lifecycle_emit(s, "stock-reaped");
        fprintf(stderr, "stock zkgui exited: pid=%ld status=%d signal=%d\n", (long)r,
                WIFEXITED(status) ? WEXITSTATUS(status) : -1,
                WIFSIGNALED(status) ? WTERMSIG(status) : 0);
    } else
        fprintf(stderr, "stock zkgui wait failed: errno=%d\n", errno);
    s->child = -1; return 0;
}

/* Called only in the forked zkgui child before redirecting its unbounded stderr
 * to /dev/null. Only the preload's typed records use this independent sink.
 * No parent-owned fd is retargeted; preload consumes the private handoff and
 * reapplies CLOEXEC so unrelated helper executions cannot inherit it. */
static int prepare_usb_diagnostic(void)
{
    int saved = errno;
    int fd = -1;
    char descriptor[16];
    if (unsetenv("D200_USB_DIAGNOSTIC_FD")) goto done;
    fd = open("/proc/self/fd/2", O_WRONLY | O_NONBLOCK | O_CLOEXEC | O_APPEND);
    if (fd < 0) goto done;
    if (fd <= STDERR_FILENO || fcntl(fd, F_SETFD, 0)) goto failed;
    (void)snprintf(descriptor, sizeof(descriptor), "%d", fd);
    if (setenv("D200_USB_DIAGNOSTIC_FD", descriptor, 1)) goto failed;
    goto done;
failed:
    (void)close(fd);
    fd = -1;
done:
    errno = saved;
    return fd;
}

static int launch_child(struct state *s)
{
    pid_t pid = fork();
    if (pid < 0) return -1;
    if (pid == 0) {
        int nullfd = open("/dev/null", O_RDWR);
        (void)prepare_usb_diagnostic();
        unsetenv("LD_PRELOAD"); unsetenv("D200_HIDG0_SOCKET"); unsetenv("D200_HIDG1_SOCKET");
        if (setenv("LD_PRELOAD", s->preload, 1) ||
            setenv("D200_HIDG0_SOCKET", s->sockpath[0], 1) ||
            setenv("D200_HIDG1_SOCKET", s->sockpath[1], 1) ||
            setenv("D200_VIDEO_UNDER_STUDIO", "1", 1) ||
            setenv("TSLIB_TSDEVICE", "/dev/input/event0", 1) ||
            setenv("TSLIB_CONFFILE", "/etc/ts.conf", 1) ||
            setenv("TSLIB_CONSOLEDEVICE", "none", 1) ||
            setenv("TSLIB_FBDEVICE", "/dev/fb0", 1) ||
            setenv("TSLIB_PLUGINDIR", "/lib/ts/", 1) ||
            setenv("TSLIB_CALIBFILE", "/data/pointercal", 1) ||
            setenv("PATH", "/sbin:/bin:/tmp:", 1) ||
            setenv("LD_LIBRARY_PATH", "/tmp:/res/lib:/lib", 1) ||
            setenv("ANDROID_ROOT", "/", 1) ||
            setenv("ANDROID_DATA", "/data", 1) || chdir("/") || nullfd < 0)
            _exit(127);
        if (dup2(nullfd, 0) < 0 || dup2(nullfd, 1) < 0 || dup2(nullfd, 2) < 0) _exit(127);
        if (nullfd > 2) close(nullfd);
        execl("/bin/zkgui", "/bin/zkgui", (char *)NULL); _exit(127);
    }
    s->child = pid; return 0;
}

static int accept_peer(struct state *s, int i)
{
    int fd = accept4(s->listener[i], NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
    if (fd < 0) return errno == EAGAIN || errno == EWOULDBLOCK ? 0 : -1;
    if (s->peer[i] >= 0) { close(fd); errno = EBUSY; return -1; }
    s->peer[i] = fd; return 1;
}

static int send_to_peer(struct state *s, int i, const struct packet *p)
{
    ssize_t n;
    if (!s->hello) { errno = EPROTO; return -1; }
    if (s->pending[i].used) { errno = ENOBUFS; return -1; }
    if (s->peer[i] < 0) { if (s->pending[i].used) { errno = ENOBUFS; return -1; } s->pending[i].used = true; s->pending[i].packet = *p; return 0; }
    n = send(s->peer[i], p->data, p->length, MSG_NOSIGNAL | MSG_DONTWAIT);
    if (n == (ssize_t)p->length) return send_output_ack(s, p->sequence);
    if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) && !s->pending[i].used) { s->pending[i].used = true; s->pending[i].packet = *p; return 0; }
    if (n >= 0) errno = EPROTO;
    return -1;
}

static int flush_pending(struct state *s, int i)
{
    ssize_t n;
    if (!s->hello) return 0;
    if (!s->pending[i].used) return 0;
    if (s->peer[i] < 0) return 0;
    n = send(s->peer[i], s->pending[i].packet.data, s->pending[i].packet.length,
             MSG_NOSIGNAL | MSG_DONTWAIT);
    if (n == (ssize_t)s->pending[i].packet.length) {
        uint32_t sequence = s->pending[i].packet.sequence;
        s->pending[i].used = false;
        return send_output_ack(s, sequence);
    }
    if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return 0;
    if (n >= 0) errno = EPROTO;
    return -1;
}

static int receive_from_peer(struct state *s, int i)
{
    struct msghdr msg; struct iovec iov; unsigned char data[MAX_PAYLOAD]; ssize_t n;
    if (!s->hello) return 0;
    memset(&msg, 0, sizeof(msg)); iov.iov_base = data; iov.iov_len = sizeof(data); msg.msg_iov = &iov; msg.msg_iovlen = 1;
    n = recvmsg(s->peer[i], &msg, MSG_DONTWAIT);
    if (n == 0) { close(s->peer[i]); s->peer[i] = -1; return 0; }
    if (n < 0) return (errno == EAGAIN || errno == EWOULDBLOCK) ? 0 : -1;
    if (msg.msg_flags & MSG_TRUNC) { errno = EMSGSIZE; return -1; }
    return send_packet(s, i == 0 ? INPUT0 : INPUT1, data, (uint32_t)n);
}

static void close_session(struct state *s)
{
    int i;
    video_close_fd(&s->video.listener);
    video_close_fd(&s->video.data_fd);
    video_close_fd(&s->video.child_fd);
    for (i = 0; i < 2; ++i) {
        if (s->peer[i] >= 0) close(s->peer[i]);
        if (s->listener[i] >= 0) close(s->listener[i]);
        if (s->listener_owned[i]) unlink(s->sockpath[i]);
    }
    if (s->lock_fd >= 0) close(s->lock_fd);
    if (s->session[0]) {
        char path[PATH_MAX];
        if (s->lock_owned && snprintf(path, sizeof(path), "%s/proxy.lock", s->session) < (int)sizeof(path))
            unlink(path);
        if (s->preload[0]) unlink(s->preload);
        if (snprintf(path, sizeof(path), "%s/proxy", s->session) < (int)sizeof(path))
            unlink(path);
        rmdir(s->session);
    }
}

static int terminate_child(struct state *s)
{
    uint64_t end; int status;
    if (s->child <= 0) return 0;
    (void)kill(s->child, SIGTERM); end = monotonic_ms() + KILL_WAIT_MS;
    while (monotonic_ms() < end) { pid_t r = waitpid(s->child, &status, WNOHANG); if (r == s->child || (r < 0 && errno == ECHILD)) { if (r == s->child) { s->stock_wait_status = status; s->stock_wait_known = true; lifecycle_emit(s, "stock-reaped"); } s->child = -1; return 0; } usleep(50000); }
    (void)kill(s->child, SIGKILL);
    for (;;) {
        pid_t r = waitpid(s->child, &status, 0);
        if (r == s->child) {
            s->stock_wait_status = status; s->stock_wait_known = true;
            lifecycle_emit(s, "stock-reaped");
        }
        if (r == s->child || (r < 0 && errno == ECHILD))
            break;
        if (r < 0 && errno == EINTR)
            continue;
        return -1;
    }
    s->child = -1; return 0;
}

static int restore(struct state *s)
{
    int rc;
    lifecycle_emit(s, "restore-begin");
    video_fail(&s->video, D200_VS_RESULT_CANCELLED);
    /* Whole-authority shutdown is separate from normal Studio polling. */
    uint64_t end = monotonic_ms() + 5000;
    while (s->video.exists && !s->video.checked && monotonic_ms() < end) {
        video_tick(s);
        lifecycle_agent_wait(s);
        usleep(10000);
    }
    rc = terminate_child(s);
    if (s->video.exists && !s->video.checked) rc = -1;
    close_session(s);
    if (s->stdout_ok) (void)send_packet(s, RESTORING, NULL, 0);
    if (run_service("setprop ctl.start zkswe") || wait_restored()) { lifecycle_emit(s, "restore-failed"); return -1; }
    if (s->stdout_ok) (void)send_packet(s, RESTORED, NULL, 0);
    end = monotonic_ms() + WAIT_MS;
    while (s->stdout_ok && s->output_count && monotonic_ms() < end) {
        if (control_flush(s)) break;
        usleep(10000);
    }
    lifecycle_emit(s, rc ? "restore-failed" : "restore-complete");
    return rc;
}

int main(int argc, char **argv)
{
    struct state s; struct stat st; struct packet packet; bool ready = false, session_started = false, host_connected = true; int i, exit_code = 1;
    int host_listener[2] = {-1, -1}, host_peer = -1;
    uint64_t reconnect_deadline = 0;
    unsigned char host_capability[CAPABILITY_MAX];
    uint32_t host_capability_length = 0;
    unsigned long host_port = argc > 2 ? strtoul(argv[2], NULL, 10) : 0;
    memset(&s, 0, sizeof(s)); s.lock_fd = s.listener[0] = s.listener[1] = s.peer[0] = s.peer[1] = -1; video_reset(&s.video); s.child = -1; s.stdout_ok = true;
    if (argc != 7 || strcmp(argv[1], "--host-port") || !host_port || host_port > 65535 ||
        strcmp(argv[3], "--session-dir") || strcmp(argv[5], "--preload") ||
        !safe_tmp_absolute(argv[4]) || !safe_tmp_absolute(argv[6]) ||
        strlen(argv[6]) < 4 || strcmp(argv[6] + strlen(argv[6]) - 3, ".so")) {
        fprintf(stderr, "proxy startup: invalid arguments argc=%d", argc);
        for (int argument = 0; argument < argc; argument++)
            fprintf(stderr, " argv%d=[%s]", argument, argv[argument]);
        fputc('\n', stderr);
        return 2;
    }
    if (strlen(argv[4]) >= sizeof(s.session) || strlen(argv[6]) >= sizeof(s.preload) ||
        lstat(argv[4], &st) || !S_ISDIR(st.st_mode) ||
        lstat(argv[6], &st) || !S_ISREG(st.st_mode)) {
        fprintf(stderr, "proxy startup: invalid staged files errno=%d\n", errno);
        return 2;
    }
    strcpy(s.session, argv[4]); strcpy(s.preload, argv[6]);
    const char *session_name = strrchr(s.session, '/');
    session_name = session_name ? session_name + 1 : s.session;
    if (snprintf(s.sockpath[0], sizeof(s.sockpath[0]),
                 "@d200-zkgui-hidg0-%s", session_name) >=
            (int)sizeof(s.sockpath[0]) ||
        snprintf(s.sockpath[1], sizeof(s.sockpath[1]),
                 "@d200-zkgui-hidg1-%s", session_name) >=
            (int)sizeof(s.sockpath[1]))
        return 2;
    { char lock[PATH_MAX]; if (snprintf(lock, sizeof(lock), "%s/proxy.lock", s.session) >= (int)sizeof(lock)) return 2; s.lock_fd = open(lock, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600); }
    if (s.lock_fd < 0) { fprintf(stderr, "proxy startup: lock errno=%d\n", errno); return 2; }
    s.lock_owned = true;
    if (fchmod(s.lock_fd, 0600)) { fprintf(stderr, "proxy startup: lock chmod errno=%d\n", errno); close_session(&s); return 2; }
    s.listener[0] = make_listener(s.sockpath[0]); s.listener[1] = make_listener(s.sockpath[1]);
    if (s.listener[0] < 0 || s.listener[1] < 0) { fprintf(stderr, "proxy startup: HID listener errno=%d\n", errno); close_session(&s); return 2; }
    signal(SIGINT, signal_handler); signal(SIGTERM, signal_handler); signal(SIGHUP, signal_handler); signal(SIGPIPE, SIG_IGN);
    host_listener[0] = make_host_listener((uint16_t)host_port);
    host_listener[1] = make_host_listener((uint16_t)(host_port + 1));
    if (host_listener[0] < 0 || host_listener[1] < 0) { fprintf(stderr, "proxy startup: host listener errno=%d\n", errno); close_session(&s); return 2; }
    for (i = 0; i < 2; i++) {
        int flags = fcntl(host_listener[i], F_GETFL, 0);
        if (flags < 0 || fcntl(host_listener[i], F_SETFL, flags | O_NONBLOCK)) {
            close(host_listener[0]);
            close(host_listener[1]);
            close_session(&s);
            return 2;
        }
    }
    while (!stop_requested) {
        for (i = 0; i < 2 && host_peer < 0; i++)
            host_peer = accept(host_listener[i], NULL, NULL);
        if (host_peer >= 0)
            break;
        if (errno != EINTR && errno != EAGAIN && errno != EWOULDBLOCK)
            break;
        usleep(10000);
    }
    if (stop_requested) {
        close(host_listener[0]);
        close(host_listener[1]);
        close_session(&s);
        return 0;
    }
    if (host_peer < 0 || dup2(host_peer, STDIN_FILENO) < 0 ||
        dup2(host_peer, STDOUT_FILENO) < 0) {
        fprintf(stderr, "proxy startup: host accept or dup errno=%d\n", errno);
        if (host_peer >= 0) close(host_peer);
        close_session(&s);
        return 2;
    }
    if (host_peer > STDOUT_FILENO) close(host_peer);
    if (configure_raw_terminal() || send_packet(&s, BOOTSTRAP, NULL, 0)) {
        fprintf(stderr, "proxy startup: bootstrap errno=%d\n", errno);
        restore_terminal();
        close_session(&s);
        return 2;
    }
    while (!stop_requested) {
        struct pollfd pfds[10]; int map[10], nfd = 0, r;
        if (host_connected) {
            pfds[nfd] = (struct pollfd){ .fd = STDIN_FILENO, .events = POLLIN | POLLHUP | (s.output_count ? POLLOUT : 0) };
            map[nfd++] = -1;
        }
        for (i = 0; i < 2; i++) {
            pfds[nfd] = (struct pollfd){ .fd = host_listener[i], .events = POLLIN };
            map[nfd++] = -2 - i;
        }
        for (i = 0; i < 2; ++i) { pfds[nfd] = (struct pollfd){ .fd = s.listener[i], .events = POLLIN }; map[nfd++] = 10 + i; }
        for (i = 0; i < 2; ++i) if (s.hello && s.peer[i] >= 0) { pfds[nfd] = (struct pollfd){ .fd = s.peer[i], .events = POLLIN | (s.pending[i].used ? POLLOUT : 0) }; map[nfd++] = i; }
        unsigned video_fds = video_pollfds(&s.video, pfds + nfd);
        for (unsigned index = 0; index < video_fds; index++) map[nfd++] = 100;
        r = poll(pfds, nfd, s.video.exists && !s.video.checked ? 10 : 100); if (r < 0) { if (errno == EINTR) continue; lifecycle_exit(&s, "poll-error"); break; }
        video_tick(&s);
        lifecycle_agent_wait(&s);
        int control_failed = host_connected ? control_flush(&s) : 0;
        if (host_connected && (control_failed ||
            (s.input.sent && monotonic_ms() - s.input.started >= WAIT_MS))) {
            lifecycle_control_loss(&s, control_failed ? "control-write" : "control-record-timeout");
            host_connected = false; connection_boundary(&s);
            close(STDIN_FILENO); close(STDOUT_FILENO);
            reconnect_deadline = monotonic_ms() + 30000;
            continue;
        }
        if (!host_connected && monotonic_ms() >= reconnect_deadline) {
            lifecycle_exit(&s, "reconnect-timeout");
            break;
        }
        if (s.hello && monotonic_ms() - s.last_rx_ms > HEARTBEAT_TIMEOUT_MS) {
            lifecycle_control_loss(&s, "heartbeat-timeout");
            host_connected = false;
            connection_boundary(&s);
            close(STDIN_FILENO);
            close(STDOUT_FILENO);
            reconnect_deadline = monotonic_ms() + 30000;
            continue;
        }
        if (s.hello && !child_alive(&s)) { lifecycle_exit(&s, "stock-exit-or-wait-error"); send_error(&s, "zkgui exited"); break; }
        for (i = 0; i < nfd && r > 0; ++i) if (pfds[i].revents) {
            if (map[i] == 100) {
                /* video_tick already serviced this readiness snapshot. */
                continue;
            } else if (map[i] <= -2) {
                int replacement = accept(host_listener[-2 - map[i]], NULL, NULL);
                if (replacement < 0) {
                    if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
                        lifecycle_exit(&s, "replacement-accept-error");
                        stop_requested = 1;
                    }
                    continue;
                }
                connection_boundary(&s);
                if (dup2(replacement, STDIN_FILENO) < 0 ||
                    dup2(replacement, STDOUT_FILENO) < 0) {
                    lifecycle_exit(&s, "replacement-configure-error");
                    close(replacement);
                    stop_requested = 1;
                    break;
                }
                if (replacement > STDOUT_FILENO)
                    close(replacement);
                s.rx_seq = 0;
                s.tx_seq = 0;
                s.stdout_ok = true;
                host_connected = true;
                s.last_rx_ms = monotonic_ms();
                if (send_packet(&s, BOOTSTRAP, NULL, 0)) {
                    lifecycle_exit(&s, "bootstrap-write");
                    stop_requested = 1;
                    break;
                }
                /* All remaining revents belong to the old connection snapshot. */
                break;
            } else if (map[i] == -1) {
                if (!(pfds[i].revents & (POLLIN | POLLHUP | POLLERR))) continue;
                int received = receive_packet(&s, &packet);
                if (received > 0) continue;
                if (received < 0) {
                    lifecycle_control_loss(&s, s.control_eof ? "control-eof" :
                        errno == ETIMEDOUT ? "control-record-timeout" :
                        errno == EPROTO ? "control-protocol" : "control-read");
                    host_connected = false;
                    connection_boundary(&s);
                    close(STDIN_FILENO);
                    close(STDOUT_FILENO);
                    reconnect_deadline = monotonic_ms() + 30000;
                    break;
                }
                if (!s.hello) {
                    if (packet.kind != HELLO || packet.length == 0 || packet.length > CAPABILITY_MAX) { lifecycle_exit(&s, "hello-invalid"); send_error(&s, "expected HELLO"); stop_requested = 1; break; }
                    if (session_started &&
                        (packet.length != host_capability_length ||
                         memcmp(packet.data, host_capability, packet.length))) {
                        lifecycle_exit(&s, "hello-auth");
                        send_error(&s, "replacement host capability mismatch");
                        stop_requested = 1;
                        break;
                    }
                    s.hello = true;
                    if (!session_started) {
                        memcpy(host_capability, packet.data, packet.length);
                        host_capability_length = packet.length;
                        if (run_service("setprop ctl.stop zkswe") ||
                            wait_no_zkgui_and_tty() || launch_child(&s)) {
                            lifecycle_exit(&s, "stock-launch");
                            send_error(&s, "unable to start shimmed zkgui");
                            stop_requested = 1;
                            break;
                        }
                        session_started = true;
                    } else if (ready && send_packet(&s, READY, NULL, 0)) {
                        lifecycle_exit(&s, "ready-write");
                        stop_requested = 1;
                        break;
                    }
                    if (!ready && s.peer[0] >= 0) {
                        if (send_packet(&s, READY, NULL, 0)) { lifecycle_exit(&s, "ready-write"); stop_requested = 1; }
                        ready = true;
                    }
                } else if (packet.kind == STOP && packet.length == 0) { lifecycle_exit(&s, "stop-request"); stop_requested = 1; break;
                } else if (packet.kind == PING && packet.length == 0) {
                    if (send_packet(&s, PONG, NULL, 0)) { lifecycle_exit(&s, "pong-write"); stop_requested = 1; }
                } else if (packet.kind == D200_VS_VIDEO_OPEN_REQUEST ||
                           packet.kind == D200_VS_VIDEO_CANCEL_REQUEST ||
                           packet.kind == D200_VS_VIDEO_STATUS_REQUEST) {
                    if (video_control(&s, &packet)) {
                        lifecycle_control_loss(&s, "video-control");
                        host_connected = false; connection_boundary(&s);
                        close(STDIN_FILENO); close(STDOUT_FILENO);
                        reconnect_deadline = monotonic_ms() + 30000;
                        break;
                    }
                } else if ((packet.kind == OUTPUT0 || packet.kind == OUTPUT1) && send_to_peer(&s, packet.kind == OUTPUT0 ? 0 : 1, &packet)) { lifecycle_exit(&s, "hid-output"); send_error(&s, "hid output failure"); stop_requested = 1; break;
                } else if (packet.kind != OUTPUT0 && packet.kind != OUTPUT1 &&
                           packet.kind != PING) { lifecycle_exit(&s, "packet-kind"); send_error(&s, "unexpected packet"); stop_requested = 1; break; }
            } else if (map[i] >= 10) {
                int which = map[i] - 10, a = accept_peer(&s, which); if (a < 0 || (a > 0 && flush_pending(&s, which))) { lifecycle_exit(&s, "hid-accept"); send_error(&s, "hid accept failure"); stop_requested = 1; break; }
                if (s.hello && which == 0 && s.peer[0] >= 0 && !ready) {
                    if (!child_alive(&s)) { lifecycle_exit(&s, "stock-exit-or-wait-error"); send_error(&s, "zkgui exited"); stop_requested = 1; break; }
                    if (send_packet(&s, READY, NULL, 0)) { lifecycle_exit(&s, "ready-write"); stop_requested = 1; }
                    ready = true;
                }
            } else { int which = map[i]; if ((pfds[i].revents & POLLOUT) && flush_pending(&s, which)) { lifecycle_exit(&s, "hid-send"); send_error(&s, "hid send failure"); stop_requested = 1; break; } if ((pfds[i].revents & (POLLIN | POLLHUP | POLLERR)) && receive_from_peer(&s, which)) { lifecycle_exit(&s, "hid-input"); send_error(&s, "hid input failure"); stop_requested = 1; break; } }
        }
    }
    if (stop_signo == SIGTERM) lifecycle_exit(&s, "signal-term");
    else if (stop_signo == SIGINT) lifecycle_exit(&s, "signal-int");
    else if (stop_signo == SIGHUP) lifecycle_exit(&s, "signal-hup");
    lifecycle_emit(&s, "loop-ended");
    close(host_listener[0]);
    close(host_listener[1]);
    if (!session_started) close_session(&s);
    else if (restore(&s)) {
        send_error(&s, "restoration failed");
        exit_code = 1;
        restore_terminal();
        return exit_code;
    }
    restore_terminal();
    return session_started ? 0 : 1;
}
