"""Edit mode: pen, box, arrow, text, pixelate and blur on the full-resolution image."""
import math

from PySide6.QtCore import QLineF, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (QAction, QActionGroup, QColor, QFont, QImage, QKeySequence, QPainter, QPainterPath,
                           QPen, QPixmap, QTransform)
from PySide6.QtWidgets import QColorDialog, QInputDialog, QSpinBox, QToolBar, QVBoxLayout, QWidget

TOOLS = [("pen", "P", "Pen"), ("box", "B", "Box"), ("arrow", "A", "Arrow"), ("text", "T", "Text"),
         ("pixelate", "C", "Censor (pixelate)"), ("blur", "U", "Blur")]


def pixelate(img: QImage, r: QRect) -> QImage:
    sub = img.copy(r)
    b = max(8, min(r.width(), r.height()) // 10)
    small = sub.scaled(max(1, r.width() // b), max(1, r.height() // b),
                       Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
    return small.scaled(r.size(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)


def blur(img: QImage, r: QRect) -> QImage:
    sub = img.copy(r)
    for _ in range(2):
        small = sub.scaled(max(1, r.width() // 16), max(1, r.height() // 16),
                           Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        sub = small.scaled(r.size(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    return sub


def arrow_path(a: QPointF, b: QPointF, width) -> QPainterPath:
    path = QPainterPath(a)
    path.lineTo(b)
    ang = math.atan2(b.y() - a.y(), b.x() - a.x())
    head = 4 * width + 10
    for d in (math.radians(155), math.radians(-155)):
        path.moveTo(b)
        path.lineTo(b + QPointF(math.cos(ang + d), math.sin(ang + d)) * head)
    return path


class _Canvas(QWidget):
    def __init__(self, ed):
        super().__init__()
        self.ed = ed
        self.xf = QTransform()
        self.start: QPointF | None = None
        self.cur: QPointF | None = None
        self.path: QPainterPath | None = None
        self.view: QPixmap | None = None
        self.setMouseTracking(False)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def resizeEvent(self, e):
        img = self.ed.image
        s = min(self.width() / img.width(), self.height() / img.height(), 1.0)
        self.xf = QTransform.fromTranslate((self.width() - img.width() * s) / 2,
                                           (self.height() - img.height() * s) / 2).scale(s, s)
        self.refresh()

    def refresh(self):
        """Re-render the display-sized copy after the image changed."""
        r = self.xf.mapRect(QRectF(self.ed.image.rect())).toAlignedRect()
        self.view = QPixmap.fromImage(self.ed.image.scaled(
            r.size() * self.devicePixelRatioF(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        self.view.setDevicePixelRatio(self.devicePixelRatioF())
        self.update()

    def _img_pos(self, e) -> QPointF:
        return self.xf.inverted()[0].map(e.position())

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        p = self._img_pos(e)
        if self.ed.tool == "text":
            self.ed.add_text(p)
            return
        self.start = self.cur = p
        self.path = QPainterPath(p)

    def mouseMoveEvent(self, e):
        if self.start is None:
            return
        self.cur = self._img_pos(e)
        if self.ed.tool == "pen":
            self.path.lineTo(self.cur)
        self.update()

    def mouseReleaseEvent(self, e):
        if self.start is None:
            return
        self.ed.commit(self.start, self.cur, self.path)
        self.start = self.cur = self.path = None
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(16, 16, 16))
        if self.view:
            p.drawPixmap(self.xf.map(QPointF(0, 0)), self.view)
        p.setTransform(self.xf)
        if self.start is None:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(self.ed.pen())
        t = self.ed.tool
        if t == "pen":
            p.drawPath(self.path)
        elif t == "arrow":
            p.drawPath(arrow_path(self.start, self.cur, self.ed.width.value()))
        else:
            if t in ("pixelate", "blur"):
                p.setPen(QPen(QColor(255, 255, 255), 0, Qt.PenStyle.DashLine))
            p.drawRect(QRectF(self.start, self.cur).normalized())


class EditView(QWidget):
    save_requested = Signal()
    close_requested = Signal()

    def __init__(self, image: QImage):
        super().__init__()
        self.image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        self.undo: list[tuple[QRect, QImage]] = []
        self.tool = "pixelate"
        self.color = QColor("#ff2020")
        bar = QToolBar()
        group = QActionGroup(self)
        for key, sc, label in TOOLS:
            a = QAction(f"{label} ({sc})", self, checkable=True, shortcut=QKeySequence(sc))
            a.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            a.setChecked(key == self.tool)
            a.triggered.connect(lambda _=False, k=key: setattr(self, "tool", k))
            group.addAction(a)
            bar.addAction(a)
            self.addAction(a)
        bar.addSeparator()
        self.color_act = bar.addAction("Color", self.pick_color)
        self.width = QSpinBox(minimum=1, maximum=500, value=max(3, self.image.width() // 300))
        self.width.setPrefix("width ")
        bar.addWidget(self.width)
        bar.addSeparator()
        for label, sc, fn in [("Undo", "Ctrl+Z", self.undo_step), ("Save (overwrite)", "Ctrl+S", self.save_requested.emit),
                              ("Cancel", "Esc", self.close_requested.emit), (None, "[", lambda: self._nudge(-1)),
                              (None, "]", lambda: self._nudge(1))]:
            a = QAction(f"{label} ({sc})" if label else sc, self, shortcut=QKeySequence(sc))
            a.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            a.triggered.connect(fn)
            self.addAction(a)
            if label:
                bar.addAction(a)
        self._update_color_icon()
        self.canvas = _Canvas(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(bar)
        lay.addWidget(self.canvas, 1)

    @property
    def dirty(self):
        return bool(self.undo)

    def _nudge(self, d):
        self.width.setValue(self.width.value() + d * max(1, self.width.value() // 5))

    def pen(self):
        return QPen(self.color, self.width.value(), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin)

    def pick_color(self):
        c = QColorDialog.getColor(self.color, self)
        if c.isValid():
            self.color = c
            self._update_color_icon()

    def _update_color_icon(self):
        self.color_act.setText(f"Color ■ {self.color.name()}")

    def _push(self, rect: QRectF) -> QRect:
        r = rect.toAlignedRect().intersected(self.image.rect())
        self.undo.append((r, self.image.copy(r)))
        return r

    def commit(self, a: QPointF, b: QPointF, path: QPainterPath):
        w = self.width.value()
        t = self.tool
        if t in ("pixelate", "blur", "box"):
            rect = QRectF(a, b).normalized()
            if rect.width() < 2 or rect.height() < 2:
                return
        if t in ("pixelate", "blur"):
            r = self._push(rect)
            if r.isEmpty():
                self.undo.pop()
                return
            p = QPainter(self.image)
            p.drawImage(r.topLeft(), (pixelate if t == "pixelate" else blur)(self.image, r))
        else:
            shape = path if t == "pen" else arrow_path(a, b, w) if t == "arrow" else None
            bounds = shape.boundingRect() if shape else rect
            self._push(bounds.adjusted(-w, -w, w, w))
            p = QPainter(self.image)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(self.pen())
            p.drawPath(shape) if shape else p.drawRect(rect)
        p.end()
        self.canvas.refresh()

    def add_text(self, pos: QPointF):
        text, ok = QInputDialog.getText(self, "Text", "Text:")
        if not ok or not text:
            return
        f = QFont()
        f.setPixelSize(max(12, self.width.value() * 6))
        f.setBold(True)
        p = QPainter(self.image)
        p.setFont(f)
        br = QRectF(p.fontMetrics().boundingRect(text)).translated(pos)
        p.end()
        self._push(br.adjusted(-4, -4, 4, 4))
        p = QPainter(self.image)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(f)
        p.setPen(self.color)
        p.drawText(pos, text)
        p.end()
        self.canvas.refresh()

    def undo_step(self):
        if not self.undo:
            return
        r, patch = self.undo.pop()
        p = QPainter(self.image)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.drawImage(r.topLeft(), patch)
        p.end()
        self.canvas.refresh()
