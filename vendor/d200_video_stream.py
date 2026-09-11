"""Pure D2JF v1 and new D2PX v1 video codecs; no I/O or device effects.

Payloads remain explicit bytes. StreamState validates one ordered duplex hop;
callers own authentication, JPEG decoding, deadlines, transport and cleanup proof.
"""

from dataclasses import dataclass
import re
import struct

VERSION = 1
HEADER_SIZE = 40
CONTROL_HEADER_SIZE = 16
MAX_PAYLOAD = 65536
MAX_JPEG = 1048576
WINDOW_FRAMES = 2
WINDOW_BYTES = 2097152
UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1
ATTACH, READY, FRAME, CONSUMED, EOS, DONE, CANCEL, CANCELLED, ERROR = range(1, 10)
(VIDEO_OPEN_REQUEST, VIDEO_OPEN_RESULT, VIDEO_CANCEL_REQUEST,
 VIDEO_CANCEL_RESULT, VIDEO_STATUS_REQUEST, VIDEO_STATUS_RESULT) = range(21, 27)
(OK, BUSY, UNAUTHORIZED, UNSUPPORTED_VERSION, INVALID_PARAMETERS, START_FAILED,
 TIMEOUT, NOT_FOUND, CLEANUP_FAILED, PROTOCOL, PRESENTATION, SOURCE_FAILURE,
 DISCONNECTED, RESULT_CANCELLED, EMPTY_SOURCE, RESOURCE_LIMIT) = range(16)
(IDLE, OPENING, ATTACHING, STATE_READY, STREAMING, DRAINING, STATE_DONE,
 CANCELLING, STATE_CANCELLED, FAILED) = range(10)
PRODUCER = 'producer'
CONSUMER = 'consumer'
_RECORD = struct.Struct('>4sBBHI16sIQ')
_CONTROL = struct.Struct('>4sBBHII')
_CONTROL_LENGTHS = {21: 40, 22: 72, 23: 64, 24: 44, 25: 60, 26: 72}


class ProtocolError(ValueError):
    """A malformed value, identity, or stream transition."""


def uint(value, bits, name='integer', minimum=0, maximum=None):
    limit = (1 << bits) - 1 if maximum is None else maximum
    if type(value) is not int or not minimum <= value <= limit:
        raise ProtocolError(f'{name} outside unsigned {bits}-bit range')
    return value


def session_bytes(value):
    """Accept only 16 bytes or canonical 32-character lowercase hex."""
    if type(value) is bytes and len(value) == 16:
        return value
    if type(value) is str and re.fullmatch('[0-9a-f]{32}', value):
        return bytes.fromhex(value)
    raise ProtocolError('invalid session')


def capability_bytes(value):
    if type(value) is bytes and len(value) == 32:
        return value
    if type(value) is str and re.fullmatch('[0-9a-f]{64}', value):
        return bytes.fromhex(value)
    raise ProtocolError('invalid capability')


def validate_fps(numerator, denominator):
    return (uint(numerator, 32, 'fps numerator', 1),
            uint(denominator, 32, 'fps denominator', 1))


def _bytes(value):
    if type(value) is not bytes:
        raise ProtocolError('payload must be bytes')
    return value


def _code(value, nonzero=False):
    return uint(value, 32, 'result code', int(nonzero), 15)


def _count(value):
    return uint(value, 64, 'frame count/index', maximum=UINT64_MAX - 1)


