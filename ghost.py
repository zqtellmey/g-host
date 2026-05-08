import os, re, time, json, requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# ================= 公共配置 =================
ACCOUNTS_DATA = os.getenv("ACCOUNTS_JSON", "[]") # 从环境变量读取 JSON
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PROXY_URL = os.getenv("PROXY_URL", "socks5h://127.0.0.1:10808") 

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
    except: return 0

def send_notice(kind, fields, account_email=""):
    titles = {
        "renew_success": f"🎉 <b>GreatHost 续期成功</b> ({account_email})",
        "maxed_out": f"🈵 <b>GreatHost 已达上限</b> ({account_email})",
        "cooldown": f"⏳ <b>GreatHost 冷却中</b> ({account_email})",
        "renew_failed": f"⚠️ <b>GreatHost 续期失败</b> ({account_email})",
        "error": f"🚨 <b>GreatHost 脚本报错</b> ({account_email})"
    }
    body = "\n".join([f"{e} {k}: {v}" for e, k, v in fields])
    msg = f"{titles.get(kind, '📢 通知')}\n\n{body}\n📅 时间: {now_shanghai()}"
    
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
                proxies=proxies, timeout=25
            )
            print(f"📡 TG 回执: {r.status_code}")
        except Exception as e:
            print(f"❌ TG 发送异常: {e}")

class GH:
    def __init__(self):
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        if PROXY_URL:
            clean_proxy = PROXY_URL.split("://")[-1]
            opts.add_argument(f'--proxy-server=socks5://{clean_proxy}')

        service = Service(ChromeDriverManager().install())
        self.d = webdriver.Chrome(service=service, options=opts)
        self.w = WebDriverWait(self.d, 30)

    def api(self, url, method="GET"):
        script = f"return fetch('{url}',{{method:'{method}'}}).then(r=>r.json()).catch(e=>({{success:false,message:e.toString()}}))"
        return self.d.execute_script(script)

    def get_ip(self):
        try:
            self.d.get("https://api.ipify.org?format=json")
            return json.loads(self.d.find_element(By.TAG_NAME, "body").text).get("ip", "Unknown")
        except: return "Unknown"

    def process_account(self, email, password, target):
        print(f"\n--- 正在处理账号: {email} ---")
        try:
            self.d.delete_all_cookies() # 切换账号前清理 Cookie
            self.d.get("https://greathost.es/login")
            
            # 网站状态预检
            title = self.d.title
            if any(x in title for x in ["522", "502", "Connection timed out", "Cloudflare"]):
                raise Exception(f"网站暂时无法访问 (标题: {title})")

            # 登录逻辑
            self.w.until(EC.presence_of_element_located((By.NAME, "email"))).send_keys(email)
            self.d.find_element(By.NAME, "password").send_keys(password)
            self.d.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
            self.w.until(EC.url_contains("/dashboard"))

            # 获取服务器
            srv_data = self.api("/api/servers")
            srv = next((s for s in srv_data.get("servers", []) if s.get("name") == target), None)
            if not srv:
                raise Exception(f"未找到名为 [{target}] 的服务器")

            sid = srv["id"]
            # 状态核对
            info = self.api(f"/api/renewal/contracts/{sid}/information") # 有些套件路径不同，维持你之前的可用路径
            st = info.get("status", "unknown").lower()
            icon, stname = STATUS_MAP.get(st, ["❓", st])
            
            # 检查按钮状态
            self.d.get(f"https://greathost.es/contracts/{sid}")
            btn_el = self.w.until(EC.presence_of_element_located((By.ID, "renew-free-server-btn")))
            self.w.until(lambda d: btn_el.text.strip() != "")
            btn_text = btn_el.text.strip()

            # 时间计算
            renew_data = self.api(f"/api/renewal/contracts/{sid}")
            r_info = renew_data.get("contract", {}).get("renewalInfo") or renew_data.get("renewalInfo", {})
            before_h = calculate_hours(r_info.get("nextRenewalDate"))

            if "Wait" in btn_text:
                send_notice("cooldown", [("📛","服务器",target),("📊","余额",f"{before_h}h")], email)
                return

            # 执行续期
            res = self.api(f"/api/renewal/contracts/{sid}/renew-free", "POST")
            ok = res.get("success", False)
            after_h = calculate_hours(res.get("details", {}).get("nextRenewalDate")) if ok else before_h

            if ok and after_h > before_h:
                send_notice("renew_success", [("📛","服务器",target),("⏰","增加",f"{before_h}➔{after_h}h")], email)
            else:
                msg = res.get("message", "未知原因")
                send_notice("renew_failed", [("📛","服务器",target),("💡","提示",msg)], email)

        except Exception as e:
            print(f"❌ 账号 {email} 处理异常: {e}")
            send_notice("error", [("❌","错误详情",str(e)[:200])], email)

    def close(self):
        self.d.quit()

def run():
    try:
        accounts = json.loads(ACCOUNTS_DATA)
    except:
        print("❌ 错误: ACCOUNTS_JSON 格式非法")
        return

    if not accounts:
        print("⚠️ 未配置任何账号")
        return

    gh = GH()
    ip = gh.get_ip()
    print(f"🌐 落地 IP: {ip}")

    for acc in accounts:
        gh.process_account(acc.get("email"), acc.get("password"), acc.get("target"))
        time.sleep(5) # 账号间稍微停顿

    gh.close()

if __name__ == "__main__":
    run()
