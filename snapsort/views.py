"""Single-image/video view, thumbnail grid and help overlay."""
import time

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QImageReader, QMovie, QPainter, QPen, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QLabel, QListView, QStackedWidget, QStyle, QStyledItemDelegate, QWidget

from .store import is_video

CELL = QSize(196, 216)
KEEP_COLOR, REJECT_COLOR, FAV_COLOR = QColor("#2e7d32"), QColor("#c62828"), QColor("#f9a825")


def theme_icon(*names) -> QIcon:
    for n in names:
        i = QIcon.fromTheme(n)
        if not i.isNull():
            return i
    return QIcon()


def draw_badge(p: QPainter, right: int, top: int, text: str, bg: QColor) -> int:
    """Rounded label ending at x=right; returns its left edge."""
    f = QFont(p.font())
    f.setBold(True)
    f.setPointSize(12)
    p.setFont(f)
    w = p.fontMetrics().horizontalAdvance(text) + 20
    h = p.fontMetrics().height() + 12
    r = QRectF(right - w, top, w, h)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(bg)
    p.drawRoundedRect(r, 6, 6)
    p.setPen(QColor("white"))
    p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
    return int(r.left())


class ImageCanvas(QWidget):
    resized = Signal()

    def __init__(self):
        super().__init__()
        self.pix: QPixmap | None = None
        self.placeholder = False
        self.text = ""
        self.tags: set = set()
        self.setMinimumSize(200, 150)

    def show_image(self, img: QImage | None, text="", placeholder=False):
        self.pix = QPixmap.fromImage(img) if img is not None and not img.isNull() else None
        self.placeholder = placeholder
        self.text = text
        self.update()

    def set_tags(self, tags):
        self.tags = tags
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
            size = self.pix.size() / self.devicePixelRatioF()
            if self.placeholder or size.width() > self.width() or size.height() > self.height():
                size = size.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            r = QRect((self.width() - size.width()) // 2, (self.height() - size.height()) // 2,
                      size.width(), size.height())
            p.drawPixmap(r, self.pix)
        if self.text:
            p.setPen(QColor(220, 220, 220))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        x = self.width() - 12
        if "keep" in self.tags:
            x = draw_badge(p, x, 12, "✓ Behalten", KEEP_COLOR) - 8
        elif "reject" in self.tags:
            x = draw_badge(p, x, 12, "✗ Verworfen", REJECT_COLOR) - 8
        if "fav" in self.tags:
            draw_badge(p, x, 12, "★", FAV_COLOR)


class SingleView(QStackedWidget):
    navigate = Signal(int)
    back = Signal()

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
        self.last_wheel = 0.0

    def wheelEvent(self, e):
        now = time.monotonic()
        dy = e.angleDelta().y()
        if dy and now - self.last_wheel > 0.15:
            self.last_wheel = now
            self.navigate.emit(-1 if dy > 0 else 1)
        e.accept()

    def mouseDoubleClickEvent(self, e):
        self.back.emit()

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

    def kind(self) -> str:
        return {self.video: "video", self.anim: "anim"}.get(self.currentWidget(), "image")

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
            return self.win.store.tags(name)
        return None

    def _ready(self, path, *_):
        row = self.rows.get(path)
        if row is not None:
            self.refresh(row)

    def refresh(self, row):
        i = self.index(row)
        self.dataChanged.emit(i, i)


class GridDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return CELL

    def paint(self, p: QPainter, opt, index):
        r = opt.rect.adjusted(5, 5, -5, -5)
        p.save()
        img_r = QRect(r.x(), r.y(), r.width(), r.height() - 20)
        img = index.data(Qt.ItemDataRole.DecorationRole)
        name = index.data()
        tags = index.data(Qt.ItemDataRole.UserRole)
        if img is not None and not img.isNull():
            s = img.size().scaled(img_r.size(), Qt.AspectRatioMode.KeepAspectRatio)
            img_r = QRect(img_r.x() + (img_r.width() - s.width()) // 2,
                          img_r.y() + (img_r.height() - s.height()) // 2, s.width(), s.height())
            p.drawImage(img_r, img)
        else:
            p.fillRect(img_r, QColor(60, 60, 60, 120))
        if "reject" in tags:
            p.fillRect(img_r, QColor(0, 0, 0, 140))
        if is_video(name):
            f = QFont(p.font())
            f.setPointSize(22)
            p.setFont(f)
            p.setPen(QColor(255, 255, 255, 220))
            p.drawText(img_r, Qt.AlignmentFlag.AlignCenter, "▶")
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        x = img_r.right() - 4
        if "keep" in tags:
            x = draw_badge(p, x, img_r.top() + 4, "✓", KEEP_COLOR) - 4
        elif "reject" in tags:
            x = draw_badge(p, x, img_r.top() + 4, "✗", REJECT_COLOR) - 4
        if "fav" in tags:
            draw_badge(p, x, img_r.top() + 4, "★", FAV_COLOR)
        if opt.state & QStyle.StateFlag.State_Selected:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(opt.palette.highlight().color(), 3))
            p.drawRoundedRect(QRectF(opt.rect).adjusted(2, 2, -2, -2), 4, 4)
        f = QFont(opt.font)
        f.setPointSize(9)
        p.setFont(f)
        p.setPen(opt.palette.text().color())
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
        self.setSpacing(0)
        self.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setModel(model)
        self.setItemDelegate(delegate)
        self.empty = QLabel(self.viewport(), alignment=Qt.AlignmentFlag.AlignCenter, wordWrap=True)
        self.empty.hide()

    def show_empty(self, text):
        self.empty.setText(text)
        self.empty.setVisible(bool(text))
        self.empty.setGeometry(self.viewport().rect())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.empty.setGeometry(self.viewport().rect())

    def keyboardSearch(self, text):
        pass  # letters are app shortcuts, not type-ahead


class HelpOverlay(QLabel):
    def __init__(self, parent):
        super().__init__(parent)
        self.setStyleSheet("background:rgba(0,0,0,215); color:#eee; padding:24px; font-size:13px")
        self.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.hide()

    def set_rows(self, rows):
        html = "".join(f"<tr><td style='padding-right:18px'><b>{k}</b></td><td>{d}</td></tr>" for k, d in rows)
        self.setText(f"<h3>Tastenkürzel</h3><table>{html}</table><p>Esc oder ? schließt</p>")

    def toggle(self):
        self.setVisible(not self.isVisible())
        if self.isVisible():
            self.setGeometry(self.parentWidget().rect())
            self.raise_()
