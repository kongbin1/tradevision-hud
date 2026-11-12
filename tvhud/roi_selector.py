"""마우스 드래그로 차트 영역(ROI)을 지정하는 선택 화면."""
from __future__ import annotations

from PyQt6.QtCore import QObject, QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PyQt6.QtWidgets import QWidget

MIN_W, MIN_H = 240, 160


class _ScreenVeil(QWidget):
    picked = pyqtSignal(QRect)
    cancelled = pyqtSignal()

    def __init__(self, screen):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setGeometry(screen.geometry())
        self._start: QPoint | None = None
        self._end: QPoint | None = None

    def _rect(self) -> QRect:
        if self._start is None or self._end is None:
            return QRect()
        return QRect(self._start, self._end).normalized()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 120))
        rect = self._rect()
        if not rect.isEmpty():
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            p.fillRect(rect, QColor(0, 0, 0, 1))   # 완전 투명(0)이면 클릭이 뒤 창으로 빠지므로 1
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            ok = rect.width() >= MIN_W and rect.height() >= MIN_H
            p.setPen(QPen(QColor("#22C55E" if ok else "#FF4D4D"), 2))
            p.drawRect(rect)
        font = QFont("Malgun Gothic", 14)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor("#FFFFFF"))
        p.drawText(
            self.rect().adjusted(0, 40, 0, 0),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            "차트 영역을 드래그하세요  (우측 가격축 포함 · 취소: Esc 또는 우클릭)",
        )
        p.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self.cancelled.emit()
        elif event.button() == Qt.MouseButton.LeftButton:
            self._start = self._end = event.position().toPoint()
            self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._start is not None:
            self._end = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._start is None:
            return
        rect = self._rect()
        self._start = self._end = None
        if rect.width() >= MIN_W and rect.height() >= MIN_H:
            self.picked.emit(QRect(self.mapToGlobal(rect.topLeft()), rect.size()))
        else:
            self.update()   # 너무 작으면 다시 드래그

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()


class RoiSelector(QObject):
    """모든 모니터에 반투명 막을 띄우고, 드래그한 사각형(전역 논리좌표)을 돌려준다."""

    selected = pyqtSignal(QRect)
    cancelled = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._veils: list[_ScreenVeil] = []

    @property
    def active(self) -> bool:
        return bool(self._veils)

    def start(self) -> None:
        if self._veils:
            return
        for screen in QGuiApplication.screens():
            veil = _ScreenVeil(screen)
            veil.picked.connect(self._on_picked)
            veil.cancelled.connect(self._on_cancelled)
            veil.show()
            self._veils.append(veil)
        if self._veils:
            self._veils[0].activateWindow()
            self._veils[0].setFocus()

    def _close(self) -> None:
        veils, self._veils = self._veils, []
        for veil in veils:
            veil.hide()
            veil.deleteLater()

    def _on_picked(self, rect: QRect) -> None:
        self._close()
        self.selected.emit(rect)

    def _on_cancelled(self) -> None:
        self._close()
        self.cancelled.emit()
