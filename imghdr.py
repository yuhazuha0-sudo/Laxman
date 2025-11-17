# imghdr fallback using Pillow
from io import BytesIO
from PIL import Image

def what(file, h=None):
    """
    Minimal replacement for stdlib imghdr.what()
    Accepts either (fileobj) or (None, header_bytes) usage.
    Returns strings like 'jpeg', 'png' or None.
    """
    data = None
    # If header bytes passed as second arg
    if h is not None:
        data = h if isinstance(h, (bytes, bytearray)) else bytes(h)
    else:
        # try to read first bytes from file-like
        try:
            # file may be bytes or file-like
            if isinstance(file, (bytes, bytearray)):
                data = file
            else:
                pos = None
                try:
                    pos = file.tell()
                except Exception:
                    pos = None
                try:
                    data = file.read(512)
                except Exception:
                    data = None
                try:
                    if pos is not None:
                        file.seek(pos)
                except Exception:
                    pass
        except Exception:
            data = None

    if not data:
        return None

    try:
        img = Image.open(BytesIO(data))
        fmt = img.format
        if not fmt:
            return None
        fmt = fmt.lower()
        # map Pillow formats to imghdr names
        if fmt == 'jpeg':
            return 'jpeg'
        if fmt == 'png':
            return 'png'
        if fmt == 'gif':
            return 'gif'
        if fmt == 'bmp':
            return 'bmp'
        if fmt == 'webp':
            return 'webp'
        return fmt
    except Exception:
        return None
