"""Gemini Vision 차트 분석 엔진."""
from __future__ import annotations

import json
import re
import time
from typing import Literal

from pydantic import BaseModel

from . import config


# --------------------------------------------------------------------------- #
# 응답 스키마 (Gemini structured output 으로 강제)
# --------------------------------------------------------------------------- #
class AxisTick(BaseModel):
    price: float
    y: int


class Level(BaseModel):
    type: Literal["SUPPORT", "RESISTANCE"]
    price: float
    note: str


class BuyZone(BaseModel):
    enabled: bool
    price_top: float
    price_bottom: float


class HorizonView(BaseModel):
    """한 매매 관점(단타 또는 스윙)의 계획."""
    horizon: Literal["SCALP", "SWING"]
    action: Literal["BUY_ON_PULLBACK", "WAIT", "AVOID"]
    summary: str
    holding_period: str
    buy_zone: BuyZone
    stop_loss_price: float
    entry_condition: str


class ChartAnalysis(BaseModel):
    chart_readable: bool
    symbol: str
    timeframe: str
    market_status: Literal["UPTREND", "PULLBACK", "REBOUND", "SIDEWAYS", "DOWNTREND_WARNING", "UNKNOWN"]
    summary: str
    reasons: list[str]
    current_price: float
    price_axis: list[AxisTick]
    levels: list[Level]
    preferred_horizon: Literal["SCALP", "SWING"]
    horizon_reason: str
    views: list[HorizonView]


