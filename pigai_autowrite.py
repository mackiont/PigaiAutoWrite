# -*- coding: utf-8 -*-
"""
pigai_autowrite.py - 批改网作文自动录入工具

流程：
  1. 启动带调试端口(--remote-debugging-port)的 Chrome（独立用户目录，不影响日常使用的 Chrome）
  2. 打开作文写作页面，等待用户手动登录
  3. 等待用户在控制台粘贴作文（支持空格、换行、多段落，以单独一行 EOF 结束；也可用文件参数）
  4. 通过 CDP(Input.insertText) 把标题/正文逐段"输入"到页面（非粘贴，不触发禁止粘贴拦截）
  5. 校验输入结果与页面字数统计
  6. 首次登录成功后自动把批改网 cookie 存到 pigai_cookies.json，下次运行自动恢复免登录

用法：
  python pigai_autowrite.py                 # 交互式：在控制台粘贴作文
  python pigai_autowrite.py essay.txt       # 从文件读取作文
  python pigai_autowrite.py -r 3444331      # 直接用作文编号（推荐）
  python pigai_autowrite.py -c D:\\my.json  # 指定 cookie 文件（恢复+保存都用它）
  python pigai_autowrite.py --no-cookie     # 本次不读写 cookie
  python pigai_autowrite.py -u <作文页地址>  # 指定完整作文页地址（与 -r 二选一）

依赖：仅 websocket-client（首次运行自动安装）
注意：正文首行若以 "# 标题" 开头，会被识别为作文标题填入标题栏。
      程序不会自动点击"提交作文"，请确认内容后自行提交。
"""

import base64
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.request

# ========================= 配置区 =========================
DEBUG_PORT = 9222
ESSAY_URL = "https://www.pigai.org/index.php?c=v2&a=write&rid=3444331&eid="
URL_TEMPLATE = "https://www.pigai.org/index.php?c=v2&a=write&rid={rid}&eid="
CHUNK_SIZE = 200          # 每段注入的字符数
CHUNK_DELAY = 0.12        # 每段之间的间隔（秒）
WAIT_LOGIN_TIMEOUT = 600  # 等待登录的最长时间（秒）
# cookie 文件与本脚本同目录（包含登录凭据，请勿外传）
COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pigai_cookies.json")
# ==========================================================


def rid_to_url(rid):
    """作文编号 -> 写作页地址。编号必须是纯数字，否则抛 ValueError。"""
    rid = str(rid).strip()
    if not rid.isdigit():
        raise ValueError(f"作文编号只能是数字，收到: {rid!r}")
    return URL_TEMPLATE.format(rid=rid)

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


# ---------------- Chrome 启动与端口探测 ----------------

def port_open(port, host="127.0.0.1", timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_chrome():
    """按候选路径查找 Chrome 可执行文件。"""
    for p in CHROME_CANDIDATES:
        if os.path.isfile(p):
            return p
    raise FileNotFoundError(
        "未找到 Chrome，请确认安装路径，或修改脚本顶部的 CHROME_CANDIDATES 列表"
    )


def launch_chrome_debug(url, log=print):
    """启动带调试端口的 Chrome（若端口已开则复用现有实例），返回是否新启动。"""
    if port_open(DEBUG_PORT):
        log(f"[+] 检测到调试端口 {DEBUG_PORT} 已开启，复用现有 Chrome 实例")
        return False

    chrome = find_chrome()
    user_data_dir = os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "pigai_chrome_debug"
    )
    os.makedirs(user_data_dir, exist_ok=True)
    args = [
        chrome,
        f"--remote-debugging-port={DEBUG_PORT}",
        f'--user-data-dir={user_data_dir}',   # 独立目录：避免被已运行的 Chrome 接管而使调试参数失效
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]
    log(f"[+] 启动 Chrome 调试实例: {chrome}")
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 轮询等待调试端口就绪
    for _ in range(30):
        time.sleep(1)
        if port_open(DEBUG_PORT):
            log(f"[+] 调试端口 {DEBUG_PORT} 就绪")
            return True
    raise RuntimeError("Chrome 调试端口启动超时，请检查 Chrome 是否被安全软件拦截")


def http_get_json(path):
    req = urllib.request.Request(f"http://127.0.0.1:{DEBUG_PORT}{path}")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------- 极简 WebSocket 客户端（纯标准库） ----------------

