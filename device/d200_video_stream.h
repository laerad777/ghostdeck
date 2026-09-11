/* Pure portable D2JF/D2PX video byte codecs. No native packed structs or I/O.
 * Functions return 1 on success, 0 on invalid input. Encode destinations must
 * have the documented capacity. Integer arguments are unsigned wire values;
 * callers parsing text/signed values must range-check before narrowing.
 */
#ifndef D200_VIDEO_STREAM_H
#define D200_VIDEO_STREAM_H
#include <stdint.h>
#include <stddef.h>
#include <string.h>

#define D200_VS_VERSION 1u
#define D200_VS_HEADER_SIZE 40u
#define D200_VS_CONTROL_HEADER_SIZE 16u
#define D200_VS_MAX_PAYLOAD 65536u
#define D200_VS_MAX_JPEG 1048576u
#define D200_VS_WINDOW_FRAMES 2u
#define D200_VS_WINDOW_BYTES 2097152u
#define D200_VS_PRODUCER 1u
#define D200_VS_CONSUMER 2u
#define D200_VS_ATTACH 1u
#define D200_VS_READY 2u
#define D200_VS_FRAME 3u
#define D200_VS_CONSUMED 4u
#define D200_VS_EOS 5u
#define D200_VS_DONE 6u
#define D200_VS_CANCEL 7u
#define D200_VS_CANCELLED 8u
#define D200_VS_ERROR 9u
#define D200_VS_VIDEO_OPEN_REQUEST 21u
#define D200_VS_VIDEO_OPEN_RESULT 22u
#define D200_VS_VIDEO_CANCEL_REQUEST 23u
#define D200_VS_VIDEO_CANCEL_RESULT 24u
#define D200_VS_VIDEO_STATUS_REQUEST 25u
#define D200_VS_VIDEO_STATUS_RESULT 26u
#define D200_VS_OK 0u
#define D200_VS_BUSY 1u
#define D200_VS_UNAUTHORIZED 2u
#define D200_VS_UNSUPPORTED_VERSION 3u
#define D200_VS_INVALID_PARAMETERS 4u
#define D200_VS_START_FAILED 5u
#define D200_VS_TIMEOUT 6u
#define D200_VS_NOT_FOUND 7u
#define D200_VS_CLEANUP_FAILED 8u
#define D200_VS_PROTOCOL 9u
#define D200_VS_PRESENTATION 10u
#define D200_VS_SOURCE_FAILURE 11u
#define D200_VS_DISCONNECTED 12u
#define D200_VS_RESULT_CANCELLED 13u
#define D200_VS_EMPTY_SOURCE 14u
#define D200_VS_RESOURCE_LIMIT 15u
#define D200_VS_IDLE 0u
#define D200_VS_OPENING 1u
#define D200_VS_ATTACHING 2u
#define D200_VS_STATE_READY 3u
#define D200_VS_STREAMING 4u
#define D200_VS_DRAINING 5u
#define D200_VS_STATE_DONE 6u
#define D200_VS_CANCELLING 7u
#define D200_VS_STATE_CANCELLED 8u
#define D200_VS_FAILED 9u

