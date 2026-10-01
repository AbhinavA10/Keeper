"""Read and write star ratings and colour labels the way NX Studio stores them.

NX Studio keeps per-image settings in NKSC_PARAM/<name>.<ext>.nksc. The star
rating and label are NOT the visible <nine:Rating>/<nine:Label> tags (always 0)
but live in an XMP packet stored base64-encoded inside
<ast:XMLPackets><rdf:value>. The camera also writes an XMP packet into the
NEF/JPG itself (<xmp:Rating>0</xmp:Rating>), which is what NX Studio falls back
to when there is no sidecar.

A label is written as <photoshop:Urgency>N</photoshop:Urgency> plus
<xmp:Label>Name</xmp:Label>; removing it deletes both.

All edits keep packets the same length where possible by consuming or adding
XMP padding whitespace, so in-file edits never shift any other bytes.
"""
import base64
import os
import re

SIDECAR_DIR = "NKSC_PARAM"
XMP_NS = b'xmlns:xmp="http://ns.adobe.com/xap/1.0/"'
PS_NS = b'xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"'
# Windows' own percentage scale, which NX Studio mirrors.
MS_RATING = {0: 0, 1: 1, 2: 25, 3: 50, 4: 75, 5: 99}
# NX Studio label numbers (stored as photoshop:Urgency).
LABELS = {"Red": 1, "Orange": 2, "Yellow": 3, "Green": 4, "Blue": 5,
          "Purple": 6, "Pink": 7, "Brown": 8, "Gray": 9}
UNSET = object()

_RATING_RE = re.compile(rb"<xmp:Rating>(-?\d+)</xmp:Rating>")
_LABEL_RE = re.compile(rb"<xmp:Label>([^<]*)</xmp:Label>")
_URGENCY_RE = re.compile(rb"<photoshop:Urgency>(\d+)</photoshop:Urgency>")
_MS_RE = re.compile(rb"<MicrosoftPhoto:Rating>(\d+)</MicrosoftPhoto:Rating>")
_PACKET_VALUE_RE = re.compile(
    r'(<ast:XMLPackets rdf:parseType="Resource">\s*<rdf:value>)([\s\S]*?)(</rdf:value>)')


class XmpError(Exception):
    pass


# ---------------------------------------------------------------- packets

def _packet_bounds(buf):
    """Return (start, pad_start, end) of the XMP packet body in buf.

    start: '<x:xmpmeta', end: '<?xpacket end' (or len), pad_start: start of the
    whitespace padding just before end.
    """
    start = buf.find(b"<x:xmpmeta")
    if start < 0:
        return None
    close = buf.find(b"</x:xmpmeta>", start)
    if close < 0:
        return None
    close += len(b"</x:xmpmeta>")
    # Padding runs from </x:xmpmeta> to '<?xpacket end' (NEF packets may omit
    # the trailer, in which case the padding simply ends).
    end = close
    while end < len(buf) and buf[end] in b" \t\r\n":
        end += 1
    pad_start = end
    while pad_start > close and buf[pad_start - 1] in b" \t\r\n":
        pad_start -= 1
    return start, pad_start, end


def read_packet(buf):
    """Return (rating, label) from the first XMP packet in buf."""
    b = _packet_bounds(buf)
    if not b:
        return None, None
    body = buf[b[0]:b[1]]
    m = _RATING_RE.search(body)
    lm = _LABEL_RE.search(body)
    return (int(m.group(1)) if m else None,
            lm.group(1).decode("utf-8", "replace") or None if lm else None)


def _ensure_ns(body, decl):
    if decl in body:
        return body
    d = body.find(b"<rdf:Description")
    if d < 0:
        raise XmpError("no rdf:Description")
    d += len(b"<rdf:Description")
    return body[:d] + b" " + decl + body[d:]


def _insert(body, element):
    close = body.find(b"</rdf:Description>")
    if close < 0:
        raise XmpError("no rdf:Description")
    return body[:close] + element + body[close:]


def _edit_body(body, rating=UNSET, label=UNSET):
    """Return body with rating and/or label changed."""
    if rating is not UNSET:
        if _RATING_RE.search(body):
            body = _RATING_RE.sub(b"<xmp:Rating>%d</xmp:Rating>" % rating, body, count=1)
        elif rating > 0:
            body = _ensure_ns(_insert(body, b"<xmp:Rating>%d</xmp:Rating>" % rating), XMP_NS)
        if _MS_RE.search(body):
            body = _MS_RE.sub(b"<MicrosoftPhoto:Rating>%d</MicrosoftPhoto:Rating>"
                              % MS_RATING[rating], body, count=1)
    if label is not UNSET:
        body = _URGENCY_RE.sub(b"", body)
        body = _LABEL_RE.sub(b"", body)
        if label:
            body = _insert(body, b"<photoshop:Urgency>%d</photoshop:Urgency>"
                           b"<xmp:Label>%s</xmp:Label>" % (LABELS[label], label.encode()))
            body = _ensure_ns(_ensure_ns(body, XMP_NS), PS_NS)
    return body


