"""TradeVision HUD 메인 컨트롤러 (트레이, 단축키, 캡처, 분석 흐름)."""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import traceback
from dataclasses import asdict
from datetime import datetime, timedelta

from PyQt6.QtCore import QBuffer, QIODevice, QLockFile, QObject, QRect, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QColor, QFont, QGuiApplication, QIcon, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QInputDialog, QLineEdit, QMenu, QMessageBox, QSystemTrayIcon, QWidget

from . import axis_ocr, config
from .analyzer import Analyzer, AnalyzerError, demo_result
from .hotkeys import HotkeyManager
from .overlay import Overlay
from .roi_selector import RoiSelector
from .rules import Plan, build_plan

SIG_W, SIG_H = 96, 54
AUTO_CHOICES = [(60, "1분"), (180, "3분"), (300, "5분"), (900, "15분"), (3600, "60분")]


# --------------------------------------------------------------------------- #
# 이미지 유틸
# --------------------------------------------------------------------------- #
def signature(img: QImage) -> bytes:
    """화면 변화 감지를 위한 저해상도 그레이스케일 지문."""
    small = img.scaled(
        SIG_W, SIG_H, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
    ).convertToFormat(QImage.Format.Format_Grayscale8)
    ptr = small.constBits()
    ptr.setsize(small.sizeInBytes())
    data = bytes(ptr)
    bpl = small.bytesPerLine()
    return b"".join(data[r * bpl: r * bpl + SIG_W] for r in range(SIG_H))


def changed_fraction(a: bytes, b: bytes) -> float:
    """밝기가 뚜렷이 달라진 셀의 비율(0~1). 십자선·틱 깜빡임 정도는 0에 가깝다."""
    if len(a) != len(b) or not a:
        return 1.0
    return sum(1 for x, y in zip(a, b) if abs(x - y) > 24) / len(a)


def to_png(img: QImage, max_width: int) -> bytes:
    if max_width and img.width() > max_width:
        img = img.scaledToWidth(max_width, Qt.TransformationMode.SmoothTransformation)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def make_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#0D1117"))
    p.drawRoundedRect(2, 2, 60, 60, 12, 12)
    p.setBrush(QColor("#22C55E"))
    p.drawRect(14, 30, 8, 20)
    p.setBrush(QColor("#FF4D4D"))
    p.drawRect(28, 18, 8, 26)
    p.setBrush(QColor("#22C55E"))
    p.drawRect(42, 12, 8, 22)
    p.end()
    return QIcon(pm)


# --------------------------------------------------------------------------- #
class Worker(QThread):
    done = pyqtSignal(dict, str, float)
    failed = pyqtSignal(str, bool)   # (메시지, API 키 문제 여부)
    progress = pyqtSignal(str)

    def __init__(self, analyzer: Analyzer, png: bytes, demo: bool):
        super().__init__()
        self.analyzer, self.png, self.demo = analyzer, png, demo

    def run(self) -> None:
        try:
            if self.demo:
                self.done.emit(demo_result(), "demo", 0.8)
            else:
                self.done.emit(*self.analyzer.analyze(self.png, progress=self.progress.emit))
        except AnalyzerError as exc:
            self.failed.emit(str(exc), exc.auth)
        except Exception as exc:  # noqa: BLE001 - 어떤 오류든 HUD에 표시
            self.failed.emit(f"예상치 못한 오류: {exc}", False)


