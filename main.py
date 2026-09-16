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
import sys
import requests
from pathlib import Path
import re
import glob
import base64
import zipfile

# PDF解析用ライブラリ
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextContainer

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
DEFAULT_SHEET_ID = "Yb6Zwo"

FIXED_COMPANY_NAME = "ベイシス株式会社 IoT推進部"

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_CSV_PATH = BASE_DIR / "output.csv"
PROCESSED_DIR = BASE_DIR / "processed"
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

KANTO_PREFS = ['東京都', '神奈川県', '埼玉県', '千葉県', '茨城県', '栃木県', '群馬県']
KANSAI_PREFS = ['大阪府', '京都府', '兵庫県', '奈良県', '滋賀県', '和歌山県']

KANSAI_CITIES = ['大阪市', '京都市', '神戸市', '堺市', '奈良市', '和歌山市', '大津市', '東大阪市', '西宮市', '尼崎市', '豊中市', '吹田市', '枚方市', '高槻市', '茨木市', '八尾市', '寝屋川市', '姫路市', '明石市', '加古川市', '宝塚市', '伊丹市', '川西市', '精華', '木津川', '宇治市', '城陽市', '生駒市']
KANTO_CITIES = ['横浜市', '川崎市', 'さいたま市', '千葉市', '相模原市', '船橋市', '川口市', '新宿区', '世田谷区', '港区', '渋谷区', '中央区', '千代田区', '品川区', '目黒区', '大田区', '杉並区', '練馬区', '八王子市', '町田市', '藤沢市', '横須賀市', '平塚市', '茅ヶ崎市', '大和市', '厚木市', '所沢市', '川越市', '越谷市', '草加市', '市川市', '松戸市', '柏市', '市原市', '宇都宮市', '前橋市', '高崎市', '水戸市']

TEST_DOWNLOAD_ONLY = False
TEST_CSV_ONLY = False

LOG_DIR = BASE_DIR / "log"
LOG_DIR.mkdir(parents=True, exist_ok=True)
log_filename = f"selenium_log_{datetime.datetime.now().strftime('%Y%m%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / log_filename, encoding='utf-8'), 
        logging.StreamHandler(sys.stdout)
    ]
)

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

def log_flush(msg, level=logging.INFO):
    logging.log(level, msg)
    sys.stdout.flush()

def get_chrome_options():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
    prefs = {
        "download.default_directory": str(DOWNLOAD_DIR),
        "download.prompt_for_download": False,
        "directory_upgrade": True,
        "safebrowsing.enabled": True,
        "safebrowsing.disable_download_protection": True
    }
    options.add_experimental_option("prefs", prefs)
    return options

def get_region_from_info(filename, address):
    filename_lower = filename.lower() if filename else ""
    if "関西" in filename_lower or "西日本" in filename_lower:
        return "関西"
    elif "関東" in filename_lower or "東日本" in filename_lower:
        return "関東"

    if not address: return "不明"
    match = re.search(r'([一-龠]{2,3}[都道府県])', address)
    if match:
        prefecture = match.group(1).strip()
        if prefecture in KANTO_PREFS: return "関東"
        elif prefecture in KANSAI_PREFS: return "関西"

    if any(city in address for city in KANSAI_CITIES): return "関西"
    if any(city in address for city in KANTO_CITIES): return "関東"

    if match: return f"その他（{match.group(1).strip()}）"
    return "不明"

