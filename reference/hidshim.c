// hidshim.c - virtual D200 hidapi client for the copied Studio bundle.
//
// Two halves, one of which is unbuildable here. `D200_HOST_VISUAL` selects a
// fixture transport whose header, "hidshim_host_transport.h", is not in this
// repository and is not defined or supplied by anything else in the tree, so
// `cc -DD200_HOST_VISUAL -fsyntax-only reference/hidshim.c` stops at that include
// and those regions (about 220 lines) are not covered by any check this repo
// runs. Only the production half is compiled -- the check in the brief is the
// plain `cc -fsyntax-only reference/hidshim.c` -- and production is
// authoritative on uncertainty semantics wherever the halves disagree. They do
// disagree at hid_close()/hid_error(): the production branch reports the
// `close_uncertain` flag set by the bounded active-call drain below, while the
// fixture branch reports `host.cleanup_uncertain` and has never been re-checked
// against it. Read the D200_HOST_VISUAL regions as unverified reference
// material, not as a second implementation with coverage. See finding B-124.
#ifndef D200_HOST_VISUAL
#include <dlfcn.h>
#endif
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#include <wchar.h>
#ifdef D200_HOST_VISUAL
#include "hidshim_host_transport.h"
#endif

typedef struct hid_device_ hid_device;
typedef enum {
    HID_API_BUS_UNKNOWN = 0,
    HID_API_BUS_USB = 1,
    HID_API_BUS_BLUETOOTH = 2,
    HID_API_BUS_I2C = 3,
    HID_API_BUS_SPI = 4
} hid_bus_type;
typedef struct hid_device_info {
    char *path;
    unsigned short vendor_id, product_id;
    wchar_t *serial_number;
    unsigned short release_number;
    wchar_t *manufacturer_string, *product_string;
    unsigned short usage_page, usage;
    int interface_number;
    struct hid_device_info *next;
    hid_bus_type bus_type;
} hid_device_info;

typedef struct hid_api_version {
    int major;
    int minor;
    int patch;
} hid_api_version;

#define VID 0x2207
#define PID 0x0019
#define REPORT_BYTES 1025
#ifndef D200_HOST_VISUAL
/* Overridable at compile time: the host model in tests/test_vendorbridge_shimlog.py
 * must point the shim at a socket that is provably absent, and the real deck's
 * socket is never probed by a test. The shipped build defines nothing, so the
 * default below is what Studio gets. */
#ifndef D200_HIDSHIM_SOCKET_PATH
#define D200_HIDSHIM_SOCKET_PATH "/tmp/d200-adb-bridge.sock"
#endif
#define SOCKET_PATH D200_HIDSHIM_SOCKET_PATH
/* FIX-5-T13: where an unreachable bridge is reported. This shim is loaded into
 * the Studio process, which is usually launched from Finder, so its stderr goes
 * nowhere a user can read; the record therefore goes to an append-only file.
 * Compile-time, not an environment lookup: Studio's environment is not a
 * trustworthy input for a path this process writes to (the same reason the
 * bridge's own socket is not read from the environment).
 *
 * The file is bounded twice over: at most D200_HIDSHIM_LOG_REASONS distinct
 * reasons per process, and never appended to once it reaches
 * D200_HIDSHIM_LOG_LIMIT bytes. Studio enumerates in a loop, so an undecorated
 * record would be unbounded by construction. */
#ifndef D200_HIDSHIM_LOG_PATH
#define D200_HIDSHIM_LOG_PATH "/tmp/d200-hidshim.log"
#endif
#define D200_HIDSHIM_LOG_LIMIT 4096
#define D200_HIDSHIM_LOG_REASONS 8
/* The socket path may be as long as a unix sockaddr_un allows (about 104 bytes);
 * the bound below leaves room for that plus the record's fixed text, and
 * D200_HIDSHIM_LOG_CHARS is the whole line's capacity with margin. */
#define D200_HIDSHIM_SOCKET_CHARS 256
#define D200_HIDSHIM_LOG_CHARS 640
/* One wording for both this record and the `play` refusal FIX-1-T19 added. */
#define D200_HIDSHIM_HINT "the hidshim bridge is not running; run `ghostdeck studio` first"
/* The record must always fit: a diagnostic that is dropped because it did not
 * fit its own buffer is the silent failure this file exists to remove. The
 * fixed text, a pid, a millisecond clock, the errno, the reason class and the
 * hint come to at most 320 bytes; the socket path is bounded separately. */
#if D200_HIDSHIM_LOG_CHARS - D200_HIDSHIM_SOCKET_CHARS < 320
#error "D200_HIDSHIM_LOG_CHARS must hold the whole record with the longest bounded socket path"
#endif
#endif
#define VPATH0 "d200-adb://2207:0019/interface/0"
#define VPATH1 "d200-adb://2207:0019/interface/1"
#define MAGIC 0x44323030U
/* Host-side budget for one production transport request. The host-visual build
 * has its own per-call deadlines (hidshim_host_transport.h) and ignores this
 * value; the wire protocol's own timeoutMs field is unaffected by it. */
#define D200_RPC_BUDGET_MS 15000

typedef struct virtual_device {
    uint32_t magic;
    uint64_t handle;
    char capability[65];
    int interface_number;
    int nonblocking;
    int closing;
    /* Set when hid_close() could not prove that every in-flight call returned. */
    int close_uncertain;
    unsigned active_calls;
    hid_device_info *info;
    pthread_mutex_t mutex;
    pthread_mutex_t read_mutex;
    pthread_cond_t condition;
#ifdef D200_HOST_VISUAL
    d200_host_state host;
    struct virtual_device *host_next;
#endif
} virtual_device;

#ifndef D200_HOST_VISUAL
static void *real_hidapi;
#endif
static pthread_mutex_t handle_lock = PTHREAD_MUTEX_INITIALIZER;
static uint64_t next_handle = 1;
#ifdef D200_HOST_VISUAL
static virtual_device *host_devices;
#else
static int daemon_seen;
static int daemon_waited;

static void free_info_node(hid_device_info *node)
{
    if (!node) return;
    free(node->path);
    free(node->serial_number);
    free(node->manufacturer_string);
    free(node->product_string);
    free(node);
}

static hid_device_info *without_d200(hid_device_info *list)
{
    hid_device_info *head = NULL, **tail = &head;
    while (list) {
        hid_device_info *next = list->next;
        list->next = NULL;
        if (list->vendor_id == VID && list->product_id == PID)
            free_info_node(list);
        else {
            *tail = list;
            tail = &list->next;
        }
        list = next;
    }
    return head;
}


static void *sym(const char *name)
{
    if (!real_hidapi)
        real_hidapi = dlopen("libhidapi.0.real.dylib", RTLD_NOW | RTLD_LOCAL);
    return real_hidapi ? dlsym(real_hidapi, name) : NULL;
}
#endif

static int is_virtual(hid_device *device)
{
#ifdef D200_HOST_VISUAL
    int found = 0;
    pthread_mutex_lock(&handle_lock);
    for (virtual_device *current = host_devices; current; current = current->host_next)
        if ((hid_device *)current == device) { found = 1; break; }
    pthread_mutex_unlock(&handle_lock);
    return found;
#else
    return device && ((virtual_device *)device)->magic == MAGIC;
#endif
}

#ifndef D200_HOST_VISUAL
/* One bounded policy for the whole production transport: every rpc() call gets an
 * explicit host-side budget (D200_RPC_BUDGET_MS for control requests, and the
 * peer's own timeoutMs plus D200_RPC_SLACK_MS for reads), and connect completion,
 * the request write and the reply read are all driven by poll() against that
 * single absolute deadline, so no wait inside rpc() can outlive it. */
