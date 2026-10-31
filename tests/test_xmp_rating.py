"""NX Studio sidecars: what Keeper writes, and that it changes nothing else."""
import os
import tempfile
import unittest

import xmp_rating as X
from helpers import fingerprint, make_jpeg

# An NX Studio edit block. Keeper must carry it over byte for byte.
NINE_EDITS = "\r\n".join([
    '         <nine:NineEdits rdf:parseType="Resource">',
    '            <rdf:value>AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=&#xA;</rdf:value>',
    '            <astype:Type>Binary</astype:Type>',
    '         </nine:NineEdits>',
    ''])


def nx_sidecar(path, rating=2, label=None, extra=b'<xmp:CreatorTool>NX Studio 1.9</xmp:CreatorTool>'):
    """A sidecar shaped like one NX Studio wrote after editing and rating a
    photo: an edit block plus an extra XMP property Keeper doesn't manage."""
    packet = X._nx_packet(rating, label).replace(
        b"      </rdf:Description>", b"         " + extra + b"\n      </rdf:Description>")
    text = (X._SIDECAR_HEAD.format(packet=X._encode_value(packet, "x" * 76 + "&#xA;y"))
            + NINE_EDITS + X._SIDECAR_TAIL)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return path


def read_text(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def packet_of(path):
    return X._decode_value(X._PACKET_VALUE_RE.search(read_text(path)).group(2))


class TempDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.sc = X.sidecar_path(self.dir, "DSC_0001.NEF")

    def tearDown(self):
        self._tmp.cleanup()


class CreateSidecar(TempDir):
    def test_round_trips_every_rating_and_label(self):
        for rating in range(6):
            for label in (None, "Red", "Blue", "Gray"):
                with self.subTest(rating=rating, label=label):
                    if os.path.exists(self.sc):
                        os.remove(self.sc)
                    X.create_sidecar(self.sc, rating, label)
                    r, l = X.read_sidecar(self.sc)
                    self.assertEqual((r or 0, l), (rating, label))

    def test_matches_nx_studio_layout(self):
        X.create_sidecar(self.sc, 4, "Blue", gps=b"\x02\x03\x00\x00")
        text = read_text(self.sc)
        self.assertNotIn("\n", text.replace("\r\n", ""), "lines must end in CRLF")
        self.assertIn("<sdc:appname>NX Studio</sdc:appname>", text)
        self.assertNotIn("NineEdits", text, "edits are left to NX Studio")
        self.assertIn("<ast:GPSVersionID", text)
        value = X._PACKET_VALUE_RE.search(text).group(2)
        self.assertTrue(all(len(line) <= 76 for line in value.split("&#xA;")))
        packet = packet_of(self.sc)
        for part in (b"<xmp:Rating>4</xmp:Rating>", b"<MicrosoftPhoto:Rating>75</MicrosoftPhoto:Rating>",
                     b"<photoshop:Urgency>5</photoshop:Urgency>", b"<xmp:Label>Blue</xmp:Label>"):
            self.assertIn(part, packet)

    def test_refuses_to_overwrite(self):
        X.create_sidecar(self.sc, 1, None)
        with self.assertRaises(X.XmpError):
            X.create_sidecar(self.sc, 2, None)


class UpdateSidecar(TempDir):
    def test_changes_only_rating_and_label(self):
        nx_sidecar(self.sc, rating=2)
        before = read_text(self.sc)
        m0 = X._PACKET_VALUE_RE.search(before)
        X.write_sidecar(self.sc, rating=4, label="Red")
        after = read_text(self.sc)
        m1 = X._PACKET_VALUE_RE.search(after)
        # Everything outside the XMP packet, NX Studio's edits included, is identical.
        self.assertEqual(before[:m0.start(2)], after[:m1.start(2)])
        self.assertEqual(before[m0.end(2):], after[m1.end(2):])
        self.assertIn(NINE_EDITS, after)
        packet = packet_of(self.sc)
        self.assertIn(b"<xmp:CreatorTool>NX Studio 1.9</xmp:CreatorTool>", packet)
        self.assertIn(b"<MicrosoftPhoto:Rating>75</MicrosoftPhoto:Rating>", packet)
        self.assertEqual(X.read_sidecar(self.sc), (4, "Red"))

    def test_switching_and_clearing_labels(self):
        nx_sidecar(self.sc, rating=3, label="Red")
        X.write_sidecar(self.sc, label="Blue")
        self.assertEqual(X.read_sidecar(self.sc), (3, "Blue"))
        self.assertNotIn(b"<photoshop:Urgency>1<", packet_of(self.sc))
        X.write_sidecar(self.sc, label=None)
        self.assertEqual(X.read_sidecar(self.sc), (3, None))
        packet = packet_of(self.sc)
        self.assertNotIn(b"Urgency", packet)
        self.assertNotIn(b"xmp:Label>", packet)

    def test_clearing_the_rating(self):
        nx_sidecar(self.sc, rating=5)
        X.write_sidecar(self.sc, rating=0)
        self.assertEqual(X.read_sidecar(self.sc)[0] or 0, 0)
        self.assertIn(b"<MicrosoftPhoto:Rating>0</MicrosoftPhoto:Rating>", packet_of(self.sc))

    def test_packet_with_only_xmp_namespace(self):
        # Many real NX Studio packets look like this: xmp:Rating plus tool and
        # date, no MicrosoftPhoto rating and no photoshop namespace yet.
        packet = b"\n".join([
            b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>',
            b'<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="XMP Core 5.5.0">',
            b'   <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">',
            b'      <rdf:Description rdf:about=""',
            b'            xmlns:xmp="http://ns.adobe.com/xap/1.0/">',
            b'         <xmp:Rating>0</xmp:Rating>',
            b'         <xmp:CreatorTool>NIKON D7500 Ver.1.21</xmp:CreatorTool>',
            b'         <xmp:CreateDate>2026-08-24T11:48:06.40</xmp:CreateDate>',
            b'      </rdf:Description>',
            b'   </rdf:RDF>',
            b'</x:xmpmeta>',
            b" " * 2000,
            b'<?xpacket end="w"?>'])
        out = X.edit_packet(packet, keep_length=False, rating=5, label="Red")
        self.assertEqual(X.read_packet(out), (5, "Red"))
        self.assertIn(b'xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"', out)
        self.assertIn(b"\n         <photoshop:Urgency>1</photoshop:Urgency>\n         <xmp:Label>Red</xmp:Label>", out)
        self.assertIn(b"<xmp:CreateDate>2026-08-24T11:48:06.40</xmp:CreateDate>", out)
        self.assertNotIn(b"MicrosoftPhoto", out)

    def test_no_change_means_no_write(self):
        nx_sidecar(self.sc, rating=3)
        before = fingerprint(self.sc)
        self.assertFalse(X.write_sidecar(self.sc, rating=3))
        self.assertEqual(fingerprint(self.sc), before)

    def test_keep_length_edit_fits_in_padding(self):
        raw = X._nx_packet(1, None)
        out = X.edit_packet(raw, keep_length=True, rating=5, label="Purple")
        self.assertEqual(len(out), len(raw))
        self.assertEqual(X.read_packet(out), (5, "Purple"))


class Pairs(TempDir):
    def setUp(self):
        super().setUp()
        self.names = ["DSC_0001.NEF", "DSC_0001.JPG"]
        make_jpeg(os.path.join(self.dir, "DSC_0001.JPG"), xmp_rating=3)
        make_jpeg(os.path.join(self.dir, "DSC_0001.NEF"))

    def test_never_writes_the_photos(self):
        before = {n: fingerprint(os.path.join(self.dir, n)) for n in self.names}
        X.write_pair(self.dir, self.names, 5, "Red")
        X.write_pair(self.dir, self.names, 2, "Blue")
        X.write_pair(self.dir, self.names, 0, None)
        self.assertEqual({n: fingerprint(os.path.join(self.dir, n)) for n in self.names}, before)

    def test_writes_one_sidecar_per_file(self):
        X.write_pair(self.dir, self.names, 4, "Blue")
        for n in self.names:
            self.assertEqual(X.read_sidecar(X.sidecar_path(self.dir, n)), (4, "Blue"))
        self.assertEqual(X.read_pair(self.dir, self.names), (4, "Blue"))

    def test_unrated_pair_gets_no_sidecars(self):
        X.write_pair(self.dir, self.names, 0, None)
        self.assertFalse(os.path.exists(os.path.join(self.dir, X.SIDECAR_DIR)))

    def test_falls_back_to_the_camera_rating(self):
        self.assertEqual(X.read_pair(self.dir, self.names), (3, None))
        X.write_pair(self.dir, self.names, 1, None)
        self.assertEqual(X.read_pair(self.dir, self.names), (1, None))

    def test_rejects_bad_values_without_writing(self):
        for bad in ({"rating": 6, "label": None}, {"rating": 1, "label": "Teal"}):
            with self.subTest(**bad), self.assertRaises(ValueError):
                X.write_pair(self.dir, self.names, **bad)
        self.assertFalse(os.path.exists(os.path.join(self.dir, X.SIDECAR_DIR)))


if __name__ == "__main__":
    unittest.main()
