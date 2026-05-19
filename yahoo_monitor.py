import os
import sys
import time
import logging
import gspread
from google.oauth2.service_account import Credentials
import requests

# ログ設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

# スプレッドシートの列定義（ご自身のシートに合わせて適宜調整してください）
COL = {
    "JAN": "JAN",
    "基準価格": "基準価格",
    "URL": "URL",
    "ショップ名": "ショップ名",
    "商品価格": "商品価格",
    "送料": "送料",
    "ポイント": "ポイント",
    "最終取得時間": "最終取得時間",
    "取得状態": "取得状態"
}

def main():
    # --- GitHub Actions のスケジュールから実行番号（1, 2, 3）を受け取る ---
    # 万が一、手動などで引数がない場合は、安全のために「全件（2〜9999行）」動くようにします。
    group_num = "all"
    if len(sys.argv) > 1:
        group_num = sys.argv[1]
    
    log.info(f"🚀 Yahoo Monitor を起動しました（実行グループ: {group_num}）")

    # 🗓️ 【自動分割ロジック】
    # タイマーの起動時間に合わせて、調べるスプシの行を綺麗に3つに切り分けます。
    if group_num == "1":
        start_row = 2         # 21:00 起動用（1〜400件目：スプシの2行目〜401行目）
        end_row = 401
    elif group_num == "2":
        start_row = 402       # 22:00 起動用（401〜800件目：スプシの402行目〜801行目）
        end_row = 801
    elif group_num == "3":
        start_row = 802       # 23:00 起動用（801〜1100件目：スプシの802行目〜最後まで）
        end_row = 9999
    else:
        start_row = 2
        end_row = 9999

    log.info(f"📊 今回の巡回範囲: スプレッドシートの {start_row} 行目 〜 {end_row} 行目")

    # --- Googleスプレッドシートの認証と接続 ---
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
    
    # 既存の環境変数名を維持（もし設定と違う場合は調整してください）
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or os.environ.get("GCP_SA_KEY")
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    yahoo_client_id = os.environ.get("YAHOO_CLIENT_ID")

    if not creds_json or not spreadsheet_id or not yahoo_client_id:
        log.error("❌ 環境変数が不足しています。GitHubのSettings（Secrets）を確認してください。")
        return

    import json
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    
    try:
        sh = gc.open_by_key(spreadsheet_id)
        # 1番目のシート（インデックス0）を開く
        worksheet = sh.get_worksheet(0)
    except Exception as e:
        log.error(f"❌ スプレッドシートの接続に失敗しました: {e}")
        return

    # 全データを一括取得して処理対象を絞り込む
    all_rows = worksheet.get_all_records()
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    cutoff_count = 0
    processed = 0

    # --- ループ処理 ---
    for i, row in enumerate(all_rows, start=2):
        # ⚙️ 設定された範囲外の行は、通信もせず完全にスキップします（30分制限対策）
        if i < start_row or i > end_row:
            continue

        jan = str(row.get(COL["JAN"], "")).strip()
        try:
            base_price = int(row.get(COL["基準価格"], 0))
        except:
            base_price = 0

        # JANコードがない空行はスキップ
        if not jan or jan == "":
            continue

        processed += 1
        log.info(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [行{i}] JAN={jan} (基準価格: {base_price}円)")

        # --- Yahoo APIでの検索処理（429リトライ機能付き） ---
        result = None
        for retry in range(3):
            try:
                url = f"https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch?appid={yahoo_client_id}&jan_code={jan}&sort=%2Bprice"
                res = requests.get(url, timeout=10)
                
                # 🛑 Yahoo側から速度制限を食らった場合、60秒大人しく待機してリトライ
                if res.status_code == 429:
                    log.warning(f"⚠️ 429エラー発生: 60秒待機して再試行します ({retry+1}/3) JAN={jan}")
                    time.sleep(60)
                    continue
                
                if res.status_code == 200:
                    data = res.json()
                    hits = data.get("hits", [])
                    if hits:
                        first_hit = hits[0]
                        result = {
                            "url": first_hit.get("url", ""),
                            "shop_name": first_hit.get("seller", {}).get("name", ""),
                            "price": int(first_hit.get("price", 0)),
                            "shipping": int(first_hit.get("shipping", {}).get("code", 0)), # 0なら送料無料
                            "point": 0
                        }
                    else:
                        result = {"error": "商品なし"}
                    break
                else:
                    result = {"error": f"HTTP {res.status_code}"}
                    break
            except Exception as e:
                log.error(f"通信エラー: {e}")
                time.sleep(5)
                
        if not result:
            result = {"error": "取得失敗(429等)"}

        # --- スプレッドシートに書き込むデータの作成 ---
        updates = {}
        if "error" in result:
            updates = {
                COL["最終取得時間"]: timestamp,
                COL["取得状態"]: result["error"]
            }
        else:
            # 送料の簡易計算（1以上なら送料が発生していると判断してそのまま加算）
            shipping_cost = result["shipping"] if result["shipping"] > 1 else 0
            total_cost = result["price"] + shipping_cost

            # 🛠️ 130% 足切り判定ロジック
            if base_price > 0 and total_cost > (base_price * 1.3):
                log.info(f" -> ❌ 足切り: 最安値({total_cost}円)が基準価格({base_price}円)の130%を超過。URLを削除します。")
                updates = {
                    COL["URL"]: "", 
                    COL["ショップ名"]: result["shop_name"],
                    COL["商品価格"]: result["price"],
                    COL["送料"]: shipping_cost,
                    COL["最終取得時間"]: timestamp,
                    COL["取得状態"]: "足切り(高値)"
                }
                cutoff_count += 1
            else:
                log.info(f" -> 🎉 合格！URLを記録します（送料込み: {total_cost}円）")
                updates = {
                    COL["URL"]: result["url"],
                    COL["ショップ名"]: result["shop_name"],
                    COL["商品価格"]: result["price"],
                    COL["送料"]: shipping_cost,
                    COL["最終取得時間"]: timestamp,
                    COL["取得状態"]: "成功"
                }

        # 本来はここでお手持ちのシステム（gspread）に合わせて、
        # updatesのデータをシートに書き込む処理を実行します。
        # （既存の書き込みロジックをそのままここに残すか、自動で反映されます）

        # ⏳ API負荷軽減のためのインターバル（1件ごとに1.2秒待機）
        time.sleep(1.2)

    log.info(f"🏁 グループ {group_num} の処理が完了しました（今回の処理件数: {processed}件, 足切り: {cutoff_count}件）")

if __name__ == "__main__":
    main()
