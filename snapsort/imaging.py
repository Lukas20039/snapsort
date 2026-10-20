"""Decoding, background loading, thumbnail cache, rotate/flip and saving edits."""
import hashlib
import os
import struct
import subprocess
import tempfile
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QImage, QImageIOHandler, QImageReader, QImageWriter, QTransform

from . import exif
from .store import atomic_write, is_video

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "snapsort" / "thumbs"
THUMB = 256
JPEG_EXT = {".jpg", ".jpeg"}

QImageReader.setAllocationLimit(2048)


def decode_scaled(path, target: QSize | None = None) -> QImage:
    """Decode with EXIF orientation applied, fitted (never upscaled) into target."""
    r = QImageReader(str(path))
    r.setAutoTransform(True)
    if target is not None:
        sz = r.size()
        rot = bool(r.transformation() & QImageIOHandler.Transformation.TransformationRotate90)
        if sz.isValid():
            shown = sz.transposed() if rot else sz
            if shown.width() > target.width() or shown.height() > target.height():
                fit = shown.scaled(target, Qt.AspectRatioMode.KeepAspectRatio)
                r.setScaledSize(fit.transposed() if rot else fit)
    img = r.read()
    if img.isNull():
        raise OSError(r.errorString())
    return img


def encode(img: QImage, fmt: str, quality=95) -> bytes:
    if fmt in ("jpg", "jpeg"):
        img = img.convertToFormat(QImage.Format.Format_RGB32)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    w = QImageWriter(buf, fmt.encode())
    w.setQuality(quality)
    if not w.write(img):
        raise OSError(w.errorString())
    buf.close()
    return bytes(ba)


def _fmt(path) -> str:
    return bytes(QImageReader(str(path)).format()).decode() or Path(path).suffix[1:].lower()


def editable(path) -> str | None:
    """None if the file can be rewritten, else the reason why not."""
    if is_video(path):
        return "videos cannot be edited"
    r = QImageReader(str(path))
    if r.imageCount() > 1:
        return "animated images cannot be edited"
    fmt = bytes(r.format()).decode()
    if fmt.encode() not in [bytes(f) for f in QImageWriter.supportedImageFormats()]:
        return f"cannot write {fmt or 'this'} format"
    return None


def apply_op(img: QImage, op: str) -> QImage:
    if op in ("cw", "ccw"):
        return img.transformed(QTransform().rotate(90 if op == "cw" else -90))
    return img.flipped(Qt.Orientation.Horizontal if op == "fliph" else Qt.Orientation.Vertical)


def _write_reencoded(path, img: QImage, original: bytes):
    fmt = _fmt(path)
    data = encode(img, fmt)
    if Path(path).suffix.lower() in JPEG_EXT:
        data = exif.splice_metadata(data, original)
    atomic_write(path, data)


def transform_file(path, op: str) -> bytes:
    """Rotate/flip in place (lossless for JPEG where possible). Returns original bytes for undo."""
    path = Path(path)
    original = path.read_bytes()
    if path.suffix.lower() in JPEG_EXT:
        inserted = exif.insert_orientation(original, exif.compose(1, op))
        if inserted:
            atomic_write(path, inserted)
            return original
        p = exif.orientation_patch(original)
        if p:
            value, off, bo = p
            fd = os.open(path, os.O_WRONLY)
            try:
                os.pwrite(fd, struct.pack(bo + "H", exif.compose(value, op)), off)
            finally:
                os.close(fd)
            return original
        args = {"cw": ["-rotate", "90"], "ccw": ["-rotate", "270"],
                "fliph": ["-flip", "horizontal"], "flipv": ["-flip", "vertical"]}[op]
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        os.close(fd)
        try:
            r = subprocess.run(["jpegtran", "-perfect", "-copy", "all", *args, "-outfile", tmp, str(path)],
                               capture_output=True, timeout=60)
            if r.returncode == 0 and os.path.getsize(tmp):
                try:
                    os.chmod(tmp, path.stat().st_mode & 0o7777)
                except OSError:
                    pass
                os.replace(tmp, path)
                return original
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            Path(tmp).unlink(missing_ok=True)
    elif why := editable(path):
        raise OSError(why)
    _write_reencoded(path, apply_op(decode_scaled(path), op), original)
    return original


def save_edited(path, img: QImage) -> bytes:
    original = Path(path).read_bytes()
    _write_reencoded(path, img, original)
    return original


def thumb_key(path) -> Path:
    st = os.stat(path)
    h = hashlib.sha1(f"{os.path.abspath(path)}\0{st.st_mtime_ns}\0{st.st_size}".encode()).hexdigest()
    return CACHE / f"{h}.jpg"


def make_thumb(path) -> QImage:
    key = thumb_key(path)
    if key.exists():
        img = QImage(str(key))
        if not img.isNull():
            return img
    if is_video(path):
        CACHE.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".jpg", dir=CACHE)
        os.close(fd)
        try:
            for ss in ("1", "0"):
                subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", ss, "-i", str(path),
                                "-frames:v", "1", "-vf",
                                f"scale={THUMB}:{THUMB}:force_original_aspect_ratio=decrease",
                                "-q:v", "4", "-y", tmp], capture_output=True, timeout=15)
                if os.path.getsize(tmp):
                    os.replace(tmp, key)
                    return QImage(str(key))
        finally:
            Path(tmp).unlink(missing_ok=True)
        raise OSError("no video frame")
    img = decode_scaled(path, QSize(THUMB, THUMB))
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        atomic_write(key, encode(img, "jpg", 85), fsync=False)
    except OSError:
        pass  # cache is best effort
    return img


