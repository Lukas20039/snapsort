"""Edit mode: censor (pixelate, blur, black out), pen, box, arrow, text on the full-resolution image."""
import math

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QAction, QActionGroup, QColor, QFont, QIcon, QImage, QKeySequence, QPainter, QPainterPath,
                           QPen, QPixmap, QTransform)
from PySide6.QtWidgets import (QColorDialog, QInputDialog, QSizePolicy, QSpinBox, QToolBar, QVBoxLayout,
                               QWidget)

from .views import theme_icon

# key, shortcut, label, icons, text fallback, hint on the canvas
TOOLS = [
    ("pixelate", "C", "Zensieren", ("pixelate",), "▦ Zensieren",
     "Rechteck über Kennzeichen oder Gesicht ziehen"),
    ("black", "S", "Schwärzen", ("color-fill", "fill-color"), "■ Schwärzen",
     "Rechteck ziehen – wird komplett schwarz"),
    ("blur", "U", "Weichzeichnen", ("blur", "blurfx"), "◌ Weichzeichnen",
     "Rechteck ziehen – nur weich, für Kennzeichen besser Zensieren oder Schwärzen"),
    None,
    ("pen", "P", "Stift", ("draw-freehand",), "✎ Stift", "Mit gedrückter Maustaste zeichnen"),
    ("box", "B", "Rahmen", ("draw-rectangle",), "▭ Rahmen", "Rechteck ziehen"),
    ("arrow", "A", "Pfeil", ("draw-arrow", "draw-arrow-forward"), "➚ Pfeil", "Von Start zu Ziel ziehen"),
    ("text", "T", "Text", ("draw-text",), "T Text", "Klicken, wo der Text stehen soll"),
]
HINTS = {t[0]: t[5] for t in TOOLS if t}
REGION_TOOLS = ("pixelate", "black", "blur")


