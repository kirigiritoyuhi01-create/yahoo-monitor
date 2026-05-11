"""
Yahoo Shopping Monitor Bot - API専用版
- Yahoo APIで10件取得 → 送料込み最安値選択
- クーポンはyahoo_coupon.pyで別途取得
- 更新対象: G,H,I,J,K,O,P列のみ（L列クーポンは触らない）
- 絶対禁止: A,B,C,D,E,F,M,N列
"""

import os
import time
import json
import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional
import gspread
from google.oauth2.service_account import Credentials

# ───────────────────────────────────────────
# 設定
# ───────────────────────────────────────────
YAHOO_APP_ID   = os.environ["YAHOO_APP_ID"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
SHEET_NAME     = os.environ.get("SHEET_NAME", "Sheet1")
GCP_SA_JSON    = os.environ["GCP_SERVICE_ACCOUNT_JSON"]

YAHOO_API_URL  = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"
FETCH_COUNT    = 10   # 送料込み最安値計算のために取得する件数
API_INTERVAL   = 1  # API制限対策（1秒/リクエスト）

# 列定義（0始まりインデックス）
COL = {
    "商品名":       0,   # A 読み取り専用
    "JAN":         1,   # B 読み取り専用
    # C,D,E,F → ルデヤBOT管理・触禁止
    "URL":         6,   # G ← 更新対象
    "ショップ名":   7,   # H ← 更新対象
    "商品価格":     8,   # I ← 更新対象
    "送料":         9,   # J ← 更新対象
    "ポイント":    10,   # K ← 更新対象
    # L=11 クーポン → yahoo_coupon.pyが管理
    # M=12, N=13 → スプレッドシート関数・触禁止
    "最終取得時間": 14,  # O ← 更新対象
    "取得状態":    15,   # P ← 更新対象
}

# このBOTが書き込んでよい列のみ（クーポンL=11は含まない）
WRITABLE_COLS  = {6, 7, 8, 9, 10, 14, 15}       # G,H,I,J,K,O,P
FORBIDDEN_COLS = {0, 1, 2, 3, 4, 5, 11, 12, 13} # A,B,C,D,E,F,L,M,N

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
# 列破壊防止（絶対に外さない）
# ───────────────────────────────────────────
def bulk_update_row(sheet, row: int, updates: dict):
    for col_idx in updates:
        if col_idx in FORBIDDEN_COLS:
            raise RuntimeError(
                f"列破壊防止: 列{col_idx+1}({'ABCDEFGHIJKLMNOP'[col_idx]})への書き込みは禁止"
            )
        if col_idx not in WRITABLE_COLS:
            raise RuntimeError(f"予期しない列: 列{col_idx+1}")

    cell_list = [
        gspread.Cell(row=row, col=col_idx + 1, value=value)
        for col_idx, value in updates.items()
    ]
    sheet.update_cells(cell_list, value_input_option="USER_ENTERED")


# ───────────────────────────────────────────
# Yahoo Shopping API（10件取得→送料込み最安値選択）
# ───────────────────────────────────────────
def search_yahoo_best(jan_code: str) -> Optional[dict]:
    """
    JANコードで新品10件取得し、送料込み最安値の1件を返す
    返り値: {url, shop_name, price, shipping, point} or None or {"error": "..."}
    """
    params = {
        "appid":     YAHOO_APP_ID,
        "jan_code":  jan_code,
        "results":   FETCH_COUNT,
        "sort":      "+price",
        "condition": "new",
        "in_stock":  "true",
    }
    try:
        resp = requests.get(YAHOO_API_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        hits = data.get("hits", [])
        if not hits:
            return None

        # 送料込み金額で最安値を選ぶ
        best_item          = None
        best_total         = float("inf")
        best_shipping_cost = 0

        for item in hits:
            price    = item.get("price", 0) or 0
            shipping = item.get("shipping", {})

            if shipping.get("code") == 2 or shipping.get("name") == "送料無料":
                shipping_cost = 0
            else:
                shipping_cost = shipping.get("lowestPrice", 0) or 0

            total = price + shipping_cost
            if total < best_total:
                best_total         = total
                best_item          = item
                best_shipping_cost = shipping_cost

        if best_item is None:
            return None

        # ポイント（LYPポイント優先）
        point_data = best_item.get("point", {})
        point = (
            point_data.get("lyLimitedBonusAmount") or
            point_data.get("amount") or
            0
        )

        return {
            "url":       best_item.get("url", ""),
            "shop_name": best_item.get("seller", {}).get("name", ""),
            "price":     best_item.get("price", 0),
            "shipping":  best_shipping_cost,
            "point":     point,
        }

    except requests.exceptions.Timeout:
        log.warning(f"Yahoo API タイムアウト: {jan_code}")
        return {"error": "API失敗"}
    except Exception as e:
        log.error(f"Yahoo API エラー: {e}")
        return {"error": "API失敗"}


# ───────────────────────────────────────────
# JST現在時刻
# ───────────────────────────────────────────
def now_jst() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M")


# ───────────────────────────────────────────
# メイン処理
# ───────────────────────────────────────────
def run():
    log.info("=== Yahoo Monitor BOT 開始（APIのみ版）===")
    sheet = get_sheet()

    all_values = sheet.get_all_values()
    processed  = 0
    errors     = 0

    for i, row_data in enumerate(all_values):
        sheet_row = i + 1
        if sheet_row == 1:
            continue  # ヘッダースキップ

        jan_code     = row_data[COL["JAN"]].strip()    if len(row_data) > COL["JAN"]    else ""
        product_name = row_data[COL["商品名"]].strip() if len(row_data) > COL["商品名"] else ""

        if not jan_code and not product_name:
            continue  # 空行スキップ

        if not jan_code:
            log.warning(f"[行{sheet_row}] JANコードなし・スキップ: {product_name}")
            continue

        log.info(f"[行{sheet_row}] JAN={jan_code}")

        result    = search_yahoo_best(jan_code)
        timestamp = now_jst()

        if result is None:
            updates = {
                COL["URL"]:         "",
                COL["ショップ名"]:   "",
                COL["商品価格"]:     "",
                COL["送料"]:         "",
                COL["ポイント"]:     "",
                COL["最終取得時間"]: timestamp,
                COL["取得状態"]:     "商品なし",
            }
        elif "error" in result:
            updates = {
                COL["最終取得時間"]: timestamp,
                COL["取得状態"]:     result["error"],
            }
            errors += 1
        else:
            updates = {
                COL["URL"]:         result["url"],
                COL["ショップ名"]:   result["shop_name"],
                COL["商品価格"]:     result["price"],
                COL["送料"]:         result["shipping"],
                COL["ポイント"]:     result["point"],
                COL["最終取得時間"]: timestamp,
                COL["取得状態"]:     "成功",
            }
            processed += 1

        try:
            bulk_update_row(sheet, sheet_row, updates)
        except RuntimeError as e:
            log.error(str(e))
            continue

        time.sleep(API_INTERVAL)

    log.info(f"=== 完了: 成功={processed}, エラー={errors} ===")


if __name__ == "__main__":
    run()
