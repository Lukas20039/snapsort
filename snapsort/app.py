"""Main window: keymap, modes, tagging, filters, rounds, move, undo."""
import os
import sys
from pathlib import Path

from PySide6.QtCore import QFile, QSettings, Qt, QTimer, QUrl
from PySide6.QtDBus import QDBusInterface, QDBusMessage
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox, QSpinBox, QStackedWidget,
                               QToolBar, QToolButton, QWidget)

from . import imaging
from . import store as st
from .editor import EditView
from .store import Filter, TagStore
from .views import GridDelegate, GridModel, GridView, HelpOverlay, SingleView, tag_color

S, G = "single", "grid"
BROWSE = (S, G)
UNDO_LIMIT = 20


class MainWindow(QMainWindow):
    def __init__(self, target=None):
        super().__init__()
        self.settings = QSettings("snapsort", "snapsort")
        self.slots = list(self.settings.value("slots", [f"tag{i}" for i in range(1, 10)]))[:9]
        self.slots += [f"tag{i}" for i in range(len(self.slots) + 1, 10)]
        self.loader = imaging.Loader(self)
        self.folder: Path | None = None
        self.names: list[str] = []
        self.view: list[str] = []
        self.store: TagStore | None = None
        self.filter = Filter()
        self.index = 0
        self.mode = G
        self.undo: list[tuple[str, callable]] = []
        self.editor: EditView | None = None
        self.edit_path: str | None = None
        self.lossy_ok = False
        self.fb_tags: tuple | None = None

        self.single = SingleView()
        self.model = GridModel(self)
        self.grid = GridView(self.model, GridDelegate(self))
        self.grid.selectionModel().currentChanged.connect(self._grid_current)
        self.grid.doubleClicked.connect(lambda i: self.set_mode(S))
        self.stack = QStackedWidget()
        self.stack.addWidget(self.grid)
        self.stack.addWidget(self.single)
        self.setCentralWidget(self.stack)
        self.loader.imageReady.connect(self._image_ready)
        self.loader.thumbReady.connect(self._thumb_ready)
        self.loader.failed.connect(self._image_ready)
        self.resize_timer = QTimer(self, singleShot=True, interval=150, timeout=self.show_current)
        self.single.canvas.resized.connect(self.resize_timer.start)

        self.filterbar = QToolBar("Filter")
        self.filterbar.setMovable(False)
        box = QWidget()
        self.fb_layout = QHBoxLayout(box)
        self.fb_layout.setContentsMargins(4, 0, 4, 0)
        self.filterbar.addWidget(box)
        # rebuilt on the next loop turn: chips must not be deleted inside their own click signal
        self.fb_timer = QTimer(self, singleShot=True, interval=0, timeout=self._build_filterbar)
        self.addToolBar(self.filterbar)
        self._build_actions()
        self.help = HelpOverlay(self.stack, [(k, d) for k, d, *_ in self.keymap])
        self.resize(1400, 900)
        self.open_target(Path(target or os.getcwd()))

    # ---------- setup ----------
    def _build_actions(self):
        km = [
            (["Right"], "next", lambda: self.go(1), (S,)),
            (["Left"], "previous", lambda: self.go(-1), (S,)),
            (["Home"], "first", lambda: self.go_to(0), (S,)),
            (["End"], "last", lambda: self.go_to(len(self.view) - 1), (S,)),
            (["G"], "grid ↔ single view", lambda: self.set_mode(S if self.mode == G else G), BROWSE),
            (["Return"], "open in single view", lambda: self.set_mode(S), (G,)),
            (["Esc"], "back to grid / close help", self.escape, BROWSE),
            (["F"], "toggle favourite ★", lambda: self.toggle_tag("fav"), BROWSE),
            (["K"], "keep (and next)", lambda: self.verdict("keep"), BROWSE),
            (["X"], "reject (and next)", lambda: self.verdict("reject"), BROWSE),
            (["N"], "next round: continue with the kept ones", self.next_round, BROWSE),
            (["1 … 9"], "toggle tag slot", None, BROWSE),
            (["Ctrl+F"], "filter ★: off → only → without", lambda: self.cycle_filter("fav"), BROWSE),
            (["Ctrl+1 … 9"], "filter tag slot: off → only → without", None, BROWSE),
            (["Ctrl+U"], "filter: untagged only", self.toggle_untagged, BROWSE),
            (["Ctrl+A"], "filter: any ↔ all tags", self.toggle_all, BROWSE),
            (["Ctrl+0"], "clear filter", self.clear_filter, BROWSE),
            (["/"], "show/hide filter bar", lambda: self.filterbar.setVisible(not self.filterbar.isVisible()), BROWSE),
            (["M"], "move shown files to subfolder", self.move_view, BROWSE),
            (["Del"], "move current to trash", self.trash, BROWSE),
            (["Ctrl+Z"], "undo (move, round, rotate, edit, trash)", self.undo_last, BROWSE),
            (["R"], "rotate 90° clockwise", lambda: self.transform("cw"), BROWSE),
            (["L"], "rotate 90° counter-clockwise", lambda: self.transform("ccw"), BROWSE),
            (["H"], "flip horizontal", lambda: self.transform("fliph"), BROWSE),
            (["V"], "flip vertical", lambda: self.transform("flipv"), BROWSE),
            (["E"], "edit: censor, pen, text, box, arrow", self.enter_edit, (S,)),
            (["Space"], "video / GIF play/pause", self.single.toggle_play, (S,)),
            ([","], "video −5 s", lambda: self.single.seek(-5000), (S,)),
            (["."], "video +5 s", lambda: self.single.seek(5000), (S,)),
            (["Ctrl+T"], "rename tag slots", self.edit_slots, BROWSE),
            (["Ctrl+O"], "open folder", self.choose_folder, BROWSE),
            (["F5"], "rescan folder", self.rescan, BROWSE),
            (["F11"], "fullscreen", self.toggle_fullscreen, BROWSE),
            (["?", "F1"], "this help", lambda: self.help.toggle(), BROWSE),
            (["Ctrl+Q"], "quit", self.close, BROWSE),
        ]
        for i in range(9):
            km.append(([str(i + 1)], None, lambda i=i: self.toggle_tag(self.slots[i]), BROWSE))
            km.append(([f"Ctrl+{i + 1}"], None, lambda i=i: self.cycle_filter(self.slots[i]), BROWSE))
        self.keymap = [(" / ".join(k), d, f, m) for k, d, f, m in km if d]
        self.actions_by_mode: list[tuple[QAction, tuple]] = []
        for keys, desc, fn, modes in km:
            if fn is None:
                continue
            a = QAction(desc or keys[0], self)
            a.setShortcuts([QKeySequence(k) for k in keys])
            a.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            a.triggered.connect(fn)
            self.addAction(a)
            self.actions_by_mode.append((a, modes))

    def _sync_actions(self):
        for a, modes in self.actions_by_mode:
            a.setEnabled(self.mode in modes)
        self.filterbar.setEnabled(self.mode in BROWSE)

    # ---------- folder / view ----------
    def open_target(self, target: Path):
        select = None
        if target.is_file():
            target, select = target.parent, target.name
        self.open_folder(target, select)

    def open_folder(self, folder: Path, select=None):
        self.single.stop()
        self.folder = folder.resolve()
        self.store = TagStore(self.folder)
        self.names = st.list_media(self.folder)
        self.filter = Filter()
        self.undo.clear()
        self.setWindowTitle(f"snapsort — {self.folder}")
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

    def matches(self, name) -> bool:
        return self.filter.matches(self.store.tags(name), self.store.survived(name))

    def rebuild_view(self, keep=None):
        keep = keep or self.current()
        self.view = [n for n in self.names if self.matches(n)]
        if keep in self.view:
            self.index = self.view.index(keep)
        else:
            later = [i for i, n in enumerate(self.view) if keep and st.natural_key(n) > st.natural_key(keep)]
            self.index = later[0] if later else max(0, len(self.view) - 1) if keep else 0
        self.model.reset()
        self._select_grid()
        self.refresh_filterbar(force=True)
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
        self._sync_actions()
        self.update_status()

    def escape(self):
        if self.help.isVisible():
            self.help.hide()
        elif self.mode == S:
            self.set_mode(G)

    def go(self, d):
        self.go_to(self.index + d)

    def go_to(self, i):
        if not self.view:
            return
        self.index = max(0, min(len(self.view) - 1, i))
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
                self.single.canvas.show_image(None, f"Cannot open {name}\n{self.loader.errors[p]}")
            else:
                self.single.canvas.show_image(self.loader.thumbs.get(p))
        n = len(self.view)
        order = [self.index] + [self.index + d for d in (1, -1, 2, 3, -2, 4)]
        self.loader.prefetch([self.path(self.view[i]) for i in order if 0 <= i < n], size)

    def _image_ready(self, path, img_or_msg):
        name = self.current()
        if self.mode != S or name is None or path != self.path(name) or self.single.currentWidget() is not self.single.canvas:
            return
        if isinstance(img_or_msg, str):
            if not img_or_msg:
                return  # thumbnail failure
            self.single.canvas.show_image(None, f"Cannot open {name}\n{img_or_msg}")
        else:
            self.single.canvas.show_image(img_or_msg)

    def _thumb_ready(self, path, img):
        name = self.current()
        if self.mode == S and name and path == self.path(name) and self.single.canvas.pix is None \
                and path not in self.loader.cache:
            self.single.canvas.show_image(img)

    # ---------- tagging ----------
    def _changed(self, name):
        if name in self.view:
            self.model.refresh(self.view.index(name))
        if self.store.warning:
            self.flash(self.store.warning, 0)
        self.refresh_filterbar()
        self.update_status()

    def toggle_tag(self, tag):
        name = self.current()
        if name:
            self.store.toggle(name, tag)
            self._changed(name)

    def verdict(self, tag):
        name = self.current()
        if not name:
            return
        self.store.set_verdict(name, tag)
        self._changed(name)
        if self.mode == S:
            self.go(1)
        else:
            i = min(self.index + 1, len(self.view) - 1)
            self.index = i
            self._select_grid()

    # ---------- filter ----------
    def cycle_filter(self, tag):
        f = self.filter
        if tag in f.include:
            f.include.discard(tag)
            f.exclude.add(tag)
        elif tag in f.exclude:
            f.exclude.discard(tag)
        else:
            f.include.add(tag)
        self.rebuild_view()
        self.show_current()

    def toggle_untagged(self):
        self.filter.untagged = not self.filter.untagged
        self.rebuild_view()
        self.show_current()

    def toggle_all(self):
        self.filter.mode_all = not self.filter.mode_all
        self.rebuild_view()
        self.show_current()

    def set_min_round(self, v):
        if v != self.filter.min_round:
            self.filter.min_round = v
            self.rebuild_view()
            self.show_current()

    def clear_filter(self):
        self.filter = Filter(min_round=self.filter.min_round)
        self.rebuild_view()
        self.show_current()

    def refresh_filterbar(self, force=False):
        if force:
            self.fb_tags = None
        self.fb_timer.start()

    def _build_filterbar(self):
        f = self.filter
        key = (frozenset(self.store.all_tags()), tuple(self.slots), len(self.view), self.store.round)
        if key == self.fb_tags:
            return
        self.fb_tags = key
        bar = self.fb_layout
        while item := bar.takeAt(0):
            if item.widget():
                item.widget().deleteLater()
        tags = ["fav", "keep", "reject"] + self.slots
        tags += sorted(self.store.all_tags() | f.include | f.exclude)
        for t in dict.fromkeys(tags):
            b = QToolButton()
            state = "include" if t in f.include else "exclude" if t in f.exclude else ""
            b.setText(("★" if t == "fav" else t) + {"include": " ✓", "exclude": " ✗"}.get(state, ""))
            col = {"include": "#2a7", "exclude": "#c33"}.get(state, "transparent")
            b.setStyleSheet(f"QToolButton{{background:{col}; border-left:4px solid {tag_color(t, self.slots).name()};"
                            f"padding:2px 6px; {'text-decoration:line-through;' if state == 'exclude' else ''}}}")
            b.setToolTip("click: off → only → without")
            b.clicked.connect(lambda _=False, t=t: self.cycle_filter(t))
            bar.addWidget(b)
        bar.addSpacing(12)
        anyall = QToolButton(text="match ALL" if f.mode_all else "match ANY")
        anyall.clicked.connect(self.toggle_all)
        bar.addWidget(anyall)
        unt = QCheckBox("untagged")
        unt.setChecked(f.untagged)
        unt.toggled.connect(lambda _: self.toggle_untagged())
        bar.addWidget(unt)
        rnd = QSpinBox(minimum=0, maximum=max(self.store.round, f.min_round), value=f.min_round)
        rnd.setPrefix("round ≥ ")
        rnd.valueChanged.connect(self.set_min_round)
        bar.addWidget(rnd)
        clear = QToolButton(text="clear")
        clear.clicked.connect(self.clear_filter)
        bar.addWidget(clear)
        bar.addWidget(QLabel(f"  {len(self.view)} of {len(self.names)} shown"))

    # ---------- rounds ----------
    def next_round(self):
        if not self.view:
            return
        keep, reject, undecided = st.round_counts(self.store, self.view)
        box = QMessageBox(self)
        box.setWindowTitle(f"Finish round {self.store.round}")
        box.setText(f"{keep} kept, {reject} rejected, {undecided} undecided.\n"
                    f"Round {self.store.round + 1} continues with the kept files.")
        if undecided:
            k = box.addButton("Undecided → keep", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Undecided → reject", QMessageBox.ButtonRole.AcceptRole)
        else:
            k = box.addButton("Next round", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() in (cancel, None):
            return
        old_filter = self.filter
        snap = st.next_round(self.store, self.view, undecided_keep=box.clickedButton() is k)
        self.filter = Filter(min_round=self.store.round)

        def undo():
            st.restore_snapshot(self.store, snap)
            self.filter = old_filter
        self.push_undo(f"round {self.store.round}", undo)
        self.rebuild_view()
        self.index = 0
        self._select_grid()
        self.show_current()
        self.flash(f"Round {self.store.round}: {len(self.view)} files")

    # ---------- move / trash ----------
    def move_view(self):
        if not self.view:
            return
        default = next(iter(sorted(self.filter.include)), None) or (
            f"round{self.store.round}" if self.filter.min_round else "selected")
        name, ok = QInputDialog.getText(self, "Move", f"Move {len(self.view)} files to {self.folder}/<name>/",
                                        text=default)
        name = name.strip()
        if not ok:
            return
        if not name or "/" in name or name.startswith("."):
            self.flash("Invalid folder name")
            return
        dst = self.folder / name
        if dst.exists() and not dst.is_dir():
            self.flash(f"{name} exists and is not a folder")
            return
        res = st.execute_move(self.store, st.plan_move(self.view, dst), dst)
        moved = {old for old, _ in res.moved}
        self.names = [n for n in self.names if n not in moved]
        for old in moved:
            self.loader.invalidate(self.path(old))

        def undo():
            failed = st.undo_move(self.store, res)
            self.names = st.list_media(self.folder)
            if failed:
                self.undo.clear()  # older entries may point at names that are now taken
                raise OSError(f"could not restore {', '.join(failed)} (name taken); undo history cleared")
        self.push_undo(f"move {len(res.moved)} files", undo)
        self.filter = Filter()
        self.rebuild_view()
        self.set_mode(G)
        self.flash(f"Moved {len(res.moved)} files to {name}/" + (f" — stopped: {res.error}" if res.error else ""))

    def trash(self):
        name = self.current()
        if not name:
            return
        self.single.stop()
        f = QFile(self.path(name))
        if not f.moveToTrash():
            self.flash(f"Could not trash {name}: {f.errorString()}")
            return
        trashed = Path(f.fileName())
        entry = self.store.files.pop(name, None)
        self.store.save()
        self.names.remove(name)
        self.loader.invalidate(self.path(name))

        def undo():
            if os.path.exists(self.path(name)):
                raise FileExistsError(f"{name} exists again")
            os.rename(trashed, self.path(name))
            (trashed.parent.parent / "info" / f"{trashed.name}.trashinfo").unlink(missing_ok=True)
            if entry:
                self.store.files[name] = entry
                self.store.save()
            self.names = st.list_media(self.folder)
        self.push_undo(f"trash {name}", undo)
        self.rebuild_view()
        self.show_current()
        self.flash(f"Trashed {name}")

    # ---------- undo ----------
    def push_undo(self, label, fn):
        self.undo.append((label, fn))
        del self.undo[:-UNDO_LIMIT]

    def undo_last(self):
        if not self.undo:
            self.flash("Nothing to undo")
            return
        label, fn = self.undo.pop()
        try:
            fn()
        except OSError as e:
            self.flash(f"Undo failed: {e}", 0)
            self.rebuild_view()
            return
        self.rebuild_view()
        self.show_current()
        self.flash(f"Undid: {label}")

    def _restore_bytes(self, p, data):
        stamp = os.stat(p)
        sig = (stamp.st_size, stamp.st_mtime_ns)

        def undo():
            now = os.stat(p)
            if (now.st_size, now.st_mtime_ns) != sig:
                raise OSError(f"{Path(p).name} changed since, not restored")
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
                    self, "Rotate", f"Rotating {ext} re-encodes the file: slight quality loss and its "
                    "metadata (capture date, GPS) is removed. Continue for this session?") \
                    != QMessageBox.StandardButton.Yes:
                return
            self.lossy_ok = True
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            original = imaging.transform_file(p, op)
        except OSError as e:
            self.flash(f"Cannot {op} {name}: {e}")
            return
        finally:
            QGuiApplication.restoreOverrideCursor()
        self.push_undo(f"{op} {name}", self._restore_bytes(p, original))
        self.loader.invalidate(p)
        self._changed(name)
        self.show_current()

    def enter_edit(self):
        name = self.current()
        if not name:
            return
        p = self.path(name)
        if why := imaging.editable(p):
            self.flash(f"Cannot edit: {why}")
            return
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            img = imaging.decode_scaled(p)
        except OSError as e:
            self.flash(f"Cannot edit: {e}")
            return
        finally:
            QGuiApplication.restoreOverrideCursor()
        self.edit_path = p
        self.editor = EditView(img)
        self.editor.save_requested.connect(self.save_edit)
        self.editor.close_requested.connect(self.close_edit)
        self.stack.addWidget(self.editor)
        self.stack.setCurrentWidget(self.editor)
        self.mode = "edit"
        self._sync_actions()
        self.editor.canvas.setFocus()
        self.update_status()

    def save_edit(self):
        p = self.edit_path
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            original = imaging.save_edited(p, self.editor.image)
        except OSError as e:
            self.flash(f"Save failed: {e}", 8000)
            return
        finally:
            QGuiApplication.restoreOverrideCursor()
        self.push_undo(f"edit {Path(p).name}", self._restore_bytes(p, original))
        self.loader.invalidate(p)
        self._leave_edit()
        self.flash(f"Saved {Path(p).name}")

    def close_edit(self):
        if self.editor.dirty and QMessageBox.question(self, "Discard edits?", "Discard unsaved edits?") \
                != QMessageBox.StandardButton.Yes:
            return
        self._leave_edit()

    def _leave_edit(self):
        self.stack.removeWidget(self.editor)
        self.editor.deleteLater()
        self.editor = None
        self.edit_path = None
        self.set_mode(S)
        self.update_status()

    # ---------- misc ----------
    def edit_slots(self):
        d = QDialog(self)
        d.setWindowTitle("Tag slots")
        form = QFormLayout(d)
        edits = [QLineEdit(s) for s in self.slots]
        for i, e in enumerate(edits):
            form.addRow(f"{i + 1}:", e)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        form.addRow(bb)
        if d.exec():
            new = [e.text().strip() or old for e, old in zip(edits, self.slots)]
            if len(set(new)) != 9 or set(new) & {"fav", "keep", "reject"}:
                self.flash("Slot names must be unique and not fav/keep/reject")
                return
            self.slots = new
            self.settings.setValue("slots", new)
            self.refresh_filterbar()
            self.grid.viewport().update()

    def choose_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Open folder", str(self.folder))
        if d:
            self.open_folder(Path(d))

    def toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def flash(self, msg, ms=4000):
        self.statusBar().showMessage(msg, ms)

    def update_status(self):
        name = self.current()
        if self.mode == "edit":
            text = f"EDIT {name} · P pen · B box · A arrow · T text · C censor · U blur · [ ] width · " \
                   "Ctrl+Z undo · Ctrl+S save (overwrites) · Esc cancel"
        elif name is None:
            text = f"0/{len(self.names)} — nothing matches the filter (Ctrl+0 clears)" if self.names else \
                "No images or videos in this folder (Ctrl+O opens another)"
        else:
            tags = self.store.tags(name)
            user = sorted(tags - {"fav", *st.VERDICTS})
            parts = [f"{self.index + 1}/{len(self.view)}", name]
            if "fav" in tags or user:
                parts.append(" ".join(["★"] * ("fav" in tags) + [", ".join(user)]).strip())
            parts += [v.upper() for v in st.VERDICTS if v in tags]
            if s := self.store.survived(name):
                parts.append(f"survived {s}")
            parts.append(f"round {self.store.round}")
            parts.append(f"filter: {self.filter.describe()}")
            if not self.matches(name):
                parts.append("(filtered out)")
            parts.append("? help")
            text = " · ".join(parts)
        if not hasattr(self, "status_label"):
            self.status_label = QLabel()
            self.statusBar().addPermanentWidget(self.status_label, 1)
        self.status_label.setText(text)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.help.isVisible():
            self.help.setGeometry(self.stack.rect())

    def closeEvent(self, e):
        self.single.stop()
        if self.editor and self.editor.dirty and QMessageBox.question(
                self, "Quit", "Discard unsaved edits?") != QMessageBox.StandardButton.Yes:
            e.ignore()
            return
        self.loader.decode_pool.clear()
        self.loader.thumb_pool.clear()
        self.loader.decode_pool.waitForDone(2000)
        self.loader.thumb_pool.waitForDone(2000)
        super().closeEvent(e)


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
        raise OSError(f"kio-fuse could not mount {arg}: {reply.errorMessage() or 'no reply'}")
    return Path(reply.arguments()[0])


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("snapsort")
    app.setDesktopFileName("snapsort")
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        target = resolve_target(arg)
        if not target.exists():
            raise OSError(f"{target} does not exist")
    except OSError as e:
        QMessageBox.critical(None, "snapsort", str(e))
        d = QFileDialog.getExistingDirectory(None, "Open folder", str(Path.home()))
        if not d:
            sys.exit(1)
        target = Path(d)
    w = MainWindow(target)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
