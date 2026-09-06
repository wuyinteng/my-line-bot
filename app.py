ffrom flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage, QuickReply, QuickReplyButton, MessageAction

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
warnings.filterwarnings("ignore")

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import mplfinance as mpf

app = Flask(__name__)

# ==========================================
# 🔑 1. 金鑰改由雲端環境變數讀取
# ==========================================
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN")
IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY")
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
except Exception as e:
    pass

# ==========================================
# 📈 2. 文字現況報告
# ==========================================
def get_quote(msg):
    msg = msg.upper().strip()
    if msg.isdigit() and len(msg) >= 4:
        try:
            stock_name = tw_stock_dict.get(msg, "")
            name_display = f"{stock_name} ({msg})" if stock_name else f"代碼：{msg}"
            start_date = (datetime.datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
            df = dl.taiwan_stock_daily(stock_id=msg, start_date=start_date)
            
            if df.empty: return f"找不到台股【{name_display}】資料"
            if len(df) >= 2:
                tc, to, pc = df['close'].iloc[-1], df['open'].iloc[-1], df['close'].iloc[-2]
                dp, pp = tc - pc, (tc - pc) / pc * 100
                sp = "🔺" if dp > 0 else ("🔻" if dp < 0 else "➖")
                return (f"📊 【股票報價】{name_display}\n"
                        f"▪️ 成交價：{tc:.2f} TWD\n▪️ 漲跌幅：{sp}{dp:+.2f} ({pp:+.2f}%)")
        except: return None
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
    buf.seek(0)
    img_base64 = base64.b64encode(buf.read()).decode('utf-8')
    res = requests.post("https://api.imgbb.com/1/upload", data={"key": IMGBB_API_KEY, "image": img_base64})
    if res.status_code == 200: return res.json()['data']['url']
    return None

def calc_ylim(series):
    s_min, s_max = series.min(), series.max()
    rng = s_max - s_min
    if rng == 0: rng = abs(s_max) if s_max != 0 else 100
    return (s_min - rng * 0.1, s_max + rng * 0.1)

# ==========================================
# 🎨 3. 繪製三圖流 (K線、大戶散戶、外資投信融資)
# ==========================================
def generate_kline_vol_chart(stock_id, chart_type="K"):
    stock = yf.Ticker(f"{stock_id}.TW" if stock_id.isdigit() else stock_id)
    if chart_type == "走":
        df = stock.history(period="5d", interval="5m" if stock_id.isdigit() else "1m")
        if df.empty: return None
        df = df.dropna(subset=['Close'])
        if len(df) >= 2:
            df.index = df.index.tz_localize(None)
            df = df[df.index.date == df.index[-1].date()]
        if df.empty: return None
        mc = mpf.make_marketcolors(up='#ff4d4d', down='#00b300', inherit=True)
        buf = io.BytesIO()
        mpf.plot(df, type='line', volume=False, style=mpf.make_mpf_style(marketcolors=mc), title=f"{stock_id} Intraday", tight_layout=True, savefig=buf)
        return upload_imgbb(buf)

    df_full = stock.history(period='4mo')
    if df_full.empty: return None
    cutoff_date = df_full.index.max() - pd.DateOffset(months=3)
    df = df_full[df_full.index >= cutoff_date].copy()
    df.index = df.index.tz_localize(None)
    
    mc = mpf.make_marketcolors(up='#ff4d4d', down='#00b300', inherit=True)
    buf = io.BytesIO()
    mpf.plot(df, type='candle', volume=True, style=mpf.make_mpf_style(marketcolors=mc), mav=(5, 10, 20),
             title=f"{stock_id} 3M K-Line", figratio=(10, 7), tight_layout=True, savefig=buf)
    return upload_imgbb(buf)

def generate_holders_chart(stock_id):
    if not stock_id.isdigit(): return None
    try:
        url = f"https://norway.twsthr.info/StockHolders.aspx?stock={stock_id}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers, timeout=10)
        res.encoding = 'utf-8'
        soup = BeautifulSoup(res.text, 'html.parser')
        
        target_table = None
        for table in soup.find_all('table'):
            if '小於1張' in table.text and '1000張以上' in table.text:
                target_table = table
                break
                
        parsed_data = []
        if target_table:
            for tr in target_table.find_all('tr'):
                cols = [td.text.replace('\xa0', '').strip() for td in tr.find_all(['td', 'th'])]
                date_idx = -1
                for i, c in enumerate(cols):
                    if c.startswith('20') and len(c) == 8 and c.isdigit():
                        date_idx = i
                        break
                if date_idx != -1 and len(cols) >= date_idx + 16:
                    try:
                        parsed_data.append({
                            'date': pd.to_datetime(cols[date_idx], format='%Y%m%d'),
                            'Big_Holder': float(cols[date_idx+15]),
                            'Retail_Holder': float(cols[date_idx+1]) + float(cols[date_idx+2]) + float(cols[date_idx+3])
                        })
                    except: continue
                    
        if not parsed_data: return None
        df = pd.DataFrame(parsed_data).drop_duplicates(subset=['date'], keep='first').sort_values('date').tail(16)
        
        df['Big_Color'] = df['Big_Holder'].diff().apply(lambda x: '#ff4d4d' if x > 0 else ('#00b300' if x < 0 else '#808080'))
        df['Retail_Color'] = df['Retail_Holder'].diff().apply(lambda x: '#ff4d4d' if x > 0 else ('#00b300' if x < 0 else '#808080'))
        
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 5), sharex=True)
        x_labels = df['date'].dt.strftime('%m-%d')
        x_pos = range(len(df))
        
        ax1.bar(x_pos, df['Big_Holder'], color=df['Big_Color'])
        ax1.set_title("Big Holders (>1000) %", loc='left', fontweight='bold')
        ax1.set_ylim(calc_ylim(df['Big_Holder']))
        ax1.grid(True, alpha=0.3)
        
        ax2.bar(x_pos, df['Retail_Holder'], color=df['Retail_Color'])
        ax2.set_title("Retail Holders (<10) %", loc='left', fontweight='bold')
        ax2.set_ylim(calc_ylim(df['Retail_Holder']))
        ax2.grid(True, alpha=0.3)
        
        ax2.set_xticks(x_pos[::2])
        ax2.set_xticklabels(x_labels.iloc[::2], rotation=45)
        plt.tight_layout()
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=120)
        plt.close(fig)
        return upload_imgbb(buf)
    except: return None