static inline uint16_t d200_vs_get_u16(const uint8_t *p) {
    return (uint16_t)(((uint16_t)p[0] << 8) | p[1]);
}
static inline uint32_t d200_vs_get_u32(const uint8_t *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) | p[3];
}
static inline uint64_t d200_vs_get_u64(const uint8_t *p) {
    return ((uint64_t)d200_vs_get_u32(p) << 32) | d200_vs_get_u32(p + 4);
}
static inline void d200_vs_put_u16(uint8_t *p, uint16_t v) {
    p[0] = (uint8_t)(v >> 8); p[1] = (uint8_t)v;
}
static inline void d200_vs_put_u32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)(v >> 24); p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8); p[3] = (uint8_t)v;
}
static inline void d200_vs_put_u64(uint8_t *p, uint64_t v) {
    d200_vs_put_u32(p, (uint32_t)(v >> 32));
    d200_vs_put_u32(p + 4, (uint32_t)v);
}
static inline int d200_vs_zero(const uint8_t *p, size_t n) {
    size_t i;
    for (i = 0; i < n; ++i) if (p[i]) return 0;
    return 1;
}
static inline int d200_vs_fps(uint32_t n, uint32_t d) { return n && d; }
static inline int d200_vs_reason(uint32_t r) { return r == 11 || r == 13; }
static inline int d200_vs_record_length(uint8_t k, uint32_t n) {
    switch (k) {
    case 1: return n == 32;
    case 2: return n == 24;
    case 3: return n >= 17 && n <= D200_VS_MAX_PAYLOAD;
    case 4: case 5: return n == 8;
    case 6: return n == 20;
    case 7: case 8: return n == 4;
    case 9: return n >= 4 && n <= 256;
    default: return 0;
    }
}
/* Strict Unicode scalar UTF-8, with no embedded NUL. */
static inline int d200_vs_utf8(const uint8_t *p, size_t n) {
    size_t i = 0;
    while (i < n) {
        uint32_t c = p[i++], minimum;
        unsigned remaining;
        if (!c) return 0;
        if (c < 128) continue;
        if (c >= 0xc2 && c <= 0xdf) { c &= 31; remaining = 1; minimum = 128; }
        else if (c >= 0xe0 && c <= 0xef) { c &= 15; remaining = 2; minimum = 2048; }
        else if (c >= 0xf0 && c <= 0xf4) { c &= 7; remaining = 3; minimum = 65536; }
        else return 0;
        if (n - i < remaining) return 0;
        while (remaining--) {
            if ((p[i] & 0xc0) != 0x80) return 0;
            c = (c << 6) | (p[i++] & 63);
        }
        if (c < minimum || c > 0x10ffff || (c >= 0xd800 && c <= 0xdfff)) return 0;
    }
    return 1;
}
static inline int d200_vs_validate_payload(uint8_t k, const uint8_t *p,
                                           uint32_t n, unsigned direction) {
    uint64_t a, b;
    uint32_t total, offset;
    if (!p || !d200_vs_record_length(k, n) || direction > 2) return 0;
    if (direction == D200_VS_PRODUCER && k != 1 && k != 3 && k != 5 && k != 7 && k != 9) return 0;
    if (direction == D200_VS_CONSUMER && k != 2 && k != 4 && k != 6 && k != 8 && k != 9) return 0;
    switch (k) {
    case 2:
        return d200_vs_fps(d200_vs_get_u32(p), d200_vs_get_u32(p + 4)) &&
            d200_vs_get_u32(p + 8) == 2 && d200_vs_get_u32(p + 12) == D200_VS_MAX_JPEG &&
            d200_vs_get_u32(p + 16) == D200_VS_WINDOW_BYTES && d200_vs_get_u32(p + 20) == D200_VS_MAX_PAYLOAD;
    case 3:
        total = d200_vs_get_u32(p + 8); offset = d200_vs_get_u32(p + 12);
        return d200_vs_get_u64(p) != UINT64_MAX && total && total <= D200_VS_MAX_JPEG &&
            offset < total && n - 16 <= total - offset;
    case 4: case 5: return d200_vs_get_u64(p) != UINT64_MAX;
    case 6:
        a = d200_vs_get_u64(p); b = d200_vs_get_u64(p + 8);
        return a && a != UINT64_MAX && a == b && !d200_vs_get_u32(p + 16);
    case 7: case 8: return d200_vs_reason(d200_vs_get_u32(p));
    case 9: return d200_vs_get_u32(p) >= 1 && d200_vs_get_u32(p) <= 15 && d200_vs_utf8(p + 4, n - 4);
    default: return 1;
    }
}

typedef struct {
    uint8_t kind, session[16];
    uint32_t payload_length, epoch;
    uint64_t sequence;
} d200_vs_header;

