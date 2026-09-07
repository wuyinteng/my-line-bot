from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage, QuickReply, QuickReplyButton, MessageAction
import time
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import yfinance as yf
from FinMind.data import DataLoader
import datetime
from datetime import timedelta
import pandas as pd
import os
import requests
import base64
import io
import traceback
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
import warnings
import mplfinance as mpf

warnings.filterwarnings("ignore")

app = Flask(__name__)

# ==========================================
# 🔑 1. 金鑰讀取 (加入預設值防呆)
# ==========================================
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN")
IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY", "4bc61e9d363f21433c906beb7440dd92")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
MY_USER_ID = os.environ.get("MY_USER_ID")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

dl = DataLoader()
if FINMIND_TOKEN:
    dl.login_by_token(api_token=FINMIND_TOKEN)

tw_stock_dict = {}
try:
    df_info = dl.taiwan_stock_info()
    tw_stock_dict = dict(zip(df_info['stock_id'], df_info['stock_name']))
except:
    pass

# ==========================================
# 📈 2. 報價與籌碼
# ==========================================
def get_quote(msg):
    msg = msg.upper().strip()
    if msg.isdigit() and len(msg) >= 4:
        try:
            stock_name = tw_stock_dict.get(msg, "")
            name_display = f"{stock_name} ({msg})" if stock_name else f"代碼：{msg}"
            start_date = (datetime.datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
            df = dl.taiwan_stock_daily(stock_id=msg, start_date=start_date)
            
            if df.empty: return f"找不到台股【{name_display}】近期資料，可能是 API 限制或代碼錯誤。"
            if len(df) >= 2:
                tc, to, pc = df['close'].iloc[-1], df['open'].iloc[-1], df['close'].iloc[-2]
                dp, pp = tc - pc, (tc - pc) / pc * 100
                sp = "🔺" if dp > 0 else ("🔻" if dp < 0 else "➖")
                return (f"📊 【股票報價】{name_display}\n"
                        f"▪️ 成交價：{tc:.2f} TWD\n▪️ 漲跌幅：{sp}{dp:+.2f} ({pp:+.2f}%)")
        except:
            return None
    elif msg.isalpha() and 1 <= len(msg) <= 5:
        try:
            stock = yf.Ticker(msg)
            df = stock.history(period='5d')
            if df.empty: return f"找不到美股代號【{msg}】"
            if len(df) >= 2:
                tc, pc = df['Close'].iloc[-1], df['Close'].iloc[-2]
                dp, pp = tc - pc, (tc - pc) / pc * 100
                sp = "🔺" if dp > 0 else ("🔻" if dp < 0 else "➖")
                try: comp_name = stock.info.get('shortName', msg)
                except: comp_name = msg
                return (f"📊 【美股報價】{comp_name} ({msg})\n"
                        f"▪️ 成交價：{tc:.2f} USD\n▪️ 漲跌幅：{sp}{dp:+.2f} ({pp:+.2f}%)")
        except:
            return None
    return None

def get_holding_shares_info(stock_id):
    if not stock_id.isdigit(): return ""
    try:
        url = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers, stream=True, timeout=10)
        latest_date, big_percent, small_percent = "", 0.0, 0.0
        found_stock = False
        
        for line in res.iter_lines():
            if not line: continue
            cols = [c.strip().replace('"', '') for c in line.decode('utf-8', errors='ignore').split(',')]
            if len(cols) >= 6:
                if cols[1] == stock_id:
                    found_stock = True
                    latest_date, level = cols[0], cols[2]
                    try:
                        percent = float(cols[5])
                        if level == '15': big_percent = percent
                        elif level in ['1', '2', '3']: small_percent += percent
                    except: continue
                elif found_stock and cols[1] != stock_id: break
        res.close()
        
        if not found_stock: return ""
        formatted_date = f"{latest_date[:4]}-{latest_date[4:6]}-{latest_date[6:]}" if len(latest_date) == 8 else latest_date
        return (f"\n\n👥 【集保籌碼現況】({formatted_date})\n"
                f"👑 千張大戶佔比：{big_percent:.2f}%\n"
                f"🐟 10張散戶佔比：{small_percent:.2f}%")
    except: return ""

def upload_imgbb(buf):
    try:
        if not IMGBB_API_KEY:
            print("❌ 找不到 IMGBB_API_KEY", flush=True)
            return None
            
        url = "https://api.imgbb.com/1/upload"
        payload = {"key": IMGBB_API_KEY}
        
        # 關鍵修復：將指標歸零，並改用二進位 (binary) 檔案格式直接上傳，徹底避開 Base64 長度超載問題
        buf.seek(0)
        files = {"image": ('chart.png', buf.getvalue(), 'image/png')}
        
        # 發送 Multipart/form-data 請求
        res = requests.post(url, data=payload, files=files, timeout=20)
        
        if res.status_code == 200:
            image_url = res.json()['data']['url']
            print(f"✅ 圖片上傳 ImgBB 成功！網址：{image_url}", flush=True)
            # 若您的其他函式是接收單一網址，請回傳 image_url；若接收 Tuple 請自行調整
            return image_url
        else:
            # 將 ImgBB 官方的真實報錯訊息抓出來
            err_detail = res.json().get('error', {}).get('message', '未知錯誤')
            print(f"❌ ImgBB 上傳失敗 (狀態碼 400): {err_detail}", flush=True)
            return None
            
    except Exception as e:
        print(f"❌ 圖片上傳過程發生未預期錯誤：{str(e)}", flush=True)
        return None
    