class MiniWebSocket:
    """最小化的 WebSocket 客户端实现，避免第三方依赖。
    仅支持文本帧收发，满足 CDP 的 JSON 消息需求。"""

    def __init__(self, url, timeout=30):
        m = re.match(r"ws://([^:/]+):(\d+)(/.*)", url)
        if not m:
            raise ValueError(f"无效的 WebSocket 地址: {url}")
        host, port, path = m.group(1), int(m.group(2)), m.group(3)
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(handshake.encode())
        # 读取握手响应（HTTP 头以 \r\n\r\n 结束）
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        head, _, rest = buf.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n")[0]:
            raise RuntimeError(f"WebSocket 握手失败: {head[:200]}")
        self._buf = rest  # 握手响应中可能已携带部分帧数据

    def _recv_exact(self, n):
        while len(self._buf) < n:
            data = self.sock.recv(4096)
            if not data:
                raise ConnectionError("连接被关闭")
            self._buf += data
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self):
        """接收一条完整文本消息（自动处理分片帧）。"""
        payload = b""
        while True:
            b1, b2 = self._recv_exact(2)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            length = b2 & 0x7F
            if length == 126:
                (length,) = struct.unpack(">H", self._recv_exact(2))
            elif length == 127:
                (length,) = struct.unpack(">Q", self._recv_exact(8))
            if masked:  # 服务端帧一般不掩码，仅为完整性实现
                self._recv_exact(4)
                mask = self._recv_exact(0)  # 占位，实际掩码已随帧头读取
            data = self._recv_exact(length)
            payload += data
            if fin:  # 最后一个分片
                if opcode == 0x8:
                    raise ConnectionError("服务端关闭了连接")
                if opcode == 0x9:  # ping -> pong
                    self.send_raw(payload, 0xA)
                    payload = b""
                    continue
                return payload.decode("utf-8", errors="replace")

    def send_raw(self, payload, opcode=0x1):
        header = bytes([0x80 | opcode])
        mask_bit = 0x80
        length = len(payload)
        if length < 126:
            header += bytes([mask_bit | length])
        elif length < 65536:
            header += bytes([mask_bit | 126]) + struct.pack(">H", length)
        else:
            header += bytes([mask_bit | 127]) + struct.pack(">Q", length)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send_text(self, text):
        self.send_raw(text.encode("utf-8"), 0x1)

    def close(self):
        try:
            self.send_raw(b"", 0x8)
            self.sock.close()
        except OSError:
            pass


# ---------------- CDP 客户端 ----------------

class CDPClient:
    """浏览器级 CDP 客户端：attach 到指定页面后可执行 JS / 模拟输入。"""

    def __init__(self):
        ver = http_get_json("/json/version")
        self.ws = MiniWebSocket(ver["webSocketDebuggerUrl"])
        self._id = 0
        self.session_id = None

    def _send(self, method, params=None, session_id=None):
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self.ws.send_text(json.dumps(msg))
        deadline = time.time() + 30
        while time.time() < deadline:
            resp = json.loads(self.ws.recv())
            if resp.get("id") == self._id:
                if "error" in resp:
                    raise RuntimeError(f"CDP 错误 {method}: {resp['error']}")
                return resp.get("result", {})
        raise TimeoutError(f"CDP 响应超时: {method}")

    def attach(self, url_keyword):
        """attach 到 URL 包含 url_keyword 的页面；不存在则新建标签页。"""
        targets = self._send("Target.getTargets").get("targetInfos", [])
        page = next(
            (t for t in targets if t.get("type") == "page" and url_keyword in t.get("url", "")),
            None,
        )
        if page is None:
            self._send("Target.createTarget", {"url": ESSAY_URL})
            for _ in range(20):
                time.sleep(1)
                targets = self._send("Target.getTargets").get("targetInfos", [])
                page = next(
                    (t for t in targets if t.get("type") == "page" and url_keyword in t.get("url", "")),
                    None,
                )
                if page:
                    break
            if page is None:
                raise RuntimeError("未能打开作文页面")
        result = self._send("Target.attachToTarget", {"targetId": page["targetId"], "flatten": True})
        self.session_id = result["sessionId"]
        return page

    def evaluate(self, expression):
        """在目标页面执行 JS 并返回结果值。"""
        r = self._send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            session_id=self.session_id,
        )
        if r.get("exceptionDetails"):
            raise RuntimeError(f"页面 JS 异常: {json.dumps(r['exceptionDetails'], ensure_ascii=False)}")
        return r.get("result", {}).get("value")

    def insert_text(self, text):
        """以输入法级别注入文本（不经过剪贴板、不触发 paste 事件）。"""
        self._send("Input.insertText", {"text": text}, session_id=self.session_id)

    def get_all_cookies(self):
        """读取浏览器全部 cookie（含 HttpOnly）。"""
        r = self._send("Storage.getCookies", session_id=self.session_id)
        return r.get("cookies", [])

    def set_cookies(self, cookies):
        """批量写入 cookie（用于恢复登录态）。"""
        self._send("Storage.setCookies", {"cookies": cookies}, session_id=self.session_id)

    def navigate(self, url):
        """跳转页面并等待加载完成。"""
        self._send("Page.navigate", {"url": url}, session_id=self.session_id)
        for _ in range(20):
            time.sleep(0.5)
            try:
                if self.evaluate("document.readyState") == "complete":
                    return True
            except Exception:
                pass  # 导航过程中页面上下文可能暂时不可用
        return False

    def close(self):
        self.ws.close()


