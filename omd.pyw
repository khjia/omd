#!/usr/bin/env python3
"""omd — a Windows GUI front-end for `omp --mode rpc`.

Stdlib only (tkinter + subprocess). One window hosts several sessions as tabs;
each tab owns its own `omp --mode rpc` child process, transcript and composer,
and talks its newline-delimited JSON protocol on stdio.

Extras:
  * Ctrl+N / the "+" tab opens a new session tab in the same window,
    Ctrl+W closes the current one, Ctrl+Tab cycles.
  * Background update check (`omp update --check`) with a jittered start delay,
    a banner when a newer omp exists, and one-click `omp update`.
  * OMD_LOG=<file> appends a digest of every protocol frame.
"""

import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from collections import deque
from tkinter import filedialog, messagebox, ttk

# ---------------------------------------------------------------- palette ---

BG = "#1b1b20"
SURFACE = "#232329"
SURFACE_HI = "#2c2c34"
FG = "#e6e6ea"
MUTED = "#8f8f9b"
ACCENT = "#7aa2f7"
USER = "#9ece6a"
OK = "#7dc97d"
WARN = "#e0af68"
ERR = "#e06c75"
BORDER = "#33333c"

UI_FONT = ("Microsoft YaHei UI", 10)
UI_FONT_SM = ("Microsoft YaHei UI", 9)
MONO_FONT = ("Cascadia Mono", 9)

CREATE_NO_WINDOW = 0x08000000

APP_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "omd")
STATE_FILE = os.path.join(APP_DIR, "state.json")
KEY_FILE = os.path.join(APP_DIR, "keys.json")

# Mainstream providers: label, environment variable omp reads, key console.
PROVIDERS = [
    ("OpenAI", "OPENAI_API_KEY", "https://platform.openai.com/api-keys"),
    ("Anthropic Claude", "ANTHROPIC_API_KEY", "https://console.anthropic.com/settings/keys"),
    ("Google Gemini", "GEMINI_API_KEY", "https://aistudio.google.com/apikey"),
    ("OpenRouter", "OPENROUTER_API_KEY", "https://openrouter.ai/keys"),
    ("DeepSeek", "DEEPSEEK_API_KEY", "https://platform.deepseek.com/api_keys"),
    ("智谱 GLM (z.ai)", "ZAI_API_KEY", "https://open.bigmodel.cn/usercenter/apikeys"),
    ("Moonshot Kimi", "MOONSHOT_API_KEY", "https://platform.moonshot.cn/console/api-keys"),
    ("xAI Grok", "XAI_API_KEY", "https://console.x.ai/"),
    ("Mistral", "MISTRAL_API_KEY", "https://console.mistral.ai/api-keys"),
    ("Groq", "GROQ_API_KEY", "https://console.groq.com/keys"),
    ("MiniMax", "MINIMAX_API_KEY", "https://platform.minimaxi.com/user-center/basic-information/interface-key"),
    ("Cerebras", "CEREBRAS_API_KEY", "https://cloud.cerebras.ai/"),
    ("SiliconFlow", "SILICONFLOW_API_KEY", "https://cloud.siliconflow.cn/account/ak"),
    ("OpenCode Zen / Go", "OPENCODE_API_KEY", "https://opencode.ai/auth"),
    ("阿里云百炼 Qwen", "ALIBABA_CODING_PLAN_API_KEY", "https://bailian.console.aliyun.com/"),
    ("DeepInfra", "DEEPINFRA_API_KEY", "https://deepinfra.com/dash/api_keys"),
    ("StepFun", "STEPFUN_API_KEY", "https://platform.stepfun.com/interface-key"),
    ("Kilo", "KILO_API_KEY", "https://kilocode.ai/"),
]

VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")

APP_VERSION = "1.7.8"
RELEASES_URL = "https://github.com/khjia/omd/releases"


def semver(text):
    try:
        return tuple(int(part) for part in text.split("."))
    except (AttributeError, ValueError):
        return (0, 0, 0)


def load_keys():
    try:
        with open(KEY_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str) and v.strip()}


def save_keys(keys):
    os.makedirs(APP_DIR, exist_ok=True)
    with open(KEY_FILE, "w", encoding="utf-8") as fh:
        json.dump(keys, fh, ensure_ascii=False, indent=1)
    if os.name == "nt":
        user = os.environ.get("USERNAME") or ""
        if user:
            try:
                subprocess.run(
                    ["icacls", KEY_FILE, "/inheritance:r", "/grant:r", f"{user}:F"],
                    capture_output=True,
                    creationflags=CREATE_NO_WINDOW,
                )
            except OSError:
                pass


def merged_env(keys):
    env = dict(os.environ)
    for name, value in keys.items():
        if value:
            env[name] = value
    return env


def flat_button(parent, text, command, fg=FG, accent=False):
    bg = ACCENT if accent else SURFACE_HI
    fg_color = "#12131a" if accent else fg
    return tk.Button(
        parent,
        text=text,
        command=command,
        bg=bg,
        fg=fg_color,
        activebackground=ACCENT if accent else BORDER,
        activeforeground=fg_color,
        relief="flat",
        borderwidth=0,
        font=UI_FONT_SM,
        padx=10,
        pady=4,
        cursor="hand2",
    )


def pick_thinking_level(text, available):
    """Rough effort estimate so simple prompts stay cheap and hard ones think longer."""
    lowered = (text or "").lower()
    hard_markers = (
        "重构", "调试", "架构", "设计", "分析", "为什么", "报错", "异常", "性能", "优化",
        "算法", "推导", "证明", "排查", "定位", "迁移", "兼容", "并发", "死锁",
        "refactor", "debug", "architect", "design", "analy", "profil", "test", "bug",
    )
    score = 0
    if len(lowered) > 120:
        score += 1
    if len(lowered) > 400:
        score += 1
    if "```" in lowered:
        score += 1
    if any(marker in lowered for marker in hard_markers):
        score += 2
    if lowered.count("?") + lowered.count("？") >= 2:
        score += 1
    order = ["off", "minimal", "low", "medium", "high", "xhigh", "max"]
    if score >= 4:
        wanted = "max"
    elif score >= 2:
        wanted = "high"
    else:
        wanted = "low"
    levels = [lv for lv in (available or []) if lv in order]
    if not levels:
        return wanted
    if wanted in levels:
        return wanted
    target = order.index(wanted)
    return min(levels, key=lambda lv: abs(order.index(lv) - target))


def resource_path(name):
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


def find_omp():
    found = shutil.which("omp")
    if found:
        return found
    candidate = os.path.join(os.environ.get("LOCALAPPDATA", ""), "omp", "omp.exe")
    return candidate if os.path.exists(candidate) else None


# ----------------------------------------------------------------- updater --


