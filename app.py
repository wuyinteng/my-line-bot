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
from bs4 import BeautifulSoup

import warnings
warnings.filterwarnings("ignore")

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import mplfinance as mpf

app = Flask(__name__)

# ==========================================
# 1. 金鑰與初始化設定
# ==========================================
FINMIND_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoid3V5aW50ZW5nIiwiZW1haWwiOiJ3dXlpbnRlbmcxMjA2QGdtYWlsLmNvbSJ9.3-HFSvEh15UnzB4Nt_TZUYLCF7OSjrDuB31fwZ1foJA"
IMGBB_API_KEY = "4bc61e9d363f21433c906beb7440dd92"
LINE_CHANNEL_ACCESS_TOKEN = '8g/5K/9WQ7EiuEm16BBJ/aOjy7beli9UQS1oKoX3Jswq1iGuYxvlvT+OLpWO4ZTjRWscQlvRknxmtdioggR+rILSsd28GBtd1lbDcvPgv1VEE6yzdGScPxD/Evstgxtd6+lFTohe+R5lBjVi/+fqpQdB04t89/1O/w1cDnyilFU='

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler('6394456d4596cc6aadb9c92dda96b296')

dl = DataLoader()
if FINMIND_TOKEN:
    dl.login_by_token(api_token=FINMIND_TOKEN)

tw_stock_dict = {}
try:
    df_info = dl.taiwan_stock_info()
    tw_stock_dict = dict(zip(df_info['stock_id'], df_info['stock_name']))
except Exception:
    pass

# ==========================================
# 2. 文字現況報告
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
# 3. 繪製三圖流 (K線帶籌碼與高低點、三大法人)
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
    df_plot.index = df_plot.index.tz_localize(None)
    
    ap = []
    
    # 神秘金字塔爬蟲 (抓取 3 個月約 12 筆)
    if stock_id.isdigit():
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
                        
            if parsed_data:
                # 擷取最後 12 筆，符合 3 個月歷史資料的需求
                target_df = pd.DataFrame(parsed_data).drop_duplicates(subset=['date'], keep='first').sort_values('date').tail(12).set_index('date')
                target_df['Big_Color'] = target_df['Big_Holder'].diff().apply(lambda x: '#ff4d4d' if x > 0 else ('#00b300' if x < 0 else '#808080'))
                target_df['Retail_Color'] = target_df['Retail_Holder'].diff().apply(lambda x: '#ff4d4d' if x > 0 else ('#00b300' if x < 0 else '#808080'))
                
                df_plot = df_plot.join(target_df[['Big_Holder', 'Retail_Holder', 'Big_Color', 'Retail_Color']], how='left')
                df_plot['Big_Holder'] = df_plot['Big_Holder'].ffill().bfill()
                df_plot['Retail_Holder'] = df_plot['Retail_Holder'].ffill().bfill()
                df_plot['Big_Color'] = df_plot['Big_Color'].ffill().bfill()
                df_plot['Retail_Color'] = df_plot['Retail_Color'].ffill().bfill()
                
                if not df_plot['Big_Holder'].isna().all():
                    big_ylim = (df_plot['Big_Holder'].min() - 1.0, df_plot['Big_Holder'].max() + 1.0)
                    retail_ylim = (df_plot['Retail_Holder'].min() - 1.0, df_plot['Retail_Holder'].max() + 1.0)
                    ap.append(mpf.make_addplot(df_plot['Big_Holder'], panel=2, type='bar', color=df_plot['Big_Color'].tolist(), ylabel='Big (%)', ylim=big_ylim))
                    ap.append(mpf.make_addplot(df_plot['Retail_Holder'], panel=3, type='bar', color=df_plot['Retail_Color'].tolist(), ylabel='Retail (%)', ylim=retail_ylim))
        except: pass

    df_plot = df_plot.dropna(subset=['Close'])
    if df_plot.empty: return None

    mc = mpf.make_marketcolors(up='#ff4d4d', down='#00b300', inherit=True)
    s  = mpf.make_mpf_style(marketcolors=mc)
    
    panel_ratios = (6, 1.2, 1, 1) if ap else (6, 1.2)
    fig, axes = mpf.plot(df_plot, type='candle', volume=True, style=s, mav=(5, 10, 20, 60),
                         title=f"{stock_id} 3M K-Line", addplot=ap, panel_ratios=panel_ratios, 
                         figratio=(10, 16), tight_layout=True, returnfig=True)
    
    max_idx = df_plot.index.get_loc(df_plot['High'].idxmax())
    min_idx = df_plot.index.get_loc(df_plot['Low'].idxmin())
    max_p = df_plot['High'].max()
    min_p = df_plot['Low'].min()
    
    axes[0].annotate(f'{max_p:.1f}', xy=(max_idx, max_p), xytext=(0, 15), 
                     textcoords='offset points', ha='center', color='#ff4d4d', fontweight='bold',
                     arrowprops=dict(arrowstyle='-|>', color='#ff4d4d', lw=1.5))
    
    axes[0].annotate(f'{min_p:.1f}', xy=(min_idx, min_p), xytext=(0, -20), 
                     textcoords='offset points', ha='center', color='#00b300', fontweight='bold',
                     arrowprops=dict(arrowstyle='-|>', color='#00b300', lw=1.5))

    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=120)
    plt.close(fig) 
    
    return upload_imgbb(buf)

