#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/ioctl.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

typedef int (*open_fn)(const char *, int, ...);
typedef int (*close_fn)(int);
typedef int (*set_string_fn)(const char *, const char *);

struct color_key {
    unsigned char enable;
    unsigned char red;
    unsigned char green;
    unsigned char blue;
};

#define FB_GET_COLOR_KEY 0x80044666UL
#define FB_SET_COLOR_KEY 0x40044667UL

static open_fn real_open_fn;
static open_fn real_open64_fn;
static open_fn real___open_2_fn;
static open_fn real___open64_2_fn;
static close_fn real_close_fn;
static set_string_fn real_set_string_fn;
/* Private proxy-to-preload handoff, consumed at load time, not a user option. */
static int usb_diagnostic_fd = -1;
static pthread_once_t init_once = PTHREAD_ONCE_INIT;
static pthread_mutex_t framebuffer_lock = PTHREAD_MUTEX_INITIALIZER;
static int framebuffer_fd = -1;
static int framebuffer_configured;
static int framebuffer_teardown;
static struct color_key original_color_key;

static void resolve_symbols(void)
{
    real_open_fn = (open_fn)dlsym(RTLD_NEXT, "open");
    real_open64_fn = (open_fn)dlsym(RTLD_NEXT, "open64");
    real___open_2_fn = (open_fn)dlsym(RTLD_NEXT, "__open_2");
    real___open64_2_fn = (open_fn)dlsym(RTLD_NEXT, "__open64_2");
    real_close_fn = (close_fn)dlsym(RTLD_NEXT, "close");
    real_set_string_fn = (set_string_fn)dlsym(
        RTLD_NEXT, "_ZN16SystemProperties9setStringEPKcS1_");
}

__attribute__((constructor)) static void initialize_usb_diagnostic(void)
{
    int saved_errno = errno;
    const char *text = getenv("D200_USB_DIAGNOSTIC_FD");
    int fd = 0, valid = text != NULL && *text != '\0';
    if (valid) {
        unsigned digits = 0;
        for (const char *p = text; *p; ++p) {
            if (++digits > 10 || *p < '0' || *p > '9' || fd > (INT32_MAX - (*p - '0')) / 10) { valid = 0; break; }
            fd = fd * 10 + (*p - '0');
        }
    }
    (void)unsetenv("D200_USB_DIAGNOSTIC_FD");
    if (valid && fd > STDERR_FILENO) {
        (void)pthread_once(&init_once, resolve_symbols);
        if (real_close_fn != NULL) {
            int descriptor_flags = fcntl(fd, F_GETFD);
            int flags = fcntl(fd, F_GETFL);
            /* The proxy opened an independent nonblocking description. Do not
             * change shared stdout/stderr flags or inherit this into helpers. */
            if (descriptor_flags >= 0 && flags >= 0 && (flags & O_NONBLOCK) &&
                (flags & O_ACCMODE) != O_RDONLY &&
                fcntl(fd, F_SETFD, descriptor_flags | FD_CLOEXEC) == 0)
                usb_diagnostic_fd = fd;
            else (void)real_close_fn(fd);
        }
    }
    errno = saved_errno;
}

__attribute__((destructor)) static void close_usb_diagnostic(void)
{
    int saved_errno = errno;
    int fd = usb_diagnostic_fd;
    usb_diagnostic_fd = -1;
    if (fd >= 0 && real_close_fn != NULL) (void)real_close_fn(fd);
    errno = saved_errno;
}

static void configure_framebuffer(const char *path, int fd)
{
    int saved_errno = errno;
    const char *enabled = getenv("D200_VIDEO_UNDER_STUDIO");
    struct color_key black = {1, 0, 0, 0};
    int control_fd;
    int cancel_state;
    if (fd < 0 || path == NULL || strcmp(path, "/dev/fb0") != 0 ||
        enabled == NULL || strcmp(enabled, "1") != 0 ||
        real_open_fn == NULL || real_close_fn == NULL) {
        errno = saved_errno;
        return;
    }
    /* Owned open/close calls are cancellation points. Do not abandon the
     * mutex or a partially acquired restoration obligation at those calls. */
    (void)pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, &cancel_state);
    pthread_mutex_lock(&framebuffer_lock);
    if (framebuffer_fd < 0 && !framebuffer_teardown) {
        /* Own the control handle until teardown. Never borrow an application
         * descriptor: close/dup2 of an application handle cannot retarget it. */
        control_fd = real_open_fn("/dev/fb0", O_RDWR | O_CLOEXEC);
        if (control_fd < 0)
            goto out;
        if (ioctl(control_fd, FB_GET_COLOR_KEY, &original_color_key) < 0) {
            (void)real_close_fn(control_fd);
            goto out;
        }
        framebuffer_fd = control_fd;
        /* Keep the rollback obligation even if SET reports failure. */
        framebuffer_configured = 1;
        if (ioctl(control_fd, FB_SET_COLOR_KEY, &black) < 0 &&
            ioctl(control_fd, FB_SET_COLOR_KEY, &original_color_key) == 0) {
            framebuffer_configured = 0;
            framebuffer_fd = -1;
            (void)real_close_fn(control_fd);
        }
    }
