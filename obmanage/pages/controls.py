"""Visible affordances for controls under the application's light Qt style."""
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QComboBox, QSpinBox


class DropDownCombo(QComboBox):
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#7B8D96" if self.isEnabled() else "#B6C1C6"), 1.4))
        x, y = self.width() - 14, self.height() // 2
        painter.drawLine(x - 3, y - 2, x, y + 1)
        painter.drawLine(x, y + 1, x + 3, y - 2)
        painter.end()


class LevelSpinBox(QSpinBox):
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#7B8D96" if self.isEnabled() else "#B6C1C6"), 1.4))
        x = self.width() - 10
        for y, direction in ((9, -1), (self.height() - 9, 1)):
            painter.drawLine(x - 3, y - direction, x, y + direction)
            painter.drawLine(x, y + direction, x + 3, y - direction)
        painter.end()