SYSTEM_PROMPT = """\
당신은 한국 주식 매매를 보조하는 차트 판독기입니다. 봉 주기에 따라 단타 또는 스윙(중기) 관점으로 해석합니다.
입력은 증권사 캔들 차트의 스크린샷 한 장입니다. 화면에 실제로 보이는 것만 근거로 삼고,
보이지 않는 것은 추측하지 마세요. 결과는 사람이 최종 판단할 때 참고하는 보조 자료입니다.

[좌표 규칙]
- 이미지 최상단 y=0, 최하단 y=1000 인 정규화 좌표를 씁니다.
- price_axis: 차트 가격축(보통 우측)에 인쇄된 눈금 숫자를 가능한 한 모두(최소 3개) 읽어
  각 눈금의 가격(price)과 그 숫자 글자의 세로 중심 위치(y)를 적습니다.
  일반 눈금만 포함하고, 현재가·최고가·최저가처럼 색 배경이 있는 배지는 제외합니다. 숫자를 지어내지 마세요.
- 그 외 모든 위치는 좌표가 아니라 '가격'으로 답합니다. 가격은 가격축 눈금과 캔들 위치를 대조해 읽습니다.

[공통 분석]
- symbol: 화면에 보이는 종목명(없으면 빈 문자열).
- timeframe: 화면에서 선택된 봉 주기를 정확히 읽습니다(예: "1분", "3분", "15분", "30분", "60분", "일", "주", "월").
  모르면 빈 문자열. 이 값으로 관점이 정해지므로 중요합니다.
- current_price: 현재가(모르면 0).
- 캔들 패턴, 이동평균선(5·20·60·120) 배열, 거래량 급증 구간, 주요 매물대를 봅니다.
  이동평균선은 차트 상단 범례로 어느 색이 몇 선인지 확인될 때만 단정합니다.
- levels: 현재가 바로 아래의 핵심 지지(SUPPORT)와 바로 위의 핵심 저항(RESISTANCE)을 우선으로, 최대 4개. note는 근거 12자 이내.
- summary: 현재 추세 진단 1~2문장(80자 이내). reasons: 판단 근거 최대 2개, 각 40자 이내.

[관점 선택 - 봉 주기에 따름]
- 1분·3분·5분·10분·15분봉 → views 에 SCALP(단타) 하나.
- 30분·60분봉(및 2~4시간봉) → views 에 SCALP 와 SWING 둘 다.
  preferred_horizon 에 지금 차트가 어느 쪽에 더 맞는지 고르고, horizon_reason 에 이유를 40자 이내로.
- 일봉·주봉·월봉 → views 에 SWING(스윙·중기, 보유 수일~1개월) 하나.
- 봉 주기를 모르면 차트 모양으로 판단해 가장 맞는 관점 하나.
- 관점이 하나뿐이면 preferred_horizon 은 그 관점, horizon_reason 은 빈 문자열.

[관점별 기준]
- SCALP(단타, 보유 당일~1·2일): 분봉의 직전 저점·5·20선 지지, 거래량 변화 중심.
  buy_zone 폭은 현재가의 0.5~2%, 손절은 직전 분봉 저점 바로 아래(대략 -1~3%).
- SWING(스윙·중기, 보유 수일~1개월): 일봉 기준 추세, 20·60일선, 주요 매물대·전저점 중심.
  buy_zone 폭은 현재가의 1~5%, 손절은 핵심 지지(전저점·60일선) 이탈 지점(대략 -3~8%).
  30·60분봉에서 SWING 을 볼 때는 화면에 보이는 더 큰 흐름(며칠치 고점·저점, 긴 이평선)을 기준으로 합니다.
- holding_period: 예상 보유 기간을 짧게(예: "당일", "1~2일", "1~2주", "2~4주").

[매매 원칙 - 모든 관점에서 반드시 준수]
1. 추격 매수 금지. 급등 중인 양봉을 따라 사는 타점은 제시하지 않습니다.
2. 진입은 상승 추세 또는 정배열 전환 구간에서 눌림목 분할 진입만 고려합니다.
3. stop_loss_price 는 직전 지지선(직전 저점) 바로 아래, 반드시 buy_zone 하단보다 낮게 둡니다.
4. 익절(목표가)은 사용자가 직접 판단하므로 다루지 않습니다.
5. 다음은 진입 금지 구간입니다 → action 을 AVOID 로 하고 buy_zone.enabled=false:
   - 이평선 역배열 상태에서 하락이 가속되는 구간
   - 거래량이 실린 장대음봉 직후 하락이 진행 중
   - 직전 지지선을 이탈한 직후
6. 근거가 애매하면 WAIT 입니다. 지금 바로 사라는 신호는 BUY_ON_PULLBACK 일 때만 냅니다.
   같은 차트라도 단타는 WAIT, 스윙은 BUY_ON_PULLBACK 처럼 관점마다 다를 수 있습니다.

[각 view 의 buy_zone 작성 규칙]
- BUY_ON_PULLBACK: 지금 분할 진입을 검토할 수 있는 눌림 구간.
- WAIT: '진입 관심 구간'을 반드시 제시합니다. 현재가 아래의 가장 가까운 유효 지지 부근 가격대를 buy_zone 으로,
  그 아래 손절가를 stop_loss_price 로. 현재가보다 높은 가격대는 추격 매수이므로 금지.
  화면에 근거가 되는 지지가 전혀 없을 때만 enabled=false.
- AVOID: buy_zone.enabled=false.
- entry_condition: 그 구간에서 실제로 진입하려면 확인해야 할 조건을 40자 이내로
  (예: "9,000원 지지 확인 후 양봉 전환 시", "60일선 지지 후 거래량 증가 시"). buy_zone 이 없으면 빈 문자열.
- view.summary: 그 관점에서의 판단 한 문장(50자 이내).
- buy_zone.enabled=false 이면 price_top, price_bottom, stop_loss_price 는 0.

[판독 불가]
- 차트가 아니거나 판독이 불가능하면 chart_readable=false, 나머지는 빈 값/0/UNKNOWN, views 는 빈 배열.
"""

_EXCLUDE = ("image", "tts", "live", "audio", "embedding", "native", "computer", "robotics", "thinking", "omni")


class AnalyzerError(Exception):
    """사용자에게 그대로 보여줄 수 있는 한국어 오류 메시지.

    auth=True 이면 API 키 문제(없음/무효)라서 키를 다시 입력받아야 한다.
    """

    def __init__(self, message: str, auth: bool = False):
        super().__init__(message)
        self.auth = auth


def _rank_model(name: str) -> tuple | None:
    m = re.fullmatch(r"gemini-(\d+(?:\.\d+)?)-flash(.*)", name)
    if not m or any(word in name for word in _EXCLUDE):
        return None
    suffix = m.group(2)
    full = "lite" not in suffix      # lite 는 정확도가 낮아 마지막 대안으로만 쓴다
    stable = suffix in ("", "-lite")
    return (full, float(m.group(1)), stable, -len(name))