def get_lark_tenant_access_token():
    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    headers = {"Content-Type": "application/json; charset=utf-8"}
    body = {"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET}
    try:
        res = requests.post(url, headers=headers, json=body, timeout=10)
        res_json = res.json()
        if res_json.get("code") == 0:
            return res_json.get("tenant_access_token")
        log_flush(f"Lark API Token取得失敗: {res_json}", logging.ERROR)
    except Exception as e:
        log_flush(f"Lark API接続エラー: {e}", logging.ERROR)
    return None

def get_spreadsheet_token(tenant_token):
    return LARK_WIKI_TOKEN

def write_to_lark_sheet(extracted_data, detected_region):
    tenant_token = get_lark_tenant_access_token()
    if not tenant_token:
        log_flush("Lark APIトークンが取得できなかったため、シート書き込みをスキップします。", logging.WARNING)
        return

    spreadsheet_token = get_spreadsheet_token(tenant_token)
    headers = {
        "Authorization": f"Bearer {tenant_token}",
        "Content-Type": "application/json; charset=utf-8"
    }

    now = jst_now()
    target_sheet_title = f"{now.year}年{now.month}月"
    sheet_id = None

    try:
        sheets_url = f"https://open.larksuite.com/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query"
        res_sheets = requests.get(sheets_url, headers=headers, timeout=10)
        res_data = res_sheets.json()
        if res_sheets.status_code == 200 and res_data.get("code") == 0:
            sheets_list = res_data.get("data", {}).get("sheets", [])
            for s in sheets_list:
                if target_sheet_title in s.get("title", ""):
                    sheet_id = s.get("sheet_id")
                    break
    except Exception as e:
        log_flush(f"シートタブ一覧の取得失敗: {e}", logging.WARNING)

    if not sheet_id:
        try:
            copy_url = f"https://open.larksuite.com/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/{DEFAULT_SHEET_ID}/copy"
            copy_body = {"destination_name": target_sheet_title}
            res_copy = requests.post(copy_url, headers=headers, json=copy_body, timeout=10)
            res_copy_json = res_copy.json()
            if res_copy_json.get("code") == 0:
                sheet_id = res_copy_json.get("data", {}).get("sheet", {}).get("sheet_id")
            else:
                sheet_id = DEFAULT_SHEET_ID
        except Exception as e:
            sheet_id = DEFAULT_SHEET_ID

    read_url = f"https://open.larksuite.com/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{sheet_id}!B1:B200"
    target_row = 4
    try:
        res_read = requests.get(read_url, headers=headers, timeout=10)
        res_read_data = res_read.json()
        if res_read.status_code == 200 and res_read_data.get("code") == 0:
            values = res_read_data.get("data", {}).get("valueRange", {}).get("values", [])
            for idx in range(3, len(values)):
                row_val = values[idx]
                if not row_val or not str(row_val[0]).strip():
                    target_row = idx + 1
                    break
            else:
                target_row = len(values) + 1 if len(values) >= 3 else 4
    except Exception as e:
        log_flush(f"空行判定エラー: {e}", logging.WARNING)

    next_biz_day = get_next_business_day().strftime('%Y/%m/%d')
    write_url = f"https://open.larksuite.com/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values"

    for d in extracted_data:
        team = "ガスプラ課" if detected_region == "関東" else "西日本"
        action = f"【{d['停止or復旧']}】"
        subject = f"{action}{d['物件名']} {d['部屋番号']}"

        row_bj = ["", team, action, d['物件種別'], subject, next_biz_day, 1, 35000, 3500]

        body_bj = {
            "valueRange": {
                "range": f"{sheet_id}!B{target_row}:J{target_row}",
                "values": [row_bj]
            }
        }
        
        try:
            requests.put(write_url, headers=headers, json=body_bj, timeout=10)
            if d.get('備考'):
                body_k = {
                    "valueRange": {
                        "range": f"{sheet_id}!K{target_row}:K{target_row}",
                        "values": [[d['備考']]]
                    }
                }
                requests.put(write_url, headers=headers, json=body_k, timeout=10)
            log_flush(f"📝 Larkシート [{target_sheet_title}] (行{target_row}) に転記完了: {subject}")
            target_row += 1
        except Exception as e:
            log_flush(f"Larkシート書き込み例外: {e}", logging.ERROR)

def send_combined_lark_report(success_list, failure_list):
    if not LARK_WEBHOOK_URL or (not success_list and not failure_list): return
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

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "🤖 Web自動化処理 SUCCESS" if not failure_list else "⚠️ Web自動化処理 REPORT"},
                "template": "green" if not failure_list else "red"
            },
            "elements": elements
        }
    }
    try:
        requests.post(LARK_WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        log_flush(f"Lark通知送信エラー: {e}", logging.ERROR)

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
        try: creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except Exception: pass
    if creds and creds.expired and creds.refresh_token:
        try: creds.refresh(Request())
        except Exception: creds = None
    if not creds or not creds.valid:
        raise RuntimeError("Gmail認証トークン(token.json)が無効または存在しません。")
    return build('gmail', 'v1', credentials=creds, cache_discovery=False)

def get_or_create_processed_label_id(service, label_name="処理済み"):
    try:
        results = service.users().labels().list(userId='me').execute()
        for label in results.get('labels', []):
            if label['name'] == label_name: return label['id']
        return service.users().labels().create(userId='me', body={'name': label_name}).execute()['id']
    except Exception: return None

def add_processed_label(service, msg_ids, label_id):
    if not label_id: return
    valid_ids = [m_id for m_id in msg_ids if m_id]
    if not valid_ids: return
    try:
        service.users().messages().batchModify(userId='me', body={'ids': valid_ids, 'addLabelIds': [label_id]}).execute()
        log_flush(f"🏷️ 処理済みラベルを付与しました (対象: {len(valid_ids)}件)")
    except Exception as e: log_flush(f"ラベル付与失敗: {e}", logging.WARNING)

def get_email_body(payload):
    plain_text, html_text = "", ""
    def extract_parts(part_payload):
        nonlocal plain_text, html_text
        mime_type = part_payload.get('mimeType', '')
        if 'parts' in part_payload:
            for p in part_payload['parts']: extract_parts(p)
        else:
            data = part_payload.get('body', {}).get('data', '')
            if data:
                decoded = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
                if mime_type == 'text/plain' and not plain_text: plain_text = decoded
                elif mime_type == 'text/html' and not html_text: html_text = decoded
    extract_parts(payload)
    body = plain_text if plain_text else html_text
    return body.replace('=\r\n', ' ').replace('=\n', ' ')

def fetch_hennge_details(service, processed_label_id):
    url, subject_text = None, ""
    url_msg_id, url_msg_timestamp, url_from, url_thread_id, target_region = None, 0, "", "", None
    log_flush("Gmail APIに接続し、対象メールを検索中...")
    try:
        search_query = 'label:電力停止 停止 -label:処理済み'
        results_url = service.users().messages().list(userId='me', q=search_query, maxResults=50).execute()
        messages_url = results_url.get('messages', [])
        
        log_flush(f"🔍 未処理メール検索件数: {len(messages_url)}件")

        for m in messages_url:
            msg = service.users().messages().get(userId='me', id=m['id']).execute()
            headers = {h['name'].lower(): h['value'] for h in msg['payload'].get('headers', [])}
            subj = headers.get('subject', '')
            if '停止' not in subj: continue

            body = get_email_body(msg['payload'])
            clean_body = re.sub(r'<[^>]+>', ' ', body).replace('\r\n', ' ').replace('\n', ' ')
            url_match = re.search(r'(https://[a-zA-Z0-9.-]*transfer\.hennge\.com/[^\s"\'<>]+)', clean_body)

            if url_match and not url:
                raw_extracted_url = url_match.group(1)
                url = raw_extracted_url.rstrip('。、.）」】)\] \t\r\n').rstrip('.')
                url_msg_id = m['id']
                url_thread_id = msg.get('threadId', '')
                url_from = headers.get('from', '')
                subject_text = subj
                url_msg_timestamp = int(msg.get('internalDate', 0)) / 1000
                if "関西" in subject_text: target_region = "関西"
                elif "関東" in subject_text: target_region = "関東"
                log_flush(f"📧 対象メール発見 - 件名: {subj}")
                break

        if not url:
            log_flush("❌ 対象のHENNGE URLを含む未処理メールが見つかりませんでした。")
            return None, [], "", None

        log_flush(f"🔗 取得したダウンロードURL: {url}")

        url_dt = datetime.datetime.fromtimestamp(url_msg_timestamp)
        after_date = url_dt.strftime('%Y/%m/%d')
        before_date = (url_dt + datetime.timedelta(days=1)).strftime('%Y/%m/%d')
        
        results_pass = service.users().messages().list(userId='me', q=f'(パスワード OR Password) after:{after_date} before:{before_date}', maxResults=50).execute()
        candidates = []

        for m in results_pass.get('messages', []):
            msg = service.users().messages().get(userId='me', id=m['id']).execute()
            headers = {h['name'].lower(): h['value'] for h in msg['payload'].get('headers', [])}
            p_timestamp = int(msg.get('internalDate', 0)) / 1000
            time_diff = abs(p_timestamp - url_msg_timestamp)
            if time_diff > 7200 and m['id'] != url_msg_id: continue

            clean_body_pass = re.sub(r'<[^>]+>', ' ', get_email_body(msg['payload'])).replace('\r\n', '\n').replace('\r', '\n')
            
            pat = r'(?:ファイルダウンロードパスワード|ファイルパスワード|ダウンロードパスワード|パスワード|Password)[:：]\s*\n?\s*([^\s]{12})'
            
            for match_item in re.finditer(pat, clean_body_pass, re.IGNORECASE):
                c_val = match_item.group(1).strip()
                if not any(w in c_val.lower() for w in ["password", "japanese", "english", "hennge", "transfer", "http", "https"]):
                    candidates.append((time_diff, c_val, headers.get('subject', ''), headers.get('date', ''), m['id'], time_diff))

        candidates.sort(key=lambda x: x[0])
        unique_candidates = []
        seen_pw = set()
        for c_item in candidates:
            if c_item[1] not in seen_pw:
                seen_pw.add(c_item[1])
                unique_candidates.append(c_item)

        log_flush(f"🔑 抽出したパスワード候補 ({len(unique_candidates)}件): {[c[1] for c in unique_candidates]}")
        return url, unique_candidates, subject_text, url_msg_id

    except Exception as e:
        log_flush(f"Gmail API 取得エラー: {e}", logging.ERROR)
        return None, [], "", None

def fetch_verification_code(service, start_timestamp, processed_label_id):
    for _ in range(15):
        time.sleep(3)
        try:
            results = service.users().messages().list(userId='me', q='認証コード OR HENNGE OR 確認コード', maxResults=5).execute()
            for m in results.get('messages', []):
                msg = service.users().messages().get(userId='me', id=m['id']).execute()
                if int(msg.get('internalDate', 0)) / 1000 >= start_timestamp - 10:
                    clean_body = re.sub(r'<[^>]+>', ' ', get_email_body(msg['payload'])).replace('\r\n', ' ').replace('\n', ' ')
                    code_match = re.search(r'\b(\d{6})\b', clean_body)
                    if code_match: return code_match.group(1).strip(), m['id']
        except Exception: pass
    return None, None

def download_from_hennge(url, password_candidates, service, processed_label_id):
    log_flush(f"🌐 HENNGEへアクセス開始 URL: {url}")
    my_email = service.users().getProfile(userId='me').execute().get('emailAddress', '')
    
    for f in glob.glob(str(DOWNLOAD_DIR / '*')): 
        try: os.remove(f)
        except Exception: pass

    driver = None
    try:
        service_chrome = ChromeService(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service_chrome, options=get_chrome_options())
        wait = WebDriverWait(driver, 20)
        driver.get(url)
        time.sleep(3)

        page_source = driver.page_source
        if "存在しません" in page_source or "見つかりません" in page_source or "Expired" in page_source or "拒否され" in page_source:
            log_flush(f"❌ 画面エラー検知（アクセス拒否/リンク切れ/有効期限切れ）", logging.ERROR)
            return None, None, None

        # ==========================================
        # パターンA: すでに認証完了済み画面の場合
        # ==========================================
        try:
            direct_download_btns = driver.find_elements(By.XPATH, "//button[contains(@aria-label, 'ダウンロード')] | //button[contains(., 'ダウンロード')] | //a[contains(., 'ダウンロード')]")
            for btn in direct_download_btns:
                if btn.is_displayed():
                    log_flush("ℹ️ すでに認証済み画面が表示されています。直接『ダウンロード』を実行します。")
                    try: btn.click()
                    except Exception: driver.execute_script("arguments[0].click();", btn)
                    
                    wait_time = 0
                    while wait_time < 60:
                        time.sleep(2)
                        wait_time += 2
                        files = os.listdir(DOWNLOAD_DIR)
                        if files and not any(f.endswith('.crdownload') or f.endswith('.tmp') for f in files): break

                    downloaded_files = glob.glob(str(DOWNLOAD_DIR / '*'))
                    if downloaded_files:
                        latest_file = max(downloaded_files, key=os.path.getctime)
                        log_flush(f"✅ ファイルダウンロード成功: {latest_file}")
                        return latest_file, None, None
        except Exception:
            pass

        # ==========================================
        # STEP 1: パスワード入力
        # ==========================================
        email_input = None
        try:
            email_input = driver.find_element(By.XPATH, "//input[@type='email' or contains(@placeholder, 'メールアドレス') or contains(@name, 'email')]")
            log_flush("ℹ️ すでにステップ2（メールアドレス入力画面）が表示されています。パスワード入力をスキップします。")
        except Exception:
            pass

        if not email_input:
            try:
                pass_input = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@type='password']")))
            except Exception:
                body_text = driver.find_element(By.TAG_NAME, "body").text.replace('\n', ' ')[:200]
                log_flush(f"❌ パスワード入力欄が見つかりません。画面表示: {body_text}", logging.ERROR)
                return None, None, None

            successful_password, successful_pass_msg_id = None, None

            for idx, (score, cand_password, p_subj, p_date, p_msg_id, t_diff) in enumerate(password_candidates):
                log_flush(f"🔑 パスワード入力試行中 ({idx + 1}/{len(password_candidates)}): {cand_password}")
                
                pass_input.clear()
                driver.execute_script("arguments[0].click();", pass_input)
                time.sleep(0.2)
                
                for char in cand_password:
                    pass_input.send_keys(char)
                    time.sleep(0.05)
                
                driver.execute_script("arguments[0].blur();", pass_input)
                time.sleep(0.5)
                
                try:
                    btns = driver.find_elements(By.XPATH, "//button[@type='submit' or contains(., '送信') or contains(., '次へ')] | //input[@type='submit']")
                    if btns:
                        try: btns[0].click()
                        except Exception: driver.execute_script("arguments[0].click();", btns[0])
                    else: pass_input.send_keys(Keys.RETURN)
                except Exception:
                    pass_input.send_keys(Keys.RETURN)
                
                time.sleep(2)
                try:
                    email_input = WebDriverWait(driver, 8).until(
                        EC.presence_of_element_located((By.XPATH, "//input[@type='email' or contains(@placeholder, 'メールアドレス') or contains(@name, 'email')]"))
                    )
                    successful_password = cand_password
                    successful_pass_msg_id = p_msg_id
                    log_flush(f"✅ パスワード認証成功: 次の画面（メールアドレス入力）へ遷移しました")
                    break
                except Exception:
                    body_text = driver.find_element(By.TAG_NAME, "body").text.replace('\n', ' ')
                    log_flush(f"⚠️ パスワード認証失敗: 画面上のテキスト(一部): {body_text[:300]}", logging.WARNING)

            if not successful_password and not email_input:
                log_flush("❌ 全パスワード候補で認証失敗、またはタイムアウトしました。", logging.ERROR)
                return None, None, None

        # ==========================================
        # STEP 2: メールアドレス入力と送信
        # ==========================================
        if not email_input:
            email_input = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@type='email' or contains(@placeholder, 'メールアドレス') or contains(@name, 'email')]")))

        log_flush(f"✉️ メールアドレス入力試行: {my_email}")
        email_input.clear()
        driver.execute_script("arguments[0].click();", email_input)
        time.sleep(0.2)
        
        for char in my_email:
            email_input.send_keys(char)
            time.sleep(0.02)
            
        driver.execute_script("arguments[0].blur();", email_input)
        time.sleep(0.5)

        request_timestamp = time.time()
        
        log_flush("🔘 『認証コードを送信』ボタンをクリックします")
        try:
            send_code_btn = wait.until(EC.presence_of_element_located((By.XPATH, "//button[@type='submit' or contains(., '認証コード')]")))
            driver.execute_script("arguments[0].removeAttribute('disabled');", send_code_btn)
            try: send_code_btn.click()
            except Exception: driver.execute_script("arguments[0].click();", send_code_btn)
        except Exception as e:
            log_flush(f"❌ 『認証コードを送信』ボタンが押せませんでした: {e}", logging.ERROR)
            return None, None, None

        log_flush("⏳ Gmailから認証コード(6桁の数字)の受信を待機中...(最大約45秒)")
        auth_code, auth_msg_id = fetch_verification_code(service, request_timestamp, processed_label_id)
        if not auth_code: 
            log_flush("❌ 認証コードがGmailに届きませんでした（タイムアウト）。", logging.ERROR)
            raise Exception("認証コードが取得できませんでした。")
            
        log_flush(f"✅ 認証コードを受信しました: {auth_code}")

        # ==========================================
        # STEP 3: 認証コード入力と確実な送信処理
        # ==========================================
        code_input = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@type='text' or @type='number' or contains(@placeholder, 'コード')]")))
        code_input.clear()
        
        log_flush(f"🔢 認証コードを入力します: {auth_code}")
        driver.execute_script("arguments[0].focus();", code_input)
        for char in auth_code:
            code_input.send_keys(char)
            time.sleep(0.05)
            
        # React/Vueに入力完了イベントを伝播させる
        driver.execute_script("""
            arguments[0].dispatchEvent(new Event('input', { bubbles: true }));
            arguments[0].dispatchEvent(new Event('change', { bubbles: true }));
        """, code_input)
        time.sleep(0.5)

        log_flush("🔘 認証実行（フォーム送信）を行います")
        verify_clicked = False
        try:
            verify_btns = driver.find_elements(By.XPATH, "//button[@type='submit' or contains(., '認証') or contains(., '次へ') or contains(., '確認')] | //input[@type='submit']")
            for v_btn in verify_btns:
                driver.execute_script("arguments[0].removeAttribute('disabled');", v_btn)
                try:
                    v_btn.click()
                    verify_clicked = True
                    break
                except Exception:
                    driver.execute_script("arguments[0].click();", v_btn)
                    verify_clicked = True
                    break
        except Exception:
            pass

        if not verify_clicked:
            code_input.send_keys(Keys.RETURN)

        log_flush("⏳ 画面遷移（ファイル受信画面）を待機しています...")
        time.sleep(5)

        # ==========================================
        # STEP 4: ダウンロードボタンの全自動探索
        # ==========================================
        log_flush("📥 『ダウンロード』ボタンを探してクリックします")
        
        download_clicked = False
        for attempt in range(10): # 2秒おきに10回試行 (計20秒待機)
            download_clicked = driver.execute_script("""
                const els = document.querySelectorAll('button, a, div[role="button"], span');
                for (let el of els) {
                    const txt = el.innerText || '';
                    const label = el.getAttribute('aria-label') || '';
                    if (txt.includes('ダウンロード') || label.includes('ダウンロード')) {
                        el.click();
                        return true;
                    }
                }
                return false;
            """)
            if download_clicked:
                log_flush("✅ JavaScriptによる『ダウンロード』ボタンの強制クリックに成功しました。")
                break
            time.sleep(2)

        if not download_clicked:
            body_text = driver.find_element(By.TAG_NAME, "body").text.replace('\n', ' ')[:400]
            log_flush(f"❌ ダウンロードボタンが発見できませんでした。画面表示: {body_text}", logging.ERROR)
            return None, None, None

        # ファイル生成を待機
        wait_time = 0
        while wait_time < 60:
            time.sleep(2)
            wait_time += 2
            files = os.listdir(DOWNLOAD_DIR)
            if files and not any(f.endswith('.crdownload') or f.endswith('.tmp') for f in files): break

        downloaded_files = glob.glob(str(DOWNLOAD_DIR / '*'))
        if downloaded_files:
            latest_file = max(downloaded_files, key=os.path.getctime)
            log_flush(f"✅ ファイルダウンロード成功: {latest_file}")
            return latest_file, successful_pass_msg_id, auth_msg_id
            
        log_flush("❌ ダウンロードフォルダにファイルが生成されませんでした。", logging.ERROR)
        return None, None, None
        
    except Exception as e:
        log_flush(f"HENNGEダウンロード例外発生: {e}", logging.ERROR)
        return None, None, None
    finally:
        if driver: driver.quit()