# --------------------------------------------------------------------------- #
class Controller(QObject):
    def __init__(self, app: QApplication, demo: bool = False):
        super().__init__()
        self.app = app
        self.demo = demo
        self.cfg = config.load()
        self.analyzer = Analyzer(self.cfg)
        self.overlay = Overlay(self.cfg)
        self.selector = RoiSelector()
        self.selector.selected.connect(self._on_roi_selected)
        self.selector.cancelled.connect(self._on_roi_cancelled)

        self.roi: QRect | None = self._load_roi()
        self.worker: Worker | None = None
        self.baseline: bytes | None = None      # 마지막으로 선 위치를 맞춘 화면의 지문
        self._pending: tuple[bytes, bytes, QImage] | None = None  # (지문, png, 원본 이미지)
        self.tracker: axis_ocr.AxisTracker | None = None  # 줌/스크롤 시 가격축 재인식
        self._resume_analyze = False          # 영역을 다시 지정하면 이어서 분석
        self._axis_skip_roi: QRect | None = None  # 경고를 무시하고 그대로 쓰기로 한 영역
        self.auto_on = False

        self.watch_timer = QTimer(self)
        self.watch_timer.setInterval(500)
        self.watch_timer.timeout.connect(self._watch)
        self.auto_timer = QTimer(self)
        self.auto_timer.setSingleShot(True)
        self.auto_timer.timeout.connect(self._auto_tick)

        self._helper = QWidget()   # 단축키 메시지를 받을 숨은 창
        self.hotkeys = HotkeyManager(app, int(self._helper.winId()))
        self._build_tray()
        self._register_hotkeys()
        self._refresh_hints()
        self._prune_logs()

    # -- 시작 ---------------------------------------------------------------- #
    def start(self) -> None:
        self.watch_timer.start()
        if self.roi is None:
            self.tray.showMessage("TradeVision HUD", "차트 영역을 마우스로 드래그해 지정하세요.")
            self.select_roi()
        else:
            self.overlay.place(self.roi)
            self.overlay.set_state("idle", self._idle_message())
            self.overlay.show()

    def _idle_message(self) -> str:
        key = self.cfg["hotkeys"]["analyze"].upper()
        return f"{key} 를 누르면 현재 차트를 분석합니다." + ("  [데모 모드]" if self.demo else "")

    def _load_roi(self) -> QRect | None:
        roi = self.cfg.get("roi")
        if not (isinstance(roi, list) and len(roi) == 4):
            return None
        try:
            rect = QRect(*(int(v) for v in roi))
        except (TypeError, ValueError):
            return None
        if rect.width() < 100 or rect.height() < 100:
            return None
        # 모니터 구성이 바뀌어 영역이 화면 밖이면 다시 지정받는다
        if not any(s.geometry().contains(rect.center()) for s in QGuiApplication.screens()):
            return None
        return rect

    # -- 트레이 --------------------------------------------------------------- #
    def _build_tray(self) -> None:
        hk = self.cfg["hotkeys"]
        self.tray = QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip("TradeVision HUD" + (" (데모)" if self.demo else ""))
        menu = QMenu()

        def add(text: str, slot, combo: str = "") -> QAction:
            act = QAction(text + (f"\t{combo.upper()}" if combo else ""), menu)
            act.triggered.connect(lambda _=False: slot())
            menu.addAction(act)
            return act

        add("지금 분석", self.analyze, hk["analyze"])
        add("오버레이 표시/숨김", self.toggle_overlay, hk["toggle_overlay"])
        self.act_auto = add("자동 갱신", self.toggle_auto, hk["toggle_auto"])
        self.act_auto.setCheckable(True)
        add("단타 ↔ 스윙 선 전환", self.switch_view, hk.get("switch_view", ""))

        sub = menu.addMenu("자동 갱신 주기")
        group = QActionGroup(sub)
        current = int(self.cfg.get("auto_interval_sec") or 60)
        for sec, name in AUTO_CHOICES:
            act = QAction(name, sub, checkable=True)
            act.setChecked(sec == current)
            act.triggered.connect(lambda _=False, s=sec: self._set_interval(s))
            group.addAction(act)
            sub.addAction(act)

        menu.addSeparator()
        add("차트 영역 다시 지정", self.select_roi, hk["select_roi"])
        add("API 키 설정", self.ask_key)
        add("설정 파일 열기", lambda: self._open(config.CONFIG_PATH))
        add("로그 폴더 열기", lambda: self._open(config.LOG_DIR))
        menu.addSeparator()
        add("종료", self.quit, hk["quit"])
        self._menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_overlay()

    def _open(self, path) -> None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        if path == config.CONFIG_PATH and not path.exists():
            config.save(self.cfg)
        if path == config.LOG_DIR:
            path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(path))  # noqa: S606 - 사용자 자신의 설정/로그 열기
        except OSError as exc:
            self.tray.showMessage("TradeVision HUD", f"열 수 없습니다: {exc}")

    # -- 단축키 --------------------------------------------------------------- #
    def _register_hotkeys(self) -> None:
        actions = {
            "analyze": self.analyze,
            "toggle_overlay": self.toggle_overlay,
            "select_roi": self.select_roi,
            "toggle_auto": self.toggle_auto,
            "switch_view": self.switch_view,
            "quit": self.quit,
        }
        failed = []
        for name, slot in actions.items():
            combo = str(self.cfg["hotkeys"].get(name) or "")
            # 단축키 콜백 안에서 바로 창을 띄우지 않도록 이벤트 루프로 넘긴다
            if combo and not self.hotkeys.register(combo, lambda s=slot: QTimer.singleShot(0, s)):
                failed.append(combo.upper())
        if failed:
            self.tray.showMessage(
                "TradeVision HUD",
                "단축키 등록 실패(다른 프로그램이 사용 중): " + ", ".join(failed)
                + "\n트레이 메뉴는 그대로 사용할 수 있습니다. 설정 파일에서 변경하세요.",
            )

    def _refresh_hints(self) -> None:
        hk = self.cfg["hotkeys"]

        def last(name: str) -> str:
            return str(hk.get(name) or "").split("+")[-1].upper()

        prefix = "+".join(str(hk.get("analyze") or "").split("+")[:-1]).upper()
        auto = "켜짐" if self.auto_on else "꺼짐"
        self.overlay.hints = (
            f"{prefix}+  {last('analyze')} 분석 · {last('toggle_overlay')} 숨김 · "
            f"{last('select_roi')} 영역 · {last('switch_view')} 관점 · {last('toggle_auto')} 자동({auto})"
        )
        self.overlay.update()

    # -- 영역 지정 ------------------------------------------------------------ #
    def select_roi(self, message: str = "") -> None:
        if self.selector.active or self._busy():
            return
        self.overlay.hide()
        self.selector.start(message)

    def _on_roi_selected(self, rect: QRect) -> None:
        self.roi = rect
        self.cfg["roi"] = [rect.x(), rect.y(), rect.width(), rect.height()]
        config.save(self.cfg)
        self.baseline = None
        self._axis_skip_roi = None
        self.overlay.clear_plan()
        self.overlay.place(rect)
        self.overlay.set_state("idle", "영역 지정 완료. " + self._idle_message())
        self.overlay.show()
        # 선택 화면(어두운 막)이 완전히 사라진 뒤 새 영역에 가격축이 보이는지 확인
        QTimer.singleShot(250, self._verify_roi_axis)

    def _verify_roi_axis(self) -> None:
        if self.roi is None or self.selector.active:
            return
        img = self._capture()
        if img is not None and not self._axis_visible(img):
            self.select_roi(self._AXIS_HELP + "\n(이 영역을 그대로 쓰려면 Esc)")
            return
        if self._resume_analyze:
            self._resume_analyze = False
            self.analyze("manual")

    def _on_roi_cancelled(self) -> None:
        if self.roi is not None:
            if self._resume_analyze:
                # 경고를 보고도 Esc → 이 영역 그대로 분석(가격축 확인 건너뜀)
                self._resume_analyze = False
                self._axis_skip_roi = QRect(self.roi)
            self.overlay.show()
        else:
            self.tray.showMessage("TradeVision HUD", "영역이 지정되지 않았습니다. 트레이 메뉴에서 다시 지정하세요.")

    _AXIS_HELP = "선택한 영역에서 오른쪽 가격축 숫자를 찾지 못했습니다. 가격축 숫자가 끝까지 다 보이게 조금 더 넓게 드래그하세요."

    def _axis_visible(self, img: QImage) -> bool:
        """가격축 숫자를 읽을 수 있는지(OCR을 못 쓰는 환경이면 확인 생략 → True)."""
        if self.demo or not axis_ocr.available():
            return True
        try:
            return axis_ocr.AxisTracker.locate(img) is not None
        except Exception:  # noqa: BLE001
            return True

    # -- API 키 --------------------------------------------------------------- #
    def ask_key(self) -> bool:
        dlg = QInputDialog()
        dlg.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        dlg.setWindowTitle("Gemini API 키 설정")
        dlg.setLabelText(
            "Google AI Studio(https://aistudio.google.com/apikey)에서 발급한 API 키를 입력하세요.\n"
            f"키는 이 PC의 {config.CONFIG_PATH} 에만 저장됩니다."
        )
        dlg.setTextEchoMode(QLineEdit.EchoMode.Password)
        dlg.setWindowIcon(make_icon())
        if dlg.exec() != QInputDialog.DialogCode.Accepted:
            return False
        key = dlg.textValue().strip()
        if not key:
            return False
        self.cfg["api_key"] = key
        config.save(self.cfg)
        return True

    # -- 캡처 ----------------------------------------------------------------- #
    def _capture(self) -> QImage | None:
        roi = self.roi
        if roi is None:
            return None
        screen = QGuiApplication.screenAt(roi.center()) or QGuiApplication.primaryScreen()
        geo = screen.geometry()
        # 캡처 제외 설정이 안 먹힌 환경에서는 잠깐 숨기고 찍는다
        must_hide = self.overlay.isVisible() and not self.overlay.capture_excluded
        if must_hide:
            self.overlay.hide()
            self.app.processEvents()
            QThread.msleep(80)
        pm = screen.grabWindow(0, roi.x() - geo.x(), roi.y() - geo.y(), roi.width(), roi.height())
        if must_hide:
            self.overlay.show()
        img = pm.toImage()
        return None if img.isNull() else img

    # -- 분석 ----------------------------------------------------------------- #
    def _busy(self) -> bool:
        return self.worker is not None

    def analyze(self, trigger: str = "manual") -> None:
        if self.selector.active or self._busy():
            return
        if self.roi is None:
            if trigger == "manual":
                self.select_roi()
            return
        if not self.demo and not config.api_key(self.cfg):
            if trigger != "manual" or not self.ask_key():
                return
        if trigger == "manual" and not self.overlay.isVisible():
            self.overlay.show()
            self.app.processEvents()

        img = self._capture()
        if img is None:
            self.overlay.set_state("error", "화면 캡처에 실패했습니다.")
            return
        # AI에 보내기 전에 가격축이 보이는지 확인 → 잘렸으면 API를 쓰지 않고 영역부터 다시 지정
        if self._axis_skip_roi != self.roi and not self._axis_visible(img):
            if trigger == "manual":
                self._resume_analyze = True
                key = str(self.cfg["hotkeys"].get("analyze") or "").upper()
                self.select_roi(self._AXIS_HELP + f"\n(지금 영역 그대로 분석하려면 Esc 후 {key})")
            else:
                self.overlay.set_state("error", self._AXIS_HELP)
            return
        sig = signature(img)
        plan = self.overlay.plan
        if trigger == "auto" and plan is not None and self.baseline is not None:
            if changed_fraction(sig, self.baseline) < 0.003:
                plan.ts = time.time()   # 화면이 그대로면 API를 다시 부르지 않는다
                self.overlay.update()
                return
        png = to_png(img, int(self.cfg.get("max_image_width") or 0))
        self._pending = (sig, png, img)
        self.overlay.busy_since = time.time()
        self.overlay.set_state("busy", "캡처 완료, AI에 전송 중")
        self.worker = Worker(self.analyzer, png, self.demo)
        self.worker.progress.connect(lambda text: self.overlay.set_state("busy", text))
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()

    def _on_done(self, raw: dict, model: str, elapsed: float) -> None:
        plan = build_plan(raw, self.cfg, model, elapsed)
        if not self.demo and model and "lite" not in model and self.cfg.get("last_good_model") != model:
            self.cfg["last_good_model"] = model   # 다음 실행 때 이 모델부터 시도
            config.save(self.cfg)
        sig, png, img = self._pending or (None, b"", QImage())
        self.baseline = sig
        self._attach_ocr_axis(plan, img)
        self.overlay.set_state("result", plan=plan)
        self._save_log(png, raw, plan)

    def _attach_ocr_axis(self, plan: Plan, img: QImage) -> None:
        """가격축을 PC에서 직접 읽어 선 위치를 보정하고, 이후 줌/스크롤 추적을 준비한다."""
        self.tracker = None
        if self.demo or img.isNull() or not self.cfg.get("track_zoom", True) or not axis_ocr.available():
            return
        try:
            tracker = axis_ocr.AxisTracker.locate(img)
            fit = tracker.fit(img) if tracker else None
        except Exception:  # noqa: BLE001 - OCR 실패는 기능 저하일 뿐
            tracker, fit = None, None
        if fit is None:
            # 가장 흔한 원인: 창 크기·배치가 바뀌어 영역 오른쪽이 가격축 숫자를 자름
            key = str(self.cfg["hotkeys"].get("select_roi") or "").upper()
            plan.warnings.insert(0, f"가격축 숫자를 읽지 못함(영역이 축을 자른 듯) → {key} 로 가격축 전체가 보이게 "
                                    "영역을 다시 지정하세요. 이번 결과의 가격은 틀릴 수 있고, 줌 추적도 꺼집니다.")
            return
        self.tracker = tracker
        plan.axis = fit          # AI가 읽은 축보다 직접 측정한 값이 정확하다
        plan.axis_source = "OCR"
        plan.warnings = [w for w in plan.warnings if not w.startswith("가격축")]
        # AI가 읽은 현재가가 실제 가격축 범위와 동떨어지면 결과 전체를 믿기 어렵다
        top, bottom = fit.price_at(0.0), fit.price_at(1.0)
        if plan.current > 0 and top and bottom:
            lo, hi = min(top, bottom), max(top, bottom)
            span = hi - lo
            if not (lo - span * 0.5 <= plan.current <= hi + span * 0.5):
                plan.warnings.insert(0, f"AI가 읽은 현재가 {plan.current:,.0f} 가 화면 가격축({lo:,.0f}~{hi:,.0f})과 "
                                        "맞지 않음 → 이번 분석은 신뢰하기 어렵습니다. 다시 분석하세요.")

    def _on_failed(self, message: str, auth: bool = False) -> None:
        self.overlay.set_state("error", message)
        if auth:
            # 잘못 저장된 키는 지우고 바로 다시 입력받는다(작업 스레드가 끝난 뒤 실행)
            self.cfg["api_key"] = ""
            config.save(self.cfg)
            QTimer.singleShot(200, self._reenter_key)

    def _reenter_key(self) -> None:
        if self._busy():
            QTimer.singleShot(200, self._reenter_key)
            return
        if self.ask_key():
            self.analyze("manual")
        else:
            self.overlay.set_state(
                "error", "API 키가 없습니다. Ctrl+Alt+A 를 누르면 다시 입력할 수 있습니다. (종료: Ctrl+Alt+Q)"
            )

    def _on_worker_finished(self) -> None:
        worker, self.worker = self.worker, None
        self._pending = None
        if worker is not None:
            worker.deleteLater()

    # -- 화면 변화 감시 / 자동 갱신 ------------------------------------------- #
    def _watch(self) -> None:
        ov = self.overlay
        if self._busy():
            ov.update()   # 경과 초 표시 갱신
            return
        if not ov.isVisible() or ov.plan is None or self.selector.active:
            return
        if self.baseline is not None and ov.capture_excluded:
            img = self._capture()
            if img is not None:
                sig = signature(img)
                frac = changed_fraction(sig, self.baseline)
                if frac > 0.002 and self.tracker is not None:
                    # 화면이 바뀜(줌/스크롤/새 캔들) → 가격축을 다시 읽어 선을 옮긴다
                    try:
                        fit = self.tracker.fit(img)
                    except Exception:  # noqa: BLE001
                        fit = None
                    if fit is not None:
                        ov.plan.axis = fit
                        self.baseline = sig
                        frac = 0.0
                ov.set_stale(frac > float(self.cfg.get("change_threshold") or 0.06))
        ov.update()   # 선 위치·경과 시간 갱신

    def toggle_auto(self) -> None:
        self.auto_on = not self.auto_on
        self.act_auto.setChecked(self.auto_on)
        self._schedule_auto()
        self._refresh_hints()
        if self.auto_on:
            self.analyze("manual")

    def _set_interval(self, sec: int) -> None:
        self.cfg["auto_interval_sec"] = sec
        config.save(self.cfg)
        self._schedule_auto()

    def _schedule_auto(self) -> None:
        self.auto_timer.stop()
        if not self.auto_on:
            return
        interval = max(30, int(self.cfg.get("auto_interval_sec") or 60))
        # 봉 마감 시각(주기의 배수) 3초 뒤에 실행되도록 정렬
        delay = interval - (time.time() % interval) + 3
        self.auto_timer.start(int(delay * 1000))

    def _auto_tick(self) -> None:
        self.analyze("auto")
        self._schedule_auto()

    def switch_view(self) -> None:
        """30·60분봉처럼 관점이 둘일 때, 선을 그릴 관점을 바꾼다."""
        plan = self.overlay.plan
        if plan is not None and plan.cycle_view():
            self.overlay.update()

    def toggle_overlay(self) -> None:
        if self.selector.active:
            return
        if self.overlay.isVisible():
            self.overlay.hide()
        elif self.roi is not None:
            self.overlay.show()
        else:
            self.select_roi()

    # -- 로그 ----------------------------------------------------------------- #
    def _save_log(self, png: bytes, raw: dict, plan: Plan) -> None:
        if not self.cfg.get("save_logs") or self.demo:
            return
        try:
            now = datetime.now()
            folder = config.LOG_DIR / now.strftime("%Y%m%d")
            folder.mkdir(parents=True, exist_ok=True)
            stem = now.strftime("%H%M%S")
            if png:
                (folder / f"{stem}.png").write_bytes(png)
            record = {"time": now.isoformat(timespec="seconds"), "raw": raw, "plan": asdict(plan)}
            (folder / f"{stem}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def _prune_logs(self) -> None:
        keep = int(self.cfg.get("log_keep_days") or 0)
        if keep <= 0 or not config.LOG_DIR.is_dir():
            return
        cutoff = (datetime.now() - timedelta(days=keep)).strftime("%Y%m%d")
        for child in config.LOG_DIR.iterdir():
            if child.is_dir() and len(child.name) == 8 and child.name.isdigit() and child.name < cutoff:
                shutil.rmtree(child, ignore_errors=True)

    # -- 종료 ----------------------------------------------------------------- #
    def quit(self) -> None:
        self.watch_timer.stop()
        self.auto_timer.stop()
        self.hotkeys.unregister_all()
        self.overlay.hide()
        self.tray.hide()
        if self.worker is not None and self.worker.isRunning():
            self.worker.wait(15000)
        self.app.quit()


# --------------------------------------------------------------------------- #
def _excepthook(exc_type, exc, tb) -> None:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now().isoformat(timespec='seconds')}]\n")
            traceback.print_exception(exc_type, exc, tb, file=f)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    demo = "--demo" in argv
    sys.excepthook = _excepthook

    app = QApplication(argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(config.APP_NAME)
    app.setFont(QFont("Malgun Gothic", 9))

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(config.DATA_DIR / "app.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, "TradeVision HUD", "이미 실행 중입니다. 작업 표시줄 트레이 아이콘을 확인하세요.")
        return 0

    controller = Controller(app, demo=demo)
    QTimer.singleShot(0, controller.start)
    code = app.exec()
    lock.unlock()
    return code
