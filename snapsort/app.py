"""Main window: one QAction per command (shared by keys, buttons, menus), culling rounds, move, undo."""
import os
import sys
from pathlib import Path

from PySide6.QtCore import QFile, QSettings, QSize, Qt, QTimer, QUrl
from PySide6.QtDBus import QDBusInterface, QDBusMessage
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QGuiApplication, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
                               QListView, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressBar,
                               QSizePolicy, QStackedWidget, QToolBar, QToolButton, QVBoxLayout, QWidget)

from . import imaging
from . import store as st
from .editor import EditView
from .store import Filter, TagStore
from .views import GridDelegate, GridModel, GridView, HelpOverlay, SingleView, theme_icon

S, G, E = "single", "grid", "edit"
UNDO_LIMIT = 20
# id, label, include, exclude
SEGMENTS = [("all", "Alle", (), ()), ("open", "Offen", (), ("keep", "reject")), ("keep", "Behalten", ("keep",), ()),
            ("reject", "Verworfen", ("reject",), ()), ("fav", "★ Favoriten", ("fav",), ())]


def keys_text(keys) -> str:
    return " / ".join(QKeySequence(k).toString(QKeySequence.SequenceFormat.NativeText) or k for k in keys)


def spacer() -> QWidget:
    w = QWidget()
    w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    return w


def kind_of(name) -> str:
    if st.is_video(name):
        return "video"
    return "gif" if name.lower().endswith(".gif") else "image"