# --- PDFデータ動的自動解析処理 ---
def process_pdf_data(pdf_path):
    extracted_data = []
    stop_count, recovery_count = 0, 0

    for page_layout in extract_pages(pdf_path):
        text_elements = []
        for element in page_layout:
            if isinstance(element, LTTextContainer):
                for text_line in element:
                    text = text_line.get_text().strip()
                    if text:
                        bbox = text_line.bbox
                        text_elements.append({
                            'x0': bbox[0],
                            'y0': bbox[1],
                            'x1': bbox[2],
                            'y1': bbox[3],
                            'text': text
                        })

        header_nodes = [el for el in text_elements if 505 <= el['y0'] <= 525]
        header_nodes.sort(key=lambda e: e['x0'])

        col_headers = []
        for el in header_nodes:
            if not col_headers or (el['x0'] - col_headers[-1]['x1']) > 6:
                col_headers.append({'x0': el['x0'], 'x1': el['x1'], 'text': el['text']})
            else:
                col_headers[-1]['text'] += " " + el['text']
                col_headers[-1]['x1'] = max(col_headers[-1]['x1'], el['x1'])

        header_map = []
        for idx, h in enumerate(col_headers):
            txt = h['text'].lower()
            key = 'IGNORE'
            if any(k in txt for k in ['物件id', 'mid', 'ｍｉｄ']): key = 'MID'
            elif '物件名' in txt: key = '物件名'
            elif '部屋番号' in txt: key = '部屋番号'
            elif any(k in txt for k in ['住所', '物件住所']): key = '住所'
            elif '訪問' in txt: key = '訪問'
            elif any(k in txt for k in ['在籍', '駐在', '管理員', '管理人', '管理']): key = '管理人'
            elif any(k in txt for k in ['al', 'オートロック']): key = 'AL'
            elif '備考' in txt: key = '備考'

            l_bound = 0.0 if idx == 0 else header_map[-1]['right']
            if idx == len(col_headers) - 1:
                r_bound = 9999.0
            else:
                next_x0 = col_headers[idx+1]['x0']
                r_bound = (h['x1'] + next_x0) / 2.0
                if key in ['訪問', 'AL']:
                    r_bound = min(r_bound, h['x1'] + 3.0)

            header_map.append({'key': key, 'left': l_bound, 'right': r_bound, 'text': h['text']})

        mid_header = next((hm for hm in header_map if hm['key'] == 'MID'), None)
        mid_l = mid_header['left'] if mid_header else 0
        mid_r = mid_header['right'] if mid_header else 120

        mids = [el for el in text_elements if re.match(r'^\d+-[R\d]+', el['text']) and mid_l <= el['x0'] < mid_r]
        mids.sort(key=lambda el: -el['y0'])

        current_type, current_action = 'レジル', '停止'

        for idx, mid in enumerate(mids):
            mid_y = mid['y0']
            prev_y = mids[idx - 1]['y0'] if idx > 0 else mid_y + 20
            next_y = mids[idx + 1]['y0'] if idx < len(mids) - 1 else mid_y - 20

            top_bound = (mid_y + prev_y) / 2.0
            bottom_bound = (mid_y + next_y) / 2.0

            row_elements = [el for el in text_elements if bottom_bound <= el['y0'] < top_bound]
            row_elements.sort(key=lambda el: (-el['y0'], el['x0']))

            field_values = {k: [] for k in ['MID', '物件名', '部屋番号', '住所', '訪問', '管理人', 'AL', '備考', 'IGNORE']}
            mid_val = mid['text']

            for el in row_elements:
                x_center = (el['x0'] + el['x1']) / 2.0
                txt = el['text']
                if txt == mid_val: continue

                matched_col = None
                for hm in header_map:
                    if hm['left'] <= x_center < hm['right']:
                        matched_col = hm['key']
                        break

                if matched_col and txt not in field_values[matched_col]:
                    field_values[matched_col].append(txt)

            obj_name = " ".join(field_values['物件名'])
            room_val = " ".join(field_values['部屋番号'])
            addr_raw = " ".join(field_values['住所'])
            visit_val = "".join(field_values['訪問'])
            kanri_val = " ".join(field_values['管理人'])
            al_raw = " ".join(field_values['AL'])
            remark_val = " ".join(field_values['備考'])

            addr = re.sub(r'^\s*0\s*', '', addr_raw)
            addr = re.sub(r'^\s*[\d\.]+\s+(?=[一-龠都道府県])', '', addr)
            addr = re.sub(r'\.0$', '', addr)

            if '文書投函' in visit_val or '文書投函' in remark_val: action_status = '文書投函'
            elif '復旧' in remark_val or '復旧' in kanri_val: action_status = '復旧'
            else: action_status = current_action

            if action_status == '復旧':
                recovery_count += 1
                continue

            stop_count += 1
            al_status = '' if not al_raw else ('有' if any(k in al_raw for k in ['放', '有', 'あり']) else '無')

            extracted_data.append({
                '停止or復旧': action_status,
                '物件種別': current_type,
                '物件名': obj_name,
                '部屋番号': room_val,
                '住所': addr,
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
    def get_c(*names): 
        for name in names:
            for idx, col_name in enumerate(cols):
                if name in col_name: return idx
        return -1

    extracted_data = []
    current_action, current_type = '停止', 'NP'
    suspension_keywords = {'＜レジル＞': 'レジル', '＜旧オリックス＞': 'NP', '＜旧Eハウス＞': 'NP', '＜旧NTT-AE＞': 'レジル'}
    stop_count, recovery_count = 0, 0

    for i in range(header_idx + 1, len(full_sheet)):
        row = full_sheet.iloc[i]
        row_str = "".join([str(v) for v in row.values])
        
        if any(k in row_str for k in ['＜復旧＞', '復旧', '復電']): current_action = '復旧'
        else:
            for kw, t in suspension_keywords.items():
                if kw in row_str:
                    current_action, current_type = '停止', t
                    break
                    
        if 'レジル' in row_str and '＜' not in row_str: current_type = 'レジル'
        if '旧オリックス' in row_str or '旧Eハウス' in row_str: current_type = 'NP'

        mid_col, obj_col, room_col = get_c('ＭＩＤ', 'MID'), get_c('物件名'), get_c('部屋番号')
        if mid_col == -1 or obj_col == -1 or room_col == -1: continue
        
        mid_val = str(row[mid_col]).strip()
        obj_name = str(row[obj_col]).strip()
        room_val = str(row[room_col]).strip()

        mid_val = '' if mid_val.lower() == 'nan' else mid_val
        obj_name = '' if obj_name.lower() == 'nan' else obj_name
        room_val = '' if room_val.lower() == 'nan' else room_val

        if not obj_name or not room_val: continue

        processed_room = re.sub(r'\.0$', '', room_val)

        pref_col, addr_col = get_c('都道府県'), get_c('物件住所', '住所')
        pref = str(row[pref_col]).strip() if pref_col != -1 else ''
        addr = str(row[addr_col]).strip() if addr_col != -1 else ''
        full_address = re.sub(r'^\s*0\s*', '', pref + addr)
        full_address = re.sub(r'\.0$', '', full_address)

        al_col = get_c('AL', 'オートロック')
        al_raw = str(row[al_col]).strip() if al_col != -1 else ''
        al_status = '' if (al_raw == '' or al_raw.lower() == 'nan') else ('有' if any(k in al_raw for k in ['放', '有', 'あり']) else '無')
        
        remark_col = get_c('備考')
        remark_val = str(row[remark_col]).strip() if remark_col != -1 else ''
        remark_val = '' if remark_val.lower() == 'nan' else remark_val
        
        action_status = '文書投函' if '文書投函' in remark_val else current_action
        kanri_col = get_c('管理員様在籍日時', '管理人駐在時間', '管理員', '管理')
        kanri_val = str(row[kanri_col]).strip() if kanri_col != -1 else ''
        kanri_val = '' if kanri_val.lower() == 'nan' else kanri_val
        
        if '復旧' in remark_val or '復旧' in kanri_val: action_status = '復旧'
            
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
    REMARK_COL_2 = "レジル様記入備考" + " " * 52

    ALL_HEADERS = [
        'BLAS_データ管理番号', 'BLAS_担当会社', '停止or復旧', '物件種別', '物件名', 
        '部屋番号※番号のみ入力', '物件住所', '工事会社', '作業者', '作業日', 
        'メーター番号', '腕章は携帯しているか', '管理人駐在時間', '入館方法', 'オートロックの有無', 
        'オートロック番号', 'スキルレススイッチの有無', '文書投函場所', '入館時刻', '停止・復電完了時刻', 
        '退館時刻', 'ステータス', '脚立の必要有無', '脚立必要の場合：メーターの高さや必要な脚立の高さ', 
        'パネルの有無', 'パネルありの場合：パネルの大きさやビス数、位置高さ', '鍵の必要有無', '鍵が必要な場合：必要な鍵', 
        '【退館前】撮影写真に不備はないか', '【退館前】ゴミ・忘れものはしていないか', '駐車場', 
        REMARK_COL_2, 
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

# --- メイン処理 ---
if __name__ == '__main__':
    log_flush("=== VERSION_CHECK_555 ===")
    log_flush("=== 自動処理を開始します ===")
    gmail_service = get_gmail_service()
    processed_label_id = get_or_create_processed_label_id(gmail_service, "処理済み")
    
    target_url, password_candidates, subject_text, url_msg_id = fetch_hennge_details(gmail_service, processed_label_id)
    
    if not target_url or not password_candidates:
        log_flush("有効な対象メールまたはパスワードが見つかりませんでした。処理を終了します。")
        exit(0)
        
    downloaded_file_path, pass_msg_id, auth_msg_id = download_from_hennge(target_url, password_candidates, gmail_service, processed_label_id)
    if not downloaded_file_path or not os.path.exists(downloaded_file_path):
        log_flush("❌ ダウンロード失敗。終了します。", logging.ERROR)
        exit(1)

    if downloaded_file_path.lower().endswith('.zip'):
        with zipfile.ZipFile(downloaded_file_path, 'r') as zip_ref:
            zip_ref.extractall(DOWNLOAD_DIR)
        os.remove(downloaded_file_path)

    target_files = glob.glob(str(DOWNLOAD_DIR / '*.pdf')) + glob.glob(str(DOWNLOAD_DIR / '*.xlsx')) + glob.glob(str(DOWNLOAD_DIR / '*.xls'))
    if not target_files:
        log_flush("❌ PDF/Excelファイルが見つかりません。", logging.ERROR)
        exit(1)

    options = get_chrome_options()
    driver = None
    wait = None
    try:
        service_chrome = ChromeService(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service_chrome, options=options)
        wait = WebDriverWait(driver, 30)

        log_flush("BLASにログイン中...")
        driver.get("https://www.basis-service.com/blas70/users/login")
        wait.until(EC.presence_of_element_located((By.NAME, "username"))).send_keys(BASIS_USERNAME)
        driver.find_element(By.NAME, "password").send_keys(BASIS_PASSWORD)
        driver.find_element(By.XPATH, "//input[@type='submit']").click()
        time.sleep(5)
    except Exception as e:
        log_flush(f"BLAS初期ログイン失敗: {e}", logging.ERROR)
        if driver: driver.quit()
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

            if p_count == 0:
                log_flush(f"⏭️ {file_name} には「停止」対象データがありませんでした（復旧データ {r_count}件 をスキップ）。")
                os.remove(file_path)
                continue

            if TEST_CSV_ONLY:
                continue

            driver.get("https://www.basis-service.com/blas70/items")
            wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "select2-selection__arrow"))).click()
            search_field = wait.until(EC.presence_of_element_located((By.CLASS_NAME, "select2-search__field")))
            search_field.send_keys("【レジル】停止・復電業務")
            time.sleep(2)
            wait.until(EC.element_to_be_clickable((By.XPATH, "//li[contains(text(), '【レジル】停止・復電業務')]"))).click()
            
            wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(.,'CSVインポート')]"))).click()
            chk = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@type='radio' and @value='1']")))
            driver.execute_script("arguments[0].click();", chk)
            
            driver.find_element(By.XPATH, "//input[@type='file']").send_keys(os.path.abspath(unique_csv_path))
            wait.until(EC.element_to_be_clickable((By.ID, "csv_import_btn"))).click()
            
            try:
                WebDriverWait(driver, 5).until(EC.alert_is_present())
                driver.switch_to.alert.accept()
            except Exception: pass
            
            time.sleep(10)
            
            shutil.move(str(unique_csv_path), PROCESSED_DIR / f"output_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}.csv")
            
            detected_region = get_region_from_info(file_name, p_addr)
            write_to_lark_sheet(ext_data, detected_region)

            os.remove(file_path)
            
            success_items.append({
                "name": f"{p_name} 外 ({p_count}件)",
                "region": detected_region
            })
            
        except Exception as e:
            log_flush(f"❌ エラーが発生しました ({file_name}): {e}", logging.ERROR)
            failure_items.append((file_name, str(e)))

    if driver:
        driver.quit()

    send_combined_lark_report(success_items, failure_items)

    if not failure_items and success_items:
        add_processed_label(gmail_service, [url_msg_id, pass_msg_id, auth_msg_id], processed_label_id)

    log_flush("=== 全処理終了 ===")