def generate_inst_margin_chart(stock_id):
    if not stock_id.isdigit(): return None
    try:
        start_date = (datetime.datetime.now() - timedelta(days=100)).strftime('%Y-%m-%d')
        df_inst = dl.taiwan_stock_institutional_investors(stock_id=stock_id, start_date=start_date)
        df_margin = dl.taiwan_stock_margin_purchase_short_sale(stock_id=stock_id, start_date=start_date)
        
        if df_inst.empty and df_margin.empty: return None
        
        df_inst['date'] = pd.to_datetime(df_inst['date'])
        df_inst['buy'] = pd.to_numeric(df_inst['buy'], errors='coerce').fillna(0)
        df_inst['sell'] = pd.to_numeric(df_inst['sell'], errors='coerce').fillna(0)
        df_inst['net'] = (df_inst['buy'] - df_inst['sell']) / 1000  
        
        df_foreign = df_inst[df_inst['name'].str.contains('外資|外陸|Foreign', na=False, case=False)].groupby('date')['net'].sum().reset_index()
        df_trust = df_inst[df_inst['name'].str.contains('投信|Trust', na=False, case=False)].groupby('date')['net'].sum().reset_index()
        
        if not df_margin.empty:
            df_margin['date'] = pd.to_datetime(df_margin['date'])
            df_margin['MarginPurchaseBuy'] = pd.to_numeric(df_margin['MarginPurchaseBuy'], errors='coerce').fillna(0)
            df_margin['MarginPurchaseSell'] = pd.to_numeric(df_margin['MarginPurchaseSell'], errors='coerce').fillna(0)
            df_margin['margin_net'] = df_margin['MarginPurchaseBuy'] - df_margin['MarginPurchaseSell']
        else:
            df_margin = pd.DataFrame(columns=['date', 'margin_net'])
        
        dates = sorted(list(set(df_foreign['date'].tolist() + df_trust['date'].tolist() + df_margin['date'].tolist())))
        df_plot = pd.DataFrame({'date': dates})
        df_plot = pd.merge(df_plot, df_foreign.rename(columns={'net': 'Foreign'}), on='date', how='left')
        df_plot = pd.merge(df_plot, df_trust.rename(columns={'net': 'Trust'}), on='date', how='left')
        df_plot = pd.merge(df_plot, df_margin[['date', 'margin_net']].rename(columns={'margin_net': 'Margin'}), on='date', how='left')
        
        df_plot.fillna(0, inplace=True)
        df_plot = df_plot.tail(60) 
        
        fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(8, 7), sharex=True)
        x_labels = df_plot['date'].dt.strftime('%m-%d')
        x_pos = range(len(df_plot))
        
        ax1.bar(x_pos, df_plot['Foreign'], color=['#ff4d4d' if x > 0 else '#00b300' for x in df_plot['Foreign']])
        ax1.set_title("Foreign Net (Lots)", loc='left', fontweight='bold')
        ax1.set_ylim(calc_ylim(df_plot['Foreign']))
        ax1.axhline(0, color='black', linewidth=0.8)
        ax1.grid(True, alpha=0.3)
        
        ax2.bar(x_pos, df_plot['Trust'], color=['#ff4d4d' if x > 0 else '#00b300' for x in df_plot['Trust']])
        ax2.set_title("Trust Net (Lots)", loc='left', fontweight='bold')
        ax2.set_ylim(calc_ylim(df_plot['Trust']))
        ax2.axhline(0, color='black', linewidth=0.8)
        ax2.grid(True, alpha=0.3)
        
        ax3.bar(x_pos, df_plot['Margin'], color=['#ff4d4d' if x > 0 else '#00b300' for x in df_plot['Margin']])
        ax3.set_title("Margin Net Buy/Sell (Lots)", loc='left', fontweight='bold')
        ax3.set_ylim(calc_ylim(df_plot['Margin']))
        ax3.axhline(0, color='black', linewidth=0.8)
        ax3.grid(True, alpha=0.3)
        
        ax3.set_xticks(x_pos[::5])
        ax3.set_xticklabels(x_labels.iloc[::5], rotation=45)
        plt.tight_layout()
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=120)
        plt.close(fig)
        return upload_imgbb(buf)
    except Exception as e: 
        print(f"外資融資圖繪製錯誤：{e}")
        return None

