import itertools

from conftest import DATE, THUMB, exif_app1, fake_jpeg, seg

from snapsort import exif

TABLE = {  # old: (cw, ccw, fliph, flipv)
    1: (6, 8, 2, 4), 2: (7, 5, 1, 3), 3: (8, 6, 4, 2), 4: (5, 7, 3, 1),
    5: (2, 4, 6, 8), 6: (3, 1, 5, 7), 7: (4, 2, 8, 6), 8: (1, 3, 7, 5),
}
OPS = ("cw", "ccw", "fliph", "flipv")

# independent oracle on a grid (list of row tuples)
G = ((1, 2, 3), (4, 5, 6))
mh = lambda g: tuple(r[::-1] for r in g)  # noqa: E731
mv = lambda g: tuple(g[::-1])  # noqa: E731
cw = lambda g: tuple(zip(*g[::-1]))  # noqa: E731
APPLY = {"cw": cw, "ccw": lambda g: cw(cw(cw(g))), "fliph": mh, "flipv": mv}
# exiftool wording: stored -> displayed
DISPLAY = {
    1: lambda g: g, 2: mh, 3: lambda g: cw(cw(g)), 4: mv,
    5: lambda g: cw(cw(cw(mh(g)))), 6: cw, 7: lambda g: cw(mh(g)), 8: lambda g: cw(cw(cw(g))),
}


def test_compose_table():
    for old, row in TABLE.items():
        assert tuple(exif.compose(old, op) for op in OPS) == row


def test_compose_matches_oracle():
    for v, op in itertools.product(range(1, 9), OPS):
        assert DISPLAY[exif.compose(v, op)](G) == APPLY[op](DISPLAY[v](G))


def test_compose_group():
    for v in range(1, 9):
        assert exif.compose(exif.compose(v, "cw"), "ccw") == v
        assert exif.compose(exif.compose(v, "fliph"), "fliph") == v
        assert exif.compose(exif.compose(v, "flipv"), "flipv") == v


def test_segments():
    data = fake_jpeg(exif_app1())
    markers = [m for m, s, e in exif.segments(data)]
    assert markers == [0xE0, 0xE1, 0xDB]
    for m, s, e in exif.segments(data):
        assert data[s] == 0xFF


def test_segments_fill_bytes_and_garbage():
    data = b"\xFF\xD8\xFF\xFF" + seg(0xE0, b"x")[1:] + seg(0xDA, b"")
    assert [m for m, *_ in exif.segments(data)] == [0xE0]
    assert exif.segments(b"not a jpeg") == []
    assert exif.segments(b"\xFF\xD8\xFF\xE1\xFF\xFF") == []
    assert exif.segments(fake_jpeg(exif_app1())[:30]) == exif.segments(fake_jpeg(exif_app1())[:30])


def test_orientation_both_byteorders():
    for bo in "<>":
        for v in range(1, 9):
            data = fake_jpeg(exif_app1(orientation=v, bo=bo))
            assert exif.get_orientation(data) == v


def test_orientation_invalid():
    assert exif.get_orientation(fake_jpeg()) == 1
    assert exif.get_orientation(fake_jpeg(exif_app1(orientation=0))) == 1
    assert exif.get_orientation(fake_jpeg(exif_app1(orientation=9))) == 1
    bad = fake_jpeg(seg(0xE1, b"Exif\0\0XX\0*\0\0\0\x08"))
    assert exif.orientation_patch(bad) is None
    assert exif.get_orientation(b"garbage") == 1


def test_xmp_before_exif_not_confused():
    xmp = seg(0xE1, b"http://ns.adobe.com/xap/1.0/\0<x tiff:Orientation=\"6\"/>")
    data = fake_jpeg(xmp, exif_app1(orientation=3))
    assert exif.get_orientation(data) == 3


def test_splice_metadata():
    xmp = seg(0xE1, b"http://ns.adobe.com/xap/1.0/\0<x tiff:Orientation=\"6\"/>")
    icc = seg(0xE2, b"ICC_PROFILE\0\1\1prof")
    mpf = seg(0xE2, b"MPF\0junk")
    iptc = seg(0xED, b"Photoshop 3.0\0iptc")
    orig = fake_jpeg(exif_app1(orientation=6, bo=">"), xmp, icc, mpf, iptc)
    new = fake_jpeg(seg(0xE2, b"ICC_PROFILE\0\1\1newprof"))
    out = exif.splice_metadata(new, orig)
    segs = exif.segments(out)
    assert [m for m, *_ in segs][:2] == [0xE0, 0xE1]
    assert exif.get_orientation(out) == 1
    assert DATE in out and THUMB not in out
    assert b'tiff:Orientation="1"' in out
    assert b"prof" in out and b"newprof" not in out and b"MPF\0" not in out and b"iptc" in out
    tail = new[exif.segments(new)[-1][1]:]
    assert out.endswith(tail)


def test_splice_without_metadata_is_identity():
    new = fake_jpeg()
    assert exif.splice_metadata(new, b"\x89PNG") == new
