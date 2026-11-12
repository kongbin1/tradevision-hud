"""TradeVision HUD 실행 진입점.  사용법: run.bat  (또는 python main.py [--demo])"""
import sys

from tvhud.app import main

if __name__ == "__main__":
    sys.exit(main())