from flask import Flask, request, abort
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
from io import StringIO
from apscheduler.schedulers.background import BackgroundScheduler

import warnings
warnings.filterwarnings("ignore")

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import mplfinance as mpf

app = Flask(__name__)

# ==========================================
# 🔑 1. 金鑰與初始化設定
# ==========================================
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoid3V5aW50ZW5nIiwiZW1haWwiOiJ3dXlpbnRlbmcxMjA2QGdtYWlsLmNvbSJ9.3-HFSvEh15UnzB4Nt_TZUYLCF7OSjrDuB31fwZ1foJA")
IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY", "d63d28973c97e47057422ecdc217136f") 
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "8g/5K/9WQ7EiuEm16BBJ/aOjy7beli9UQS1oKoX3Jswq1iGuYxvlvT+OLpWO4ZTjRWscQlvRknxmtdioggR+rILSsd28GBtd1lbDcvPgv1VEE6yzdGScPxD/Evstgxtd6+lFTohe+R5lBjVi/+fqpQdB04t89/1O/w1cDnyilFU=")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "6394456d4596cc6aadb9c92dda96b296")
# ⚠️ 主動推播必備：請填入您的 LINE User ID (U開頭的字串)
MY_USER_ID = os.environ.get("MY_USER_ID", "U288dc1f88aabee28ca0342d542b8040f")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

dl = DataLoader()
if FINMIND_TOKEN:
    dl.login_by_token(api_token=FINMIND_TOKEN)

# 載入台股名稱
tw_stock_dict = {}
try:
    res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=10)
    if res.status_code == 200:
        for item in res.json():
            tw_stock_dict[item['Code']] = item['Name']
except Exception as e:
    pass

# ==========================================
# ⏰ 2. 每日 8:50 盤前全球指數速報 (圖文版)
# ==========================================
def morning_indices_report():
    if not MY_USER_ID or MY_USER_ID.startswith("請填入"):
        print("⚠️ 尚未設定 MY_USER_ID，無法發送推播。")
        return
        
    targets = {
        "^DJI": "道瓊工業",
        "^GSPC": "標普 500",
        "^IXIC": "那斯達克",
        "^SOX": "費城半導體",
        "^N225": "日經 225",
        "^KS11": "韓國 KOSPI"
    }
    
    text_lines = ["🌅 【8:50 早盤全球指數速報】\n"]
    
    for ticker, name in targets.items():
        try:
            df = yf.Ticker(ticker).history(period='5d')
            if len(df) >= 2:
                tc, pc = df['Close'].iloc[-1], df['Close'].iloc[-2]
                dp, pp = tc - pc, (tc - pc) / pc * 100
                sp = "🔺" if dp > 0 else ("🔻" if dp < 0 else "➖")
                text_lines.append(f"▪️ {name}: {tc:.2f} ({sp}{pp:+.2f}%)")
            else:
                text_lines.append(f"▪️ {name}: 暫無資料")
        except Exception as e:
            text_lines.append(f"▪️ {name}: 讀取失敗")
            
    # 第一個泡泡：文字總匯
    messages_to_send = [TextSendMessage(text="\n".join(text_lines))]
    
    # 接下來的三個泡泡：日經、韓國、費半走勢圖
    chart_targets = {"^N225": "日經 225", "^KS11": "韓國 KOSPI", "^SOX": "費城半導體"}
    for ticker, name in chart_targets.items():
        try:
            # 呼叫我們現有的繪圖函式
            img_url = generate_main_chart(ticker, chart_type="走")
            if img_url:
                messages_to_send.append(ImageSendMessage(original_content_url=img_url, preview_image_url=img_url))
        except Exception as e:
            print(f"⚠️ {name} 繪圖失敗: {e}")

    # 一次打包發送 (僅算 1 則額度)
    try:
        line_bot_api.push_message(MY_USER_ID, messages_to_send)
        print("✅ 盤前總匯推播發送成功（含走勢圖）！")
    except Exception as e:
        print(f"❌ 盤前推播發送失敗: {e}")

# ==========================================
# 🛠️ 輔助函式：動態對焦 Y 軸 (鎖定位階)
# ==========================================
def get_focused_ylim(series, buffer_ratio=0.1):
    clean_series = series.dropna()
    if clean_series.empty: return (0, 100)
    s_min, s_max = clean_series.min(), clean_series.max()
    rng = s_max - s_min
    if rng == 0: rng = abs(s_max) * 0.1 if s_max != 0 else 10
    return (s_min - rng * buffer_ratio, s_max + rng * buffer_ratio)

