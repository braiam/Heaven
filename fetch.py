"""
Heir fetcher — pulls fresh load/index + pre_single_mode/index from the game server.

First run on a PC:
    python fetch.py
    -> launches Umamusume via Steam, hooks Frida to capture auth_key, saves
       auth_config.json. You'll also be asked for your Steam username/password
       (saved obfuscated in auth_config.json) so future runs can refresh the ticket.

Later runs on the same PC:
    python fetch.py
    -> uses saved auth_config.json. Refreshes Steam ticket headlessly (no game needed),
       calls load/index + pre_single_mode/index, writes data/heir_capture_<ts>.jsonl.

Requirements:
    pip install -r requirements.txt
    Node.js + `npm install` (for steam-user, to refresh the Steam ticket).
"""

import os
import sys
import json
import time
import base64
import ctypes
import hashlib
import random
import re
import shutil
import socket
import struct
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path

import msgpack
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from curl_cffi import requests

import safe_store

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
AUTH_PATH = ROOT / "auth_config.json"

PROCESS_NAME = "UmamusumePrettyDerby.exe"
APP_ID = "3224770"
BASE_URL = "https://api.games.umamusume.com/umamusume/"
SALT = b"co!=Y;(UQCGxJ_n82"
HEAD = bytes.fromhex(
    "6b20e2ab6c311330f761d737ce3f3025750850665eea58b6372f8d2f57501eb3"
    "44bdb7270a9067f5b63cd61f152cfb986cbfbf7a"
)


# ---------- Frida capture (auth_key + viewer_id + udid + app/res ver) ----------

