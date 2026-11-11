# -*- coding: utf-8 -*-
"""
pigai_autowrite_gui.py - 批改网作文自动录入工具（tkinter 图形界面版）

功能与命令行版 pigai_autowrite.py 完全一致，区别在于：
  - 作文在图形界面的文本框中粘贴/输入（完整保留空格与换行）
  - 标题在输入框中填写（正文首行 "# 标题" 也可自动识别）
  - cookie 文件可自由指定（"浏览..." 按钮选择），恢复与保存使用同一文件
  - 录入过程实时显示在日志区，界面全程不卡顿（后台线程执行）

流程：启动调试 Chrome -> 打开作文页 -> 等待手动登录 -> 分块注入标题/正文 -> 校验字数
输入方式与网页一致：CDP Input.insertText（输入法级输入，非粘贴，不触发禁止粘贴拦截）

用法：python pigai_autowrite_gui.py
注意：程序不会自动点击"提交作文"，请在浏览器中确认后自行提交。
"""

import os
import queue
import re
import sys
import threading

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk
except ImportError:
    sys.stderr.write(
        "[错误] 当前 Python 缺少 tkinter 图形库。\n"
        "       请使用系统安装的 Python（安装时勾选 tcl/tk），例如：\n"
        "       \"C:\\Users\\<用户名>\\AppData\\Local\\Programs\\Python\\Python310\\python.exe\" "
        "pigai_autowrite_gui.py\n"
        "       或命令行版：python pigai_autowrite.py\n"
    )
    sys.exit(1)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pigai_autowrite import (  # noqa: E402
    COOKIE_FILE,
    CDPClient,
    ESSAY_URL,
    URL_TEMPLATE,
    fill_body,
    fill_title,
    launch_chrome_debug,
    normalize_text,
    restore_cookies,
    save_cookies,
    split_title_body,
    wait_for_write_page,
)

