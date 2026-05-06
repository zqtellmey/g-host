import os, re, time, json, requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
# --- 修正点 1: 使用原生 webdriver 和驱动管理器 ---
from selenium import webdriver
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
PROXY_URL = os.getenv("PROXY_URL", "socks5h://127.0.0.1:10808") 
TARGET_NAME = os.getenv("TARGET_NAME", "myserver1")

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
        # 使用 socks5h 代理确保 Telegram 的请求能绕过 GitHub 封锁
        proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
                proxies=proxies,
                timeout=20
            )
            print(f"✅ Telegram 通知发送成功")
        except Exception as e:
            print(f"❌ TG 请求异常: {e}")

    try:
        md = msg.replace("<b>", "**").replace("</b>", "**").replace("<code>", "`").replace("</code>", "`")
        with open("README.md", "w", encoding="utf-8") as f:
            f.write(f"# GreatHost 自动续期状态\n\n{md}\n\n> 最近更新: {now_shanghai()}")
    except: pass

class GH:
    def __init__(self):
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        # --- 修正点 2: 显式注入代理，强制 Chrome 走 SOCKS 端口 ---
        if PROXY_URL:
            # 去掉协议头，Chrome 参数只需要 127.0.0.1:10808
            clean_proxy = PROXY_URL.split("://")[-1]
            opts.add_argument(f'--proxy-server=socks5://{clean_proxy}')
            print(f"🔧 Chrome 代理已通过参数配置: {clean_proxy}")

        # 自动下载匹配 Chrome 148 的驱动
        service = Service(ChromeDriverManager().install())
        self.d = webdriver.Chrome(service=service, options=opts)
        self.w = WebDriverWait(self.d, 30)

    def api(self, url, method="GET"):
        print(f"📡 API 调用 [{method}] {url}")
        script = f"return fetch('{url}',{{method:'{method}'}}).then(r=>r.json()).catch(e=>({{success:false,message:e.toString()}}))"
        return self.d.execute_script(script)

    def get_ip(self):
        try:
            self.d.get("https://api.ipify.org?format=json")
            ip_text = self.d.find_element(By.TAG_NAME, "body").text
            ip = json.loads(ip_text).get("ip", "Unknown")
            print(f"🌐 当前代理落地 IP: {ip}")
            return ip
        except:
            return "Unknown"

    def login(self):
        print(f"🔑 正在登录: {EMAIL[:3]}***...")
        self.d.get("https://greathost.es/login")
        self.w.until(EC.presence_of_element_located((By.NAME, "email"))).send_keys(EMAIL)
        self.d.find_element(By.NAME, "password").send_keys(PASSWORD)
        self.d.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
        self.w.until(EC.url_contains("/dashboard"))

    def get_server(self):
        servers = self.api("/api/servers").get("servers", [])
        return next((s for s in servers if s.get("name") == TARGET_NAME), None)

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
        if not srv: raise Exception(f"未找到服务器 {TARGET_NAME}")
        sid = srv["id"]
        
        icon, stname = gh.get_status(sid)
        status_disp = f"{icon} {stname}"
        info = gh.get_renew_info(sid)
        before = calculate_hours(info.get("nextRenewalDate"))
        btn = gh.get_btn(sid)

        if "Wait" in btn:
            m = re.search(r"Wait\s+(\d+\s+\w+)", btn)
            send_notice("cooldown", [
                ("📛","服务器名称",TARGET_NAME),
                ("⏳","冷却时间",m.group(1) if m else btn),
                ("📊","当前累计",f"{before}h"),
                ("🚀","服务器状态",status_disp)
            ])
            return

        res = gh.renew(sid)
        ok = res.get("success", False)
        msg = res.get("message", "无返回消息")
        after = calculate_hours(res.get("details", {}).get("nextRenewalDate")) if ok else before

        if ok and after > before:
            send_notice("renew_success", [
                ("📛","服务器名称",TARGET_NAME),
                ("⏰","增加时间",f"{before} ➔ {after}h"),
                ("🚀","服务器状态",status_disp),
                ("🌐","落地 IP",f"<code>{ip}</code>")
            ])
        else:
            send_notice("renew_failed", [
                ("📛","服务器名称",TARGET_NAME),
                ("💡","提示",msg)
            ])
    except Exception as e:
        send_notice("error", [("📛", "服务器", TARGET_NAME), ("❌", "故障", f"<code>{str(e)[:100]}</code>")])
    finally:
        if 'gh' in locals(): gh.close()

if __name__ == "__main__":
    run()