FRIDA_JS = r"""
'use strict';
(function() {
    var buffers = {};
    var attached = {};
    function hex2(n) { return ('0' + (n & 255).toString(16)).slice(-2); }
    function uuidFromHex(h) {
        return h.substring(0,8)+'-'+h.substring(8,12)+'-'+h.substring(12,16)+'-'+h.substring(16,20)+'-'+h.substring(20);
    }
    function b64(s) {
        var chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
        var out = [], buffer = 0, bits = 0;
        for (var i = 0; i < s.length; i++) {
            var c = s.charAt(i);
            if (c === '=') break;
            var idx = chars.indexOf(c);
            if (idx < 0) continue;
            buffer = (buffer << 6) | idx; bits += 6;
            if (bits >= 8) { bits -= 8; out.push((buffer >> bits) & 255); }
        }
        return out;
    }
    function parseWire(endpoint, viewerId, body, appVer, resVer) {
        var d = b64(body);
        if (d.length < 140) return;
        var hl = d[0] | (d[1] << 8) | (d[2] << 16) | (d[3] << 24);
        var end1 = 4 + hl;
        if (hl < 120 || hl > 2048 || d.length < end1) return;
        var udidHex = '', authHex = '';
        for (var i = end1 - 96; i < end1 - 80; i++) udidHex += hex2(d[i]);
        for (var j = end1 - 48; j < end1; j++) authHex += hex2(d[j]);
        if (!viewerId || !authHex || authHex.length < 64 || udidHex.length !== 32) return;
        send({
            type: 'creds', endpoint: endpoint,
            viewer_id: parseInt(viewerId, 10), udid: uuidFromHex(udidHex),
            auth_key: authHex, auth_key_len: authHex.length / 2,
            app_ver: appVer, res_ver: resVer
        });
    }
    function parseHttp(text) {
        if (text.indexOf('/umamusume/') < 0) return;
        var em = text.match(/POST\s+\/umamusume\/([^\s]+)\s+HTTP/i);
        var vm = text.match(/(?:^|\r\n)(?:ViewerID|ViewerId):\s*(\d+)/i);
        var appVer = text.match(/(?:^|\r\n)APP-VER:\s*([^\r\n]+)/i);
        var resVer = text.match(/(?:^|\r\n)RES-VER:\s*([^\r\n]+)/i);
        var idx = text.indexOf('\r\n\r\n');
        if (!em || !vm || idx < 0) return;
        parseWire(em[1], vm[1], text.substring(idx + 4),
                  appVer ? appVer[1].trim() : '', resVer ? resVer[1].trim() : '');
    }
    function parseChunk(key, chunk) {
        var buf = (buffers[key] || '') + chunk;
        if (buf.length > 2097152) buf = buf.substring(buf.length - 1048576);
        var start = buf.indexOf('POST ');
        if (start < 0) { buffers[key] = buf.slice(-4096); return; }
        if (start > 0) buf = buf.substring(start);
        var he = buf.indexOf('\r\n\r\n');
        if (he < 0) { buffers[key] = buf; return; }
        var lm = buf.substring(0, he).match(/Content-Length:\s*(\d+)/i);
        var len = lm ? parseInt(lm[1], 10) : 0;
        var total = he + 4 + len;
        if (len > 0 && buf.length < total) { buffers[key] = buf; return; }
        parseHttp(len > 0 ? buf.substring(0, total) : buf);
        buffers[key] = buf.length > total ? buf.substring(total) : '';
    }
    function hookTls() {
        var ga = Process.findModuleByName('GameAssembly.dll');
        if (!ga) return false;
        var installFn = ga.findExportByName('il2cpp_unity_install_unitytls_interface');
        if (!installFn) return false;
        var rb = new Uint8Array(installFn.readByteArray(16));
        var realFn = installFn;
        if (rb[0] === 0xe9) {
            var off = rb[1] | (rb[2] << 8) | (rb[3] << 16) | (rb[4] << 24);
            if (off > 0x7fffffff) off -= 0x100000000;
            realFn = installFn.add(5 + off);
            rb = new Uint8Array(realFn.readByteArray(16));
        }
        var globalPtr = null;
        if (rb[0] === 0x48 && rb[1] === 0x89 && rb[2] === 0x0d) {
            var disp = rb[3] | (rb[4] << 8) | (rb[5] << 16) | (rb[6] << 24);
            if (disp > 0x7fffffff) disp -= 0x100000000;
            globalPtr = realFn.add(7 + disp);
        }
        if (!globalPtr) return false;
        var iface = globalPtr.readPointer();
        if (!iface || iface.isNull()) return false;
        var hookedTls = 0;
        [0xd0, 0xd8, 0xe0, 0xe8].forEach(function(off) {
            var addr = iface.add(off).readPointer();
            if (!addr || addr.isNull()) return;
            var key = 'tls_' + addr.toString();
            if (attached[key]) return;
            try {
                Interceptor.attach(addr, {
                    onEnter: function(args) {
                        var len = args[2].toInt32();
                        if (len <= 0 || len > 1048576 || args[1].isNull()) return;
                        try {
                            var bytes = args[1].readByteArray(len);
                            var u8 = new Uint8Array(bytes);
                            var s = '';
                            for (var i = 0; i < u8.length; i++) s += String.fromCharCode(u8[i]);
                            parseChunk(args[0].toString(), s);
                        } catch (e) {}
                    }
                });
                attached[key] = true;
                hookedTls++;
            } catch (e) {}
        });
        return hookedTls > 0;
    }
    var done = false;
    var timer = setInterval(function() {
        try { if (!done) done = hookTls(); if (done) clearInterval(timer); } catch (e) {}
    }, 1000);
})();
"""


# ---------- Steam ticket via Node steam-user (no game needed) ----------

TICKET_JS = r"""
const SteamUser = require("steam-user");
const args = process.argv.slice(2);
let username = "", password = "", appid = 3224770, code = "";
for (let i = 0; i < args.length; i++) {
  if (args[i] === "--username") username = args[++i];
  else if (args[i] === "--password") password = args[++i];
  else if (args[i] === "--appid") appid = parseInt(args[++i]);
  else if (args[i] === "--code") code = args[++i];
}
const client = new SteamUser();
const opts = { accountName: username, password: password };
if (code) opts.twoFactorCode = code;
client.logOn(opts);
client.on("steamGuard", () => { process.stderr.write("NEED_GUARD\n"); process.exit(2); });
client.on("error", (err) => { process.stderr.write("ERROR:" + err.message + "\n"); process.exit(1); });
client.on("loggedOn", () => {
  client.createAuthSessionTicket(appid, (err, t) => {
    if (err) { process.stderr.write("Ticket error: " + err.message + "\n"); process.exit(1); }
    const buf = Buffer.isBuffer(t) ? t : (t.sessionTicket || t);
    process.stdout.write(JSON.stringify({
      steam_id: client.steamID.getSteamID64(),
      session_ticket: Buffer.from(buf).toString("hex").toUpperCase()
    }) + "\n");
    setTimeout(() => process.exit(0), 500);
  });
});
"""