def validate_record_payload(kind, payload, direction=None):
    uint(kind, 8, 'record kind', 1, 9)
    _bytes(payload)
    if direction not in (None, PRODUCER, CONSUMER):
        raise ProtocolError('invalid direction')
    allowed = {PRODUCER: (ATTACH, FRAME, EOS, CANCEL, ERROR),
               CONSUMER: (READY, CONSUMED, DONE, CANCELLED, ERROR)}
    if direction is not None and kind not in allowed[direction]:
        raise ProtocolError('wrong direction')
    lengths = {ATTACH: 32, READY: 24, CONSUMED: 8, EOS: 8,
               DONE: 20, CANCEL: 4, CANCELLED: 4}
    if kind in lengths and len(payload) != lengths[kind]:
        raise ProtocolError('wrong payload length')
    if kind == READY:
        n, d, w, j, b, p = struct.unpack('>6I', payload)
        validate_fps(n, d)
        if (w, j, b, p) != (WINDOW_FRAMES, MAX_JPEG, WINDOW_BYTES, MAX_PAYLOAD):
            raise ProtocolError('wrong READY limits')
    elif kind == FRAME:
        if not 17 <= len(payload) <= MAX_PAYLOAD:
            raise ProtocolError('wrong FRAME length')
        index, total, offset = struct.unpack_from('>QII', payload)
        _count(index)
        if not 1 <= total <= MAX_JPEG or offset >= total or len(payload) - 16 > total - offset:
            raise ProtocolError('invalid JPEG fragment bounds')
    elif kind in (CONSUMED, EOS):
        _count(struct.unpack('>Q', payload)[0])
    elif kind == DONE:
        total, submitted, code = struct.unpack('>QQI', payload)
        _count(total)
        _count(submitted)
        if total == 0 or submitted != total or code != OK:
            raise ProtocolError('invalid DONE totals/code')
    elif kind in (CANCEL, CANCELLED):
        if struct.unpack('>I', payload)[0] not in (SOURCE_FAILURE, RESULT_CANCELLED):
            raise ProtocolError('invalid cancellation reason')
    elif kind == ERROR:
        if not 4 <= len(payload) <= 256:
            raise ProtocolError('wrong ERROR length')
        _code(struct.unpack_from('>I', payload)[0], True)
        try:
            text = payload[4:].decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ProtocolError('invalid diagnostic UTF-8') from exc
        if '\0' in text:
            raise ProtocolError('NUL diagnostic')
    return payload


@dataclass(frozen=True)
class Record:
    kind: int
    session: bytes
    epoch: int
    sequence: int
    payload: bytes


def encode_record(kind, session, epoch, sequence, payload):
    validate_record_payload(kind, payload)
    session = session_bytes(session)
    uint(epoch, 32, 'epoch', 1, 1)
    uint(sequence, 64, 'record sequence')
    return _RECORD.pack(b'D2JF', VERSION, kind, 0, len(payload), session,
                        epoch, sequence) + payload


def decode_record_header(data):
    """Validate exactly 40 bytes before allocating payload; return header tuple.

    Result is (kind, payload_length, session_bytes, epoch, sequence).
    """
    _bytes(data)
    if len(data) != HEADER_SIZE:
        raise ProtocolError('wrong record header length')
    magic, version, kind, flags, length, session, epoch, sequence = _RECORD.unpack(data)
    if magic != b'D2JF' or version != VERSION or flags or not 1 <= kind <= 9:
        raise ProtocolError('invalid record header')
    if length > MAX_PAYLOAD or epoch != 1:
        raise ProtocolError('invalid record length/epoch')
    lengths = {1: (32, 32), 2: (24, 24), 3: (17, MAX_PAYLOAD),
               4: (8, 8), 5: (8, 8), 6: (20, 20), 7: (4, 4),
               8: (4, 4), 9: (4, 256)}
    low, high = lengths[kind]
    if not low <= length <= high:
        raise ProtocolError('wrong record payload length')
    return kind, length, session, epoch, sequence


def decode_record(data, *, direction=None):
    _bytes(data)
    kind, length, session, epoch, sequence = decode_record_header(data[:HEADER_SIZE])
    if len(data) != HEADER_SIZE + length:
        raise ProtocolError('wrong record length')
    payload = validate_record_payload(kind, data[HEADER_SIZE:], direction)
    return Record(kind, session, epoch, sequence, payload)


