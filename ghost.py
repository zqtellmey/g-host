import os, re, time, json, requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from seleniumwire import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# ================= 配置区 =================
EMAIL = os.getenv("GREATHOST_EMAIL", "")
PASSWORD = os.getenv("GREATHOST_PASSWORD", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TARGET_NAME = os.getenv("TARGET_NAME", "myserver1")
# 获取 YML 传进来的代理地址
PROXY_ENV = os.getenv("PROXY_SOCKS5") 

STATUS_MAP = {
    "running": ["🟢", "Running"],
    "starting": ["🟡", "Starting"],
    "stopped": ["🔴", "Stopped"],
    "offline": ["⚪", "Offline"],
    "suspended": ["🚫", "Suspended"]
}

def now_shanghai():
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime('%Y/%m/%d %H:%M:%S')

def calculate_hours(date_str):
    try:
        if not date_str: return 0
        clean = re.sub(r'\.\d+Z$', 'Z', date_str)
        expiry = datetime.fromisoformat(clean.replace('Z', '+00:00'))
        diff = (expiry - datetime.now(timezone.utc)).total_seconds() / 3600
        return max(0, int(diff))
    except Exception as e:
        print(f"⚠️ 时间解析失败: {e}")
        return 0

def send_notice(kind, fields):
    titles = {
        "renew_success": "🎉 <b>GreatHost 续期成功</b>",
        "maxed_out": "🈵 <b>GreatHost 已达上限</b>",
        "cooldown": "⏳ <b>GreatHost 还在冷却中</b>",
        "renew_failed": "⚠️ <b>GreatHost 续期未生效</b>",
        "error": "🚨 <b>GreatHost 脚本报错</b>"
    }
    body = "\n".join([f"{e} {k}: {v}" for e, k, v in fields])
    msg = f"{titles.get(kind, '📢 通知')}\n\n{body}\n📅 时间: {now_shanghai()}"
    
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        # TG 必须走代理，否则 GitHub 连不上
        proxies = None
        if PROXY_ENV:
            p_url = PROXY_ENV.replace("socks5://", "socks5h://")
            proxies = {"http": p_url, "https": p_url}
            
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
                proxies=proxies,
                timeout=20
            )
            print(f"✅ Telegram 通知状态: {r.status_code}")
        except Exception as e:
            print(f"❌ TG 发送异常: {e}")

class GH:
    def __init__(self):
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--ignore-certificate-errors")
        
        sw_options = {}
        if PROXY_ENV:
            # 关键：同时在 Chrome 参数和 Selenium-Wire 中注入代理
            proxy_url = PROXY_ENV.replace("socks5://", "socks5h://")
            
            # 1. 给 Chrome 核心增加代理参数 (解决 ERR_CONNECTION_CLOSED)
            opts.add_argument(f'--proxy-server={proxy_url}')
            
            # 2. 给 Selenium-Wire 增加拦截代理
            sw_options = {
                'proxy': {
                    'http': proxy_url,
                    'https': proxy_url,
                    'no_proxy': 'localhost,127.0.0.1'
                }
            }
            print(f"🔧 代理模式已开启: {proxy_url}")

        # 自动下载驱动
        service = Service(ChromeDriverManager().install())
        self.d = webdriver.Chrome(service=service, options=opts, seleniumwire_options=sw_options)
        self.w = WebDriverWait(self.d, 30)

    def api(self, url, method="GET"):
        script = f"return fetch('{url}',{{method:'{method}'}}).then(r=>r.json()).catch(e=>({{success:false,message:e.toString()}}))"
        return self.d.execute_script(script)

    def get_ip(self):
        try:
            # 增加重试逻辑，确保代理已完全建立
            for _ in range(3):
                try:
                    self.d.get("https://api.ipify.org?format=json")
                    ip = json.loads(self.d.find_element(By.TAG_NAME, "body").text).get("ip", "Unknown")
                    print(f"🌐 出口 IP: {ip}")
                    return ip
                except:
                    time.sleep(5)
            return "Retry Failed"
        except: return "Unknown"

    def login(self):
        print(f"🔑 尝试登录 GreatHost...")
        self.d.get("https://greathost.es/login")
        self.w.until(EC.presence_of_element_located((By.NAME, "email"))).send_keys(EMAIL)
        self.d.find_element(By.NAME, "password").send_keys(PASSWORD)
        self.d.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
        self.w.until(EC.url_contains("/dashboard"))

    def get_server(self):
        res = self.api("/api/servers")
        return next((s for s in res.get("servers", []) if s.get("name") == TARGET_NAME), None)

    def get_status(self, sid):
        info = self.api(f"/api/servers/{sid}/information")
        st = info.get("status", "unknown").lower()
        icon, name = STATUS_MAP.get(st, ["❓", st])
        return icon, name

    def get_renew_info(self, sid):
        data = self.api(f"/api/renewal/contracts/{sid}")
        return data.get("contract", {}).get("renewalInfo") or data.get("renewalInfo", {})

    def get_btn(self, sid):
        self.d.get(f"https://greathost.es/contracts/{sid}")
        btn = self.w.until(EC.presence_of_element_located((By.ID, "renew-free-server-btn")))
        self.w.until(lambda d: btn.text.strip() != "")
        return btn.text.strip()

    def renew(self, sid):
        return self.api(f"/api/renewal/contracts/{sid}/renew-free", "POST")

    def close(self):
        self.d.quit()

def run():
    gh = GH()
    try:
        ip = gh.get_ip()
        gh.login()
        srv = gh.get_server()
        if not srv: raise Exception(f"找不到服务器: {TARGET_NAME}")
        sid = srv["id"]
        
        icon, stname = gh.get_status(sid)
        info = gh.get_renew_info(sid)
        before = calculate_hours(info.get("nextRenewalDate"))
        btn = gh.get_btn(sid)

        if "Wait" in btn:
            m = re.search(r"Wait\s+(\d+\s+\w+)", btn)
            send_notice("cooldown", [("📛","名称",TARGET_NAME), ("⏳","等待",m.group(1) if m else btn), ("📊","累计",f"{before}h")])
            return

        res = gh.renew(sid)
        ok = res.get("success", False)
        after = calculate_hours(res.get("details", {}).get("nextRenewalDate")) if ok else before

        if ok and after > before:
            send_notice("renew_success", [("📛","名称",TARGET_NAME), ("⏰","续期",f"{before}➔{after}h"), ("🌐","IP",f"<code>{ip}</code>")])
        else:
            send_notice("renew_failed", [("📛","名称",TARGET_NAME), ("💡","提示",res.get("message","API未生效"))])
            
    except Exception as e:
        print(f"🚨 报错详情: {e}")
        send_notice("error", [("📛", "名称", TARGET_NAME), ("❌", "错误", f"<code>{str(e)[:100]}</code>")])
    finally:
        if 'gh' in locals(): gh.close()

if __name__ == "__main__":
    run()
