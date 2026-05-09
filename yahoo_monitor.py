import os
import re
import time
import json
import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import quote

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
FETCH_COUNT    = 10
API_INTERVAL   = 0.3   # 1.0 → 0.3 に短縮（必要なら 0.5 でも可）

COL = {
    "商品名":       0,
    "JAN":         1,
    "URL":         6,
    "ショップ名":   7,
    "商品価格":     8,
    "送料":         9,
    "ポイント":    10,
    "クーポン":    11,
    "最終取得時間": 14,
    "取得状態":    15,
}

WRITABLE_COLS  = {6, 7, 8, 9, 10, 11, 14, 15}
FORBIDDEN_COLS = {0, 1, 2, 3, 4, 5, 12, 13}

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
    gc = gspread.Client(auth=creds)
    return gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)


# ───────────────────────────────────────────
# 列破壊防止
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
# Yahoo Shopping API
# ───────────────────────────────────────────
def search_yahoo_best(jan_code: str) -> Optional[dict]:
    params = {
        "appid":    YAHOO_APP_ID,
        "jan_code": jan_code,
        "results":  FETCH_COUNT,
        "sort":     "+price",
    }
    try:
        resp = requests.get(YAHOO_API_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        hits = data.get("hits", [])
        if not hits:
            return None

        best_item          = None
        best_total         = float("inf")
        best_shipping_cost = 0

        for item in hits:
            if item.get("condition") != "new":
                continue

            price = int(item.get("price", 0) or 0)
            shipping = item.get("shipping", {}) or {}

            if shipping.get("code") == 2 or shipping.get("name") == "送料無料":
                shipping_cost = 0
            else:
                shipping_cost = int(shipping.get("lowestPrice", 0) or 0)

            total = price + shipping_cost

            if total < best_total:
                best_total = total
                best_item = item
                best_shipping_cost = shipping_cost

        if best_item is None:
            return None

        point_data = best_item.get("point", {}) or {}
        point = (
            point_data.get("lyLimitedBonusAmount") or
            point_data.get("bonusAmount") or
            point_data.get("amount") or
            0
        )

        return {
            "url":       best_item.get("url", ""),
            "shop_name": (best_item.get("seller") or {}).get("name", ""),
            "price":     int(best_item.get("price", 0) or 0),
            "shipping":  best_shipping_cost,
            "point":     int(point or 0),
        }

    except requests.exceptions.Timeout:
        log.warning(f"Yahoo API タイムアウト: {jan_code}")
        return {"error": "API失敗"}
    except Exception as e:
        log.error(f"Yahoo API エラー ({jan_code}): {e}")
        return {"error": "API失敗"}


# ───────────────────────────────────────────
# クーポン文字列解析
# ───────────────────────────────────────────
def parse_coupon_value(coupon_text: str) -> int:
    if not coupon_text:
        return 0

    nums = re.findall(r"\d+", coupon_text.replace(",", ""))
    return int(nums[0]) if nums else 0


# ───────────────────────────────────────────
# Playwright クーポン取得（page使い回し）
# ───────────────────────────────────────────
def get_coupon_from_page(page, jan_code: str) -> int:
    url = (
        f"https://shopping.yahoo.co.jp/search/{quote(jan_code)}/0/"
        f"?tab_ex=commerce&used=2&prom=1&X=12"
    )

    selector = '[class*="SearchResultItem__coupon--withLabel"]'

    try:
        page.goto(url, timeout=10000, wait_until="domcontentloaded")

        try:
            page.wait_for_selector(selector, timeout=2000)
        except Exception:
            return 0

        elem = page.query_selector(selector)
        if not elem:
            return 0

        coupon_text = elem.inner_text()
        return parse_coupon_value(coupon_text)

    except Exception as e:
        log.warning(f"Playwright クーポン取得失敗 ({jan_code}): {type(e).__name__}: {e}")
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
    log.info("=== Yahoo Monitor BOT 開始 ===")
    sheet = get_sheet()
    all_values = sheet.get_all_values()

    processed = 0
    errors = 0

    # 同一JANの無駄呼び出し防止
    item_cache = {}
    coupon_cache = {}

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context()

        # 不要リソースをブロックして軽量化
        def route_handler(route):
            req = route.request
            if req.resource_type in {"image", "media", "font"}:
                route.abort()
            else:
                route.continue_()

        context.route("**/*", route_handler)
        page = context.new_page()

        try:
            for i, row_data in enumerate(all_values):
                sheet_row = i + 1
                if sheet_row == 1:
                    continue

                jan_code = row_data[COL["JAN"]].strip() if len(row_data) > COL["JAN"] else ""
                product_name = row_data[COL["商品名"]].strip() if len(row_data) > COL["商品名"] else ""

                if not jan_code and not product_name:
                    continue

                if not jan_code:
                    log.warning(f"[行{sheet_row}] JANコードなし・スキップ: {product_name}")
                    continue

                log.info(f"[行{sheet_row}] JAN={jan_code}")

                if jan_code in item_cache:
                    result = item_cache[jan_code]
                else:
                    result = search_yahoo_best(jan_code)
                    item_cache[jan_code] = result

                timestamp = now_jst()

                if result is None:
                    updates = {
                        COL["URL"]: "",
                        COL["ショップ名"]: "",
                        COL["商品価格"]: "",
                        COL["送料"]: "",
                        COL["ポイント"]: "",
                        COL["クーポン"]: "",
                        COL["最終取得時間"]: timestamp,
                        COL["取得状態"]: "商品なし",
                    }

                elif "error" in result:
                    updates = {
                        COL["最終取得時間"]: timestamp,
                        COL["取得状態"]: result["error"],
                    }
                    errors += 1

                else:
                    if jan_code in coupon_cache:
                        coupon_val = coupon_cache[jan_code]
                    else:
                        coupon_val = get_coupon_from_page(page, jan_code)
                        coupon_cache[jan_code] = coupon_val

                    if coupon_val == -1:
                        status = "クーポン取得失敗"
                        coupon_val = 0
                    else:
                        status = "成功"

                    updates = {
                        COL["URL"]: result["url"],
                        COL["ショップ名"]: result["shop_name"],
                        COL["商品価格"]: result["price"],
                        COL["送料"]: result["shipping"],
                        COL["ポイント"]: result["point"],
                        COL["クーポン"]: coupon_val,
                        COL["最終取得時間"]: timestamp,
                        COL["取得状態"]: status,
                    }
                    processed += 1

                try:
                    bulk_update_row(sheet, sheet_row, updates)
                except RuntimeError as e:
                    log.error(str(e))
                    continue
                except Exception as e:
                    log.error(f"[行{sheet_row}] シート更新失敗: {e}")
                    errors += 1
                    continue

                time.sleep(API_INTERVAL)

        finally:
            context.close()
            browser.close()

    log.info(f"=== 完了: 成功={processed}, エラー={errors} ===")


if __name__ == "__main__":
    run()
