import json
from pathlib import Path

import pytest
from PySide6.QtCore import QFile, QPointF, QSettings, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QToolButton

from snapsort import app as app_mod
from snapsort import imaging

NAMES = ["a1.png", "a2.png", "a3.png", "a4.png", "b.jpg"]


def noise(w, h, seed):
    img = QImage(w, h, QImage.Format.Format_RGB32)
    for y in range(h):
        for x in range(w):
            img.setPixelColor(x, y, QColor((x * 37 + y * 11 + seed) % 256, (x * 7 + y * 53) % 256, (x * y) % 256))
    return img


@pytest.fixture
def win(qapp, tmp_path, monkeypatch):
    QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, str(tmp_path / "config"))
    monkeypatch.setattr(imaging, "CACHE", tmp_path / "thumbs")
    folder = tmp_path / "pics"
    folder.mkdir()
    for i, n in enumerate(NAMES):
        (folder / n).write_bytes(imaging.encode(noise(48, 32, i), Path(n).suffix[1:]))
    w = app_mod.MainWindow(folder)
    w.show()
    QTest.qWaitForWindowExposed(w)
    w.activateWindow()
    QTest.qWaitForWindowActive(w)
    yield w
    w.loader.decode_pool.waitForDone()
    w.loader.thumb_pool.waitForDone()
    w.close()
    w.deleteLater()
    QTest.qWait(1)


def key(w, k, mod=Qt.KeyboardModifier.NoModifier):
    QTest.keyClick(QApplication.focusWidget() or w, k, mod)


def test_starts_in_grid(win):
    assert win.mode == "grid" and win.index == 0 and win.view == NAMES


def test_letter_key_on_grid_tags_current(win):
    QTest.keyClick(win.grid, Qt.Key.Key_F)
    assert win.store.tags("a1.png") == {"fav"}


def test_return_opens_single(win):
    key(win, Qt.Key.Key_Return)
    assert win.mode == "single"


def test_keep_then_reject_is_exclusive(win):
    win.set_mode("single")
    key(win, Qt.Key.Key_K)
    assert win.store.tags("a1.png") == {"keep"} and win.index == 1
    win.go_to(0)
    key(win, Qt.Key.Key_X)
    assert win.store.tags("a1.png") == {"reject"}


def test_slot_tag_and_filter_cycle(win):
    key(win, Qt.Key.Key_1)
    assert win.store.tags("a1.png") == {"tag1"}
    ctrl = Qt.KeyboardModifier.ControlModifier
    key(win, Qt.Key.Key_1, ctrl)
    assert win.view == ["a1.png"]
    key(win, Qt.Key.Key_1, ctrl)
    assert win.view == NAMES[1:]
    key(win, Qt.Key.Key_1, ctrl)
    assert win.view == NAMES


class FirstButtonBox:
    ButtonRole = QMessageBox.ButtonRole
    StandardButton = QMessageBox.StandardButton

    def __init__(self, *_):
        self.buttons = []

    def setWindowTitle(self, _): pass
    def setText(self, _): pass
    def exec(self): pass

    def addButton(self, *_):
        self.buttons.append(object())
        return self.buttons[-1]

    def clickedButton(self):
        return self.buttons[0]