def pixelate(img: QImage, r: QRect) -> QImage:
    sub = img.copy(r)
    b = max(8, min(r.width(), r.height()) // 4)  # at most ~4 blocks across the short side
    small = sub.scaled(max(1, r.width() // b), max(1, r.height() // b),
                       Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
    return small.scaled(r.size(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)


def blur(img: QImage, r: QRect) -> QImage:
    sub = img.copy(r)
    s = max(4, min(r.width(), r.height()) // 4)  # ~4 samples across the short side
    for _ in range(2):
        small = sub.scaled(max(1, r.width() // s), max(1, r.height() // s),
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
        if self.start is None:
            self._hint(p)
            return
        p.setTransform(self.xf)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(self.ed.pen())
        t = self.ed.tool
        if t == "pen":
            p.drawPath(self.path)
        elif t == "arrow":
            p.drawPath(arrow_path(self.start, self.cur, self.ed.width.value()))
        else:
            if t in REGION_TOOLS:
                p.setPen(QPen(QColor(255, 255, 255), 0, Qt.PenStyle.DashLine))
            p.drawRect(QRectF(self.start, self.cur).normalized())


    def _hint(self, p: QPainter):
        f = QFont(self.font())
        f.setPointSize(11)
        p.setFont(f)
        text = HINTS[self.ed.tool]
        w = p.fontMetrics().horizontalAdvance(text) + 28
        r = QRectF((self.width() - w) / 2, 14, w, p.fontMetrics().height() + 14)
        p.setOpacity(0.5 if self.ed.dirty else 1.0)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 170))
        p.drawRoundedRect(r, 8, 8)
        p.setPen(QColor("white"))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)


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
        bar.setIconSize(QSize(28, 28))
        group = QActionGroup(self)
        for t in TOOLS:
            if t is None:
                bar.addSeparator()
                continue
            key, sc, label, icons, fallback, _ = t
            a = self._action(label, sc, icons, fallback, lambda _=False, k=key: self.set_tool(k), checkable=True)
            a.setChecked(key == self.tool)
            group.addAction(a)
            bar.addAction(a)
            bar.widgetForAction(a).setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        bar.addSeparator()
        self.color_act = QAction("Farbe", self, triggered=self.pick_color)
        self.width = QSpinBox(minimum=1, maximum=500, value=max(3, self.image.width() // 300))
        self.width.setPrefix("Stärke ")
        self.width.setSuffix(" px")
        self.width.setToolTip("Strichstärke ([ und ])")
        bar.addAction(self.color_act)
        self.style_acts = [self.color_act, bar.addWidget(self.width)]
        for sc, d in (("[", -1), ("]", 1)):
            self._action(sc, sc, (), sc, lambda _=False, d=d: self._nudge(d))
        bar.addSeparator()
        self.undo_act = self._action("Rückgängig", "Ctrl+Z", ("edit-undo",), "↶", self.undo_step)
        bar.addAction(self.undo_act)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        cancel = self._action("Abbrechen", "Esc", ("dialog-cancel",), "✗", self.close_requested.emit)
        self.save_act = self._action("Speichern (überschreibt Original)", "Ctrl+S", ("document-save",), "💾",
                                     self.save_requested.emit)
        self.save_act.setToolTip("Überschreibt die Originaldatei. Bis zum Beenden mit Strg+Z rückgängig zu machen.")
        for a in (cancel, self.save_act):
            bar.addAction(a)
            bar.widgetForAction(a).setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        f = bar.widgetForAction(self.save_act).font()
        f.setBold(True)
        bar.widgetForAction(self.save_act).setFont(f)
        self.bar = bar
        self._update_color_icon()
        self.canvas = _Canvas(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(bar)
        lay.addWidget(self.canvas, 1)
        self.set_tool(self.tool)
        self._sync()

    def _action(self, label, sc, icons, fallback, fn, checkable=False) -> QAction:
        a = QAction(label, self, checkable=checkable, shortcut=QKeySequence(sc))
        a.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        icon = theme_icon(*icons)
        a.setIcon(icon)
        if icon.isNull():
            a.setIconText(fallback)
        a.setToolTip(f"{label} ({QKeySequence(sc).toString(QKeySequence.SequenceFormat.NativeText)})")
        a.triggered.connect(fn)
        self.addAction(a)
        return a

    def set_tool(self, key):
        self.tool = key
        for a in self.style_acts:
            a.setVisible(key not in REGION_TOOLS)
        if hasattr(self, "canvas"):
            self.canvas.update()

    def _sync(self):
        self.undo_act.setEnabled(self.dirty)
        self.save_act.setEnabled(self.dirty)

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
        pm = QPixmap(16, 16)
        pm.fill(self.color)
        self.color_act.setIcon(QIcon(pm))
        self.color_act.setToolTip(f"Farbe wählen ({self.color.name()})")

    def _push(self, rect: QRectF) -> QRect:
        r = rect.toAlignedRect().intersected(self.image.rect())
        self.undo.append((r, self.image.copy(r)))
        return r

    def commit(self, a: QPointF, b: QPointF, path: QPainterPath):
        w = self.width.value()
        t = self.tool
        if t in (*REGION_TOOLS, "box"):
            rect = QRectF(a, b).normalized()
            if rect.width() < 2 or rect.height() < 2:
                return
        if t in REGION_TOOLS:
            r = self._push(rect)
            if r.isEmpty():
                self.undo.pop()
                return
            p = QPainter(self.image)
            if t == "black":
                p.fillRect(r, QColor("black"))
            else:
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
        self._sync()

    def add_text(self, pos: QPointF):
        text, ok = QInputDialog.getText(self, "Text einfügen", "Text:")
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
        self._sync()

    def undo_step(self):
        if not self.undo:
            return
        r, patch = self.undo.pop()
        p = QPainter(self.image)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.drawImage(r.topLeft(), patch)
        p.end()
        self.canvas.refresh()
        self._sync()
