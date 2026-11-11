#!/usr/bin/env python3
"""omd installer (and uninstaller).

Build: pyinstaller --onefile --windowed --name omd-setup
         --add-data "dist/omd.exe;payload" --icon assets/omd.ico installer.py

Usage:
    omp-desktop-setup.exe                 interactive install
    omp-desktop-setup.exe --silent         install without UI
    omp-desktop-setup.exe --dir <path>     install location
    omp-desktop-setup.exe --uninstall      remove (also copied as uninstall.exe)
"""

import base64
import os
import shutil
import subprocess
import sys
import tempfile
import time
import winreg

APP_NAME = "omd"
APP_ID = "omd"
VERSION = "1.7.9"
EXE_NAME = "omd.exe"
OMP_EXE = "omp.exe"
UNINST_LOCATION = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}"
NO_WINDOW = 0x08000000
BREAKAWAY = 0x01000000  # leave the PyInstaller job object, so children survive our exit


def payload_dir():
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "payload")


def default_omp_dir():
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(root, "omp")


# ------------------------------------------------------------------- PATH ---


def read_user_path():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, kind = winreg.QueryValueEx(key, "Path")
            return value or "", kind
    except OSError:
        return "", winreg.REG_EXPAND_SZ


def write_user_path(value, kind=winreg.REG_EXPAND_SZ):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "Path", 0, kind, value)
    try:
        import ctypes

        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, ctypes.c_wchar_p("Environment"), 0x0002, 5000, ctypes.byref(result)
        )
    except Exception:
        pass


def _same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def add_to_user_path(directory):
    current, kind = read_user_path()
    parts = [p for p in current.split(";") if p.strip()]
    if any(_same_path(p, directory) for p in parts):
        return False
    write_user_path(";".join(parts + [directory]), kind or winreg.REG_EXPAND_SZ)
    return True


def remove_from_user_path(directory):
    current, kind = read_user_path()
    parts = [p for p in current.split(";") if p.strip()]
    kept = [p for p in parts if not _same_path(p, directory)]
    if len(kept) == len(parts):
        return False
    write_user_path(";".join(kept), kind or winreg.REG_EXPAND_SZ)
    return True


def default_dir():
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(root, "Programs", APP_ID)


def program_files_dir():
    for var in ("ProgramFiles", "ProgramFiles(x86)"):
        path = os.environ.get(var)
        if path and os.path.isdir(path):
            return os.path.join(path, APP_NAME)
    return default_dir()


def spawn_detached(args, env=None):
    """Start a process that keeps running after this one exits.

    A windowed onefile build has no valid std handles, so give the child its
    own DEVNULL handles; BREAKAWAY leaves PyInstaller's kill-on-close job.
    """
    kwargs = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env=env,
    )
    try:
        return subprocess.Popen(args, creationflags=NO_WINDOW | BREAKAWAY, **kwargs)
    except OSError:
        return subprocess.Popen(args, creationflags=NO_WINDOW, **kwargs)


def spawn_outside_job(script):
    """Run a PowerShell script outside PyInstaller's kill-on-close job object.

    A onefile bootloader owns its children through a job object, so a detached
    child dies with us. Ask the WMI provider host to create the process instead:
    its parent then lives outside the job.
    """
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    command = f"powershell -NoProfile -WindowStyle Hidden -EncodedCommand {encoded}"
    launcher = (
        f"$cmd = {ps_quote(command)}; "
        "Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        "-Arguments @{CommandLine = $cmd} | Out-Null"
    )
    try:
        run_hidden(["powershell", "-NoProfile", "-Command", launcher])
        return True
    except OSError:
        return False


def launch_detached(path):
    """Launch an app so it outlives this process (explorer owns it)."""
    try:
        subprocess.Popen(
            ["explorer.exe", path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=NO_WINDOW,
        )
        return True
    except OSError:
        return False


def run_hidden(args):
    return subprocess.run(args, capture_output=True, creationflags=NO_WINDOW)


def ps_quote(text):
    return "'" + text.replace("'", "''") + "'"


def make_shortcut(link_path, target, workdir, icon):
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut({ps_quote(link_path)}); "
        f"$s.TargetPath = {ps_quote(target)}; "
        f"$s.WorkingDirectory = {ps_quote(workdir)}; "
        f"$s.IconLocation = {ps_quote(icon + ',0')}; "
        f"$s.Description = {ps_quote(APP_NAME)}; "
        "$s.Save()"
    )
    return run_hidden(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script])


