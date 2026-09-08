import os, io, requests
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage

app = Flask(__name__)

# ==========================================
# 🔑 金鑰設定 (使用您成功申請的 ImgBB Key)
# ==========================================
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "請確保雲端有設定此變數")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "請確保雲端有設定此變數")
IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY", "4bc61e9d363f21433c906beb7440dd92")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

def test_draw_and_upload():
    """只畫一條最簡單的紅色折線，測試 ImgBB 上傳是否暢通"""
    try:
        # 1. 畫一張極簡測試圖
        plt.figure(figsize=(4, 3))
        plt.plot([1, 2, 3], [10, 20, 15], color='red', marker='o')
        plt.title("ImgBB Connection Test")
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png')
        plt.close()
        
        # 2. 以二進位傳送至 ImgBB
        buf.seek(0)
        files = {"image": ('test.png', buf.getvalue(), 'image/png')}
        res = requests.post("https://api.imgbb.com/1/upload", data={"key": IMGBB_API_KEY}, files=files, timeout=15)
        
        if res.status_code == 200:
            url = res.json()['data']['url']
            print(f"✅ 上傳成功：{url}", flush=True)
            return url
        else:
            print(f"❌ 上傳失敗：{res.text}", flush=True)
            return None
    except Exception as e:
        print(f"❌ 發生錯誤：{e}", flush=True)
        return None

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
    # 無論輸入什麼，都觸發圖片測試
    url = test_draw_and_upload()
    
    if url:
        line_bot_api.reply_message(event.reply_token, [
            TextSendMessage(text="✅ 圖片管線測試成功！這是系統畫出來的第一張圖："),
            ImageSendMessage(original_content_url=url, preview_image_url=url)
        ])
    else:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 圖片上傳失敗，請查看終端機 Logs 的報錯原因。"))

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)