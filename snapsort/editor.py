"""Edit mode: censor (pixelate, blur, black out), pen, box, arrow, text, crop/letterbox to a fixed aspect."""
import math

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QAction, QActionGroup, QColor, QFont, QIcon, QImage, QKeySequence, QPainter, QPainterPath,
                           QPen, QPixmap, QTransform)
from PySide6.QtWidgets import (QColorDialog, QComboBox, QInputDialog, QSizePolicy, QSpinBox, QToolBar,
                               QVBoxLayout, QWidget)

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
    None,
    ("format", "Z", "Format", ("transform-crop", "crop"), "⬚ Format",
     "Rahmen verschieben, an Ecken ziehen oder Mausrad · dann „Ausschnitt übernehmen“ oder „Ganz einpassen“"),
]
HINTS = {t[0]: t[5] for t in TOOLS if t}
REGION_TOOLS = ("pixelate", "black", "blur")
DRAW_TOOLS = ("pen", "box", "arrow", "text")
RATIOS = [("1:1", 1.0), ("4:5 · Instagram hoch", 4 / 5), ("1,91:1 · Instagram quer", 1.91), ("3:4", 3 / 4),
          ("4:3", 4 / 3), ("2:3", 2 / 3), ("3:2", 3 / 2), ("9:16 · Story", 9 / 16), ("16:9", 16 / 9)]
FILLS = [("Rand: Weiß", "white"), ("Rand: Schwarz", "black"), ("Rand: unscharfes Bild", "blur"),
         ("Rand: gewählte Farbe", "color")]


def fit_rect(w: float, h: float, ratio: float) -> QRectF:
    """Largest rect with width/height == ratio, centered inside w x h."""
    cw, ch = (w, w / ratio) if w / h < ratio else (h * ratio, h)
    return QRectF((w - cw) / 2, (h - ch) / 2, cw, ch)


def clamp_rect(r: QRectF, w: float, h: float) -> QRectF:
    """Move r (not resize) so it lies inside 0..w x 0..h."""
    return QRectF(min(max(r.x(), 0), w - r.width()), min(max(r.y(), 0), h - r.height()), r.width(), r.height())