def get_steam_ticket(username, password, code=""):
    if not shutil.which("node"):
        raise RuntimeError("Node.js no instalado. Descargalo de https://nodejs.org")
    if not (ROOT / "node_modules").exists():
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if not npm:
            raise RuntimeError("npm no encontrado. Ejecuta 'npm install' en la carpeta de Heir")
        print("[*] Instalando steam-user (npm install)...", flush=True)
        subprocess.run([npm, "install", "--silent"], check=True, cwd=str(ROOT), shell=False)
    # node -e consumes the first arg after `--`, so put a dummy there first
    cmd = ["node", "-e", TICKET_JS, "--", "--dummy", "--username", username, "--password", password]
    if code:
        cmd += ["--code", code]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=str(ROOT))
    if proc.returncode == 2:
        raise RuntimeError("STEAM_GUARD_REQUIRED")
    out = proc.stdout.strip()
    if not out or proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "ticket gen failed")
    d = json.loads(out.split("\n")[-1])
    return d["steam_id"], d["session_ticket"]


# ---------- Crypto + hardware fingerprint ----------

def sm5(b): h = hashlib.md5(); h.update(b); h.update(SALT); return h.digest()
def make_sid(vid, udid): return sm5((str(vid) + udid).encode())
def next_sid(sid): return sm5(sid.encode())
def get_iv(udid): return udid.replace("-", "").lower()[:16].encode()
def get_raw_udid(udid): return bytes.fromhex(udid.replace("-", "").lower())

def gen_key():
    out = b""
    while len(out) < 32:
        out += format(random.randint(0, 65535), "x").encode()
    return out[:32]

def pack(sid, udid_raw, auth, payload, udid):
    key = gen_key()
    p = msgpack.packb(payload, use_bin_type=True)
    body = AES.new(key, AES.MODE_CBC, get_iv(udid)).encrypt(
        pad(struct.pack("<I", len(p)) + p, 16)
    ) + key
    h = HEAD + sid + udid_raw + os.urandom(32)
    if auth:
        h += auth
    return base64.b64encode(struct.pack("<I", len(h)) + h + body)

def unpack(text, udid):
    raw = base64.b64decode(text)
    key, cipher = raw[-32:], raw[:-32]
    p = unpad(AES.new(key, AES.MODE_CBC, get_iv(udid)).decrypt(cipher), 16)
    n = struct.unpack("<I", p[:4])[0]
    return msgpack.unpackb(p[4 : 4 + n], raw=False, strict_map_key=False)


def get_gpu():
    if os.name != "nt":
        return "Generic GPU"
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Video") as vk:
            for i in range(winreg.QueryInfoKey(vk)[0]):
                guid = winreg.EnumKey(vk, i)
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Control\Video\{guid}\0000") as ak:
                        v, _ = winreg.QueryValueEx(ak, "HardwareInformation.AdapterString")
                        if isinstance(v, bytes):
                            v = v.decode("utf-16-le", errors="ignore")
                        s = str(v).replace("\x00", "").strip()
                        if s:
                            return s
                except OSError:
                    continue
    except Exception:
        pass
    return "Generic GPU"


def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(1.0)            # never hang the whole setup if offline
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def get_hwid():
    if os.name != "nt":
        return {"device_name": "linux", "graphics_device_name": "Generic GPU",
                "platform_os_version": "Linux 64bit", "ip_address": get_ip(),
                "device_id": hashlib.sha1(b"linux").hexdigest()}
    import platform, winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS") as bk:
        dn, _ = winreg.QueryValueEx(bk, "SystemProductName")
        dn = str(dn).strip()
        try:
            mfg, _ = winreg.QueryValueEx(bk, "BaseBoardManufacturer")
            if mfg:
                dn = f"{dn} ({str(mfg).strip()})"
        except OSError:
            pass
    machine_guid = ""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as ck:
            machine_guid, _ = winreg.QueryValueEx(ck, "MachineGuid")
    except OSError:
        pass
    return {
        "device_name": dn,
        "graphics_device_name": get_gpu(),
        "platform_os_version": f"Windows 11  ({platform.version()}) 64bit",
        "ip_address": get_ip(),
        "device_id": hashlib.sha1(f"{dn}_{machine_guid}".encode()).hexdigest(),
    }