def generate_inst_margin_charts(stock_id):
    if not stock_id.isdigit(): return []
    try:
        start_date = (datetime.datetime.now() - timedelta(days=100)).strftime('%Y-%m-%d')
        df_inst = dl.taiwan_stock_institutional_investors(stock_id=stock_id, start_date=start_date)
        df_margin = dl.taiwan_stock_margin_purchase_short_sale(stock_id=stock_id, start_date=start_date)
        
        if df_inst.empty and df_margin.empty: return []
        
        df_inst['date'] = pd.to_datetime(df_inst['date'])
        df_inst['net'] = (pd.to_numeric(df_inst['buy'], errors='coerce').fillna(0) - pd.to_numeric(df_inst['sell'], errors='coerce').fillna(0)) / 1000  
        
        df_foreign = df_inst[df_inst['name'].str.contains('外資|外陸|Foreign', na=False, case=False)].groupby('date')['net'].sum().reset_index()
        df_trust = df_inst[df_inst['name'].str.contains('投信|Trust', na=False, case=False)].groupby('date')['net'].sum().reset_index()
        
        if not df_margin.empty:
            df_margin['date'] = pd.to_datetime(df_margin['date'])
            # 將融資買進、賣出、現金償還計算淨額後，轉換為 1000 的單位 (即「張數」)
            for col in ['MarginPurchaseBuy', 'MarginPurchaseSell', 'MarginPurchaseCashRepayment']:
                df_margin[col] = pd.to_numeric(df_margin.get(col, 0), errors='coerce').fillna(0)
            df_margin['margin_net'] = (df_margin['MarginPurchaseBuy'] - df_margin['MarginPurchaseSell'] - df_margin['MarginPurchaseCashRepayment']) / 1000
        else:
            df_margin = pd.DataFrame(columns=['date', 'margin_net'])
        
        dates = sorted(list(set(df_foreign['date'].tolist() + df_trust['date'].tolist() + df_margin['date'].tolist())))
        df_plot = pd.DataFrame({'date': dates})
        df_plot = pd.merge(df_plot, df_foreign.rename(columns={'net': 'Foreign'}), on='date', how='left')
        df_plot = pd.merge(df_plot, df_trust.rename(columns={'net': 'Trust'}), on='date', how='left')
        df_plot = pd.merge(df_plot, df_margin[['date', 'margin_net']].rename(columns={'margin_net': 'Margin'}), on='date', how='left')
        
        df_plot.fillna(0, inplace=True)
        df_plot = df_plot.tail(60) 
        
        metrics = [
            ('Foreign', 'Foreign Investors Net (Lots)', calc_ylim(df_plot['Foreign'])),
            ('Trust', 'Investment Trust Net (Lots)', calc_ylim(df_plot['Trust'])),
            ('Margin', 'Margin Net Change (Lots)', calc_ylim(df_plot['Margin']))
        ]
        
        x_labels = df_plot['date'].dt.strftime('%m-%d')
        x_pos = range(len(df_plot))
        uploaded_urls = []
        
        for col, title, ylim in metrics:
            fig, ax = plt.subplots(figsize=(10, 3.5)) 
            colors = ['#ff4d4d' if x > 0 else '#00b300' for x in df_plot[col]]
            ax.bar(x_pos, df_plot[col], color=colors)
            ax.set_title(title, loc='left', fontsize=14, fontweight='bold')
            ax.axhline(0, color='black', linewidth=0.8)
            ax.set_ylim(ylim) 
            ax.grid(True, alpha=0.3)
            ax.set_xticks(x_pos[::5])
            ax.set_xticklabels(x_labels.iloc[::5], rotation=45)
            plt.tight_layout()
            
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=100)
            plt.close(fig)
            
            res = requests.post("https://api.imgbb.com/1/upload", data={"key": IMGBB_API_KEY, "image": base64.b64encode(buf.getvalue()).decode('utf-8')})
            if res.status_code == 200:
                uploaded_urls.append(res.json()['data']['url'])
                
        return uploaded_urls
    except: return []

# ==========================================
# 4. LINE Bot 路由與訊息處理
# ==========================================
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
            
            if stock_id.isdigit():
                url_inst_list = generate_inst_margin_charts(stock_id)
                for u in url_inst_list:
                    messages_to_send.append(ImageSendMessage(original_content_url=u, preview_image_url=u))
                
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
    app.run(host='0.0.0.0', port=5000)