class Updater:
    """Version discovery + self-update."""

    INTERVAL = 20 * 3600  # seconds between background checks
    _lock = threading.Lock()
    _state = None
    _thread = None
    _install_thread = None
    _current = None
    _dismissed = None
    _auto_started = False

    # -- persistence -------------------------------------------------------

    @classmethod
    def _load(cls):
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    @classmethod
    def _save(cls, data):
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except OSError:
            pass

    # -- process helpers ---------------------------------------------------

    @staticmethod
    def _run(args, timeout):
        exe = find_omp()
        if not exe:
            raise FileNotFoundError("找不到 omp 可执行文件")
        flags = CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.run(
            [exe] + args,
            capture_output=True,
            timeout=timeout,
            creationflags=flags,
        )
        text = (proc.stdout or b"").decode("utf-8", "replace") + (proc.stderr or b"").decode("utf-8", "replace")
        return proc.returncode, text.strip()

    @classmethod
    def current_version(cls):
        with cls._lock:
            if cls._current:
                return cls._current
        try:
            _, out = cls._run(["--version"], 30)
            match = VERSION_RE.search(out)
            version = match.group(1) if match else ""
        except (OSError, subprocess.SubprocessError):
            version = ""
        with cls._lock:
            cls._current = version
        return version

    # -- public API --------------------------------------------------------

    @classmethod
    def snapshot(cls):
        with cls._lock:
            return dict(cls._state) if cls._state else None

    @classmethod
    def dismissed_version(cls):
        with cls._lock:
            if cls._dismissed is None:
                cls._dismissed = cls._load().get("dismissed") or ""
            return cls._dismissed

    @classmethod
    def dismiss(cls, version):
        with cls._lock:
            cls._dismissed = version or ""
        data = cls._load()
        data["dismissed"] = version or ""
        data["checked_at"] = time.time()
        cls._save(data)
        with cls._lock:
            if cls._state:
                cls._state = dict(cls._state)
                cls._state["newer"] = False

    @classmethod
    def auto_check(cls):
        """One jittered check per process, at most once per INTERVAL."""
        with cls._lock:
            if cls._auto_started:
                return
            cls._auto_started = True
            cls._thread = threading.Thread(target=cls._auto_worker, daemon=True)
            cls._thread.start()

    @classmethod
    def check_now(cls):
        with cls._lock:
            cls._thread = threading.Thread(target=cls._worker, args=(True,), daemon=True)
            cls._thread.start()

    @classmethod
    def start_install(cls):
        with cls._lock:
            if cls._install_thread and cls._install_thread.is_alive():
                return
            base = dict(cls._state or {})
            base.update({"installing": True, "installed": False, "install_error": None})
            cls._state = base
            cls._install_thread = threading.Thread(target=cls._install_worker, args=(base,), daemon=True)
            cls._install_thread.start()

    # -- workers -----------------------------------------------------------

    @classmethod
    def _auto_worker(cls):
        # Jitter so a room full of windows does not check in lockstep.
        time.sleep(random.uniform(3, 25))
        persisted = cls._load()
        last = float(persisted.get("checked_at") or 0)
        if time.time() - last < cls.INTERVAL:
            latest = persisted.get("latest")
            current = persisted.get("current") or cls.current_version()
            if latest:
                cls._publish({
                    "current": current,
                    "latest": latest,
                    "newer": semver(latest) > semver(current),
                    "checked_at": last,
                })
            return
        cls._worker(False)

    @classmethod
    def _worker(cls, force):
        current = cls.current_version()
        try:
            _, out = cls._run(["update", "--check"], 90)
        except (OSError, subprocess.SubprocessError) as exc:
            cls._publish({"current": current, "error": str(exc)})
            return
        match = re.search(r"Current version:\s*(\d+\.\d+\.\d+)", out)
        if match:
            current = match.group(1)
        versions = VERSION_RE.findall(out)
        latest = max(versions, key=semver) if versions else current
        state = {
            "current": current,
            "latest": latest,
            "newer": bool(latest and current and semver(latest) > semver(current)),
            "checked_at": time.time(),
            "output": out,
        }
        cls._publish(state)
        data = cls._load()
        data.update({"checked_at": state["checked_at"], "latest": latest, "current": current})
        cls._save(data)

    @classmethod
    def _install_worker(cls, base):
        try:
            code, out = cls._run(["update"], 900)
        except (OSError, subprocess.SubprocessError) as exc:
            cls._publish_install(base, error=str(exc))
            return
        if code != 0:
            tail = out.splitlines()[-6:] if out else []
            cls._publish_install(base, error="\n".join(tail) or f"omp update 退出码 {code}")
            return
        with cls._lock:
            cls._current = None
        cls._publish_install(base, installed=True)

    @classmethod
    def _publish_install(cls, base, installed=False, error=None):
        state = dict(base)
        state.update({
            "installing": False,
            "installed": installed,
            "install_error": error,
            "newer": False if installed else base.get("newer", False),
        })
        if installed:
            state["current"] = base.get("latest") or base.get("current") or ""
        cls._publish(state)

    @classmethod
    def _publish(cls, state):
        with cls._lock:
            cls._state = dict(state)