def _validate_control(kind, fields):
    uint(kind, 8, 'control kind', 21, 26)
    request = kind % 2 == 1
    names = {'generation', 'session', 'epoch'}
    if not request:
        names |= {'request_sequence', 'result_code'}
    names |= {21: {'fps_n', 'fps_d'}, 22: {'version', 'port', 'capability'},
              23: {'capability', 'reason'}, 24: {'state', 'reason'},
              25: {'capability'}, 26: {'state', 'frames_received', 'frames_consumed',
                                     'eos_total', 'terminal_reason', 'renderer_ready'}}[kind]
    if set(fields) != names:
        raise ProtocolError('wrong control fields')
    f = dict(fields)
    f['session'] = session_bytes(f['session'])
    uint(f['generation'], 64, 'generation')
    uint(f['epoch'], 32, 'epoch', 0, 1)
    if request:
        if f['epoch'] != (0 if kind == VIDEO_OPEN_REQUEST else 1):
            raise ProtocolError('wrong request epoch')
    else:
        uint(f['request_sequence'], 32, 'request sequence')
        _code(f['result_code'])
        if f['epoch'] != (0 if kind == VIDEO_OPEN_RESULT and f['result_code'] else 1):
            raise ProtocolError('wrong response epoch')
    if 'capability' in f:
        f['capability'] = capability_bytes(f['capability'])
    if kind == VIDEO_OPEN_REQUEST:
        validate_fps(f['fps_n'], f['fps_d'])
    elif kind == VIDEO_OPEN_RESULT:
        uint(f['version'], 8, 'version')
        uint(f['port'], 16, 'port')
        if f['result_code'] == OK:
            if f['version'] != VERSION or not f['port']:
                raise ProtocolError('invalid successful OPEN')
        elif f['version'] or f['port'] or any(f['capability']):
            raise ProtocolError('failed OPEN fields must be zero')
    elif kind == VIDEO_CANCEL_REQUEST:
        if type(f['reason']) is not int or f['reason'] not in (11, 13):
            raise ProtocolError('invalid cancellation reason')
    elif kind in (VIDEO_CANCEL_RESULT, VIDEO_STATUS_RESULT):
        uint(f['state'], 32, 'state', 0, 9)
        _code(f['reason'] if kind == VIDEO_CANCEL_RESULT else f['terminal_reason'])
        if kind == VIDEO_STATUS_RESULT:
            _count(f['frames_received'])
            _count(f['frames_consumed'])
            uint(f['eos_total'], 64, 'EOS total')
            uint(f['renderer_ready'], 32, 'renderer ready', 0, 1)
        extras = names - {'generation', 'session', 'epoch', 'request_sequence', 'result_code'}
        if f['result_code'] and any(f[key] for key in extras):
            raise ProtocolError('failed response fields must be zero')
        if not f['result_code']:
            if kind == VIDEO_CANCEL_RESULT and f['state'] not in (6, 8, 9):
                raise ProtocolError('cancel result is not terminal')
            if kind == VIDEO_STATUS_RESULT:
                received, consumed, eos = f['frames_received'], f['frames_consumed'], f['eos_total']
                if consumed > received or (eos != UINT64_MAX and eos != received):
                    raise ProtocolError('invalid status counts')
                if f['state'] == STATE_DONE and (not received or eos != consumed or f['terminal_reason'] != OK):
                    raise ProtocolError('invalid DONE status')
    return f


def encode_control(kind, sequence, **fields):
    """Encode only kinds 21..26; exact field names are those from decode_control."""
    uint(sequence, 32, 'control sequence')
    f = _validate_control(kind, fields)
    if kind % 2:
        payload = struct.pack('>Q16sI', f['generation'], f['session'], f['epoch'])
    else:
        payload = struct.pack('>QI16sII', f['generation'], f['request_sequence'],
                              f['session'], f['epoch'], f['result_code'])
    if kind == 21:
        payload += struct.pack('>B3xII', VERSION, f['fps_n'], f['fps_d'])
    elif kind == 22:
        payload += struct.pack('>BxH32s', f['version'], f['port'], f['capability'])
    elif kind == 23:
        payload += struct.pack('>32sI', f['capability'], f['reason'])
    elif kind == 24:
        payload += struct.pack('>II', f['state'], f['reason'])
    elif kind == 25:
        payload += f['capability']
    else:
        payload += struct.pack('>IQQQII', f['state'], f['frames_received'],
                               f['frames_consumed'], f['eos_total'],
                               f['terminal_reason'], f['renderer_ready'])
    return _CONTROL.pack(b'D2PX', VERSION, kind, 0, len(payload), sequence) + payload


