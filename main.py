# coding: utf-8

import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
import time
import os
import shutil
import datetime
from datetime import timezone, timedelta
import logging
import requests
from pathlib import Path
import pdfplumber
import re
import glob
import base64
import zipfile

# Gmail API用ライブラリ
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# --- 日本時間(JST)の設定 ---
JST = timezone(timedelta(hours=9))
def jst_now(): return datetime.datetime.now(JST)

# --- 設定情報 ---
BASIS_USERNAME = os.getenv('BASIS_USERNAME', '')
BASIS_PASSWORD = os.getenv('BASIS_PASSWORD', '')
LARK_WEBHOOK_URL = os.getenv('LARK_WEBHOOK_URL', "https://open.larksuite.com/open-apis/bot/v2/hook/3827abd3-eb1b-41b3-8df3-e920884d2d30")

# Lark API 認証情報
LARK_APP_ID = os.getenv('LARK_APP_ID', "cli_aac716ae45b81e14")
LARK_APP_SECRET = os.getenv('LARK_APP_SECRET', '')
LARK_WIKI_TOKEN = "Ykm6w1c70iDJ0kkmM68jPcf5pEf"
# テンプレート用FormatシートのID
DEFAULT_SHEET_ID = "Yb6Zwo"

FIXED_COMPANY_NAME = "ベイシス株式会社 IoT推進部"

# GitHub Actions環境（Linux）用に相対パスへ変更
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_CSV_PATH = BASE_DIR / "output.csv"
PROCESSED_DIR = BASE_DIR / "processed"
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# エリア判定用リスト
KANTO_PREFS = ['東京都', '神奈川県', '埼玉県', '千葉県', '茨城県', '栃木県', '群馬県']
KANSAI_PREFS = ['大阪府', '京都府', '兵庫県', '奈良県', '滋賀県', '和歌山県']

TEST_DOWNLOAD_ONLY = False
TEST_CSV_ONLY = False

LOG_DIR = BASE_DIR / "log"
LOG_DIR.mkdir(parents=True, exist_ok=True)
log_filename = f"selenium_log_{datetime.datetime.now().strftime('%Y%m%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(LOG_DIR / log_filename, encoding='utf-8'), logging.StreamHandler()]
)

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

# --- Chromeオプション作成関数 (Headless対応) ---
def get_chrome_options():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    prefs = {
        "download.default_directory": str(DOWNLOAD_DIR),
        "download.prompt_for_download": False,
        "directory_upgrade": True,
        "safebrowsing.enabled": True,
        "safebrowsing.disable_download_protection": True
    }
    options.add_experimental_option("prefs", prefs)
    return options

def get_region_from_address(address):
    if not address:
        return "不明"
    match = re.search(r'([一-龠]{2,3}[都道府県])', address)
    if match:
        prefecture = match.group(1).strip()
        if prefecture in KANTO_PREFS: return "関東"
        elif prefecture in KANSAI_PREFS: return "関西"

    kansai_cities = ['大阪市', '京都市', '神戸市', '堺市', '奈良市', '和歌山市', '大津市', '東大阪市', '西宮市', '尼崎市', '豊中市', '吹田市']
    if any(city in address for city in kansai_cities): return "関西"
    
    kanto_cities = ['横浜市', '川崎市', 'さいたま市', '千葉市', '相模原市', '船橋市', '川口市', '新宿区', '世田谷区']
    if any(city in address for city in kanto_cities): return "関東"

    if match: return f"その他（{match.group(1).strip()}）"
    return "不明"

