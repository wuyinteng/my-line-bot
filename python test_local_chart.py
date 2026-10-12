import yfinance as yf
import pandas as pd
import requests
import io
import datetime
import warnings

warnings.filterwarnings("ignore")

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import mplfinance as mpf

def test_local_pyramid_chart(stock_id="2330"):
    print(f"⏳ 正在向神秘金字塔抓取【{stock_id}】近 3 個月籌碼與 K 線...")
    
    # 1. 抓取 K 線資料
    stock = yf.Ticker(f"{stock_id}.TW")
    df_plot = stock.history(period='4mo')
    df_plot.index = df_plot.index.tz_localize(None)
    
    ap = []
    
    # 2. 抓取神秘金字塔資料
    try:
        url = f"https://norway.twsthr.info/StockHolders.aspx?stock={stock_id}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        res = requests.get(url, headers=headers, timeout=10)
        
        dfs = pd.read_html(io.StringIO(res.text))
        target_df = None
        
        for temp_df in dfs:
            if isinstance(temp_df.columns, pd.MultiIndex):
                temp_df.columns = [str(c[-1]) for c in temp_df.columns]
                
            cols_str = "".join(str(c) for c in temp_df.columns)
            if '日期' not in cols_str:
                for i in range(min(3, len(temp_df))):
                    if '日期' in str(temp_df.iloc[i].values):
                        temp_df.columns = temp_df.iloc[i]
                        temp_df = temp_df.iloc[i+1:]
                        break
                        
            if '1000' in temp_df.to_string() and any('日期' in str(c) for c in temp_df.columns):
                target_df = temp_df.dropna(axis=1, how='all')
                break
        
        if target_df is not None:
            date_col = next(c for c in target_df.columns if '日期' in str(c))
            big_col = next(c for c in target_df.columns if '1000' in str(c) and ('%' in str(c) or '比' in str(c)))
            small_col = next(c for c in target_df.columns if '10' in str(c) and '100' not in str(c) and ('%' in str(c) or '比' in str(c)))
            
            target_df = target_df[target_df[date_col].astype(str).str.contains(r'\d', na=False)]
            # 抓取近 15 週 (約 3 個月)
            target_df = target_df.head(15).iloc[::-1].copy()
            
            target_df['date'] = pd.to_datetime(target_df[date_col].astype(str).str.replace('/', ''), format='%Y%m%d', errors='coerce')
            target_df['Big_Holder'] = target_df[big_col].astype(str).str.replace('%', '').astype(float)
            target_df['Retail_Holder'] = target_df[small_col].astype(str).str.replace('%', '').astype(float)
            
            target_df = target_df.dropna(subset=['date']).set_index('date')
            
            # 將每週的籌碼資料對齊到每日的 K 線
            df_plot = df_plot.join(target_df[['Big_Holder', 'Retail_Holder']], how='left')
            df_plot['Big_Holder'] = df_plot['Big_Holder'].ffill().bfill()
            df_plot['Retail_Holder'] = df_plot['Retail_Holder'].ffill().bfill()
            
            if not df_plot['Big_Holder'].isna().all():
                ap.append(mpf.make_addplot(df_plot['Big_Holder'], panel=2, type='bar', color='#ff4d4d', ylabel='Big(%)'))
                ap.append(mpf.make_addplot(df_plot['Retail_Holder'], panel=3, type='bar', color='#4da6ff', ylabel='Retail(%)'))
                
    except Exception as e:
        print(f"❌ 神秘金字塔籌碼抓取失敗: {e}")

    df_plot = df_plot.dropna(subset=['Close'])
    
    # 3. 繪製圖表並儲存到本機
    mc = mpf.make_marketcolors(up='#ff4d4d', down='#00b300', inherit=True)
    s  = mpf.make_mpf_style(marketcolors=mc)
    
    title_str = f"{stock_id} 3M Chart (Local Test)"
    file_name = f"local_test_{stock_id}.png"
    
    if ap:
        mpf.plot(df_plot, type='candle', volume=True, style=s, mav=(5, 10, 20), 
                 title=title_str, addplot=ap, panel_ratios=(4, 1.2, 1.5, 1.5), figratio=(10, 13), savefig=file_name)
        print(f"✅ 成功繪製 4 層圖表！請在資料夾中查看 {file_name}")
    else:
        mpf.plot(df_plot, type='candle', volume=True, style=s, mav=(5, 10, 20), 
                 title=title_str, savefig=file_name)
        print(f"⚠️ 未能抓取到籌碼，僅繪製 K 線圖。請查看 {file_name}")

if __name__ == "__main__":
    test_local_pyramid_chart("2330")