"""Single-image/video view, thumbnail grid and help overlay."""
import zlib

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QImage, QImageReader, QMovie, QPainter, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QLabel, QListView, QStackedWidget, QStyle, QStyledItemDelegate, QWidget

from .store import VERDICTS, is_video

SLOT_COLORS = ["#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231",
               "#911eb4", "#42d4f4", "#f032e6", "#bfef45"]
CELL = QSize(196, 216)


def tag_color(tag, slots) -> QColor:
    if tag in slots:
        return QColor(SLOT_COLORS[slots.index(tag)])
    return QColor.fromHsv(zlib.crc32(tag.encode()) % 360, 160, 220)


class ImageCanvas(QWidget):
    resized = Signal()

    def __init__(self):
        super().__init__()
        self.pix: QPixmap | None = None
        self.text = ""
        self.setMinimumSize(200, 150)

    def show_image(self, img: QImage | None, text=""):
        self.pix = QPixmap.fromImage(img) if img is not None and not img.isNull() else None
        self.text = text
        self.update()

    def target_size(self) -> QSize:
        return self.size() * self.devicePixelRatioF()

    def resizeEvent(self, e):
        self.resized.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(16, 16, 16))
        if self.pix:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            dpr = self.devicePixelRatioF()
            size = self.pix.size() / dpr
            if size.width() > self.width() or size.height() > self.height():
                size = size.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            r = QRect((self.width() - size.width()) // 2, (self.height() - size.height()) // 2,
                      size.width(), size.height())
            p.drawPixmap(r, self.pix)
        if self.text:
            p.setPen(QColor(220, 220, 220))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text)


class SingleView(QStackedWidget):
    def __init__(self):
        super().__init__()
        self.canvas = ImageCanvas()
        self.video = QVideoWidget()
        self.anim = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.anim.setStyleSheet("background:#101010")
        for w in (self.canvas, self.video, self.anim):
            self.addWidget(w)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.movie: QMovie | None = None

    def stop(self):
        self.player.stop()
        self.player.setSource(QUrl())
        if self.movie:
            self.movie.stop()
            self.movie = None

    def show_path(self, path) -> str:
        """Switch page for path; returns 'video', 'anim' or 'image'."""
        self.stop()
        if is_video(path):
            self.setCurrentWidget(self.video)
            self.player.setSource(QUrl.fromLocalFile(path))
            self.player.play()
            return "video"
        # only probe formats that can animate: opening a file costs a round trip on network mounts
        r = QImageReader(path) if path.lower().endswith((".gif", ".webp")) else None
        if r and r.supportsAnimation() and r.imageCount() > 1:
            self.movie = QMovie(path)
            sz = r.size()
            if sz.width() > self.width() or sz.height() > self.height():
                self.movie.setScaledSize(sz.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio))
            self.anim.setMovie(self.movie)
            self.movie.start()
            self.setCurrentWidget(self.anim)
            return "anim"
        self.setCurrentWidget(self.canvas)
        return "image"

    def toggle_play(self):
        if self.movie:
            self.movie.setPaused(self.movie.state() == QMovie.MovieState.Running)
        elif self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def seek(self, ms):
        self.player.setPosition(max(0, self.player.position() + ms))