# --- Lark API 連携処理 (スプレッドシートへの追記＆Formatからの自動複製) ---
def get_lark_tenant_access_token():
    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    headers = {"Content-Type": "application/json; charset=utf-8"}
    body = {"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET}
    try:
        res = requests.post(url, headers=headers, json=body, timeout=10)
        res_json = res.json()
        if res_json.get("code") == 0:
            return res_json.get("tenant_access_token")
        logging.error(f"Lark API Token取得失敗: {res_json}")
    except Exception as e:
        logging.error(f"Lark API接続エラー: {e}")
    return None

def get_spreadsheet_token(tenant_token):
    url = f"https://open.larksuite.com/open-apis/wiki/v2/spaces/get_node?token={LARK_WIKI_TOKEN}"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        res_json = res.json()
        if res_json.get("code") == 0:
            return res_json.get("data", {}).get("node", {}).get("obj_token", LARK_WIKI_TOKEN)
    except Exception as e:
        logging.warning(f"Wikiノード取得エラー（直接Wikiトークンを使用します）: {e}")
    return LARK_WIKI_TOKEN

def write_to_lark_sheet(extracted_data):
    tenant_token = get_lark_tenant_access_token()
    if not tenant_token:
        logging.warning("Lark APIトークンが取得できなかったため、シート書き込みをスキップします。")
        return

    spreadsheet_token = get_spreadsheet_token(tenant_token)
    headers = {
        "Authorization": f"Bearer {tenant_token}",
        "Content-Type": "application/json; charset=utf-8"
    }

    now = jst_now()
    target_sheet_title = f"{now.year}年{now.month}月"
    sheet_id = None

    # 当月タブ（例: 2026年9月）の ID を自動検索
    try:
        sheets_url = f"https://open.larksuite.com/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query"
        res_sheets = requests.get(sheets_url, headers=headers, timeout=10)
        if res_sheets.status_code == 200:
            sheets_list = res_sheets.json().get("data", {}).get("sheets", [])
            for s in sheets_list:
                if target_sheet_title in s.get("title", ""):
                    sheet_id = s.get("sheet_id")
                    break
    except Exception as e:
        logging.warning(f"シートタブ一覧の取得失敗: {e}")

    # 当月タブが存在しない場合、Formatシート（Yb6Zwo）を複製して自動作成
    if not sheet_id:
        logging.info(f"✨ 当月シート [{target_sheet_title}] が存在しないため、Formatシートから複製します...")
        try:
            copy_url = f"https://open.larksuite.com/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/{DEFAULT_SHEET_ID}/copy"
            copy_body = {"destination_name": target_sheet_title}
            res_copy = requests.post(copy_url, headers=headers, json=copy_body, timeout=10)
            res_copy_json = res_copy.json()
            
            if res_copy_json.get("code") == 0:
                sheet_id = res_copy_json.get("data", {}).get("sheet", {}).get("sheet_id")
                logging.info(f"✅ Formatシートを複製して [{target_sheet_title}] (ID: {sheet_id}) を作成しました。")
            else:
                logging.error(f"シート作成失敗: {res_copy_json}")
                sheet_id = DEFAULT_SHEET_ID
        except Exception as e:
            logging.error(f"シート自動作成エラー: {e}")
            sheet_id = DEFAULT_SHEET_ID

    # F列（件名）を読み込んで最初の空行（データ開始行: 4行目〜）を特定
    read_url = f"https://open.larksuite.com/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{sheet_id}!F1:F200"
    target_row = 4
    try:
        res_read = requests.get(read_url, headers=headers, timeout=10)
        if res_read.status_code == 200:
            values = res_read.json().get("data", {}).get("valueRange", {}).get("values", [])
            for idx in range(3, len(values)):
                row_val = values[idx]
                if not row_val or not str(row_val[0]).strip():
                    target_row = idx + 1
                    break
            else:
                target_row = len(values) + 1 if len(values) >= 3 else 4
    except Exception as e:
        logging.warning(f"空行判定エラー: {e}")

    next_biz_day = get_next_business_day().strftime('%Y/%m/%d')
    write_url = f"https://open.larksuite.com/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values"

    for d in extracted_data:
        region = get_region_from_address(d['住所'])
        team = "ガスプラ課" if region == "関東" else "西日本"
        action = f"【{d['停止or復旧']}】"
        subject = f"{action}{d['物件名']} {d['部屋番号']}"

        # B列:作業者名(空白), C列:チーム, D列:内容, E列:物件区分, F列:件名, G列:作業日, H列:納入数量(1), I列:金額(35000), J列:消費税(3500)
        row_bj = ["", team, action, d['物件種別'], subject, next_biz_day, 1, 35000, 3500]

        body_bj = {
            "valueRange": {
                "range": f"{sheet_id}!B{target_row}:J{target_row}",
                "values": [row_bj]
            }
        }
        
        try:
            res_w = requests.put(write_url, headers=headers, json=body_bj, timeout=10)
            if d.get('備考'):
                body_k = {
                    "valueRange": {
                        "range": f"{sheet_id}!K{target_row}:K{target_row}",
                        "values": [[d['備考']]]
                    }
                }
                requests.put(write_url, headers=headers, json=body_k, timeout=10)
            
            logging.info(f"📝 Larkシート [{target_sheet_title}] (行{target_row}) に転記完了: {subject}")
            target_row += 1
        except Exception as e:
            logging.error(f"Larkシート書き込み失敗: {e}")

def send_combined_lark_report(success_list, failure_list):
    if not LARK_WEBHOOK_URL: return
    if not success_list and not failure_list: return

    now_str = jst_now().strftime('%Y-%m-%d %H:%M:%S')
    elements = []

    for item in success_list:
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**ステータス:** ✅ SUCCESS\n"
                    f"**詳細:** レジル停止作業 「{item['name']}」 BLASおよびLarkシートの登録が完了しました\n"
                    f"**地域:** {item['region']}\n"
                    f"**スイッチ:** {item.get('switch', 'あり')}\n"
                    f"**実行日時:** {now_str}"
                )
            }
        })

    if failure_list:
        if success_list: elements.append({"tag": "hr"})
        for name, reason in failure_list:
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**ステータス:** ❌ FAILURE\n"
                        f"**詳細:** {name} の登録に失敗しました\n"
                        f"**理由:** {reason}\n"
                        f"**実行日時:** {now_str}"
                    )
                }
            })

    header_template = "red" if failure_list else "orange"

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "🤖 Web自動化処理 SUCCESS" if not failure_list else "⚠️ Web自動化処理 REPORT"},
                "template": header_template
            },
            "elements": elements
        }
    }
    
    try:
        response = requests.post(LARK_WEBHOOK_URL, json=payload, timeout=10)
        response.raise_for_status()
    except Exception as e:
        logging.error(f"Lark通知送信エラー: {e}")