#define D200_RPC_SLACK_MS 1000

/* A deadline expiry is not an I/O failure: distinct status so every entry point
 * can report ETIMEDOUT instead of a generic transport error. */
#define D200_TRANSPORT_TIMEOUT (-2)

static int64_t monotonic_ms(void)
{
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

/* Host-side budget for a request whose peer-side timeoutMs is `timeout_ms`.
 * A negative peer timeout (blocking hid_read) is still bounded here: no call may
 * request an unbounded wait. */
static int rpc_budget_ms(int timeout_ms)
{
    if (timeout_ms < 0)
        return D200_RPC_BUDGET_MS;
    if (timeout_ms > INT_MAX - D200_RPC_SLACK_MS)
        return INT_MAX;
    return timeout_ms + D200_RPC_SLACK_MS;
}

/* Wait for `events` on a non-blocking descriptor, never past `deadline`.
 * 0 ready, D200_TRANSPORT_TIMEOUT expired, -1 error with errno set. */
static int wait_ready(int fd, short events, int64_t deadline)
{
    for (;;) {
        struct pollfd entry;
        int64_t remaining = deadline - monotonic_ms();
        int ready;
        if (remaining <= 0) {
            errno = ETIMEDOUT;
            return D200_TRANSPORT_TIMEOUT;
        }
        entry.fd = fd;
        entry.events = events;
        entry.revents = 0;
        ready = poll(&entry, 1, (int)(remaining > INT_MAX ? INT_MAX : remaining));
        if (ready > 0)
            return 0;
        if (ready == 0) {
            errno = ETIMEDOUT;
            return D200_TRANSPORT_TIMEOUT;
        }
        if (errno != EINTR)
            return -1;
    }
}

static int write_all(int fd, const char *buffer, size_t length, int64_t deadline)
{
    while (length) {
        ssize_t written;
        int waited = wait_ready(fd, POLLOUT, deadline);
        if (waited)
            return waited;
        written = send(fd, buffer, length, MSG_NOSIGNAL);
        if (written > 0) {
            buffer += written;
            length -= (size_t)written;
            continue;
        }
        if (written < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK))
            continue;
        return -1;
    }
    return 0;
}

/* 0 = one framed reply line, D200_TRANSPORT_TIMEOUT = deadline expired, -1 = error. */
static int read_frame(int fd, char *buffer, size_t capacity, int64_t deadline)
{
    size_t used = 0;
    while (used + 1 < capacity) {
        ssize_t received;
        int waited = wait_ready(fd, POLLIN, deadline);
        if (waited)
            return waited;
        received = recv(fd, buffer + used, 1, 0);
        if (received > 0) {
            if (buffer[used] == '\0')
                return -1;
            if (buffer[used++] == '\n') {
                buffer[used] = '\0';
                return 0;
            }
            continue;
        }
        if (received < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK))
            continue;
        return -1;
    }
    return -1;
}

/* Teardown budget for draining in-flight handle calls in hid_close(). The
 * bridge's own longest single control round trip is a 10 s output acknowledgement
 * and proxy readiness allows 15 s, so 15 s lets a legitimately in-flight call
 * still finish while a deck that vanished mid-call can no longer hang the caller
 * forever. The default pthread condition clock is CLOCK_REALTIME, so the drain
 * deadline is built on that clock. */
#define D200_CLOSE_DRAIN_MS 15000

static void close_drain_deadline(struct timespec *deadline)
{
    clock_gettime(CLOCK_REALTIME, deadline);
    deadline->tv_sec += D200_CLOSE_DRAIN_MS / 1000;
    deadline->tv_nsec += (long)(D200_CLOSE_DRAIN_MS % 1000) * 1000000L;
    if (deadline->tv_nsec >= 1000000000L) {
        deadline->tv_sec += 1;
        deadline->tv_nsec -= 1000000000L;
    }
}
#endif

/* No JSON library is linked into the copied shim. Bound grammar, decoded
 * storage, tokens and recursion independently; never publish a partial reply. */
#define JSON_BYTES 4096
#define JSON_NODES 512
#define JSON_DEPTH 32

typedef struct {
    char type;
    int parent;
    size_t offset, length;
} json_node;

typedef struct {
    const unsigned char *text;
    size_t length, position, used;
    unsigned count;
    unsigned char strings[JSON_BYTES];
    json_node nodes[JSON_NODES];
} json_parser;