def test_next_round_and_undo(win, monkeypatch):
    monkeypatch.setattr(app_mod, "QMessageBox", FirstButtonBox)
    win.store.set_verdict("a1.png", "keep")
    win.store.set_verdict("a3.png", "reject")
    key(win, Qt.Key.Key_N)  # first button: undecided -> keep
    assert {n: win.store.survived(n) for n in NAMES} == {
        "a1.png": 1, "a2.png": 1, "a3.png": 0, "a4.png": 1, "b.jpg": 1}
    assert win.filter.min_round == 1 and "a3.png" not in win.view
    key(win, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert win.store.round == 0 and win.filter.min_round == 0 and win.view == NAMES
    assert win.store.tags("a1.png") == {"keep"} and win.store.tags("a3.png") == {"reject"}


def test_move_view_and_undo(win, monkeypatch):
    monkeypatch.setattr(app_mod.QInputDialog, "getText", staticmethod(lambda *a, **k: ("best", True)))
    folder = win.folder
    for n in ("a1.png", "a2.png"):
        win.store.toggle(n, "fav")
    win.cycle_filter("fav")
    key(win, Qt.Key.Key_M)
    best = folder / "best"
    assert sorted(p.name for p in best.iterdir()) == [".snapsort.json", "a1.png", "a2.png"]
    assert json.loads((best / ".snapsort.json").read_text())["files"]["a1.png"]["tags"] == ["fav"]
    assert win.names == NAMES[2:] and not win.store.tags("a1.png")
    key(win, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert not best.exists() and win.names == NAMES and win.store.tags("a1.png") == {"fav"}


def test_rotate_and_undo(win):
    p = win.folder / "a1.png"
    before = p.read_bytes()
    key(win, Qt.Key.Key_R)
    assert len(win.undo) == 1 and p.read_bytes() != before
    key(win, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert p.read_bytes() == before


def test_trash_and_undo(win, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()  # Qt does not create XDG_DATA_HOME itself
    p = win.folder / "a1.png"
    before = p.read_bytes()
    win.store.toggle("a1.png", "fav")
    probe = win.folder / "probe.txt"
    probe.write_text("x")
    f = QFile(str(probe))
    assert f.moveToTrash()
    if not Path(f.fileName()).is_relative_to(tmp_path):
        Path(f.fileName()).unlink()
        pytest.skip(f"Qt trash outside tmp: {f.fileName()}")
    key(win, Qt.Key.Key_Delete)
    assert not p.exists() and "a1.png" not in win.names and not win.store.tags("a1.png")
    key(win, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert p.read_bytes() == before and win.store.tags("a1.png") == {"fav"} and win.view == NAMES
    assert not (tmp_path / "data/Trash/info/a1.png.trashinfo").exists()


def test_edit_save_and_undo(win):
    p = win.folder / "a1.png"
    before = p.read_bytes()
    key(win, Qt.Key.Key_Return)
    key(win, Qt.Key.Key_E)
    assert win.mode == "edit"
    key(win, Qt.Key.Key_F)
    assert not win.store.tags("a1.png")
    win.editor.tool = "pixelate"
    win.editor.commit(QPointF(0, 0), QPointF(48, 32), None)
    QTest.keyClick(win.editor.canvas, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    assert win.mode == "single" and win.editor is None and p.read_bytes() != before
    key(win, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert p.read_bytes() == before


def test_edit_save_targets_edited_file_after_filter_change(win):
    win.store.toggle("a2.png", "fav")
    others = {n: (win.folder / n).read_bytes() for n in NAMES if n != "a1.png"}
    key(win, Qt.Key.Key_Return)
    key(win, Qt.Key.Key_E)
    assert not win.filterbar.isEnabled()
    win.cycle_filter("fav")  # a1 drops out of the view while editing
    win.editor.tool = "pixelate"
    win.editor.commit(QPointF(0, 0), QPointF(48, 32), None)
    QTest.keyClick(win.editor.canvas, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    assert {n: (win.folder / n).read_bytes() for n in others} == others


def test_undo_refuses_to_overwrite_changed_file(win):
    p = win.folder / "a1.png"
    key(win, Qt.Key.Key_R)
    p.write_bytes(b"changed by someone else")
    key(win, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert p.read_bytes() == b"changed by someone else"


def test_filterbar_does_not_leak_widgets(win):
    QApplication.processEvents()
    for _ in range(30):
        win.toggle_tag("tag1")
        win.toggle_tag("x" if _ % 2 else "y")
        QApplication.processEvents()
        QApplication.sendPostedEvents(None, 52)  # DeferredDelete
    assert len(win.filterbar.findChildren(QToolButton)) < 40
