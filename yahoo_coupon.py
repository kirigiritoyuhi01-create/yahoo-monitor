"""
Yahoo Shopping Coupon Bot - Playwright専用版
- L列（クーポン）のみ更新
- 深夜1回実行
- 絶対禁止: A,B,C,D,E,F,G,H,I,J,K,M,N,O,P列
"""

import os
import re
import time
import json
import logging
from datetime import datetime, timezone, timedelta
import gspread
from google.oauth2.service_account import Credentials

# ───────────────────────────────────────────
# 設定
# ───────────────────────────────────────────
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
SHEET_NAME     = os.environ.get("SHEET_NAME", "Sheet1")
GCP_SA_JSON    = os.environ["GCP_SERVICE_ACCOUNT_JSON"]

COUPON_INTERVAL = 2.0  # Playwright実行間隔（秒）

# 列定義（0始まりインデックス）
COL = {
    "JAN":     1,   # B 読み取り専用
    "クーポン": 11,  # L ← このBOTが更新する唯一の列
}

# このBOTが書き込んでよい列はL=11のみ
WRITABLE_COLS  = {11}
FORBIDDEN_COLS = {0,1,2,3,4,5,6,7,8,9,10,12,13,14,15}

JST = timezone(timedelta(hours=9))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)


# ───────────────────────────────────────────
# スプレッドシート接続
# ───────────────────────────────────────────
def get_sheet():
    sa_info = json.loads(GCP_SA_JSON)
    creds = Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)


# ───────────────────────────────────────────
# 列破壊防止
# ───────────────────────────────────────────
def safe_update_coupon(sheet, row: int, value):
    col_idx = COL["クーポン"]
    if col_idx in FORBIDDEN_COLS:
        raise RuntimeError(f"列破壊防止: 列{col_idx+1}への書き込みは禁止")
    sheet.update_cell(row, col_idx + 1, value)


# ───────────────────────────────────────────
# Playwright クーポン取得
# ───────────────────────────────────────────
def get_coupon_playwright(jan_code: str) -> int:
    """
    一覧ページ（価格+送料安い順・新品）から
    1件目のクーポンバッジを取得
    返り値: クーポン額（円）/ 0=なし / -1=取得失敗
    """
    url = (
        f"https://shopping.yahoo.co.jp/search/{jan_code}/0/"
        f"?tab_ex=commerce&used=2&prom=1&X=12"
    )
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            # 確認済みセレクタ
            selector = '[class*="SearchResultItem__coupon--withLabel"]'
            elem = page.query_selector(selector)
            browser.close()

            if not elem:
                return 0

            coupon_text = elem.inner_text()
            nums = re.findall(r"\d+", coupon_text.replace(",", ""))
            return int(nums[0]) if nums else 0

    except Exception as e:
        log.warning(f"Playwright クーポン取得失敗 ({jan_code}): {e}")
        return -1


# ───────────────────────────────────────────
# メイン処理
# ───────────────────────────────────────────
def run():
    log.info("=== Yahoo Coupon BOT 開始（深夜1回版）===")
    sheet = get_sheet()

    all_values = sheet.get_all_values()
    processed  = 0
    errors     = 0

    for i, row_data in enumerate(all_values):
        sheet_row = i + 1
        if sheet_row == 1:
            continue  # ヘッダースキップ

        jan_code = row_data[COL["JAN"]].strip() if len(row_data) > COL["JAN"] else ""

        if not jan_code:
            continue  # 空行スキップ

        log.info(f"[行{sheet_row}] クーポン取得: JAN={jan_code}")

        coupon_val = get_coupon_playwright(jan_code)

        if coupon_val == -1:
            log.warning(f"[行{sheet_row}] クーポン取得失敗")
            errors += 1
        else:
            try:
                safe_update_coupon(sheet, sheet_row, coupon_val)
                processed += 1
            except RuntimeError as e:
                log.error(str(e))
                continue

        time.sleep(COUPON_INTERVAL)

    log.info(f"=== 完了: 成功={processed}, エラー={errors} ===")


if __name__ == "__main__":
    run()