class MainWindow(QMainWindow):
    def __init__(self, target=None):
        super().__init__()
        self.settings = QSettings("snapsort", "snapsort")
        self.loader = imaging.Loader(self)
        self.folder: Path | None = None
        self.names: list[str] = []
        self.view: list[str] = []
        self.store: TagStore | None = None
        self.segment = "all"
        self.sel_round = 0
        self.index = 0
        self.mode = G
        self.undo: list[tuple[str, callable]] = []
        self.editor: EditView | None = None
        self.edit_path: str | None = None
        self.edit_return = G
        self.lossy_ok = False
        self.combo_round = None

        self.single = SingleView()
        self.single.navigate.connect(lambda d: self.mode == S and self.go(d))
        self.single.back.connect(lambda: self.mode == S and self.set_mode(G))
        self.single.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.single.customContextMenuRequested.connect(self._single_menu)
        self.model = GridModel(self)
        self.grid = GridView(self.model, GridDelegate())
        self.grid.selectionModel().currentChanged.connect(self._grid_current)
        self.grid.doubleClicked.connect(lambda i: self.set_mode(S))
        self.grid.customContextMenuRequested.connect(self._grid_menu)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.grid)
        self.stack.addWidget(self.single)
        self.setCentralWidget(self.stack)
        self.loader.imageReady.connect(self._image_ready)
        self.loader.thumbReady.connect(self._thumb_ready)
        self.loader.failed.connect(self._image_ready)
        self.resize_timer = QTimer(self, singleShot=True, interval=150, timeout=self.show_current)
        self.single.canvas.resized.connect(self.resize_timer.start)

        self.help = HelpOverlay(self.stack)
        self._build_actions()
        self._build_toolbars()
        self.resize(1400, 900)
        self.open_target(Path(target or os.getcwd()))

    # ---------- actions and toolbars ----------
    def _build_actions(self):
        defs = [
            ("grid", "Übersicht", ["Esc"], ("view-grid",), "▦ Übersicht", self.to_grid, True),
            ("single", "Einzelbild", ["Return"], ("view-preview",), "▣ Einzelbild", lambda: self.set_mode(S), True),
            ("toggle", "Übersicht ↔ Einzelbild", ["G"], (), "", self.toggle_mode, False),
            ("prev", "Vorheriges", ["Left"], ("go-previous",), "◀", lambda: self.go(-1), False),
            ("next", "Nächstes", ["Right"], ("go-next",), "▶", lambda: self.go(1), False),
            ("first", "Erstes", ["Home"], ("go-first",), "", lambda: self.go_to(0), False),
            ("last", "Letztes", ["End"], ("go-last",), "", lambda: self.go_to(len(self.view) - 1), False),
            ("reject", "Verwerfen", ["X"], ("dialog-cancel", "emblem-error"), "✗ Verwerfen",
             lambda: self.verdict("reject"), True),
            ("fav", "Favorit", ["F"], ("emblem-favorite", "rating"), "★ Favorit", self.toggle_fav, True),
            ("keep", "Behalten", ["K"], ("dialog-ok-apply", "emblem-checked"), "✓ Behalten",
             lambda: self.verdict("keep"), True),
            ("rot_l", "Links drehen", ["L"], ("object-rotate-left",), "⟲", lambda: self.transform("ccw"), False),
            ("rot_r", "Rechts drehen", ["R"], ("object-rotate-right",), "⟳", lambda: self.transform("cw"), False),
            ("flip_h", "Horizontal spiegeln", ["H"], ("object-flip-horizontal",), "⇋",
             lambda: self.transform("fliph"), False),
            ("flip_v", "Vertikal spiegeln", ["V"], ("object-flip-vertical",), "⇵",
             lambda: self.transform("flipv"), False),
            ("edit", "Bearbeiten / Zensieren", ["E"], ("document-edit", "edit-image"), "✎ Bearbeiten",
             self.enter_edit, False),
            ("trash", "In den Papierkorb", ["Del"], ("user-trash", "edit-delete"), "🗑", self.trash, False),
            ("undo", "Rückgängig", ["Ctrl+Z"], ("edit-undo",), "↶", self.undo_last, False),
            ("move", "Verschieben…", ["M"], ("folder-move", "go-jump", "document-send"), "➜ Verschieben…",
             self.move_view, False),
            ("next_round", "Nächste Runde", ["N"], ("media-skip-forward", "arrow-right-double"), "⏭ Nächste Runde",
             self.next_round, False),
            ("play", "Abspielen / Pause", ["Space"], ("media-playback-start",), "⏯", self.single.toggle_play, False),
            ("seek_b", "5 s zurück", [","], ("media-seek-backward",), "−5s", lambda: self.single.seek(-5000), False),
            ("seek_f", "5 s vor", ["."], ("media-seek-forward",), "+5s", lambda: self.single.seek(5000), False),
            ("open", "Ordner öffnen…", ["Ctrl+O"], ("document-open-folder",), "", self.choose_folder, False),
            ("rescan", "Neu einlesen", ["F5"], ("view-refresh",), "", self.rescan, False),
            ("reveal", "Im Dateimanager zeigen", [], ("system-file-manager",), "", self.reveal, False),
            ("fullscreen", "Vollbild", ["F11"], ("view-fullscreen",), "", self.toggle_fullscreen, True),
            ("help", "Tastenkürzel", ["?", "F1"], ("help-keyboard-shortcuts", "help-keybord-shortcuts",
                                                   "input-keyboard"), "?", self.help.toggle, False),
            ("quit", "Beenden", ["Ctrl+Q"], ("application-exit",), "", self.close, False),
        ]
        defs += [(f"seg_{sid}", label, [f"Ctrl+{i}"], (), label, lambda _=False, s=sid: self.set_segment(s), True)
                 for i, (sid, label, *_) in enumerate(SEGMENTS)]
        self.act: dict[str, QAction] = {}
        modes = QActionGroup(self)
        segs = QActionGroup(self)
        for aid, label, keys, icons, fallback, fn, checkable in defs:
            a = QAction(label, self, checkable=checkable)
            a.setShortcuts([QKeySequence(k) for k in keys])
            a.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            a.setShortcutVisibleInContextMenu(True)
            icon = theme_icon(*icons)
            a.setIcon(icon)
            if fallback and (icon.isNull() or aid.startswith("seg_")):
                a.setIconText(fallback)
            a.setToolTip(f"{label} ({keys_text(keys)})" if keys else label)
            a.triggered.connect(lambda _=False, fn=fn: fn())
            self.addAction(a)
            self.act[aid] = a
            if aid in ("grid", "single"):
                modes.addAction(a)
            if aid.startswith("seg_"):
                segs.addAction(a)
        self.help.set_rows([(keys_text(keys), label) for aid, label, keys, *_ in defs if keys])

    def _bar(self, title, area=Qt.ToolBarArea.TopToolBarArea) -> QToolBar:
        tb = QToolBar(title)
        tb.setMovable(False)
        self.addToolBar(area, tb)
        return tb

    def _add(self, tb, aid, style=Qt.ToolButtonStyle.ToolButtonIconOnly):
        a = self.act[aid]
        tb.addAction(a)
        btn = tb.widgetForAction(a)
        btn.setToolButtonStyle(style if not a.icon().isNull() else Qt.ToolButtonStyle.ToolButtonTextOnly)
        return btn

    def _build_toolbars(self):
        beside = Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        tb = self.top = self._bar("Werkzeuge")
        self._add(tb, "grid", beside)
        self._add(tb, "single", beside)
        tb.addSeparator()
        for aid in ("rot_l", "rot_r", "flip_h", "flip_v"):
            self._add(tb, aid)
        tb.addSeparator()
        self._add(tb, "edit", beside)
        tb.addSeparator()
        self._add(tb, "trash")
        self._add(tb, "undo")
        tb.addWidget(spacer())
        self._add(tb, "move", beside)
        menu = QMenu(self)
        for aid in ("open", "rescan", "reveal", "fullscreen", "help", None, "quit"):
            menu.addSeparator() if aid is None else menu.addAction(self.act[aid])
        self.menu_btn = QToolButton(popupMode=QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu_btn.setIcon(theme_icon("application-menu", "open-menu-symbolic"))
        if self.menu_btn.icon().isNull():
            self.menu_btn.setText("≡")
        self.menu_btn.setToolTip("Menü")
        self.menu_btn.setMenu(menu)
        tb.addWidget(self.menu_btn)

        self.addToolBarBreak()
        sb = self.sichten = self._bar("Sichten")
        for sid, *_ in SEGMENTS:
            self._add(sb, f"seg_{sid}", Qt.ToolButtonStyle.ToolButtonTextOnly)
        sb.addSeparator()
        self.round_combo = QComboBox()
        self.round_combo.setToolTip("Frühere Runden ansehen. Entscheiden geht nur in der aktuellen Runde.")
        self.round_combo.currentIndexChanged.connect(self._round_selected)
        sb.addWidget(self.round_combo)
        self.progress_label = QLabel()
        self.progress_label.setContentsMargins(10, 0, 6, 0)
        sb.addWidget(self.progress_label)
        self.progress = QProgressBar(maximumWidth=120, textVisible=False)
        sb.addWidget(self.progress)
        sb.addWidget(spacer())
        self.next_round_btn = self._add(sb, "next_round", beside)

        bb = self.bottom = self._bar("Entscheiden", Qt.ToolBarArea.BottomToolBarArea)
        bb.setIconSize(QSize(24, 24))
        bb.addWidget(spacer())
        self._add(bb, "prev")
        for aid in ("reject", "fav", "keep"):
            btn = self._add(bb, aid, beside)
            btn.setMinimumHeight(36)
            f = btn.font()
            f.setBold(True)
            btn.setFont(f)
        self._add(bb, "next")
        bb.addSeparator()
        for aid in ("play", "seek_b", "seek_f"):
            self._add(bb, aid)
        bb.addWidget(spacer())
        self.position = QLabel()
        self.position.setMinimumWidth(260)
        self.position.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bb.addWidget(self.position)

    # ---------- folder / view ----------
    def open_target(self, target: Path):
        select = None
        if target.is_file():
            target, select = target.parent, target.name
        self.open_folder(target, select)

    def open_folder(self, folder: Path, select=None):
        folder = folder.resolve()
        try:
            names = st.list_media(folder)
        except OSError as e:
            self.flash(f"Ordner lässt sich nicht lesen: {e}", 8000)
            return
        self.single.stop()
        self.help.hide()
        self.folder, self.names = folder, names
        self.store = TagStore(folder)
        self.segment = "all"
        self.sel_round = self.store.round
        self.combo_round = None
        self.undo.clear()
        self.view, self.index = [], 0
        self.rebuild_view(select)
        self.set_mode(S if select else G)
        if self.store.warning:
            self.flash(self.store.warning, 10000)

    def rescan(self):
        cur = self.current()
        self.loader.errors.clear()
        self.loader.thumb_errors.clear()
        self.names = st.list_media(self.folder)
        self.rebuild_view(cur)
        self.show_current()

    def path(self, name) -> str:
        return str(self.folder / name)

    def current(self) -> str | None:
        return self.view[self.index] if 0 <= self.index < len(self.view) else None

    @property
    def filter(self) -> Filter:
        _, _, inc, exc = next(s for s in SEGMENTS if s[0] == self.segment)
        return Filter(include=set(inc), exclude=set(exc), min_round=self.sel_round)

    def round_set(self) -> list[str]:
        return [n for n in self.names if self.store.survived(n) >= self.sel_round]

    def rebuild_view(self, keep=None):
        keep = keep or self.current()
        f = self.filter
        self.view = [n for n in self.names if f.matches(self.store.tags(n), self.store.survived(n))]
        if keep in self.view:
            self.index = self.view.index(keep)
        else:
            later = [i for i, n in enumerate(self.view) if keep and st.natural_key(n) > st.natural_key(keep)]
            self.index = later[0] if later else max(0, len(self.view) - 1) if keep else 0
        self.model.reset()
        self._select_grid()
        if not self.view and self.mode == S:
            self.set_mode(G)
        self.update_status()

    def _select_grid(self):
        if self.view:
            i = self.model.index(self.index)
            self.grid.setCurrentIndex(i)
            self.grid.scrollTo(i)

    def _grid_current(self, cur, _prev):
        if cur.isValid() and self.mode == G:
            self.index = cur.row()
            self.update_status()

    def set_segment(self, sid):
        self.segment = sid
        self.rebuild_view()
        self.show_current()

    def _round_selected(self, i):
        r = self.round_combo.itemData(i)
        if r is not None and r != self.sel_round:
            self.sel_round = r
            self.segment = "all"  # verdicts belong to the current round only
            self.rebuild_view()
            self.show_current()

    # ---------- modes / navigation ----------
    def set_mode(self, mode):
        if mode == S and not self.view:
            mode = G
        self.mode = mode
        if mode == G:
            self.single.stop()
            self.stack.setCurrentWidget(self.grid)
            self._select_grid()
            self.grid.setFocus()
        elif mode == S:
            self.stack.setCurrentWidget(self.single)
            self.single.setFocus()
            self.show_current()
        self.update_status()

    def to_grid(self):
        if self.help.isVisible():
            self.help.hide()
            self.update_status()
        else:
            self.set_mode(G)

    def toggle_mode(self):
        self.set_mode(S if self.mode == G else G)

    def go(self, d):
        self.go_to(self.index + d)

    def go_to(self, i):
        if not self.view:
            return
        self.index = max(0, min(len(self.view) - 1, i))
        if self.mode == G:
            self._select_grid()
        self.show_current()
        self.update_status()

    def show_current(self):
        name = self.current()
        if self.mode != S or name is None:
            return
        p = self.path(name)
        kind = self.single.show_path(p)
        size = self.single.canvas.target_size()
        if kind == "image":
            img = self.loader.cached(p, size)
            if img is not None:
                self.single.canvas.show_image(img)
            elif p in self.loader.errors:
                self.single.canvas.show_image(None, f"{name} lässt sich nicht öffnen\n{self.loader.errors[p]}")
            else:
                self.single.canvas.show_image(self.loader.thumbs.get(p), placeholder=True)
        n = len(self.view)
        order = [self.index] + [self.index + d for d in (1, -1, 2, 3, -2, 4)]
        self.loader.prefetch([self.path(self.view[i]) for i in order if 0 <= i < n], size)

    def _image_ready(self, path, img_or_msg):
        name = self.current()
        if self.mode != S or name is None or path != self.path(name) \
                or self.single.currentWidget() is not self.single.canvas:
            return
        if isinstance(img_or_msg, str):
            if img_or_msg:  # empty: thumbnail failure or skipped preload
                self.single.canvas.show_image(None, f"{name} lässt sich nicht öffnen\n{img_or_msg}")
        else:
            self.single.canvas.show_image(img_or_msg)

    def _thumb_ready(self, path, img):
        name = self.current()
        if self.mode == S and name and path == self.path(name) and self.single.canvas.pix is None \
                and path not in self.loader.cache:
            self.single.canvas.show_image(img, placeholder=True)

    # ---------- deciding ----------
    def _changed(self, name):
        if name in self.view:
            self.model.refresh(self.view.index(name))
        if self.store.warning:
            self.flash(self.store.warning, 0)
        self.update_status()

    def toggle_fav(self):
        name = self.current()
        if name:
            self.store.toggle(name, "fav")
            self._changed(name)

    def verdict(self, tag):
        name = self.current()
        if not name or self.sel_round != self.store.round:
            return
        self.store.set_verdict(name, tag)
        self._changed(name)
        k, x, o = st.round_counts(self.store, self.round_set())
        set_now = tag in self.store.tags(name)
        if not o and set_now:
            self.flash(f"Alle {k + x} entschieden – weiter mit „Nächste Runde“ (N)", 6000)
        else:
            self.flash(f"{name}: {('behalten' if tag == 'keep' else 'verworfen') if set_now else 'offen'}", 2000)
        if set_now:
            self.go(1)

    # ---------- rounds ----------
    def next_round(self):
        if self.sel_round != self.store.round:
            return
        names = self.round_set()
        k, x, o = st.round_counts(self.store, names)
        r = self.store.round
        keep_open = True
        if o:
            box = QMessageBox(self)
            box.setWindowTitle(f"Runde {r + 1} abschließen")
            box.setText(f"{k} behalten, {x} verworfen, <b>{o} noch offen</b>.<br>Was passiert mit den offenen?")
            take = box.addButton("Offene mitnehmen", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Offene aussortieren", QMessageBox.ButtonRole.AcceptRole)
            cancel = box.addButton("Abbrechen", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(take)
            box.exec()
            if box.clickedButton() in (cancel, None):
                return
            keep_open = box.clickedButton() is take
        if not k and not (o and keep_open):
            self.flash("Nichts behalten – mindestens ein Foto behalten", 6000)
            return
        old = (self.segment, self.sel_round)
        snap = st.next_round(self.store, names, undecided_keep=keep_open)
        if self.store.warning:
            self.flash(self.store.warning, 0)

        def undo():
            st.restore_snapshot(self.store, snap)
            self.segment, self.sel_round = old
        self.push_undo(f"Runde {r + 2} starten", undo)
        self.segment, self.sel_round = "all", self.store.round
        self.view, self.index = [], 0
        self.rebuild_view()
        self.show_current()
        if not self.store.warning:
            self.flash(f"Runde {self.store.round + 1}: {len(self.view)} Fotos", 6000)

    # ---------- move / trash ----------
    def move_view(self):
        self.rebuild_view()  # decisions made since the last filter change must count
        if not self.view:
            return
        recent = list(self.settings.value("recent_targets", []) or [])
        if isinstance(recent, str):
            recent = [recent]
        d = MoveDialog(self, recent)
        if not d.exec():
            return
        name = d.target()
        dst = self.folder / name
        try:
            res = st.execute_move(self.store, st.plan_move(d.names, dst), dst)
        except OSError as e:
            self.flash(f"Verschieben fehlgeschlagen: {e}", 8000)
            return
        self.settings.setValue("recent_targets", [name] + [r for r in recent if r != name][:7])
        moved = {old for old, _ in res.moved}
        self.names = [n for n in self.names if n not in moved]
        for old in moved:
            self.loader.invalidate(self.path(old))

        def undo():
            failed = st.undo_move(self.store, res)
            self.names = st.list_media(self.folder)
            if failed:
                self.undo.clear()  # older entries may point at names that are now taken
                raise OSError(f"{', '.join(failed)} nicht zurückgeholt (Name belegt); Verlauf geleert")
        n = len(res.moved)
        self.push_undo(f"Verschieben von {n} Fotos nach {name}/", undo)
        self.segment = "all"
        self.rebuild_view()
        self.set_mode(G)
        if res.error:
            self.flash(f"Abgebrochen nach {n} von {len(d.names)}: {res.error}", 8000)
        else:
            self.flash(f"{n} Fotos nach {name}/ verschoben – Rückgängig mit Strg+Z", 8000)

    def trash(self):
        name = self.current()
        if not name:
            return
        self.single.stop()
        f = QFile(self.path(name))
        if not f.moveToTrash():
            self.flash(f"{name} konnte nicht in den Papierkorb: {f.errorString()}")
            return
        trashed = Path(f.fileName())
        entry = self.store.files.pop(name, None)
        self.store.save()
        self.names.remove(name)
        self.loader.invalidate(self.path(name))

        def undo():
            if os.path.exists(self.path(name)):
                raise FileExistsError(f"{name} existiert wieder")
            os.rename(trashed, self.path(name))
            (trashed.parent.parent / "info" / f"{trashed.name}.trashinfo").unlink(missing_ok=True)
            if entry:
                self.store.files[name] = entry
                self.store.save()
            self.names = st.list_media(self.folder)
        self.push_undo(f"{name} in den Papierkorb", undo)
        self.rebuild_view()
        self.show_current()
        self.flash(f"{name} in den Papierkorb verschoben – Rückgängig mit Strg+Z", 6000)

    # ---------- undo ----------
    def push_undo(self, label, fn):
        self.undo.append((label, fn))
        del self.undo[:-UNDO_LIMIT]
        self.update_status()

    def undo_last(self):
        if not self.undo:
            return
        label, fn = self.undo.pop()
        try:
            fn()
        except OSError as e:
            self.flash(f"Rückgängig fehlgeschlagen: {e}", 0)
            self.rebuild_view()
            return
        self.rebuild_view()
        self.show_current()
        self.flash(f"Rückgängig: {label}")

    def _restore_bytes(self, p, data):
        stamp = os.stat(p)
        sig = (stamp.st_size, stamp.st_mtime_ns)

        def undo():
            now = os.stat(p)
            if (now.st_size, now.st_mtime_ns) != sig:
                raise OSError(f"{Path(p).name} wurde seitdem geändert, nicht wiederhergestellt")
            st.atomic_write(p, data)
            self.loader.invalidate(p)
        return undo

    # ---------- rotate / edit ----------
    def transform(self, op):
        name = self.current()
        if not name:
            return
        p = self.path(name)
        ext = Path(name).suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png") and not self.lossy_ok:
            if QMessageBox.question(
                    self, "Drehen", f"Drehen von {ext} speichert neu: leichter Qualitätsverlust, "
                    "Aufnahmedatum und GPS gehen verloren. Für diese Sitzung erlauben?") \
                    != QMessageBox.StandardButton.Yes:
                return
            self.lossy_ok = True
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            original = imaging.transform_file(p, op)
        except OSError as e:
            self.flash(f"{name} lässt sich nicht drehen: {e}")
            return
        finally:
            QGuiApplication.restoreOverrideCursor()
        label = {"cw": "rechts gedreht", "ccw": "links gedreht", "fliph": "gespiegelt", "flipv": "gespiegelt"}[op]
        self.push_undo(f"{name} {label}", self._restore_bytes(p, original))
        self.loader.invalidate(p)
        self._changed(name)
        self.show_current()

    def enter_edit(self):
        name = self.current()
        if not name or self.mode == E:
            return
        p = self.path(name)
        if why := imaging.editable(p):
            self.flash(f"Bearbeiten nicht möglich: {why}")
            return
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            img = imaging.decode_scaled(p)
        except OSError as e:
            self.flash(f"Bearbeiten nicht möglich: {e}")
            return
        finally:
            QGuiApplication.restoreOverrideCursor()
        self.single.stop()
        self.help.hide()
        self.edit_return = self.mode
        self.edit_path = p
        self.editor = EditView(img)
        self.editor.save_requested.connect(self.save_edit)
        self.editor.close_requested.connect(self.close_edit)
        self.stack.addWidget(self.editor)
        self.stack.setCurrentWidget(self.editor)
        self.mode = E
        self.editor.canvas.setFocus()
        self.update_status()

    def save_edit(self):
        p = self.edit_path
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            original = imaging.save_edited(p, self.editor.image)
        except OSError as e:
            self.flash(f"Speichern fehlgeschlagen: {e}", 8000)
            return
        finally:
            QGuiApplication.restoreOverrideCursor()
        self.push_undo(f"Bearbeitung von {Path(p).name}", self._restore_bytes(p, original))
        self.loader.invalidate(p)
        self._leave_edit()
        self.flash(f"{Path(p).name} gespeichert (Original überschrieben) – Rückgängig mit Strg+Z", 6000)

    def close_edit(self):
        if self.editor.dirty:
            box = QMessageBox(QMessageBox.Icon.Question, "Änderungen verwerfen?",
                              "Die Änderungen sind noch nicht gespeichert.", parent=self)
            discard = box.addButton("Verwerfen", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("Weiter bearbeiten", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is not discard:
                return
        self._leave_edit()

    def _leave_edit(self):
        self.stack.removeWidget(self.editor)
        self.editor.deleteLater()
        self.editor = None
        self.edit_path = None
        self.set_mode(self.edit_return)
        if self.mode == G:
            self.model.refresh(self.index) if self.view else None

    # ---------- menus / misc ----------
    def _menu(self, ids, pos_widget, pos):
        m = QMenu(self)
        for aid in ids:
            m.addSeparator() if aid is None else m.addAction(self.act[aid])
        m.exec(pos_widget.mapToGlobal(pos))

    def _grid_menu(self, pos):
        idx = self.grid.indexAt(pos)
        if not idx.isValid():
            self._menu(["move", None, "open", "rescan"], self.grid.viewport(), pos)
            return
        self.grid.setCurrentIndex(idx)
        self._menu(["single", None, "keep", "reject", "fav", None, "rot_l", "rot_r", "flip_h", "flip_v", "edit",
                    None, "reveal", "trash"], self.grid.viewport(), pos)

    def _single_menu(self, pos):
        self._menu(["grid", None, "keep", "reject", "fav", None, "rot_l", "rot_r", "flip_h", "flip_v", "edit",
                    None, "reveal", "fullscreen", "trash"], self.single, pos)

    def reveal(self):
        name = self.current()
        if not name:
            return
        uri = QUrl.fromLocalFile(self.path(name)).toString()
        reply = QDBusInterface("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                               "org.freedesktop.FileManager1").call("ShowItems", [uri], "")
        if reply.type() != QDBusMessage.MessageType.ReplyMessage:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.folder)))

    def choose_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Ordner öffnen", str(self.folder))
        if d:
            self.open_folder(Path(d))

    def toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def flash(self, msg, ms=4000):
        self.statusBar().showMessage(msg, ms)

    # ---------- state -> widgets ----------
    def update_status(self):
        if self.store is None:
            return
        cur = self.current()
        m = self.mode
        browse = m in (S, G)
        kind = kind_of(cur) if cur else None
        in_round = self.sel_round == self.store.round
        names = self.round_set()
        k, x, o = st.round_counts(self.store, names)
        tags = self.store.tags(cur) if cur else set()
        n = len(self.view)
        playing = self.single.kind() if m == S else None
        enabled = {
            "grid": browse, "single": browse and n > 0, "toggle": browse and n > 0,
            "prev": m == S and self.index > 0, "next": m == S and self.index < n - 1,
            "first": m == S, "last": m == S,
            "keep": browse and cur is not None and in_round, "reject": browse and cur is not None and in_round,
            "fav": browse and cur is not None,
            "rot_l": browse and kind == "image", "rot_r": browse and kind == "image",
            "flip_h": browse and kind == "image", "flip_v": browse and kind == "image",
            "edit": browse and kind == "image", "trash": browse and cur is not None,
            "undo": browse and bool(self.undo), "move": browse and n > 0,
            "next_round": browse and in_round and bool(names) and bool(k or o),
            "play": playing in ("video", "anim"), "seek_b": playing == "video", "seek_f": playing == "video",
            "open": browse, "rescan": browse, "reveal": browse and cur is not None, "help": browse,
        }
        for aid, a in self.act.items():
            a.setEnabled(enabled.get(aid, browse and in_round if aid.startswith("seg_") else True))
        for aid in ("play", "seek_b", "seek_f"):
            self.act[aid].setVisible(enabled[aid])
        self.act["grid"].setChecked(m == G)
        self.act["single"].setChecked(m == S)
        for aid in ("keep", "reject", "fav"):
            self.act[aid].setChecked(aid in tags)
        self.act["fullscreen"].setChecked(self.isFullScreen())
        self.act["move"].setText(f"Verschieben… ({n})")
        self.act["move"].setToolTip(f"Die {n} angezeigten Fotos in einen Unterordner verschieben (M)")
        self.act["undo"].setToolTip(f"Rückgängig: {self.undo[-1][0]} (Strg+Z)" if self.undo else "Rückgängig (Strg+Z)")
        if not names or (not k and not o):
            self.act["next_round"].setToolTip("Nichts behalten – mindestens ein Foto behalten")
        else:
            self.act["next_round"].setToolTip("Mit den behaltenen Fotos weitermachen (N)")
        f = self.next_round_btn.font()
        f.setBold(in_round and not o and k > 0)
        self.next_round_btn.setFont(f)

        counts = {"all": len(names), "open": o, "keep": k, "reject": x,
                  "fav": sum("fav" in self.store.tags(nm) for nm in names)}
        for sid, label, *_ in SEGMENTS:
            a = self.act[f"seg_{sid}"]
            a.setIconText(f"{label} {counts[sid]}")
            a.setChecked(sid == self.segment)
        combo_key = (self.folder, self.store.round, len(self.names))
        if self.combo_round != combo_key:
            self.combo_round = combo_key
            self.round_combo.blockSignals(True)
            self.round_combo.clear()
            for r in range(self.store.round + 1):
                cnt = sum(self.store.survived(nm) >= r for nm in self.names)
                self.round_combo.addItem(f"Runde {r + 1} ({cnt})", r)
            self.round_combo.blockSignals(False)
        self.round_combo.blockSignals(True)
        self.round_combo.setCurrentIndex(self.sel_round)
        self.round_combo.blockSignals(False)
        if in_round:
            self.progress_label.setText(f"Runde {self.sel_round + 1} · {k} behalten · {x} verworfen · {o} offen")
        else:
            self.progress_label.setText(f"Runde {self.sel_round + 1} (abgeschlossen) · {len(names)} Fotos")
        self.progress.setVisible(in_round)
        self.progress.setMaximum(max(1, len(names)))
        self.progress.setValue(k + x)

        self.sichten.setVisible(m != E)
        self.bottom.setVisible(m != E)
        self.top.setVisible(m != E)
        self.single.canvas.set_tags(tags)
        if cur:
            text = f"{self.index + 1} / {n} · {cur}"
            self.position.setText(self.position.fontMetrics().elidedText(
                text, Qt.TextElideMode.ElideMiddle, self.position.width() or 260))
        else:
            self.position.setText("")
        if not self.names:
            self.grid.show_empty("Keine Fotos oder Videos in diesem Ordner.\n\nAnderen Ordner öffnen: Strg+O")
        elif not self.view:
            label = next(s[1] for s in SEGMENTS if s[0] == self.segment)
            self.grid.show_empty(f"Keine Fotos in „{label}“.\n\nAlle anzeigen: Strg+0")
        else:
            self.grid.show_empty("")
        self.setWindowTitle(f"{self.folder.name} – Runde {self.store.round + 1} – snapsort")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.help.isVisible():
            self.help.setGeometry(self.stack.rect())

    def closeEvent(self, e):
        self.single.stop()
        if self.editor and self.editor.dirty and QMessageBox.question(
                self, "Beenden", "Ungespeicherte Änderungen verwerfen?") != QMessageBox.StandardButton.Yes:
            e.ignore()
            return
        self.loader.decode_pool.clear()
        self.loader.thumb_pool.clear()
        self.loader.decode_pool.waitForDone(2000)
        self.loader.thumb_pool.waitForDone(2000)
        super().closeEvent(e)


class MoveDialog(QDialog):
    def __init__(self, win: MainWindow, recent: list[str]):
        super().__init__(win)
        self.win = win
        self.names = list(win.view)
        folder = win.folder
        self.setWindowTitle("In Unterordner verschieben")
        seg = next(s[1] for s in SEGMENTS if s[0] == win.segment)
        lay = QVBoxLayout(self)
        summary = QLabel(f"<b>{len(self.names)} Fotos</b> aus „{folder.name}“ (Ansicht: {seg}, "
                         f"Runde {win.sel_round + 1}) verschieben nach:")
        summary.setWordWrap(True)
        lay.addWidget(summary)
        row = QHBoxLayout()
        row.addWidget(QLabel(f"{folder.name}/"))
        self.combo = QComboBox(editable=True)
        with os.scandir(folder) as it:  # one listing, no stat per entry on network mounts
            subdirs = sorted((e.name for e in it if e.is_dir() and not e.name.startswith(".")), key=st.natural_key)
        self.combo.addItems(list(dict.fromkeys(recent + subdirs)))
        default = {"fav": "Favoriten", "keep": "Auswahl", "reject": "Aussortiert"}.get(win.segment) or (recent[0] if recent else "Auswahl")
        self.combo.setEditText(default)
        self.combo.setMinimumWidth(260)
        row.addWidget(self.combo, 1)
        lay.addLayout(row)
        self.hint = QLabel()
        lay.addWidget(self.hint)
        preview = QListWidget(viewMode=QListView.ViewMode.IconMode, flow=QListView.Flow.LeftToRight,
                              wrapping=False, selectionMode=QListWidget.SelectionMode.NoSelection)
        preview.setIconSize(QSize(64, 64))
        preview.setFixedHeight(96)
        grey = QPixmap(64, 64)
        grey.fill(Qt.GlobalColor.darkGray)
        for nm in self.names[:50]:
            thumb = win.loader.thumbs.get(win.path(nm))
            pm = QPixmap.fromImage(thumb) if thumb is not None else grey
            preview.addItem(QListWidgetItem(QIcon(pm), ""))
        if len(self.names) > 50:
            preview.addItem(f"… und {len(self.names) - 50} weitere")
        lay.addWidget(preview)
        self.buttons = QDialogButtonBox()
        self.buttons.addButton("Abbrechen", QDialogButtonBox.ButtonRole.RejectRole)
        self.ok = self.buttons.addButton(f"{len(self.names)} Fotos verschieben", QDialogButtonBox.ButtonRole.AcceptRole)
        self.ok.setIcon(win.act["move"].icon())
        self.ok.setDefault(True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self.combo.editTextChanged.connect(self._validate)
        self._validate()
        self.resize(560, self.sizeHint().height())

    def target(self) -> str:
        return self.combo.currentText().strip()

    def _validate(self):
        name = self.target()
        dst = self.win.folder / name if name else None
        if not name or "/" in name or name.startswith("."):
            msg, ok = "⚠ Ungültiger Name", False
        elif dst.exists() and not dst.is_dir():
            msg, ok = "⚠ Existiert als Datei", False
        elif dst.exists():
            msg, ok = "Ordner existiert – Fotos werden hinzugefügt", True
        else:
            msg, ok = "Neuer Ordner wird angelegt", True
        self.hint.setText(msg)
        self.ok.setEnabled(ok)


def resolve_target(arg: str | None) -> Path:
    """Local path for a CLI argument; remote URLs (webdavs://, smb://, sftp://) are mounted via kio-fuse."""
    if not arg:
        return Path.cwd()
    if "://" not in arg:
        return Path(arg).expanduser()
    url = QUrl(arg)
    if url.isLocalFile():
        return Path(url.toLocalFile())
    reply = QDBusInterface("org.kde.KIOFuse", "/org/kde/KIOFuse", "org.kde.KIOFuse.VFS").call("mountUrl", arg)
    if reply.type() != QDBusMessage.MessageType.ReplyMessage or not reply.arguments():
        raise OSError(f"kio-fuse konnte {arg} nicht einbinden: {reply.errorMessage() or 'keine Antwort'}")
    return Path(reply.arguments()[0])


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("snapsort")
    app.setDesktopFileName("snapsort")
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        target = resolve_target(arg)
        if not target.exists():
            raise OSError(f"{target} existiert nicht")
    except OSError as e:
        QMessageBox.critical(None, "snapsort", str(e))
        d = QFileDialog.getExistingDirectory(None, "Ordner öffnen", str(Path.home()))
        if not d:
            sys.exit(1)
        target = Path(d)
    w = MainWindow(target)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
