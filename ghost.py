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

# ================= 配置区 (支持环境变量) =================
EMAIL = os.getenv("GREATHOST_EMAIL", "")
PASSWORD = os.getenv("GREATHOST_PASSWORD", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# 这里的 PROXY_URL 主要用于 Python requests 访问 TG 和 API
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
    
    print(f"📤 准备发送 TG 通知: {kind}")
    
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
                proxies=proxies,
                timeout=25
            )
            # 调试关键：打印 TG 原始回执
            print(f"📡 TG 接口回执: {r.status_code} - {r.text}")
        except Exception as e:
            print(f"❌ TG 请求物理失败 (代理/网络问题): {e}")

    # 同时更新 README.md 方便网页查看
    try:
        md = msg.replace("<b>", "**").replace("</b>", "**").replace("<code>", "`").replace("</code>", "`")
        with open("README.md", "w", encoding="utf-8") as f:
            f.write(f"# GreatHost 自动续期状态\n\n{md}\n\n> 最近更新: {now_shanghai()}")
    except: pass

class GH:
    def __init__(self):
        print("🛠️ 正在初始化浏览器引擎...")
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        # 强制 Chrome 通过 SOCKS 代理访问网页
        if PROXY_URL:
            clean_proxy = PROXY_URL.split("://")[-1]
            opts.add_argument(f'--proxy-server=socks5://{clean_proxy}')
            print(f"🔧 Chrome 代理设为: {clean_proxy}")

        service = Service(ChromeDriverManager().install())
        self.d = webdriver.Chrome(service=service, options=opts)
        self.w = WebDriverWait(self.d, 30)

    def api(self, url, method="GET"):
        print(f"📡 内部 API 调用 [{method}] {url}")
        script = f"return fetch('{url}',{{method:'{method}'}}).then(r=>r.json()).catch(e=>({{success:false,message:e.toString()}}))"
        return self.d.execute_script(script)

    def get_ip(self):
        try:
            self.d.get("https://api.ipify.org?format=json")
            ip = json.loads(self.d.find_element(By.TAG_NAME, "body").text).get("ip", "Unknown")
            print(f"🌐 当前代理落地 IP: {ip}")
            return ip
        except: return "Unknown"

    def login(self):
        print(f"🔑 尝试访问登录页面...")
        self.d.get("https://greathost.es/login")
        
        # 检查页面状态
        title = self.d.title
        print(f"📄 当前页面标题: {title}")
        if "Error" in title or "502" in title or "504" in title or "Cloudflare" in title:
            raise Exception(f"网站暂时无法访问 (标题: {title})")

        try:
            self.w.until(EC.presence_of_element_located((By.NAME, "email"))).send_keys(EMAIL)
            self.d.find_element(By.NAME, "password").send_keys(PASSWORD)
            self.d.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
            
            # 等待跳转到 dashboard
            self.w.until(EC.url_contains("/dashboard"))
            print("✅ 登录成功！")
        except:
            raise Exception(f"登录失败，可能被 Cloudflare 拦截或密码错误 (当前 URL: {self.d.current_url})")

    def get_server(self):
        data = self.api("/api/servers")
        servers = data.get("servers", [])
        return next((s for s in servers if s.get("name") == TARGET_NAME), None)

    def get_status(self, sid):
        info = self.api(f"/api/servers/{sid}/information")
        st = info.get("status", "unknown").lower()
        icon, name = STATUS_MAP.get(st, ["❓", st])
        return icon, name

    def get_renew_info(self, sid):
        data = self.api(f"/api/renewal/contracts/{sid}")
        return data.get("contract", {}).get("renewalInfo") or data.get("renewalInfo", {})

    def get_btn_text(self, sid):
        self.d.get(f"https://greathost.es/contracts/{sid}")
        btn = self.w.until(EC.presence_of_element_located((By.ID, "renew-free-server-btn")))
        self.w.until(lambda d: btn.text.strip() != "")
        return btn.text.strip()

    def close(self):
        self.d.quit()

def run():
    # 0. 基础环境预检
    if not EMAIL or not PASSWORD:
        print("❌ 错误: 环境变量 GREATHOST_EMAIL 或 PASSWORD 未设置")
        return

    gh = GH()
    try:
        ip = gh.get_ip()
        gh.login()
        
        srv = gh.get_server()
        if not srv:
            raise Exception(f"在控制面板中未找到名为 [{TARGET_NAME}] 的服务器")
        
        sid = srv["id"]
        icon, stname = gh.get_status(sid)
        status_disp = f"{icon} {stname}"
        
        info = gh.get_renew_info(sid)
        before_h = calculate_hours(info.get("nextRenewalDate"))
        
        btn = gh.get_btn_text(sid)
        print(f"🔘 按钮文字: '{btn}' | 剩余时间: {before_h}h")

        if "Wait" in btn:
            m = re.search(r"Wait\s+(\d+\s+\w+)", btn)
            send_notice("cooldown", [
                ("📛","服务器",TARGET_NAME),
                ("⏳","冷却中", m.group(1) if m else btn),
                ("📊","当前累计", f"{before_h}h"),
                ("🚀","状态", status_disp)
            ])
            return

        # 执行续期
        print("🚀 执行续期动作...")
        res = gh.api(f"/api/renewal/contracts/{sid}/renew-free", "POST")
        ok = res.get("success", False)
        msg = res.get("message", "无回执信息")
        
        after_h = calculate_hours(res.get("details", {}).get("nextRenewalDate")) if ok else before_h

        if ok and after_h > before_h:
            send_notice("renew_success", [
                ("📛","服务器",TARGET_NAME),
                ("⏰","时间增加",f"{before_h} ➔ {after_h}h"),
                ("🚀","状态",status_disp),
                ("🌐","落地IP",f"<code>{ip}</code>")
            ])
        elif "5 d" in msg or before_h > 108:
            send_notice("maxed_out", [
                ("📛","服务器",TARGET_NAME),
                ("⏰","当前余额",f"{after_h}h"),
                ("💡","提示","已达到续期上限")
            ])
        else:
            send_notice("renew_failed", [
                ("📛","服务器",TARGET_NAME),
                ("💡","回执",msg)
            ])

    except Exception as e:
        err_msg = str(e)
        print(f"🚨 运行异常: {err_msg}")
        send_notice("error", [
            ("📛", "服务器", TARGET_NAME),
            ("❌", "故障原因", f"<code>{err_msg[:200]}</code>")
        ])
    finally:
        try: gh.close()
        except: pass

if __name__ == "__main__":
    run()