def get_next_business_day():
    next_day = datetime.date.today() + datetime.timedelta(days=1)
    try:
        import jpholiday
        while next_day.weekday() >= 5 or jpholiday.is_holiday(next_day):
            next_day += datetime.timedelta(days=1)
    except ImportError:
        while next_day.weekday() >= 5:
            next_day += datetime.timedelta(days=1)
    return next_day

def get_gmail_service():
    creds = None
    token_path = BASE_DIR / 'token.json'

    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except Exception as e:
            logging.warning(f"token.json の読み込みに失敗しました: {e}")

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            logging.warning(f"トークンの自動更新に失敗しました: {e}")
            creds = None

    if not creds or not creds.valid:
        raise RuntimeError("Gmail認証トークン(token.json)が無効または存在しません。GitHub Secretsを確認してください。")

    return build('gmail', 'v1', credentials=creds, cache_discovery=False)

def get_or_create_processed_label_id(service, label_name="処理済み"):
    try:
        results = service.users().labels().list(userId='me').execute()
        labels = results.get('labels', [])
        for label in labels:
            if label['name'] == label_name:
                return label['id']
        
        label_object = {
            'name': label_name,
            'labelListVisibility': 'labelShow',
            'messageListVisibility': 'show'
        }
        created_label = service.users().labels().create(userId='me', body=label_object).execute()
        return created_label['id']
    except Exception as e:
        return None

def add_processed_label(service, msg_ids, label_id):
    if not label_id: return
    valid_ids = [m_id for m_id in msg_ids if m_id]
    if not valid_ids: return
    try:
        service.users().messages().batchModify(
            userId='me', body={'ids': valid_ids, 'addLabelIds': [label_id]}
        ).execute()
        logging.info(f"🏷️ 処理済みラベルを付与しました (対象: {len(valid_ids)}件)")
    except Exception as e:
        logging.warning(f"ラベルの付与に失敗しました: {e}")

def get_email_body(payload):
    plain_text = ""
    html_text = ""

    def extract_parts(part_payload):
        nonlocal plain_text, html_text
        mime_type = part_payload.get('mimeType', '')
        if 'parts' in part_payload:
            for p in part_payload['parts']:
                extract_parts(p)
        else:
            data = part_payload.get('body', {}).get('data', '')
            if data:
                decoded = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
                if mime_type == 'text/plain' and not plain_text:
                    plain_text = decoded
                elif mime_type == 'text/html' and not html_text:
                    html_text = decoded

    extract_parts(payload)
    body = plain_text if plain_text else html_text
    return body.replace('=\r\n', ' ').replace('=\n', ' ')

