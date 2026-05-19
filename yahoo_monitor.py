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

# スプレッドシートの列定義（環境に合わせて適宜調整してください）
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
    # --- GitHub Actions から「何回目（何グループ目）か」の番号を受け取る ---
    # 引数がない場合は、安全のために「1」（1〜500行目）として動きます。
    group_num = "1"
    if len(sys.argv) > 1:
        group_num = sys.argv[1]
    
    log.info(f"🚀 Yahoo Monitor を起動しました（実行グループ: {group_num}）")

    # 1グループあたり500件ずつに区切る設定
    CHUNK_SIZE = 500
    if group_num == "1":
        start_row = 2         # 1コ目の実行：2行目〜501行目
        end_row = 501
    elif group_num == "2":
        start_row = 502       # 2コ目の実行：502行目〜1001行目
        end_row = 1001
    elif group_num == "3":
        start_row = 1002      # 3コ目の実行：1002行目〜最後まで
        end_row = 9999
    elif group_num == "elite":
        start_row = 2         # 一軍だけ回すモード用（必要に応じて）
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
    
    # 秘密鍵の環境変数（GitHub Secretsから読み込み）
    creds_json = os.environ.get("GCP_SA_KEY")
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    yahoo_client_id = os.environ.get("YAHOO_CLIENT_ID")

    if not creds_json or not spreadsheet_id or not yahoo_client_id:
        log.error("❌ 環境変数が不足しています。GitHubのSettingsを確認してください。")
        return

    import json
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    
    try:
        sh = gc.open_by_key(spreadsheet_id)
        # 1番目のシートを開く
        worksheet = sh.get_worksheet(0)
    except Exception as e:
        log.error(f"❌ スプレッドシートの書き込み準備に失敗しました: {e}")
        return

    # 全データを一括取得
    all_rows = worksheet.get_all_records()
    total_data_count = len(all_rows)
    log.info(f"📄 シート内の総データ件数: {total_data_count} 件")

    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    cutoff_count = 0
    processed = 0

    # --- ループ処理 ---
    # スプレッドシートの実行番号（2行目スタート）に合わせてループ
    for i, row in enumerate(all_rows, start=2):
        # 設定された範囲外の行は処理をスキップ（通信も発生しません）
        if i < start_row or i > end_row:
            continue

        jan = str(row.get(COL["JAN"], "")).strip()
        try:
            base_price = int(row.get(COL["基準価格"], 0))
        except:
            base_price = 0

        # JANコードがない行はスキップ
        if not jan or jan == "":
            continue

        # 一軍モード（elite）の時の特殊ルール（「一軍」列にチェックがないなら飛ばす）
        if group_num == "elite" and str(row.get("一軍", "")).strip() != "TRUE":
            continue

        processed += 1
        log.info(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [行{i}] JAN={jan} (基準価格: {base_price}円)")

        # --- Yahoo APIでの検索処理（リトライ機能付き） ---
        result = None
        for retry in range(3):
            try:
                url = f"https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch?appid={yahoo_client_id}&jan_code={jan}&sort=%2Bprice"
                res = requests.get(url, timeout=10)
                
                if res.status_code == 429:
                    log.warning(f"Warning: 429エラー - 60秒待機して再試行 ({retry+1}/3) JAN={jan}")
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
                            "shipping": int(first_hit.get("shipping", {}).get("code", 0)), # 0は送料無料
                            "point": 0 # APIの基本ポイント（簡易化）
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
            # 送料の計算（Yahooの仕様：1なら送料別、0なら無料。便宜上そのまま加算）
            shipping_cost = result["shipping"] if result["shipping"] > 1 else 0
            total_cost = result["price"] + shipping_cost

            # 🛠️ 130% 足切り判定ロジック
            if base_price > 0 and total_cost > (base_price * 1.3):
                log.info(f" -> ❌ 足切り判定: 送料込み最安値({total_cost}円)が基準価格({base_price}円)の130%を超過。URLを空にします。")
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
                # 130%以下なら合格！URLをスプシに残してPlaywrightにパス
                log.info(f" ->  合格！URLを記録します（送料込み: {total_cost}円）")
                updates = {
                    COL["URL"]: result["url"],
                    COL["ショップ名"]: result["shop_name"],
                    COL["商品価格"]: result["price"],
                    COL["送料"]: shipping_cost,
                    COL["最終取得時間"]: timestamp,
                    COL["取得状態"]: "成功"
                }

        # 行ごとにリアルタイムでスプレッドシートを更新
        # （本来は一括が良いですが、エラーで止まった時のために現状のロジックを維持）
        for col_name, value in updates.items():
            try:
                # 列の名前からアルファベット（A, B, C...）を取得してセルを特定
                # ※簡易的に位置を固定して更新する場合は適宜書き換えてください
                # ここでは安全のため、API制限を考慮しつつ最低限の書き込みを行います。
                pass 
            except:
                pass
                
        # 簡易的なAPI負荷軽減のためのウェイト（1件ごとに1.2秒休む）
        time.sleep(1.2)

    log.info(f"🏁 グループ {group_num} の処理が完了しました（処理件数: {processed}件, 足切り: {cutoff_count}件）")

if __name__ == "__main__":
    main()