# ---------- minimal API client ----------

class UmaClient:
    def __init__(self, cfg):
        self.viewer_id = cfg["viewer_id"]
        self.udid = cfg["udid"]
        self.auth_key = cfg["auth_key"]
        self.steam_id = str(cfg["steam_id"])
        self.steam_ticket = cfg["steam_session_ticket"]
        self.device_id = cfg["device_id"]
        self.device_name = cfg["device_name"]
        self.graphics_device = cfg["graphics_device_name"]
        self.ip_address = cfg["ip_address"]
        self.platform_os = cfg["platform_os_version"]
        self.app_ver = cfg["app_ver"]
        self.res_ver = cfg["res_ver"]
        self.unity_ver = cfg.get("unity_ver", "2022.3.62f2")
        self.sid = bytes(16)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": f"UnityPlayer/{self.unity_ver} (UnityWebRequest/1.0, libcurl/8.10.1-DEV)",
            "Accept": "*/*", "Accept-Encoding": "deflate, gzip",
            "Content-Type": "application/x-msgpack", "X-Unity-Version": self.unity_ver,
        })

    def common(self):
        return {
            "viewer_id": self.viewer_id, "device": 4, "device_id": self.device_id,
            "device_name": self.device_name, "graphics_device_name": self.graphics_device,
            "ip_address": self.ip_address, "platform_os_version": self.platform_os,
            "carrier": "", "keychain": 0, "locale": "JPN",
            "button_info": "", "dmm_viewer_id": None, "dmm_onetime_token": None,
            "steam_id": self.steam_id, "steam_session_ticket": self.steam_ticket,
        }

    def regen_sid(self):
        self.sid = make_sid(self.viewer_id, self.udid)

    def call(self, ep, args=None):
        payload = dict(args or {})
        payload.update(self.common())
        body = pack(self.sid, get_raw_udid(self.udid),
                    bytes.fromhex(self.auth_key), payload, self.udid)
        headers = {
            "SID": self.sid.hex(), "Device": "4",
            "ViewerID": str(self.viewer_id),
            "APP-VER": self.app_ver, "RES-VER": self.res_ver,
        }
        resp = self.session.post(BASE_URL + ep, data=body, headers=headers, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code} on {ep}: {resp.text[:300]}")
        res = unpack(resp.text.strip(), self.udid)
        dh = res.get("data_headers", {})
        rc = dh.get("result_code", 0)
        if rc != 1:
            raise RuntimeError(f"API error {rc} on {ep}: {json.dumps(res.get('data'), default=str)[:300]}")
        sid = dh.get("sid")
        if isinstance(sid, str) and sid.strip():
            self.sid = next_sid(sid)
        return res

    def login(self):
        self.regen_sid()
        self.call("tool/start_session", {"attestation_type": 0, "device_token": None})
        return self.call("load/index", {"adid": ""})

    def pre_single_mode(self):
        return self.call("pre_single_mode/index", {})


# ---------- auth_config: capture / save / load ----------

# Steam credentials in auth_config.json are encrypted. Three formats coexist:
#   enc3:<b64>  Windows DPAPI (CryptProtectData) — bound to *this user* on
#               *this PC*. What Chrome/Edge use. Cannot decrypt if copied to
#               another machine OR opened by a different Windows user.
#   enc2:<b64>  AES-CBC with a key derived from the Windows MachineGuid +
#               salt. Bound to the PC but not to the user. Used as fallback
#               if DPAPI isn't available (non-Windows, or registry blocked).
#   enc:<b64>   Legacy reversed-base64 placeholder (still readable for
#               transparent upgrade to enc3: on next load).

_KEY_SALT = b"heir_v1_steam_creds"

def _machine_key():
    """32-byte AES key bound to this PC (used when DPAPI isn't available)."""
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Cryptography") as k:
                mg, _ = winreg.QueryValueEx(k, "MachineGuid")
            return hashlib.sha256(_KEY_SALT + str(mg).encode("utf-8")).digest()
        except Exception:
            pass
    home = os.path.expanduser("~")
    return hashlib.sha256(_KEY_SALT + home.encode("utf-8")).digest()


# --- Windows DPAPI via ctypes (no pywin32 dep needed) ------------------------

class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_char))]