def edit_packet(buf, keep_length, **changes):
    """Return a copy of buf with rating= and/or label= changed.

    With keep_length the result is exactly len(buf) bytes (padding absorbs the
    difference); raises XmpError if that is impossible. Returns buf unchanged if
    nothing differs.
    """
    b = _packet_bounds(buf)
    if not b:
        raise XmpError("no XMP packet")
    start, pad_start, end = b
    old_body = buf[start:pad_start]
    new_body = _edit_body(old_body, **changes)
    if new_body == old_body:
        return buf
    pad_len = (end - pad_start) - (len(new_body) - len(old_body))
    if pad_len < 0:
        if keep_length:
            raise XmpError("no room in XMP padding")
        pad_len = 0
    padding = buf[pad_start:end]
    # Keep the padding's line structure; just trim or extend its tail.
    if pad_len <= len(padding):
        new_pad = padding[:pad_len]
    else:
        new_pad = padding + b" " * (pad_len - len(padding))
    return buf[:start] + new_body + new_pad + buf[end:]


# ---------------------------------------------------------------- sidecars

def _decode_value(v):
    return base64.b64decode(v.replace("&#xA;", "").replace("\n", "").strip())


def _encode_value(raw, template):
    """Base64-encode raw using the same line width and separators as template."""
    enc = base64.b64encode(raw).decode("ascii")
    lines = template.split("&#xA;")
    width = len(lines[0]) if len(lines) > 1 else 0
    if not width:
        return enc
    chunks = [enc[i:i + width] for i in range(0, len(enc), width)]
    out = "&#xA;".join(chunks)
    if template.endswith("&#xA;"):
        out += "&#xA;"
    return out


def read_sidecar(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    m = _PACKET_VALUE_RE.search(text)
    if not m:
        return None, None
    return read_packet(_decode_value(m.group(2)))


def _check(got, changes, where):
    rating, label = got
    if "rating" in changes and (rating or 0) != changes["rating"]:
        raise XmpError(f"verification failed for {where}")
    if "label" in changes and label != changes["label"]:
        raise XmpError(f"verification failed for {where}")


def write_sidecar(path, **changes):
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    m = _PACKET_VALUE_RE.search(text)
    if not m:
        raise XmpError(f"no XMP packet in {path}")
    raw = _decode_value(m.group(2))
    try:
        new_raw = edit_packet(raw, keep_length=False, **changes)
    except XmpError as e:
        raise XmpError(f"{e} in {os.path.basename(path)}")
    if new_raw == raw:
        return False
    new_text = text[:m.start(2)] + _encode_value(new_raw, m.group(2)) + text[m.end(2):]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(new_text)
    try:
        _check(read_sidecar(tmp), changes, os.path.basename(path))
    except XmpError:
        os.remove(tmp)
        raise
    os.replace(tmp, path)
    return True


# ---------------------------------------------------------------- image files

_HEAD = 512 * 1024  # camera XMP sits well within the first 512 KB


def read_file(path):
    with open(path, "rb") as f:
        return read_packet(f.read(_HEAD))


def write_file_inplace(path, **changes):
    """Change the image's own XMP packet without moving any other bytes.

    Only the packet region is rewritten; returns False if nothing changed,
    raises XmpError if it can't be done at the same length.
    """
    name = os.path.basename(path)
    with open(path, "r+b") as f:
        head = f.read(_HEAD)
        b = _packet_bounds(head)
        if not b or b[2] >= len(head):
            raise XmpError(f"no XMP packet in {name}")
        start, _, end = b
        region = head[start:end]
        try:
            new_region = edit_packet(region, keep_length=True, **changes)
        except XmpError as e:
            raise XmpError(f"{e} in {name}")
        if new_region == region:
            return False
        f.seek(start)
        f.write(new_region)
        f.flush()
        f.seek(start)
        if f.read(len(new_region)) != new_region:
            raise XmpError(f"verification failed for {name}")
    _check(read_file(path), changes, name)
    return True


# ---------------------------------------------------------------- pairs

def sidecars_for(folder, names):
    d = os.path.join(folder, SIDECAR_DIR)
    return [p for p in (os.path.join(d, n + ".nksc") for n in names) if os.path.exists(p)]


def read_pair(folder, names):
    """Rating/label for a set of files that NX Studio shows as one item."""
    for sc in sidecars_for(folder, names):
        r, l = read_sidecar(sc)
        return (r or 0), l
    for n in names:
        r, l = read_file(os.path.join(folder, n))
        if r is not None:
            return max(r, 0), l
    return 0, None


def write_pair(folder, names, **changes):
    """Set rating= and/or label= everywhere NX Studio will look for this pair.

    Each file gets it where NX Studio reads it for that file: its sidecar if it
    has one, otherwise the camera's XMP packet in the file.
    """
    if "rating" in changes and not 0 <= changes["rating"] <= 5:
        raise ValueError("rating must be 0-5")
    if changes.get("label") not in (None, *LABELS):
        raise ValueError("unknown label")
    written, errors = 0, []
    for n in names:
        sc = sidecars_for(folder, [n])
        try:
            if sc:
                write_sidecar(sc[0], **changes)
            else:
                write_file_inplace(os.path.join(folder, n), **changes)
            written += 1
        except XmpError as e:
            errors.append(str(e))
    if errors and not written:
        raise XmpError("; ".join(errors))