def fetch_hennge_details(service, processed_label_id):
    url, subject_text = None, ""
    url_msg_id = None
    url_msg_timestamp = 0
    url_from = ""
    url_thread_id = ""
    target_region = None
    logging.info("Gmail APIに接続し、対象メールを検証中...")
    
    try:
        # 「電力停止」ラベルが付いていて「停止」キーワードを含み、「処理済み」でないメールを検索
        search_query = 'label:電力停止 停止 -label:処理済み'
        results_url = service.users().messages().list(userId='me', q=search_query, maxResults=50).execute()
        messages_url = results_url.get('messages', [])
        
        logging.info(f"--- 検索該当（未処理）メール件数: {len(messages_url)}件 ---")
        
        for idx, m in enumerate(messages_url, 1):
            time.sleep(0.1)
            msg = service.users().messages().get(userId='me', id=m['id']).execute()
            payload = msg['payload']
            headers = {h['name'].lower(): h['value'] for h in payload.get('headers', [])}
            subj = headers.get('subject', '（件名なし）')
            
            # 安全ガード: 件名に「停止」が含まれない問い合わせや復旧メール等は除外
            if '停止' not in subj:
                logging.info(f"  ⏭️ 件名に『停止』が含まれないためスキップ: {subj}")
                continue

            body = get_email_body(payload)
            clean_body = re.sub(r'<[^>]+>', ' ', body)
            clean_body = clean_body.replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ')
            
            url_match = re.search(r'(https://[a-zA-Z0-9.-]*transfer\.hennge\.com/[^\s"\'<>]+)', clean_body)

            if url_match and not url:
                url = url_match.group(1)
                url_msg_id = m['id']
                url_thread_id = msg.get('threadId', '')
                url_from = headers.get('from', '')
                subject_text = subj
                url_msg_timestamp = int(msg.get('internalDate', 0)) / 1000
                
                if "関西" in subject_text: target_region = "関西"
                elif "関東" in subject_text: target_region = "関東"
                
                logging.info(f"  👉 処理対象として決定: {url} (件名: {subj})")
                break

        if not url: return None, [], "", None

        url_dt = datetime.datetime.fromtimestamp(url_msg_timestamp)
        after_date = url_dt.strftime('%Y/%m/%d')
        before_date = (url_dt + datetime.timedelta(days=1)).strftime('%Y/%m/%d')
        
        search_query_pass = f'(パスワード OR Password) after:{after_date} before:{before_date}'
        results_pass = service.users().messages().list(userId='me', q=search_query_pass, maxResults=50).execute()
        messages_pass = results_pass.get('messages', [])
        
        candidates = []

        for p_idx, m in enumerate(messages_pass, 1):
            time.sleep(0.1)
            if m['id'] == url_msg_id: continue

            msg = service.users().messages().get(userId='me', id=m['id']).execute()
            p_thread_id = msg.get('threadId', '')
            p_label_ids = msg.get('labelIds', [])
            is_p_processed = processed_label_id in p_label_ids if processed_label_id else False
            
            payload = msg['payload']
            headers = {h['name'].lower(): h['value'] for h in payload.get('headers', [])}
            p_subject = headers.get('subject', '')
            p_date = headers.get('date', '')
            p_from = headers.get('from', '')
            p_timestamp = int(msg.get('internalDate', 0)) / 1000
            
            time_diff = abs(p_timestamp - url_msg_timestamp)
            if time_diff > 7200: continue

            body_pass = get_email_body(payload)
            clean_body_pass = re.sub(r'<[^>]+>', ' ', body_pass).replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ')
            
            patterns = [
                r'(?:ファイルダウンロードパスワード|ファイルパスワード|ダウンロードパスワード|パスワード|Password)[:：\s]+([\x21-\x7e]+)'
            ]
            
            cand = None
            for pat in patterns:
                for match_item in re.finditer(pat, clean_body_pass, re.IGNORECASE):
                    c_val = match_item.group(1).strip().rstrip('。、.）」】')
                    if not c_val.isascii() or len(c_val) != 12: continue
                    if c_val.lower() in ["password", "japanese", "english", "hennge", "transfer", "http", "https", "mailto", "url"]: continue
                    if any(c_val.startswith(prefix) for prefix in ["http", "mailto"]): continue
                    cand = c_val
                    break
                if cand: break

            if cand:
                score = time_diff
                if not is_p_processed: score -= 1000
                if url_from and p_from and (url_from in p_from or p_from in url_from): score -= 500
                if url_thread_id and p_thread_id == url_thread_id: score -= 100000
                if target_region and target_region in p_subject: score -= 300
                candidates.append((score, cand, p_subject, p_date, m['id'], time_diff))

        candidates.sort(key=lambda x: x[0])

        unique_candidates = []
        seen_pw = set()
        for c_item in candidates:
            if c_item[1] not in seen_pw:
                seen_pw.add(c_item[1])
                unique_candidates.append(c_item)

    except Exception as e:
        logging.error(f"Gmail API 取得エラー: {e}")
        
    return url, unique_candidates, subject_text, url_msg_id

def fetch_verification_code(service, start_timestamp, processed_label_id):
    for attempt in range(15):
        time.sleep(3)
        try:
            results = service.users().messages().list(
                userId='me', q='認証コード OR HENNGE OR 確認コード', maxResults=5
            ).execute()
            
            messages = results.get('messages', [])
            for m in messages:
                msg = service.users().messages().get(userId='me', id=m['id']).execute()
                msg_timestamp = int(msg.get('internalDate', 0)) / 1000
                
                if msg_timestamp >= start_timestamp - 10:
                    body = get_email_body(msg['payload'])
                    code_match = re.search(r'(?:認証コード|確認コード|code)[:：\s\n]+([A-Za-z0-9]{4,8})', body, re.IGNORECASE)
                    if not code_match:
                        code_match = re.search(r'\b(\d{6})\b', body)
                        
                    if code_match:
                        return code_match.group(1).strip(), m['id']
        except: pass
    return None, None