def _dpapi_protect(plain: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    bin_blob = _DataBlob(len(plain),
                         ctypes.cast(ctypes.c_char_p(plain), ctypes.POINTER(ctypes.c_char)))
    out_blob = _DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(bin_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    ):
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)

def _dpapi_unprotect(cipher: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    bin_blob = _DataBlob(len(cipher),
                         ctypes.cast(ctypes.c_char_p(cipher), ctypes.POINTER(ctypes.c_char)))
    out_blob = _DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(bin_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    ):
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)

def _dpapi_available():
    return os.name == "nt"


def _obf(s):
    """Encrypt with the strongest backend available. Prefers DPAPI (enc3:),
    falls back to AES-CBC + MachineGuid (enc2:)."""
    if not s or not isinstance(s, str):
        return s
    if s.startswith(("enc3:", "enc2:")):
        return s
    # auto-promote legacy enc: payloads
    if s.startswith("enc:"):
        try:
            s = base64.b64decode(s[4:]).decode("utf-8")[::-1]
        except Exception:
            return s
    if _dpapi_available():
        try:
            ct = _dpapi_protect(s.encode("utf-8"))
            return "enc3:" + base64.b64encode(ct).decode("ascii")
        except Exception:
            pass  # fall through to AES
    iv = os.urandom(16)
    ct = AES.new(_machine_key(), AES.MODE_CBC, iv).encrypt(pad(s.encode("utf-8"), 16))
    return "enc2:" + base64.b64encode(iv + ct).decode("ascii")


def _deobf(s):
    if not s or not isinstance(s, str):
        return s
    if s.startswith("enc3:"):
        try:
            return _dpapi_unprotect(base64.b64decode(s[5:])).decode("utf-8")
        except Exception:
            return s
    if s.startswith("enc2:"):
        try:
            raw = base64.b64decode(s[5:])
            iv, ct = raw[:16], raw[16:]
            return unpad(AES.new(_machine_key(), AES.MODE_CBC, iv).decrypt(ct), 16).decode("utf-8")
        except Exception:
            return s
    if s.startswith("enc:"):
        try:
            return base64.b64decode(s[4:]).decode("utf-8")[::-1]
        except Exception:
            return s
    return s


def fresh_auth(cfg):
    try:
        if int(cfg.get("auth_key_len") or 0) != 48:
            return False
        ak = str(cfg.get("auth_key") or "").lower()
        if not re.fullmatch(r"[0-9a-f]+", ak) or len(ak) < 32 or len(ak) % 2:
            return False
        udid = str(cfg.get("udid") or "")
        if len(udid) != 36 or udid.count("-") != 4:
            return False
        return bool(cfg.get("viewer_id") and cfg.get("app_ver") and cfg.get("res_ver"))
    except Exception:
        return False


