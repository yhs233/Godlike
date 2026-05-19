#!/usr/bin/env python3

import os, sys, time, json, base64, traceback, random
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ---------- 配置 ----------
API_BASE = "https://panel.godlike.host"
LOGIN_URL = f"{API_BASE}/auth/login"
OUTPUT_DIR = Path("Godlike")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CN_TZ = timezone(timedelta(hours=8))

# ---------- 工具函数 ----------
def cn_time():
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")

def mask_email(email: str) -> str:
    if not email or "@" not in email:
        return "***"
    user, domain = email.split("@", 1)
    return f"{user[:3]}***@{domain}"

def mask_server(server_id: str) -> str:
    if not server_id or len(server_id) < 6:
        return "***"
    return f"{server_id[:3]}***{server_id[-3:]}"

def snapshot(name: str) -> str:
    return str(OUTPUT_DIR / f"{name}_{int(time.time())}.png")

def notify_tg(ok: bool, email: str = "", server: str = "",
              before: str = "", after: str = "",
              error_msg: str = "", screenshot: str = None):
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        return

    msg = "✅ 续期成功\n\n" if ok else "❌ 续期失败\n\n"
    if email:   msg += f"账号：{email}\n"
    if server:  msg += f"服务器：{server}\n"
    if ok:
        if before and after:
            msg += f"到期：{before} → {after}\n"
        elif after:
            msg += f"到期：{after}\n"
        elif before:
            msg += f"到期：{before}\n"
    else:
        if error_msg: msg += f"原因：{error_msg}\n"
        if before:    msg += f"上次到期：{before}\n"
        if after:     msg += f"现在到期：{after}\n"
    msg += "\nGodlike Host Auto Renew"

    try:
        if screenshot and Path(screenshot).exists():
            with open(screenshot, "rb") as f:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                    data={"chat_id": chat_id, "caption": msg},
                    files={"photo": f}, timeout=30)
        else:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "disable_web_page_preview": True},
                timeout=30)
        print("[INFO] TG 通知已发送", flush=True)
    except Exception as e:
        print(f"[WARN] TG 通知发送失败: {e}", flush=True)

# ---------- Secret 处理 ----------
def parse_secret(raw: str) -> Dict[str, Any]:
    parts = raw.strip().split("-----")
    if len(parts) < 2:
        raise ValueError("格式错误：最少需要 用户名/邮箱-----密码")
    user = parts[0].strip()
    pwd = parts[1].strip()
    cookies = None
    if len(parts) >= 3 and parts[2].strip():
        try:
            cookies = json.loads(base64.b64decode(parts[2].strip()).decode())
        except Exception:
            print("[WARN] Cookie 解码失败，将忽略", flush=True)
    return {"user": user, "password": pwd, "cookies": cookies}