def upload_imgbb(buf):
    buf.seek(0)
    img_base64 = base64.b64encode(buf.read()).decode('utf-8')
    res = requests.post("https://api.imgbb.com/1/upload", data={"key": IMGBB_API_KEY, "image": img_base64})
    if res.status_code == 200: return res.json()['data']['url']
    return None

# ==========================================
# 📈 3. 美股與台股報價查詢 (文字)
# ==========================================
def get_quote(msg):
    msg = msg.upper().strip()
    if msg.isdigit() and len(msg) >= 4:
        try:
            stock_name = tw_stock_dict.get(msg, msg)
            stock = yf.Ticker(f"{msg}.TW")
            df = stock.history(period="5d")
            if df.empty: return f"找不到台股【{msg}】資料"
            if len(df) >= 2:
                tc, pc = df['Close'].iloc[-1], df['Close'].iloc[-2]
                dp, pp = tc - pc, (tc - pc) / pc * 100
                sp = "🔺" if dp > 0 else ("🔻" if dp < 0 else "➖")
                return (f"📊 【台股報價】{stock_name} ({msg})\n"
                        f"--------------------\n▪️ 成交價：{tc:.2f} TWD\n▪️ 漲跌幅：{sp}{dp:+.2f} ({pp:+.2f}%)")
        except: return None
    else:
        try:
            stock = yf.Ticker(msg)
            df = stock.history(period="5d")
            if df.empty: return None
            if len(df) >= 2:
                tc, pc = df['Close'].iloc[-1], df['Close'].iloc[-2]
                dp, pp = tc - pc, (tc - pc) / pc * 100
                sp = "🔺" if dp > 0 else ("🔻" if dp < 0 else "➖")
                return (f"📊 【美股報價】{msg}\n"
                        f"--------------------\n▪️ 成交價：{tc:.2f} USD\n▪️ 漲跌幅：{sp}{dp:+.2f} ({pp:+.2f}%)")
        except: return None

# ==========================================
# 🕷️ 4. 神秘金字塔爬蟲 (大戶與散戶)
# ==========================================
# ==========================================
# 🕷️ 神秘金字塔爬蟲 (大戶與散戶)
# ==========================================
def get_chip_from_pyramid(stock_id):
    url = f"https://norway.twsthr.info/StockHolders.aspx?stock={stock_id}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.encoding = 'utf-8'
        dfs = pd.read_html(io.StringIO(res.text))
        
        target_df = None
        for df in dfs:
            head_str = "".join(df.head(3).astype(str).values.flatten())
            if '日期' in head_str and '1000' in head_str:
                target_df = df
                break
        if target_df is None: return None
            
        date_col, big_col, retail_col = None, None, None
        for c in target_df.columns:
            col_text = "".join(target_df[c].head(3).astype(str).tolist())
            if '日期' in col_text: date_col = c
            if '1000' in col_text and ('%' in col_text or '比' in col_text): big_col = c
            if '10' in col_text and '100' not in col_text and ('%' in col_text or '比' in col_text): retail_col = c

        if not (date_col and big_col and retail_col): return None

        clean_df = target_df[[date_col, big_col, retail_col]].copy()
        clean_df.columns = ['Date', 'Big_Holder', 'Retail_Holder']
        clean_df = clean_df.dropna()
        
        clean_df['Date'] = clean_df['Date'].astype(str).str.replace(r'[/\\-]', '', regex=True).str.strip()
        clean_df = clean_df[clean_df['Date'].str.startswith('20')] 
        clean_df['Big_Holder'] = pd.to_numeric(clean_df['Big_Holder'].astype(str).str.replace('%', ''), errors='coerce')
        clean_df['Retail_Holder'] = pd.to_numeric(clean_df['Retail_Holder'].astype(str).str.replace('%', ''), errors='coerce')
        
        clean_df['Date'] = pd.to_datetime(clean_df['Date'], format='%Y%m%d', errors='coerce')
        clean_df = clean_df.dropna(subset=['Date']).sort_values('Date').set_index('Date')
        clean_df.index = clean_df.index.normalize()
        return clean_df
    except: return None