def load_saved_auth():
    if not AUTH_PATH.exists():
        return None
    try:
        with open(AUTH_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        needs_rewrite = False
        for k in ("steam_username", "steam_password"):
            if k in cfg:
                if str(cfg[k]).startswith("enc:"):
                    needs_rewrite = True
                cfg[k] = _deobf(cfg[k])
        if not fresh_auth(cfg):
            return None
        if needs_rewrite:
            # transparently upgrade legacy enc: to enc2:
            save_auth(cfg)
        return cfg
    except Exception as e:
        print(f"[-] auth_config.json corrupto: {e}")
        return None


def save_auth(cfg):
    save = dict(cfg)
    for k in ("steam_username", "steam_password"):
        if k in save:
            save[k] = _obf(save[k])
    with open(AUTH_PATH, "w", encoding="utf-8") as f:
        json.dump(save, f, indent=2)


def launch_game():
    if os.name != "nt":
        print("[-] Lanzar el juego solo soportado en Windows.")
        return False
    try:
        os.startfile(f"steam://rungameid/{APP_ID}")
        return True
    except Exception as e:
        print(f"[-] No se pudo lanzar el juego: {e}")
        return False


def capture_auth(timeout_sec=240):
    try:
        import frida
    except ImportError:
        raise RuntimeError("frida no instalado: pip install frida")

    print("[*] Lanzando Umamusume via Steam...", flush=True)
    launch_game()
    print(f"[*] Esperando hasta {timeout_sec}s. Loguéate y entra al menú principal.\n"
          f"    (cuando veas tu home con tus umas, ya estará capturado)", flush=True)

    captured = {}
    session = None
    deadline = time.time() + timeout_sec

    def on_msg(msg, data):
        if msg.get("type") == "error":
            print(f"[frida-err] {msg.get('description')}", flush=True)
            return
        p = msg.get("payload") or {}
        if p.get("type") == "creds" and p.get("app_ver") and p.get("res_ver"):
            captured.update(p)
            print(f"[+] Capturado {p.get('endpoint')} — viewer_id={p.get('viewer_id')}", flush=True)

    while time.time() < deadline:
        try:
            session = frida.attach(PROCESS_NAME)
            print("[+] Frida attached", flush=True)
            break
        except Exception:
            time.sleep(1)
    if not session:
        raise RuntimeError(f"Timeout esperando a {PROCESS_NAME}")

    script = session.create_script(FRIDA_JS)
    script.on("message", on_msg)
    script.load()

    try:
        while time.time() < deadline:
            if fresh_auth(captured):
                time.sleep(1)
                return dict(captured)
            time.sleep(0.5)
    finally:
        try:
            session.detach()
        except Exception:
            pass
    raise RuntimeError("Timeout sin capturar credenciales válidas.")


def write_trace(load_res, pre_res):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Breeding traces live in the safe (AppData) store so they survive deleting
    # the project folder (migrated on first use).
    out = safe_store.breeding_dir() / f"heir_capture_{ts}.jsonl"

    def _default(o):
        if isinstance(o, bytes):
            return o.hex()
        return str(o)

    with open(out, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": time.time(), "direction": "RES",
            "endpoint": "load/index", "data": load_res,
        }, ensure_ascii=False, default=_default) + "\n")
        f.write(json.dumps({
            "ts": time.time(), "direction": "RES",
            "endpoint": "pre_single_mode/index", "data": pre_res,
        }, ensure_ascii=False, default=_default) + "\n")
    return out


def main():
    cfg = load_saved_auth()

    if cfg is None:
        print("[*] Sin auth_config.json. Primera captura — necesito que abras el juego.\n")
        captured = capture_auth()
        cfg = dict(captured)
        cfg.update(get_hwid())

        print("\n[*] Captura OK. Ahora Steam:")
        print("    Las credenciales se guardan obfuscadas en auth_config.json (LOCAL, NO subir al repo).")
        cfg["steam_username"] = input("    Steam username: ").strip()
        cfg["steam_password"] = input("    Steam password: ")

        save_auth(cfg)
        print(f"[+] Guardado {AUTH_PATH.name}\n")
    else:
        print(f"[+] auth_config.json encontrado (viewer_id={cfg['viewer_id']})")
        if "device_id" not in cfg:
            cfg.update(get_hwid())

    print("[*] Generando Steam session ticket...")
    user = cfg.get("steam_username")
    pwd = cfg.get("steam_password")
    if not user or not pwd:
        user = input("    Steam username: ").strip()
        pwd = input("    Steam password: ")
        cfg["steam_username"] = user
        cfg["steam_password"] = pwd
    try:
        sid, tkt = get_steam_ticket(user, pwd)
    except RuntimeError as e:
        if "STEAM_GUARD_REQUIRED" in str(e):
            code = input("    Steam Guard code: ").strip()
            sid, tkt = get_steam_ticket(user, pwd, code)
        else:
            raise
    cfg["steam_id"] = sid
    cfg["steam_session_ticket"] = tkt
    save_auth(cfg)
    print(f"[+] Ticket OK (steam_id={sid})")

    print("[*] Login al servidor del juego...")
    client = UmaClient(cfg)
    load_res = client.login()
    print("[+] load/index OK")
    pre_res = client.pre_single_mode()
    print("[+] pre_single_mode/index OK")

    out = write_trace(load_res, pre_res)
    mine = len((load_res.get("data") or {}).get("trained_chara") or [])
    rent = len(((pre_res.get("data") or {}).get("succession_trained_chara_data") or {})
               .get("succession_trained_chara_array") or [])
    print(f"\n[+] Trace escrito: {out.relative_to(ROOT)}")
    print(f"    {mine} umas tuyas + {rent} padres prestables")
    print(f"\nAhora puedes:  python server.py   (UI en http://localhost:1620)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[!] Cancelado.")
        sys.exit(1)
    except Exception as e:
        print(f"\n[-] Error: {e}")
        sys.exit(1)