out:
    pthread_mutex_unlock(&framebuffer_lock);
    (void)pthread_setcancelstate(cancel_state, NULL);
    errno = saved_errno;
}

/* Revalidate the private control handle against the descriptor table and the
 * framebuffer before any restore write. The probe reads the live key, so it
 * must never target original_color_key: a recycled number would otherwise
 * overwrite the saved key we are restoring. */
static int framebuffer_handle_valid(int fd)
{
    struct color_key probe;
    return fd >= 0 && fcntl(fd, F_GETFD) >= 0 &&
           ioctl(fd, FB_GET_COLOR_KEY, &probe) == 0;
}

/* Restore the saved key through a handle opened after the original number was
 * released: the application may hold that number now, so it is never probed,
 * written through or closed again. The fresh handle is ours by construction
 * and is closed here. Caller holds framebuffer_lock and has already latched
 * the teardown latch; the black-out obligation never survives the call. */
static void restore_color_key_through_fresh_handle(void)
{
    int control_fd;
    if (real_open_fn == NULL || real_close_fn == NULL) return;
    control_fd = real_open_fn("/dev/fb0", O_RDWR | O_CLOEXEC);
    if (control_fd < 0) return;
    if (framebuffer_handle_valid(control_fd) &&
        ioctl(control_fd, FB_SET_COLOR_KEY, &original_color_key) == 0)
        framebuffer_configured = 0;
    (void)real_close_fn(control_fd);
}

/* The application may close a number that is, or was, our private handle:
 * directly, via a recycled descriptor, or by dup2() overwriting it without
 * calling close() at all. Relinquish ownership under the same lock the rest of
 * the state uses, so teardown never ioctls or closes a descriptor we no longer
 * own. The saved key is only valid while the original black-out is live, so
 * the obligation is discharged here, through a fresh handle, before teardown
 * latches: re-acquiring later would probe a screen that is still forced black
 * and overwrite the true original, while dropping it here would strand the
 * black-out on the panel for the rest of the process. */
static void release_framebuffer_fd(int fd)
{
    int cancel_state;
    if (fd < 0) return;
    (void)pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, &cancel_state);
    pthread_mutex_lock(&framebuffer_lock);
    if (fd == framebuffer_fd) {
        framebuffer_fd = -1;
        framebuffer_teardown = 1;
        if (framebuffer_configured)
            restore_color_key_through_fresh_handle();
        framebuffer_configured = 0;
    }
    pthread_mutex_unlock(&framebuffer_lock);
    (void)pthread_setcancelstate(cancel_state, NULL);
}

static void restore_framebuffer(void)
{
    int saved_errno = errno;
    int attempt;
    int cancel_state;
    (void)pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, &cancel_state);
    pthread_mutex_lock(&framebuffer_lock);
    framebuffer_teardown = 1;
    /* Failed restoration stays pending between attempts. Teardown is bounded
     * even on permanent ioctl failure and releases the owned handle once. */
    for (attempt = 0; framebuffer_configured && attempt < 3; ++attempt) {
        /* Revalidate per attempt. A number we no longer own is never written
         * through, and an invalid handle stops the retries immediately. */
        if (!framebuffer_handle_valid(framebuffer_fd)) break;
        if (ioctl(framebuffer_fd, FB_SET_COLOR_KEY, &original_color_key) == 0)
            framebuffer_configured = 0;
    }
    /* Release only a handle that just validated; a stale or recycled number
     * must not be closed on someone else's behalf. */
    if (framebuffer_handle_valid(framebuffer_fd)) {
        int control_fd = framebuffer_fd;
        framebuffer_fd = -1;
        framebuffer_configured = 0;
        (void)real_close_fn(control_fd);
    } else {
        /* The owned number no longer validates, so it is neither written
         * through nor closed: if fcntl() succeeds it may be a recycled
         * application descriptor. The black-out can still be live, so take
         * one fresh handle we own by construction and restore through that.
         * The reopen failure and the leftover unvalidated number are bounded:
         * teardown runs once, and the obligation never survives it. */
        framebuffer_fd = -1;
        if (framebuffer_configured)
            restore_color_key_through_fresh_handle();
        framebuffer_configured = 0;
    }
    pthread_mutex_unlock(&framebuffer_lock);
    (void)pthread_setcancelstate(cancel_state, NULL);
    errno = saved_errno;
}

