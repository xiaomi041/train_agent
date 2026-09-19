import requests
import certifi
import os
from dotenv import load_dotenv

# 加载.env配置
load_dotenv()

api_key = os.getenv("DEEPSEEK_API_KEY")
skip_ssl = os.getenv("SKIP_SSL_VERIFY", "0") == "1"

# 安全逻辑：默认使用certifi证书；仅手动开启才关闭校验
verify_option = False if skip_ssl else certifi.where()

headers = {"Authorization": f"Bearer {api_key}"}
payload = {
    "model": "deepseek-chat",
    "messages": [{"role": "user", "content": "你好"}]
}

resp = requests.post(
    "https://api.deepseek.com/v1/chat/completions",
    headers=headers,
    json=payload,
    verify=verify_option
)
print(resp.text)
