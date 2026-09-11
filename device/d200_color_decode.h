/* One persistent JPEG decoder and exactly two caller-owned frame slots.
 * Zero-initialize the queue. The caller serializes init/destroy with every API
 * call; only the worker invokes decode. Do not copy an initialized queue.
 * Indices are contiguous from zero; UINT64_MAX is reserved by the wire format.
 * Submission borrows jpeg and the callback's per-slot output storage. Neither
 * may be reused until release succeeds, or destroy has successfully joined the
 * worker. DONE publishes callback writes but does not release a slot or credit.
 * Release belongs at protocol CONSUMED, not at decode completion.
 *
 * Notifications are coalescible wakeups, not completion counts. Drain and check
 * the admitted indices' status before polling again. Only DONE writes result.
 * The notification fd is borrowed: never read/close it outside this module.
 * A failed join retains all resources: keep the queue, context, decoder and
 * both slots alive and retry destroy. Close errors are sticky and reported on
 * repeat destroy, but ambiguous close failures are never retried (fd reuse).
 * A failed init rolls back acquired resources; call destroy to check cleanup.
 */
#ifndef D200_COLOR_DECODE_H
#define D200_COLOR_DECODE_H
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>

typedef int (*d200_decode_fn)(void *context, unsigned slot,
                              const uint8_t *jpeg, size_t size);

enum d200_decode_state {
    D200_DECODE_EMPTY = 0,
    D200_DECODE_PENDING = 1,
    D200_DECODE_DONE = 2,
    D200_DECODE_INVALID = -1
};

struct d200_decode_job {
    uint64_t index;
    const uint8_t *jpeg;
    size_t size;
    int state, result;
};

struct d200_decode_queue {
    pthread_mutex_t mutex;
    pthread_cond_t wake;
    pthread_t worker;
    d200_decode_fn decode;
    void *context;
    struct d200_decode_job slots[2];
    uint64_t submitted, started;
    int notify[2];
    unsigned mutex_ready, cond_ready, pipe_owned, worker_started, initialized;
    unsigned stop;
    int notify_error, cleanup_error;
};

static inline int d200_decode_error(int error) {
    errno = error;
    return -1;
}

/* Called with mutex held. A full pipe already guarantees a poll wakeup. */
static inline void d200_decode_notify(struct d200_decode_queue *q) {
    const uint8_t byte = 1;
    ssize_t count;
    do { count = write(q->notify[1], &byte, 1); } while (count < 0 && errno == EINTR);
    if (count != 1 && !(count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) &&
        !q->notify_error) q->notify_error = count < 0 ? errno : EIO;
}

static inline void *d200_decode_worker(void *context) {
    struct d200_decode_queue *q = context;
    pthread_mutex_lock(&q->mutex);
    while (!q->stop) {
        while (!q->stop && q->started == q->submitted) {
            int error = pthread_cond_wait(&q->wake, &q->mutex);
            if (error) {
                q->stop = 1;
                q->notify_error = error;
                d200_decode_notify(q);
            }
        }
        if (q->stop) break;
        unsigned slot = (unsigned)(q->started % 2);
        struct d200_decode_job *job = &q->slots[slot];
        const uint8_t *jpeg = job->jpeg;
        size_t size = job->size;
        q->started++;
        pthread_mutex_unlock(&q->mutex);
        int result = q->decode(q->context, slot, jpeg, size);
        pthread_mutex_lock(&q->mutex);
        job->result = result;
        job->state = D200_DECODE_DONE;
        d200_decode_notify(q);
    }
    pthread_mutex_unlock(&q->mutex);
    return NULL;
}

static inline int d200_decode_destroy(struct d200_decode_queue *q) {
    int error;
    if (!q) return d200_decode_error(EINVAL);
    if (q->worker_started) {
        error = pthread_mutex_lock(&q->mutex);
        if (error) return d200_decode_error(error);
        q->stop = 1;
        error = pthread_cond_signal(&q->wake);
        pthread_mutex_unlock(&q->mutex);
        if (error) return d200_decode_error(error);
        error = pthread_join(q->worker, NULL);
        if (error) return d200_decode_error(error);
        q->worker_started = 0;
    }
    q->initialized = 0;
    for (unsigned i = 0; i < 2; i++) {
        if (q->pipe_owned & (1u << i)) {
            int fd = q->notify[i];
            q->pipe_owned &= ~(1u << i);
            q->notify[i] = -1;
            /* Linux/macOS close errors must not cause a later close of a
             * descriptor that another thread may already have reused. */
            if (close(fd) && !q->cleanup_error) q->cleanup_error = errno;
        }
    }
    if (q->cond_ready) {
        error = pthread_cond_destroy(&q->wake);
        if (error) return d200_decode_error(error);
        q->cond_ready = 0;
    }
    if (q->mutex_ready) {
        error = pthread_mutex_destroy(&q->mutex);
        if (error) return d200_decode_error(error);
        q->mutex_ready = 0;
    }
    error = q->cleanup_error;
    memset(q, 0, sizeof(*q));
    q->cleanup_error = error;
    return error ? d200_decode_error(error) : 0;
}

