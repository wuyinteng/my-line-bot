from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage, QuickReply, QuickReplyButton, MessageAction

import yfinance as yf
from FinMind.data import DataLoader
import datetime
from datetime import timedelta
import pandas as pd
import numpy as np
import os
import requests
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
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "8g/5K/9WQ7EiuEm16BBJ/aOjy7beli9UQS1oKoX3Jswq1iGuYxvlvT+OLpWO4ZTjRWscQlvRknxmtdioggR+rILSsd28GBtd1lbDcvPgv1VEE6yzdGScPxD/Evstgxtd6+lFTohe+R5lBjVi/+fqpQdB04t89/1O/w1cDnyilFU=")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "6394456d4596cc6aadb9c92dda96b296")
MY_USER_ID = os.environ.get("MY_USER_ID", "請替換成您的_USER_ID")

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
# 🕷️ 2. 神秘金字塔爬蟲 (防衝突極致版)
# ==========================================
def get_chip_from_pyramid(stock_id):
    url = f"https://norway.twsthr.info/StockHolders.aspx?stock={stock_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.encoding = 'utf-8'
        dfs = pd.read_html(StringIO(response.text))
        
        target_df = None
        for df in dfs:
            if len(df.columns) > 10 and '日期' in df.to_string():
                target_df = df.copy()
                break
                
        if target_df is None or target_df.empty: 
            return None
            
        # 將欄位名稱重置為數字，徹底避免「比例(%)」名稱重複導致報錯
        target_df.columns = [str(i) for i in range(len(target_df.columns))]
        
        date_col, big_col, retail_col = None, None, None
        
        # 掃描前 3 列的內容來精準定位所需欄位
        for i in range(len(target_df.columns)):
            col_name = str(i)
            # 將前3列內容合併成字串判斷
            header_text = " ".join(target_df[col_name].iloc[:3].astype(str).tolist())
            
            if '日期' in header_text:
                if date_col is None: date_col = col_name
            elif '1000' in header_text and ('%' in header_text or '比例' in header_text):
                big_col = col_name
            elif '10' in header_text and '50' not in header_text and '100' not in header_text and ('%' in header_text or '比例' in header_text):
                retail_col = col_name

        if not (date_col and big_col and retail_col):
            return None

        clean_df = target_df[[date_col, big_col, retail_col]].copy()
        clean_df.columns = ['Date', 'Big_Holder', 'Retail_Holder']
        clean_df = clean_df.dropna()
        
        # 清理字串與轉換
        clean_df['Date'] = clean_df['Date'].astype(str).str.replace(r'[/\\-]', '', regex=True).str.strip()
        clean_df = clean_df[clean_df['Date'].str.startswith('20')] 
        clean_df['Date'] = pd.to_datetime(clean_df['Date'].str[:8], format='%Y%m%d', errors='coerce')
        
        clean_df['Big_Holder'] = pd.to_numeric(clean_df['Big_Holder'].astype(str).str.replace('%', '', regex=False), errors='coerce')
        clean_df['Retail_Holder'] = pd.to_numeric(clean_df['Retail_Holder'].astype(str).str.replace('%', '', regex=False), errors='coerce')
        
        clean_df = clean_df.dropna(subset=['Date', 'Big_Holder']).sort_values('Date').tail(15)
        
        # 校正週末日期至週五，對齊交易日
        clean_df['Date'] = clean_df['Date'].apply(lambda d: d - pd.Timedelta(days=d.weekday() - 4) if d.weekday() > 4 else d)
        
        clean_df = clean_df.set_index('Date')
        clean_df.index = clean_df.index.normalize()
        return clean_df
    except Exception as e:
        print(f"Pyramid Data Error: {e}")
        return None

# ==========================================
# 📈 3. 文字現況報告 (維持全中文)
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
                tc, pc = df['close'].iloc[-1], df['close'].iloc[-2]
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
                    latest_date, level = cols[2] if len(cols)>2 else "", cols[2]
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
        url = "https://freeimage.host/api/1/upload"
        payload = {"key": "6d207e02198a847aa98d0a2a901485a5"} 
        buf.seek(0)
        files = {"source": ('chart.png', buf.getvalue(), 'image/png')}
        res = requests.post(url, data=payload, files=files, timeout=20)
        if res.status_code == 200:
            return res.json()['image']['url']
        return None
    except:
        return None