def download_from_hennge(url, password_candidates, service, processed_label_id):
    logging.info(f"HENNGEからファイルのダウンロードを開始します (URL: {url})")
    my_email = service.users().getProfile(userId='me').execute().get('emailAddress', '')
    auth_msg_id = None
    successful_pass_msg_id = None

    for f in glob.glob(str(DOWNLOAD_DIR / '*')): 
        try: os.remove(f)
        except Exception as e: logging.warning(f"削除失敗: {e}")

    options = get_chrome_options()
    driver = None
    try:
        service_chrome = ChromeService(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service_chrome, options=options)
        wait = WebDriverWait(driver, 20)

        driver.get(url)
        time.sleep(2)

        page_src = driver.page_source
        if "アクセスが拒否されました" in page_src or "有効期限が切れている" in page_src or "リンクが存在しません" in page_src:
            logging.error(f"❌ リンク無効: {url}")
            return None, None, None

        pass_input = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@type='password']")))
        
        successful_password = None
        for score, cand_password, p_subj, p_date, p_msg_id, t_diff in password_candidates:
            pass_input.clear()
            pass_input.send_keys(cand_password)
            time.sleep(0.5)

            try:
                submit_btn = driver.find_element(By.XPATH, "//button[@type='submit']")
                driver.execute_script("arguments[0].click();", submit_btn)
            except Exception:
                pass_input.send_keys(Keys.RETURN)
                
            time.sleep(1.5)

            try:
                email_input = WebDriverWait(driver, 3).until(
                    EC.presence_of_element_located((By.XPATH, "//input[@type='email' or contains(@placeholder, 'メールアドレス')]"))
                )
                logging.info(f"✅ パスワード認証成功: {cand_password}")
                successful_password = cand_password
                successful_pass_msg_id = p_msg_id
                break
            except Exception:
                logging.warning(f"⚠️ パスワード [{cand_password}] 拒否")

        if not successful_password:
            return None, None, None

        email_input.clear()
        email_input.send_keys(my_email)
        time.sleep(1)

        request_timestamp = time.time()
        send_code_btn = wait.until(EC.presence_of_element_located((By.XPATH, "//button[@type='submit' or contains(., '認証コード') or contains(., '送信')]")))
        driver.execute_script("arguments[0].click();", send_code_btn)

        auth_code, auth_msg_id = fetch_verification_code(service, request_timestamp, processed_label_id)
        if not auth_code: raise Exception("認証コードが取得できませんでした。")

        code_input = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@type='text' or @type='number' or contains(@placeholder, 'コード')]")))
        code_input.clear()
        code_input.send_keys(auth_code)
        time.sleep(1)

        verify_btn = wait.until(EC.presence_of_element_located((By.XPATH, "//button[@type='submit' or contains(., '次へ') or contains(., '認証')]")))
        driver.execute_script("arguments[0].click();", verify_btn)

        download_btn = wait.until(EC.presence_of_element_located((By.XPATH, "//button[contains(., 'ダウンロード') or contains(., 'Download')]")))
        time.sleep(2)
        driver.execute_script("arguments[0].click();", download_btn)

        wait_time = 0
        while wait_time < 60:
            time.sleep(2)
            wait_time += 2
            files = os.listdir(DOWNLOAD_DIR)
            if files and not any(f.endswith('.crdownload') or f.endswith('.tmp') for f in files):
                break

        downloaded_files = glob.glob(str(DOWNLOAD_DIR / '*'))
        if downloaded_files:
            latest_file = max(downloaded_files, key=os.path.getctime)
            return latest_file, successful_pass_msg_id, auth_msg_id
        else:
            raise Exception("ダウンロードファイルが存在しません。")

    except Exception as e:
        logging.error(f"HENNGEダウンロード失敗: {e}")
        return None, None, None
    finally:
        if driver: driver.quit()