static inline int d200_vs_decode_header(const uint8_t *p, size_t n, d200_vs_header *h) {
    if (!p || !h || n != 40 || memcmp(p, "D2JF", 4) || p[4] != 1 ||
        p[6] || p[7] || d200_vs_get_u32(p + 28) != 1 ||
        !d200_vs_record_length(p[5], d200_vs_get_u32(p + 8))) return 0;
    h->kind = p[5]; h->payload_length = d200_vs_get_u32(p + 8);
    memcpy(h->session, p + 12, 16); h->epoch = 1; h->sequence = d200_vs_get_u64(p + 32);
    return 1;
}
static inline int d200_vs_encode_header(uint8_t *p, size_t capacity, const d200_vs_header *h) {
    if (!p || !h || capacity < 40 || h->epoch != 1 || !d200_vs_record_length(h->kind, h->payload_length)) return 0;
    memcpy(p, "D2JF", 4); p[4] = 1; p[5] = h->kind; p[6] = p[7] = 0;
    d200_vs_put_u32(p + 8, h->payload_length); memcpy(p + 12, h->session, 16);
    d200_vs_put_u32(p + 28, h->epoch); d200_vs_put_u64(p + 32, h->sequence);
    return 1;
}
/* Decode requires an exact complete record; payload remains in caller memory. */
static inline int d200_vs_decode_record(const uint8_t *p, size_t n, unsigned direction,
                                        d200_vs_header *h) {
    return n >= 40 && d200_vs_decode_header(p, 40, h) && n - 40 == h->payload_length &&
        d200_vs_validate_payload(h->kind, p + 40, h->payload_length, direction);
}
static inline int d200_vs_encode_record(uint8_t *out, size_t capacity,
                                        const d200_vs_header *h, const uint8_t *payload) {
    if (!out || !h || h->epoch != 1 || capacity < 40 || capacity - 40 < h->payload_length ||
        !d200_vs_validate_payload(h->kind, payload, h->payload_length, 0)) return 0;
    memmove(out + 40, payload, h->payload_length);
    return d200_vs_encode_header(out, capacity, h);
}
static inline uint32_t d200_vs_control_length(uint8_t kind) {
    switch (kind) {
    case 21: return 40; case 22: return 72; case 23: return 64;
    case 24: return 44; case 25: return 60; case 26: return 72;
    default: return 0;
    }
}
/* Control fields are validated in-place, preserving exact Q/R byte offsets. */
static inline int d200_vs_validate_control_payload(uint8_t k, const uint8_t *p, size_t n) {
    uint32_t result, state, reason;
    uint64_t received, consumed, eos;
    if (!p || !d200_vs_control_length(k) || n != d200_vs_control_length(k)) return 0;
    if (k & 1) {
        if (d200_vs_get_u32(p + 24) != (k == 21 ? 0u : 1u)) return 0;
        if (k == 21) return p[28] == 1 && d200_vs_zero(p + 29, 3) &&
            d200_vs_fps(d200_vs_get_u32(p + 32), d200_vs_get_u32(p + 36));
        if (k == 23) return d200_vs_reason(d200_vs_get_u32(p + 60));
        return 1;
    }
    result = d200_vs_get_u32(p + 32);
    if (result > 15 || d200_vs_get_u32(p + 28) != (k == 22 && result ? 0u : 1u)) return 0;
    if (result) return d200_vs_zero(p + 36, n - 36);
    if (k == 22) return p[36] == 1 && !p[37] && d200_vs_get_u16(p + 38);
    state = d200_vs_get_u32(p + 36);
    reason = d200_vs_get_u32(p + (k == 24 ? 40 : 64));
    if (state > 9 || reason > 15) return 0;
    if (k == 24) return state == 6 || state == 8 || state == 9;
    received = d200_vs_get_u64(p + 40); consumed = d200_vs_get_u64(p + 48);
    eos = d200_vs_get_u64(p + 56);
    if (received == UINT64_MAX || consumed == UINT64_MAX || consumed > received ||
        (eos != UINT64_MAX && eos != received) || d200_vs_get_u32(p + 68) > 1) return 0;
    return state != 6 || (received && eos == consumed && !reason);
}
static inline int d200_vs_decode_control(const uint8_t *p, size_t n,
                                         uint8_t *kind, uint32_t *sequence) {
    uint32_t length;
    if (!p || !kind || !sequence || n < 16 || memcmp(p, "D2PX", 4) ||
        p[4] != 1 || p[6] || p[7]) return 0;
    length = d200_vs_get_u32(p + 8);
    if (n - 16 != length || !d200_vs_validate_control_payload(p[5], p + 16, length)) return 0;
    *kind = p[5]; *sequence = d200_vs_get_u32(p + 12);
    return 1;
}
static inline int d200_vs_encode_control(uint8_t *out, size_t capacity, uint8_t kind,
                                         uint32_t sequence, const uint8_t *payload, size_t n) {
    if (!out || capacity < 16 || capacity - 16 < n ||
        !d200_vs_validate_control_payload(kind, payload, n)) return 0;
    memmove(out + 16, payload, n);
    memcpy(out, "D2PX", 4); out[4] = 1; out[5] = kind; out[6] = out[7] = 0;
    d200_vs_put_u32(out + 8, (uint32_t)n); d200_vs_put_u32(out + 12, sequence);
    return 1;
}