def desktop_shortcut():
    return os.path.join(os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"), APP_NAME + ".lnk")


def start_menu_shortcut():
    root = os.environ.get("APPDATA") or ""
    return os.path.join(root, "Microsoft", "Windows", "Start Menu", "Programs", APP_NAME + ".lnk")


def write_registry(target_dir, omp_installed=False, omp_dir=None):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINST_LOCATION) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, VERSION)
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, "omd")
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, target_dir)
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, os.path.join(target_dir, EXE_NAME))
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ,
                          f'"{os.path.join(target_dir, "uninstall.exe")}" --uninstall')
        winreg.SetValueEx(key, "QuietUninstallString", 0, winreg.REG_SZ,
                          f'"{os.path.join(target_dir, "uninstall.exe")}" --uninstall --silent')
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        if omp_installed:
            winreg.SetValueEx(key, "OmpInstalled", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "OmpDir", 0, winreg.REG_SZ, omp_dir or default_omp_dir())


def remove_registry():
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINST_LOCATION)
    except OSError:
        pass


def bundled_omp_version():
    path = os.path.join(payload_dir(), OMP_EXE)
    if not os.path.exists(path):
        return None
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, timeout=30, creationflags=NO_WINDOW
        ).stdout.decode("utf-8", "replace")
        return out.strip().splitlines()[0] if out.strip() else "?"
    except (OSError, subprocess.SubprocessError):
        return "?"


def install(target_dir, shortcuts=True, report=print, with_omp=True, omp_dir=None):
    src = os.path.join(payload_dir(), EXE_NAME)
    if not os.path.exists(src):
        raise FileNotFoundError(f"安装包内缺少 {EXE_NAME}")
    os.makedirs(target_dir, exist_ok=True)
    report("复制程序…")
    shutil.copy2(src, os.path.join(target_dir, EXE_NAME))
    icon_src = os.path.join(os.path.dirname(payload_dir()), "omd.ico")
    if os.path.exists(icon_src):
        shutil.copy2(icon_src, os.path.join(target_dir, "omd.ico"))
    report("写入卸载程序…")
    try:
        shutil.copy2(sys.executable, os.path.join(target_dir, "uninstall.exe"))
    except OSError:
        pass
    report("注册卸载信息…")
    omp_created = False
    omp_target = os.path.abspath(omp_dir or default_omp_dir())
    bundled = os.path.join(payload_dir(), OMP_EXE)
    if with_omp and os.path.exists(bundled):
        existing = os.path.join(omp_target, OMP_EXE)
        if os.path.exists(existing):
            report("检测到已安装 omp，保留现有版本…")
        else:
            report("安装 omp 命令行…")
            os.makedirs(omp_target, exist_ok=True)
            shutil.copy2(bundled, existing)
            omp_created = True
            if add_to_user_path(omp_target):
                report("已将 omp 加入 PATH…")

    write_registry(target_dir, omp_installed=omp_created, omp_dir=omp_target if omp_created else None)
    if shortcuts:
        report("创建快捷方式…")
        target = os.path.join(target_dir, EXE_NAME)
        icon = os.path.join(target_dir, "omd.ico")
        make_shortcut(desktop_shortcut(), target, target_dir, icon)
        try:
            os.makedirs(os.path.dirname(start_menu_shortcut()), exist_ok=True)
            make_shortcut(start_menu_shortcut(), target, target_dir, icon)
        except OSError:
            pass
    report("安装完成。")