class SelfUpdater:
    """Checks the omd GitHub release feed; no API token, no rate-limit budget."""

    INTERVAL = 24 * 3600
    _lock = threading.Lock()
    _state = None
    _started = False

    @classmethod
    def _load(cls):
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @classmethod
    def _save(cls, updates):
        data = cls._load()
        data.update(updates)
        os.makedirs(APP_DIR, exist_ok=True)
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except OSError:
            pass

    @classmethod
    def snapshot(cls):
        with cls._lock:
            return dict(cls._state) if cls._state else None

    @classmethod
    def dismissed(cls):
        return cls._load().get("omd_dismissed") or ""

    @classmethod
    def dismiss(cls, version):
        cls._save({"omd_dismissed": version or "", "omd_checked_at": time.time()})
        with cls._lock:
            if cls._state:
                cls._state = dict(cls._state)
                cls._state["newer"] = False

    @classmethod
    def check(cls, force=False):
        with cls._lock:
            if cls._started and not force:
                return
            cls._started = True
        threading.Thread(target=cls._worker, args=(force,), daemon=True).start()

    @classmethod
    def _worker(cls, force):
        persisted = cls._load()
        if not force and time.time() - float(persisted.get("omd_checked_at") or 0) < cls.INTERVAL:
            latest = persisted.get("omd_latest")
            if latest:
                cls._publish(latest, RELEASES_URL + "/tag/v" + latest)
            return
        time.sleep(random.uniform(2, 15))
        import urllib.request

        request = urllib.request.Request(
            RELEASES_URL + "/latest",
            method="HEAD",
            headers={"User-Agent": f"omd/{APP_VERSION}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                final_url = response.url
        except Exception as exc:  # offline, blocked, rate limited: stay quiet
            with cls._lock:
                cls._state = {"error": str(exc)}
            return
        # a tag such as "v1.7.0" has no word boundary before the digits
        match = re.search(r"(\d+\.\d+\.\d+)", final_url.rsplit("/", 1)[-1])
        if not match:
            return
        latest = match.group(1)
        cls._publish(latest, final_url)
        cls._save({"omd_latest": latest, "omd_checked_at": time.time()})

    @classmethod
    def _publish(cls, latest, url):
        with cls._lock:
            cls._state = {
                "current": APP_VERSION,
                "latest": latest,
                "newer": semver(latest) > semver(APP_VERSION),
                "url": url,
            }


# ------------------------------------------------------------------ backend --


class Backend:
    """Owns one `omp --mode rpc` child process and the JSONL transport."""

    def __init__(self, frames, stderr_lines, on_exit):
        self.frames = frames
        self.stderr_lines = stderr_lines
        self.on_exit = on_exit
        self.proc = None
        self._lock = threading.Lock()

    def start(self, cwd, env=None, resume=False):
        exe = find_omp()
        if not exe:
            raise FileNotFoundError("找不到 omp 可执行文件；请确认 PATH 中有 omp。")
        flags = CREATE_NO_WINDOW if os.name == "nt" else 0
        args = [exe, "--mode", "rpc"]
        if resume:
            args.append("--continue")
        self.proc = subprocess.Popen(
            args,
            cwd=cwd or None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=flags,
            env=env,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        proc = self.proc
        assert proc and proc.stdout
        for raw in iter(proc.stdout.readline, b""):
            line = raw.strip()
            if not line:
                continue
            try:
                self.frames.put(json.loads(line.decode("utf-8", "replace")))
            except json.JSONDecodeError:
                self.frames.put({"type": "_malformed"})
        code = proc.wait()
        self.on_exit(code)

    def _read_stderr(self):
        proc = self.proc
        assert proc and proc.stderr
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.stderr_lines.append(line)

    def send(self, obj):
        proc = self.proc
        if not proc or proc.poll() is not None or not proc.stdin:
            return False
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (OSError, ValueError):
                return False
        return True

    def stop(self):
        proc = self.proc
        self.proc = None
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=2.0)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass


# ---------------------------------------------------------------- transcript --


class Transcript:
    """Single Text widget; each message occupies a marked block."""

    def __init__(self, parent):
        self.frame = tk.Frame(parent, bg=BG, highlightthickness=0)
        self.text = tk.Text(
            self.frame,
            bg=BG,
            fg=FG,
            insertbackground=FG,
            selectbackground=SURFACE_HI,
            selectforeground=FG,
            font=UI_FONT,
            wrap="word",
            relief="flat",
            borderwidth=0,
            padx=18,
            pady=12,
            spacing1=2,
            spacing3=2,
            cursor="arrow",
        )
        self.scroll = ttk.Scrollbar(self.frame, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=self.scroll.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        self.scroll.grid(row=0, column=1, sticky="ns")
        self.frame.rowconfigure(0, weight=1)
        self.frame.columnconfigure(0, weight=1)

        t = self.text
        t.tag_configure("name_user", foreground=USER, font=(UI_FONT[0], 9, "bold"), spacing1=10)
        t.tag_configure("name_assistant", foreground=ACCENT, font=(UI_FONT[0], 9, "bold"), spacing1=10)
        t.tag_configure("body", foreground=FG, lmargin1=2, lmargin2=2, spacing3=6)
        t.tag_configure("thinking", foreground=MUTED, font=(UI_FONT[0], 9, "italic"), lmargin1=10, lmargin2=10)
        t.tag_configure("tool", foreground=MUTED, font=MONO_FONT, lmargin1=10, lmargin2=10)
        t.tag_configure("tool_err", foreground=ERR, font=MONO_FONT, lmargin1=10, lmargin2=10)
        t.tag_configure("meta", foreground=MUTED, font=UI_FONT_SM, spacing1=8)
        t.tag_configure("error", foreground=ERR, font=UI_FONT_SM, lmargin1=2)
        t.tag_configure("system", foreground=WARN, font=UI_FONT_SM)

        t.bind("<Key>", self._block_keys)
        t.bind("<Button-1>", self._focus_out)

    def _block_keys(self, event):
        if event.state & 0x4 and event.keysym.lower() in ("c", "a"):
            return None
        if event.keysym in ("Up", "Down", "Left", "Right", "Prior", "Next", "Home", "End"):
            return None
        return "break"

    def _focus_out(self, _event):
        self.text.focus_set()
        return None

    def at_bottom(self):
        return self.text.yview()[1] >= 0.999

    def _pin(self, was_at_bottom):
        if was_at_bottom:
            self.text.see("end")

    def clear(self):
        self.text.delete("1.0", "end")

    def add_block(self, key, name=None, name_tag="meta"):
        t = self.text
        pinned = self.at_bottom()
        t.insert("end", "\n")
        if name:
            t.insert("end", name + "\n", name_tag)
        pos = t.index("end-1c")
        t.mark_set(f"b_{key}_s", pos)
        t.mark_gravity(f"b_{key}_s", "left")
        t.mark_set(f"b_{key}_e", pos)
        t.mark_gravity(f"b_{key}_e", "right")
        self._pin(pinned)
        return key

    def has_block(self, key):
        try:
            self.text.index(f"b_{key}_s")
            return True
        except tk.TclError:
            return False

    def set_block(self, key, segments):
        t = self.text
        start_mark = f"b_{key}_s"
        end_mark = f"b_{key}_e"
        try:
            t.index(start_mark)
            t.index(end_mark)
        except tk.TclError:
            return
        pinned = self.at_bottom()
        t.delete(start_mark, end_mark)
        t.mark_set("btmp", start_mark)
        t.mark_gravity("btmp", "right")
        for text, tag in segments:
            if text:
                t.insert("btmp", text, tag)
        t.mark_set(end_mark, "btmp")
        t.mark_unset("btmp")
        self._pin(pinned)

    def append(self, text, tag):
        pinned = self.at_bottom()
        self.text.insert("end", text, tag)
        self._pin(pinned)


# ------------------------------------------------------------------- helpers --


def text_parts(content):
    if isinstance(content, str):
        return content
    out = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            out.append(block.get("text") or "")
    return "".join(out)


def tool_call_summary(block):
    name = block.get("name") or block.get("toolName") or "tool"
    args = block.get("arguments")
    if isinstance(args, dict):
        args = {k: v for k, v in args.items() if k != "i"}
        if len(args) == 1:
            args = next(iter(args.values()))
        if isinstance(args, str):
            args = args.replace("\n", " ")
    if args is None:
        return name
    if isinstance(args, str):
        raw = args
    else:
        try:
            raw = json.dumps(args, ensure_ascii=False)
        except (TypeError, ValueError):
            raw = str(args)
    raw = raw.replace("\n", " ")
    if len(raw) > 150:
        raw = raw[:150] + "…"
    return f"{name}  {raw}"


def tool_result_text(result):
    if isinstance(result, dict):
        body = text_parts(result.get("content"))
        return body or result.get("text") or ""
    if isinstance(result, list):
        return text_parts(result)
    if result is None:
        return ""
    return str(result)


def first_lines(text, limit=6):
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    lines = [ln.rstrip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln.strip()]
    if len(lines) > limit:
        lines = lines[:limit] + ["…"]
    return "\n".join(lines)


# ------------------------------------------------------------------ session --


class Session:
    """One conversation tab: its own backend process, transcript and composer."""

    POLL_MS = 30

    def __init__(self, window, cwd=None, initial_prompt=None, resume=False):
        self.window = window
        self.resume = resume
        self.history = []
        self.history_loading = False
        self.phase = ""
        self.turn_started = None
        self.spin = 0
        self._tick_running = False
        self.frames = queue.Queue()
        self.stderr_lines = deque(maxlen=200)
        self.backend = Backend(self.frames, self.stderr_lines, self._on_backend_exit)
        self.cwd = cwd or os.path.expanduser("~")
        self.seq = 0
        self.busy = False
        self.state = {}
        self.status_text = "启动中…"
        self.dirty = {}
        self.flush_scheduled = False
        self.msg_starts = set()
        self.last_msgs = {}
        self._tool_fallback = deque()
        self._tool_seq = 0
        self._closing = False
        self.preferred_pair = None      # (provider, modelId) chosen for this tab
        self.current_pair = None
        self.preferred_level = None     # thinking level chosen for this tab
        self.name = None                # user-given tab name
        self.auto_thinking = True       # pick effort per message unless overridden

        self.frame = tk.Frame(window.container, bg=BG)
        self._build()
        self._start_backend()
        if initial_prompt:
            self.window.root.after(2500, lambda: self.send_text(initial_prompt))
        self.frame.after(self.POLL_MS, self._poll)

    # -- layout ------------------------------------------------------------

    def _build(self):
        # per-session toolbar: model and thinking belong to this tab only
        bar = tk.Frame(self.frame, bg=SURFACE, padx=12, pady=6)
        bar.pack(fill="x", side="top")

        split = ttk.PanedWindow(bar, orient="horizontal")
        split.pack(fill="x", side="left", expand=True)
        model_pane = tk.Frame(split, bg=SURFACE)
        tools_pane = tk.Frame(split, bg=SURFACE)

        self.model_box = ttk.Combobox(model_pane, width=34, state="readonly")
        self.model_box.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.model_box.bind("<<ComboboxSelected>>", self._on_model_selected)
        self.model_box.set("模型加载中…")

        self.thinking_box = ttk.Combobox(tools_pane, width=8, state="readonly")
        self.thinking_box.pack(side="left", padx=(0, 10))
        self.thinking_box.bind("<<ComboboxSelected>>", self._on_thinking_selected)
        self.thinking_box.set("思考")

        tk.Checkbutton(
            tools_pane,
            text="显示思考",
            variable=self.window.show_thinking,
            command=self.redraw,
            bg=SURFACE,
            fg=MUTED,
            activebackground=SURFACE,
            activeforeground=FG,
            selectcolor=FG,
            font=UI_FONT_SM,
            borderwidth=0,
            highlightthickness=0,
        ).pack(side="left")

        self.abort_btn = flat_button(tools_pane, "停止", self.abort, fg=ERR)
        self.abort_btn.pack(side="right", padx=(6, 0))
        self.abort_btn.configure(state="disabled", disabledforeground=MUTED)
        self.reset_btn = flat_button(tools_pane, "重置会话", self.new_session)
        self.reset_btn.pack(side="right", padx=6)
        self.dir_btn = flat_button(tools_pane, "工作目录", self.choose_dir)
        self.dir_btn.pack(side="right", padx=6)

        split.add(model_pane, weight=3)
        split.add(tools_pane, weight=0)

        self.transcript = Transcript(self.frame)

        composer = tk.Frame(self.frame, bg=SURFACE, padx=12, pady=10)
        composer.pack(fill="x", side="bottom")
        self.input = tk.Text(
            composer,
            height=3,
            bg=SURFACE_HI,
            fg=FG,
            insertbackground=FG,
            font=UI_FONT,
            relief="flat",
            borderwidth=0,
            padx=12,
            pady=8,
            wrap="word",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
        )
        self.input.bind("<Return>", self._on_return)
        self.input.bind("<Control-Return>", self._on_return)

        send = tk.Button(
            composer,
            text="发送  ⏎",
            command=self.send,
            bg=ACCENT,
            fg="#12131a",
            activebackground=ACCENT,
            activeforeground="#12131a",
            relief="flat",
            borderwidth=0,
            font=(UI_FONT[0], 10, "bold"),
            padx=10,
            pady=4,
            cursor="hand2",
        )
        # fixed controls claim their space first; the stretches shrink instead
        send.pack(side="right", padx=(10, 0), ipady=10, ipadx=8)
        self.input.pack(side="left", fill="both", expand=True)

        self.activity = tk.Label(
            self.frame, text="", bg=BG, fg=ACCENT, font=UI_FONT_SM, anchor="w", padx=18, pady=3
        )
        self.activity.pack(fill="x", side="bottom", before=composer)
        self.transcript.frame.pack(fill="both", expand=True)

        self.frame.after(150, self.input.focus_force)

    # -- backend lifecycle --------------------------------------------------

    def _start_backend(self, keep_transcript=False):
        if not keep_transcript:
            self.transcript.clear()
            self.transcript.append("omd · 输入消息开始，或在下方选择工作目录\n", "meta")
        try:
            self.backend.start(self.cwd, self.window.env(), resume=self.resume)
        except FileNotFoundError as exc:
            self.transcript.append(f"\n{exc}\n", "error")
            self.set_status("后端未启动")
            return
        self.window.refresh_session_chrome(self)

    def _on_backend_exit(self, code):
        self.frames.put({"type": "_backend_exit", "code": code})

    def restart(self, keep_transcript=False, fresh=False):
        if fresh:
            self.resume = False
            self.history = []
        self.backend.stop()
        self._start_backend(keep_transcript)

    def close(self):
        if self._closing:
            return
        self._closing = True
        self.backend.stop()
        try:
            self.frame.destroy()
        except tk.TclError:
            pass

    # -- protocol send ------------------------------------------------------

    def _send(self, obj):
        if not self.backend.send(obj):
            self.transcript.append("\n后端已断开，无法发送。\n", "error")

    def request(self, cmd, **fields):
        self.seq += 1
        rid = f"r{self.seq}"
        payload = {"id": rid, "type": cmd}
        payload.update(fields)
        self._send(payload)
        return rid

    # -- UI actions ---------------------------------------------------------

    def _on_return(self, event):
        if event.state & 0x1:  # Shift inserts a newline
            return None
        self.send()
        return "break"

    def send(self):
        text = self.input.get("1.0", "end-1c").strip()
        if text:
            self.input.delete("1.0", "end")
            self.send_text(text)
        return "break"

    def send_text(self, text):
        text = (text or "").strip()
        if not text:
            return
        self.seq += 1
        self.transcript.add_block(f"u{self.seq}", "你", "name_user")
        self.transcript.set_block(f"u{self.seq}", [(text, "body")])
        queued = self.busy
        if queued:
            self.transcript.append("\n(排队，当前回合结束后处理)", "meta")
        if self.auto_thinking:
            level = pick_thinking_level(text, self.window.thinking_levels)
            self.request("set_thinking_level", level=level)
            self.set_phase(f"思考 {level}")
        fields = {"message": text}
        if queued:
            fields["streamingBehavior"] = "followUp"
        self.request("prompt", **fields)
        if not queued:
            self.set_busy(True)
            if not (self.auto_thinking and self.phase.startswith("思考 ")):
                self.set_phase("等待模型…")

    def abort(self):
        self.request("abort")

    def new_session(self):
        self.request("new_session")

    def choose_dir(self):
        path = filedialog.askdirectory(initialdir=self.cwd, title="选择工作目录")
        if not path:
            return
        self.cwd = os.path.abspath(path)
        self.restart(fresh=True)
        self.window._style_chips()

    def apply_catalog(self):
        labels = list(self.window.model_map)
        if labels:
            self.model_box.configure(values=labels)
        label = self.window._label_for(self)
        if label:
            self.model_box.set(label)
        levels = list(self.window.thinking_levels)
        if levels and "自动" not in levels:
            levels = ["自动"] + levels
        if levels:
            self.thinking_box.configure(values=levels)
        if self.auto_thinking:
            self.thinking_box.set("自动")
        else:
            level = self.preferred_level or self.state.get("thinkingLevel")
            if level:
                self.thinking_box.set(str(level))

    def _on_model_selected(self, _event):
        pair = self.window.model_map.get(self.model_box.get())
        if not pair:
            return
        self.preferred_pair = pair
        self.request("set_model", provider=pair[0], modelId=pair[1])
        self.transcript.append(f"\n模型：{pair[1]}\n", "meta")

    def _on_thinking_selected(self, _event):
        level = self.thinking_box.get()
        if not level or level == "思考":
            return
        if level == "自动":
            self.auto_thinking = True
            self.preferred_level = None
            return
        self.auto_thinking = False
        self.preferred_level = level
        self.request("set_thinking_level", level=level)

    # -- frame handling -----------------------------------------------------

    def _log(self, frame):
        path = os.environ.get("OMD_LOG")
        if not path:
            return
        digest = {
            "type": frame.get("type"),
            "id": frame.get("id"),
            "command": frame.get("command"),
            "status": frame.get("status"),
            "success": frame.get("success"),
            "method": frame.get("method"),
            "cwd": self.cwd,
        }
        if frame.get("type") == "message_update":
            digest["delta_type"] = (frame.get("assistantMessageEvent") or {}).get("type")
        if not frame.get("success") and frame.get("error"):
            digest["error"] = str(frame.get("error"))[:300]
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(digest, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _poll(self):
        if self._closing:
            return
        while True:
            try:
                frame = self.frames.get_nowait()
            except queue.Empty:
                break
            self._log(frame)
            try:
                self._handle(frame)
            except Exception as exc:  # never lose the UI thread
                sys.stderr.write(f"frame error: {exc}\n")
        try:
            self.frame.after(self.POLL_MS, self._poll)
        except tk.TclError:
            pass

    def _handle(self, f):
        t = f.get("type")

        if t == "ready":
            self.set_status("就绪")
            self.request("get_available_models")
            self.request("get_available_thinking_levels")
            self.request("get_state")
            if self.preferred_pair:
                self.request("set_model", provider=self.preferred_pair[0], modelId=self.preferred_pair[1])
            if self.preferred_level:
                self.request("set_thinking_level", level=self.preferred_level)
            if self.resume:
                self.set_status("恢复上次会话…")
                self.history_loading = True
                self.request("get_messages_page", limit=100)
            return

        if t == "response":
            self._on_response(f)
            return

        if t == "prompt_result":
            status = f.get("status")
            err = (f.get("error") or {}).get("message")
            if status == "error" and err:
                self.transcript.append(f"\n错误：{err}\n", "error")
            elif status == "aborted":
                self.transcript.append("\n已中止\n", "meta")
            self.set_busy(False)
            self.set_status("就绪")
            self.request("get_state")
            return

        if t == "session_settled":
            self.set_busy(False)
            self.set_status("就绪")
            self.request("get_state")
            return

        if t == "agent_start":
            self.set_busy(True)
            self.set_phase("思考中")
            self.set_status("思考中…")
            return

        if t == "agent_end":
            if f.get("yielded") is False:
                self.set_status("重试/压缩中…")
            return

        if t in ("message_start", "message_update", "message_end"):
            self._on_message_frame(t, f)
            return

        if t == "tool_execution_start":
            name = f.get("toolName") or f.get("name") or "tool"
            self.set_phase(f"执行 {name}")
            key = self._tool_key(f, name)
            summary = tool_call_summary({"name": name, "arguments": f.get("args")})
            self.transcript.add_block(key, f"⚙ {summary}", "tool")
            self.transcript.set_block(key, [("…", "tool")])
            return

        if t == "tool_execution_end":
            name = f.get("toolName") or f.get("name") or "tool"
            self.set_phase("整理结果")
            key = self._tool_key(f, name)
            failed = bool(f.get("isError") or f.get("error"))
            body = first_lines(tool_result_text(f.get("result")))
            if not body and f.get("error"):
                body = str(f.get("error"))
            if not self.transcript.has_block(key):
                self.transcript.add_block(key, f"↳ {name}", "tool_err" if failed else "tool")
            self.transcript.set_block(key, [(body or "完成", "tool_err" if failed else "tool")])
            return

        if t in ("auto_compaction_start", "manual_compaction_start"):
            self.set_phase("压缩上下文")
            self.set_status("压缩上下文…")
            return
        if t in ("auto_compaction_end", "manual_compaction_end"):
            self.set_status("就绪")
            return
        if t == "auto_retry_start":
            self.set_phase("重试中")
            self.set_status("重试中…")
            return
        if t == "auto_retry_end":
            self.set_status("就绪")
            return
        if t == "command_output":
            text = f.get("output") or f.get("text") or ""
            if text:
                self.transcript.append("\n" + text + "\n", "system")
            return
        if t == "extension_ui_request":
            if f.get("method") in ("select", "confirm", "input", "editor", "ask", "cancel"):
                self._send({"type": "extension_ui_response", "id": f.get("id"), "cancelled": True})
            return
        if t == "rpc_frame_error":
            self.transcript.append("\n[事件过大，已截断]\n", "meta")
            return
        if t == "_malformed":
            return
        if t == "_backend_exit":
            tail = "\n".join(list(self.stderr_lines)[-8:])
            msg = f"\n[后端已退出 code={f.get('code')}]"
            if tail:
                msg += "\n" + tail
            self.transcript.append(msg + "\n", "error")
            self.set_status("后端已退出 · 重新发送以重启")
            self.set_busy(False)
            return
        if t == "notice":
            self.transcript.append("\n" + str(f.get("message") or f) + "\n", "meta")
            return

    def _on_response(self, f):
        cmd = f.get("command")
        ok = f.get("success")
        data = f.get("data")

        if cmd == "get_messages_page":
            if ok and isinstance(data, dict):
                self.history.extend(data.get("messages") or [])
                cursor = data.get("nextCursor")
                if cursor:
                    self.request("get_messages_page", cursor=cursor, limit=100)
                else:
                    self._finish_history()
            else:
                self.resume = False
                self.history_loading = False
                self.set_status("就绪")
            return

        if not ok and cmd not in ("abort",):
            self.transcript.append(f"\n{cmd} 失败：{f.get('error')}\n", "error")
            return

        if cmd in ("prompt", "abort_and_prompt") and ok:
            if isinstance(data, dict) and data.get("agentInvoked") is False:
                self.set_busy(False)
                self.set_status("就绪")
                self.request("get_state")
            return

        if cmd == "get_state" and ok and isinstance(data, dict):
            self._apply_state(data)
            return

        if cmd == "get_available_models" and ok:
            models = data
            if isinstance(data, dict):
                models = data.get("models") or next((v for v in data.values() if isinstance(v, list)), [])
            self.window.set_models(models or [])
            self.window.refresh_model_choice(self)
            return

        if cmd == "get_available_thinking_levels" and ok:
            levels = data
            if isinstance(data, dict):
                levels = data.get("levels") or next((v for v in data.values() if isinstance(v, list)), [])
            self.window.set_thinking_levels(levels or [])
            self.window.refresh_model_choice(self)
            return

        if cmd == "new_session" and ok:
            self.transcript.clear()
            self.set_busy(False)
            self.set_status("新会话")
            self.request("get_state")
            return

    # -- model / state ------------------------------------------------------

    def _apply_state(self, data):
        self.state = data
        model = data.get("model") or {}
        if isinstance(model, dict) and model.get("provider") and model.get("id"):
            self.current_pair = (model["provider"], model["id"])
        if not self.name and data.get("sessionName"):
            self.name = str(data["sessionName"])
        if data.get("isStreaming") and not self.busy:
            self.set_busy(True)
        parts = []
        ctx = data.get("contextUsage") or {}
        if ctx:
            pct = ctx.get("percent")
            tokens = ctx.get("tokens")
            if isinstance(pct, (int, float)):
                parts.append(f"上下文 {pct:.2f}%".replace(".00%", "%"))
            elif tokens:
                parts.append(f"上下文 {tokens}")
        tps = data.get("tokensPerSecond")
        if tps:
            parts.append(f"{tps:.0f} tok/s")
        sid = data.get("sessionId")
        if sid:
            parts.append(f"会话 {str(sid)[-8:]}")
        if self.busy:
            parts.insert(0, "工作中")
        elif data.get("isCompacting"):
            parts.insert(0, "压缩中")
        else:
            parts.insert(0, "就绪")
        self.set_status("  ·  ".join(parts))
        self.window.refresh_session_chrome(self)

    def set_busy(self, flag):
        was_busy = self.busy
        self.busy = bool(flag)
        if self.busy and not was_busy:
            self.turn_started = time.monotonic()
            self.phase = self.phase or "思考中"
            self._render_activity()
            if not self._tick_running:
                self._tick_running = True
                self.frame.after(140, self._tick)
        elif not self.busy:
            self.turn_started = None
            self.phase = ""
            self._tick_running = False
            self.activity.configure(text="")
        if hasattr(self, "abort_btn"):
            self.abort_btn.configure(state="normal" if self.busy else "disabled")
        self.window.refresh_session_chrome(self)

    def set_phase(self, text):
        self.phase = text
        if self.busy:
            self._render_activity()

    def _tick(self):
        if not self.busy:
            self._tick_running = False
            self.activity.configure(text="")
            return
        self.spin += 1
        self._render_activity()
        self.frame.after(140, self._tick)

    def _render_activity(self):
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        spin = frames[self.spin % len(frames)]
        elapsed = time.monotonic() - (self.turn_started or time.monotonic())
        text = f"{spin} {self.phase or '处理中'}"
        if elapsed >= 1:
            text += f" · {elapsed:.0f}s"
        self.activity.configure(text=text)

    def set_status(self, text):
        self.status_text = text
        self.window.refresh_status(self)

    # -- history ------------------------------------------------------------

    def display_name(self):
        if self.name:
            return self.name
        base = os.path.basename(self.cwd.rstrip("\\/")) or self.cwd
        sid = self.state.get("sessionId") if self.state else None
        return f"{base} · {str(sid)[-4:]}" if sid else base

    def rename(self, name):
        name = (name or "").strip()
        self.name = name or None
        if name:
            self.request("set_session_name", name=name)
        self.window._style_chips()

    def _finish_history(self):
        self.resume = False
        self.history_loading = False
        self._render_history(self.history)
        self.history = []
        self.set_status("就绪")
        self.request("get_state")

    def _render_history(self, messages):
        self.transcript.clear()
        if not messages:
            self.transcript.append("omd · 新会话，输入消息开始\n", "meta")
            return
        self.transcript.append(f"omd · 已恢复上次会话（{len(messages)} 条消息）\n", "meta")
        for index, message in enumerate(messages):
            role = message.get("role")
            key = f"h{index}"
            if role == "user":
                if message.get("attribution") in ("agent", "internal"):
                    continue
                body = text_parts(message.get("content"))
                if not body.strip():
                    continue
                self.transcript.add_block(key, "你", "name_user")
                self.transcript.set_block(key, [(body, "body")])
            elif role == "assistant":
                segments = self._segments_for(message)
                if not segments:
                    continue
                self.last_msgs[key] = message
                self.msg_starts.add("a" + key)
                self.transcript.add_block(key, "omd", "name_assistant")
                self.transcript.set_block(key, segments)
            elif role == "tool":
                body = first_lines(text_parts(message.get("content")))
                name = message.get("toolName") or "tool"
                failed = bool(message.get("isError"))
                self.transcript.add_block(key, f"↳ {name}", "tool_err" if failed else "tool")
                self.transcript.set_block(key, [(body or "（无输出）", "tool_err" if failed else "tool")])
        self.transcript.text.see("end")

    def _segments_for(self, message):
        segments = []
        content = message.get("content") or []
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                segments.append((block.get("text") or "", "body"))
            elif btype == "thinking":
                if self.window.show_thinking.get():
                    segments.append((block.get("thinking") or "", "thinking"))
        return segments

    # -- message rendering --------------------------------------------------

    def _tool_key(self, f, name):
        cid = f.get("toolCallId") or f.get("id")
        if cid:
            return "t" + str(cid)
        if f.get("type") == "tool_execution_start":
            self._tool_seq += 1
            key = f"tf{self._tool_seq}"
            self._tool_fallback.append((name, key))
            return key
        for idx, (fb_name, key) in enumerate(self._tool_fallback):
            if fb_name == name:
                del self._tool_fallback[idx]
                return key
        self._tool_seq += 1
        return f"tf{self._tool_seq}"

    def _on_message_frame(self, kind, f):
        msg = f.get("message") or {}
        role = msg.get("role")
        mid = f.get("messageId") or "m0"

        if role == "user":
            return  # rendered locally on send

        if role == "tool":
            if kind == "message_update":
                return
            key = "tr" + mid
            failed = bool(msg.get("isError"))
            body = first_lines(text_parts(msg.get("content")))
            name = msg.get("toolName") or "tool"
            self.transcript.add_block(key, f"↳ {name}", "tool_err" if failed else "tool")
            self.transcript.set_block(key, [(body or "（无输出）", "tool_err" if failed else "tool")])
            return

        if role != "assistant":
            return

        key = "a" + mid
        if kind == "message_start":
            if key in self.msg_starts:
                return
            self.msg_starts.add(key)
            self.transcript.add_block(key, "omd", "name_assistant")
            return

        if kind == "message_update":
            payload = f.get("assistantMessageEvent") or {}
            event_type = payload.get("type") or ""
            if self.busy:
                if event_type.startswith("thinking"):
                    self.set_phase("思考中")
                elif event_type.startswith("text"):
                    self.set_phase("生成回答")
                elif event_type.startswith("toolcall"):
                    self.set_phase("准备工具调用")
            partial = payload.get("partial")
            if isinstance(partial, dict):
                msg = partial
            self.dirty[key] = msg
            self._schedule_flush()
            return

        if kind == "message_end":
            self.dirty.pop(key, None)
            if key not in self.msg_starts:
                self.msg_starts.add(key)
                self.transcript.add_block(key, "omd", "name_assistant")
            self._render_assistant(key, msg)

    def _schedule_flush(self):
        if self.flush_scheduled:
            return
        self.flush_scheduled = True
        self.frame.after(45, self._flush)

    def _flush(self):
        self.flush_scheduled = False
        dirty, self.dirty = self.dirty, {}
        for key, msg in dirty.items():
            self._render_assistant(key, msg)

    def redraw(self):
        for key, msg in list(self.last_msgs.items()):
            self._render_assistant(key, msg)

    def _render_assistant(self, key, msg):
        self.last_msgs[key] = msg
        segments = []
        content = msg.get("content") or []
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                segments.append((block.get("text") or "", "body"))
            elif btype == "thinking":
                if self.window.show_thinking.get():
                    segments.append((block.get("thinking") or "", "thinking"))
            elif btype == "toolCall":
                continue  # rendered from tool_execution_start/end
        self.transcript.set_block(key, segments)



# ------------------------------------------------------------------ settings --


class SettingsDialog(tk.Toplevel):
    """API-key editor: writes keys.json, which is injected as child env vars."""

    def __init__(self, window):
        super().__init__(window.root, bg=BG)
        self.window = window
        self.entries = {}
        self.reveal = tk.BooleanVar(value=False)
        self.title("设置 · API 密钥")
        self.geometry("720x620")
        self.minsize(560, 420)
        self.transient(window.root)
        self.grab_set()
        self._build()
        # wheel over any child widget (entries, labels) still scrolls the list
        self.bind_all("<MouseWheel>", self._on_wheel, add="+")

    def _on_wheel(self, event):
        canvas = getattr(self, "canvas", None)
        if canvas is not None:
            canvas.yview_scroll(int(-event.delta / 120), "units")
            return "break"

    def destroy(self):
        try:
            self.unbind_all("<MouseWheel>")
        except tk.TclError:
            pass
        super().destroy()

    def _build(self):
        head = tk.Frame(self, bg=BG, padx=18, pady=14)
        head.pack(fill="x", side="top")
        tk.Label(head, text="API 密钥", bg=BG, fg=FG, font=(UI_FONT[0], 13, "bold")).pack(anchor="w")
        tk.Label(
            head,
            text="保存在 %APPDATA%\\omd\\keys.json（仅当前 Windows 用户可读），启动 omp 时作为环境变量注入；\n"
                 "不修改 omp 配置，也不会覆盖已有的 OAuth 登录。留空表示不设置。",
            bg=BG,
            fg=MUTED,
            font=UI_FONT_SM,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=(4, 0))
        canvas = tk.Canvas(body, bg=BG, highlightthickness=0)
        self.canvas = canvas
        scroll = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        saved = load_keys()
        for label, env, url in PROVIDERS:
            row = tk.Frame(inner, bg=BG, pady=4)
            row.pack(fill="x")
            tk.Label(row, text=label, width=19, anchor="w", bg=BG, fg=FG, font=UI_FONT_SM).pack(side="left")
            entry = tk.Entry(
                row,
                show="•",
                bg=SURFACE_HI,
                fg=FG,
                insertbackground=FG,
                relief="flat",
                font=MONO_FONT,
                highlightthickness=1,
                highlightbackground=BORDER,
                highlightcolor=ACCENT,
            )
            entry.pack(side="left", fill="x", expand=True, padx=(0, 8), ipady=3)
            value = saved.get(env, "")
            if value:
                entry.insert(0, value)
            elif os.environ.get(env):
                tk.Label(row, text="环境变量已设置", bg=BG, fg=WARN, font=UI_FONT_SM).pack(side="left", padx=(0, 8))
            link = tk.Label(row, text="获取密钥", bg=BG, fg=ACCENT, font=UI_FONT_SM, cursor="hand2")
            link.pack(side="left")
            link.bind("<Button-1>", lambda _e, u=url: webbrowser.open(u))
            self.entries[env] = entry

        foot = tk.Frame(self, bg=SURFACE, padx=18, pady=12)
        foot.pack(fill="x", side="bottom")
        tk.Button(
            foot, text="保存并应用", command=self.save, bg=ACCENT, fg="#12131a",
            activebackground=ACCENT, activeforeground="#12131a", relief="flat", borderwidth=0,
            font=(UI_FONT[0], 10, "bold"), padx=14, pady=6, cursor="hand2",
        ).pack(side="right", padx=(8, 0))
        tk.Button(
            foot, text="取消", command=self.destroy, bg=SURFACE_HI, fg=FG,
            activebackground=BORDER, activeforeground=FG, relief="flat", borderwidth=0,
            font=UI_FONT_SM, padx=12, pady=6, cursor="hand2",
        ).pack(side="right")
        tk.Button(
            foot, text="清空全部", command=self.clear, bg=SURFACE_HI, fg=ERR,
            activebackground=BORDER, activeforeground=ERR, relief="flat", borderwidth=0,
            font=UI_FONT_SM, padx=12, pady=6, cursor="hand2",
        ).pack(side="left")
        tk.Checkbutton(
            foot, text="显示明文", variable=self.reveal, command=self._toggle_reveal,
            bg=SURFACE, fg=MUTED, activebackground=SURFACE, activeforeground=FG,
            selectcolor=FG, font=UI_FONT_SM, borderwidth=0, highlightthickness=0,
        ).pack(side="left", padx=(12, 0))

        self.bind("<Escape>", lambda _e: self.destroy())

    def _toggle_reveal(self):
        show = "" if self.reveal.get() else "•"
        for entry in self.entries.values():
            entry.configure(show=show)

    def save(self):
        keys = {}
        for env, entry in self.entries.items():
            value = entry.get().strip()
            if value:
                keys[env] = value
        save_keys(keys)
        self.destroy()
        self.window.apply_keys()

    def clear(self):
        if not messagebox.askyesno("清空密钥", "删除本应用保存的所有 API 密钥？", parent=self):
            return
        save_keys({})
        self.destroy()
        self.window.apply_keys()


# -------------------------------------------------------------------- window --


class Window:
    """Single application window: header, session tabs, banner, status bar."""

    POLL_MS = 30

    def __init__(self, root):
        self.root = root
        self.sessions = []
        self.active = None
        self.model_map = {}
        self.show_thinking = tk.BooleanVar(value=True)
        self.thinking_levels = []
        self._last_update_state = None
        self._banner_sig = None
        self._closing = False

        self._build()
        Updater.auto_check()
        SelfUpdater.check()
        root.after(self.POLL_MS, self._poll)
        root.after(800, self._ensure_backend_available)

    # -- layout ------------------------------------------------------------

    def _build(self):
        win = self.root
        win.title("omd")
        win.configure(bg=BG)
        win.geometry("1040x760")
        win.minsize(720, 480)
        try:
            win.iconbitmap(default=resource_path("omp.ico"))
        except tk.TclError:
            pass

        style = ttk.Style(win)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Vertical.TScrollbar",
            background=BORDER,
            troughcolor=BG,
            bordercolor=BG,
            arrowcolor=MUTED,
            lightcolor=BORDER,
            darkcolor=BORDER,
            relief="flat",
            gripcount=0,
        )
        style.map("Vertical.TScrollbar", background=[("active", SURFACE_HI)])
        style.configure(
            "TCombobox",
            fieldbackground=SURFACE_HI,
            background=SURFACE_HI,
            foreground=FG,
            arrowcolor=MUTED,
            bordercolor=BG,
            lightcolor=SURFACE_HI,
            darkcolor=SURFACE_HI,
            insertcolor=FG,
            selectbackground=SURFACE_HI,
            selectforeground=FG,
            padding=4,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", SURFACE_HI), ("disabled", SURFACE)],
            foreground=[("readonly", FG), ("disabled", MUTED)],
            selectbackground=[("readonly", SURFACE_HI)],
            selectforeground=[("readonly", FG)],
            background=[("readonly", SURFACE_HI)],
            arrowcolor=[("readonly", MUTED)],
        )
        win.option_add("*TCombobox*Listbox.background", SURFACE_HI)
        win.option_add("*TCombobox*Listbox.foreground", FG)
        win.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        win.option_add("*TCombobox*Listbox.selectForeground", "#12131a")
        win.option_add("*TCombobox*Listbox.font", UI_FONT)

        header = tk.Frame(win, bg=SURFACE, pady=8)
        header.pack(fill="x", side="top")

        tk.Label(header, text="omd", bg=SURFACE, fg=ACCENT, font=(UI_FONT[0], 13, "bold")).pack(
            side="left", padx=(16, 10)
        )

        self.update_btn = flat_button(header, "更新", Updater.check_now, fg=MUTED)
        self.update_btn.pack(side="right", padx=(6, 14))
        self.settings_btn = flat_button(header, "设置", self.open_settings, fg=MUTED)
        self.settings_btn.pack(side="right", padx=6)

        # -- session tabs --
        self.tabs = tk.Frame(win, bg=BG, pady=6)
        self.tabs.pack(fill="x", side="top")
        self.tab_chips = {}

        # -- update banner --
        self.banner = tk.Frame(win, bg=SURFACE_HI, padx=14, pady=8)
        self.banner_label = tk.Label(
            self.banner, text="", bg=SURFACE_HI, fg=FG, font=UI_FONT_SM, anchor="w", justify="left"
        )
        self.banner_label.pack(side="left", fill="x", expand=True)
        self.banner_actions = tk.Frame(self.banner, bg=SURFACE_HI)
        self.banner_actions.pack(side="right")

        # -- session content --
        self.container = tk.Frame(win, bg=BG)

        # -- status bar --
        status = tk.Frame(win, bg=BG, pady=5)
        status.pack(fill="x", side="bottom")
        self.status = tk.Label(status, text="启动中…", bg=BG, fg=MUTED, font=UI_FONT_SM, anchor="w")
        self.status.pack(side="left", padx=16)
        self.cwd_label = tk.Label(status, text=os.path.expanduser("~"), bg=BG, fg=MUTED, font=UI_FONT_SM, anchor="e")
        self.cwd_label.pack(side="right", padx=(8, 16))
        tk.Label(status, text="Enter 发送 · Ctrl+N 新会话 · Ctrl+W 关闭 · 双击标签重命名", bg=BG, fg=BORDER, font=UI_FONT_SM).pack(
            side="right", padx=8
        )
        tk.Label(
            status, text=f"omd {APP_VERSION}", bg=BG, fg=BORDER, font=UI_FONT_SM, anchor="w"
        ).pack(side="left", padx=(0, 10))
        self.version_label = tk.Label(status, text="", bg=BG, fg=BORDER, font=UI_FONT_SM, anchor="w")
        self.version_label.pack(side="left", padx=(0, 0))

        # packed last so shrinking squeezes the transcript, never the input row
        self.container.pack(fill="both", expand=True)

        win.protocol("WM_DELETE_WINDOW", self.close)
        win.bind_all("<Control-n>", lambda _e: self.new_session())
        win.bind_all("<Control-w>", lambda _e: self.close_session(self.active))
        win.bind_all("<Control-Tab>", lambda _e: self.cycle(1))
        win.bind_all("<Control-Shift-Tab>", lambda _e: self.cycle(-1))

        self.new_session(resume=True)

    def _ensure_backend_available(self):
        if find_omp():
            return
        again = messagebox.askretrycancel(
            "未检测到 omp",
            "本应用是 omp 的图形前端，需要先安装 omp 命令行。\n\n"
            "请安装 omp，并确认命令行里 `omp --version` 能正常输出。\n"
            "安装完成后点「重试」重新检测。",
            parent=self.root,
        )
        if again:
            for session in self.sessions:
                session.restart(keep_transcript=True)
            self.root.after(800, self._ensure_backend_available)

    # -- tabs ---------------------------------------------------------------

    def new_session(self, initial_prompt=None, resume=False):
        session = Session(
            self,
            cwd=self.active.cwd if self.active else None,
            initial_prompt=initial_prompt,
            resume=resume,
        )
        if self.active:
            session.preferred_pair = self.active.preferred_pair or self.active.current_pair
            session.preferred_level = self.active.preferred_level
        self.sessions.append(session)
        chip = self._make_chip(session)
        self.tab_chips[session] = chip
        self.switch_to(session)
        return session

    def _make_chip(self, session):
        chip = tk.Frame(self.tabs, bg=SURFACE, padx=10, pady=4, cursor="hand2")
        label = tk.Label(
            chip,
            text=self._chip_text(session),
            bg=SURFACE,
            fg=MUTED,
            font=UI_FONT_SM,
            cursor="hand2",
        )
        close = tk.Label(chip, text="✕", bg=SURFACE, fg=MUTED, font=UI_FONT_SM, cursor="hand2", padx=6)
        label.pack(side="left")
        close.pack(side="left")
        for widget in (chip, label):
            widget.bind("<Button-1>", lambda _e, s=session: self.switch_to(s))
            widget.bind("<Double-Button-1>", lambda _e, s=session: self.rename_session(s))
        close.bind("<Button-1>", lambda _e, s=session: self.close_session(s))
        chip.pack(side="left", padx=(0, 4))
        chip.label_widget = label
        chip.close_widget = close

        plus = tk.Label(self.tabs, text="+", bg=BG, fg=MUTED, font=(UI_FONT[0], 12, "bold"), cursor="hand2", padx=8)
        plus.pack(side="left")
        plus.bind("<Button-1>", lambda _e: self.new_session())
        if getattr(self, "_plus", None):
            self._plus.pack_forget()
        self._plus = plus
        plus.pack(side="left")
        return chip

    def _chip_text(self, session):
        return session.display_name()

    def _style_chips(self):
        for session, chip in self.tab_chips.items():
            active = session is self.active
            bg = SURFACE_HI if active else SURFACE
            fg = FG if active else MUTED
            chip.configure(bg=bg)
            chip.label_widget.configure(bg=bg, fg=fg)
            chip.close_widget.configure(bg=bg, fg=fg if active else BORDER)
            chip.label_widget.configure(text=self._chip_text(session))

    def switch_to(self, session):
        if session is None or session not in self.sessions:
            return
        if self.active is not None and self.active is not session:
            self.active.frame.pack_forget()
        self.active = session
        session.frame.pack(fill="both", expand=True)
        session.input.focus_force()
        self._style_chips()
        self.refresh_session_chrome(session)
        self.refresh_status(session)

    def cycle(self, step):
        if not self.sessions:
            return
        idx = self.sessions.index(self.active) if self.active in self.sessions else 0
        self.switch_to(self.sessions[(idx + step) % len(self.sessions)])

    def close_session(self, session):
        if session is None or session not in self.sessions:
            return
        if len(self.sessions) == 1:
            self.close()
            return
        idx = self.sessions.index(session)
        self.sessions.remove(session)
        chip = self.tab_chips.pop(session, None)
        if chip is not None:
            chip.destroy()
        session.close()
        if self.active is session:
            self.active = None
            self.switch_to(self.sessions[min(idx, len(self.sessions) - 1)])
        self._style_chips()

    # -- app-level actions --------------------------------------------------

    def env(self):
        return merged_env(load_keys())

    def apply_keys(self):
        busy = [session for session in self.sessions if session.busy]
        if busy and not messagebox.askyesno(
            "应用密钥",
            "有会话正在运行，重启后端会中止当前回合。继续？",
            parent=self.root,
        ):
            return
        for session in self.sessions:
            session.restart(keep_transcript=True)
        if self.active:
            self.active.transcript.append("\n已应用 API 密钥配置（新会话生效）\n", "meta")

    def open_settings(self):
        SettingsDialog(self)


    # -- shared model catalog ----------------------------------------------

    def set_models(self, models):
        labels = []
        self.model_map = {}
        for m in models:
            if not isinstance(m, dict):
                continue
            provider = m.get("provider") or m.get("providerId") or ""
            mid = m.get("id") or m.get("modelId") or m.get("model") or ""
            if not provider or not mid:
                continue
            name = m.get("name") or mid
            label = f"{name}  ·  {provider}"
            labels.append(label)
            self.model_map[label] = (provider, mid)
        for session in self.sessions:
            session.apply_catalog()

    def set_thinking_levels(self, levels):
        if levels:
            self.thinking_levels = [str(x) for x in levels]
            for session in self.sessions:
                session.apply_catalog()

    def _label_for(self, session):
        pair = None
        if session and session.preferred_pair:
            pair = session.preferred_pair
        else:
            model = session.state.get("model") if session else None
            if isinstance(model, dict) and model.get("provider") and model.get("id"):
                pair = (model["provider"], model["id"])
        if not pair:
            return None
        for label, value in self.model_map.items():
            if value == pair:
                return label
        return f"{pair[1]}  ·  {pair[0]}"

    def refresh_model_choice(self, session):
        session.apply_catalog()

    # -- chrome -------------------------------------------------------------

    def refresh_session_chrome(self, session):
        if session is not self.active:
            return
        session.apply_catalog()
        self.cwd_label.configure(text=session.cwd)
        self.root.title(f"omd — {os.path.basename(session.cwd) or session.cwd}")
        if session.state.get("sessionId"):
            self._style_chips()
        version = Updater.current_version()
        self.version_label.configure(text=f" omp {version}" if version else "")

    def refresh_status(self, session):
        if session is not self.active:
            return
        self.status.configure(text=session.status_text)

    def rename_session(self, session):
        """Inline rename: the tab chip turns into an entry, no dialog."""
        chip = self.tab_chips.get(session)
        if not chip or getattr(chip, "editing", False):
            return
        self.switch_to(session)
        chip.editing = True
        original = session.name or session.display_name()
        chip.label_widget.pack_forget()
        entry = tk.Entry(
            chip,
            bg=SURFACE_HI,
            fg=FG,
            insertbackground=FG,
            relief="flat",
            borderwidth=0,
            font=UI_FONT_SM,
            width=max(8, min(28, len(original) + 2)),
            highlightthickness=1,
            highlightbackground=ACCENT,
            highlightcolor=ACCENT,
        )
        entry.pack(side="left", fill="x", padx=(0, 2), before=chip.close_widget)
        entry.insert(0, original)
        entry.select_range(0, "end")
        entry.after(30, entry.focus_set)
        chip.entry = entry

        def finish(commit):
            if not chip.editing:
                return
            chip.editing = False
            value = entry.get()
            entry.destroy()
            chip.label_widget.pack(side="left", before=chip.close_widget)
            try:
                self.root.unbind("<Button-1>", click_id)
            except tk.TclError:
                pass
            if commit:
                session.rename(value)
            else:
                self._style_chips()
            session.input.focus_set()

        def on_click(event):
            if event.widget is entry:
                return
            finish(True)

        entry.bind("<Return>", lambda _e: finish(True))
        entry.bind("<Escape>", lambda _e: finish(False))
        # commit only on a click elsewhere in the app; losing OS focus keeps editing
        click_id = self.root.bind("<Button-1>", on_click, add="+")

    # -- update banner ------------------------------------------------------

    def _poll(self):
        if self._closing:
            return
        self._sync_update_banner()
        try:
            self.root.after(self.POLL_MS, self._poll)
        except tk.TclError:
            pass

    def _show_banner(self, text, actions, error=False):
        signature = (text, tuple(label for label, _ in actions), error)
        if signature == self._banner_sig:
            return
        self._banner_sig = signature
        self.banner_label.configure(text=text, fg=ERR if error else FG)
        for widget in list(self.banner_actions.winfo_children()):
            widget.destroy()
        for label, command in actions:
            btn = flat_button(self.banner_actions, label, command, accent=(label == "立即更新"))
            btn.pack(side="left", padx=(6, 0))
        if not self.banner.winfo_ismapped():
            self.banner.pack(fill="x", before=self.container)

    def _hide_banner(self):
        self._banner_sig = None
        self.banner.pack_forget()

    def _install_update(self):
        Updater.start_install()

    def _dismiss_self_update(self):
        own = SelfUpdater.snapshot() or {}
        SelfUpdater.dismiss(own.get("latest") or "")
        self._hide_banner()

    def _dismiss_update(self):
        state = Updater.snapshot() or {}
        Updater.dismiss(state.get("latest") or "")
        self._hide_banner()

    def _restart_active(self):
        self._hide_banner()
        if self.active:
            self.active.restart()

    def _sync_update_banner(self):
        own = SelfUpdater.snapshot()
        if own and own.get("newer") and own.get("latest") != SelfUpdater.dismissed():
            self._show_banner(
                f"omd 有新版本 {own['latest']}（当前 {APP_VERSION}）",
                [
                    ("打开发布页", lambda: webbrowser.open(own.get("url") or RELEASES_URL)),
                    ("忽略此版本", self._dismiss_self_update),
                ],
            )
            return

        state = Updater.snapshot()
        if state is None or state == self._last_update_state:
            return
        self._last_update_state = state
        self._banner_sig = None
        current = state.get("current") or Updater.current_version()
        if current:
            self.version_label.configure(text=f" omp {current}")

        if state.get("installing"):
            self._show_banner(f"正在更新 omp（当前 {current or '?'}）…", [])
            return
        if state.get("install_error"):
            self._show_banner(
                "更新失败：" + str(state["install_error"]),
                [("重试", self._install_update), ("关闭", self._hide_banner)],
                error=True,
            )
            return
        if state.get("installed"):
            latest = state.get("latest") or state.get("current") or ""
            self.version_label.configure(text=f" omp {latest}")
            self._show_banner(
                f"已更新到 {latest}，重启会话后生效",
                [("重启会话", self._restart_active), ("关闭", self._hide_banner)],
            )
            return
        if state.get("newer") and state.get("latest") != Updater.dismissed_version():
            self._show_banner(
                f"omp 有新版本 {state.get('latest')}（当前 {current}）",
                [("立即更新", self._install_update), ("忽略此版本", self._dismiss_update)],
            )
            return
        if state.get("error"):
            self._show_banner(
                f"检查更新失败：{state['error']}",
                [("重试", Updater.check_now), ("关闭", self._hide_banner)],
                error=True,
            )
            return
        self._hide_banner()

    # -- shutdown -----------------------------------------------------------

    def close(self):
        if self._closing:
            return
        self._closing = True
        for session in list(self.sessions):
            session.close()
        self.root.destroy()


def main():
    initial = None
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg in ("--prompt", "-p") and i + 1 < len(args):
            initial = args[i + 1]
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.35)
    except tk.TclError:
        pass
    window = Window(root)
    if initial:
        # Window.__init__ already created the first session; reuse it.
        window.root.after(0, lambda: window.active.send_text(initial))
    root.mainloop()


if __name__ == "__main__":
    main()
