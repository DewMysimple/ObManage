"""Small, consistent line icons rendered by Qt (also available in frozen builds)."""
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap


def action_icon(name: str, color: str = "#476C82") -> QIcon:
    paths = {
        "folder": ((2, 6, 2, 16, 18, 16, 18, 6, 10, 6, 8, 3, 2, 3, 2, 6),),
        "search": ((8, 3, 4, 4, 2, 8, 4, 12, 8, 13, 12, 11, 13, 7, 11, 4, 8, 3),
                   (12, 12, 18, 18)),
        "refresh": ((17, 7, 14, 3, 7, 3, 3, 7), (17, 2, 17, 7, 12, 7),
                    (3, 13, 6, 17, 13, 17, 17, 13), (3, 18, 3, 13, 8, 13)),
        "arrow": ((3, 10, 17, 10), (12, 5, 17, 10, 12, 15)),
        "check": ((3, 10, 8, 15, 17, 5),),
        "clear": ((5, 5, 15, 15), (15, 5, 5, 15)),
        "undo": ((7, 4, 2, 9, 7, 14), (2, 9, 12, 9, 17, 12, 17, 17)),
    }
    icon = QIcon()
    for scale in (1, 2, 3):
        pixmap = QPixmap(20 * scale, 20 * scale)
        pixmap.setDevicePixelRatio(scale)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(color), 1.6, Qt.PenStyle.SolidLine,
                           Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        for coordinates in paths[name]:
            points = [QPointF(*coordinates[i:i + 2]) for i in range(0, len(coordinates), 2)]
            for first, second in zip(points, points[1:]):
                painter.drawLine(first, second)
        painter.end()
        icon.addPixmap(pixmap)
    return icon
