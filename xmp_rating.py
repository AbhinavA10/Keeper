"""Read and write star ratings and colour labels the way NX Studio stores them.

NX Studio keeps per-image settings in NKSC_PARAM/<name>.<ext>.nksc. The star
rating and label are NOT the visible <nine:Rating>/<nine:Label> tags (always 0)
but live in an XMP packet stored base64-encoded inside
<ast:XMLPackets><rdf:value>. The camera also writes an XMP packet into the
NEF/JPG itself (<xmp:Rating>0</xmp:Rating>), which is what NX Studio falls back
to when there is no sidecar.

A label is written as <photoshop:Urgency>N</photoshop:Urgency> plus
<xmp:Label>Name</xmp:Label>; removing it deletes both.

Original photo files are only ever READ (for in-camera ratings). Everything
Keeper stores goes into sidecars, which are created when missing.
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
    """Add element(s) as the last properties, matching the packet's layout."""
    close = re.search(rb"(\r?\n[ \t]*)?</rdf:Description>", body)
    if not close:
        raise XmpError("no rdf:Description")
    indent = re.search(rb"(\r?\n[ \t]+)<\w+:\w+>", body)
    if close.group(1) and indent:  # pretty-printed (NX Studio style)
        sep = indent.group(1)
        element = b"".join(sep + e for e in re.findall(rb"<\w+:\w+>[^<]*</\w+:\w+>", element))
    return body[:close.start()] + element + body[close.start():]


def _remove(regex_body, body):
    """Remove a property element together with its line break and indent."""
    return re.sub(rb"(\r?\n[ \t]*)?" + regex_body, b"", body)


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
        body = _remove(_URGENCY_RE.pattern, body)
        body = _remove(_LABEL_RE.pattern, body)
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
    """Rating/label the camera stored in the image's own XMP (read-only)."""
    with open(path, "rb") as f:
        return read_packet(f.read(_HEAD))


# ---------------------------------------------------------------- new sidecars
# Layout copied from sidecars NX Studio 1.9 wrote for this user's D7500 and
# Z50 II. NX Studio's edit settings (nine:NineEdits) are deliberately left out:
# with none recorded, NX Studio uses the camera's as-shot settings.

_SIDECAR_HEAD = "\r\n".join([
    '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>',
    '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="XMP Core 5.5.0">',
    '   <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">',
    '      <rdf:Description rdf:about="core-sidear-tags/1.0"',
    '            xmlns:sdc="http://ns.nikon.com/sdc/1.0/"',
    '            xmlns:ast="http://ns.nikon.com/asteroid/1.0/"',
    '            xmlns:astype="http://ns.nikon.com/asteroid/Types/1.0/"',
    '            xmlns:nine="http://ns.nikon.com/nine/1.0/">',
    '         <sdc:about>nikon sidecar/1.0</sdc:about>',
    '         <sdc:version>1.1.0</sdc:version>',
    '         <sdc:appversion>1.9 W</sdc:appversion>',
    '         <sdc:appname>NX Studio</sdc:appname>',
    '         <ast:about>core-asteroid-tags</ast:about>',
    '         <ast:version>11.0.0.3000</ast:version>',
    '         <ast:XMLPackets rdf:parseType="Resource">',
    '            <rdf:value>{packet}</rdf:value>',
    '            <astype:Type>Binary</astype:Type>',
    '         </ast:XMLPackets>',
    ''])
_SIDECAR_GPS = "\r\n".join([
    '         <ast:GPSVersionID rdf:parseType="Resource">',
    '            <rdf:value>{gps}</rdf:value>',
    '            <astype:Type>Binary</astype:Type>',
    '         </ast:GPSVersionID>',
    ''])
_SIDECAR_TAIL = "\r\n".join([
    '         <nine:about>nine-tags</nine:about>',
    '         <nine:version>2.0.0</nine:version>',
    '         <nine:Label>0</nine:Label>',
    '         <nine:Rating>0</nine:Rating>',
    '         <nine:Trim>/////////////////////w==</nine:Trim>',
    '      </rdf:Description>',
    '   </rdf:RDF>',
    '</x:xmpmeta>',
    '',
    '<?xpacket end="w"?>'])


def _nx_packet(rating, label):
    """The XMP packet NX Studio stores inside a sidecar after rating/labelling."""
    props = []
    if label:
        props += [f"<photoshop:Urgency>{LABELS[label]}</photoshop:Urgency>",
                  f"<xmp:Label>{label}</xmp:Label>"]
    if rating:
        props.append(f"<xmp:Rating>{rating}</xmp:Rating>")
    props.append(f"<MicrosoftPhoto:Rating>{MS_RATING[rating]}</MicrosoftPhoto:Rating>")
    lines = [
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>',
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="XMP Core 5.5.0">',
        '   <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">',
        '      <rdf:Description rdf:about=""',
        '            xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"',
        '            xmlns:xmp="http://ns.adobe.com/xap/1.0/"',
        '            xmlns:MicrosoftPhoto="http://ns.microsoft.com/photo/1.0/">',
        *(f"         {p}" for p in props),
        '      </rdf:Description>',
        '   </rdf:RDF>',
        '</x:xmpmeta>',
        *([" " * 100] * 20),  # standard XMP padding, as NX Studio writes it
        '<?xpacket end="w"?>']
    return "\n".join(lines).encode("utf-8")


def create_sidecar(path, rating, label, gps=None):
    """Write a new NX Studio sidecar at path (which must not exist yet)."""
    if os.path.exists(path):
        raise XmpError(f"{os.path.basename(path)} already exists")
    # NX Studio wraps base64 at 76 characters with &#xA; separators.
    text = _SIDECAR_HEAD.format(packet=_encode_value(_nx_packet(rating, label), "x" * 76 + "&#xA;y"))
    if gps:
        text += _SIDECAR_GPS.format(gps=base64.b64encode(gps).decode("ascii"))
    text += _SIDECAR_TAIL
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    try:
        _check(read_sidecar(tmp), {"rating": rating, "label": label}, os.path.basename(path))
    except XmpError:
        os.remove(tmp)
        raise
    os.replace(tmp, path)


# ---------------------------------------------------------------- pairs

def sidecar_path(folder, name):
    return os.path.join(folder, SIDECAR_DIR, name + ".nksc")


def sidecars_for(folder, names):
    return [p for p in (sidecar_path(folder, n) for n in names) if os.path.exists(p)]


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


def write_pair(folder, names, rating, label, gps=None):
    """Store rating and label for a pair in NX Studio sidecars, one per file.

    Existing sidecars are updated; missing ones are created. The image files
    themselves are never opened for writing.
    """
    if not 0 <= rating <= 5:
        raise ValueError("rating must be 0-5")
    if label not in (None, *LABELS):
        raise ValueError("unknown label")
    for n in names:
        sc = sidecar_path(folder, n)
        if os.path.exists(sc):
            write_sidecar(sc, rating=rating, label=label)
        elif rating or label:
            create_sidecar(sc, rating, label, gps)
