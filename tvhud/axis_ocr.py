"""Windows 내장 OCR로 차트 가격축 숫자를 읽어, 줌/스크롤 후에도 선 위치를 다시 맞춘다.

AI 호출 없이 PC 안에서 처리한다(보통 0.1초 내외). OCR을 쓸 수 없는 환경이면
available() 이 False 가 되고, 프로그램은 기존처럼 '차트 변경 감지 → 흐리게' 로 동작한다.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QImage

from .rules import AxisFit, fit_axis

_NUM = re.compile(r"^-?\d{1,3}(?:[,.]\d{3})*(?:\.\d+)?$|^-?\d+(?:\.\d+)?$")
SCALES = (2, 1, 3)     # 확대 배율 후보. 글자 크기(화면 배율)에 따라 잘 읽히는 배율이 다르다
ENOUGH_WORDS = 4       # 이만큼 읽히면 다른 배율은 시도하지 않는다
_engine = None
_engine_tried = False


@dataclass
class Word:
    value: float
    x0: float
    x1: float
    yc: float


def available() -> bool:
    return _get_engine() is not None


def _get_engine():
    global _engine, _engine_tried
    if _engine_tried:
        return _engine
    _engine_tried = True
    try:
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine

        eng = OcrEngine.try_create_from_user_profile_languages()
        if eng is None:
            eng = OcrEngine.try_create_from_language(Language("en-US"))
        _engine = eng
    except Exception:
        _engine = None
    return _engine


def _to_bitmap(img: QImage):
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter

    img = img.convertToFormat(QImage.Format.Format_ARGB32)  # 메모리상 BGRA
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    raw = bytes(ptr)
    stride = img.bytesPerLine()
    w, h = img.width(), img.height()
    if stride != w * 4:
        raw = b"".join(raw[r * stride: r * stride + w * 4] for r in range(h))
    writer = DataWriter()
    writer.write_bytes(raw)
    return SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8, w, h)


def _prepare(img: QImage, scale: int) -> QImage:
    """확대 + 흑백 + (어두운 배경이면) 반전 → 흰 바탕 검은 글씨."""
    img = img.scaled(
        img.width() * scale, img.height() * scale,
        Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation,
    ).convertToFormat(QImage.Format.Format_Grayscale8)
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    data = bytes(ptr)
    if data and sum(data[:: max(1, len(data) // 4000)]) / len(data[:: max(1, len(data) // 4000)]) < 110:
        img.invertPixels()
    return img


def read_words(img: QImage) -> list[Word]:
    """이미지에서 숫자로 읽히는 단어와 위치(원본 픽셀 좌표)를 돌려준다.

    여러 확대 배율로 시도해 가장 많이 읽힌 결과를 쓴다(보통 첫 시도에서 끝남).
    """
    if _get_engine() is None or img.isNull():
        return []
    best: list[Word] = []
    for scale in SCALES:
        words = _read_at(img, scale)
        if len(words) > len(best):
            best = words
        if len(best) >= ENOUGH_WORDS:
            break
    return best


def _read_at(img: QImage, scale: int) -> list[Word]:
    eng = _get_engine()
    prepared = _prepare(img, scale)

    async def run():
        return await eng.recognize_async(_to_bitmap(prepared))

    try:
        result = asyncio.run(run())
    except Exception:
        return []
    words: list[Word] = []
    for line in result.lines:
        # 줄 단위로 합쳐서 "13,500" 이 "13," "500" 으로 쪼개지는 경우도 처리
        parts = [(w.text, w.bounding_rect) for w in line.words]
        candidates = [parts] if len(parts) > 1 else []
        candidates += [[p] for p in parts]
        for group in candidates:
            text = "".join(t for t, _ in group).replace(" ", "")
            text = text.replace("O", "0").replace("o", "0")
            if not _NUM.match(text):
                continue
            try:
                value = float(text.replace(",", ""))
            except ValueError:
                continue
            x0 = min(r.x for _, r in group) / scale
            x1 = max(r.x + r.width for _, r in group) / scale
            yc = sum(r.y + r.height / 2 for _, r in group) / len(group) / scale
            words.append(Word(value, x0, x1, yc))
            if len(group) > 1:
                break   # 줄 전체가 숫자 하나면 개별 단어는 건너뜀
    return words


def _column(words: list[Word], width: float) -> list[Word]:
    """오른쪽 정렬된 숫자 열(가격축) 하나를 고른다."""
    best: list[Word] = []
    for anchor in words:
        col = [w for w in words if abs(w.x1 - anchor.x1) <= max(8.0, width * 0.012)]
        if len(col) > len(best) or (len(col) == len(best) and col and anchor.x1 > best[0].x1):
            best = col
    # 같은 높이에 두 개 이상이면 하나만
    seen: dict[int, Word] = {}
    for w in sorted(best, key=lambda w: w.yc):
        seen.setdefault(int(w.yc // 4), w)
    return list(seen.values())


@dataclass
class AxisTracker:
    """분석 직후 가격축 열의 위치를 찾아 두고, 이후 그 띠만 OCR 해서 축을 다시 맞춘다."""
    strip: QRect            # ROI 이미지 안의 가격축 띠
    ref_price: float        # 가격 크기 검증용(현재가 등)

    @staticmethod
    def locate(img: QImage, ref_price: float) -> "AxisTracker | None":
        if not available():
            return None
        w, h = img.width(), img.height()
        right = QRect(int(w * 0.6), 0, w - int(w * 0.6), h)
        words = [
            Word(x.value, x.x0 + right.x(), x.x1 + right.x(), x.yc)
            for x in read_words(img.copy(right))
        ]
        if ref_price > 0:
            words = [x for x in words if ref_price / 3 <= x.value <= ref_price * 3]
        col = _column(words, w)
        if len(col) < 3:
            return None
        x0 = max(0, int(min(c.x0 for c in col)) - 12)
        x1 = min(w, int(max(c.x1 for c in col)) + 12)
        return AxisTracker(QRect(x0, 0, x1 - x0, h), ref_price)

    def fit(self, img: QImage) -> AxisFit | None:
        """현재 화면의 가격축을 읽어 가격→y 대응식을 만든다. 확신이 없으면 None."""
        if img.isNull():
            return None
        strip = self.strip.intersected(QRect(0, 0, img.width(), img.height()))
        if strip.width() < 10:
            return None
        words = read_words(img.copy(strip))
        if self.ref_price > 0:
            words = [x for x in words if self.ref_price / 4 <= x.value <= self.ref_price * 4]
        col = _column([Word(x.value, x.x0, x.x1, x.yc) for x in words], strip.width())
        h = img.height()
        ticks = [{"price": c.value, "y": c.yc / h * 1000} for c in col]
        axis = fit_axis(ticks)
        if axis is None or axis.n < 3 or axis.rms > 8:
            return None
        return axis