static int json_hex(unsigned char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static void json_space(json_parser *p)
{
    while (p->position < p->length) {
        unsigned char c = p->text[p->position];
        if (c != ' ' && c != '\t' && c != '\r' && c != '\n') break;
        p->position++;
    }
}

static int json_take(json_parser *p, unsigned char c)
{
    if (p->position >= p->length || p->text[p->position] != c) return 0;
    p->position++;
    return 1;
}

static int json_u16(json_parser *p, uint32_t *value)
{
    unsigned i;
    *value = 0;
    for (i = 0; i < 4; i++) {
        int digit;
        if (p->position == p->length ||
            (digit = json_hex(p->text[p->position++])) < 0) return -1;
        *value = (*value << 4) | (unsigned)digit;
    }
    return 0;
}

static int json_string(json_parser *p, json_node *node)
{
    if (!json_take(p, '"')) return -1;
    node->offset = p->used;
    while (p->position < p->length) {
        uint32_t c = p->text[p->position++];
        if (c == '"') {
            node->length = p->used - node->offset;
            return 0;
        }
        if (c < 0x20) return -1;
        if (c == '\\') {
            if (p->position == p->length) return -1;
            c = p->text[p->position++];
            switch (c) {
            case '"': case '\\': case '/': break;
            case 'b': c = '\b'; break;
            case 'f': c = '\f'; break;
            case 'n': c = '\n'; break;
            case 'r': c = '\r'; break;
            case 't': c = '\t'; break;
            case 'u':
                if (json_u16(p, &c)) return -1;
                if (c >= 0xd800 && c <= 0xdbff) {
                    uint32_t low;
                    if (!json_take(p, '\\') || !json_take(p, 'u') ||
                        json_u16(p, &low) || low < 0xdc00 || low > 0xdfff) return -1;
                    c = 0x10000 + ((c - 0xd800) << 10) + low - 0xdc00;
                } else if (c >= 0xdc00 && c <= 0xdfff) return -1;
                break;
            default: return -1;
            }
        } else if (c >= 0x80) {
            unsigned extra, i;
            uint32_t minimum;
            if (c >= 0xc2 && c <= 0xdf) { extra = 1; minimum = 0x80; c &= 0x1f; }
            else if (c >= 0xe0 && c <= 0xef) { extra = 2; minimum = 0x800; c &= 0x0f; }
            else if (c >= 0xf0 && c <= 0xf4) { extra = 3; minimum = 0x10000; c &= 7; }
            else return -1;
            for (i = 0; i < extra; i++) {
                unsigned char next;
                if (p->position == p->length) return -1;
                next = p->text[p->position++];
                if ((next & 0xc0) != 0x80) return -1;
                c = (c << 6) | (next & 0x3f);
            }
            if (c < minimum || c > 0x10ffff || (c >= 0xd800 && c <= 0xdfff)) return -1;
        }
        if (p->used + 4 > sizeof(p->strings)) return -1;
        if (c < 0x80) p->strings[p->used++] = (unsigned char)c;
        else {
            if (c >= 0x10000) p->strings[p->used++] = (unsigned char)(0xf0 | (c >> 18));
            else if (c >= 0x800) p->strings[p->used++] = (unsigned char)(0xe0 | (c >> 12));
            else p->strings[p->used++] = (unsigned char)(0xc0 | (c >> 6));
            if (c >= 0x10000) p->strings[p->used++] = (unsigned char)(0x80 | ((c >> 12) & 63));
            if (c >= 0x800) p->strings[p->used++] = (unsigned char)(0x80 | ((c >> 6) & 63));
            p->strings[p->used++] = (unsigned char)(0x80 | (c & 63));
        }
    }
    return -1;
}

static int json_number(json_parser *p, json_node *node)
{
    size_t start = p->position;
    (void)json_take(p, '-');
    if (!json_take(p, '0')) {
        if (p->position == p->length || p->text[p->position] < '1' ||
            p->text[p->position] > '9') return -1;
        do { p->position++; }
        while (p->position < p->length && p->text[p->position] >= '0' && p->text[p->position] <= '9');
    }
    if (json_take(p, '.')) {
        size_t digits = p->position;
        while (p->position < p->length && p->text[p->position] >= '0' && p->text[p->position] <= '9')
            p->position++;
        if (digits == p->position) return -1;
    }
    if (json_take(p, 'e') || json_take(p, 'E')) {
        size_t digits;
        if (!json_take(p, '+')) (void)json_take(p, '-');
        digits = p->position;
        while (p->position < p->length && p->text[p->position] >= '0' && p->text[p->position] <= '9')
            p->position++;
        if (digits == p->position) return -1;
    }
    node->offset = start;
    node->length = p->position - start;
    node->type = 'n';
    return 0;
}

static int json_value(json_parser *p, int parent, unsigned depth)
{
    unsigned index;
    json_node *node;
    unsigned char c;
    if (depth > JSON_DEPTH || p->count == JSON_NODES) return -1;
    json_space(p);
    if (p->position == p->length) return -1;
    index = p->count++;
    node = &p->nodes[index];
    node->parent = parent;
    c = p->text[p->position];
    if (c == '"') {
        node->type = 's';
        return json_string(p, node);
    }
    if (c == '{' || c == '[') {
        unsigned char end = c == '{' ? '}' : ']';
        node->type = (char)c;
        p->position++;
        json_space(p);
        if (json_take(p, end)) return 0;
        for (;;) {
            if (c == '{') {
                unsigned key, previous;
                json_node *name;
                if (p->count == JSON_NODES) return -1;
                key = p->count++;
                name = &p->nodes[key];
                name->type = 'k';
                name->parent = (int)index;
                if (json_string(p, name)) return -1;
                for (previous = index + 1; previous < key; previous++) {
                    json_node *other = &p->nodes[previous];
                    if (other->type == 'k' && other->parent == (int)index &&
                        other->length == name->length &&
                        !memcmp(p->strings + other->offset, p->strings + name->offset, name->length))
                        return -1;
                }
                json_space(p);
                if (!json_take(p, ':')) return -1;
            }
            if (json_value(p, (int)index, depth + 1)) return -1;
            json_space(p);
            if (json_take(p, end)) return 0;
            if (!json_take(p, ',')) return -1;
            json_space(p);
        }
    }
    if (c == 't' || c == 'f' || c == 'n') {
        const char *literal = c == 't' ? "true" : c == 'f' ? "false" : "null";
        size_t length = strlen(literal);
        if (p->length - p->position < length || memcmp(p->text + p->position, literal, length)) return -1;
        p->position += length;
        node->type = c == 'n' ? 'z' : (char)c;
        return 0;
    }
    return json_number(p, node);
}

static int json_key(json_parser *p, const json_node *node, const char *name)
{
    return node->length == strlen(name) && !memcmp(p->strings + node->offset, name, node->length);
}

static int parse_reply(const char *answer, size_t length, const char *operation,
                       unsigned char *output, size_t *output_length,
                       char opened_capability[65])
{
    /* Both bridge implementations emit integer schemaVersion 1. Unknown
     * metadata is grammar-checked, not searched for protocol field names. */
    json_parser p = {0};
    const json_node *report = NULL, *capability = NULL;
    int accepted = 0, schema = 0;
    unsigned i;
    if (!answer || !operation || length == 0 || length >= JSON_BYTES ||
        (output && !output_length) ||
        (output_length && *output_length && !output)) return -1;
    p.text = (const unsigned char *)answer;
    p.length = length;
    if (json_value(&p, -1, 0)) return -1;
    json_space(&p);
    if (p.position != length || p.nodes[0].type != '{') return -1;
    for (i = 1; i < p.count; i++) {
        const json_node *key = &p.nodes[i], *value;
        if (key->type != 'k' || key->parent != 0) continue;
        value = &p.nodes[i + 1];
        if (json_key(&p, key, "accepted")) {
            if (value->type != 't') return -1;
            accepted = 1;
        } else if (json_key(&p, key, "schemaVersion")) {
            if (value->type != 'n' || value->length != 1 || p.text[value->offset] != '1') return -1;
            schema = 1;
        } else if (json_key(&p, key, "capability")) {
            if (value->type != 's' || value->length != 64) return -1;
            capability = value;
        } else if (json_key(&p, key, "report")) {
            if (value->type != 's' || value->length % 2 || value->length > REPORT_BYTES * 2) return -1;
            report = value;
        }
    }
    if (!accepted || !schema || (!strcmp(operation, "open") && !capability) ||
        (!strcmp(operation, "input") && !report) ||
        (opened_capability && !capability) || (output_length && !report)) return -1;
    if (capability)
        for (i = 0; i < capability->length; i++)
            if (json_hex(p.strings[capability->offset + i]) < 0) return -1;
    if (report) {
        for (i = 0; i < report->length; i++)
            if (json_hex(p.strings[report->offset + i]) < 0) return -1;
        if (output_length && report->length / 2 > *output_length) return -1;
    }
    if (opened_capability) {
        memcpy(opened_capability, p.strings + capability->offset, 64);
        opened_capability[64] = '\0';
    }
    if (output_length) {
        for (i = 0; i < report->length / 2; i++)
            output[i] = (unsigned char)((json_hex(p.strings[report->offset + 2 * i]) << 4) |
                                        json_hex(p.strings[report->offset + 2 * i + 1]));
        *output_length = report->length / 2;
    }
    return 0;
}

static int rpc(const char *operation, uint64_t handle, const char *capability,
               int interface_number, int timeout_ms, int budget_ms,
               const unsigned char *report, size_t report_length,
               unsigned char *output, size_t *output_length,
               char opened_capability[65]
#ifdef D200_HOST_VISUAL
               , d200_host_state *host, int64_t started
#endif
               )
{
#ifndef D200_HOST_VISUAL
    int fd = -1;
    int64_t deadline;
#else
    (void)budget_ms;
    d200_host_state transient;
    d200_host_call call;
    int temporary = host == NULL, begun;
    unsigned char staged_report[REPORT_BYTES];
    size_t staged_length = output_length ? *output_length : 0;
    char staged_capability[65];
#endif
    int result = -1;
    char message[4096];
    char answer[4096];
#ifndef D200_HOST_VISUAL
    struct sockaddr_un address;
#endif
    int length;
    size_t index;

    if (!operation || (report_length && (!report || report_length != REPORT_BYTES)) ||
        (output && !output_length) || (output_length && *output_length && !output))
        return -1;
#ifdef D200_HOST_VISUAL
    if (temporary) {
        if (d200_host_init(&transient)) return -1;
        host = &transient;
    }
    begun = d200_host_begin(host, &call, operation, timeout_ms, started);
    if (begun) {
        result = begun > 0 ? 0 : -1;
        if (begun > 0 && output_length) *output_length = 0;
        goto host_done;
    }
#else
    fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0)
        return -1;
    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    snprintf(address.sun_path, sizeof(address.sun_path), "%s", SOCKET_PATH);
    /* Non-blocking transport: the connect, the request write and the reply read
     * are each bounded by this one absolute deadline, so a peer that accepted
     * the connection and stopped answering cannot block the caller. */
    deadline = monotonic_ms() + budget_ms;
    if (fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK)) {
        /* Without non-blocking mode there is no deadline to enforce: fail closed. */
        goto done;
    }
    if (connect(fd, (struct sockaddr *)&address, sizeof(address))) {
        if (errno != EINPROGRESS && errno != EAGAIN)
            goto done;
        if (wait_ready(fd, POLLOUT, deadline))
            goto done;
        {
            int socket_error = 0;
            socklen_t error_length = sizeof(socket_error);
            if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &socket_error, &error_length))
                goto done;
            if (socket_error) {
                errno = socket_error;
                goto done;
            }
        }
    }
