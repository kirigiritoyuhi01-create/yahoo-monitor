"""
Yahoo Shopping Coupon & Point Bot - Playwright専用・高速一括・足切り連携版
- G列のURLが空（yahoo_monitor.pyで足切りされた商品）の場合はPlaywrightを起動せずスキップ
- ブラウザを1回だけ起動して使い回すことで、処理速度を劇的に向上
- 処理終了後にスプレッドシートへ一括バルク書き込み（API制限回避）
- L列（クーポン）、Q列（クーポン取得時間）、R列（クーポン取得状態）、S列（ショップ独自ポイント）を更新
- 絶対禁止: A〜K, M〜P, T以降の列（列破壊防止ガード搭載）
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

COUPON_INTERVAL = 1.5  # 精度のために1.5秒に調整

# 列定義（0始まりインデックス）
COL = {
    "JAN":              1,  # B 読み取り専用
    "URL":              6,  # G 読み取り専用（足切り判定に使用）
    "クーポン":         11,  # L ← 更新対象
    "クーポン取得時間": 16,  # Q ← 更新対象
    "クーポン取得状態": 17,  # R ← 更新対象
    "ショップポイント": 18,  # S ← 【新設】更新対象
}

# このBOTが書き込んでよい列
WRITABLE_COLS  = {11, 16, 17, 18}  # L, Q, R, S
# 絶対に上書きしてはいけない禁止列
FORBIDDEN_COLS = {0,1,2,3,4,5,6,7,8,9,10,12,13,14,15}  # A〜K, M〜P。T以降は安全のため触らない

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
# Playwright クーポン＆ショップ独自ポイント同時取得
# ───────────────────────────────────────────
def get_coupon_and_point_playwright(page, jan_code: str) -> tuple:
    """
    立ち上がっているpageオブジェクトを使って、クーポン(円)とショップ独自ポイント(%)を同時に取得。
    返り値: (クーポン額(int), ショップポイント(int))  ※エラー時は (-1, 0)
    """
    url = (
        f"https://shopping.yahoo.co.jp/search/{jan_code}/0/"
        f"?tab_ex=commerce&used=2&prom=1&X=12"
    )
    
    coupon_val = 0
    shop_point = 0
    
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)  # 読み込み待機

        # 1. クーポンの抽出（SearchResultItem__coupon--withLabel クラスをターゲット）
        coupon_selector = '[class*="SearchResultItem__coupon--withLabel"]'
        coupon_elem = page.query_selector(coupon_selector)

        if coupon_elem:
            coupon_text = coupon_elem.inner_text()
            nums = re.findall(r"\d+", coupon_text.replace(",", ""))
            if nums:
                coupon_val = int(nums[0])

        # 2. ショップ独自ストアボーナスの抽出
        # Yahoo検索画面の「+○%」「対象商品でさらに+○%」というテキストを狙い撃ち
        # 主に "SearchResultItem__bonus" やポイント関連のクラスを検知
        point_selector = '[class*="SearchResultItem__bonus"], [class*="SearchResultItem__point"]'
        point_elems = page.query_selector_all(point_selector)
        
        for elem in point_elems:
            text = elem.inner_text()
            # 「+5%」「+10%」といった表記から数字だけを引っこ抜く
            if "+" in text and "%" in text:
                match = re.search(r"\+(\d+)%", text)
                if match:
                    # 見つかった中で最も高い倍率をショップボーナスとして採用
                    extracted_point = int(match.group(1))
                    if extracted_point > shop_point:
                        shop_point = extracted_point
                        
        log.info(f"-> スクレイピング結果 ({jan_code}): クーポン={coupon_val}円, ストアボーナス=+{shop_point}%")
        return coupon_val, shop_point

    except Exception as e:
        log.warning(f"Playwright データ取得失敗 ({jan_code}): {e}")
        return -1, 0


# ───────────────────────────────────────────
# JST現在時刻
# ───────────────────────────────────────────
def now_jst() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M")


# ───────────────────────────────────────────
# メイン処理
# ───────────────────────────────────────────
def run():
    log.info("=== Yahoo Coupon & Point BOT 開始（ストアボーナス対応版） ===")
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

            # 【足切り連携】モニター側でURLが入っていない（除外された）行は完全スルー
            if not url_val:
                log.info(f"[行{sheet_row}] ⏩ 足切り商品のためスキップ (JAN={jan_code})")
                skipped += 1
                continue

            log.info(f"[行{sheet_row}] 🔍 クーポン＆ストアボーナス精査を開始: JAN={jan_code}")

            # 本当に必要な本命が来た時だけ、初めてブラウザを起動（超省エネ・高速化）
            if browser is None:
                log.info("-> 🚀 Playwright ブラウザを起動します...")
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()

            coupon_val, shop_point = get_coupon_and_point_playwright(page, jan_code)
            timestamp  = now_jst()

            if coupon_val == -1:
                log.warning(f"[行{sheet_row}] 取得エラー発生")
                updates = {
                    COL["クーポン取得時間"]: timestamp,
                    COL["クーポン取得状態"]: "取得失敗",
                    COL["ショップポイント"]: 0,
                }
                errors += 1
            else:
                updates = {
                    COL["クーポン"]:         coupon_val,
                    COL["クーポン取得時間"]: timestamp,
                    COL["クーポン取得状態"]: "成功",
                    COL["ショップポイント"]: shop_point, # S列にストアボーナス（%）を代入
                }
                processed += 1

            # 列破壊防止の厳密な検証＆一括保存リストへのプール
            for col_idx, value in updates.items():
                if col_idx in FORBIDDEN_COLS:
                    raise RuntimeError(f"列破壊防止ガード発動: 禁止された列{col_idx+1}への書き込みをブロックしました")
                cell_list.append(gspread.Cell(row=sheet_row, col=col_idx + 1, value=value))

            time.sleep(COUPON_INTERVAL)

        # すべての処理が終わったらブラウザを安全に閉じる
        if browser:
            log.info("-> 🛑 Playwright ブラウザを終了します。")
            browser.close()

    # ───────────────────────────────────────────
    # 最後に一括（バルク）でスプレッドシートへ書き込み
    # ───────────────────────────────────────────
    if cell_list:
        log.info(f"--- スプレッドシートに一括保存中... (合計 {len(cell_list)} セル) ---")
        sheet.update_cells(cell_list, value_input_option="USER_ENTERED")
        log.info("--- スプレッドシートの一括保存が完了しました！ ---")

    log.info(f"=== 完了: 精査成功={processed}, スキップ={skipped}, エラー={errors} ===")


if __name__ == "__main__":
    run()