static int mode_required(int flags)
{
    return (flags & O_CREAT) != 0 ||
           ((flags & O_TMPFILE) == O_TMPFILE);
}

static const char *hid_socket_for_path(const char *path)
{
    if (path != NULL && strcmp(path, "/dev/hidg0") == 0)
        return getenv("D200_HIDG0_SOCKET");
    if (path != NULL && strcmp(path, "/dev/hidg1") == 0)
        return getenv("D200_HIDG1_SOCKET");
    return NULL;
}

static int redirected_open(const char *socket_path, int flags)
{
    struct sockaddr_un address;
    int type = SOCK_SEQPACKET;
    int fd;
    size_t length;

    if (socket_path == NULL ||
        (socket_path[0] != '/' && socket_path[0] != '@')) {
        errno = ENOENT;
        return -1;
    }
    length = strlen(socket_path);
    if (length == 0 || length >= sizeof(address.sun_path)) {
        errno = ENAMETOOLONG;
        return -1;
    }
    if (flags & O_CLOEXEC)
        type |= SOCK_CLOEXEC;

    fd = socket(AF_UNIX, type, 0);
    if (fd < 0)
        return -1;
    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    if (socket_path[0] == '@') {
        address.sun_path[0] = '\0';
        memcpy(address.sun_path + 1, socket_path + 1, length - 1);
    } else {
        memcpy(address.sun_path, socket_path, length + 1);
    }
    if (connect(fd, (struct sockaddr *)&address,
                offsetof(struct sockaddr_un, sun_path) + length +
                    (socket_path[0] == '@' ? 0 : 1)) < 0) {
        int saved_errno = errno;
        close(fd);
        errno = saved_errno;
        return -1;
    }
    if (flags & O_NONBLOCK) {
        int descriptor_flags = fcntl(fd, F_GETFL);
        if (descriptor_flags < 0 || fcntl(fd, F_SETFL,
                                           descriptor_flags | O_NONBLOCK) < 0) {
            int saved_errno = errno;
            close(fd);
            errno = saved_errno;
            return -1;
        }
    }
    return fd;
}

static void close_unreturned_fd(void *argument)
{
    (void)real_close_fn(*(int *)argument);
}

static int intercept_open(open_fn fallback, const char *path, int flags, mode_t mode)
{
    const char *socket_path;
    int fd;

    (void)pthread_once(&init_once, resolve_symbols);
    socket_path = hid_socket_for_path(path);
    if (socket_path != NULL ||
        (path != NULL && (strcmp(path, "/dev/hidg0") == 0 ||
                          strcmp(path, "/dev/hidg1") == 0)))
        return redirected_open(socket_path, flags);
    if (fallback == NULL) {
        errno = ENOSYS;
        return -1;
    }
    if (mode_required(flags))
        fd = fallback(path, flags, mode);
    else
        fd = fallback(path, flags);
    if (fd >= 0 && real_close_fn != NULL) {
        /* A cancellation delivered when configuration restores the caller's
         * cancellation state must also release the not-yet-returned fd. */
        pthread_cleanup_push(close_unreturned_fd, &fd);
        configure_framebuffer(path, fd);
        pthread_cleanup_pop(0);
    }
    return fd;
}

int open(const char *path, int flags, ...)
{
    va_list ap;
    mode_t mode = 0;
    if (mode_required(flags)) {
        va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }
    (void)pthread_once(&init_once, resolve_symbols);
    return intercept_open(real_open_fn, path, flags, mode);
}

int open64(const char *path, int flags, ...)
{
    va_list ap;
    mode_t mode = 0;
    if (mode_required(flags)) {
        va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }
    (void)pthread_once(&init_once, resolve_symbols);
    return intercept_open(real_open64_fn ? real_open64_fn : real_open_fn, path, flags, mode);
}

int __open_2(const char *path, int flags)
{
    (void)pthread_once(&init_once, resolve_symbols);
    return intercept_open(real___open_2_fn ? real___open_2_fn : real_open_fn, path, flags, 0);
}

