"""AI 응답 검증 + 가격→화면좌표 변환 + 매매 원칙 강제.

핵심 설계: AI에게는 '가격'만 받는다. 선을 그을 y 좌표는 AI가 읽어준
가격축 눈금(가격, y)들로 회귀식을 만들어 코드가 직접 계산한다.
목표가/손절폭/손익비 같은 산술도 AI가 아니라 코드가 계산한다.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field

STATUS_LABELS = {
    "UPTREND": "상승 추세",
    "PULLBACK": "상승 중 눌림목",
    "REBOUND": "단기 반등 구간",
    "SIDEWAYS": "횡보",
    "DOWNTREND_WARNING": "하락 경고",
    "UNKNOWN": "판단 불가",
}
ACTION_LABELS = {
    "BUY_ON_PULLBACK": "눌림목 분할매수 대기",
    "WAIT": "관망 권장",
    "AVOID": "진입 금지",
}


def fmt_price(p: float) -> str:
    if abs(p) >= 1000:
        return f"{p:,.0f}"
    return f"{p:,.2f}".rstrip("0").rstrip(".")


def _num(value, default: float = 0.0) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


# --------------------------------------------------------------------------- #
# 가격축 보정
# --------------------------------------------------------------------------- #
@dataclass
class AxisFit:
    mode: str       # "linear" | "log"
    a: float
    b: float
    rms: float      # 잔차 RMS (0~1000 스케일)
    n: int          # 사용한 눈금 수

    def y_ratio(self, price: float) -> float | None:
        """가격 → 이미지 높이 대비 비율(0.0=최상단, 1.0=최하단)."""
        if price <= 0:
            return None
        x = math.log(price) if self.mode == "log" else price
        return (self.a * x + self.b) / 1000.0

    def price_at(self, ratio: float) -> float | None:
        """화면 높이 비율(0.0=위, 1.0=아래) → 가격. y_ratio 의 역함수."""
        if self.a == 0:
            return None
        x = (ratio * 1000 - self.b) / self.a
        if self.mode == "log":
            try:
                return math.exp(x)
            except OverflowError:
                return None
        return x

    @property
    def quality(self) -> str:
        if self.n < 3:
            return "낮음(눈금 2개)"
        if self.rms <= 6:
            return "좋음"
        if self.rms <= 15:
            return "보통"
        return "낮음"


def _lsq(points: list[tuple[float, float]]) -> tuple[float, float, list[float]] | None:
    n = len(points)
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    sxx = sum((x - mx) ** 2 for x, _ in points)
    if sxx <= 0:
        return None
    a = sum((x - mx) * (y - my) for x, y in points) / sxx
    b = my - a * mx
    return a, b, [abs(a * x + b - y) for x, y in points]


def _fit_mode(ticks: list[tuple[float, float]], mode: str) -> AxisFit | None:
    pts = [(math.log(p) if mode == "log" else p, y) for p, y in ticks]
    res = _lsq(pts)
    if res is None:
        return None
    a, b, resid = res
    rms = _rms(resid)
    # 눈금이 4개 이상이고 적합이 나쁘면, 하나씩 빼 보고 확연히 좋아지는 경우
    # 그 눈금을 오독으로 보고 버린다(leave-one-out).
    if len(pts) >= 4 and rms > 8.0:
        best = None
        for i in range(len(pts)):
            sub = pts[:i] + pts[i + 1:]
            r = _lsq(sub)
            if r is not None and (best is None or _rms(r[2]) < _rms(best[1][2])):
                best = (sub, r)
        if best is not None and _rms(best[1][2]) < 0.4 * rms:
            pts, (a, b, resid) = best
            rms = _rms(resid)
    if a >= 0:  # 가격이 높을수록 화면 위쪽(y 작음)이어야 한다
        return None
    return AxisFit(mode, a, b, rms, len(pts))


def _rms(resid: list[float]) -> float:
    return math.sqrt(sum(r * r for r in resid) / len(resid))


def fit_axis(raw_ticks) -> AxisFit | None:
    seen: dict[float, float] = {}
    for t in raw_ticks or []:
        if not isinstance(t, dict):
            continue
        price, y = _num(t.get("price")), _num(t.get("y"), -1)
        if price > 0 and 0 <= y <= 1000:
            seen.setdefault(price, y)
    ticks = sorted(seen.items())
    if len(ticks) < 2:
        return None
    linear = _fit_mode(ticks, "linear")
    log = _fit_mode(ticks, "log") if len(ticks) >= 3 else None
    # 로그 스케일 차트: 로그 적합이 확실히 더 좋을 때만 채택
    if log and (linear is None or log.rms < 0.6 * linear.rms - 0.5):
        return log
    return linear


# --------------------------------------------------------------------------- #
# 매매 관점(단타 / 스윙)
# --------------------------------------------------------------------------- #
HORIZON_LABELS = {"SCALP": "단타", "SWING": "스윙·중기"}
HORIZON_HOLD = {"SCALP": "당일~1·2일", "SWING": "수일~1개월"}


def horizons_for(timeframe: str) -> list[str] | None:
    """봉 주기 → 해석 관점.  1~15분: 단타 / 30분~4시간: 단타+스윙 / 일·주·월: 스윙.  모르면 None."""
    tf = (timeframe or "").replace(" ", "").lower()
    if not tf:
        return None
    if any(k in tf for k in ("일", "주", "월", "day", "week", "month", "1d", "1w", "1m봉")) and "분" not in tf:
        return ["SWING"]
    m = re.search(r"(\d+)\s*(분|min|m)", tf)
    if m:
        n = int(m.group(1))
        return ["SCALP"] if n <= 15 else ["SCALP", "SWING"]
    m = re.search(r"(\d+)\s*(시간|h)", tf)
    if m:
        return ["SCALP", "SWING"]
    return None


@dataclass
class View:
    """한 관점(단타 또는 스윙)의 매매 계획."""
    horizon: str = "SCALP"
    action: str = "WAIT"
    summary: str = ""
    hold: str = ""
    zone: tuple[float, float] | None = None                 # (top, bottom)
    conditional: bool = False      # True = 관망 중 '진입 관심 구간'(지금 사라는 신호 아님)
    condition: str = ""            # 진입 조건 문구
    stop: float = 0.0
    risk_pct: float = 0.0
    facts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return HORIZON_LABELS.get(self.horizon, self.horizon)

    @property
    def action_label(self) -> str:
        return ACTION_LABELS.get(self.action, self.action)


# --------------------------------------------------------------------------- #
# 플랜(오버레이에 그릴 최종 데이터)
# --------------------------------------------------------------------------- #
@dataclass
class Plan:
    readable: bool = True
    symbol: str = ""
    timeframe: str = ""
    status: str = "UNKNOWN"
    summary: str = ""
    reasons: list[str] = field(default_factory=list)
    current: float = 0.0
    axis: AxisFit | None = None
    axis_source: str = "AI"        # "AI" | "OCR"(PC에서 가격축을 직접 읽음, 줌/스크롤 추적)
    levels: list[dict] = field(default_factory=list)       # {type, price, note}
    views: list[View] = field(default_factory=list)        # 관점별 계획(1~2개)
    selected: int = 0              # 선을 그릴 관점
    preferred: str = ""            # AI가 더 맞다고 본 관점
    preferred_reason: str = ""
    warnings: list[str] = field(default_factory=list)      # 공통 경고
    model: str = ""
    elapsed: float = 0.0
    ts: float = field(default_factory=time.time)

    # 오버레이가 쓰는 '현재 선택된 관점' 바로가기
    @property
    def view(self) -> View | None:
        return self.views[self.selected] if 0 <= self.selected < len(self.views) else None

    @property
    def action(self) -> str:
        return self.view.action if self.view else "WAIT"

    @property
    def zone(self):
        return self.view.zone if self.view else None

    @property
    def conditional(self) -> bool:
        return bool(self.view and self.view.conditional)

    @property
    def stop(self) -> float:
        return self.view.stop if self.view else 0.0

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def action_label(self) -> str:
        return ACTION_LABELS.get(self.action, self.action)

    def cycle_view(self) -> bool:
        if len(self.views) < 2:
            return False
        self.selected = (self.selected + 1) % len(self.views)
        return True


def _nearest_levels(levels: list[dict], current: float) -> list[dict]:
    """현재가 기준 가장 가까운 지지 1개(아래) + 저항 1개(위)만 남긴다."""
    supports = [lv for lv in levels if lv["type"] == "SUPPORT"]
    resists = [lv for lv in levels if lv["type"] == "RESISTANCE"]
    if current > 0:
        below = [lv for lv in supports if lv["price"] <= current]
        above = [lv for lv in resists if lv["price"] >= current]
        supports = sorted(below or supports, key=lambda lv: abs(current - lv["price"]))
        resists = sorted(above or resists, key=lambda lv: abs(lv["price"] - current))
    return supports[:1] + resists[:1]


def _build_view(rv: dict, horizon: str, status: str, current: float, cfg: dict) -> View:
    v = View(horizon=horizon)
    v.action = str(rv.get("action") or "WAIT")
    if v.action not in ACTION_LABELS:
        v.action = "WAIT"
    v.summary = str(rv.get("summary") or "").strip()
    v.hold = str(rv.get("holding_period") or "").strip() or HORIZON_HOLD.get(horizon, "")
    v.condition = str(rv.get("entry_condition") or "").strip()

    # ---- 매매 원칙 강제 (AI가 뭐라 하든 코드가 최종 판정) ----
    if status == "DOWNTREND_WARNING" and v.action == "BUY_ON_PULLBACK":
        v.action = "WAIT"
        v.warnings.append("하락 경고 구간 → 진입 보류로 변경")

    bz = rv.get("buy_zone") if isinstance(rv.get("buy_zone"), dict) else {}
    top, bottom = _num(bz.get("price_top")), _num(bz.get("price_bottom"))
    if top < bottom:
        top, bottom = bottom, top
    stop = _num(rv.get("stop_loss_price"))

    zone_ok = False
    if v.action in ("BUY_ON_PULLBACK", "WAIT") and bz.get("enabled") and bottom > 0:
        problem = ""
        if not (0 < stop < bottom):
            problem = "손절가가 진입 구간 아래에 없음"
        elif 0 < current <= stop:
            problem = "현재가가 이미 손절가 이하"
        elif current > 0 and bottom > current * 1.003:
            problem = "진입 구간이 현재가보다 위(추격 매수)"
        if problem:
            v.warnings.append(f"{problem} → 구간 무효")
        else:
            zone_ok = True
    if v.action == "BUY_ON_PULLBACK" and not zone_ok:
        v.action = "WAIT"
        v.warnings.append("유효한 진입 구간이 없어 관망으로 변경")

    if zone_ok:
        v.zone = (top, bottom)
        v.conditional = v.action != "BUY_ON_PULLBACK"
        v.stop = stop
        entry = (top + bottom) / 2
        v.risk_pct = (entry - stop) / entry * 100
        # 익절은 사용자가 직접 판단 → 목표가는 계산·표시하지 않는다
        name = "진입 관심" if v.conditional else "진입 추천"
        v.facts.append(f"{name} {fmt_price(bottom)} ~ {fmt_price(top)}")
        v.facts.append(f"손절 추천 {fmt_price(stop)} (구간 중심가 대비 -{v.risk_pct:.1f}%)")
        if v.condition:
            v.facts.append(f"진입 조건: {v.condition}")
        key = "max_stop_pct_swing" if horizon == "SWING" else "max_stop_pct"
        max_stop = _num(cfg.get(key), 8.0 if horizon == "SWING" else 3.0)
        if v.risk_pct > max_stop:
            v.warnings.append(f"손절폭 -{v.risk_pct:.1f}%로 큼({v.label} 허용 {max_stop:g}%)")
        if current > 0:
            if current > top:
                gap = (current - top) / current * 100
                v.facts.append(f"현재가 {fmt_price(current)} → 구간까지 -{gap:.1f}%. 추격 금지, 눌림 대기")
            elif current >= bottom:
                v.facts.append(f"현재가 {fmt_price(current)} → 구간 안. 조건 충족 여부 확인")
            else:
                v.facts.append(f"현재가 {fmt_price(current)} → 구간 하단 이탈, 손절가 주의")
    return v


def build_plan(raw: dict, cfg: dict, model: str = "", elapsed: float = 0.0) -> Plan:
    plan = Plan(model=model, elapsed=elapsed)
    plan.readable = bool(raw.get("chart_readable", True))
    plan.symbol = str(raw.get("symbol") or "").strip()
    plan.timeframe = str(raw.get("timeframe") or "").strip()
    plan.status = str(raw.get("market_status") or "UNKNOWN")
    plan.summary = str(raw.get("summary") or "").strip()
    plan.reasons = [str(r).strip() for r in (raw.get("reasons") or []) if str(r).strip()][:2]
    plan.current = _num(raw.get("current_price"))

    if not plan.readable:
        plan.warnings.append("차트를 인식하지 못했습니다. 차트 영역을 다시 지정해 보세요.")
        return plan

    plan.axis = fit_axis(raw.get("price_axis"))
    if plan.axis is None:
        plan.warnings.append("가격축 인식 실패 → 선을 그릴 수 없습니다. 영역에 우측 가격축을 포함하세요.")
    elif plan.axis.quality.startswith("낮음"):
        plan.warnings.append(f"가격축 보정 정확도 {plan.axis.quality} → 선 위치 오차 가능")

    # 지지/저항
    for lv in raw.get("levels") or []:
        if not isinstance(lv, dict):
            continue
        price = _num(lv.get("price"))
        kind = str(lv.get("type") or "")
        if price > 0 and kind in ("SUPPORT", "RESISTANCE"):
            plan.levels.append({"type": kind, "price": price, "note": str(lv.get("note") or "").strip()})
    plan.levels = _nearest_levels(plan.levels, plan.current)

    # 관점: 봉 주기로 코드가 정한다(모르면 AI가 준 관점을 그대로 쓴다)
    allowed = horizons_for(plan.timeframe)
    raw_views = [rv for rv in (raw.get("views") or []) if isinstance(rv, dict)]
    by_h: dict[str, dict] = {}
    for rv in raw_views:
        h = str(rv.get("horizon") or "")
        if h in HORIZON_LABELS and h not in by_h:
            by_h[h] = rv
    order = allowed or [h for h in ("SCALP", "SWING") if h in by_h] or ["SCALP"]
    plan.views = [_build_view(by_h.get(h, {}), h, plan.status, plan.current, cfg) for h in order]

    pref = str(raw.get("preferred_horizon") or "")
    plan.preferred = pref if pref in order else order[0]
    plan.preferred_reason = str(raw.get("horizon_reason") or "").strip() if len(order) > 1 else ""
    plan.selected = order.index(plan.preferred)
    return plan