def uninstall(target_dir, report=print):
    # Running from inside the install dir keeps that image locked. Re-exec from
    # %TEMP% first so the whole folder can be removed right away.
    if not os.environ.get("OMD_UNINSTALL_TEMP"):
        try:
            temp_exe = os.path.join(tempfile.gettempdir(), f"omd-uninstall-{os.getpid()}.exe")
            shutil.copy2(sys.executable, temp_exe)
            subprocess.Popen(
                [temp_exe, "--uninstall", "--silent", "--dir", target_dir],
                env=dict(os.environ, OMD_UNINSTALL_TEMP="1"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=NO_WINDOW,
            )
            return
        except OSError:
            pass

    report("删除快捷方式…")
    for link in (desktop_shortcut(), start_menu_shortcut()):
        try:
            os.remove(link)
        except OSError:
            pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINST_LOCATION) as key:
            created = bool(winreg.QueryValueEx(key, "OmpInstalled")[0])
            omp_dir = winreg.QueryValueEx(key, "OmpDir")[0]
    except OSError:
        created, omp_dir = False, None
    remove_registry()

    if created and omp_dir:
        report("移除 omp 命令行…")
        remove_from_user_path(omp_dir)
        shutil.rmtree(omp_dir, ignore_errors=True)

    report("删除程序文件…")
    shutil.rmtree(target_dir, ignore_errors=True)

    # A real-time antivirus scan can hold a freshly written .exe for a while, so
    # finish the removal in a detached retry loop instead of blocking the UI.
    if os.path.exists(target_dir):
        quoted = ps_quote(target_dir)
        script = (
            "for ($i = 0; $i -lt 180; $i++) { "
            f"Remove-Item -LiteralPath {quoted} -Recurse -Force -ErrorAction SilentlyContinue; "
            f"if (-not (Test-Path -LiteralPath {quoted})) {{ break }}; "
            "Start-Sleep -Seconds 1 }"
        )
        spawn_outside_job(script)

    # remove this temporary copy once it has exited
    temp_dir = os.path.abspath(tempfile.gettempdir())
    if os.path.dirname(os.path.abspath(sys.executable)).lower() == temp_dir.lower():
        quoted_exe = ps_quote(sys.executable)
        cleanup = (
            "for ($i = 0; $i -lt 30; $i++) { "
            "Start-Sleep -Seconds 1; "
            f"if (-not (Test-Path -LiteralPath {quoted_exe})) {{ break }}; "
            f"Remove-Item -LiteralPath {quoted_exe} -Force -ErrorAction SilentlyContinue }}"
        )
        spawn_outside_job(cleanup)

    report("卸载完成。")


# --------------------------------------------------------------------- GUI --


BG = "#1b1b20"
SURFACE = "#232329"
SURFACE_HI = "#2c2c34"
FG = "#e6e6ea"
MUTED = "#8f8f9b"
ACCENT = "#7aa2f7"
BORDER = "#33333c"
FONT = ("Microsoft YaHei UI", 10)
FONT_SM = ("Microsoft YaHei UI", 9)