MAX_ATTEMPTS = 7          # 한 번 분석할 때 최대 호출 수
TOTAL_BUDGET_SEC = 60     # 한 번 분석에 쓸 최대 시간(이후엔 새 시도를 시작하지 않음)
REQUEST_TIMEOUT_SEC = 20  # 모델 1회 호출 최대 대기(정상일 때 응답은 4~10초)
COOLDOWN_SEC = 120        # 혼잡/한도 초과였던 모델은 이 시간 동안 뒤로 미룬다


class Analyzer:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._client = None
        self._key = ""
        self._models: list[str] = []          # 자동 선택 후보(좋은 순)
        self._preferred = ""                  # 마지막으로 성공한 모델
        self._cooldown: dict[str, float] = {}  # 모델 → 다시 써도 되는 시각
        self._no_thinking: set[str] = set()    # thinking 설정을 거부한 모델
        self._tier_rejected = False            # service_tier 를 계정이 거부함

    # -- 내부 --------------------------------------------------------------- #
    def _get_client(self):
        key = config.api_key(self.cfg)
        if not key:
            raise AnalyzerError("Gemini API 키가 없습니다. 트레이 메뉴 > API 키 설정에서 입력하세요.", auth=True)
        if self._client is None or key != self._key:
            from google import genai
            from google.genai import types

            self._client = genai.Client(
                api_key=key, http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_SEC * 1000)
            )
            self._key = key
            self._models = []
            self._preferred = ""
        return self._client

    def _list_models(self, client) -> list[str]:
        if self._models:
            return self._models
        found: list[tuple[tuple, str]] = []
        try:
            for m in client.models.list():
                name = (getattr(m, "name", "") or "").split("/")[-1]
                actions = getattr(m, "supported_actions", None) or []
                if actions and "generateContent" not in actions:
                    continue
                rank = _rank_model(name)
                if rank:
                    found.append((rank, name))
        except Exception as exc:  # 목록 조회 실패 → 기본 별칭으로 시도
            if _status_code(exc) in (400, 401, 403):
                raise _friendly(exc) from exc
        self._models = [n for _, n in sorted(found, reverse=True)] or ["gemini-flash-latest"]
        return self._models

    def _candidates(self, client) -> list[str]:
        """시도 순서: 지정 모델 → 마지막 성공 모델 → 최신순. 쿨다운 중인 모델은 맨 뒤."""
        wanted = str(self.cfg.get("model") or "auto").strip()
        order: list[str] = []
        if wanted.lower() != "auto":
            order.append(wanted)
        last_good = self._preferred or str(self.cfg.get("last_good_model") or "")
        if "lite" in last_good:
            last_good = ""
        if last_good:
            order.append(last_good)
        order += self._list_models(client)
        order = list(dict.fromkeys(order))
        now = time.time()
        ready = [m for m in order if self._cooldown.get(m, 0) <= now]
        cooling = [m for m in order if self._cooldown.get(m, 0) > now]
        return ready + cooling

    def _gen_config(self, system: str, model: str):
        from google.genai import types

        kw = dict(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=ChartAnalysis,
            temperature=0.2,
        )
        tier = str(self.cfg.get("service_tier") or "standard").strip().lower()
        if tier in ("priority", "flex") and not self._tier_rejected:
            kw["service_tier"] = tier
        level = str(self.cfg.get("thinking") or "low").strip().lower()
        if level not in ("", "default") and model not in self._no_thinking:
            if re.match(r"gemini-2\.", model):
                kw["thinking_config"] = types.ThinkingConfig(thinking_budget=0 if level in ("minimal", "off") else 1024)
            else:
                kw["thinking_config"] = types.ThinkingConfig(thinking_level=level)
        return types.GenerateContentConfig(**kw)

    # -- 공개 --------------------------------------------------------------- #
    def analyze(self, png: bytes, progress=None) -> tuple[dict, str, float]:
        """PNG 바이트 → (원본 JSON dict, 사용 모델명, 소요 초).

        모델이 혼잡(503)·한도 초과(429)·시간 초과면 기다리지 않고 다음 모델로 넘어간다.
        progress(text) 가 주어지면 진행 상황을 알려 준다.
        """
        from google.genai import types

        client = self._get_client()
        system = SYSTEM_PROMPT
        extra = str(self.cfg.get("user_rules") or "").strip()
        if extra:
            system += "\n[사용자 추가 원칙]\n" + extra + "\n"
        contents = [
            types.Part.from_bytes(data=png, mime_type="image/png"),
            "이 차트를 규칙에 따라 분석해 JSON으로만 답하세요.",
        ]

        started = time.time()
        last_exc: Exception | None = None
        busy_models: list[str] = []
        self._last_429 = None
        queue = self._candidates(client)
        last_good = self._preferred or str(self.cfg.get("last_good_model") or "")
        retried: set[str] = set()
        attempts = 0
        while queue and attempts < MAX_ATTEMPTS and time.time() - started < TOTAL_BUDGET_SEC:
            model = queue[0]
            attempts += 1
            if progress:
                note = f" · {', '.join(busy_models)} 혼잡 → 다른 모델" if busy_models else ""
                progress(f"{model} 응답 대기{note}")
            t0 = time.time()
            try:
                resp = client.models.generate_content(
                    model=model, contents=contents, config=self._gen_config(system, model)
                )
                headers = getattr(getattr(resp, "sdk_http_response", None), "headers", None) or {}
                served = headers.get("x-gemini-service-tier", "")
                _log_attempt(model, "OK", time.time() - t0, f"tier={served}" if served else "")
            except Exception as exc:
                last_exc = exc
                code = _status_code(exc)
                text = str(exc)
                _log_attempt(model, code or type(exc).__name__, time.time() - t0, text)
                if code in (400, 403) and ("service_tier" in text or "service tier" in text.lower()) \
                        and not self._tier_rejected:
                    self._tier_rejected = True   # 등급 미달 → 일반 처리로 같은 모델 재시도
                    continue
                if code == 400 and "thinking" in text.lower() and model not in self._no_thinking:
                    self._no_thinking.add(model)   # 이 모델은 thinking 설정 없이 같은 모델로 재시도
                    continue
                if code in (500, 503) and model == last_good and model not in retried:
                    # 방금까지 잘 되던 모델의 503은 대개 순간적 → 잠깐 쉬고 한 번 더
                    retried.add(model)
                    if progress:
                        progress(f"{model} 일시 혼잡 → 잠시 후 재시도")
                    time.sleep(1.5)
                    continue
                if code == 429:
                    self._last_429 = exc
                if code in (429, 500, 503, 504) or _is_timeout(exc):
                    self._cooldown[model] = time.time() + COOLDOWN_SEC
                    busy_models.append(model)
                    queue.pop(0)
                    continue
                if code == 404:
                    if model in self._models:
                        self._models.remove(model)
                    if self._preferred == model:
                        self._preferred = ""
                    if self.cfg.get("last_good_model") == model:
                        self.cfg["last_good_model"] = ""
                    queue.pop(0)
                    continue
                raise _friendly(exc) from exc

            text = (resp.text or "").strip()
            if not text:
                raise AnalyzerError("AI가 빈 응답을 반환했습니다. 다시 시도하세요.")
            try:
                data = json.loads(text)
            except ValueError as exc:
                raise AnalyzerError("AI 응답(JSON) 해석 실패. 다시 시도하세요.") from exc
            if not isinstance(data, dict):
                raise AnalyzerError("AI 응답 형식이 올바르지 않습니다.")
            if "lite" not in model:   # lite 는 혼잡할 때의 대안일 뿐, 기본으로 기억하지 않는다
                self._preferred = model
            return data, model, time.time() - started
        # 혼잡과 한도 초과가 섞이면, 사용자가 조치할 수 있는 한도 초과(429)를 우선 알린다
        if self._last_429 is not None:
            last_exc = self._last_429
        err = _friendly(last_exc)
        if busy_models:
            err = AnalyzerError(f"{err}\n(시도한 모델: {', '.join(dict.fromkeys(busy_models))})", err.auth)
        raise err