def letterbox(img: QImage, ratio: float, fill) -> QImage:
    """Whole image centered on a canvas of the given aspect; the bars are a colour or a blurred copy."""
    w, h = img.width(), img.height()
    cw, ch = (w, round(w / ratio)) if w / h > ratio else (round(h * ratio), h)
    out = QImage(cw, ch, QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(out)
    if fill == "blur":
        cover = img.scaled(cw, ch, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           Qt.TransformationMode.SmoothTransformation)
        cover = cover.copy((cover.width() - cw) // 2, (cover.height() - ch) // 2, cw, ch)
        p.drawImage(0, 0, blur(cover, cover.rect()))
        p.fillRect(out.rect(), QColor(0, 0, 0, 60))
    else:
        p.fillRect(out.rect(), QColor(fill))
    p.drawImage((cw - w) // 2, (ch - h) // 2, img)
    p.end()
    return out


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
        self.drag: tuple | None = None  # format tool: ("move", offset) or ("resize", anchor)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def resizeEvent(self, e):
        self.relayout()

    def relayout(self):
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
        if self.ed.tool == "format":
            c = self.ed.crop
            grab = 16 / self.xf.m11()  # 16 screen px in image px
            corners = [c.topLeft(), c.topRight(), c.bottomLeft(), c.bottomRight()]
            near = [i for i, q in enumerate(corners) if abs(q.x() - p.x()) < grab and abs(q.y() - p.y()) < grab]
            if near:
                self.drag = ("resize", corners[3 - near[0]])  # opposite corner stays put
            elif c.contains(p):
                self.drag = ("move", p - c.topLeft())
            return
        if self.ed.tool == "text":
            self.ed.add_text(p)
            return
        self.start = self.cur = p
        self.path = QPainterPath(p)

    def mouseMoveEvent(self, e):
        if self.drag:
            self._drag_to(self._img_pos(e))
            return
        if self.start is None:
            return
        self.cur = self._img_pos(e)
        if self.ed.tool == "pen":
            self.path.lineTo(self.cur)
        self.update()

    def _drag_to(self, p: QPointF):
        kind, ref = self.drag
        w, h = self.ed.image.width(), self.ed.image.height()
        c = self.ed.crop
        if kind == "move":
            c = clamp_rect(QRectF(p - ref, c.size()), w, h)
        else:
            ratio = self.ed.ratio()
            dx, dy = p.x() - ref.x(), p.y() - ref.y()
            # room from the anchor towards the dragged corner
            room_w = w - ref.x() if dx >= 0 else ref.x()
            room_h = h - ref.y() if dy >= 0 else ref.y()
            cw = max(abs(dx), abs(dy) * ratio, 32)
            cw = min(cw, room_w, room_h * ratio)
            ch = cw / ratio
            c = QRectF(ref.x() if dx >= 0 else ref.x() - cw, ref.y() if dy >= 0 else ref.y() - ch, cw, ch)
        self.ed.crop = c
        self.update()

    def wheelEvent(self, e):
        if self.ed.tool != "format" or not e.angleDelta().y():
            return
        img = self.ed.image
        c = self.ed.crop
        f = 1 / 1.1 if e.angleDelta().y() > 0 else 1.1
        full = fit_rect(img.width(), img.height(), self.ed.ratio())
        cw = min(max(c.width() * f, 32), full.width())
        ch = cw / self.ed.ratio()
        r = QRectF(c.center().x() - cw / 2, c.center().y() - ch / 2, cw, ch)
        self.ed.crop = clamp_rect(r, img.width(), img.height())
        self.update()

    def mouseReleaseEvent(self, e):
        if self.drag:
            self.drag = None
            return
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
        if self.ed.tool == "format":
            self._paint_crop(p)
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


    def _paint_crop(self, p: QPainter):
        img_r = self.xf.mapRect(QRectF(self.ed.image.rect()))
        c = self.xf.mapRect(self.ed.crop)
        outside = QPainterPath()
        outside.addRect(img_r)
        inner = QPainterPath()
        inner.addRect(c)
        p.fillPath(outside.subtracted(inner), QColor(0, 0, 0, 150))
        p.setPen(QPen(QColor(255, 255, 255, 110), 1))
        for i in (1, 2):  # rule of thirds
            x = c.left() + c.width() * i / 3
            y = c.top() + c.height() * i / 3
            p.drawLine(QPointF(x, c.top()), QPointF(x, c.bottom()))
            p.drawLine(QPointF(c.left(), y), QPointF(c.right(), y))
        p.setPen(QPen(QColor("white"), 2))
        p.drawRect(c)
        p.setBrush(QColor("white"))
        for q in (c.topLeft(), c.topRight(), c.bottomLeft(), c.bottomRight()):
            p.drawRect(QRectF(q.x() - 5, q.y() - 5, 10, 10))
        p.setBrush(Qt.BrushStyle.NoBrush)

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
    save_copy_requested = Signal()
    close_requested = Signal()

    def __init__(self, image: QImage):
        super().__init__()
        self.image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        self.undo: list[tuple[QRect | None, QImage]] = []  # None: whole image (size changed)
        self.crop = QRectF()
        self.copy_tag = "bearbeitet"  # file name suffix for "save as copy", e.g. "1x1" after a format change
        self.tool = "pixelate"
        self.color = QColor("#ff2020")
        bar = QToolBar()
        bar.setIconSize(QSize(28, 28))
        group = QActionGroup(self)
        self.tool_acts: dict[str, QAction] = {}
        for t in TOOLS:
            if t is None:
                bar.addSeparator()
                continue
            key, sc, label, icons, fallback, _ = t
            a = self._action(label, sc, icons, fallback, lambda _=False, k=key: self.set_tool(k), checkable=True)
            a.setChecked(key == self.tool)
            self.tool_acts[key] = a
            group.addAction(a)
            bar.addAction(a)
            bar.widgetForAction(a).setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        opts = self.opts = QToolBar()  # options of the selected tool, second row
        self.color_act = QAction("Farbe", self, triggered=self.pick_color)
        self.width = QSpinBox(minimum=1, maximum=500, value=max(3, self.image.width() // 300))
        self.width.setPrefix("Stärke ")
        self.width.setSuffix(" px")
        self.width.setToolTip("Strichstärke ([ und ])")
        opts.addAction(self.color_act)
        self.style_acts = [self.color_act, opts.addWidget(self.width)]
        self.ratio_box = QComboBox()
        for label, r in RATIOS:
            self.ratio_box.addItem(label, r)
        self.ratio_box.setToolTip("Seitenverhältnis")
        self.ratio_box.currentIndexChanged.connect(lambda _: self.reset_crop())
        self.crop_act = self._action("Ausschnitt übernehmen", "Return", ("transform-crop-and-resize", "transform-crop"),
                                     "✂ Ausschnitt", self.apply_crop)
        self.fill_box = QComboBox()
        for label, f in FILLS:
            self.fill_box.addItem(label, f)
        self.fill_box.setToolTip("Womit der Rand beim Einpassen gefüllt wird")
        self.fit_act = self._action("Ganz einpassen", "Shift+Return", ("zoom-fit-best", "zoom-fit-page"),
                                    "⬚ Einpassen", self.apply_fit)
        self.fit_act.setToolTip("Ganzes Bild ins Format, Rand wird gefüllt (Umschalt+Eingabe)")
        self.format_acts = [opts.addWidget(self.ratio_box)]
        opts.addAction(self.crop_act)
        self.format_acts += [self.crop_act, opts.addSeparator(), opts.addWidget(self.fill_box)]
        opts.addAction(self.fit_act)
        self.format_acts.append(self.fit_act)
        for a in (self.crop_act, self.fit_act):
            opts.widgetForAction(a).setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        for sc, d in (("[", -1), ("]", 1)):
            self._action(sc, sc, (), sc, lambda _=False, d=d: self._nudge(d))
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        opts.addWidget(spacer)
        self.undo_act = self._action("Rückgängig", "Ctrl+Z", ("edit-undo",), "↶", self.undo_step)
        cancel = self._action("Abbrechen", "Esc", ("dialog-cancel",), "✗", self.close_requested.emit)
        self.save_act = self._action("Speichern (überschreibt Original)", "Ctrl+S", ("document-save",), "💾",
                                     self.save_requested.emit)
        self.save_act.setToolTip("Überschreibt die Originaldatei. Bis zum Beenden mit Strg+Z rückgängig zu machen.")
        self.copy_act = self._action("Als Kopie speichern", "Ctrl+Shift+S", ("document-save-as",), "💾 Kopie",
                                     self.save_copy_requested.emit)
        self.copy_act.setToolTip("Neue Datei neben dem Original, das Original bleibt unverändert (Strg+Umschalt+S)")
        opts.addAction(self.undo_act)
        opts.addSeparator()
        for a in (cancel, self.copy_act, self.save_act):
            opts.addAction(a)
            opts.widgetForAction(a).setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        f = opts.widgetForAction(self.save_act).font()
        f.setBold(True)
        opts.widgetForAction(self.save_act).setFont(f)
        self.bar = bar
        self._update_color_icon()
        self.canvas = _Canvas(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(bar)
        lay.addWidget(opts)
        lay.addWidget(self.canvas, 1)
        self.reset_crop()
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
        self.tool_acts[key].setChecked(True)
        for a in self.style_acts:
            a.setVisible(key in DRAW_TOOLS)
        for a in self.format_acts:
            a.setVisible(key == "format")
        if hasattr(self, "canvas"):
            self.canvas.update()

    def _sync(self):
        for a in (self.undo_act, self.save_act, self.copy_act):
            a.setEnabled(self.dirty)

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

    def ratio(self) -> float:
        return self.ratio_box.currentData()

    def reset_crop(self):
        self.crop = fit_rect(self.image.width(), self.image.height(), self.ratio())
        if hasattr(self, "canvas"):
            self.canvas.update()

    def _replace_image(self, img: QImage):
        self.copy_tag = self.ratio_box.currentText().split(" ")[0].replace(":", "x").replace(",", "_")
        self.undo.append((None, self.image))
        self.image = img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        self.reset_crop()
        self.canvas.relayout()
        self._sync()

    def apply_crop(self):
        if self.tool == "format":
            self._replace_image(self.image.copy(self.crop.toAlignedRect().intersected(self.image.rect())))

    def apply_fit(self):
        if self.tool == "format":
            fill = self.fill_box.currentData()
            self._replace_image(letterbox(self.image, self.ratio(), self.color if fill == "color" else fill))

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
        if r is None:
            self.image = patch
            self.reset_crop()
            self.canvas.relayout()
            self._sync()
            return
        p = QPainter(self.image)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.drawImage(r.topLeft(), patch)
        p.end()
        self.canvas.refresh()
        self._sync()