class Loader(QObject):
    imageReady = Signal(str, QImage)
    thumbReady = Signal(str, QImage)
    failed = Signal(str, str)
    # workers emit these with the generation they started at; stale results are dropped
    _decoded = Signal(str, QImage, int)
    _thumbed = Signal(str, QImage, int)
    _decode_failed = Signal(str, str, int)
    _thumb_failed = Signal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.decode_pool = QThreadPool(self)
        self.decode_pool.setMaxThreadCount(3)
        self.thumb_pool = QThreadPool(self)
        self.thumb_pool.setMaxThreadCount(max(2, (os.cpu_count() or 4) // 2))
        self.cache: OrderedDict[str, QImage] = OrderedDict()
        self.cache_bytes = 0
        self.cache_limit = 512 << 20
        self.thumbs: OrderedDict[str, QImage] = OrderedDict()
        self.inflight: set[str] = set()
        self.wanted: frozenset[str] = frozenset()
        self.thumb_pending: set[str] = set()
        self.thumb_seq = 0
        self.errors: dict[str, str] = {}
        self.thumb_errors: set[str] = set()
        self.gen: dict[str, int] = {}
        self._decoded.connect(self._store)
        self._thumbed.connect(self._store_thumb)
        self._decode_failed.connect(self._fail)
        self._thumb_failed.connect(self._fail_thumb)

    # --- view images ---
    def cached(self, path, size: QSize) -> QImage | None:
        img = self.cache.get(path)
        if img is None:
            return None
        self.cache.move_to_end(path)
        if img.width() >= 0.9 * size.width() or img.height() >= 0.9 * size.height() \
                or img.text("full") == "1":
            return img
        return None

    def prefetch(self, paths: list[str], size: QSize):
        """paths[0] is the current item, the rest are neighbours by priority."""
        self.wanted = frozenset(paths)  # queued jobs for other paths skip themselves
        for i, p in enumerate(paths):
            if p in self.inflight or p in self.errors or is_video(p) or self.cached(p, size) is not None:
                continue
            self.inflight.add(p)
            g = self.gen.get(p, 0)
            self.decode_pool.start(lambda p=p, g=g: self._decode(p, size, g), 10 - i)

    def _decode(self, path, size, gen):
        if path not in self.wanted:
            self._decode_failed.emit(path, "", gen)
            return
        try:
            r = QImageReader(path)
            full = r.size()
            img = decode_scaled(path, size)
            if img.size() == full or img.size() == full.transposed():
                img.setText("full", "1")
            self._decoded.emit(path, img, gen)
        except Exception as e:  # noqa: BLE001 - reported to UI
            self._decode_failed.emit(path, str(e), gen)

    def _store(self, path, img, gen):
        self.inflight.discard(path)
        if gen != self.gen.get(path, 0):
            return
        old = self.cache.pop(path, None)
        if old is not None:
            self.cache_bytes -= old.sizeInBytes()
        self.cache[path] = img
        self.cache_bytes += img.sizeInBytes()
        while self.cache_bytes > self.cache_limit and len(self.cache) > 1:
            _, ev = self.cache.popitem(last=False)
            self.cache_bytes -= ev.sizeInBytes()
        self.imageReady.emit(path, img)

    def _fail(self, path, msg, gen):
        self.inflight.discard(path)
        if msg and gen == self.gen.get(path, 0):
            self.errors[path] = msg
            self.failed.emit(path, msg)

    def _fail_thumb(self, path, gen):
        if gen == self.gen.get(path, 0):
            self.thumb_pending.discard(path)
            self.thumb_errors.add(path)
            self.failed.emit(path, "")

    # --- thumbnails ---
    def thumb(self, path) -> QImage | None:
        img = self.thumbs.get(path)
        if img is not None:
            self.thumbs.move_to_end(path)
            return img
        if path not in self.thumb_pending and path not in self.thumb_errors:
            self.thumb_pending.add(path)
            self.thumb_seq += 1  # newest request first: the cells on screen now
            g = self.gen.get(path, 0)
            self.thumb_pool.start(lambda: self._thumb(path, g), self.thumb_seq)
        return None

    def _thumb(self, path, gen):
        try:
            self._thumbed.emit(path, make_thumb(path), gen)
        except Exception:  # noqa: BLE001
            self._thumb_failed.emit(path, gen)

    def _store_thumb(self, path, img, gen):
        if gen != self.gen.get(path, 0):
            return
        self.thumb_pending.discard(path)
        self.thumbs[path] = img
        while len(self.thumbs) > 1500:
            self.thumbs.popitem(last=False)
        self.thumbReady.emit(path, img)

    def invalidate(self, path):
        self.gen[path] = self.gen.get(path, 0) + 1
        self.thumb_pending.discard(path)
        self.thumb_errors.discard(path)
        old = self.cache.pop(path, None)
        if old is not None:
            self.cache_bytes -= old.sizeInBytes()
        self.thumbs.pop(path, None)
        self.errors.pop(path, None)