def _log_attempt(model: str, result, secs: float, detail: str) -> None:
    """모델 호출 1회의 결과를 attempts.log 에 한 줄로 남긴다(원인 진단용)."""
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        detail = " ".join(detail.split())[:240]
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {model:26s} {str(result):6s} {secs:5.1f}s  {detail}\n"
        path = config.DATA_DIR / "attempts.log"
        if path.exists() and path.stat().st_size > 512_000:
            path.replace(path.with_suffix(".old.log"))
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def _is_timeout(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    return "timeout" in name or "timed out" in str(exc).lower()


def _status_code(exc: Exception | None) -> int:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    return code if isinstance(code, int) else 0


def _friendly(exc: Exception | None) -> AnalyzerError:
    code = _status_code(exc)
    detail = str(exc or "")[:600]
    if code in (401, 403) or (code == 400 and ("API key" in detail or "API_KEY" in detail)):
        return AnalyzerError("API 키가 유효하지 않거나 권한이 없습니다. 키를 다시 입력하세요.", auth=True)
    if code == 404:
        return AnalyzerError("모델을 찾을 수 없습니다. 설정의 model 값을 \"auto\"로 바꿔 보세요.")
    if code == 429:
        per_day = "PerDay" in detail or "per day" in detail.lower()
        return AnalyzerError(
            "API 무료 사용 한도 초과(429)"
            + (" - 오늘 일일 한도를 다 썼습니다. 내일 다시 쓰거나 유료 결제를 연결하세요." if per_day
               else ". 1분 정도 기다렸다가 다시 시도하세요.")
        )
    if code in (500, 503, 504):
        return AnalyzerError("Gemini 모델들이 모두 혼잡합니다(구글 서버 사정). 1~2분 뒤 Ctrl+Alt+A 로 다시 시도하세요.")
    if exc is not None and _is_timeout(exc):
        return AnalyzerError(f"응답 시간 초과({REQUEST_TIMEOUT_SEC}초). 네트워크를 확인하거나 잠시 후 다시 시도하세요.")
    if code == 0:
        return AnalyzerError(f"네트워크 또는 알 수 없는 오류: {detail}")
    return AnalyzerError(f"API 오류({code}): {detail}")


def demo_result() -> dict:
    """API 키 없이 오버레이를 확인하기 위한 가짜 응답(--demo). 60분봉 → 단타·스윙 둘 다."""
    time.sleep(0.8)
    return {
        "chart_readable": True,
        "symbol": "데모 종목",
        "timeframe": "60분",
        "market_status": "PULLBACK",
        "summary": "데모 데이터입니다. 실제 차트 분석이 아니며 선 위치는 화면과 무관합니다.",
        "reasons": ["20선 지지 확인 후 음봉 눌림", "직전 저점 위에서 거래량 감소"],
        "current_price": 13480,
        "price_axis": [
            {"price": 14000, "y": 100},
            {"price": 13750, "y": 300},
            {"price": 13500, "y": 500},
            {"price": 13250, "y": 700},
            {"price": 13000, "y": 900},
        ],
        "levels": [
            {"type": "RESISTANCE", "price": 13820, "note": "직전 고점"},
            {"type": "SUPPORT", "price": 13150, "note": "직전 저점"},
        ],
        "preferred_horizon": "SWING",
        "horizon_reason": "일봉 상승 추세 중 눌림이라 스윙에 유리",
        "views": [
            {
                "horizon": "SCALP", "action": "WAIT", "summary": "단기 하락 중, 분봉 지지 확인 필요",
                "holding_period": "당일",
                "buy_zone": {"enabled": True, "price_top": 13380, "price_bottom": 13300},
                "stop_loss_price": 13220, "entry_condition": "13,300원 지지 후 양봉 전환 시",
            },
            {
                "horizon": "SWING", "action": "BUY_ON_PULLBACK", "summary": "상승 추세 속 20선 눌림, 분할 진입 검토",
                "holding_period": "1~3주",
                "buy_zone": {"enabled": True, "price_top": 13300, "price_bottom": 13050},
                "stop_loss_price": 12850, "entry_condition": "60선 위 유지 시 분할 매수",
            },
        ],
    }
