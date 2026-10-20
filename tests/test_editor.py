import pytest
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest

from snapsort.editor import EditView, clamp_rect, fit_rect, letterbox


@pytest.fixture
def ed(qapp):
    img = QImage(400, 200, QImage.Format.Format_RGB32)
    img.fill(QColor("red"))
    e = EditView(img)
    e.resize(900, 600)
    e.show()
    QTest.qWaitForWindowExposed(e)
    e.set_tool("format")
    yield e
    e.close()


def test_fit_rect_is_largest_centered():
    assert fit_rect(400, 200, 1.0) == QRectF(100, 0, 200, 200)
    assert fit_rect(200, 400, 16 / 9).width() == 200
    assert fit_rect(400, 200, 4 / 5) == QRectF(120, 0, 160, 200)


def test_clamp_rect_keeps_size_inside():
    assert clamp_rect(QRectF(350, -20, 100, 100), 400, 200) == QRectF(300, 0, 100, 100)


def test_letterbox_keeps_whole_image():
    img = QImage(400, 200, QImage.Format.Format_RGB32)
    img.fill(QColor("red"))
    out = letterbox(img, 1.0, "white")
    assert (out.width(), out.height()) == (400, 400)
    assert out.pixelColor(200, 10) == QColor("white") and out.pixelColor(200, 200) == QColor("red")
    tall = letterbox(img, 4 / 5, "blur")
    assert (tall.width(), tall.height()) == (400, 500)


def test_crop_frame_moves_and_applies(ed):
    assert ed.crop == QRectF(100, 0, 200, 200)
    c = ed.canvas
    start = c.xf.map(QPointF(200, 100)).toPoint()
    QTest.mousePress(c, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(c, start + QPoint(400, 0))  # far right: clamped to the edge
    QTest.mouseRelease(c, Qt.MouseButton.LeftButton, pos=start + QPoint(400, 0))
    assert ed.crop == QRectF(200, 0, 200, 200)
    ed.apply_crop()
    assert (ed.image.width(), ed.image.height()) == (200, 200) and ed.copy_tag == "1x1"
    ed.undo_step()
    assert (ed.image.width(), ed.image.height()) == (400, 200)


def test_crop_corner_drag_keeps_ratio(ed):
    c = ed.canvas
    corner = c.xf.map(ed.crop.bottomRight()).toPoint()
    QTest.mousePress(c, Qt.MouseButton.LeftButton, pos=corner)
    QTest.mouseMove(c, corner - QPoint(60, 10))
    QTest.mouseRelease(c, Qt.MouseButton.LeftButton, pos=corner - QPoint(60, 10))
    assert ed.crop.topLeft() == QPointF(100, 0) and ed.crop.width() == pytest.approx(ed.crop.height())
    assert ed.crop.width() < 200


def test_fit_pads_to_ratio(ed):
    ed.ratio_box.setCurrentIndex(1)  # 4:5
    ed.apply_fit()
    assert (ed.image.width(), ed.image.height()) == (400, 500) and ed.dirty