# ==========================================
# 🚀 4. [新增] 8:50 盤前戰情總匯引擎
# ==========================================
def get_intraday_chart_url(ticker_symbol, title_name):
    try:
        stock = yf.Ticker(ticker_symbol)
        df = stock.history(period="1d", interval="5m")
        if df.empty: return None
        
        df = df.dropna(subset=['Close'])
        df.index = df.index.tz_localize(None)
        mc = mpf.make_marketcolors(up='#ff4d4d', down='#00b300', inherit=True)
        s  = mpf.make_mpf_style(marketcolors=mc)
        buf = io.BytesIO()
        
        mpf.plot(df, type='line', volume=False, style=s, title=f"{title_name} Intraday", savefig=buf)
        return upload_imgbb(buf)
    except Exception as e:
        print(f"走勢圖繪製失敗 ({title_name}): {e}")
    return None

def morning_all_in_one_report():
    if MY_USER_ID == "請替換成您的_USER_ID":
        print("尚未設定 MY_USER_ID，無法發送盤前報告")
        return

    messages_to_send = []
    text_lines = ["🌅 【8:50 盤前戰情總匯】\n"]
    
    # 1. 美股四大指數
    text_lines.append("🌎 美股收盤現況：")
    us_targets = {"^DJI": "道瓊", "^GSPC": "標普", "^IXIC": "那斯達克", "^SOX": "費半"}
    for ticker, name in us_targets.items():
        try:
            df = yf.Ticker(ticker).history(period='2d')
            if len(df) >= 2:
                tc, pc = df['Close'].iloc[-1], df['Close'].iloc[-2]
                pp = (tc - pc) / pc * 100
                sp = "🔺" if pp > 0 else "🔻"
                text_lines.append(f"▪️ {name}: {tc:.2f} ({sp}{pp:+.2f}%)")
        except: continue

    # 2. 美股期貨強勢類股
    text_lines.append("\n🚀 盤前美股期貨動能：")
    fut_targets = {
        "YM=F": {"name": "小道瓊", "sector": "傳統/工業"},
        "NQ=F": {"name": "小那斯達克", "sector": "科技/半導體"},
        "ES=F": {"name": "標普500", "sector": "大型權值"}
    }
    for ticker, info in fut_targets.items():
        try:
            df = yf.Ticker(ticker).history(period='2d')
            if len(df) >= 2:
                tc, pc = df['Close'].iloc[-1], df['Close'].iloc[-2]
                pp = (tc - pc) / pc * 100
                sp = "🔥 強勢" if pp > 0 else "🔻 偏弱"
                text_lines.append(f"▪️ {info['name']} ({info['sector']}): {sp} ({pp:+.2f}%)")
        except: continue

    # 3. 外資台指期空單動向
    text_lines.append("\n🚨 籌碼：外資台指期動向")
    try:
        start_date = (datetime.datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
        df_fut = dl.taiwan_futures_institutional_investors(futures_id="TX", start_date=start_date)
        if not df_fut.empty:
            df_f = df_fut[df_fut['name'].str.contains('外資', na=False, case=False)]
            if len(df_f) >= 2:
                df_f['net_oi'] = df_f['long_open_interest'] - df_f['short_open_interest']
                today_oi = df_f['net_oi'].iloc[-1]
                yesterday_oi = df_f['net_oi'].iloc[-2]
                diff_oi = today_oi - yesterday_oi
                
                status = f"⚠️ 偷偷加空 {-diff_oi:,.0f} 口" if diff_oi < 0 else f"✅ 逢低回補 {diff_oi:,.0f} 口"
                text_lines.append(f"▪️ 目前淨未平倉：{today_oi:,.0f} 口")
                text_lines.append(f"▪️ 較前日變化：{status}")
    except:
        text_lines.append("▪️ 今日籌碼資料暫未更新")

    # 將所有文字組合成第一則 Bubble
    final_text = "\n".join(text_lines)
    messages_to_send.append(TextSendMessage(text=final_text))

    # 4. 日韓股走勢圖
    jp_url = get_intraday_chart_url("^N225", "日經225 (Japan)")
    if jp_url:
        messages_to_send.append(ImageSendMessage(original_content_url=jp_url, preview_image_url=jp_url))
        
    kr_url = get_intraday_chart_url("^KS11", "韓國KOSPI (Korea)")
    if kr_url:
        messages_to_send.append(ImageSendMessage(original_content_url=kr_url, preview_image_url=kr_url))

    # 5. 推播發送 (僅扣除 1 則額度)
    try:
        line_bot_api.push_message(MY_USER_ID, messages_to_send)
    except Exception as e:
        print(f"盤前總匯推播失敗: {e}")

# ==========================================
# 🌐 5. LINE Bot 路由與訊息處理
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
    try: requests.post("https://api.line.me/v2/bot/chat/loading/start", headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}", "Content-Type": "application/json"}, json={"chatId": event.source.sender_id, "loadingSeconds": 15})
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
            reply_text = f"✅ 【{stock_name}】三圖流分析完成！\n\n{quote_text}{holding_info}"
            
            messages_to_send = [TextSendMessage(text=reply_text, quick_reply=qr_buttons)]
            
            url_kline = generate_kline_vol_chart(stock_id, "K")
            if url_kline: messages_to_send.append(ImageSendMessage(original_content_url=url_kline, preview_image_url=url_kline))
            
            url_holders = generate_holders_chart(stock_id)
            if url_holders: messages_to_send.append(ImageSendMessage(original_content_url=url_holders, preview_image_url=url_holders))
            
            url_inst = generate_inst_margin_chart(stock_id)
            if url_inst: messages_to_send.append(ImageSendMessage(original_content_url=url_inst, preview_image_url=url_inst))
                
            line_bot_api.reply_message(event.reply_token, messages_to_send[:5])
        else:
            url_trend = generate_kline_vol_chart(stock_id, "走")
            if url_trend:
                line_bot_api.reply_message(event.reply_token, [
                    TextSendMessage(text=f"✅ 【{stock_name}】盤中走勢圖！", quick_reply=qr_buttons),
                    ImageSendMessage(original_content_url=url_trend, preview_image_url=url_trend)
                ])
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="圖片產生失敗。", quick_reply=qr_buttons))
        return
        
    result = get_quote(user_msg)
    if result:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=result, quick_reply=qr_buttons))

# ==========================================
# ⏰ 6. 啟動伺服器與鬧鐘排程
# ==========================================
scheduler = BackgroundScheduler(timezone="Asia/Taipei")
scheduler.add_job(morning_all_in_one_report, 'cron', day_of_week='mon-fri', hour=8, minute=50)
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)