def update_secret(name: str, value: str):
    token = os.environ.get("REPO_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print("[WARN] 缺少 REPO_TOKEN，跳过回写", flush=True)
        return
    try:
        from nacl.public import PublicKey, SealedBox
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        r = requests.get(f"https://api.github.com/repos/{repo}/actions/secrets/public-key", headers=headers, timeout=15)
        r.raise_for_status()
        pk = r.json()
        public_key = base64.b64decode(pk["key"])
        sealed = base64.b64encode(SealedBox(PublicKey(public_key)).encrypt(value.encode())).decode()
        requests.put(
            f"https://api.github.com/repos/{repo}/actions/secrets/{name}",
            headers=headers,
            json={"encrypted_value": sealed, "key_id": pk["key_id"]},
            timeout=15,
        ).raise_for_status()
        print(f"[INFO] ✅ Secret {name} 已更新", flush=True)
    except Exception as e:
        print(f"[WARN] Secret 回写失败: {e}", flush=True)

# ---------- API 交互 ----------
def api_get_servers(session: requests.Session) -> Optional[List[Dict]]:
    try:
        r = session.get(f"{API_BASE}/api/client",
                        params={"page": 1, "sort": "creation", "asc": "true"},
                        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
                        timeout=30)
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        print(f"[ERROR] 获取服务器列表失败: {e}", flush=True)
        return None

def find_free_server(servers: List[Dict]) -> Optional[Dict]:
    for srv in servers:
        if srv["attributes"].get("free"):
            return srv
    return None

def get_free_timer(session: requests.Session, uuid: str) -> Optional[str]:
    servers = api_get_servers(session)
    if servers:
        for srv in servers:
            if srv["attributes"]["uuid"] == uuid:
                return srv["attributes"]["free_timer"]
    return None

def calc_remaining(timer: str) -> str:
    if not timer:
        return "未知"
    try:
        expire = datetime.fromisoformat(timer.replace("Z", "+00:00"))
        delta = expire - datetime.now(timezone.utc)
        secs = int(delta.total_seconds())
        if secs <= 0:
            return "已过期"
        d, rem = divmod(secs, 86400)
        h, rem = divmod(rem, 3600)
        m = rem // 60
        parts = []
        if d: parts.append(f"{d}天")
        if h: parts.append(f"{h}小时")
        if m: parts.append(f"{m}分钟")
        return " ".join(parts) if parts else "<1分钟"
    except:
        return timer

# ---------- Cookie 辅助 ----------
def session_from_cookies(cookie_list: List[Dict]) -> requests.Session:
    s = requests.Session()
    for c in cookie_list:
        s.cookies.set(
            c.get("name"), c.get("value"),
            domain=c.get("domain", ".godlike.host"),
            path=c.get("path", "/"),
        )
    return s

def test_cookie_valid(session: requests.Session) -> bool:
    try:
        r = session.get(f"{API_BASE}/api/client",
                        params={"page": 1},
                        headers={"Accept": "application/json"},
                        timeout=15)
        return r.status_code == 200 and "data" in r.json()
    except:
        return False

def safe_add_cookies(page, cookies):
    valid = []
    for c in cookies:
        try:
            if isinstance(c, dict):
                name = c.get("name", "")
                value = c.get("value", "")
                domain = c.get("domain", ".godlike.host")
                path = c.get("path", "/")
                secure = c.get("secure", False)
            else:
                name = c.name
                value = c.value
                domain = c.domain or ".godlike.host"
                path = c.path or "/"
                secure = c.secure
            if not name or not value:
                continue
            valid.append({
                "name": name,
                "value": value,
                "domain": domain or ".godlike.host",
                "path": path or "/",
                "secure": bool(secure),
            })
        except Exception as e:
            print(f"[WARN] 跳过无效 Cookie: {e}", flush=True)
    if valid:
        page.context.add_cookies(valid)
        print(f"[INFO] 成功注入 {len(valid)} 个 Cookie", flush=True)
        print("[INFO] 刷新 CSRF 令牌...", flush=True)
        page.goto(API_BASE, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

# ---------- 登录 ----------
def login_with_browser(user: str, pwd: str, proxy: str = None) -> Optional[Dict]:
    print("[INFO] 🔑 启动浏览器进行密码登录...", flush=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, proxy={"server": proxy} if proxy else None)
        page = browser.new_page()
        page.set_default_timeout(30000)
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            switch = 'p:has-text("Through login/password")'
            page.locator(switch).wait_for(state="visible", timeout=10000)
            page.locator(switch).click()
            page.wait_for_timeout(2000)

            page.locator('input[name="username"]').wait_for(state="visible", timeout=15000)
            page.locator('input[name="password"]').wait_for(state="visible", timeout=15000)
            page.fill('input[name="username"]', user)
            page.fill('input[name="password"]', pwd)

            clicked = False
            for sel in ['button[type="submit"]', 'button:has-text("Login")']:
                try:
                    page.locator(sel).first.click(timeout=5000)
                    clicked = True
                    break
                except:
                    pass
            if not clicked:
                page.screenshot(path=snapshot("login_no_button"))
                return None

            page.wait_for_timeout(5000)
            if "auth/login" in page.url:
                page.screenshot(path=snapshot("login_failed"))
                return None

            cookies = page.context.cookies()
            session = session_from_cookies(cookies)
            print(f"[INFO] 🔑 密码登录成功", flush=True)
            return {"session": session, "cookies": cookies}
        except Exception as e:
            print(f"[ERROR] 浏览器登录失败: {e}", flush=True)
            page.screenshot(path=snapshot("login_exception"))
            return None
        finally:
            browser.close()

# ---------- 续期操作 ----------
def do_renewal(page, server_short_id: str, max_retries: int = 3) -> bool:
    url = f"{API_BASE}/server/{server_short_id}"
    for attempt in range(1, max_retries + 1):
        try:
            if attempt == 1:
                print("[INFO] 访问服务器页面...", flush=True)
                page.goto(url, wait_until="domcontentloaded")
            else:
                print(f"[INFO] 第{attempt}次重试，刷新页面...", flush=True)
                page.reload(wait_until="domcontentloaded")

            page.wait_for_timeout(5000)  # 多等一会儿，让框架错误消失

            # 检查是否仍然有前端错误
            error_selectors = [
                'text="An error was encountered"',
                'text="error was encountered"',
                'text="Try refreshing the page"'
            ]
            has_error = False
            for sel in error_selectors:
                loc = page.locator(sel)
                if loc.count() > 0:
                    print(f"[WARN] 检测到页面错误: {sel}，将重试...", flush=True)
                    has_error = True
                    break

            if has_error and attempt < max_retries:
                continue      # 再试下一次
            elif has_error and attempt == max_retries:
                print("[ERROR] 多次重试后页面仍存在错误", flush=True)
                page.screenshot(path=snapshot("renewal_page_error"))
                return False

            # 页面正常，等待续期按钮
            add_btn = page.locator('button:has-text("Add 90 minutes")')
            add_btn.wait_for(state="visible", timeout=30000)
            add_btn.click()
            print("[INFO] 已点击 Add 90 minutes", flush=True)

            ad_btn = page.locator('button:has-text("Watch advertisment")')
            ad_btn.wait_for(state="visible", timeout=10000)
            ad_btn.click()
            print("[INFO] 已点击 Watch advertisment", flush=True)

            print("[INFO] 等待广告 120 秒...", flush=True)
            time.sleep(120)

            # 广告结束后刷新页面，获取最新时间
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            return True

        except PlaywrightTimeoutError:
            print(f"[ERROR] 第{attempt}次：续期按钮未出现（可能页面错误或网络问题）", flush=True)
            if attempt < max_retries:
                continue
            page.screenshot(path=snapshot("renewal_not_found"))
            return False
        except Exception as e:
            print(f"[ERROR] 续期异常 (重试{attempt}): {e}", flush=True)
            if attempt < max_retries:
                continue
            page.screenshot(path=snapshot("renewal_error"))
            return False

    return False

# ---------- 单账号流程 ----------
def process_account(key: str, proxy: str = None) -> bool:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return True

    try:
        sec = parse_secret(raw)
    except Exception as e:
        print(f"[ERROR] {key} 格式错误: {e}", flush=True)
        notify_tg(False, email="", error_msg="格式错误")
        return False

    user = sec["user"]
    pwd = sec["password"]
    cookielist = sec["cookies"]
    display_user = mask_email(user)

    print(f"\n{'='*60}\n[INFO] 处理 {key} ({display_user})\n{'='*60}", flush=True)

    # 登录
    session = None
    cookie_payload = None
    if cookielist:
        s = session_from_cookies(cookielist)
        if test_cookie_valid(s):
            session = s
            cookie_payload = cookielist
            print("[INFO] 🍪 Cookie 登录成功", flush=True)
        else:
            print("[INFO] Cookie 已失效，将使用密码登录", flush=True)

    if session is None:
        res = login_with_browser(user, pwd, proxy)
        if not res:
            notify_tg(False, email=user, error_msg="密码登录失败")
            return False
        session = res["session"]
        cookie_payload = res["cookies"]
        encoded = base64.b64encode(json.dumps(cookie_payload).encode()).decode()
        update_secret(key, f"{user}-----{pwd}-----{encoded}")

    # 获取服务器
    servers = api_get_servers(session)
    if not servers:
        notify_tg(False, email=user, error_msg="无法获取服务器列表")
        return False
    srv = find_free_server(servers)
    if not srv:
        notify_tg(False, email=user, error_msg="未找到免费服务器")
        return False

    uuid = srv["attributes"]["uuid"]
    short_id = srv["attributes"]["identifier"]
    before = calc_remaining(srv["attributes"].get("free_timer"))
    print(f"服务器: {mask_server(uuid)}, 续期前剩余: {before}", flush=True)

    # 过期检测
    if before == "已过期":
        # 仍然需要打开页面截图
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, proxy={"server": proxy} if proxy else None)
            page = browser.new_page()
            safe_add_cookies(page, cookie_payload)
            page.goto(f"{API_BASE}/server/{short_id}", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            ss_path = snapshot("expired")
            page.screenshot(path=ss_path)
            browser.close()
        notify_tg(False, email=user, server=short_id, before=before,
                  error_msg="服务器已过期，无法续期", screenshot=ss_path)
        return False

    # 续期
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, proxy={"server": proxy} if proxy else None)
        page = browser.new_page()
        success_ss = None
        try:
            safe_add_cookies(page, cookie_payload)
            if not do_renewal(page, short_id):
                fail_ss = snapshot("renewal_fail")
                page.screenshot(path=fail_ss)
                notify_tg(False, email=user, server=short_id, before=before,
                          error_msg="续期点击失败", screenshot=fail_ss)
                return False

            after = calc_remaining(get_free_timer(session, uuid))
            print(f"[INFO] 续期后剩余: {after}", flush=True)

            # 截图并发送成功通知（无状态信息）
            success_ss = snapshot("success")
            page.screenshot(path=success_ss)

            notify_tg(True, email=user, server=short_id, before=before, after=after,
                      screenshot=success_ss)
            print(f"[INFO] ✅ {key} 续期成功", flush=True)
            return True

        except Exception as e:
            print(f"[ERROR] 续期流程异常: {e}", flush=True)
            traceback.print_exc()
            exc_ss = snapshot("exception")
            try:
                page.screenshot(path=exc_ss)
            except:
                pass
            notify_tg(False, email=user, server=short_id, before=before,
                      error_msg=f"脚本异常: {str(e)[:200]}", screenshot=exc_ss)
            return False
        finally:
            if success_ss is None:
                try:
                    page.screenshot(path=snapshot("final_error"))
                except:
                    pass
            browser.close()

def main():
    proxy = os.environ.get("PROXY_SERVER", "")
    if proxy:
        print(f"[INFO] 代理: {proxy}", flush=True)

    accounts = [f"GODLIKE_{i}" for i in range(1, 6)]
    all_ok = True
    for idx, acc in enumerate(accounts):
        try:
            ok = process_account(acc, proxy if proxy else None)
            if not ok:
                all_ok = False
        except Exception as e:
            print(f"[FATAL] {acc} 崩溃: {e}", flush=True)
            traceback.print_exc()
            user = ""
            try:
                raw = os.environ.get(acc, "")
                if raw:
                    parts = raw.split("-----")
                    if parts: user = parts[0].strip()
            except: pass
            notify_tg(False, email=user, error_msg=f"脚本异常: {str(e)[:200]}")
            all_ok = False
        if idx < len(accounts) - 1:
            time.sleep(random.randint(5, 15))

    if all_ok:
        print("[INFO] 🎉 所有账号处理成功", flush=True)
        sys.exit(0)
    else:
        print("[ERROR] 部分账号处理失败", flush=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
