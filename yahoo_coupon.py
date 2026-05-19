"""
Yahoo Shopping Coupon Bot - Playwright専用・高速一括・足切り連携版
- G列のURLが空（yahoo_monitor.pyで足切りされた高値商品）の場合はPlaywrightを起動せずスキップ！
- ブラウザを1回だけ起動して使い回すことで、処理速度を劇的に向上
- 処理終了後にスプレッドシートへ一括バルク書き込み（制限回避）
- L列（クーポン）、Q列（クーポン取得時間）、R列（クーポン取得状態）を更新
- 絶対禁止: A〜K, M〜P列
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
SPREADSHEET_ID  = os.environ["SPREADSHEET_ID"]
SHEET_NAME      = os.environ.get("SHEET_NAME", "Sheet1")
GCP_SA_JSON     = os.environ["GCP_SERVICE_ACCOUNT_JSON"]

COUPON_INTERVAL = 1.0  # ブラウザ使い回しにより、間隔を少し短縮可能

# 列定義（0始まりインデックス）
COL = {
    "JAN":              1,  # B 読み取り専用
    "URL":              6,  # G 読み取り専用（足切り判定に使用）
    "クーポン":         11,  # L ← 更新対象
    "クーポン取得時間": 16,  # Q ← 更新対象
    "クーポン取得状態": 17,  # R ← 更新対象
}

# このBOTが書き込んでよい列
WRITABLE_COLS  = {11, 16, 17}  # L, Q, R
FORBIDDEN_COLS = {0,1,2,3,4,5,6,7,8,9,10,12,13,14,15}  # A〜K, M〜P

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
# Playwright クーポン取得（ブラウザ/ページ使い回し版）
# ───────────────────────────────────────────
def get_coupon_playwright(page, jan_code: str) -> int:
    """
    立ち上がっているpageオブジェクトを使って、クーポンを取得。
    返り値: クーポン額（円）/ 0=なし / -1=取得失敗
    """
    url = (
        f"https://shopping.yahoo.co.jp/search/{jan_code}/0/"
        f"?tab_ex=commerce&used=2&prom=1&X=12"
    )
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)  # 少し待機

        # 確認済みセレクタ
        selector = '[class*="SearchResultItem__coupon--withLabel"]'
        elem = page.query_selector(selector)

        if not elem:
            return 0

        coupon_text = elem.inner_text()
        nums = re.findall(r"\d+", coupon_text.replace(",", ""))
        return int(nums[0]) if nums else 0

    except Exception as e:
        log.warning(f"Playwright クーポン取得失敗 ({jan_code}): {e}")
        return -1


# ───────────────────────────────────────────
# JST現在時刻
# ───────────────────────────────────────────
def now_jst() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M")


# ───────────────────────────────────────────
# メイン処理
# ───────────────────────────────────────────
def run():
    log.info("=== Yahoo Coupon BOT 開始（分離連携・高速一括版） ===")
    sheet = get_sheet()

    all_values = sheet.get_all_values()
    processed  = 0
    errors     = 0
    skipped    = 0

    # 一括アップデート用のセルリスト
    cell_list = []

    # Playwrightをここで1回だけ起動する（使い回し構造）
    from playwright.sync_api import sync_playwright
    
    with sync_playwright() as p:
        # スキップ判定があるため、本当に必要な行がある場合だけブラウザを開く準備
        browser = None
        page = None

        for i, row_data in enumerate(all_values):
            sheet_row = i + 1
            if sheet_row == 1:
                continue  # ヘッダースキップ

            jan_code = row_data[COL["JAN"]].strip() if len(row_data) > COL["JAN"] else ""
            url_val  = row_data[COL["URL"]].strip() if len(row_data) > COL["URL"] else ""

            if not jan_code:
                continue  # 空行スキップ

            # ───────────────────────────────────────────
            # 【新機能】足切り連携スキップロジック
            # ───────────────────────────────────────────
            # URLが書き込まれていない＝yahoo_monitor側で利益なしと判断された商品は無視！
            if not url_val:
                log.info(f"[行{sheet_row}] ⏩ 足切り商品のためクーポン取得をスキップ (JAN={jan_code})")
                skipped += 1
                continue

            log.info(f"[行{sheet_row}] 🔍 クーポン精査を開始: JAN={jan_code}")

            # スキップをすり抜けた本命の商品が来たら、初めてブラウザを起動する（無駄な起動を防止）
            if browser is None:
                log.info("-> 🚀 Playwright ブラウザを起動します...")
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()

            coupon_val = get_coupon_playwright(page, jan_code)
            timestamp  = now_jst()

            if coupon_val == -1:
                log.warning(f"[行{sheet_row}] クーポン取得エラー")
                updates = {
                    COL["クーポン取得時間"]: timestamp,
                    COL["クーポン取得状態"]: "取得失敗",
                }
                errors += 1
            else:
                updates = {
                    COL["クーポン"]:         coupon_val,
                    COL["クーポン取得時間"]: timestamp,
                    COL["クーポン取得状態"]: "成功",
                }
                processed += 1

            # 列破壊防止の検証＆一括保存リストへのプール
            for col_idx, value in updates.items():
                if col_idx in FORBIDDEN_COLS:
                    raise RuntimeError(f"列破壊防止: 列{col_idx+1}への書き込みは禁止されています")
                cell_list.append(gspread.Cell(row=sheet_row, col=col_idx + 1, value=value))

            time.sleep(COUPON_INTERVAL)

        # すべて終わったらブラウザを閉じる
        if browser:
            log.info("-> 🛑 Playwright ブラウザを終了します。")
            browser.close()

    # ───────────────────────────────────────────
    # 【新機能】最後に一括（バルク）でスプレッドシートへ書き込み
    # ───────────────────────────────────────────
    if cell_list:
        log.info(f"--- スプレッドシートに一括保存中... (合計 {len(cell_list)} セル) ---")
        sheet.update_cells(cell_list, value_input_option="USER_ENTERED")
        log.info("--- スプレッドシートの一括保存が完了しました！ ---")

    log.info(f"=== 完了: クーポン精査成功={processed}, スキップ={skipped}, エラー={errors} ===")


if __name__ == "__main__":
    run()
