import base64
import io
from PIL import Image, ImageOps

MAX_BYTES = 32 * 1024 * 1024

def image(value, *, source=False):
    if not isinstance(value, dict) or set(value) != {'mime_type', 'data'}:
        raise ValueError('invalid_image')
    if value['mime_type'] not in {'image/png', 'image/jpeg', 'image/webp'}:
        raise ValueError('invalid_image')
    if not isinstance(value['data'], str) or len(value['data']) > MAX_BYTES * 4 // 3 + 4:
        raise ValueError('invalid_image')
    data = base64.b64decode(value['data'], validate=True)
    if not data or len(data) > (10 * 1024 * 1024 if source else MAX_BYTES):
        raise ValueError('image_size_exceeded')
    try:
        with Image.open(io.BytesIO(data)) as im:
            if (im.format not in {'PNG', 'JPEG', 'WEBP'} or getattr(im, 'n_frames', 1) != 1
                    or im.width * im.height > 16 * 1024 * 1024 or Image.MIME[im.format] != value['mime_type']):
                raise ValueError('invalid_image')
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            width, height = ImageOps.exif_transpose(im).size
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError('image_integrity_failed') from error
    return {**value, 'width': width, 'height': height}
