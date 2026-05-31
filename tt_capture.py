"""Control the mitmproxy capture and the Windows system proxy from inside the
dashboard, so the user never has to touch a terminal.

Exposes start()/stop()/status() used by the Flask endpoints. The Windows proxy
is always restored on stop AND via an atexit hook, so a crash or Ctrl+C can't
leave the machine stuck behind a dead proxy.
"""
from __future__ import annotations

import atexit
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).parent
ADDON = BASE / "discover_addon.py"
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 8080
_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

_proc: subprocess.Popen | None = None
_prev_proxy: dict | None = None      # proxy state captured before we changed it
_started_at: float | None = None


# ── Windows proxy via registry ──────────────────────────────────────────────
def _get_proxy_state() -> dict | None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATH) as k:
            try:
                enable = int(winreg.QueryValueEx(k, "ProxyEnable")[0])
            except FileNotFoundError:
                enable = 0
            try:
                server = str(winreg.QueryValueEx(k, "ProxyServer")[0])
            except FileNotFoundError:
                server = ""
            return {"enable": enable, "server": server}
    except Exception:
        return None


def _set_proxy(enable: int, server: str | None = None) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATH, 0, winreg.KEY_SET_VALUE) as k:
        if server is not None:
            winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ, server)
        winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, int(enable))
    _refresh_wininet()


def _refresh_wininet() -> None:
    """Tell WinINet the proxy changed so apps pick it up without a restart."""
    try:
        import ctypes
        wininet = ctypes.windll.wininet
        wininet.InternetSetOptionW(0, 39, 0, 0)  # SETTINGS_CHANGED
        wininet.InternetSetOptionW(0, 37, 0, 0)  # REFRESH
    except Exception:
        pass


def _restore_proxy() -> None:
    global _prev_proxy
    try:
        if _prev_proxy is not None:
            _set_proxy(_prev_proxy.get("enable", 0), _prev_proxy.get("server"))
        else:
            _set_proxy(0)
    except Exception:
        pass


# ── mitmdump location ───────────────────────────────────────────────────────
def _find_mitmdump() -> str | None:
    import shutil
    p = shutil.which("mitmdump")
    if p:
        return p
    # Same Python's Scripts dir
    cand = Path(sys.executable).parent / "Scripts" / "mitmdump.exe"
    if cand.exists():
        return str(cand)
    cand = Path(sys.executable).parent / "mitmdump.exe"
    if cand.exists():
        return str(cand)
    return None


# ── Public API ──────────────────────────────────────────────────────────────
def is_capturing() -> bool:
    return _proc is not None and _proc.poll() is None


def status() -> dict:
    return {
        "capturing": is_capturing(),
        "since": _started_at,
        "elapsed": round(time.time() - _started_at) if (_started_at and is_capturing()) else 0,
        "mitmdump_found": _find_mitmdump() is not None,
    }


def start() -> dict:
    global _proc, _prev_proxy, _started_at
    if is_capturing():
        return {"ok": True, "status": "already_running", **status()}
    mitm = _find_mitmdump()
    if not mitm:
        return {"ok": False, "error": "mitmdump not found — run: pip install mitmproxy"}
    # Free the port if something is squatting on it
    _kill_port(PROXY_PORT)
    # Save current proxy, switch to ours
    _prev_proxy = _get_proxy_state()
    try:
        _set_proxy(1, f"{PROXY_HOST}:{PROXY_PORT}")
    except Exception as e:
        return {"ok": False, "error": f"could not set system proxy: {e}"}
    # Launch mitmdump detached, no console window
    flags = 0
    if sys.platform.startswith("win"):
        flags = subprocess.CREATE_NO_WINDOW
    try:
        _proc = subprocess.Popen(
            [mitm, "-s", str(ADDON), "--listen-port", str(PROXY_PORT),
             "--set", "block_global=false"],
            cwd=str(BASE),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    except Exception as e:
        _restore_proxy()
        return {"ok": False, "error": f"could not start mitmdump: {e}"}
    _started_at = time.time()
    time.sleep(1.2)
    if not is_capturing():
        _restore_proxy()
        return {"ok": False, "error": "mitmdump exited immediately — check cert/install"}
    return {"ok": True, "status": "started", **status()}


def stop(run_analyze: bool = True) -> dict:
    global _proc, _started_at
    if _proc is not None:
        try:
            _proc.terminate()
            try:
                _proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _proc.kill()
        except Exception:
            pass
    _proc = None
    _started_at = None
    _restore_proxy()
    result = {"ok": True, "status": "stopped"}
    if run_analyze:
        result["analyze"] = run_analyzer()
    return result


def run_analyzer() -> dict:
    """Run tt_analyze.py as a subprocess and report how many trials
    were newly added."""
    try:
        proc = subprocess.run(
            [sys.executable, str(BASE / "tt_analyze.py")],
            cwd=str(BASE), capture_output=True, text=True, timeout=300,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        added = 0
        import re
        m = re.search(r"Added (\d+) new trial", out)
        if m:
            added = int(m.group(1))
        nothing = "Nothing new added" in out
        return {"ok": proc.returncode == 0, "added": added,
                "nothing_new": nothing, "tail": out.strip().splitlines()[-6:]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _kill_port(port: int) -> None:
    try:
        import socket
        # best-effort: use netstat-free approach via psutil if available
        import psutil  # type: ignore
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == port and c.pid:
                try:
                    psutil.Process(c.pid).terminate()
                except Exception:
                    pass
    except Exception:
        # psutil not installed — fall back to PowerShell
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue | "
                 f"ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }}"],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass


@atexit.register
def _cleanup() -> None:
    global _proc
    if _proc is not None:
        try:
            _proc.terminate()
        except Exception:
            pass
        _proc = None
    _restore_proxy()