# ---------------- cookie 免登录 ----------------

def save_cookies(cdp, log=print, cookie_file=COOKIE_FILE):
    """确认登录态有效后，把批改网相关 cookie 保存到指定文件，供下次运行免登录。"""
    try:
        cookies = cdp.get_all_cookies()
        pigai_cookies = [c for c in cookies if "pigai" in c.get("domain", "")]
        if not pigai_cookies:
            log("[!] 未获取到批改网 cookie，跳过保存")
            return False
        with open(cookie_file, "w", encoding="utf-8") as f:
            json.dump(pigai_cookies, f, ensure_ascii=False, indent=2)
        log(f"[+] 已保存 {len(pigai_cookies)} 条 cookie 到 {cookie_file}")
        log("    下次运行将自动恢复登录态（cookie 文件含登录凭据，请勿外传）")
        return True
    except Exception as e:
        log(f"[!] cookie 保存失败（不影响本次使用）: {e}")
        return False


def restore_cookies(cdp, log=print, cookie_file=COOKIE_FILE):
    """若存在 cookie 文件则先恢复再重新打开作文页。返回是否执行了恢复。"""
    if not cookie_file or not os.path.isfile(cookie_file):
        return False
    try:
        with open(cookie_file, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        if not cookies:
            return False
        cdp.set_cookies(cookies)
        cdp.navigate(ESSAY_URL)
        log(f"[+] 已从 {cookie_file} 恢复 {len(cookies)} 条 cookie 并重新打开作文页，尝试免登录...")
        return True
    except Exception as e:
        log(f"[!] cookie 恢复失败（将需要手动登录）: {e}")
        return False


# ---------------- 作文文本读取与清洗 ----------------

def normalize_text(text):
    """清洗作文文本：统一换行符、去掉行尾空白、压缩多余空行，保留正常空格与段落。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)      # 去掉每行末尾的空格/制表符
    text = re.sub(r"\n{3,}", "\n\n", text)      # 连续多个空行压成一个空行
    text = text.strip("\n")                     # 去掉首尾换行（保留段内空格）
    return text


def split_title_body(text):
    """首行若为 '# 标题' 格式则拆出标题。"""
    lines = text.split("\n")
    if lines and lines[0].startswith("#"):
        title = lines[0].lstrip("#").strip()
        body = "\n".join(lines[1:]).strip("\n")
        return title, body
    return "", text


def read_essay_from_stdin():
    print("\n" + "=" * 60)
    print("请在下方粘贴作文正文（保留原有的空格与换行）。")
    print("粘贴完成后，另起一行输入 EOF 并回车结束。")
    print("=" * 60)
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "EOF":
            break
        lines.append(line)
    return "\n".join(lines)


def safe_chunks(text, size=CHUNK_SIZE):
    """按固定长度分块，避免在代理对（emoji 等四字节字符）中间切开。"""
    chunks = []
    i = 0
    while i < len(text):
        j = min(i + size, len(text))
        if j < len(text) and 0xD800 <= ord(text[j - 1]) <= 0xDBFF:
            j += 1  # 把低位代理并入当前块
        chunks.append(text[i:j])
        i = j
    return chunks


# ---------------- 主流程 ----------------

def wait_for_write_page(cdp, log=print):
    """等待用户完成登录、页面出现正文输入框 #contents。"""
    log("[*] 等待登录：请在弹出的 Chrome 窗口中登录批改网账号...")
    deadline = time.time() + WAIT_LOGIN_TIMEOUT
    while time.time() < deadline:
        state = cdp.evaluate(
            "(function(){const t=document.getElementById('contents');"
            "return JSON.stringify({has:t!==null&&t.offsetParent!==null,url:location.href})})()"
        )
        info = json.loads(state)
        if info["has"]:
            log(f"[+] 已进入写作页面: {info['url']}")
            return True
        time.sleep(2)
    raise TimeoutError(f"等待登录超时（{WAIT_LOGIN_TIMEOUT} 秒），请重新运行并在窗口中及时登录")


def fill_title(cdp, title, log=print):
    """把标题填入 #title 输入框：聚焦 -> 全选 -> 输入替换。"""
    if not title:
        return
    cdp.evaluate(
        "(function(){const t=document.getElementById('title');t.focus();t.select();return true})()"
    )
    cdp.insert_text(title)
    cdp.evaluate(
        "(function(){const t=document.getElementById('title');"
        "t.dispatchEvent(new Event('input',{bubbles:true}));"
        "t.dispatchEvent(new Event('change',{bubbles:true}));t.blur();return t.value})()"
    )
    log(f"[+] 标题已填入: {title}")


def fill_body(cdp, body, log=print):
    """把正文逐块注入 #contents，并触发 input/keyup 让页面字数统计更新。"""
    cdp.evaluate(
        "(function(){const t=document.getElementById('contents');t.focus();"
        "t.setSelectionRange(t.value.length,t.value.length);return true})()"
    )
    chunks = safe_chunks(body)
    for i, chunk in enumerate(chunks, 1):
        cdp.insert_text(chunk)
        time.sleep(CHUNK_DELAY)
        log(f"    输入进度 {i}/{len(chunks)} 段")

    # 触发事件，确保页面的字数统计与自动保存逻辑感知到输入
    result = cdp.evaluate(
        "(function(){const t=document.getElementById('contents');"
        "t.dispatchEvent(new Event('input',{bubbles:true}));"
        "t.dispatchEvent(new KeyboardEvent('keyup',{bubbles:true,key:'a',keyCode:65}));"
        "const wc=document.getElementById('init_wc');"
        "return JSON.stringify({len:t.value.length,"
        "head:t.value.slice(0,50),tail:t.value.slice(-50),"
        "wordCount:wc?wc.textContent:''})})()"
    )
    return json.loads(result)


def main():
    # 0. 解析命令行参数
    import argparse
    parser = argparse.ArgumentParser(
        description="批改网作文自动录入工具（CDP 模拟输入，非粘贴）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "essay", nargs="?", default=None,
        help="作文文本文件路径（省略则在控制台粘贴输入，以单独一行 EOF 结束）",
    )
    parser.add_argument(
        "-c", "--cookie", default=COOKIE_FILE,
        help="cookie 文件路径：恢复与保存都使用该文件",
    )
    parser.add_argument(
        "-u", "--url", default=ESSAY_URL, help="批改网作文写作页地址",
    )
    parser.add_argument(
        "-r", "--rid", default=None,
        help="作文编号（纯数字），填写后自动生成地址，优先于 --url",
    )
    parser.add_argument(
        "--no-cookie", action="store_true",
        help="本次不读取/保存 cookie（完全手动登录）",
    )
    args = parser.parse_args()
    cookie_file = None if args.no_cookie else args.cookie

    # 作文编号优先于完整地址
    if args.rid:
        args.url = rid_to_url(args.rid)
    print(f"[+] 作文页地址: {args.url}")

    # 1. 读取作文（文件参数优先，否则交互输入）
    if args.essay:
        with open(args.essay, "r", encoding="utf-8") as f:
            raw = f.read()
        print(f"[+] 从文件读取作文: {args.essay}")
    else:
        raw = read_essay_from_stdin()

    text = normalize_text(raw)
    if not text:
        print("[x] 作文内容为空，退出")
        sys.exit(1)
    title, body = split_title_body(text)
    print(f"[+] 文本清洗完成：标题='{title or '(不修改)'}'，正文 {len(body)} 字符")

    # 2. 启动调试 Chrome 并打开作文页
    launch_chrome_debug(args.url)

    # 3. 连接 CDP 并 attach 到页面
    cdp = CDPClient()
    try:
        page = cdp.attach("pigai.org")
        print(f"[+] 已连接页面: {page.get('title', '')}")

        # 4. 尝试 cookie 免登录，然后等待进入写作页
        restore_cookies(cdp, cookie_file=cookie_file)
        wait_for_write_page(cdp)
        save_cookies(cdp, cookie_file=cookie_file)  # 登录态已确认有效，刷新保存供下次使用

        # 5. 填入标题与正文
        fill_title(cdp, title)
        print("[+] 开始输入正文...")
        info = fill_body(cdp, body)

        # 6. 校验结果
        print("\n" + "=" * 60)
        print(f"    正文字符数  : {info['len']}（本地发送 {len(body)}）")
        print(f"    页面字数统计: {info['wordCount']} 词")
        print(f"    开头校验    : {info['head'][:40]}...")
        print(f"    结尾校验    : ...{info['tail'][-40:]}")
        if info["len"] != len(body):
            print("[!] 警告：页面字符数与发送不一致，请到浏览器中人工核对！")
        print("=" * 60)
        print("[完成] 作文已输入。程序不会自动提交，请在浏览器中确认后点击\"提交作文\"。")
    finally:
        cdp.close()


if __name__ == "__main__":
    main()
