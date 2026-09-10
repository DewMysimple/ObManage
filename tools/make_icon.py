"""Render the app's simple folder-and-mirror mark as a Windows icon."""
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen


def make_icon(path: Path) -> None:
    canvas = QImage(256, 256, QImage.Format.Format_ARGB32)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#2E526D"))
    painter.drawRoundedRect(QRectF(4, 4, 248, 248), 56, 56)
    painter.setBrush(QColor("#96B6C7"))
    painter.drawRoundedRect(QRectF(56, 54, 102, 124), 14, 14)
    painter.setBrush(QColor("#F4F8FA"))
    painter.drawRoundedRect(QRectF(82, 80, 102, 124), 14, 14)
    painter.setPen(QPen(QColor("#2E526D"), 12, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.drawLine(QPointF(108, 142), QPointF(160, 142))
    painter.drawLine(QPointF(144, 124), QPointF(162, 142))
    painter.drawLine(QPointF(144, 160), QPointF(162, 142))
    painter.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not canvas.save(str(path), "ICO"):
        raise RuntimeError("Could not save Windows icon")


if __name__ == "__main__":
    make_icon(Path(__file__).resolve().parents[1] / "assets" / "obmanage.ico")