def decode_control(data):
    """Return (kind, header_sequence, exact_fields_dict), rejecting old chunk IDs."""
    _bytes(data)
    if len(data) < CONTROL_HEADER_SIZE:
        raise ProtocolError('short control header')
    magic, version, kind, flags, length, sequence = _CONTROL.unpack_from(data)
    if (magic != b'D2PX' or version != VERSION or flags or
            kind not in _CONTROL_LENGTHS or length != _CONTROL_LENGTHS[kind] or
            len(data) != CONTROL_HEADER_SIZE + length):
        raise ProtocolError('invalid control envelope')
    p = data[CONTROL_HEADER_SIZE:]
    if kind % 2:
        f = dict(zip(('generation', 'session', 'epoch'), struct.unpack_from('>Q16sI', p)))
    else:
        f = dict(zip(('generation', 'request_sequence', 'session', 'epoch', 'result_code'),
                     struct.unpack_from('>QI16sII', p)))
    if kind == 21:
        if p[28:32] != b'\x01\0\0\0':
            raise ProtocolError('invalid OPEN version/reserved')
        f.update(zip(('fps_n', 'fps_d'), struct.unpack_from('>II', p, 32)))
    elif kind == 22:
        if p[37]:
            raise ProtocolError('nonzero reserved')
        f.update(zip(('version', 'port', 'capability'), struct.unpack_from('>BxH32s', p, 36)))
    elif kind == 23:
        f.update(zip(('capability', 'reason'), struct.unpack_from('>32sI', p, 28)))
    elif kind == 24:
        f.update(zip(('state', 'reason'), struct.unpack_from('>II', p, 36)))
    elif kind == 25:
        f['capability'] = p[28:60]
    else:
        f.update(zip(('state', 'frames_received', 'frames_consumed', 'eos_total',
                      'terminal_reason', 'renderer_ready'), struct.unpack_from('>IQQQII', p, 36)))
    return kind, sequence, _validate_control(kind, f)


def validate_status(status):
    """Validate and copy the exact public local-JSON STATUS object."""
    names = {'state', 'rendererReady', 'framesReceived', 'framesConsumed',
             'eosTotal', 'terminalCode', 'cleanup', 'cancelPhase'}
    if type(status) is not dict or set(status) != names:
        raise ProtocolError('wrong status fields')
    s = dict(status)
    uint(s['state'], 32, 'state', 0, 9)
    if type(s['rendererReady']) is not bool:
        raise ProtocolError('rendererReady must be boolean')
    _count(s['framesReceived'])
    _count(s['framesConsumed'])
    if s['eosTotal'] is not None:
        _count(s['eosTotal'])
        if s['eosTotal'] != s['framesReceived']:
            raise ProtocolError('EOS does not match received count')
    _code(s['terminalCode'])
    if s['cleanup'] not in ('pending', 'proven', 'unproven'):
        raise ProtocolError('invalid cleanup')
    if s['cancelPhase'] not in ('none', 'requested', 'delivery_unknown',
                                'delivered', 'proven', 'unproven'):
        raise ProtocolError('invalid cancel phase')
    if s['framesConsumed'] > s['framesReceived']:
        raise ProtocolError('consumed exceeds received')
    if s['state'] == STATE_DONE and (s['terminalCode'] != OK or
            s['cleanup'] != 'proven' or not s['framesReceived'] or
            s['eosTotal'] != s['framesConsumed']):
        raise ProtocolError('unproven DONE')
    if s['cancelPhase'] == 'proven' and s['cleanup'] != 'proven':
        raise ProtocolError('unproven cancellation cleanup')
    return s


