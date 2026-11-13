"""클릭 투과 투명 HUD 오버레이."""
from __future__ import annotations

import ctypes
import time

from PyQt6.QtCore import QRect, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import QWidget

from .rules import Plan, fmt_price

WDA_EXCLUDEFROMCAPTURE = 0x11

C_TARGET = QColor("#FF4D4D")      # 빨강(진입 금지·오류)
C_STOP = QColor("#3B82F6")        # 손절 - 파랑 실선
C_RESIST = QColor("#FFA94D")      # 저항 - 주황 점선
C_SUPPORT = QColor("#38BDF8")     # 지지 - 하늘 점선
C_ZONE = QColor("#22C55E")        # 매수존 - 초록 반투명
C_PANEL = QColor(13, 17, 23, 225)
C_TEXT = QColor("#E6EDF3")
C_DIM = QColor("#9BA7B4")
C_WARN = QColor("#FBBF24")
ACTION_COLORS = {"BUY_ON_PULLBACK": C_ZONE, "WAIT": C_WARN, "AVOID": C_TARGET}

FONT_FAMILY = "Malgun Gothic"


class Overlay(QWidget):
    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.plan: Plan | None = None
        self.mode = "idle"          # idle | busy | result | error
        self.message = ""
        self.stale = False
        self.hints = ""
        self.capture_excluded = False
        self.busy_since = 0.0
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput   # 마우스 클릭/휠을 뒤 창으로 통과
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    # ------------------------------------------------------------------ #
    def place(self, roi: QRect) -> None:
        self.setGeometry(roi)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 오버레이가 스크린샷에 찍히지 않게 한다(자기 선을 AI가 다시 읽는 것을 방지).
        try:
            ok = ctypes.windll.user32.SetWindowDisplayAffinity(
                ctypes.c_void_p(int(self.winId())), WDA_EXCLUDEFROMCAPTURE
            )
            self.capture_excluded = bool(ok)
        except Exception:
            self.capture_excluded = False

    def set_state(self, mode: str, message: str = "", plan: Plan | None = None) -> None:
        self.mode = mode
        self.message = message
        if plan is not None:
            self.plan = plan
            self.stale = False
        self.update()

    def set_stale(self, stale: bool) -> None:
        if stale != self.stale:
            self.stale = stale
            self.update()

    def clear_plan(self) -> None:
        self.plan = None
        self.stale = False
        self.update()

    # ------------------------------------------------------------------ #
    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        w, h = self.width(), self.height()

        p.setPen(QPen(QColor(255, 255, 255, 70), 1, Qt.PenStyle.DotLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(0, 0, w - 1, h - 1)

        plan = self.plan
        panel = self._layout_panel(w, h)
        if plan is not None and plan.axis is not None:
            p.save()
            if self.stale or self.mode == "busy" or self._too_old(plan):
                p.setOpacity(0.3)
            # 라벨이 요약 패널에 가려지지 않도록 패널 영역을 미리 '사용 중'으로 둔다
            self._draw_marks(p, plan, w, h, [panel[0]] if panel else [])
            p.restore()
        if panel:
            self._draw_panel(p, *panel)
        p.end()

    def _busy_secs(self) -> int:
        return int(time.time() - self.busy_since) if self.busy_since else 0

    def _too_old(self, plan: Plan) -> bool:
        limit = float(self.cfg.get("stale_after_sec") or 0)
        return limit > 0 and time.time() - plan.ts > limit

    # -- 선/박스 ----------------------------------------------------------- #
    def _draw_marks(self, p: QPainter, plan: Plan, w: int, h: int, used: list[QRectF]) -> None:
        axis = plan.axis

        def y_of(price: float) -> float | None:
            r = axis.y_ratio(price)
            if r is None or r < -0.01 or r > 1.01:
                return None
            return r * h

        label_font = QFont(FONT_FAMILY, 9)
        label_font.setBold(True)
        fm = QFontMetrics(label_font)

        pending: list[tuple[str, float, QColor]] = []

        def label(text: str, y: float, color: QColor) -> None:
            pending.append((text, y, color))   # 선을 다 그린 뒤 맨 위에 그린다

        def draw_label(text: str, y: float, color: QColor) -> None:
            tw, th = fm.horizontalAdvance(text) + 12, fm.height() + 4
            top = y - th - 2 if y - th - 2 >= 0 else y + 3
            rect = QRectF(10, top, tw, th)
            for _ in range(8):
                hit = next((u for u in used if rect.intersects(u)), None)
                if hit is None or hit.right() + 8 + tw > w:
                    break
                rect.moveLeft(hit.right() + 8)
            used.append(rect)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(13, 17, 23, 215))
            p.drawRoundedRect(rect, 4, 4)
            p.setFont(label_font)
            p.setPen(color)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

        def hline(y: float, color: QColor, style: Qt.PenStyle, width: float) -> None:
            p.setPen(QPen(color, width, style))
            p.drawLine(0, int(round(y)), w, int(round(y)))

        # 매수존
        if plan.zone:
            yt, yb = axis.y_ratio(plan.zone[0]), axis.y_ratio(plan.zone[1])
            if yt is not None and yb is not None and yb > 0 and yt < 1:
                top, bottom = max(0.0, yt) * h, min(1.0, yb) * h
                rect = QRectF(0, top, w, max(3.0, bottom - top))
                fill = QColor(C_ZONE)
                if plan.conditional:
                    # 조건부 관심 구간: 옅은 채움 + 점선 테두리(지금 사라는 신호 아님)
                    fill.setAlpha(28)
                    p.setPen(QPen(C_ZONE, 1.4, Qt.PenStyle.DashLine))
                    name = "진입 관심"
                else:
                    fill.setAlpha(55)
                    p.setPen(QPen(C_ZONE, 1))
                    name = "진입 추천"
                p.setBrush(fill)
                p.drawRect(rect)
                tag = f"{plan.view.label} " if plan.view and len(plan.views) > 1 else ""
                label(f"{tag}{name} {fmt_price(plan.zone[1])}~{fmt_price(plan.zone[0])}", top, C_ZONE)

        # 지지/저항
        for lv in plan.levels:
            y = y_of(lv["price"])
            if y is None:
                continue
            is_res = lv["type"] == "RESISTANCE"
            color = C_RESIST if is_res else C_SUPPORT
            hline(y, color, Qt.PenStyle.DashLine, 1.4)
            text = f"{'저항' if is_res else '지지'} {fmt_price(lv['price'])}"
            if lv["note"]:
                text += f" · {lv['note']}"
            label(text, y, color)

        # 손절
        if plan.stop > 0:
            y = y_of(plan.stop)
            if y is not None:
                hline(y, C_STOP, Qt.PenStyle.DashLine if plan.conditional else Qt.PenStyle.SolidLine, 2)
                label(f"손절 추천 {fmt_price(plan.stop)}", y, C_STOP)

        for args in pending:
            draw_label(*args)

    # -- 요약 패널 ---------------------------------------------------------- #
    def _layout_panel(self, w: int, h: int):
        title_font = QFont(FONT_FAMILY, 11)
        title_font.setBold(True)
        body_font = QFont(FONT_FAMILY, 9)
        small_font = QFont(FONT_FAMILY, 8)

        blocks: list[tuple[str, QFont, QColor]] = []
        plan = self.plan
        if self.mode == "error":
            blocks.append(("⚠ 오류", title_font, C_TARGET))
            blocks.append((self.message, body_font, C_TEXT))
        elif plan is None:
            if self.mode == "busy":
                blocks.append((f"분석 중… {self._busy_secs()}초", title_font, C_TEXT))
                if self.message:
                    blocks.append((self.message, body_font, C_DIM))
            else:
                blocks.append(("TradeVision HUD", title_font, C_TEXT))
                blocks.append((self.message or "분석 대기 중", body_font, C_DIM))
        else:
            view = plan.view
            views_n = len(plan.views)
            tag = f"[{view.label}] " if view else ""
            title = "● " + tag + plan.action_label + ("  · 진입 관심 구간 있음" if plan.conditional else "")
            blocks.append((title, title_font, ACTION_COLORS.get(plan.action, C_TEXT)))
            head = " · ".join(x for x in (plan.symbol, plan.timeframe and plan.timeframe + "봉", plan.status_label) if x)
            blocks.append((head, body_font, C_DIM))
            if views_n > 1:
                pref = next((v for v in plan.views if v.horizon == plan.preferred), None)
                if pref is not None:
                    text = f"추천 관점: {pref.label}" + (f" — {plan.preferred_reason}" if plan.preferred_reason else "")
                    blocks.append((text, body_font, QColor("#C4B5FD")))
            if plan.summary:
                blocks.append((plan.summary, body_font, C_TEXT))
            for reason in plan.reasons:
                blocks.append(("· " + reason, body_font, C_TEXT))
            if view is not None:
                sub = f"{view.label} 관점 (보유 {view.hold})" if view.hold else f"{view.label} 관점"
                if view.summary:
                    sub += f": {view.summary}"
                blocks.append((sub, body_font, C_TEXT))
                for fact in view.facts:
                    blocks.append((fact, body_font, QColor("#A7F3D0")))
                for warn in view.warnings:
                    blocks.append(("⚠ " + warn, body_font, C_WARN))
            # 선을 그리지 않는 다른 관점은 한 줄로만
            switch = str((self.cfg.get("hotkeys") or {}).get("switch_view") or "").upper()
            for i, other in enumerate(plan.views):
                if i == plan.selected:
                    continue
                text = f"다른 관점 [{other.label}] {other.action_label}"
                if other.zone:
                    text += f" · {'관심' if other.conditional else '진입'} {fmt_price(other.zone[1])}~{fmt_price(other.zone[0])}"
                    text += f" / 손절 {fmt_price(other.stop)}"
                if switch:
                    text += f"   ({switch} 로 전환)"
                blocks.append((text, body_font, C_DIM))
            for warn in plan.warnings:
                blocks.append(("⚠ " + warn, body_font, C_WARN))


            age = int(time.time() - plan.ts)
            stamp = time.strftime("%H:%M:%S", time.localtime(plan.ts)) + " 분석"
            if plan.model and plan.model != "demo":
                stamp += f" · {plan.model} {plan.elapsed:.1f}초"
            if plan.axis_source == "OCR":
                stamp += " · 줌/스크롤 자동 맞춤"
            if age >= 60:
                stamp += f" ({age // 60}분 전)"
            if self.mode == "busy":
                blocks.append((f"재분석 중… {self._busy_secs()}초  {self.message}", body_font, C_WARN))
            elif self.stale:
                msg = ("⚠ 가격축을 읽을 수 없음(영역이 축을 자르는지 확인) → 재분석 필요" if plan.axis_source == "OCR"
                       else "⚠ 차트 변경 감지(가격축 자동 맞춤 꺼짐) → 영역 확인 후 재분석")
                blocks.append((msg, body_font, C_WARN))
            elif self._too_old(plan):
                blocks.append(("⚠ 오래된 분석 → 재분석 필요", body_font, C_WARN))
            blocks.append((stamp, small_font, C_DIM))
        if self.hints:
            blocks.append((self.hints, small_font, C_DIM))

        pad, gap = 10, 4
        pw = min(380, w - 16)
        if pw < 120:
            return None
        tw = pw - pad * 2
        flags = int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignLeft) | int(Qt.AlignmentFlag.AlignTop)
        heights = [
            QFontMetrics(font).boundingRect(QRect(0, 0, tw, 10000), flags, text).height()
            for text, font, _ in blocks
        ]
        ph = pad * 2 + sum(heights) + gap * (len(blocks) - 1)
        corner = str(self.cfg.get("hud_corner") or "top-left")
        x = w - pw - 8 if "right" in corner else 8
        y = h - ph - 8 if "bottom" in corner else 8
        y = max(0, y)
        return QRectF(x, y, pw, ph), blocks, heights

    def _draw_panel(self, p: QPainter, rect: QRectF, blocks, heights) -> None:
        pad, gap = 10, 4
        flags = int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignLeft) | int(Qt.AlignmentFlag.AlignTop)
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.setBrush(C_PANEL)
        p.drawRoundedRect(rect, 8, 8)
        x, cy, tw = int(rect.x()) + pad, int(rect.y()) + pad, int(rect.width()) - pad * 2
        for (text, font, color), bh in zip(blocks, heights):
            p.setFont(font)
            p.setPen(color)
            p.drawText(QRect(x, cy, tw, bh + 2), flags, text)
            cy += bh + gap