class GridModel(QAbstractListModel):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.rows: dict[str, int] = {}
        win.loader.thumbReady.connect(self._ready)
        win.loader.failed.connect(self._ready)

    def reset(self):
        self.beginResetModel()
        self.rows = {self.win.path(n): i for i, n in enumerate(self.win.view)}
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.win.view)

    def data(self, idx, role=Qt.ItemDataRole.DisplayRole):
        name = self.win.view[idx.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return name
        if role == Qt.ItemDataRole.DecorationRole:
            return self.win.loader.thumb(self.win.path(name))
        if role == Qt.ItemDataRole.UserRole:
            return self.win.store.tags(name), self.win.store.survived(name)
        return None

    def _ready(self, path, *_):
        row = self.rows.get(path)
        if row is not None:
            self.refresh(row)

    def refresh(self, row):
        i = self.index(row)
        self.dataChanged.emit(i, i)


class GridDelegate(QStyledItemDelegate):
    def __init__(self, win):
        super().__init__()
        self.win = win

    def sizeHint(self, option, index):
        return CELL

    def paint(self, p: QPainter, opt, index):
        r = opt.rect.adjusted(4, 4, -4, -4)
        p.save()
        if opt.state & QStyle.StateFlag.State_Selected:
            p.fillRect(opt.rect, opt.palette.highlight())
        img_r = QRect(r.x(), r.y(), r.width(), r.height() - 20)
        img = index.data(Qt.ItemDataRole.DecorationRole)
        name = index.data()
        if img is not None and not img.isNull():
            s = img.size().scaled(img_r.size(), Qt.AspectRatioMode.KeepAspectRatio)
            p.drawImage(QRect(img_r.x() + (img_r.width() - s.width()) // 2,
                              img_r.y() + (img_r.height() - s.height()) // 2, s.width(), s.height()), img)
        else:
            p.fillRect(img_r, QColor(40, 40, 40))
        tags, survived = index.data(Qt.ItemDataRole.UserRole)
        f = QFont(p.font())
        f.setBold(True)
        f.setPointSize(14)
        p.setFont(f)
        if "fav" in tags:
            p.setPen(QColor("#ffd700"))
            p.drawText(img_r.adjusted(4, 2, 0, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, "★")
        if "keep" in tags or "reject" in tags:
            keep = "keep" in tags
            p.setPen(QColor("#2ecc40" if keep else "#ff4136"))
            p.drawText(img_r.adjusted(0, 2, -4, 0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                       "✓" if keep else "✗")
        if is_video(name):
            p.setPen(QColor(255, 255, 255, 200))
            p.drawText(img_r, Qt.AlignmentFlag.AlignCenter, "▶")
        f.setPointSize(8)
        p.setFont(f)
        if survived:
            p.setPen(QColor("#7fdbff"))
            p.drawText(img_r.adjusted(4, 0, 0, -2), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                       f"R{survived}")
        x = img_r.right() - 12
        p.setPen(Qt.PenStyle.NoPen)
        for t in sorted(tags - {"fav", *VERDICTS}):
            p.setBrush(tag_color(t, self.win.slots))
            p.drawEllipse(x, img_r.bottom() - 12, 10, 10)
            x -= 13
        p.setPen(opt.palette.text().color())
        f.setBold(False)
        f.setPointSize(9)
        p.setFont(f)
        label = p.fontMetrics().elidedText(name, Qt.TextElideMode.ElideMiddle, r.width())
        p.drawText(QRect(r.x(), r.bottom() - 16, r.width(), 16), Qt.AlignmentFlag.AlignCenter, label)
        p.restore()


class GridView(QListView):
    def __init__(self, model, delegate):
        super().__init__()
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setGridSize(CELL)
        self.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.setModel(model)
        self.setItemDelegate(delegate)

    def keyboardSearch(self, text):
        pass  # letters are app shortcuts, not type-ahead


class HelpOverlay(QLabel):
    def __init__(self, parent, rows):
        super().__init__(parent)
        html = "".join(f"<tr><td style='padding-right:18px'><b>{k}</b></td><td>{d}</td></tr>" for k, d in rows)
        self.setText(f"<h3>snapsort keys</h3><table>{html}</table><p>Esc / ? closes</p>")
        self.setStyleSheet("background:rgba(0,0,0,215); color:#eee; padding:24px; font-size:13px")
        self.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.hide()

    def toggle(self):
        self.setVisible(not self.isVisible())
        if self.isVisible():
            self.setGeometry(self.parentWidget().rect())
            self.raise_()
