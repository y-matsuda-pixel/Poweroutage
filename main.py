import sys
print("==========================================", flush=True)
print("=== PERFECT_CODE_VERSION_V19_FIX_HENNGE_AUTH ===", flush=True)
print("==========================================", flush=True)

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
import re
import glob
import base64
import zipfile
import unicodedata

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
EMAIL_SHEET_ID = "KTGtTb"

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
KANTO_CITIES = ['横浜市', '川崎市', 'さいたま市', '千葉市', '相模原市', '船橋市', '川口市', '新宿区', '世田谷区', '港区', '渋谷区', '中央区', '千代田区', '品川区', '目黒区', '大田区', '杉並区', '練馬区', '八王子市', '町田市', '藤沢市', '横須賀市', '平塚市', '茅ヶ崎市', '大和市', '厚木市', '所沢市', '川越市', '越谷市', '草加市', '市川市', '松戸市', '柏市', '市原市', '宇都宮市', '前橋市', '高崎市', '水戸市', '江戸川区']

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

# --- アプローチB: 項目名表記揺れ同義語辞書 ---
COLUMN_ALIASES = {
    'AL': ['有/解除番号有/無', '有/解除番号有', 'オートロックの有無', 'オートロック', 'al'],
    '住所': ['物件住所', '設置場所', '所在地', '住所'],
    'MID': ['物件id', 'mid', 'ｍｉｄ'],
    '物件名': ['物件名', '建物名', '対象物件', '件名'],
    '部屋番号': ['部屋番号', '部番号', '号室', '号', '部屋'],
    '管理人': ['管理員様在籍日時', '管理人駐在時間', '管理員様', '在籍日時', '管理人', '管理員', '管理'],
    '備考': ['備考', 'メモ', '注意事項']
}

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

def identify_column_key(text):
    txt_clean = text.lower().replace(' ', '').replace(' ', '').replace('\n', '')
    best_match_key = 'IGNORE'
    max_len = 0
    # 最も長く一致したキーワードを優先判定
    for std_key, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in txt_clean and len(alias) > max_len:
                max_len = len(alias)
                best_match_key = std_key
    return best_match_key

def is_address_text_strict(txt):
    txt_clean = re.sub(r'^\s*0\s*', '', txt.strip())
    # 時間表記（9:00-17:00 等）が含まれる場合は住所ではない
    if re.search(r'\d+:\d+', txt_clean) or re.search(r'\d+：\d+', txt_clean):
        return False
    all_prefs = KANTO_PREFS + KANSAI_PREFS + ['東京都', '大阪府', '京都府', '兵庫県', '埼玉県', '千葉県', '神奈川県']
    if any(p in txt_clean for p in all_prefs):
        return True
    if any(c in txt_clean for c in KANTO_CITIES + KANSAI_CITIES):
        return True
    if re.search(r'[一-龠]{2,3}[都道府県]', txt_clean) or re.search(r'\d+丁目|\d+番地|\d+-\d+', txt_clean):
        if not re.match(r'^\d+$', txt_clean) and not re.match(r'^[A-Za-z0-9-]+$', txt_clean):
            return True
    return False

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
    url = f"https://open.larksuite.com/open-apis/wiki/v2/nodes/get_node?token={LARK_WIKI_TOKEN}"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        res_json = res.json()
        if res_json.get("code") == 0:
            obj_token = res_json.get("data", {}).get("node", {}).get("obj_token")
            if obj_token:
                log_flush(f"🔑 WikiトークンからSpreadsheetトークンを取得成功: {obj_token}")
                return obj_token
        log_flush(f"Wikiトークン変換失敗: {res_json}", logging.WARNING)
    except Exception as e:
        log_flush(f"Wikiトークン変換例外: {e}", logging.ERROR)
    return LARK_WIKI_TOKEN

def write_email_to_lark_sheet(email_info):
    if not email_info: return
    tenant_token = get_lark_tenant_access_token()
    if not tenant_token:
        log_flush("Lark APIトークンが取得できなかったため、メール転記をスキップします。", logging.WARNING)
        return

    spreadsheet_token = get_spreadsheet_token(tenant_token
