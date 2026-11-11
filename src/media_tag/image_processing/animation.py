"""Exact, full-canvas APNG output without frame deduplication or timing rounding."""
from dataclasses import dataclass
from fractions import Fraction
from io import BytesIO
import struct
import warnings
import zlib

from PIL import Image, ImageOps

PNG = b'\x89PNG\r\n\x1a\n'


@dataclass(frozen=True)
class Decoded:
    frames: tuple
    delays: tuple[Fraction | None, ...]
    roles: tuple[str, ...]
    plays: int | None
    poster: bool
    source_format: str
    orientation: int | None
    icc_profile: bytes | None = None


def png_controls(raw):
    """Read exact APNG rational delays and play count, verifying chunk integrity."""
    if not raw.startswith(PNG):
        return None
    offset, delays, plays, saw_idat, poster = 8, [], None, False, False
    while offset < len(raw):
        if offset+12 > len(raw):
            raise ValueError('truncated_png_chunk')
        length = struct.unpack('>I', raw[offset:offset+4])[0]
        kind = raw[offset+4:offset+8]
        end = offset+12+length
        if end > len(raw):
            raise ValueError('truncated_png_chunk')
        data = raw[offset+8:offset+8+length]
        crc = struct.unpack('>I', raw[offset+8+length:end])[0]
        if zlib.crc32(kind+data) & 0xffffffff != crc:
            raise ValueError('png_chunk_integrity')
        if kind == b'acTL':
            if len(data) != 8 or plays is not None:
                raise ValueError('invalid_animation_control')
            _, plays = struct.unpack('>II', data)
        elif kind == b'fcTL':
            if len(data) != 26:
                raise ValueError('invalid_frame_control')
            if not delays:
                poster = saw_idat
            numerator, denominator = struct.unpack('>HH', data[20:24])
            if numerator == 0:
                raise ValueError('zero_frame_delay_requires_explicit_timing_policy')
            delays.append(Fraction(numerator, denominator or 100))
        elif kind == b'IDAT':
            saw_idat = True
        offset = end
        if kind == b'IEND':
            break
    return (tuple(delays), plays, poster) if plays is not None else None


def decode(raw, limits):
    controls = png_controls(raw)
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(BytesIO(raw)) as image:
            source_format = image.format
            if source_format not in ('PNG', 'JPEG', 'GIF', 'WEBP', 'BMP', 'TIFF'):
                raise ValueError('unsupported_image_format')
            count = getattr(image, 'n_frames', 1)
            if count > limits.max_frames:
                raise ValueError('frame_budget')
            animated = count > 1 or controls is not None
            if animated and source_format not in ('PNG', 'GIF', 'WEBP'):
                raise ValueError('multipage_image_is_not_supported_animation')
            orientation = image.getexif().get(274)
            orientation = orientation if type(orientation) is int and 1 <= orientation <= 8 else None
            icc = image.info.get('icc_profile')
            if icc is not None and (not isinstance(icc, bytes) or len(icc) > 4*1024**2
                    or len(icc) < 128 or icc[16:20] != b'RGB ' or icc[36:40] != b'acsp'):
                raise ValueError('unsupported_or_invalid_rgb_color_profile')
            poster = bool(controls and controls[2])
            if controls and len(controls[0]) != count-int(poster):
                raise ValueError('animation_frame_control_mismatch')
            if controls:
                plays = controls[1]
            elif animated and source_format == 'GIF':
                # GIF stores repetitions after the first play; APNG stores plays.
                loop = image.info.get('loop')
                plays = 1 if loop is None else 0 if loop == 0 else int(loop)+1
            elif animated:
                plays = int(image.info.get('loop', 1))
            else:
                plays = None
            frames, delays, roles, total = [], [], [], 0
            size = None
            for index in range(count):
                image.seek(index)
                if image.mode not in ('1', 'L', 'LA', 'P', 'RGB', 'RGBA'):
                    raise ValueError('high_bit_depth_or_color_conversion_requires_explicit_policy')
                if image.width*image.height > limits.max_pixels:
                    raise ValueError('pixel_budget')
                total += image.width*image.height
                if total > limits.max_total_pixels:
                    raise ValueError('total_pixel_budget')
                frame = ImageOps.exif_transpose(image.copy()).convert('RGBA')
                if size is not None and frame.size != size:
                    raise ValueError('variable_display_canvas_not_supported')
                size = frame.size
                role = 'poster' if poster and index == 0 else 'animation' if animated else 'still'
                if role in ('poster', 'still'):
                    delay = None
                elif controls:
                    delay = controls[0][index-int(poster)]
                else:
                    milliseconds = image.info.get('duration')
                    if not isinstance(milliseconds, (int, float)) or milliseconds <= 0:
                        raise ValueError('missing_positive_frame_timing')
                    delay = Fraction(str(milliseconds))/1000
                if delay is not None and (delay.numerator > 65535 or delay.denominator > 65535):
                    raise ValueError('frame_delay_not_exactly_encodable')
                frames.append(frame); delays.append(delay); roles.append(role)
            return Decoded(tuple(frames), tuple(delays), tuple(roles), plays, poster, source_format, orientation, icc)


def _chunk(kind, data):
    return struct.pack('>I', len(data))+kind+data+struct.pack('>I', zlib.crc32(kind+data) & 0xffffffff)


def encode(frames, decoded, output_format, max_output_bytes):
    if output_format == 'png' and any(role != 'still' for role in decoded.roles):
        raise ValueError('animation_requires_explicit_apng_output')
    if output_format == 'apng' and all(role == 'still' for role in decoded.roles):
        raise ValueError('still_input_requires_png_output')
    width, height = frames[0].size
    output = BytesIO()
    def write(data):
        if output.tell()+len(data) > max_output_bytes:
            raise ValueError('output_byte_budget')
        output.write(data)
    write(PNG)
    write(_chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)))
    if decoded.icc_profile is not None:
        write(_chunk(b'iCCP', b'Preserved RGB profile\0\0'+zlib.compress(decoded.icc_profile)))
    animated = output_format == 'apng'
    if animated:
        write(_chunk(b'acTL', struct.pack('>II', len(frames)-int(decoded.poster), decoded.plays)))
    sequence = 0
    for index, frame in enumerate(frames):
        if animated and decoded.roles[index] != 'poster':
            delay = decoded.delays[index]
            write(_chunk(b'fcTL', struct.pack('>IIIIIHHBB', sequence, width, height, 0, 0,
                delay.numerator, delay.denominator, 0, 0)))
            sequence += 1
        pixels = frame.tobytes()
        stride = width*4
        compressed = zlib.compress(b''.join(b'\0'+pixels[offset:offset+stride] for offset in range(0, len(pixels), stride)))
        if index == 0:
            write(_chunk(b'IDAT', compressed))
        else:
            write(_chunk(b'fdAT', struct.pack('>I', sequence)+compressed))
            sequence += 1
    write(_chunk(b'IEND', b''))
    return output.getvalue()