def calc_ylim(series):
    s_min, s_max = series.min(), series.max()
    rng = s_max - s_min
    if rng == 0: rng = abs(s_max) if s_max != 0 else 100
    return (s_min - rng * 0.1, s_max + rng * 0.1)

# ==========================================
# 🎨 4. 圖表一：K線與成交量圖
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
    df_plot = df_full[df_full.index >= cutoff_date].copy()
    df_plot.index = df_plot.index.tz_localize(None).normalize()
    
    mc = mpf.make_marketcolors(up='#ff4d4d', down='#00b300', inherit=True)
    buf = io.BytesIO()
    
    title_str = f"{stock_id} 3M Chart"
    mpf.plot(df_plot, type='candle', volume=True, style=mpf.make_mpf_style(marketcolors=mc), mav=(5, 10, 20),
             title=title_str, figratio=(10, 8), savefig=buf)
    return upload_imgbb(buf)

# ==========================================
# 📊 5. 圖表二：三大法人與籌碼動向圖 (優化多柱狀圖)
# ==========================================
def generate_inst_margin_chart(stock_id):
    if not stock_id.isdigit(): return None
    try:
        start_date = (datetime.datetime.now() - timedelta(days=100)).strftime('%Y-%m-%d')
        df_inst = dl.taiwan_stock_institutional_investors(stock_id=stock_id, start_date=start_date)
        df_margin = dl.taiwan_stock_margin_purchase_short_sale(stock_id=stock_id, start_date=start_date)
        
        df_chip = get_chip_from_pyramid(stock_id)
        
        if df_inst.empty and df_margin.empty and (df_chip is None or df_chip.empty): return None
        
        if not df_inst.empty:
            df_inst['date'] = pd.to_datetime(df_inst['date'])
            df_inst['buy'] = pd.to_numeric(df_inst['buy'], errors='coerce').fillna(0)
            df_inst['sell'] = pd.to_numeric(df_inst['sell'], errors='coerce').fillna(0)
            df_inst['net'] = (df_inst['buy'] - df_inst['sell']) / 1000  
            df_foreign = df_inst[df_inst['name'].str.contains('外資|外陸|Foreign', na=False, case=False)].groupby('date')['net'].sum().reset_index()
            df_trust = df_inst[df_inst['name'].str.contains('投信|Trust', na=False, case=False)].groupby('date')['net'].sum().reset_index()
        else:
            df_foreign = pd.DataFrame(columns=['date', 'net'])
            df_trust = pd.DataFrame(columns=['date', 'net'])
        
        if not df_margin.empty:
            df_margin['date'] = pd.to_datetime(df_margin['date'])
            buy_col = 'margin_purchase_buy' if 'margin_purchase_buy' in df_margin.columns else 'MarginPurchaseBuy'
            sell_col = 'margin_purchase_sell' if 'margin_purchase_sell' in df_margin.columns else 'MarginPurchaseSell'
            repay_col = 'margin_purchase_cash_repayment' if 'margin_purchase_cash_repayment' in df_margin.columns else 'MarginPurchaseCashRepayment'
            
            if buy_col in df_margin.columns:
                df_margin[buy_col] = pd.to_numeric(df_margin[buy_col], errors='coerce').fillna(0)
                df_margin[sell_col] = pd.to_numeric(df_margin[sell_col], errors='coerce').fillna(0)
                df_margin[repay_col] = pd.to_numeric(df_margin[repay_col], errors='coerce').fillna(0)
                df_margin['margin_net'] = (df_margin[buy_col] - df_margin[sell_col] - df_margin[repay_col]) / 1000
            else:
                df_margin['margin_net'] = 0
        else:
            df_margin = pd.DataFrame(columns=['date', 'margin_net'])
        
        all_dates = set(df_foreign['date'].tolist() + df_trust['date'].tolist() + df_margin['date'].tolist())
        if df_chip is not None and not df_chip.empty:
            all_dates.update(df_chip.index.tolist())
            
        dates = sorted(list(all_dates))
        df_plot = pd.DataFrame({'date': dates})
        df_plot = pd.merge(df_plot, df_foreign.rename(columns={'net': 'Foreign'}), on='date', how='left')
        df_plot = pd.merge(df_plot, df_trust.rename(columns={'net': 'Trust'}), on='date', how='left')
        df_plot = pd.merge(df_plot, df_margin[['date', 'margin_net']].rename(columns={'margin_net': 'Margin'}), on='date', how='left')
        
        if df_chip is not None and not df_chip.empty:
            df_chip_temp = df_chip.reset_index().rename(columns={'Date': 'date'})
            df_plot = pd.merge(df_plot, df_chip_temp[['date', 'Big_Holder', 'Retail_Holder']], on='date', how='left')
        else:
            df_plot['Big_Holder'] = np.nan
            df_plot['Retail_Holder'] = np.nan
            
        df_plot['Big_Holder'] = df_plot['Big_Holder'].ffill().bfill()
        df_plot['Retail_Holder'] = df_plot['Retail_Holder'].ffill().bfill()
        
        df_plot['Foreign'] = df_plot['Foreign'].fillna(0)
        df_plot['Trust'] = df_plot['Trust'].fillna(0)
        df_plot['Margin'] = df_plot['Margin'].fillna(0)
        
        df_plot = df_plot.tail(60).reset_index(drop=True)
        
        fig, (ax1, ax2, ax3, ax4) = plt.subplots(4, 1, figsize=(8, 10), sharex=True)
        x_labels = df_plot['date'].dt.strftime('%m-%d')
        x_pos = np.arange(len(df_plot))
        
        # 1. 外資
        ax1.bar(x_pos, df_plot['Foreign'], color=['#ff4d4d' if x > 0 else '#00b300' for x in df_plot['Foreign']])
        ax1.set_title("Foreign Net Buy/Sell (Lots)", loc='left', fontweight='bold')
        ax1.set_ylim(calc_ylim(df_plot['Foreign']))
        ax1.axhline(0, color='black', linewidth=0.8)
        ax1.grid(True, alpha=0.3)
        
        # 2. 投信
        ax2.bar(x_pos, df_plot['Trust'], color=['#ff4d4d' if x > 0 else '#00b300' for x in df_plot['Trust']])
        ax2.set_title("Trust Net Buy/Sell (Lots)", loc='left', fontweight='bold')
        ax2.set_ylim(calc_ylim(df_plot['Trust']))
        ax2.axhline(0, color='black', linewidth=0.8)
        ax2.grid(True, alpha=0.3)
        
        # 3. 融資
        ax3.bar(x_pos, df_plot['Margin'], color=['#ff4d4d' if x > 0 else '#00b300' for x in df_plot['Margin']])
        ax3.set_title("Margin Net Buy/Sell (Lots)", loc='left', fontweight='bold')
        ax3.set_ylim(calc_ylim(df_plot['Margin']))
        ax3.axhline(0, color='black', linewidth=0.8)
        ax3.grid(True, alpha=0.3)
        
        # 4. 籌碼金字塔 (雙Y軸多柱狀圖)
        has_chip = not df_plot['Big_Holder'].isna().all()
        if has_chip:
            valid_x = x_pos
            valid_big = df_plot['Big_Holder']
            valid_retail = df_plot['Retail_Holder']
            
            ax4_twin = ax4.twinx() # 建立右邊獨立 Y 軸，解決大戶與散戶比例懸殊的問題
            width = 0.35
            
            b1 = ax4.bar(valid_x - width/2, valid_big, width=width, label='>1000 Lots (%)', color='#ff4d4d', alpha=0.85)
            b2 = ax4_twin.bar(valid_x + width/2, valid_retail, width=width, label='<10 Lots (%)', color='#00b300', alpha=0.85)
            
            # 整合兩條 Y 軸的圖例
            lines, labels = ax4.get_legend_handles_labels()
            lines2, labels2 = ax4_twin.get_legend_handles_labels()
            ax4_twin.legend(lines + lines2, labels + labels2, loc='upper left', fontsize=9)
            
            # 各自設定 Y 軸上下限，讓趨勢都能看得清楚
            ax4.set_ylim(valid_big.min() * 0.95, valid_big.max() * 1.05)
            ax4_twin.set_ylim(valid_retail.min() * 0.95, valid_retail.max() * 1.05)
            
            ax4.set_ylabel('>1000 Lots (%)', color='#ff4d4d', fontweight='bold')
            ax4_twin.set_ylabel('<10 Lots (%)', color='#00b300', fontweight='bold')
        else:
            ax4.text(0.5, 0.5, 'No Pyramid Data', ha='center', va='center', transform=ax4.transAxes)
            
        ax4.set_title("Holders Distribution (%)", loc='left', fontweight='bold')
        ax4.grid(True, alpha=0.3)
        
        ax4.set_xticks(x_pos[::5])
        ax4.set_xticklabels(x_labels.iloc[::5], rotation=45)
        plt.tight_layout()
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=120)
        plt.close(fig)
        return upload_imgbb(buf)
    except Exception as e: 
        print(f"Chart Error: {traceback.format_exc()}")
        return None