# ==========================================
# 🎨 圖表一：主圖 (美股 K 線 / 台股 K 線 + 大戶散戶) 
# ==========================================
def generate_main_chart(stock_id, chart_type="K"):
    try:
        is_tw = stock_id.isdigit()
        ticker = f"{stock_id}.TW" if is_tw else stock_id
        stock = yf.Ticker(ticker)
        
        mc = mpf.make_marketcolors(up='#ff4d4d', down='#00b300', inherit=True)
        s  = mpf.make_mpf_style(marketcolors=mc)
        buf = io.BytesIO()

        if chart_type == "走":
            df_plot = stock.history(period="5d", interval="5m")[cite: 26]
            df_plot = df_plot.dropna(subset=['Close'])
            if not df_plot.empty:
                df_plot.index = df_plot.index.tz_localize(None)
                df_plot = df_plot[df_plot.index.date == df_plot.index[-1].date()]
                mpf.plot(df_plot, type='line', volume=False, style=s, title=f"{stock_id} 5m Intraday", savefig=buf)
                return upload_imgbb(buf)
            return None

        # 📊 K 線圖 (3個月)
        df_full = stock.history(period='4mo')
        if df_full.empty: return None
        cutoff = df_full.index.max() - pd.DateOffset(months=3)
        df_plot = df_full[df_full.index >= cutoff].copy()
        df_plot.index = df_plot.index.tz_localize(None).normalize()
        ap = []

        # 🇹🇼 台股專屬：加入大戶與散戶 (動態對焦 Y 軸)
        if is_tw:
            df_chip = get_chip_from_pyramid(stock_id)
            if df_chip is not None and not df_chip.empty:
                # 計算顏色差異
                df_chip['Big_Diff'] = df_chip['Big_Holder'].diff()
                df_chip['Retail_Diff'] = df_chip['Retail_Holder'].diff()
                df_chip['Big_Color'] = df_chip['Big_Diff'].apply(lambda x: '#ff4d4d' if x > 0 else ('#00b300' if x < 0 else '#808080'))[cite: 25]
                df_chip['Retail_Color'] = df_chip['Retail_Diff'].apply(lambda x: '#ff4d4d' if x > 0 else ('#00b300' if x < 0 else '#808080'))[cite: 25]

                df_plot = df_plot.join(df_chip, how='left')
                for col in ['Big_Holder', 'Retail_Holder', 'Big_Color', 'Retail_Color']:
                    df_plot[col] = df_plot[col].ffill().bfill()

                if not df_plot['Big_Holder'].isna().all():
                    # 利用自訂函式鎖定特定位階區間
                    big_ylim = get_focused_ylim(df_plot['Big_Holder'], 0.1)
                    ret_ylim = get_focused_ylim(df_plot['Retail_Holder'], 0.1)

                    ap.append(mpf.make_addplot(df_plot['Big_Holder'], panel=2, type='bar', color=df_plot['Big_Color'].tolist(), ylabel='Big (%)', ylim=big_ylim))
                    ap.append(mpf.make_addplot(df_plot['Retail_Holder'], panel=3, type='bar', color=df_plot['Retail_Color'].tolist(), ylabel='Retail (%)', ylim=ret_ylim))

        title_str = f"{stock_id} 3M Chart"
        panel_ratios = (6, 1.5, 1.5, 1.5) if ap else (6, 1.5)
        figratio = (10, 14) if ap else (10, 8)
        
        mpf.plot(df_plot, type='candle', volume=True, style=s, mav=(5, 10, 20), title=title_str, addplot=ap, panel_ratios=panel_ratios, figratio=figratio, tight_layout=True, savefig=buf)
        return upload_imgbb(buf)
    except: return None

# ==========================================
# 🌐 LINE Bot 路由與訊息處理
# ==========================================
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip().upper()[cite: 14]
    
    # 圖表查詢
    if user_msg.startswith("K") or user_msg.startswith("走"):
        chart_type = "K" if user_msg.startswith("K") else "走"[cite: 14]
        stock_id = user_msg[1:] 
        
        url_main = generate_main_chart(stock_id, chart_type=chart_type)
        if url_main:
            if chart_type == "K":
                msgs = [
                    TextSendMessage(text=f"✅ 已經為您繪製【{stock_id}】近 3 個月 K 線及籌碼動向圖！"), 
                    ImageSendMessage(original_content_url=url_main, preview_image_url=url_main)[cite: 14]
                ]
                line_bot_api.reply_message(event.reply_token, msgs)[cite: 14]
            else:
                line_bot_api.reply_message(event.reply_token, [
                    TextSendMessage(text=f"✅ 已經為您繪製【{stock_id}】的今日盤中走勢圖囉！"),[cite: 14]
                    ImageSendMessage(original_content_url=url_main, preview_image_url=url_main)[cite: 14]
                ])
        else: 
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="圖片產生失敗，請確認代號是否正確。"))[cite: 14]
        return
        
    # 純文字報價查詢
    result = get_quote(user_msg)
    if result:
        btns = QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="📊 K線與籌碼圖", text=f"K{user_msg}")), 
            QuickReplyButton(action=MessageAction(label="📈 5分走勢圖", text=f"走{user_msg}"))
        ])
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=result, quick_reply=btns))