def calc_ylim(series):
    s_min, s_max = series.min(), series.max()
    rng = s_max - s_min
    if rng == 0: rng = abs(s_max) if s_max != 0 else 100
    return (s_min - rng * 0.1, s_max + rng * 0.1)

# ==========================================
# 🎨 3. 繪圖與自動除錯回報
# ==========================================
def generate_kline_vol_chart(stock_id, chart_type="K"):
    try:
        # 建立偽裝 Session，防止 Yahoo 阻擋
        session = requests.Session()
        session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'})
        
        if chart_type == "走":
            stock = yf.Ticker(f"{stock_id}.TW" if stock_id.isdigit() else stock_id, session=session)
            df = stock.history(period="5d", interval="5m" if stock_id.isdigit() else "1m")
            if df.empty: return None, "❌ Yahoo Finance 拒絕提供走勢資料 (可能遭阻擋)"
            df = df.dropna(subset=['Close'])
            if len(df) >= 2:
                df.index = df.index.tz_localize(None)
                df = df[df.index.date == df.index[-1].date()]
            if df.empty: return None, "❌ 找不到今日走勢資料"
            
            mc = mpf.make_marketcolors(up='#ff4d4d', down='#00b300', inherit=True)
            buf = io.BytesIO()
            mpf.plot(df, type='line', volume=False, style=mpf.make_mpf_style(marketcolors=mc), title=f"{stock_id} Intraday", tight_layout=True, savefig=buf)
            return upload_imgbb(buf)

        if stock_id.isdigit():
            start_date = (datetime.datetime.now() - timedelta(days=120)).strftime('%Y-%m-%d')
            df = dl.taiwan_stock_daily(stock_id=stock_id, start_date=start_date)
            if df.empty: return None, "❌ FinMind 找不到 K 線歷史資料"
            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)
            df.rename(columns={'open': 'Open', 'max': 'High', 'min': 'Low', 'close': 'Close', 'Trading_Volume': 'Volume'}, inplace=True)
        else:
            stock = yf.Ticker(stock_id, session=session)
            df = stock.history(period='4mo')
            if df.empty: return None, "❌ Yahoo Finance 找不到美股歷史資料"
            df.index = df.index.tz_localize(None)

        cutoff_date = df.index.max() - pd.DateOffset(months=3)
        df = df[df.index >= cutoff_date].copy()
        
        mc = mpf.make_marketcolors(up='#ff4d4d', down='#00b300', inherit=True)
        buf = io.BytesIO()
        mpf.plot(df, type='candle', volume=True, style=mpf.make_mpf_style(marketcolors=mc), mav=(5, 10, 20),
                 title=f"{stock_id} 3M K-Line", figratio=(10, 7), tight_layout=True, savefig=buf)
        return upload_imgbb(buf)
    except Exception as e:
        return None, f"❌ 繪圖發生未預期錯誤：{str(e)}"

# ==========================================
# 🌐 4. LINE Bot 路由與訊息處理
# ==========================================
@app.route("/", methods=['GET'])
def index():
    return "LINE Bot is running and awake!"

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip().upper()
    try: 
        requests.post(
            "https://api.line.me/v2/bot/chat/loading/start", 
            headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}", "Content-Type": "application/json"}, 
            json={"chatId": event.source.user_id, "loadingSeconds": 15} 
        )
    except: pass

    btn_target = user_msg.replace("K", "").replace("走", "")
    if btn_target in ["四大指數", "美股指數", "美股四大指數", "INDEX"]: btn_target = "^DJI"
    if not btn_target: btn_target = "2330"
    
    qr_buttons = QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="📊 K線圖", text=f"K{btn_target}")),
        QuickReplyButton(action=MessageAction(label="📈 走勢圖", text=f"走{btn_target}"))
    ])

    if user_msg.startswith("K") or user_msg.startswith("走"):
        chart_type = "K" if user_msg.startswith("K") else "走"
        stock_id = user_msg[1:] 
        stock_name = tw_stock_dict.get(stock_id, stock_id)
        
        if chart_type == "K":
            quote_text = get_quote(stock_id) or f"查詢【{stock_name}】..."
            holding_info = get_holding_shares_info(stock_id)
            
            url_kline, err_msg = generate_kline_vol_chart(stock_id, "K")
            
            if url_kline:
                reply_text = f"✅ 【{stock_name}】圖表分析完成！\n\n{quote_text}{holding_info}"
                line_bot_api.reply_message(event.reply_token, [
                    TextSendMessage(text=reply_text, quick_reply=qr_buttons),
                    ImageSendMessage(original_content_url=url_kline, preview_image_url=url_kline)
                ])
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ K線圖生成失敗\n原因：{err_msg}", quick_reply=qr_buttons))
        else:
            url_trend, err_msg = generate_kline_vol_chart(stock_id, "走")
            if url_trend:
                line_bot_api.reply_message(event.reply_token, [
                    TextSendMessage(text=f"✅ 【{stock_name}】盤中走勢圖！", quick_reply=qr_buttons),
                    ImageSendMessage(original_content_url=url_trend, preview_image_url=url_trend)
                ])
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ 走勢圖生成失敗\n原因：{err_msg}", quick_reply=qr_buttons))
        return
        
    result = get_quote(user_msg)
    if result:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=result, quick_reply=qr_buttons))
    else:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入正確的台股代碼（例如：2330），或美股代碼（例如：AAPL）。", quick_reply=qr_buttons))

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)