def run_gui():
    import tkinter as tk
    from tkinter import filedialog, ttk

    root = tk.Tk()
    root.title(f"{APP_NAME} {VERSION} · 安装")
    root.configure(bg=BG)
    root.geometry("600x400")
    root.resizable(False, False)
    try:
        icon = os.path.join(os.path.dirname(payload_dir()), "omd.ico")
        root.iconbitmap(default=icon)
    except Exception:
        pass

    tk.Label(root, text=APP_NAME, bg=BG, fg=ACCENT, font=(FONT[0], 18, "bold")).pack(anchor="w", padx=24, pady=(22, 0))
    omp_version = bundled_omp_version()
    has_omp = shutil.which("omp") is not None or os.path.exists(os.path.join(default_omp_dir(), OMP_EXE))
    subtitle = f"安装 omd（omp 的图形前端）到本机，版本 {VERSION}。"
    if omp_version:
        subtitle += f"\n安装包内含 omp 命令行 {omp_version}，" + ("本机已安装则保留原版本。" if has_omp else "将一并安装并加入 PATH。")
    tk.Label(root, text=subtitle, bg=BG, fg=MUTED, font=FONT_SM, justify="left").pack(
        anchor="w", padx=24, pady=(4, 16)
    )

    row = tk.Frame(root, bg=BG)
    row.pack(fill="x", padx=24)
    tk.Label(row, text="安装位置", bg=BG, fg=FG, font=FONT).pack(side="left")
    path_var = tk.StringVar(value=default_dir())
    entry = tk.Entry(
        row, textvariable=path_var, bg=SURFACE_HI, fg=FG, insertbackground=FG,
        relief="flat", font=FONT_SM, highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
    )
    entry.pack(side="left", fill="x", expand=True, padx=10, ipady=5)

    def browse():
        chosen = filedialog.askdirectory(initialdir=os.path.dirname(path_var.get()))
        if chosen:
            path_var.set(os.path.join(chosen, APP_ID))

    tk.Button(
        row, text="浏览…", command=browse, bg=SURFACE_HI, fg=FG, relief="flat", borderwidth=0,
        activebackground=BORDER, font=FONT_SM, padx=10, pady=5, cursor="hand2",
    ).pack(side="left")

    link_var = tk.BooleanVar(value=True)
    launch_var = tk.BooleanVar(value=True)
    omp_var = tk.BooleanVar(value=not has_omp)
    opts = tk.Frame(root, bg=BG)
    opts.pack(fill="x", padx=24, pady=(10, 0))
    if omp_version:
        tk.Checkbutton(
            opts, text=f"同时安装 omp 命令行（{omp_version}）", variable=omp_var,
            bg=BG, fg=MUTED, activebackground=BG, activeforeground=FG,
            selectcolor=SURFACE_HI, font=FONT_SM, borderwidth=0, highlightthickness=0,
        ).pack(side="left", padx=(0, 16))
    opts2 = tk.Frame(root, bg=BG)
    opts2.pack(fill="x", padx=24, pady=(6, 10))
    for text, var in (("创建桌面快捷方式", link_var), ("安装完成后启动", launch_var)):
        tk.Checkbutton(
            opts, text=text, variable=var, bg=BG, fg=MUTED, activebackground=BG, activeforeground=FG,
            selectcolor=SURFACE_HI, font=FONT_SM, borderwidth=0, highlightthickness=0,
        ).pack(side="left", padx=(0, 16))

    status = tk.Label(root, text="", bg=BG, fg=MUTED, font=FONT_SM, anchor="w")
    status.pack(fill="x", padx=24)

    buttons = tk.Frame(root, bg=BG)
    buttons.pack(fill="x", padx=24, pady=(10, 18), side="bottom")

    def do_install():
        target = path_var.get().strip()
        if not target:
            status.configure(text="请选择安装位置", fg="#e06c75")
            return
        install_btn.configure(state="disabled")
        root.update_idletasks()

        def report(message):
            status.configure(text=message, fg=MUTED)
            root.update_idletasks()

        try:
            install(target, shortcuts=link_var.get(), report=report, with_omp=omp_var.get())
        except Exception as exc:  # surface the reason instead of dying silently
            status.configure(text=f"安装失败：{exc}", fg="#e06c75")
            install_btn.configure(state="normal")
            return
        status.configure(text=f"已安装到 {target}", fg="#7dc97d")
        install_btn.configure(text="完成", command=root.destroy, state="normal")
        if launch_var.get():
            launch_detached(os.path.join(target, EXE_NAME))

    install_btn = tk.Button(
        buttons, text="安装", command=do_install, bg=ACCENT, fg="#12131a",
        activebackground=ACCENT, activeforeground="#12131a", relief="flat", borderwidth=0,
        font=(FONT[0], 10, "bold"), padx=22, pady=7, cursor="hand2",
    )
    install_btn.pack(side="right")
    tk.Button(
        buttons, text="取消", command=root.destroy, bg=SURFACE_HI, fg=FG,
        activebackground=BORDER, activeforeground=FG, relief="flat", borderwidth=0,
        font=FONT_SM, padx=16, pady=7, cursor="hand2",
    ).pack(side="right", padx=(0, 8))

    root.mainloop()


def main():
    args = sys.argv[1:]
    silent = "--silent" in args
    uninst = "--uninstall" in args
    with_omp = "--no-omp" not in args
    target = None
    omp_dir = None
    for i, arg in enumerate(args):
        if arg == "--dir" and i + 1 < len(args):
            target = args[i + 1]
        elif arg.startswith("--dir="):
            target = arg.split("=", 1)[1]
        elif arg == "--omp-dir" and i + 1 < len(args):
            omp_dir = args[i + 1]
        elif arg.startswith("--omp-dir="):
            omp_dir = arg.split("=", 1)[1]
    if uninst and target is None:
        # uninstall.exe lives in the install dir: remove the tree it sits in
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        if os.path.exists(os.path.join(exe_dir, EXE_NAME)):
            target = exe_dir
    target = os.path.abspath(target or default_dir())

    if uninst:
        if silent:
            uninstall(target)
        else:
            import tkinter.messagebox as mb
            if mb.askyesno(APP_NAME, f"从 {target} 卸载 {APP_NAME}？"):
                uninstall(target)
        return

    if silent:
        install(target, shortcuts=True, with_omp=with_omp, omp_dir=omp_dir)
    else:
        run_gui()


if __name__ == "__main__":
    main()
