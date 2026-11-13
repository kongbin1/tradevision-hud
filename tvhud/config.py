"""설정 파일 로드/저장.

설정과 로그는 프로젝트 폴더가 아니라 %LOCALAPPDATA%\\TradeVisionHUD 에 둔다.
(프로젝트 폴더가 OneDrive 안에 있어도 API 키와 캡처 이미지가 클라우드로 동기화되지 않게 하기 위함)
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

APP_NAME = "TradeVisionHUD"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_NAME
CONFIG_PATH = DATA_DIR / "config.json"
LOG_DIR = DATA_DIR / "logs"
ERROR_LOG = DATA_DIR / "error.log"

DEFAULTS: dict = {
    # Gemini API 키. 환경변수 GEMINI_API_KEY 가 있으면 그쪽이 우선한다.
    "api_key": "",
    # "auto" = 계정에서 쓸 수 있는 최신 Flash 모델을 자동 선택. 특정 모델명을 적어도 된다.
    "model": "auto",
    # AI의 '생각' 수준: minimal(가장 빠름, 약 3초) | low(기본, 약 5초) | medium | high | default(모델 기본, 약 10초)
    "thinking": "low",
    # 처리 등급: "standard"(기본) | "priority"(혼잡해도 우선 처리, 요금 75~100% 추가,
    # Google AI Studio 결제 Tier 2 이상만 적용. 그 외 계정은 일반 처리와 같음)
    "service_tier": "standard",
    # 차트 영역(논리 픽셀, 전체 데스크톱 기준). null 이면 실행 시 드래그로 지정.
    "roi": None,
    # 자동 갱신 주기(초). 봉 마감 시각에 맞춰 정렬되어 실행된다.
    "auto_interval_sec": 60,
    "hotkeys": {
        "analyze": "ctrl+alt+a",
        "toggle_overlay": "ctrl+alt+h",
        "select_roi": "ctrl+alt+r",
        "toggle_auto": "ctrl+alt+t",
        "switch_view": "ctrl+alt+v",   # 30·60분봉: 단타 ↔ 스윙 선 전환
        "quit": "ctrl+alt+q",
    },
    # 매매 원칙(코드에서 강제 계산하는 값)
    "max_stop_pct": 3.0,         # 단타: 진입 구간 중심가 대비 허용 손절폭 %
    "max_stop_pct_swing": 8.0,   # 스윙·중기: 허용 손절폭 %
    # 프롬프트에 덧붙일 나만의 매매 원칙(자유 서술)
    "user_rules": "",
    # 줌/스크롤 추적: 가격축 숫자를 PC에서 직접 읽어(Windows OCR) 선 위치를 계속 맞춘다
    "track_zoom": True,
    # 화면 변화 감지: 변한 셀의 비율이 이 값을 넘으면 '차트 변경됨'으로 흐리게 표시
    "change_threshold": 0.06,
    # 분석 후 이 시간이 지나면 '오래된 분석'으로 표시(초)
    "stale_after_sec": 300,
    "hud_corner": "bottom-left", # 요약 패널 위치: top-left | top-right | bottom-left | bottom-right
    "max_image_width": 1600,
    "save_logs": True,
    "log_keep_days": 14,
}


def load() -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    try:
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return cfg
    except (OSError, ValueError):
        # 깨진 설정 파일은 백업해 두고 기본값으로 시작
        try:
            CONFIG_PATH.replace(CONFIG_PATH.with_suffix(".broken.json"))
        except OSError:
            pass
        return cfg
    if isinstance(user, dict):
        for key, value in user.items():
            if key == "hotkeys" and isinstance(value, dict):
                cfg["hotkeys"].update(value)
            else:
                cfg[key] = value
    return cfg


def save(cfg: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def api_key(cfg: dict) -> str:
    return (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or str(cfg.get("api_key") or "")
    ).strip()