#endif
    length = snprintf(
        message, sizeof(message),
        "{\"schemaVersion\":1,\"op\":\"%s\",\"handle\":%llu,"
        "\"interface\":%d,\"timeoutMs\":%d%s%s%s,\"report\":\"",
        operation, (unsigned long long)handle, interface_number, timeout_ms,
        capability ? ",\"capability\":\"" : "",
        capability ? capability : "", capability ? "\"" : "");
    if (length < 0 || (size_t)length + report_length * 2 + 4 > sizeof(message))
        goto done;
    for (index = 0; index < report_length; index++)
        length += snprintf(message + length, sizeof(message) - (size_t)length,
                           "%02x", report[index]);
    length += snprintf(message + length, sizeof(message) - (size_t)length, "\"}\n");
#ifdef D200_HOST_VISUAL
    if (length < 0 || (size_t)length >= sizeof(message)) { errno = EINVAL; goto done; }
    result = d200_host_exchange(&call, message, (size_t)length, answer, sizeof(answer));
    if (result > 0) {
        staged_length = 0;
        result = 0;
    } else if (!result) {
        result = parse_reply(answer, strlen(answer), operation,
                             output ? staged_report : NULL, output_length ? &staged_length : NULL,
                             opened_capability ? staged_capability : NULL);
        if (result) errno = EPROTO;
        else if (d200_host_expired(call.deadline)) result = -1;
    }
done:
    result = d200_host_finish(&call, result);
    if (!result) {
        if (output_length) {
            if (staged_length) memcpy(output, staged_report, staged_length);
            *output_length = staged_length;
        }
        if (opened_capability) memcpy(opened_capability, staged_capability, sizeof(staged_capability));
    }
host_done:
    if (result) d200_host_error(host, errno);
    if (temporary) {
        int error = errno;
        d200_host_dispose(host);
        if (host->cleanup_uncertain) { result = -1; error = EIO; }
        pthread_cond_destroy(&host->changed);
        pthread_mutex_destroy(&host->lock);
        errno = error;
    }
#else
    if (length < 0 || (size_t)length >= sizeof(message) ||
        write_all(fd, message, (size_t)length, deadline) ||
        read_frame(fd, answer, sizeof(answer), deadline))
        goto done;
    /* read_frame rejects raw NUL, so strlen cannot conceal a malformed tail. */
    result = parse_reply(answer, strlen(answer), operation, output, output_length,
                         opened_capability);
done:
    /* errno is ETIMEDOUT whenever the budget expired, otherwise the I/O error. */
    if (fd >= 0)
        close(fd);
#endif
    return result;
}
#ifndef D200_HOST_VISUAL
/* FIX-5-T13: the shim used to fail silently. A D200 enumeration that cannot
 * reach the bridge falls through to the real hidapi and returns a list without
 * the deck, which is indistinguishable from "no deck is attached" -- the user's
 * "buttons don't show" with no way to tell why.
 *
 * One bounded, one-shot record per distinct errno per process, written through
 * a single non-blocking open/write on a regular file. Nothing here starts a
 * bridge, prompts, blocks or forwards anything to the deck, so the shim stays
 * passive inside a foreign process, and the real-hidapi fallthrough below is
 * untouched (a real HID deck must still work). No serial and no capability ever
 * enters the record: only the socket path, the errno and its class. */
static pthread_mutex_t diagnostic_lock = PTHREAD_MUTEX_INITIALIZER;
static int diagnostic_reasons[D200_HIDSHIM_LOG_REASONS];
static int diagnostic_reason_count;
/* errno of the last failed bridge probe in wait_for_daemon(), normalized to a
 * nonzero value so a path that left errno unset still names something. */
static int daemon_failure_errno;

/* Fixed vocabulary, so no locale-dependent strerror() text is published. */
static const char *bridge_error_class(int error)
{
    switch (error) {
    case ENOENT:
        return "absent";
    case ECONNREFUSED:
        return "refused";
    case ETIMEDOUT:
        return "timeout";
    case EPROTO:
        return "protocol";
    default:
        return "io";
    }
}

/* True when this reason was already recorded, or when the fixed vocabulary is
 * full. Caller holds diagnostic_lock. */
static int diagnostic_reason_seen(int error)
{
    int index;
    for (index = 0; index < diagnostic_reason_count; index++)
        if (diagnostic_reasons[index] == error) return 1;
    if (diagnostic_reason_count >= D200_HIDSHIM_LOG_REASONS) return 1;
    diagnostic_reasons[diagnostic_reason_count++] = error;
    return 0;
}

static void bridge_unreachable_diagnostic(const char *path, int error)
{
    int saved_errno = errno;
    if (path == NULL) path = D200_HIDSHIM_LOG_PATH;
    if (error <= 0) error = EIO;
    pthread_mutex_lock(&diagnostic_lock);
    if (diagnostic_reason_seen(error)) {
        pthread_mutex_unlock(&diagnostic_lock);
        errno = saved_errno;
        return;
    }
    /* O_NOFOLLOW: a symlink planted at a shared /tmp path is refused rather
     * than written through. O_CLOEXEC and 0600: the record is not a secret,
     * but nothing here should leak a descriptor or a world-readable file. */
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC | O_NOFOLLOW, 0600);
    struct stat status;
    if (fd >= 0 && fstat(fd, &status) == 0 && S_ISREG(status.st_mode) &&
        status.st_size < D200_HIDSHIM_LOG_LIMIT) {
        /* The path is bounded with a precision limit rather than left to the
         * format, so a long socket path cannot eat the rest of the record; the
         * `#if` above is what proves the whole line still fits. */
        char line[D200_HIDSHIM_LOG_CHARS];
        int length = snprintf(
            line, sizeof(line),
            "{\"event\":\"hidshimBridgeUnreachable\",\"pid\":%ld,"
            "\"clock\":\"host-monotonic\",\"monotonicMs\":%lld,"
            "\"socket\":\"%.*s\",\"errno\":%d,"
            "\"reason\":\"%s\",\"hint\":\"%s\"}\n",
            (long)getpid(), (long long)monotonic_ms(), D200_HIDSHIM_SOCKET_CHARS, SOCKET_PATH,
            error, bridge_error_class(error), D200_HIDSHIM_HINT);
        if (length > 0 && (size_t)length < sizeof(line)) (void)write(fd, line, (size_t)length);
    }
    if (fd >= 0) close(fd);
    pthread_mutex_unlock(&diagnostic_lock);
    errno = saved_errno;
}

