"""Import-safe incremental framing for concatenated JPEG byte streams.

No decoding, device setup, or I/O is performed here. Consumers may supply their
own max_frame_bytes; the default preserves HID's 16 MiB per-frame limit.
"""

MAX_JPEG_FRAME_BYTES = 16777216  # 16 MiB


class JpegFramingError(ValueError):
    """Malformed, truncated, or oversized JPEG pipe output."""


class JpegFramer:
    """Incremental JPEG boundaries, not a pixel decoder.

    Segment lengths shield metadata from marker interpretation. Entropy bytes
    use FF00 stuffing and standalone restart markers; other markers return to
    segment parsing, including subsequent scans. Keep at most one bounded frame.
    Pillow's Parser buffers JPEG until close (load_read disables incremental
    decoding), and exposes no consumed-byte boundary for concatenated images.
    """

    def __init__(self, max_frame_bytes=MAX_JPEG_FRAME_BYTES):
        if not isinstance(max_frame_bytes, int) or max_frame_bytes < 4:
            raise ValueError('max_frame_bytes must be an integer >= 4')
        self.max_frame_bytes = max_frame_bytes
        self.data = bytearray()
        self.state = 'soi'
        self.marker = None
        self.remaining = 0
        self.saw_scan = False
        self.in_entropy = False

    def _error(self, message):
        return JpegFramingError(f'JPEG frame at byte {len(self.data)}: {message}')

    def feed(self, chunk):
        """Frame bytes/bytearray chunks; consume this iterator before feeding again."""
        offset = 0
        size = len(chunk)
        while offset < size:
            if len(self.data) >= self.max_frame_bytes:
                raise self._error(f'exceeds maximum {self.max_frame_bytes} bytes; '
                                  'reduce tile size or check ffmpeg output')
            if self.state == 'payload':
                count = min(self.remaining, size - offset)
                self.data.extend(chunk[offset:offset + count])
                offset += count
                self.remaining -= count
                if not self.remaining:
                    self._end_segment()
                continue
            if self.state == 'entropy' and offset + 1 < size:
                # Only marker-free bytes can bypass the marker state machine.
                end = min(size, offset + self.max_frame_bytes - len(self.data))
                marker = chunk.find(b'\xff', offset, end)
                stop = end if marker < 0 else marker + 1
                self.data.extend(chunk[offset:stop])
                offset = stop
                if marker >= 0:
                    self.state = 'marker_code'
                continue
            value = chunk[offset]
            offset += 1
            self.data.append(value)
            if self.state == 'soi':
                if value != 0xff:
                    raise self._error('expected SOI (FF D8), not non-JPEG output')
                self.state = 'soi_code'
            elif self.state == 'soi_code':
                if value != 0xd8:
                    raise self._error('expected SOI (FF D8)')
                self.state = 'marker_prefix'
            elif self.state == 'marker_prefix':
                if value != 0xff:
                    raise self._error('expected marker prefix FF')
                self.state = 'marker_code'
            elif self.state == 'entropy':
                if value == 0xff:
                    self.state = 'marker_code'
            elif self.state == 'marker_code':
                if value == 0xff:  # marker fill bytes
                    continue
                if value == 0x00 or 0xd0 <= value <= 0xd7:
                    if not self.in_entropy:
                        raise self._error('stuffing/restart marker outside entropy scan')
                    self.state = 'entropy'
                elif value == 0x01:  # standalone TEM
                    self.state = 'entropy' if self.in_entropy else 'marker_prefix'
                elif value == 0xd9:
                    if not self.saw_scan:
                        raise self._error('EOI before any SOS scan')
                    frame = bytes(self.data)
                    self.data.clear()
                    self.state = 'soi'
                    self.saw_scan = self.in_entropy = False
                    yield frame
                    # A caller may resize a bytearray while this iterator is paused.
                    size = len(chunk)
                elif value == 0xd8:
                    raise self._error('unexpected SOI before EOI')
                elif value < 0xc0:
                    raise self._error(f'invalid marker FF {value:02X}')
                else:
                    self.marker = value
                    # DNL can interrupt and then resume the current scan.
                    self.in_entropy = self.in_entropy and value == 0xdc
                    self.state = 'length_high'
            elif self.state == 'length_high':
                self.remaining = value << 8
                self.state = 'length_low'
            elif self.state == 'length_low':
                length = self.remaining + value
                if length < 2:
                    raise self._error(f'invalid segment length {length} for FF {self.marker:02X}')
                self.remaining = length - 2
                if len(self.data) + self.remaining > self.max_frame_bytes:
                    raise self._error(f'segment exceeds maximum {self.max_frame_bytes} bytes; '
                                      'reduce tile size or check ffmpeg output')
                self.state = 'payload'
                if not self.remaining:
                    self._end_segment()

    def _end_segment(self):
        if self.marker == 0xda:
            self.saw_scan = self.in_entropy = True
        self.state = 'entropy' if self.in_entropy else 'marker_prefix'

    def finish(self):
        if self.data:
            raise self._error(f'truncated output at EOF while reading {self.state}; '
                              'check ffmpeg input and encoder errors')
