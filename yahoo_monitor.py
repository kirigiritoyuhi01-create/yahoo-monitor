"""
Yahoo Shopping Monitor Bot - API専用・高速一括更新・足切り分離版
- Yahoo APIで10件取得 → 送料込み最安値選択
- 429エラー時は待機して再試行（最大3回）
- 120%以上の高値商品はURLを書き込まずに足切り（yahoo_coupon.pyの負荷激減）
- 処理終了後にスプレッドシートへ一括バルク書き込み（30分制限を確実に回避）
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
API_INTERVAL   = 1.0  # API制限対策（1秒/リクエスト）
RETRY_WAITS    = [60, 120, 180]  # 429エラー時の待機時間（秒）

# 列定義（0始まりインデックス）
COL = {
    "商品名":       0,  # A 読み取り専用
    "JAN":         1,  # B 読み取り専用
    "価格基準":     2,  # C 読み取り専用（仕入れ基準価格。120%足切り判定に使用）
    "URL":         6,  # G
    "ショップ名":   7,  # H
    "商品価格":     8,  # I
    "送料":         9,  # J
    "ポイント":    10,  # K
    # L=11 クーポン → yahoo_coupon.pyが管理
    # M=12, N=13 → スプレッドシート関数・触禁止
    "最終取得時間": 14,  # O
    "取得状態":     15,  # P
}

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
# Yahoo Shopping API（10件取得→送料込み最安値選択）
# ───────────────────────────────────────────
def search_yahoo_best(jan_code: str) -> Optional[dict]:
    """
    JANコードで新品10件取得し、送料込み最安値の1件を返す
    429エラー時は待機して最大3回リトライ
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

    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            resp = requests.get(YAHOO_API_URL, params=params, timeout=15)

            # 429エラー（Too Many Requests）→ 待機して再試行
            if resp.status_code == 429:
                if attempt < len(RETRY_WAITS):
                    wait = RETRY_WAITS[attempt]
                    log.warning(f"429エラー: {wait}秒待機して再試行 ({attempt+1}/{len(RETRY_WAITS)}) JAN={jan_code}")
                    time.sleep(wait)
                    continue
                else:
                    log.error(f"429エラー: 最大リトライ超過 JAN={jan_code}")
                    return {"error": "API失敗"}

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
    log.info("=== Yahoo Monitor BOT 開始（分離・一括高速版） ===")
    sheet = get_sheet()

    all_values = sheet.get_all_values()
    processed  = 0
    errors     = 0
    cutoff_count = 0

    # 一括アップデート用のセルリスト
    cell_list = []

    for i, row_data in enumerate(all_values):
        sheet_row = i + 1
        if sheet_row == 1:
            continue  # ヘッダースキップ

        jan_code     = row_data[COL["JAN"]].strip()    if len(row_data) > COL["JAN"]    else ""
        product_name = row_data[COL["商品名"]].strip() if len(row_data) > COL["商品名"] else ""

        # C列の基準価格を取得（数値に変換、カンマ等を除去）
        base_price_str = row_data[COL["価格基準"]].strip() if len(row_data) > COL["価格基準"] else ""
        base_price = 0
        if base_price_str:
            try:
                base_price = int(base_price_str.replace(",", "").replace("円", ""))
            except ValueError:
                base_price = 0

        if not jan_code and not product_name:
            continue  # 空行スキップ

        if not jan_code:
            log.warning(f"[行{sheet_row}] JANコードなし・スキップ: {product_name}")
            continue

        log.info(f"[行{sheet_row}] JAN={jan_code} (基準価格: {base_price}円)")

        result    = search_yahoo_best(jan_code)
        timestamp = now_jst()

        updates = {}

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
            # ───────────────────────────────────────────
            # 【新機能】120%足切り判定ロジック
            # ───────────────────────────────────────────
            total_cost = result["price"] + result["shipping"]
            
            # C列に基準価格が入っており、かつ送料込み最安値がその120%を超えている場合
            if base_price > 0 and total_cost > (base_price * 1.3):
                log.info(f" -> ❌ 足切り判定: 送料込み最安値({total_cost}円)が基準価格({base_price}円)の130%を超過。URLを空にします。")
                updates = {
                    COL["URL"]:         "",  # URLを空にしてyahoo_coupon.pyの巡回対象から外す
                    COL["ショップ名"]:   result["shop_name"],
                    COL["商品価格"]:     result["price"],
                    COL["送料"]:         result["shipping"],
                    COL["ポイント"]:     result["point"],
                    COL["最終取得時間"]: timestamp,
                    COL["取得状態"]:     "足切り(高値)",
                }
                cutoff_count += 1
            else:
                # 120%以下なら合格！正常にURLを書き込んでPlaywrightにパスする
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

        # 列破壊防止の検証＆一括保存用リストへの追加
        for col_idx, value in updates.items():
            if col_idx in FORBIDDEN_COLS:
                raise RuntimeError(f"列破壊防止: 列{col_idx+1}への書き込みは禁止されています")
            
            # gspread用のCellオブジェクトを作成してプールする
            cell_list.append(gspread.Cell(row=sheet_row, col=col_idx + 1, value=value))

        time.sleep(API_INTERVAL)

    # ───────────────────────────────────────────
    # 【新機能】最後に一括（バルク）でスプレッドシートへ書き込み
    # ───────────────────────────────────────────
    if cell_list:
        log.info(f"--- スプレッドシートに一括保存中... (合計 {len(cell_list)} セル) ---")
        sheet.update_cells(cell_list, value_input_option="USER_ENTERED")
        log.info("--- スプレッドシートの一括保存が完了しました！ ---")

    log.info(f"=== 完了: 正常合格={processed}, 高値足切り={cutoff_count}, エラー={errors} ===")


if __name__ == "__main__":
    run()