static int wait_for_daemon(void)
{
    int attempt, limit = daemon_waited ? 3 : 25;
    daemon_waited = 1;
    for (attempt = 0; attempt < limit; attempt++) {
        if (rpc("event", 0, NULL, 0, -1, D200_RPC_BUDGET_MS, NULL, 0, NULL, NULL, NULL) == 0)
            return 0;
        daemon_failure_errno = errno ? errno : EIO;
        if (attempt + 1 < limit)
            usleep(200000);
    }
    return -1;
}
#endif

static int retain_virtual(virtual_device *device)
{
    int retained = 0;
    pthread_mutex_lock(&device->mutex);
    if (!device->closing) {
        device->active_calls++;
        retained = 1;
    }
    pthread_mutex_unlock(&device->mutex);
    return retained;
}

static void release_virtual(virtual_device *device)
{
    pthread_mutex_lock(&device->mutex);
    if (device->active_calls)
        device->active_calls--;
    if (device->closing && !device->active_calls)
        pthread_cond_signal(&device->condition);
    pthread_mutex_unlock(&device->mutex);
}

static hid_device *virtual_open(int interface_number)
{
    virtual_device *device;
#ifdef D200_HOST_VISUAL
    int64_t started = d200_host_now();
#endif
    int initialization_error;
    if (interface_number != 0 && interface_number != 1)
        return NULL;
    device = calloc(1, sizeof(*device));
    if (!device)
        return NULL;
#ifdef D200_HOST_VISUAL
    if (d200_host_init(&device->host)) { free(device); return NULL; }
#endif
    device->magic = MAGIC;
    device->interface_number = interface_number;
#ifdef D200_HOST_VISUAL
    initialization_error = pthread_mutex_init(&device->mutex, NULL);
    if (initialization_error) goto host_initialization_failed;
    initialization_error = pthread_mutex_init(&device->read_mutex, NULL);
    if (initialization_error) {
        pthread_mutex_destroy(&device->mutex);
        goto host_initialization_failed;
    }
    initialization_error = pthread_cond_init(&device->condition, NULL);
    if (initialization_error) {
        pthread_mutex_destroy(&device->read_mutex);
        pthread_mutex_destroy(&device->mutex);
        goto host_initialization_failed;
    }
#else
    initialization_error = pthread_mutex_init(&device->mutex, NULL);
    if (initialization_error) goto initialization_failed;
    initialization_error = pthread_mutex_init(&device->read_mutex, NULL);
    if (initialization_error) {
        pthread_mutex_destroy(&device->mutex);
        goto initialization_failed;
    }
    initialization_error = pthread_cond_init(&device->condition, NULL);
    if (initialization_error) {
        pthread_mutex_destroy(&device->read_mutex);
        pthread_mutex_destroy(&device->mutex);
        goto initialization_failed;
    }
#endif
    pthread_mutex_lock(&handle_lock);
    device->handle = next_handle++;
    pthread_mutex_unlock(&handle_lock);
    {
        int opened = rpc("open", device->handle, NULL, interface_number, -1,
                         D200_RPC_BUDGET_MS,
                         NULL, 0, NULL, NULL, device->capability
#ifdef D200_HOST_VISUAL
                         , &device->host, started
#endif
                         );
#ifndef D200_HOST_VISUAL
        int attempt;
        for (attempt = 0; attempt < 10 && opened; attempt++) {
            usleep(100000);
            opened = rpc("open", device->handle, NULL, interface_number, -1,
                         D200_RPC_BUDGET_MS,
                         NULL, 0, NULL, NULL, device->capability);
        }
#endif
        if (opened) {
#ifdef D200_HOST_VISUAL
            int error = errno;
            d200_host_dispose(&device->host);
            pthread_cond_destroy(&device->host.changed);
            pthread_mutex_destroy(&device->host.lock);
#endif
            pthread_cond_destroy(&device->condition);
            pthread_mutex_destroy(&device->read_mutex);
            pthread_mutex_destroy(&device->mutex);
            free(device);
#ifdef D200_HOST_VISUAL
            errno = error;
#endif
            return NULL;
        }
    }
#ifdef D200_HOST_VISUAL
    pthread_mutex_lock(&handle_lock);
    device->host_next = host_devices;
    host_devices = device;
    pthread_mutex_unlock(&handle_lock);
#else
    daemon_seen = 1;
#endif
    return (hid_device *)device;
#ifdef D200_HOST_VISUAL
host_initialization_failed:
    d200_host_dispose(&device->host);
    pthread_cond_destroy(&device->host.changed);
    pthread_mutex_destroy(&device->host.lock);
    free(device);
    d200_host_error(NULL, initialization_error);
    return NULL;
#else
initialization_failed:
    /* Each failure path above destroyed exactly what it had already created. */
    free(device);
    errno = initialization_error;
    return NULL;
#endif
}

static int virtual_read(hid_device *opaque, unsigned char *buffer,
                        size_t length, int timeout_ms
#ifdef D200_HOST_VISUAL
                        , int64_t started
#endif
                        )
{
    virtual_device *device = (virtual_device *)opaque;
    size_t received = length;
    int result;
#ifndef D200_HOST_VISUAL
    int transport_error = 0;
    /* Host-side transport budget; hid_read_timeout()'s own timeout still travels
     * to the peer unchanged in the wire `timeoutMs` field. */
    int budget_ms = rpc_budget_ms(timeout_ms);
#else
    /* The host-visual transport enforces its own per-call deadlines. */
    int budget_ms = 0;
#endif
    if (!opaque || (length && !buffer) || length > INT_MAX) {
        errno = EINVAL;
        return -1;
    }
    if (!retain_virtual(device)) {
        errno = ECANCELED;
        return -1;
    }
    /* A live zero-length read is a no-op; closed tombstones still fail. */
    if (!length) {
        release_virtual(device);
        return 0;
    }
#ifndef D200_HOST_VISUAL
    pthread_mutex_lock(&device->read_mutex);
#endif
    result = rpc("input", device->handle, device->capability,
                 device->interface_number, timeout_ms, budget_ms,
                 NULL, 0, buffer, &received, NULL
#ifdef D200_HOST_VISUAL
                 , &device->host, started
#endif
                 );
#ifndef D200_HOST_VISUAL
    transport_error = errno;
#endif
#ifndef D200_HOST_VISUAL
    pthread_mutex_unlock(&device->read_mutex);
#endif
    release_virtual(device);
    if (result) {
#ifndef D200_HOST_VISUAL
        /* A silent peer must surface as ETIMEDOUT, not as a generic failure:
         * hid_read_timeout()'s documented timeout behavior depends on it. */
        errno = transport_error == ETIMEDOUT ? ETIMEDOUT : EIO;
#endif
        return -1;
    }
    return (int)received;
}

static hid_device_info *identity(int interface_number)
{
    hid_device_info *info = calloc(1, sizeof(*info));
    if (!info)
        return NULL;
    info->path = strdup(interface_number ? VPATH1 : VPATH0);
    info->vendor_id = VID;
    info->product_id = PID;
    info->serial_number = wcsdup(L"GHOSTDECKVHID00000");
    info->release_number = 0xffff;
    info->manufacturer_string = wcsdup(L"Zkswe");
    info->product_string = wcsdup(L"ulanzi");
    info->usage_page = interface_number ? 1 : 12;
    info->usage = interface_number ? 6 : 1;
    info->interface_number = interface_number;
    info->bus_type = HID_API_BUS_USB;
    if (!info->path || !info->serial_number || !info->manufacturer_string ||
        !info->product_string) {
        free(info->path);
        free(info->serial_number);
        free(info->manufacturer_string);
        free(info->product_string);
        free(info);
        return NULL;
    }
    return info;
}