class StreamState:
    """Pure ordered-hop validator. Invalid input permanently fails this state.

    accept(record, direction) checks identities, contiguous per-direction sequence,
    attachment/READY order, W2 credits, fragment continuity and terminal totals.
    No JPEG is buffered. External hops require the expected capability; inherited
    child hops pass capability=None. Cleanup/authentication beyond ATTACH is owned
    by the caller, not inferred from this validator.
    """

    def __init__(self, session, fps_n, fps_d, *, capability=None):
        self.session = session_bytes(session)
        self.fps = validate_fps(fps_n, fps_d)
        self.capability = None if capability is None else capability_bytes(capability)
        self.attached = capability is None
        self.ready = False
        self.terminal = False
        self.cancel_reason = None
        self.sequences = {PRODUCER: 0, CONSUMER: 0}
        self.received = 0
        self.consumed = 0
        self.partial_total = 0
        self.partial_offset = 0
        self.eos = None

    def accept(self, record, direction):
        try:
            self._accept(record, direction)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            self.terminal = True
            if isinstance(exc, ProtocolError):
                raise
            raise ProtocolError('invalid record/state') from exc
        return record

    def _accept(self, r, direction):
        validate_record_payload(r.kind, r.payload, direction)
        if direction not in self.sequences or self.terminal:
            raise ProtocolError('terminal or invalid direction')
        if (r.session != self.session or type(r.epoch) is not int or r.epoch != 1 or
                type(r.sequence) is not int or r.sequence != self.sequences[direction] or
                r.sequence > UINT64_MAX):
            raise ProtocolError('wrong identity/sequence')
        k, p = r.kind, r.payload
        if k == ERROR:
            self.terminal = True
        elif not self.attached:
            if k != ATTACH or p != self.capability:
                raise ProtocolError('expected authenticated ATTACH')
            self.attached = True
        elif k == ATTACH:
            raise ProtocolError('duplicate ATTACH')
        elif k == CANCEL:
            if self.cancel_reason is not None:
                raise ProtocolError('duplicate CANCEL')
            self.cancel_reason = struct.unpack('>I', p)[0]
        elif k == CANCELLED:
            if self.cancel_reason != struct.unpack('>I', p)[0]:
                raise ProtocolError('unexpected CANCELLED')
            self.terminal = True
        elif self.cancel_reason is not None:
            raise ProtocolError('media after CANCEL')
        elif k == READY:
            if self.ready or struct.unpack_from('>II', p) != self.fps:
                raise ProtocolError('duplicate or mismatched READY')
            self.ready = True
        elif not self.ready:
            raise ProtocolError('record before READY')
        elif k == FRAME:
            index, total, offset = struct.unpack_from('>QII', p)
            if (self.eos is not None or index != self.received or
                    self.received - self.consumed >= WINDOW_FRAMES or
                    offset != self.partial_offset or
                    (self.partial_total and total != self.partial_total) or
                    self.received >= UINT64_MAX - 1):
                raise ProtocolError('unordered/uncredited frame')
            self.partial_total = total
            self.partial_offset += len(p) - 16
            if self.partial_offset == total:
                self.received += 1
                self.partial_total = self.partial_offset = 0
        elif k == CONSUMED:
            index = struct.unpack('>Q', p)[0]
            if index != self.consumed or index >= self.received:
                raise ProtocolError('duplicate/future CONSUMED')
            self.consumed += 1
        elif k == EOS:
            count = struct.unpack('>Q', p)[0]
            if self.eos is not None or self.partial_total or count != self.received:
                raise ProtocolError('invalid EOS')
            self.eos = count
        elif k == DONE:
            total, submitted, _ = struct.unpack('>QQI', p)
            if self.eos != total or self.consumed != submitted:
                raise ProtocolError('premature DONE')
            self.terminal = True
        self.sequences[direction] += 1

    def request_cancel(self, reason):
        """Record authenticated control cancellation without a data sequence."""
        if (type(reason) is not int or reason not in (11, 13) or self.terminal or
                self.cancel_reason not in (None, reason)):
            self.terminal = True
            raise ProtocolError('invalid control cancellation')
        self.cancel_reason = reason

    def finish(self):
        """EOF is valid only after a terminal record (including explicit failure)."""
        if not self.terminal:
            self.terminal = True
            raise ProtocolError('EOF before terminal record')