# 消息队列哨兵：通知主线程重新启用按钮
ENABLE_BTN = "__enable_button__"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("批改网作文自动录入工具")
        root.geometry("920x780")
        root.minsize(800, 640)

        self.cdp = None
        self.msgq = queue.Queue()
        self.running = False

        self._build_ui()
        root.after(100, self._drain_queue)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 界面搭建 ----------------
    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}

        # 顶部：作文编号 + 作文地址（双向联动） + 标题
        rid_row = ttk.Frame(self.root)
        rid_row.pack(fill="x", **pad)
        ttk.Label(rid_row, text="作文编号:").pack(side="left")
        self.rid_var = tk.StringVar()
        ttk.Entry(rid_row, textvariable=self.rid_var, width=14).pack(
            side="left", padx=(4, 10)
        )
        ttk.Label(
            rid_row, text="（填纯数字编号，下方地址自动生成；直接改地址也会反向提取编号）",
            foreground="gray",
        ).pack(side="left")

        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="作文地址:").pack(side="left")
        self.url_var = tk.StringVar(value=ESSAY_URL)
        ttk.Entry(top, textvariable=self.url_var).pack(
            side="left", fill="x", expand=True, padx=(4, 10)
        )

        title_row = ttk.Frame(self.root)
        title_row.pack(fill="x", **pad)
        ttk.Label(title_row, text="作文标题:").pack(side="left")
        self.title_var = tk.StringVar()
        ttk.Entry(title_row, textvariable=self.title_var, width=30).pack(
            side="left", padx=(4, 10)
        )
        ttk.Label(
            title_row, text="（留空时自动识别正文首行 \"# 标题\"）", foreground="gray"
        ).pack(side="left")

        # cookie 文件行
        cookie_row = ttk.Frame(self.root)
        cookie_row.pack(fill="x", **pad)
        ttk.Label(cookie_row, text="Cookie 文件:").pack(side="left")
        self.cookie_var = tk.StringVar(value=COOKIE_FILE)
        ttk.Entry(cookie_row, textvariable=self.cookie_var).pack(
            side="left", fill="x", expand=True, padx=(4, 6)
        )
        ttk.Button(cookie_row, text="浏览...", command=self._browse_cookie).pack(side="left")
        ttk.Label(
            cookie_row, text="（含登录凭据，请勿外传）", foreground="gray"
        ).pack(side="left", padx=(6, 0))

        # 中下部：上下分割——作文区(上,权重3) / 日志区(下,权重2)，分隔条可拖动微调
        paned = ttk.PanedWindow(self.root, orient="vertical")
        paned.pack(fill="both", expand=True, padx=10, pady=4)

        # 上半：作文正文输入区 + 按钮
        # 注意打包顺序：先让按钮行用 side="bottom" 占住上栏底部，
        # 否则正文输入框(expand=True)会把按钮挤出可视区域（按钮行被裁切成 0 高度）
        essay_frame = ttk.Frame(paned)
        paned.add(essay_frame, weight=3)
        ttk.Label(essay_frame, text="作文正文（直接在下面粘贴，保留原有的空格与换行）:").pack(
            anchor="w"
        )
        btns = ttk.Frame(essay_frame)
        btns.pack(side="bottom", fill="x")
        self.start_btn = ttk.Button(
            btns, text="开始录入（先在 Chrome 里登录）", command=self.on_start
        )
        self.start_btn.pack(side="left")
        ttk.Button(btns, text="清空", command=lambda: self.essay_text.delete("1.0", "end")).pack(
            side="left", padx=8
        )
        self.count_label = ttk.Label(btns, text="", foreground="gray")
        self.count_label.pack(side="right")

        self.essay_text = scrolledtext.ScrolledText(
            essay_frame, font=("微软雅黑", 11), wrap="word", undo=True
        )
        self.essay_text.pack(fill="both", expand=True, pady=(4, 6))

        # 下半：日志区
        log_frame = ttk.Frame(paned)
        paned.add(log_frame, weight=2)
        ttk.Label(log_frame, text="运行日志:").pack(anchor="w")
        self.log_box = scrolledtext.ScrolledText(
            log_frame, font=("Consolas", 10), state="disabled", wrap="word"
        )
        self.log_box.pack(fill="both", expand=True, pady=(4, 0))

        # 正文变化 -> 实时显示字符/行数
        self.essay_text.bind("<<Modified>>", self._on_text_change)

        # 作文编号 <-> 作文地址 双向联动
        self._syncing = False
        self.rid_var.trace_add("write", self._on_rid_change)
        self.url_var.trace_add("write", self._on_url_change)

    def _on_rid_change(self, *_args):
        """输入作文编号 -> 自动生成带 rid 的作文地址。"""
        if self._syncing:
            return
        rid = self.rid_var.get().strip()
        if rid.isdigit():
            self._syncing = True
            self.url_var.set(URL_TEMPLATE.format(rid=rid))
            self._syncing = False

    def _on_url_change(self, *_args):
        """手动修改地址 -> 反向提取 rid=数字 到作文编号框。"""
        if self._syncing:
            return
        m = re.search(r"[?&]rid=(\d+)", self.url_var.get())
        if m and m.group(1) != self.rid_var.get().strip():
            self._syncing = True
            self.rid_var.set(m.group(1))
            self._syncing = False

    # ---------------- UI 工具 ----------------
    def _log(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _drain_queue(self):
        """主线程每 100ms 取一次队列：写日志、启停按钮。"""
        try:
            while True:
                msg = self.msgq.get_nowait()
                if msg == ENABLE_BTN:
                    self.start_btn.state(["!disabled"])
                    self.running = False
                else:
                    self._log(msg)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    def _on_text_change(self, _event=None):
        self.essay_text.edit_modified(False)
        content = self.essay_text.get("1.0", "end-1c")
        chars = len(content)
        lines = content.count("\n") + 1 if content else 0
        self.count_label.configure(text=f"{chars} 字符 / {lines} 行")

    def _browse_cookie(self):
        """选择 cookie 文件：可指向已备份的 cookie 文件，也可另存到新位置。"""
        path = filedialog.askopenfilename(
            title="选择 cookie 文件（JSON 格式）",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
            initialfile=os.path.basename(self.cookie_var.get() or COOKIE_FILE),
        )
        if path:
            self.cookie_var.set(path)
            self._log(f"[+] cookie 文件已选择: {path}")

    # ---------------- 主流程 ----------------
    def on_start(self):
        if self.running:
            return
        raw = self.essay_text.get("1.0", "end-1c")
        text = normalize_text(raw)
        if not text:
            messagebox.showwarning("提示", "请先粘贴作文正文！")
            return

        url = self.url_var.get().strip()
        if "pigai.org" not in url or "rid=" not in url:
            messagebox.showwarning(
                "提示", "作文地址无效：请填写作文编号（纯数字），或包含 rid 参数的完整作文页地址。"
            )
            return

        title = self.title_var.get().strip()
        body = text
        if not title:
            auto_title, body = split_title_body(text)
            if auto_title:
                self.title_var.set(auto_title)

        self.start_btn.state(["disabled"])
        self.running = True
        self._log("=" * 50)
        self._log(f"[+] 本次任务：标题='{self.title_var.get() or '(不修改)'}'，正文 {len(body)} 字符")
        threading.Thread(target=self._work, args=(self.title_var.get().strip(), body), daemon=True).start()

    def _work(self, title, body):
        """后台线程：启动浏览器 -> 等登录 -> 录入 -> 校验。"""
        try:
            launch_chrome_debug(self.url_var.get().strip(), log=self.msgq.put)

            self.cdp = CDPClient()
            page = self.cdp.attach("pigai.org")
            self.msgq.put(f"[+] 已连接页面: {page.get('title', '')}")

            # 读取界面上的 cookie 文件路径（留空则不读写 cookie）
            cookie_file = self.cookie_var.get().strip() or None

            # 尝试 cookie 免登录，进入写作页后刷新保存 cookie
            restore_cookies(self.cdp, log=self.msgq.put, cookie_file=cookie_file)
            wait_for_write_page(self.cdp, log=self.msgq.put)
            save_cookies(self.cdp, log=self.msgq.put, cookie_file=cookie_file)

            fill_title(self.cdp, title, log=self.msgq.put)
            self.msgq.put("[+] 开始输入正文...")
            info = fill_body(self.cdp, body, log=self.msgq.put)

            # 校验
            self.msgq.put("-" * 50)
            self.msgq.put(f"    正文字符数  : {info['len']}（本地发送 {len(body)}）")
            self.msgq.put(f"    页面字数统计: {info['wordCount']} 词")
            self.msgq.put(f"    开头校验    : {info['head'][:40]}...")
            self.msgq.put(f"    结尾校验    : ...{info['tail'][-40:]}")
            if info["len"] != len(body):
                self.msgq.put("[!] 警告：页面字符数与发送不一致，请到浏览器中人工核对！")
            self.msgq.put("[完成] 请在浏览器中确认内容后，自行点击\"提交作文\"。")
        except Exception as e:
            self.msgq.put(f"[错误] {e}")
        finally:
            if self.cdp:
                try:
                    self.cdp.close()
                except Exception:
                    pass
                self.cdp = None
            self.msgq.put(ENABLE_BTN)

    def _on_close(self):
        if self.running:
            if not messagebox.askyesno("退出", "任务正在进行中，确定要退出吗？"):
                return
        if self.cdp:
            try:
                self.cdp.close()
            except Exception:
                pass
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")  # Windows 原生观感，失败则用默认主题
    except Exception:
        pass
    App(root)
    root.mainloop()