static int virtual_path_interface(const char *path)
{
    if (path && strcmp(path, VPATH0) == 0)
        return 0;
    if (path && strcmp(path, VPATH1) == 0)
        return 1;
    return -1;
}

void hid_free_enumeration(hid_device_info *current);

int hid_init(void)
{
#ifdef D200_HOST_VISUAL
    return rpc("event", 0, NULL, 0, -1, D200_RPC_BUDGET_MS, NULL, 0, NULL, NULL, NULL, NULL, d200_host_now());
#else
    int (*function)(void) = sym("hid_init");
    return function ? function() : -1;
#endif
}

int hid_exit(void)
{
#ifdef D200_HOST_VISUAL
    /* Per-handle close owns cleanup; process-lifetime tombstones stay valid. */
    return 0;
#else
    int (*function)(void) = sym("hid_exit");
    return function ? function() : -1;
#endif
}

hid_device *hid_open(unsigned short vendor, unsigned short product,
                     const wchar_t *serial)
{
    if (vendor == VID && product == PID) {
        if (serial && wcscmp(serial, L"GHOSTDECKVHID00000")) {
#ifdef D200_HOST_VISUAL
            d200_host_error(NULL, ENODEV);
#endif
            return NULL;
        }
        return virtual_open(0);
    }
#ifdef D200_HOST_VISUAL
    d200_host_error(NULL, ENODEV);
    return NULL;
#else
    hid_device *(*function)(unsigned short, unsigned short, const wchar_t *) =
        sym("hid_open");
    return function ? function(vendor, product, serial) : NULL;
#endif
}

hid_device *hid_open_path(const char *path)
{
    int interface_number = virtual_path_interface(path);
    if (interface_number >= 0)
        return virtual_open(interface_number);
#ifdef D200_HOST_VISUAL
    d200_host_error(NULL, ENODEV);
    return NULL;
#else
    hid_device *(*function)(const char *) = sym("hid_open_path");
    return function ? function(path) : NULL;
#endif
}

int hid_write(hid_device *opaque, const unsigned char *buffer, size_t length)
{
#ifdef D200_HOST_VISUAL
    int64_t started = d200_host_now();
#endif
    if (!opaque || (length && !buffer) || length > INT_MAX) {
        errno = EINVAL;
        return -1;
    }
    if (is_virtual(opaque)) {
        virtual_device *device = (virtual_device *)opaque;
        int result;
        if (!retain_virtual(device)) {
            errno = ECANCELED;
            return -1;
        }
        /* Zero bytes never submit an output, including with a NULL buffer. */
        if (!length) {
            release_virtual(device);
            return 0;
        }
        if (length != REPORT_BYTES) {
            release_virtual(device);
            errno = EINVAL;
            return -1;
        }
        result = rpc("output", device->handle, device->capability,
                     device->interface_number, -1, D200_RPC_BUDGET_MS,
                     buffer, length, NULL, NULL, NULL
#ifdef D200_HOST_VISUAL
                     , &device->host, started
#endif
                     );
        release_virtual(device);
        return result ? -1 : (int)length;
    }
#ifdef D200_HOST_VISUAL
    d200_host_error(NULL, EINVAL);
    return -1;
#else
    int (*function)(hid_device *, const unsigned char *, size_t) = sym("hid_write");
    return function ? function(opaque, buffer, length) : -1;
#endif
}

int hid_read(hid_device *opaque, unsigned char *buffer, size_t length)
{
#ifdef D200_HOST_VISUAL
    int64_t started = d200_host_now();
#endif
    if (!opaque || (length && !buffer) || length > INT_MAX) {
        errno = EINVAL;
        return -1;
    }
    if (is_virtual(opaque)) {
        virtual_device *device = (virtual_device *)opaque;
        int timeout;
        pthread_mutex_lock(&device->mutex);
        timeout = device->nonblocking ? 0 : -1;
        pthread_mutex_unlock(&device->mutex);
        return virtual_read(opaque, buffer, length, timeout
#ifdef D200_HOST_VISUAL
                            , started
#endif
                            );
    }
#ifdef D200_HOST_VISUAL
    d200_host_error(NULL, EINVAL);
    return -1;
#else
    int (*function)(hid_device *, unsigned char *, size_t) = sym("hid_read");
    return function ? function(opaque, buffer, length) : -1;
#endif
}

int hid_read_timeout(hid_device *opaque, unsigned char *buffer,
                     size_t length, int timeout_ms)
{
#ifdef D200_HOST_VISUAL
    int64_t started = d200_host_now();
#endif
    if (!opaque || (length && !buffer) || length > INT_MAX) {
        errno = EINVAL;
        return -1;
    }
    if (is_virtual(opaque))
        return virtual_read(opaque, buffer, length, timeout_ms
#ifdef D200_HOST_VISUAL
                            , started
#endif
                            );
#ifdef D200_HOST_VISUAL
    d200_host_error(NULL, EINVAL);
    return -1;
#else
    int (*function)(hid_device *, unsigned char *, size_t, int) =
        sym("hid_read_timeout");
    return function ? function(opaque, buffer, length, timeout_ms) : -1;
#endif
}

void hid_close(hid_device *opaque)
{
#ifdef D200_HOST_VISUAL
    int64_t started = d200_host_now();
#endif
    if (is_virtual(opaque)) {
        virtual_device *device = (virtual_device *)opaque;
        pthread_mutex_lock(&device->mutex);
        if (device->closing) {
            pthread_mutex_unlock(&device->mutex);
            return;
        }
        device->closing = 1;
        pthread_mutex_unlock(&device->mutex);
#ifdef D200_HOST_VISUAL
        int64_t deadline = d200_host_after(started, D200_HOST_CLOSE_MS);
        int failed_drain = 0;
        d200_host_cancel(&device->host);
        int close_result =
#else
        struct timespec drain_deadline;
        int close_result =
#endif
        rpc("close", device->handle, device->capability,
                  device->interface_number, -1, D200_RPC_BUDGET_MS, NULL, 0, NULL, NULL, NULL
#ifdef D200_HOST_VISUAL
                  , &device->host, started
#endif
                  );
        pthread_mutex_lock(&device->mutex);
#ifdef D200_HOST_VISUAL
        if (close_result) {
            pthread_mutex_lock(&device->host.lock);
            device->host.cleanup_uncertain = 1;
            pthread_mutex_unlock(&device->host.lock);
        }
        while (device->active_calls) {
            if (d200_host_condition(&device->condition, &device->mutex, deadline)) {
                failed_drain = 1;
                break;
            }
        }
        if (failed_drain) {
            pthread_mutex_lock(&device->host.lock);
            device->host.cleanup_uncertain = 1;
            device->host.error = ETIMEDOUT;
            pthread_mutex_unlock(&device->host.lock);
        } else {
            d200_host_dispose(&device->host);
        }
        if (!failed_drain && !device->host.cleanup_uncertain && device->info)
#else
        /* The close request carries an explicit budget, so a peer that accepted
         * the connection and stopped answering cannot block teardown: on expiry
         * close_result is -1 with errno ETIMEDOUT. That result alone is not
         * teardown uncertainty, so it is not what marks the handle below. */
        (void)close_result;
        /* Bounded drain: a call that never returns must not wedge teardown. */
        close_drain_deadline(&drain_deadline);
        while (device->active_calls) {
            if (pthread_cond_timedwait(&device->condition, &device->mutex, &drain_deadline))
                break;
        }
        /* Only a call that really is still using the handle makes the teardown
         * uncertain, and only that keeps hid_error() reporting it as retained. */
        device->close_uncertain = device->active_calls != 0;
#endif
        /* Released unconditionally: the handle is already marked closing, so no
         * caller can take a fresh borrow of the entry, and the process-lifetime
         * tombstone below keeps late calls identifiable as virtual. */
        {
            hid_free_enumeration(device->info);
            device->info = NULL;
        }
        pthread_mutex_unlock(&device->mutex);
        /*
         * Studio's SendDataQueue can retain and reuse the opaque pointer after
         * another thread closes it.  Keep a process-lifetime tombstone so
         * those late calls remain identifiable as virtual and fail with
         * ECANCELED instead of being delegated to real hidapi as an invalid
         * native handle.  The allocation is intentionally released by the
         * process at exit.
         */
        return;
    }
#ifdef D200_HOST_VISUAL
    if (opaque) d200_host_error(NULL, EINVAL);
#else
    void (*function)(hid_device *) = sym("hid_close");
    if (function)
        function(opaque);
#endif
}