static inline int d200_decode_init(struct d200_decode_queue *q,
                                    d200_decode_fn decode, void *context) {
    int error, flags, descriptors[2];
    if (!q || !decode) return d200_decode_error(EINVAL);
    if (q->initialized || q->worker_started || q->mutex_ready || q->cond_ready ||
        q->pipe_owned || q->cleanup_error) return d200_decode_error(EBUSY);
    memset(q, 0, sizeof(*q));
    q->notify[0] = q->notify[1] = -1;
    error = pthread_mutex_init(&q->mutex, NULL);
    if (error) goto failed;
    q->mutex_ready = 1;
    error = pthread_cond_init(&q->wake, NULL);
    if (error) goto failed;
    q->cond_ready = 1;
    if (pipe(descriptors)) { error = errno; goto failed; }
    q->notify[0] = descriptors[0]; q->notify[1] = descriptors[1];
    q->pipe_owned = 3;
    for (unsigned i = 0; i < 2; i++) {
        flags = fcntl(q->notify[i], F_GETFL);
        if (flags < 0 || fcntl(q->notify[i], F_SETFL, flags | O_NONBLOCK) < 0) {
            error = errno; goto failed;
        }
        flags = fcntl(q->notify[i], F_GETFD);
        if (flags < 0 || fcntl(q->notify[i], F_SETFD, flags | FD_CLOEXEC) < 0) {
            error = errno; goto failed;
        }
    }
    q->decode = decode; q->context = context;
    error = pthread_create(&q->worker, NULL, d200_decode_worker, q);
    if (error) goto failed;
    q->worker_started = q->initialized = 1;
    return 0;
failed:
    (void)d200_decode_destroy(q);
    return d200_decode_error(error);
}

static inline int d200_decode_submit(struct d200_decode_queue *q, uint64_t index,
                                      const uint8_t *jpeg, size_t size) {
    int error;
    if (!q || !q->initialized || !jpeg || !size || index == UINT64_MAX)
        return d200_decode_error(EINVAL);
    error = pthread_mutex_lock(&q->mutex);
    if (error) return d200_decode_error(error);
    struct d200_decode_job *job = &q->slots[index % 2];
    if (q->stop) error = ECANCELED;
    else if (q->notify_error) error = q->notify_error;
    else if (index != q->submitted) error = EINVAL;
    else if (job->state != D200_DECODE_EMPTY) error = EBUSY;
    else {
        job->index = index; job->jpeg = jpeg; job->size = size;
        job->state = D200_DECODE_PENDING;
        q->submitted++;
        error = pthread_cond_signal(&q->wake);
        if (error) {
            q->submitted--;
            memset(job, 0, sizeof(*job));
        }
    }
    pthread_mutex_unlock(&q->mutex);
    return error ? d200_decode_error(error) : 0;
}

/* EMPTY means no live job in index % 2; INVALID/ENOENT means a different live
 * index occupies it. Result is optional and untouched unless status is DONE. */
static inline int d200_decode_status(struct d200_decode_queue *q, uint64_t index,
                                      int *decode_result) {
    int error, state;
    if (!q || !q->initialized || index == UINT64_MAX)
        return d200_decode_error(EINVAL);
    error = pthread_mutex_lock(&q->mutex);
    if (error) return d200_decode_error(error);
    struct d200_decode_job *job = &q->slots[index % 2];
    state = job->state;
    if (state != D200_DECODE_EMPTY && job->index != index) {
        state = D200_DECODE_INVALID;
        error = ENOENT;
    } else if (state == D200_DECODE_DONE && decode_result) *decode_result = job->result;
    pthread_mutex_unlock(&q->mutex);
    return error ? d200_decode_error(error) : state;
}

static inline int d200_decode_release(struct d200_decode_queue *q, uint64_t index) {
    int error;
    if (!q || !q->initialized || index == UINT64_MAX)
        return d200_decode_error(EINVAL);
    error = pthread_mutex_lock(&q->mutex);
    if (error) return d200_decode_error(error);
    struct d200_decode_job *job = &q->slots[index % 2];
    if (job->state == D200_DECODE_EMPTY || job->index != index) error = ENOENT;
    else if (job->state != D200_DECODE_DONE) error = EBUSY;
    else memset(job, 0, sizeof(*job));
    pthread_mutex_unlock(&q->mutex);
    return error ? d200_decode_error(error) : 0;
}

static inline int d200_decode_notify_fd(struct d200_decode_queue *q) {
    if (!q || !q->initialized) return d200_decode_error(EINVAL);
    return q->notify[0];
}

static inline int d200_decode_drain_notifications(struct d200_decode_queue *q) {
    uint8_t bytes[256];
    int error;
    if (!q || !q->initialized) return d200_decode_error(EINVAL);
    error = pthread_mutex_lock(&q->mutex);
    if (error) return d200_decode_error(error);
    for (;;) {
        ssize_t count = read(q->notify[0], bytes, sizeof(bytes));
        if (count > 0) continue;
        if (count < 0 && errno == EINTR) continue;
        if (!count) error = EPIPE;
        else if (errno != EAGAIN && errno != EWOULDBLOCK) error = errno;
        break;
    }
    if (!error) error = q->notify_error;
    pthread_mutex_unlock(&q->mutex);
    return error ? d200_decode_error(error) : 0;
}
#endif