def process_pdf_data(pdf_path):
    extracted_data = []
    stop_count = 0
    recovery_count = 0
    current_type = 'NP' 
    current_action = '停止'
    col_idx = {'MID': 0, '物件名': 1, '部屋番号': 2, '住所': 5, '管理人': 6, 'AL': 7, '備考': 8}

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    row_clean = [str(c).strip().replace('\n', '') if c is not None else '' for c in row]
                    row_str = "".join(row_clean)
                    if not row_str: continue

                    if 'レジル' in row_str: current_type = 'レジル'
                    elif any(k in row_str for k in ['旧オリックス', '旧Eハウス', 'NP']): current_type = 'NP'
                    
                    if any(k in row_str for k in ['復旧', '復電']):
                        current_action = '復旧'
                    elif any(k in row_str for k in ['停止', '切断']):
                        current_action = '停止'

                    if '物件名' in row_str and ('部屋番号' in row_str or 'ＭＩＤ' in row_str or 'MID' in row_str):
                        for i, val in enumerate(row_clean):
                            if 'MID' in val or 'ＭＩＤ' in val: col_idx['MID'] = i
                            elif '物件名' in val: col_idx['物件名'] = i
                            elif '部屋番号' in val: col_idx['部屋番号'] = i
                            elif '住所' in val: col_idx['住所'] = i
                            elif '管理' in val: col_idx['管理人'] = i
                            elif 'AL' in val or 'ｵｰﾄﾛｯｸ' in val or '有無' in val: col_idx['AL'] = i
                            elif '備考' in val: col_idx['備考'] = i
                        continue

                    if len(row_clean) <= max(col_idx['物件名'], col_idx['部屋番号']): continue

                    mid_val = row_clean[col_idx['MID']] if len(row_clean) > col_idx['MID'] else ''
                    obj_name = row_clean[col_idx['物件名']] if len(row_clean) > col_idx['物件名'] else ''
                    room_val = row_clean[col_idx['部屋番号']] if len(row_clean) > col_idx['部屋番号'] else ''

                    if not obj_name or not room_val or obj_name.lower() == 'nan': continue

                    address = row_clean[col_idx['住所']] if len(row_clean) > col_idx['住所'] else ''
                    kanri_val = row_clean[col_idx['管理人']] if len(row_clean) > col_idx['管理人'] else ''
                    al_raw = row_clean[col_idx['AL']] if len(row_clean) > col_idx['AL'] else ''
                    remark_val = row_clean[col_idx['備考']] if len(row_clean) > col_idx['備考'] else ''

                    processed_room = re.sub(r'\.0$', '', room_val)
                    address = re.sub(r'\.0$', '', address)

                    if '文書投函' in kanri_val or '文書投函' in remark_val:
                        action_status = '文書投函'
                    elif '復旧' in remark_val or '復旧' in kanri_val:
                        action_status = '復旧'
                    else:
                        action_status = current_action

                    # 復旧データはスキップ
                    if action_status == '復旧':
                        recovery_count += 1
                        continue

                    stop_count += 1
                    al_status = '' if al_raw == '' else ('有' if any(k in al_raw for k in ['放', '有', 'あり']) else '無')

                    extracted_data.append({
                        '停止or復旧': action_status,
                        '物件種別': current_type,
                        '物件名': obj_name,
                        '部屋番号': processed_room,
                        '住所': address,
                        'ＭＩＤ': mid_val,
                        '管理人': kanri_val,
                        'AL': al_status,
                        '備考': remark_val
                    })

    if not extracted_data:
        return "対象データなし", 0, stop_count, recovery_count, "", None, []
        
    return create_output_csv(extracted_data, stop_count, recovery_count)

def process_excel_data(excel_path):
    full_sheet = pd.read_excel(excel_path, sheet_name='リスト', header=None).fillna('')
    header_idx = None
    for i, row in full_sheet.iterrows():
        row_str_list = [str(x).strip() for x in row.values]
        if any("ＭＩＤ" in x or "MID" in x for x in row_str_list) and any("物件名" in x for x in row_str_list):
            header_idx = i
            break
            
    if header_idx is None: raise ValueError("ヘッダーが見つかりませんでした。")

    cols = [str(x).strip() for x in full_sheet.iloc[header_idx]]
    def get_c(name): 
        for idx, col_name in enumerate(cols):
            if name in col_name: return idx
        return -1

    extracted_data = []
    current_action = '停止'
    current_type = 'NP'
    suspension_keywords = {'＜レジル＞': 'レジル', '＜旧オリックス＞': 'NP', '＜旧Eハウス＞': 'NP', '＜旧NTT-AE＞': 'レジル'}
    stop_count = 0
    recovery_count = 0

    for i in range(header_idx + 1, len(full_sheet)):
        row = full_sheet.iloc[i]
        row_str = "".join([str(v) for v in row.values])
        
        if any(k in row_str for k in ['＜復旧＞', '復旧', '復電']):
            current_action = '復旧'
        else:
            for kw, t in suspension_keywords.items():
                if kw in row_str:
                    current_action = '停止'
                    current_type = t
                    break
                    
        if 'レジル' in row_str and '＜' not in row_str: current_type = 'レジル'
        if '旧オリックス' in row_str or '旧Eハウス' in row_str: current_type = 'NP'

        mid_col, obj_col, room_col = get_c('ＭＩＤ'), get_c('物件名'), get_c('部屋番号')
        if mid_col == -1 or obj_col == -1 or room_col == -1: continue
        
        mid_val = str(row[mid_col]).strip()
        obj_name = str(row[obj_col]).strip()
        room_val = str(row[room_col]).strip()

        mid_val = '' if mid_val.lower() == 'nan' else mid_val
        obj_name = '' if obj_name.lower() == 'nan' else obj_name
        room_val = '' if room_val.lower() == 'nan' else room_val

        if not obj_name or not room_val: continue

        processed_room = re.sub(r'\.0$', '', room_val)
        pref_col, addr_col = get_c('都道府県'), get_c('物件住所')
        pref = str(row[pref_col]).strip() if pref_col != -1 else ''
        addr = str(row[addr_col]).strip() if addr_col != -1 else ''
        full_address = re.sub(r'\.0$', '', pref) + re.sub(r'\.0$', '', addr)

        al_col = get_c('AL')
        al_raw = str(row[al_col]).strip() if al_col != -1 else ''
        al_status = '' if (al_raw == '' or al_raw.lower() == 'nan') else ('有' if any(k in al_raw for k in ['放', '有', 'あり']) else '無')
        
        remark_col = get_c('備考')
        remark_val = str(row[remark_col]).strip() if remark_col != -1 else ''
        remark_val = '' if remark_val.lower() == 'nan' else remark_val
        
        # 判定：復旧データは除外する
        action_status = '文書投函' if '文書投函' in remark_val else current_action
        kanri_col = get_c('管理員') if get_c('管理員') != -1 else get_c('管理')
        kanri_val = str(row[kanri_col]).strip() if kanri_col != -1 else ''
        
        if '復旧' in remark_val or '復旧' in kanri_val:
            action_status = '復旧'
            
        # 復旧データはスキップ
        if action_status == '復旧':
            recovery_count += 1
            continue
            
        stop_count += 1

        extracted_data.append({
            '停止or復旧': action_status,
            '物件種別': current_type,
            '物件名': obj_name,
            '部屋番号': processed_room,
            '住所': full_address,
            'ＭＩＤ': mid_val,
            '管理人': kanri_val,
            'AL': al_status,
            '備考': remark_val.replace('\n', ' ')
        })

    if not extracted_data:
        return "対象データなし", 0, stop_count, recovery_count, "", None, []
        
    return create_output_csv(extracted_data, stop_count, recovery_count)