hid_device_info *hid_enumerate(unsigned short vendor, unsigned short product)
{
    hid_device_info *virtual_list = NULL;
    int want_d200 = (!vendor || vendor == VID) && (!product || product == PID);
#ifdef D200_HOST_VISUAL
    int bridge_reachable = want_d200 &&
        rpc("event", 0, NULL, 0, -1, D200_RPC_BUDGET_MS, NULL, 0, NULL, NULL, NULL, NULL, d200_host_now()) == 0;
#else
    /* FIX-5-T13: report an unreachable bridge once, with the reason, instead of
     * falling through to the real hidapi in silence. `daemon_seen` keeps the
     * probe itself one-per-process; the diagnostic keeps the *record* to one per
     * distinct reason, whichever of them fires first. */
    int bridge_reachable = want_d200;
    if (bridge_reachable && !daemon_seen && wait_for_daemon() != 0) {
        bridge_unreachable_diagnostic(D200_HIDSHIM_LOG_PATH, daemon_failure_errno);
        bridge_reachable = 0;
    }
#endif
    if (bridge_reachable)
    {
        hid_device_info *first = identity(0);
        hid_device_info *second = identity(1);
#ifndef D200_HOST_VISUAL
        daemon_seen = 1;
#endif
        if (!first) {
            if (second)
                hid_free_enumeration(second);
            return NULL;
        }
        first->next = second;
        virtual_list = first;
        if (vendor == VID && product == PID)
            return virtual_list;
    }
#ifdef D200_HOST_VISUAL
    if ((vendor && vendor != VID) || (product && product != PID)) d200_host_error(NULL, ENODEV);
    return virtual_list;
#else
    {
        hid_device_info *(*function)(unsigned short, unsigned short) =
            sym("hid_enumerate");
        hid_device_info *native = function ? without_d200(function(vendor, product)) : NULL;
        if (!virtual_list)
            return native;
        {
            hid_device_info *tail = virtual_list;
            while (tail->next)
                tail = tail->next;
            tail->next = native;
        }
        return virtual_list;
    }
#endif
}

void hid_free_enumeration(hid_device_info *current)
{
    if (current && virtual_path_interface(current->path) >= 0) {
        while (current) {
            hid_device_info *next = current->next;
            free(current->path);
            free(current->serial_number);
            free(current->manufacturer_string);
            free(current->product_string);
            free(current);
            current = next;
        }
        return;
    }
#ifdef D200_HOST_VISUAL
    if (current) d200_host_error(NULL, EINVAL);
#else
    void (*function)(hid_device_info *) = sym("hid_free_enumeration");
    if (function)
        function(current);
#endif
}

int hid_set_nonblocking(hid_device *opaque, int nonblocking)
{
    if (is_virtual(opaque)) {
        virtual_device *device = (virtual_device *)opaque;
        pthread_mutex_lock(&device->mutex);
        if (device->closing) {
            pthread_mutex_unlock(&device->mutex);
            errno = ECANCELED;
            return -1;
        }
        device->nonblocking = nonblocking != 0;
        pthread_mutex_unlock(&device->mutex);
        return 0;
    }
#ifdef D200_HOST_VISUAL
    d200_host_error(NULL, EINVAL);
    return -1;
#else
    int (*function)(hid_device *, int) = sym("hid_set_nonblocking");
    return function ? function(opaque, nonblocking) : -1;
#endif
}

int hid_send_feature_report(hid_device *device, const unsigned char *buffer, size_t length)
{
#ifdef D200_HOST_VISUAL
    int error = EOPNOTSUPP;
    if (!is_virtual(device) || (length && !buffer) || length > INT_MAX) error = EINVAL;
    else {
        virtual_device *owner = (virtual_device *)device;
        pthread_mutex_lock(&owner->mutex);
        if (owner->closing) error = ECANCELED;
        pthread_mutex_unlock(&owner->mutex);
    }
    d200_host_error(is_virtual(device) ? &((virtual_device *)device)->host : NULL, error);
    return -1;
#else
    if (!device || (length && !buffer) || length > INT_MAX)
        return -1;
    if (is_virtual(device))
        return -1;
    int (*function)(hid_device *, const unsigned char *, size_t) =
        sym("hid_send_feature_report");
    return function ? function(device, buffer, length) : -1;
#endif
}

int hid_get_feature_report(hid_device *device, unsigned char *buffer, size_t length)
{
#ifdef D200_HOST_VISUAL
    return hid_send_feature_report(device, buffer, length);
#else
    if (!device || (length && !buffer) || length > INT_MAX)
        return -1;
    if (is_virtual(device))
        return -1;
    int (*function)(hid_device *, unsigned char *, size_t) =
        sym("hid_get_feature_report");
    return function ? function(device, buffer, length) : -1;
#endif
}

int hid_get_input_report(hid_device *device, unsigned char *buffer, size_t length)
{
#ifdef D200_HOST_VISUAL
    int64_t started = d200_host_now();
#endif
    if (!device || (length && !buffer) || length > INT_MAX) {
        errno = EINVAL;
        return -1;
    }
    if (is_virtual(device))
        return virtual_read(device, buffer, length, 0
#ifdef D200_HOST_VISUAL
                            , started
#endif
                            );
#ifdef D200_HOST_VISUAL
    d200_host_error(NULL, EINVAL);
    return -1;
#else
    int (*function)(hid_device *, unsigned char *, size_t) =
        sym("hid_get_input_report");
    return function ? function(device, buffer, length) : -1;
#endif
}