int __open64_2(const char *path, int flags)
{
    (void)pthread_once(&init_once, resolve_symbols);
    return intercept_open(real___open64_2_fn ? real___open64_2_fn :
                          (real_open64_fn ? real_open64_fn : real_open_fn), path, flags, 0);
}

int d200_system_properties_set_string(const char *key, const char *value)
    __asm__("_ZN16SystemProperties9setStringEPKcS1_");

/* Only fixed classes are logged. Unknown values, commands, environment and
 * property contents never enter diagnostics. This does not broaden suppression. */
static const char *usb_value_class(const char *value)
{
    if (value != NULL) {
        if (strcmp(value, "hid") == 0) return "hid";
        if (strcmp(value, "adb") == 0) return "adb";
        if (strcmp(value, "hid,adb") == 0 || strcmp(value, "adb,hid") == 0) return "combined";
    }
    return "other";
}

static const char *usb_request_time(char out[32])
{
    int saved_errno = errno;
    struct timespec now;
    const char *result = "null";
    if (clock_gettime(CLOCK_MONOTONIC, &now) == 0 && now.tv_sec >= 0 &&
        now.tv_nsec >= 0 && now.tv_nsec < 1000000000L &&
        (uint64_t)now.tv_sec <= (UINT64_MAX - (uint64_t)now.tv_nsec) / UINT64_C(1000000000)) {
        uint64_t ns = (uint64_t)now.tv_sec * UINT64_C(1000000000) + (uint64_t)now.tv_nsec;
        (void)snprintf(out, 32, "%llu", (unsigned long long)ns);
        result = out;
    }
    errno = saved_errno;
    return result;
}

/* At most two bounded, one-shot writes per intercepted USB request. A request
 * record can survive a setter that never returns; only the return record has
 * an observed interceptor result. requestMonotonicNs is the request time in
 * both records, not a return timestamp or a globally unique request ID.
 * Diagnostics are optional evidence, never a USB ownership or success proof. */
static void usb_diagnostic(const char *value_class, const char *disposition,
                           const char *request_ns, int returned, int result)
{
    int saved_errno = errno;
    char line[384], number[32];
    const char *return_json = "null";
    if (returned) {
        (void)snprintf(number, sizeof(number), "%d", result);
        return_json = number;
    }
    int length = snprintf(line, sizeof(line),
        "{\"event\":\"d200-usb-config\",\"pid\":%ld,\"clock\":\"device-monotonic\","
        "\"requestMonotonicNs\":%s,\"value\":\"%s\",\"disposition\":\"%s\","
        "\"phase\":\"%s\",\"result\":%s}\n",
        (long)getpid(), request_ns, value_class, disposition,
        returned ? "return" : "request", return_json);
    if (length > 0 && (size_t)length < sizeof(line)) {
        int fd = usb_diagnostic_fd;
        int flags = fd >= 0 ? fcntl(fd, F_GETFL) : -1;
        if (flags >= 0 && (flags & O_NONBLOCK)) (void)write(fd, line, (size_t)length);
    }
    errno = saved_errno;
}

int d200_system_properties_set_string(const char *key, const char *value)
{
    (void)pthread_once(&init_once, resolve_symbols);
    int is_usb = key != NULL && strcmp(key, "sys.usb.config") == 0;
    int suppressed = is_usb && value != NULL && strcmp(value, "hid") == 0;
    char request_time[32];
    const char *request_ns = "null", *value_class = "other";
    const char *disposition = suppressed ? "suppressed" : real_set_string_fn ? "forwarded" : "unavailable";
    if (is_usb) {
        value_class = usb_value_class(value);
        request_ns = usb_request_time(request_time);
        usb_diagnostic(value_class, disposition, request_ns, 0, 0);
    }
    int result;
    if (suppressed) result = 0;
    else if (real_set_string_fn == NULL) {
        errno = ENOSYS;
        result = -1;
    } else result = real_set_string_fn(key, value);
    if (is_usb) usb_diagnostic(value_class, disposition, request_ns, 1, result);
    return result;
}

int close(int fd)
{
    int saved_errno = errno;
    (void)pthread_once(&init_once, resolve_symbols);
    if (real_close_fn == NULL) {
        errno = ENOSYS;
        return -1;
    }
    /* Relinquish the private framebuffer handle before forwarding, then keep
     * the original pass-through errno contract for every other fd. */
    release_framebuffer_fd(fd);
    errno = saved_errno;
    return real_close_fn(fd);
}

__attribute__((destructor)) static void restore_framebuffer_on_exit(void)
{
    restore_framebuffer();
}