def create_output_csv(extracted_data, stop_count, recovery_count):
    REMARK_COL_1 = "\u3000" * 5 + "備考" + "\u3000" * 5
    REMARK_COL_2 = "レジル様記入備考" + " " * 52

    ALL_HEADERS = [
        'BLAS_データ管理番号', 'BLAS_担当会社', '停止or復旧', '物件種別', '物件名', 
        '部屋番号※番号のみ入力', '物件住所', '工事会社', '作業者', '作業日', 
        'メーター番号', '腕章は携帯しているか', '管理人駐在時間', '入館方法', 'オートロックの有無', 
        'オートロック番号', 'スキルレススイッチの有無', '文書投函場所', '入館時刻', '停止・復電完了時刻', 
        '退館時刻', 'ステータス', '脚立の必要有無', '脚立必要の場合：メーターの高さや必要な脚立の高さ', 
        'パネルの有無', 'パネルありの場合：パネルの大きさやビス数、位置高さ', '鍵の必要有無', '鍵が必要な場合：必要な鍵', 
        '【退館前】撮影写真に不備はないか', '【退館前】ゴミ・忘れものはしていないか', '駐車場', 
        REMARK_COL_1, REMARK_COL_2, 
        'BLAS_ゴミ箱', 'BLAS_データ未完了', 'BLAS_画像未完了', 'BLAS_親データ管理番号', 
        '【協力会社のみ登録】腕章は携帯しているか', '【作業前】盤全景写真', '【作業前】', '【作業中】', 
        '【作業後】', '【文書投函写真】', '予備1', '予備2', '予備3', '予備4', '予備5'
    ]

    final_rows = []
    for idx, d in enumerate(extracted_data):
        row_dict = {h: '' for h in ALL_HEADERS}
        row_dict['BLAS_データ管理番号'] = idx + 1
        row_dict['BLAS_担当会社'] = FIXED_COMPANY_NAME
        row_dict['停止or復旧'] = d['停止or復旧']
        row_dict['物件種別'] = d['物件種別']
        row_dict['物件名'] = d['物件名']
        row_dict['部屋番号※番号のみ入力'] = d['部屋番号']
        row_dict['物件住所'] = d['住所']
        row_dict['工事会社'] = FIXED_COMPANY_NAME
        row_dict['作業日'] = get_next_business_day().strftime('%Y/%m/%d')
        
        if d['物件種別'] == 'レジル':
            row_dict['メーター番号'] = f"{d['ＭＩＤ']}-{d['部屋番号']}"
        else:
            row_dict['メーター番号'] = d['ＭＩＤ']
            
        row_dict['管理人駐在時間'] = d['管理人']
        row_dict['オートロックの有無'] = d['AL']
        row_dict['ステータス'] = '出向前'
        
        for c in ['腕章は携帯しているか', '入館方法', 'スキルレススイッチの有無', '脚立の必要有無', 'パネルの有無', '鍵の必要有無', '【退館前】撮影写真に不備はないか', '【退館前】ゴミ・忘れものはしていないか']:
            row_dict[c] = '選択してください'
        
        row_dict[REMARK_COL_2] = d['備考']
        final_rows.append(row_dict)

    df_final = pd.DataFrame(final_rows, columns=ALL_HEADERS)
    unique_csv_path = OUTPUT_CSV_PATH.parent / f"output_{datetime.datetime.now().strftime('%H%M%S')}.csv"
    df_final.to_csv(unique_csv_path, index=False, encoding='utf-8-sig')
    
    first_address = extracted_data[0]['住所'] if extracted_data else ""
    return extracted_data[0]['物件名'], len(df_final), stop_count, recovery_count, first_address, unique_csv_path, extracted_data