/* Pure duplex-hop ordering and credit state; no JPEG copies or I/O. */
typedef struct {
    uint8_t session[16], capability[32];
    unsigned attached, ready, terminal, cancel_reason, exhausted[2], has_eos;
    uint32_t fps_n, fps_d, partial_total, partial_offset;
    uint64_t sequence[2], received, consumed, eos;
} d200_vs_state;
static inline int d200_vs_state_init(d200_vs_state *s, const uint8_t session[16],
                                      uint32_t fps_n, uint32_t fps_d, const uint8_t *capability) {
    if (!s || !session || !d200_vs_fps(fps_n, fps_d)) return 0;
    memset(s, 0, sizeof(*s)); memcpy(s->session, session, 16);
    s->fps_n = fps_n; s->fps_d = fps_d; s->attached = capability == NULL;
    if (capability) memcpy(s->capability, capability, 32);
    return 1;
}
static inline int d200_vs_state_accept(d200_vs_state *s, const d200_vs_header *h,
                                        const uint8_t *p, unsigned direction) {
    unsigned slot;
    uint64_t index;
    uint32_t total, offset;
    if (!s) return 0;
    if (!h || direction < 1 || direction > 2 || s->terminal) goto invalid;
    slot = direction - 1;
    if (h->epoch != 1 || memcmp(h->session, s->session, 16) || s->exhausted[slot] ||
        h->sequence != s->sequence[slot] ||
        !d200_vs_validate_payload(h->kind, p, h->payload_length, direction)) goto invalid;
    if (h->kind == 9) s->terminal = 1;
    else if (!s->attached) {
        if (h->kind != 1 || memcmp(p, s->capability, 32)) goto invalid;
        s->attached = 1;
    } else if (h->kind == 1) goto invalid;
    else if (h->kind == 7) {
        if (s->cancel_reason) goto invalid;
        s->cancel_reason = d200_vs_get_u32(p);
    } else if (h->kind == 8) {
        if (s->cancel_reason != d200_vs_get_u32(p)) goto invalid;
        s->terminal = 1;
    } else if (s->cancel_reason) goto invalid;
    else if (h->kind == 2) {
        if (s->ready || d200_vs_get_u32(p) != s->fps_n || d200_vs_get_u32(p + 4) != s->fps_d) goto invalid;
        s->ready = 1;
    } else if (!s->ready) goto invalid;
    else if (h->kind == 3) {
        index = d200_vs_get_u64(p); total = d200_vs_get_u32(p + 8); offset = d200_vs_get_u32(p + 12);
        if (s->has_eos || index != s->received || s->received - s->consumed >= 2 ||
            offset != s->partial_offset || (s->partial_total && total != s->partial_total) ||
            s->received >= UINT64_MAX - 1) goto invalid;
        s->partial_total = total; s->partial_offset += h->payload_length - 16;
        if (s->partial_offset == total) {
            ++s->received; s->partial_total = s->partial_offset = 0;
        }
    } else if (h->kind == 4) {
        if (d200_vs_get_u64(p) != s->consumed || s->consumed >= s->received) goto invalid;
        ++s->consumed;
    } else if (h->kind == 5) {
        if (s->has_eos || s->partial_total || d200_vs_get_u64(p) != s->received) goto invalid;
        s->has_eos = 1; s->eos = s->received;
    } else if (h->kind == 6) {
        if (!s->has_eos || s->eos != d200_vs_get_u64(p) || s->consumed != d200_vs_get_u64(p + 8)) goto invalid;
        s->terminal = 1;
    }
    if (s->sequence[slot] == UINT64_MAX) s->exhausted[slot] = 1;
    else ++s->sequence[slot];
    return 1;
invalid:
    s->terminal = 1;
    return 0;
}
static inline int d200_vs_state_cancel(d200_vs_state *s, uint32_t reason) {
    if (!s) return 0;
    if (!d200_vs_reason(reason) || s->terminal || (s->cancel_reason && s->cancel_reason != reason)) {
        s->terminal = 1;
        return 0;
    }
    s->cancel_reason = reason;
    return 1;
}
static inline int d200_vs_state_finish(d200_vs_state *s) {
    if (!s) return 0;
    if (s->terminal) return 1;
    s->terminal = 1;
    return 0;
}
#endif