# ==========================================
# 🚀 6. 8:50 盤前戰情總匯引擎 & Line Bot 路由
# ==========================================
def get_intraday_chart_url(ticker_symbol, title_name):
    try:
        stock = yf.Ticker(ticker_symbol)
        df = stock.history(period="1d", interval="5m")
        if df.empty: 
            # 預防某些指數（如費半）可能假日或盤後無 5m 資料，改抓日線作為備案
            df = stock.history(period="5d")
            
        if df.empty: return None
        
        df = df.dropna(subset=['Close'])
        df.index = df.index.tz_localize(None)
        
        # 若是日內資料，切換為最後一天的走勢
        if len(df) > 50:
            df = df[df.index.date == df.index[-1].date()]

        mc = mpf.make_marketcolors(up='#ff4d4d', down='#00b300', inherit=True)
        s  = mpf.make_mpf_style(marketcolors=mc)
        buf = io.BytesIO()
        
        mpf.plot(df, type='line', volume=False, style=s, title=f"{title_name} Trend", savefig=buf)
        return upload_imgbb(buf)
    except Exception as e:
        return None

def morning_all_in_one_report():
    if MY_USER_ID == "請替換成您的_USER_ID": return

    messages_to_send = []
    text_lines = ["🌅 【8:50 盤前戰情總匯】\n"]
    
    # 加入美國四大指數
    text_lines.append("🌎 美股四大指數：")
    us_targets = {"^DJI": "道瓊指數", "^GSPC": "標普500", "^IXIC": "那斯達克", "^SOX": "費城半導體"}
    for ticker, name in us_targets.items():
        try:
            df = yf.Ticker(ticker).history(period='5d') # 取5天避免碰到六日無資料報錯
            if len(df) >= 2:
                tc, pc = df['Close'].iloc[-1], df['Close'].iloc[-2]
                pp = (tc - pc) / pc * 100
                sp = "🔺" if pp > 0 else "🔻"
                text_lines.append(f"▪️ {name}: {tc:.2f} ({sp}{pp:+.2f}%)")
        except: continue

    # 加入日經、韓國指數
    text_lines.append("\n🌏 亞洲早盤現況：")
    asian_targets = {"^N225": "日經指數", "^KS11": "韓國指數"}
    for ticker, name in asian_targets.items():
        try:
            df = yf.Ticker(ticker).history(period='5d')
            if len(df) >= 2:
                tc, pc = df['Close'].iloc[-1], df['Close'].iloc[-2]
                pp = (tc - pc) / pc * 100
                sp = "🔺" if pp > 0 else "🔻"
                text_lines.append(f"▪️ {name}: {tc:.2f} ({sp}{pp:+.2f}%)")
        except: continue

    # 保留原有的台指期外資動向籌碼
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

    final_text = "\n".join(text_lines)
    messages_to_send.append(TextSendMessage(text=final_text))

    # 一次推播 日經、韓股、費半走勢圖
    charts_to_generate = [
        ("^N225", "Nikkei 225"),
        ("^KS11", "KOSPI"),
        ("^SOX", "PHLX Semiconductor")
    ]
    
    for ticker, title in charts_to_generate:
        url = get_intraday_chart_url(ticker, title)
        if url:
            messages_to_send.append(ImageSendMessage(original_content_url=url, preview_image_url=url))

    try:
        # LINE Bot 單次 push 上限剛好是 5 則訊息 (1文字 + 3圖片 = 4則，安全過關)
        line_bot_api.push_message(MY_USER_ID, messages_to_send)
    except Exception as e:
        pass

@app.route("/", methods=['GET'])
def index():
    return "LINE Bot is running!"

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
            reply_text = f"✅ 【{stock_name}】雙圖流分析完成！\n\n{quote_text}{holding_info}"
            
            messages_to_send = [TextSendMessage(text=reply_text, quick_reply=qr_buttons)]
            
            url_kline = generate_kline_vol_chart(stock_id, "K")
            if url_kline: messages_to_send.append(ImageSendMessage(original_content_url=url_kline, preview_image_url=url_kline))
            
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

if __name__ == "__main__":
    scheduler = BackgroundScheduler(timezone="Asia/Taipei")
    scheduler.add_job(morning_all_in_one_report, 'cron', day_of_week='mon-fri', hour=8, minute=50)
    scheduler.start()
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)