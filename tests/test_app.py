import json
from pathlib import Path

import pytest
from PySide6.QtCore import QFile, QPointF, QSettings, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

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


def test_segments_filter_by_decision(win):
    key(win, Qt.Key.Key_K)  # a1 kept, selection moves on
    ctrl = Qt.KeyboardModifier.ControlModifier
    key(win, Qt.Key.Key_2, ctrl)
    assert win.view == ["a1.png"] and win.act["seg_keep"].isChecked()
    key(win, Qt.Key.Key_1, ctrl)
    assert win.view == NAMES[1:]
    key(win, Qt.Key.Key_0, ctrl)
    assert win.view == NAMES
    assert win.act["seg_keep"].iconText() == "Behalten 1" and win.act["seg_open"].iconText() == "Offen 4"


def test_every_command_has_a_button_or_menu_entry(win):
    keyboard_only = {"toggle", "first", "last"} | {f"seg_{s}" for s in ("all", "open", "keep", "reject", "fav")}
    missing = [aid for aid, a in win.act.items()
               if aid not in keyboard_only and not any(o is not win for o in a.associatedObjects())]
    assert missing == []


def test_toolbar_button_rotates(win):
    p = win.folder / "a1.png"
    before = p.read_bytes()
    QTest.mouseClick(win.top.widgetForAction(win.act["rot_r"]), Qt.MouseButton.LeftButton)
    assert p.read_bytes() != before


class FirstButtonBox:
    ButtonRole = QMessageBox.ButtonRole
    StandardButton = QMessageBox.StandardButton

    def __init__(self, *_):
        self.buttons = []

    def setWindowTitle(self, _): pass
    def setText(self, _): pass
    def setDefaultButton(self, _): pass
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
    key(win, Qt.Key.Key_N)  # first button: take the open ones along
    assert {n: win.store.survived(n) for n in NAMES} == {
        "a1.png": 1, "a2.png": 1, "a3.png": 0, "a4.png": 1, "b.jpg": 1}
    assert win.filter.min_round == 1 and "a3.png" not in win.view
    key(win, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert win.store.round == 0 and win.filter.min_round == 0 and win.view == NAMES
    assert win.store.tags("a1.png") == {"keep"} and win.store.tags("a3.png") == {"reject"}


def test_next_round_uses_whole_round_not_segment(win, monkeypatch):
    monkeypatch.setattr(app_mod, "QMessageBox", FirstButtonBox)
    win.store.set_verdict("a1.png", "keep")
    win.store.set_verdict("a2.png", "reject")
    win.set_segment("keep")
    key(win, Qt.Key.Key_N)
    assert win.store.survived("a3.png") == 1 and win.store.survived("a2.png") == 0


def test_earlier_round_is_read_only(win, monkeypatch):
    monkeypatch.setattr(app_mod, "QMessageBox", FirstButtonBox)
    win.store.set_verdict("a1.png", "keep")
    key(win, Qt.Key.Key_N)
    win.round_combo.setCurrentIndex(0)
    assert win.sel_round == 0 and not win.act["keep"].isEnabled() and not win.act["next_round"].isEnabled()


def test_move_view_and_undo(win, monkeypatch):
    monkeypatch.setattr(app_mod.MoveDialog, "exec", lambda self: (self.combo.setEditText("best"), 1)[1])
    folder = win.folder
    for n in ("a1.png", "a2.png"):
        win.store.toggle(n, "fav")
    win.set_segment("fav")
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
    assert not win.sichten.isVisible() and not win.act["keep"].isEnabled()
    win.set_segment("fav")  # a1 drops out of the view while editing
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


def test_move_refilters_decisions_made_since(win, monkeypatch):
    monkeypatch.setattr(app_mod.MoveDialog, "exec", lambda self: (self.combo.setEditText("best"), 1)[1])
    win.store.set_verdict("a1.png", "keep")
    win.store.set_verdict("a2.png", "keep")
    win.set_segment("keep")
    key(win, Qt.Key.Key_X)  # a1 rejected while "Behalten" is shown; it stays visible until refiltered
    key(win, Qt.Key.Key_M)
    assert sorted(p.name for p in (win.folder / "best").iterdir() if p.suffix == ".png") == ["a2.png"]


def test_move_failure_is_reported(win, monkeypatch):
    monkeypatch.setattr(app_mod.MoveDialog, "exec", lambda self: (self.combo.setEditText("x" * 300), 1)[1])
    key(win, Qt.Key.Key_M)
    assert "fehlgeschlagen" in win.statusBar().currentMessage() and win.names == NAMES


def test_open_other_folder_starts_at_first(win, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    for n in ("c0.png", "c1.png", "c2.png", "c3.png"):
        (other / n).write_bytes(imaging.encode(noise(8, 8, 0), "png"))
    win.go_to(3)
    win.open_folder(other)
    assert win.index == 0 and win.round_combo.itemText(0) == "Runde 1 (4)"


def test_help_disabled_while_editing(win):
    key(win, Qt.Key.Key_Return)
    key(win, Qt.Key.Key_E)
    assert not win.act["help"].isEnabled()
