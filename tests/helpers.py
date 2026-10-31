"""Small fake photos for the tests: real enough for Keeper to read, made on the fly."""
import hashlib
import io
import os
import struct

from PIL import Image

XMP_APP1 = b"http://ns.adobe.com/xap/1.0/\x00"


def make_jpeg(path, taken=None, xmp_rating=None, size=(48, 32), subsec=None):
    """A JPEG, optionally with EXIF Date Taken ("YYYY:MM:DD HH:MM:SS", plus
    SubSecTimeOriginal digits) and an in-camera XMP rating like the one Nikon
    cameras write."""
    exif = Image.Exif()
    if taken:
        exif.get_ifd(0x8769)[0x9003] = taken
    if subsec:
        exif.get_ifd(0x8769)[0x9291] = subsec
    buf = io.BytesIO()
    Image.new("RGB", size, (90, 120, 160)).save(buf, "JPEG", exif=exif.tobytes())
    data = buf.getvalue()
    if xmp_rating is not None:
        packet = (b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>'
                  b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                  b'<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
                  b'<xmp:Rating>%d</xmp:Rating></rdf:Description></rdf:RDF></x:xmpmeta>'
                  % xmp_rating + b" " * 200 + b'<?xpacket end="w"?>')
        seg = XMP_APP1 + packet
        data = data[:2] + b"\xff\xe1" + struct.pack(">H", len(seg) + 2) + seg + data[2:]
    with open(path, "wb") as f:
        f.write(data)
    return path


def make_nef(path, taken=None):
    """A minimal little-endian TIFF, which is what a NEF is underneath: IFD0
    pointing to an Exif IFD holding DateTimeOriginal. Pillow can't read it;
    Keeper's own raw reader must."""
    entries0, exif_entries, data = [], [], b""
    # Layout: header(8) | IFD0 (2 + 12 + 4) | Exif IFD (2 + 12n + 4) | strings
    exif_off = 8 + 2 + 12 + 4
    n = 1 if taken else 0
    strings_off = exif_off + 2 + 12 * n + 4
    if taken:
        s = taken.encode() + b"\0"
        exif_entries.append(struct.pack("<HHII", 0x9003, 2, len(s), strings_off))
        data = s
    entries0.append(struct.pack("<HHII", 0x8769, 4, 1, exif_off))
    out = b"II*\x00" + struct.pack("<I", 8)
    out += struct.pack("<H", len(entries0)) + b"".join(entries0) + struct.pack("<I", 0)
    out += struct.pack("<H", len(exif_entries)) + b"".join(exif_entries) + struct.pack("<I", 0)
    out += data + b"\0" * 64
    with open(path, "wb") as f:
        f.write(out)
    return path


def touch(path, data=b"x"):
    with open(path, "wb") as f:
        f.write(data)
    return path


def fingerprint(path):
    """(sha1, size, mtime): any write to the file changes at least one."""
    st = os.stat(path)
    with open(path, "rb") as f:
        return hashlib.sha1(f.read()).hexdigest(), st.st_size, st.st_mtime_ns


def fingerprints(folder):
    """fingerprint() of every file directly in folder (not NKSC_PARAM)."""
    return {n: fingerprint(os.path.join(folder, n)) for n in os.listdir(folder)
            if os.path.isfile(os.path.join(folder, n))}