# ==========================================
# 🎨 5. 圖表一：主圖 (美股 K 線 / 台股 K 線 + 大戶散戶) & 5分走勢圖
# ==========================================
def generate_main_chart(stock_id, chart_type="K"):
    try:
        is_tw = stock_id.isdigit()
        ticker = f"{stock_id}.TW" if is_tw else stock_id
        stock = yf.Ticker(ticker)
        
        mc = mpf.make_marketcolors(up='#ff4d4d', down='#00b300', inherit=True)
        s  = mpf.make_mpf_style(marketcolors=mc)
        buf = io.BytesIO()

        if chart_type == "走":
            df_plot = stock.history(period="5d", interval="5m")
            df_plot = df_plot.dropna(subset=['Close'])
            if not df_plot.empty:
                df_plot.index = df_plot.index.tz_localize(None)
                df_plot = df_plot[df_plot.index.date == df_plot.index[-1].date()]
                mpf.plot(df_plot, type='line', volume=False, style=s, title=f"{stock_id} 5m Intraday", savefig=buf)
                return upload_imgbb(buf)
            return None

        df_full = stock.history(period='4mo')
        if df_full.empty: return None
        cutoff = df_full.index.max() - pd.DateOffset(months=3)
        df_plot = df_full[df_full.index >= cutoff].copy()
        df_plot.index = df_plot.index.tz_localize(None).normalize()
        ap = []

        if is_tw:
            df_chip = get_chip_from_pyramid(stock_id)
            if df_chip is not None and not df_chip.empty:
                df_chip['Big_Diff'] = df_chip['Big_Holder'].diff()
                df_chip['Retail_Diff'] = df_chip['Retail_Holder'].diff()
                df_chip['Big_Color'] = df_chip['Big_Diff'].apply(lambda x: '#ff4d4d' if x > 0 else ('#00b300' if x < 0 else '#808080'))
                df_chip['Retail_Color'] = df_chip['Retail_Diff'].apply(lambda x: '#ff4d4d' if x > 0 else ('#00b300' if x < 0 else '#808080'))

                df_plot = df_plot.join(df_chip, how='left')
                for col in ['Big_Holder', 'Retail_Holder', 'Big_Color', 'Retail_Color']:
                    df_plot[col] = df_plot[col].ffill().bfill()

                if not df_plot['Big_Holder'].isna().all():
                    big_ylim = get_focused_ylim(df_plot['Big_Holder'], 0.1)
                    ret_ylim = get_focused_ylim(df_plot['Retail_Holder'], 0.1)

                    ap.append(mpf.make_addplot(df_plot['Big_Holder'], panel=2, type='bar', color=df_plot['Big_Color'].tolist(), ylabel='Big (%)', ylim=big_ylim))
                    ap.append(mpf.make_addplot(df_plot['Retail_Holder'], panel=3, type='bar', color=df_plot['Retail_Color'].tolist(), ylabel='Retail (%)', ylim=ret_ylim))

        title_str = f"{stock_id} 3M Chart"
        panel_ratios = (6, 1.5, 1.5, 1.5) if ap else (6, 1.5)
        figratio = (10, 14) if ap else (10, 8)
        
        mpf.plot(df_plot, type='candle', volume=True, style=s, mav=(5, 10, 20), title=title_str, addplot=ap, panel_ratios=panel_ratios, figratio=figratio, tight_layout=True, savefig=buf)
        return upload_imgbb(buf)
    except: return None