def run_automation(prop_name, count, stop_count, recovery_count, first_address, csv_path):
    options = get_chrome_options()
    driver = None
    try:
        service = ChromeService(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        wait = WebDriverWait(driver, 20)
        
        driver.get("https://www.basis-service.com/blas70/users/login")
        wait.until(EC.presence_of_element_located((By.NAME, "username"))).send_keys(BASIS_USERNAME)
        driver.find_element(By.NAME, "password").send_keys(BASIS_PASSWORD)
        driver.find_element(By.XPATH, "//input[@type='submit']").click()
        time.sleep(2)
        driver.get("https://www.basis-service.com/blas70/items")
        
        wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "select2-selection__arrow"))).click()
        search_field = wait.until(EC.presence_of_element_located((By.CLASS_NAME, "select2-search__field")))
        search_field.send_keys("【レジル】停止・復電業務")
        time.sleep(2)
        wait.until(EC.element_to_be_clickable((By.XPATH, "//li[contains(text(), '【レジル】停止・復電業務')]"))).click()
        
        wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(.,'CSVインポート')]"))).click()
        chk = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@type='radio' and @value='1']")))
        driver.execute_script("arguments[0].click();", chk)
        
        driver.find_element(By.XPATH, "//input[@type='file']").send_keys(os.path.abspath(csv_path))
        wait.until(EC.element_to_be_clickable((By.ID, "csv_import_btn"))).click()
        
        try:
            WebDriverWait(driver, 5).until(EC.alert_is_present())
            driver.switch_to.alert.accept()
        except: pass
        
        time.sleep(10)
        shutil.move(str(csv_path), PROCESSED_DIR / f"output_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}.csv")

    except Exception as e:
        logging.error(f"実行エラー: {e}")
        raise
    finally:
        if driver: driver.quit()

if __name__ == '__main__':
    logging.info("=== 自動処理を開始します ===")
    gmail_service = get_gmail_service()
    processed_label_id = get_or_create_processed_label_id(gmail_service, "処理済み")
    
    target_url, password_candidates, subject_text, url_msg_id = fetch_hennge_details(gmail_service, processed_label_id)
    
    if not target_url or not password_candidates:
        logging.info("有効な対象メールが見つかりませんでした。処理を終了します。")
        exit(0)
        
    downloaded_file_path, pass_msg_id, auth_msg_id = download_from_hennge(target_url, password_candidates, gmail_service, processed_label_id)
    if not downloaded_file_path or not os.path.exists(downloaded_file_path):
        logging.error("ダウンロード失敗。終了します。")
        exit(1)

    if downloaded_file_path.lower().endswith('.zip'):
        with zipfile.ZipFile(downloaded_file_path, 'r') as zip_ref:
            zip_ref.extractall(DOWNLOAD_DIR)
        os.remove(downloaded_file_path)

    target_files = glob.glob(str(DOWNLOAD_DIR / '*.pdf')) + glob.glob(str(DOWNLOAD_DIR / '*.xlsx')) + glob.glob(str(DOWNLOAD_DIR / '*.xls'))
    if not target_files:
        logging.error("❌ PDF/Excelファイルが見つかりません。")
        exit(1)

    success_items = []
    failure_items = []

    for file_path in target_files:
        file_name = os.path.basename(file_path)
        try:
            if file_path.lower().endswith('.pdf'):
                p_name, p_count, s_count, r_count, p_addr, unique_csv_path, ext_data = process_pdf_data(file_path)
            else:
                p_name, p_count, s_count, r_count, p_addr, unique_csv_path, ext_data = process_excel_data(file_path)

            # ファイル内に「復旧」しかなく抽出データが0件だった場合はスキップ
            if p_count == 0:
                logging.info(f"⏭️ {file_name} には「停止」対象データがありませんでした（復旧データ {r_count}件 をスキップ）。")
                os.remove(file_path)
                continue

            if TEST_CSV_ONLY:
                continue

            # 1. BLASへの自動登録
            run_automation(p_name, p_count, s_count, r_count, p_addr, unique_csv_path)
            
            # 2. Larkスプレッドシートへの自動転記（Formatシートから自動複製）
            write_to_lark_sheet(ext_data)

            os.remove(file_path)
            
            region_name = get_region_from_address(p_addr)
            success_items.append({
                "name": f"{p_name} 外 ({p_count}件)",
                "region": region_name,
                "switch": "あり"
            })
            
        except Exception as e:
            logging.error(f"❌ エラーが発生しました ({file_name}): {e}")
            failure_items.append((file_name, str(e)))

    # 3. Larkカード通知の送信
    send_combined_lark_report(success_items, failure_items)

    # 全処理成功時のみ「処理済み」ラベルを付与
    if not failure_items and success_items:
        add_processed_label(gmail_service, [url_msg_id, pass_msg_id, auth_msg_id], processed_label_id)

    logging.info("=== 全処理終了 ===")