const wchar_t *hid_error(hid_device *device)
{
#ifdef D200_HOST_VISUAL
    int error = d200_host_last_error;
    if (is_virtual(device)) {
        virtual_device *owner = (virtual_device *)device;
        int closing;
        pthread_mutex_lock(&owner->mutex);
        closing = owner->closing;
        pthread_mutex_unlock(&owner->mutex);
        pthread_mutex_lock(&owner->host.lock);
        int uncertain = owner->host.cleanup_uncertain;
        error = owner->host.error;
        pthread_mutex_unlock(&owner->host.lock);
        if (uncertain) return L"D200 HOST cleanup uncertain; in-use resources retained until process exit";
        if (closing) return L"D200 HOST handle closed (ECANCELED)";
    }
    switch (error) {
    case 0: return L"D200 HOST fixture transport";
    case ECANCELED: return L"D200 HOST operation cancelled (ECANCELED)";
    case ETIMEDOUT: return L"D200 HOST transport deadline exceeded";
    case EPERM: return L"D200 HOST root or fixture creation identity refused";
    case EINVAL: return L"D200 HOST invalid argument or missing fixture configuration";
    case EOPNOTSUPP: return L"D200 HOST unsupported HID operation";
    case ENODEV: return L"D200 HOST only the virtual D200 fixture is allowed";
    case EPROTO: return L"D200 HOST invalid or incomplete fixture reply";
    default: return L"D200 HOST fixture transport failure";
    }
#else
    if (is_virtual(device)) {
        virtual_device *owner = (virtual_device *)device;
        int uncertain;
        pthread_mutex_lock(&owner->mutex);
        uncertain = owner->close_uncertain;
        pthread_mutex_unlock(&owner->mutex);
        return uncertain ? L"virtual D200 transport cleanup uncertain; retained until process exit"
                         : L"virtual D200 transport error";
    }
    const wchar_t *(*function)(hid_device *) = sym("hid_error");
    return function ? function(device) : L"hidapi unavailable";
#endif
}

hid_device_info *hid_get_device_info(hid_device *device)
{
    if (is_virtual(device)) {
        virtual_device *owner = (virtual_device *)device;
        hid_device_info *info;
        if (!retain_virtual(owner)) {
            errno = ECANCELED;
            return NULL;
        }
        pthread_mutex_lock(&owner->mutex);
        if (!owner->info)
            owner->info = identity(owner->interface_number);
        info = owner->info;
        pthread_mutex_unlock(&owner->mutex);
        release_virtual(owner);
        /* Borrowed until close, not an enumeration list to free. The caller
         * must not close the owner concurrently with use of this pointer. */
        return info;
    }
#ifdef D200_HOST_VISUAL
    d200_host_error(NULL, EINVAL);
    return NULL;
#else
    hid_device_info *(*function)(hid_device *) = sym("hid_get_device_info");
    return function ? function(device) : NULL;
#endif
}

static int virtual_string(wchar_t *output, size_t maximum, const wchar_t *value)
{
    if (!output || !maximum)
        return -1;
    wcsncpy(output, value, maximum - 1);
    output[maximum - 1] = L'\0';
    return 0;
}

#ifdef D200_HOST_VISUAL
static int host_string(hid_device *device, wchar_t *output, size_t maximum, const wchar_t *value)
{
    if (!is_virtual(device)) { d200_host_error(NULL, EINVAL); return -1; }
    virtual_device *owner = (virtual_device *)device;
    if (!retain_virtual(owner)) { d200_host_error(&owner->host, ECANCELED); return -1; }
    int result = virtual_string(output, maximum, value);
    release_virtual(owner);
    if (result) d200_host_error(&owner->host, EINVAL);
    return result;
}
#endif

int hid_get_manufacturer_string(hid_device *device, wchar_t *output, size_t maximum)
{
#ifdef D200_HOST_VISUAL
    return host_string(device, output, maximum, L"Zkswe");
#else
    if (is_virtual(device))
        return virtual_string(output, maximum, L"Zkswe");
    int (*function)(hid_device *, wchar_t *, size_t) = sym("hid_get_manufacturer_string");
    return function ? function(device, output, maximum) : -1;
#endif
}

int hid_get_product_string(hid_device *device, wchar_t *output, size_t maximum)
{
#ifdef D200_HOST_VISUAL
    return host_string(device, output, maximum, L"ulanzi");
#else
    if (is_virtual(device))
        return virtual_string(output, maximum, L"ulanzi");
    int (*function)(hid_device *, wchar_t *, size_t) = sym("hid_get_product_string");
    return function ? function(device, output, maximum) : -1;
#endif
}

int hid_get_serial_number_string(hid_device *device, wchar_t *output, size_t maximum)
{
#ifdef D200_HOST_VISUAL
    return host_string(device, output, maximum, L"GHOSTDECKVHID00000");
#else
    if (is_virtual(device))
        return virtual_string(output, maximum, L"GHOSTDECKVHID00000");
    int (*function)(hid_device *, wchar_t *, size_t) = sym("hid_get_serial_number_string");
    return function ? function(device, output, maximum) : -1;
#endif
}

int hid_get_indexed_string(hid_device *device, int index, wchar_t *output, size_t maximum)
{
#ifdef D200_HOST_VISUAL
    (void)index;
    return host_string(device, output, maximum, L"");
#else
    if (is_virtual(device)) {
        (void)index;
        return virtual_string(output, maximum, L"");
    }
    int (*function)(hid_device *, int, wchar_t *, size_t) = sym("hid_get_indexed_string");
    return function ? function(device, index, output, maximum) : -1;
#endif
}

int hid_get_report_descriptor(hid_device *device, unsigned char *buffer, size_t length)
{
#ifdef D200_HOST_VISUAL
    return hid_send_feature_report(device, buffer, length);
#else
    if (!device || (length && !buffer) || length > INT_MAX)
        return -1;
    if (is_virtual(device))
        return -1;
    int (*function)(hid_device *, unsigned char *, size_t) =
        sym("hid_get_report_descriptor");
    return function ? function(device, buffer, length) : -1;
#endif
}

const hid_api_version *hid_version(void)
{
#ifdef D200_HOST_VISUAL
    static const hid_api_version version = {0, 14, 0};
    return &version;
#else
    const hid_api_version *(*function)(void) = sym("hid_version");
    return function ? function() : NULL;
#endif
}

const char *hid_version_str(void)
{
#ifdef D200_HOST_VISUAL
    return "0.14.0-d200-host-visual";
#else
    const char *(*function)(void) = sym("hid_version_str");
    return function ? function() : "0.0.0";
#endif
}

int hid_darwin_get_location_id(hid_device *device, uint32_t *location)
{
#ifdef D200_HOST_VISUAL
    if (!is_virtual(device) || !location) { d200_host_error(NULL, EINVAL); return -1; }
    virtual_device *owner = (virtual_device *)device;
    if (!retain_virtual(owner)) { d200_host_error(&owner->host, ECANCELED); return -1; }
    *location = 0;
    release_virtual(owner);
    return 0;
#else
    if (is_virtual(device)) {
        if (location)
            *location = 0;
        return 0;
    }
    int (*function)(hid_device *, uint32_t *) = sym("hid_darwin_get_location_id");
    return function ? function(device, location) : -1;
#endif
}

void hid_darwin_set_open_exclusive(int exclusive)
{
#ifdef D200_HOST_VISUAL
    /* Fixture capabilities are always exclusive; never mutate OS HID state. */
    if (!exclusive) d200_host_error(NULL, EOPNOTSUPP);
#else
    void (*function)(int) = sym("hid_darwin_set_open_exclusive");
    if (function)
        function(exclusive);
#endif
}

int hid_darwin_get_open_exclusive(void)
{
#ifdef D200_HOST_VISUAL
    return 1;
#else
    int (*function)(void) = sym("hid_darwin_get_open_exclusive");
    return function ? function() : 0;
#endif
}

int hid_darwin_is_device_open_exclusive(hid_device *device)
{
#ifdef D200_HOST_VISUAL
    if (!is_virtual(device)) { d200_host_error(NULL, EINVAL); return -1; }
    virtual_device *owner = (virtual_device *)device;
    if (!retain_virtual(owner)) { d200_host_error(&owner->host, ECANCELED); return -1; }
    release_virtual(owner);
    return 1;
#else
    if (is_virtual(device))
        return 1;
    int (*function)(hid_device *) = sym("hid_darwin_is_device_open_exclusive");
    return function ? function(device) : 0;
#endif
}