# ==========================================
# 📊 6. 圖表二：台股專屬三大法人與融資動能 (動態對焦)
# ==========================================
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
        
        df_foreign = df_inst[df_inst['name'].str.contains('外資|外陸', na=False, case=False)].groupby('date')['net'].sum().reset_index()
        df_trust = df_inst[df_inst['name'].str.contains('投信', na=False, case=False)].groupby('date')['net'].sum().reset_index()
        
        if not df_margin.empty:
            df_margin['date'] = pd.to_datetime(df_margin['date'])
            margin_col = 'MarginPurchaseTodayBalance' if 'MarginPurchaseTodayBalance' in df_margin.columns else 'margin_purchase_today_balance'
            df_margin['margin_net'] = pd.to_numeric(df_margin.get(margin_col, 0), errors='coerce').diff() / 1000
        else:
            df_margin = pd.DataFrame(columns=['date', 'margin_net'])
        
        dates = sorted(list(set(df_foreign['date'].tolist() + df_trust['date'].tolist() + df_margin['date'].tolist())))
        df_plot = pd.DataFrame({'date': dates})
        df_plot = pd.merge(df_plot, df_foreign.rename(columns={'net': 'Foreign'}), on='date', how='left')
        df_plot = pd.merge(df_plot, df_trust.rename(columns={'net': 'Trust'}), on='date', how='left')
        df_plot = pd.merge(df_plot, df_margin[['date', 'margin_net']].rename(columns={'margin_net': 'Margin'}), on='date', how='left')
        
        df_plot.fillna(0, inplace=True)
        df_plot = df_plot.tail(60) 
        
        f_ylim = get_focused_ylim(df_plot['Foreign'])
        t_ylim = get_focused_ylim(df_plot['Trust'])
        m_ylim = get_focused_ylim(df_plot['Margin'])

        fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(10, 9), sharex=True)
        x_pos = range(len(df_plot))
        
        ax1.bar(x_pos, df_plot['Foreign'], color=['#ff4d4d' if x > 0 else '#00b300' for x in df_plot['Foreign']])
        ax1.set_title("Foreign Investors Net (Lots)", loc='left', fontweight='bold')
        ax1.set_ylim(f_ylim); ax1.grid(True, alpha=0.3); ax1.axhline(0, color='black', linewidth=0.8)
        
        ax2.bar(x_pos, df_plot['Trust'], color=['#ff4d4d' if x > 0 else '#00b300' for x in df_plot['Trust']])
        ax2.set_title("Investment Trust Net (Lots)", loc='left', fontweight='bold')
        ax2.set_ylim(t_ylim); ax2.grid(True, alpha=0.3); ax2.axhline(0, color='black', linewidth=0.8)
        
        ax3.bar(x_pos, df_plot['Margin'], color=['#ff4d4d' if x > 0 else '#00b300' for x in df_plot['Margin']])
        ax3.set_title("Margin Net Change (Lots)", loc='left', fontweight='bold')
        ax3.set_ylim(m_ylim); ax3.grid(True, alpha=0.3); ax3.axhline(0, color='black', linewidth=0.8)
        
        ax3.set_xticks(x_pos[::5])
        ax3.set_xticklabels(df_plot['date'].dt.strftime('%m-%d').iloc[::5], rotation=45)
        plt.tight_layout()
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=120)
        plt.close(fig)
        return upload_imgbb(buf)
    except: return None

# ==========================================
# 🌐 7. LINE Bot 路由與訊息處理
# ==========================================
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

    if user_msg.startswith("K") or user_msg.startswith("走"):
        chart_type = "K" if user_msg.startswith("K") else "走"
        stock_id = user_msg[1:] 
        
        url_main = generate_main_chart(stock_id, chart_type=chart_type)
        if url_main:
            if chart_type == "K":
                msgs = [
                    TextSendMessage(text=f"✅ 【{stock_id}】技術與籌碼分析完成！"), 
                    ImageSendMessage(original_content_url=url_main, preview_image_url=url_main)
                ]
                if stock_id.isdigit():
                    url_inst = generate_inst_margin_chart(stock_id)
                    if url_inst: msgs.append(ImageSendMessage(original_content_url=url_inst, preview_image_url=url_inst))
                line_bot_api.reply_message(event.reply_token, msgs)
            else:
                line_bot_api.reply_message(event.reply_token, [
                    TextSendMessage(text=f"✅ 【{stock_id}】今日 5 分鐘盤中走勢圖："),
                    ImageSendMessage(original_content_url=url_main, preview_image_url=url_main)
                ])
        else: 
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="圖片產生失敗，請確認代碼是否正確。"))
        return
        
    result = get_quote(user_msg)
    if result:
        btns = QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="📊 K線與籌碼圖", text=f"K{user_msg}")), 
            QuickReplyButton(action=MessageAction(label="📈 5分走勢圖", text=f"走{user_msg}"))
        ])
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=result, quick_reply=btns))

if __name__ == "__main__":
    # --- 啟動排程器 (鬧鐘) ---
    scheduler = BackgroundScheduler(timezone="Asia/Taipei")
    scheduler.add_job(morning_indices_report, 'cron', day_of_week='mon-fri', hour=8, minute=50)
    scheduler.start()
    
    # ⬇️ 想要在本機立刻測試推播？請把下一行開頭的 # 拿掉 ⬇️
    morning_indices_report()
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host="0.0.0.0", port=port)