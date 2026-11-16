import os
import sys

if os.name != "nt":
    sys.exit("Bu uygulama yalnızca Windows'ta çalışır. / Windows only.")

import re
import json
import time
import types
import shutil
import ctypes
import logging
import importlib
import importlib.util
import threading
import subprocess
import winreg
from ctypes import wintypes
from logging.handlers import RotatingFileHandler

APP_NAME = "YanardagWallpaper"
WINDOW_TITLE = "Yanardağ Wallpaper Engine"
LOCAL_APP_DATA = os.getenv("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
APP_DIR = os.path.join(LOCAL_APP_DATA, APP_NAME)
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
LOG_FILE = os.path.join(APP_DIR, "wallpaper.log")
CRASH_LOG = os.path.join(APP_DIR, "crash.log")
BG_STATE_FILE = os.path.join(APP_DIR, "bg.json")
STATUS_FILE = os.path.join(APP_DIR, "status.json")
HB_FILE = os.path.join(APP_DIR, "hb.txt")
UPDATE_STAMP = os.path.join(APP_DIR, "update_check.txt")
RELEASE_MARK = os.path.join(APP_DIR, "release_try.txt")
RELEASE_OFF = os.path.join(APP_DIR, "release_off.txt")
LEGACY_PID_FILE = os.path.join(APP_DIR, "pid.txt")
MUTEX_NAME = "YanardagWallpaper_App_Mutex"
MPV_MUTEX_NAME = "YanardagWallpaper_MpvInstall_Mutex"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
MIN_SUPPORTED_BUILD = 9600
RAISED_BUILD = 26100
UPDATE_INTERVAL = 24 * 3600
FRAME_TIMEOUT = 20

os.makedirs(APP_DIR, exist_ok=True)

FROZEN = getattr(sys, "frozen", False)
BASE_DIR = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))

log = logging.getLogger("yanardag")


def setup_logging(tag):
    if log.handlers:
        return
    handler = RotatingFileHandler(LOG_FILE, maxBytes=512 * 1024, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter(f"%(asctime)s %(levelname)s [{tag}] %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    def _hook(exc_type, exc, tb):
        log.error("Yakalanmamış hata", exc_info=(exc_type, exc, tb))

    sys.excepthook = _hook


user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, wintypes.HDC,
                                     ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class ACCENTPOLICY(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_uint),
        ("AccentFlags", ctypes.c_uint),
        ("GradientColor", ctypes.c_uint),
        ("AnimationId", ctypes.c_uint),
    ]


class WINDOWCOMPOSITIONATTRIBDATA(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_int),
        ("Data", ctypes.c_void_p),
        ("SizeOfData", ctypes.c_size_t),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
    _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                ("StateMask", wintypes.ULONG)]


class PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def _sig(dll, name, argtypes, restype):
    fn = getattr(dll, name)
    fn.argtypes = argtypes
    fn.restype = restype
    return fn


_sig(user32, "FindWindowW", [wintypes.LPCWSTR, wintypes.LPCWSTR], wintypes.HWND)
_sig(user32, "FindWindowExW", [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR], wintypes.HWND)
_sig(user32, "SendMessageTimeoutW", [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                     wintypes.UINT, wintypes.UINT, ctypes.c_void_p], LRESULT)
_sig(user32, "PostMessageW", [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL)
_sig(user32, "EnumWindows", [WNDENUMPROC, wintypes.LPARAM], wintypes.BOOL)
_sig(user32, "EnumDisplayMonitors", [wintypes.HDC, ctypes.c_void_p, MONITORENUMPROC, wintypes.LPARAM], wintypes.BOOL)
_sig(user32, "IsWindow", [wintypes.HWND], wintypes.BOOL)
_sig(user32, "IsWindowVisible", [wintypes.HWND], wintypes.BOOL)
_sig(user32, "IsIconic", [wintypes.HWND], wintypes.BOOL)
_sig(user32, "IsZoomed", [wintypes.HWND], wintypes.BOOL)
_sig(user32, "GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD)
_sig(user32, "GetParent", [wintypes.HWND], wintypes.HWND)
_sig(user32, "SetParent", [wintypes.HWND, wintypes.HWND], wintypes.HWND)
_sig(user32, "GetWindow", [wintypes.HWND, wintypes.UINT], wintypes.HWND)
_sig(user32, "SetWindowPos", [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL)
_sig(user32, "GetWindowRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL)
_sig(user32, "GetClassNameW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int)
_sig(user32, "MonitorFromWindow", [wintypes.HWND, wintypes.DWORD], ctypes.c_void_p)
_sig(user32, "GetMonitorInfoW", [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)], wintypes.BOOL)
_sig(user32, "GetSystemMetrics", [ctypes.c_int], ctypes.c_int)
_sig(user32, "SetLayeredWindowAttributes", [wintypes.HWND, wintypes.DWORD, ctypes.c_ubyte, wintypes.DWORD],
     wintypes.BOOL)
_sig(user32, "SystemParametersInfoW", [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT],
     wintypes.BOOL)

_get_window_long = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
_set_window_long = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
_get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
_get_window_long.restype = ctypes.c_ssize_t
_set_window_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
_set_window_long.restype = ctypes.c_ssize_t

try:
    _set_wca = _sig(user32, "SetWindowCompositionAttribute",
                    [wintypes.HWND, ctypes.POINTER(WINDOWCOMPOSITIONATTRIBDATA)], wintypes.BOOL)
except AttributeError:
    _set_wca = None

_sig(kernel32, "CreateMutexW", [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE)
_sig(kernel32, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL)
_sig(kernel32, "OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE)
_sig(kernel32, "QueryFullProcessImageNameW", [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                             ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL)
_sig(kernel32, "CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE)
_sig(kernel32, "SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
     wintypes.BOOL)
_sig(kernel32, "AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL)

try:
    dwmapi = ctypes.WinDLL("dwmapi")
    dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
except Exception:
    dwmapi = None

try:
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
except Exception:
    psapi = None

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_POPUP = 0x80000000
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000
WS_CLIPSIBLINGS = 0x04000000
WS_CLIPCHILDREN = 0x02000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOREDIRECTIONBITMAP = 0x00200000
LWA_ALPHA = 0x2
GW_HWNDPREV = 3
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SMTO_NORMAL = 0x0000
MONITOR_DEFAULTTONEAREST = 2
ERROR_ALREADY_EXISTS = 183
SPI_SETDESKWALLPAPER = 0x0014
SPI_GETDESKWALLPAPER = 0x0073
SPIF_SENDCHANGE = 0x0002
WM_CLOSE = 0x0010
JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
BELOW_NORMAL_PRIORITY_CLASS = 0x4000

COVER_IGNORED_CLASSES = {"WorkerW", "Progman", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                         "SHELLDLL_DefView", "XamlExplorerHostIslandWindow", "Windows.UI.Core.CoreWindow"}


def read_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return None


def write_config(data):
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    for _ in range(10):
        try:
            os.replace(tmp, CONFIG_FILE)
            return
        except PermissionError:
            time.sleep(0.03)
    try:
        os.remove(tmp)
    except OSError:
        pass
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def update_config(**kv):
    cfg = None
    for _ in range(6):
        cfg = read_config()
        if cfg is not None:
            break
        time.sleep(0.05)
    if cfg is None:
        cfg = {}
    cfg.update(kv)
    write_config(cfg)


_last_status = None


def set_status(engine, state, detail=""):
    global _last_status
    cur = (engine, state, detail)
    if cur == _last_status:
        return
    _last_status = cur
    try:
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"engine": engine, "state": state, "detail": detail}, f)
        os.replace(tmp, STATUS_FILE)
    except OSError:
        pass


def read_status():
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def touch(path):
    try:
        open(path, "w").close()
    except OSError:
        pass


def remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def windows_build():
    try:
        return sys.getwindowsversion().build
    except Exception:
        return 0


def python_exe():
    d, n = os.path.split(sys.executable)
    if n.lower() == "pythonw.exe":
        p = os.path.join(d, "python.exe")
        if os.path.exists(p):
            return p
    return sys.executable


def pip(*args, timeout=900, capture=False):
    cmd = [python_exe(), "-m", "pip", "--disable-pip-version-check", *args]
    kw = dict(stdin=subprocess.DEVNULL, timeout=timeout)
    if capture or not sys.stdout:
        kw.update(stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, text=True, creationflags=0x08000000)
    try:
        return subprocess.run(cmd, **kw)
    except Exception:
        return None


def pip_install(*pkgs, upgrade=False):
    if FROZEN:
        return False
    base = ["install", "--no-input"] + (["--upgrade"] if upgrade else [])
    for extra in ([], ["--user"]):
        r = pip(*base, *extra, *pkgs)
        if r and r.returncode == 0:
            return True
    return False


def has_module(name):
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


_qt_cache = None


def qt_package(force=False):
    global _qt_cache
    if not force and _qt_cache is not None and time.monotonic() - _qt_cache[0] < 15:
        return _qt_cache[1]
    res = None
    for p in ("PyQt6", "PyQt5"):
        if has_module(p + ".QtMultimediaWidgets"):
            res = p
            break
    _qt_cache = (time.monotonic(), res)
    return res


def install_qt():
    if FROZEN:
        return False
    ok = pip_install("PyQt6") or pip_install("PyQt5")
    importlib.invalidate_caches()
    qt_package(True)
    return ok


def check_outdated(names):
    r = pip("list", "--outdated", "--format=json", timeout=90, capture=True)
    if not r or r.returncode != 0:
        return []
    try:
        wanted = {n.lower() for n in names}
        return [p["name"] for p in json.loads(r.stdout) if p["name"].lower() in wanted]
    except Exception:
        return []


def update_due():
    try:
        last = os.path.getmtime(UPDATE_STAMP)
    except OSError:
        last = 0
    return time.time() - last >= UPDATE_INTERVAL


def mark_update_checked():
    try:
        with open(UPDATE_STAMP, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def qt_wanted(cfg):
    pref = cfg.get("engine", "auto")
    if pref == "qt":
        return True
    return pref == "auto" and not cfg.get("qt_failed") and windows_build() < RAISED_BUILD


def choose_engine(cfg):
    if not qt_wanted(cfg):
        return "mpv"
    return "qt" if qt_package() else "mpv"


def mark_qt_failed(cfg):
    try:
        if (cfg or {}).get("engine", "auto") == "qt":
            update_config(engine="mpv")
        else:
            update_config(qt_failed=True)
    except OSError:
        log.exception("qt_failed yazılamadı")


def find_mpv(config=None):
    import glob
    cfg = config or {}
    env = os.environ.get
    local = env("LOCALAPPDATA", "")
    home = os.path.expanduser("~")
    pfs = [p for p in (env("ProgramFiles"), env("ProgramW6432"), env("ProgramFiles(x86)")) if p]
    cands = [cfg.get("mpv_path") or ""]
    roots = [BASE_DIR, APP_DIR]
    if FROZEN and hasattr(sys, "_MEIPASS"):
        roots.insert(0, sys._MEIPASS)
    for root in roots:
        cands += [os.path.join(root, "mpv.exe"), os.path.join(root, "mpv", "mpv.exe")]
    cands.append(shutil.which("mpv") or "")
    cands += [
        os.path.join(local, "Microsoft", "WinGet", "Links", "mpv.exe"),
        os.path.join(local, "Programs", "mpv", "mpv.exe"),
        os.path.join(local, "Programs", "MPV Player", "mpv.exe"),
        os.path.join(home, "scoop", "apps", "mpv", "current", "mpv.exe"),
        os.path.join(home, "scoop", "shims", "mpv.exe"),
        os.path.join(env("ProgramData", r"C:\ProgramData"), "chocolatey", "bin", "mpv.exe"),
        os.path.join(local, "Programs", "Lively Wallpaper", "Plugins", "mpv", "mpv.exe"),
    ]
    for pf in pfs:
        cands += [os.path.join(pf, "MPV Player", "mpv.exe"), os.path.join(pf, "mpv", "mpv.exe"),
                  os.path.join(pf, "Lively Wallpaper", "Plugins", "mpv", "mpv.exe")]
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths\mpv.exe") as k:
                cands.append(winreg.QueryValueEx(k, "")[0])
        except OSError:
            pass
        try:
            with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall") as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    try:
                        with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sk:
                            name = str(winreg.QueryValueEx(sk, "DisplayName")[0]).lower()
                            if "mpv" in name and "mpv.net" not in name:
                                loc = winreg.QueryValueEx(sk, "InstallLocation")[0]
                                cands.append(os.path.join(loc, "mpv.exe"))
                    except OSError:
                        continue
        except OSError:
            pass
    for c in cands:
        c = (c or "").strip('"')
        if c and os.path.isfile(c):
            return c
    patterns = [os.path.join(local, "Microsoft", "WinGet", "Packages", "*mpv*", "**", "mpv.exe"),
                os.path.join(APP_DIR, "mpv", "**", "mpv.exe")]
    patterns += [os.path.join(pf, "*", "mpv.exe") for pf in pfs]
    for pat in patterns:
        for c in glob.glob(pat, recursive=True):
            if os.path.isfile(c):
                return c
    return None


def download_portable_mpv():
    import urllib.request
    api = "https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases/latest"
    req = urllib.request.Request(api, headers={"User-Agent": APP_NAME})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    asset = None
    for a in data.get("assets", []):
        if re.match(r"^mpv-x86_64-\d{8}-git-[0-9a-f]+\.7z$", a.get("name", "")):
            asset = a
            break
    if not asset:
        log.error("GitHub sürümünde uygun mpv paketi bulunamadı")
        return None
    archive = os.path.join(APP_DIR, asset["name"])
    part = archive + ".part"
    log.info("mpv indiriliyor: %s", asset["browser_download_url"])
    dl = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": APP_NAME})
    with urllib.request.urlopen(dl, timeout=60) as r, open(part, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    os.replace(part, archive)
    dest = os.path.join(APP_DIR, "mpv")
    os.makedirs(dest, exist_ok=True)
    extracted = False
    if not has_module("py7zr") and not FROZEN:
        pip_install("py7zr")
        importlib.invalidate_caches()
    try:
        import py7zr
        with py7zr.SevenZipFile(archive, "r") as z:
            z.extractall(path=dest)
        extracted = True
    except Exception:
        log.exception("py7zr ile açılamadı, tar denenecek")
    if not extracted:
        tar = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tar.exe")
        r = subprocess.run([tar, "-xf", archive, "-C", dest], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        extracted = r.returncode == 0
    remove(archive)
    if not extracted:
        log.error("mpv arşivi açılamadı")
        return None
    return find_mpv(read_config() or {})


def _install_mpv_locked():
    log.info("mpv kuruluyor (winget)...")
    local = os.environ.get("LOCALAPPDATA", "")
    for exe in ("winget", os.path.join(local, "Microsoft", "WindowsApps", "winget.exe")):
        try:
            r = subprocess.run(
                [exe, "install", "--id", "shinchiro.mpv", "-e", "--source", "winget", "--silent",
                 "--accept-package-agreements", "--accept-source-agreements"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=900, creationflags=subprocess.CREATE_NO_WINDOW)
            log.info("winget çıkış kodu: %s", r.returncode)
            break
        except FileNotFoundError:
            continue
        except Exception:
            log.exception("winget çalıştırılamadı")
            break
    found = find_mpv(read_config() or {})
    if found:
        return found
    log.warning("winget ile kurulamadı, taşınabilir sürüm indirilecek")
    try:
        return download_portable_mpv()
    except Exception:
        log.exception("Taşınabilir mpv indirilemedi")
        return None


def install_mpv():
    deadline = time.monotonic() + 600
    while True:
        h = kernel32.CreateMutexW(None, False, MPV_MUTEX_NAME)
        if not h or ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
            break
        kernel32.CloseHandle(h)
        found = find_mpv(read_config() or {})
        if found:
            return found
        if time.monotonic() > deadline:
            return None
        time.sleep(3)
    try:
        found = find_mpv(read_config() or {})
        return found or _install_mpv_locked()
    finally:
        if h:
            kernel32.CloseHandle(h)


def pid_alive(pid):
    if not pid:
        return False
    h = kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    try:
        buf = ctypes.create_unicode_buffer(520)
        n = wintypes.DWORD(520)
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return False
        name = os.path.basename(buf.value).lower()
        return "python" in name or name == os.path.basename(sys.executable).lower()
    finally:
        kernel32.CloseHandle(h)


def bg_state():
    try:
        with open(BG_STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def bg_pid():
    try:
        return int(bg_state().get("pid", 0))
    except (TypeError, ValueError):
        return 0


def heartbeat_fresh(max_age=6.0):
    try:
        return time.time() - os.path.getmtime(HB_FILE) < max_age
    except OSError:
        return False


def start_heartbeat():
    def run():
        while True:
            try:
                with open(HB_FILE, "w") as f:
                    f.write(str(time.time()))
            except OSError:
                pass
            time.sleep(1)

    threading.Thread(target=run, daemon=True).start()


def code_stamp():
    try:
        path = sys.executable if FROZEN else os.path.abspath(__file__)
        return f"{path}|{os.path.getmtime(path)}"
    except OSError:
        return ""


def write_bg_state():
    try:
        with open(BG_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "stamp": code_stamp()}, f)
    except OSError:
        pass


def _taskkill(pid):
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], creationflags=0x08000000,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)


def stop_background(wait=4.0):
    pid = bg_pid()
    if not pid_alive(pid):
        return
    try:
        update_config(quit=True)
    except OSError:
        pass
    t0 = time.time()
    while time.time() - t0 < wait and pid_alive(pid):
        time.sleep(0.2)
    if pid_alive(pid):
        _taskkill(pid)
        time.sleep(0.4)
    try:
        update_config(quit=False)
    except OSError:
        pass


def stop_outdated_background():
    st = bg_state()
    if st.get("pid") and st.get("stamp") != code_stamp():
        stop_background()


def stop_legacy_background():
    try:
        with open(LEGACY_PID_FILE, "r") as f:
            pid = int(f.read().strip().split("|")[0])
    except (OSError, ValueError):
        return
    if pid and pid != os.getpid() and pid_alive(pid):
        _taskkill(pid)
        time.sleep(0.5)
    remove(LEGACY_PID_FILE)


def wallpaper_command():
    if FROZEN:
        return [sys.executable, "--wallpaper"]
    exe = sys.executable
    if os.path.basename(exe).lower() == "python.exe":
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pyw):
            exe = pyw
    return [exe, os.path.abspath(__file__), "--wallpaper"]


def spawn_wallpaper():
    env = os.environ.copy()
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    env.pop("_MEIPASS2", None)
    return subprocess.Popen(
        wallpaper_command(), env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True,
    )


def ensure_background_process():
    if heartbeat_fresh() or pid_alive(bg_pid()):
        return True
    try:
        spawn_wallpaper()
        return True
    except OSError:
        log.exception("Arka plan süreci başlatılamadı")
        return False


def startup_command_string():
    return subprocess.list2cmdline(wallpaper_command())


def get_startup_value():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            return winreg.QueryValueEx(key, APP_NAME)[0]
    except OSError:
        return None


def set_startup(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, startup_command_string())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def close_previous_manager():
    for _ in range(5):
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if not hwnd:
            return
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value or pid.value == os.getpid() or not pid_alive(pid.value):
            return
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        t0 = time.time()
        while time.time() - t0 < 3 and user32.IsWindow(hwnd):
            time.sleep(0.1)
        if user32.IsWindow(hwnd):
            _taskkill(pid.value)
            time.sleep(0.3)


def force_windows_transparency():
    path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
            try:
                current = winreg.QueryValueEx(key, "EnableTransparency")[0]
            except FileNotFoundError:
                current = None
            if current != 1:
                winreg.SetValueEx(key, "EnableTransparency", 0, winreg.REG_DWORD, 1)
    except OSError:
        pass


def get_taskbar_windows():
    hwnds = []
    main = user32.FindWindowW("Shell_TrayWnd", None)
    if main:
        hwnds.append(main)
    prev = None
    while True:
        prev = user32.FindWindowExW(None, prev, "Shell_SecondaryTrayWnd", None)
        if not prev:
            break
        hwnds.append(prev)
    return hwnds


def set_taskbar_transparent(transparent, opacity, hwnds=None):
    if _set_wca is None:
        return False
    try:
        accent = ACCENTPOLICY()
        accent.AccentState = (4 if windows_build() >= 17134 else 3) if transparent else 0
        accent.AccentFlags = 2 if transparent else 0
        accent.GradientColor = (((int(opacity) & 0xFF) << 24) | 0x001A1A1A) if transparent else 0
        data = WINDOWCOMPOSITIONATTRIBDATA()
        data.Attribute = 19
        data.Data = ctypes.addressof(accent)
        data.SizeOfData = ctypes.sizeof(accent)
        applied = False
        for hwnd in (hwnds if hwnds is not None else get_taskbar_windows()):
            _set_wca(hwnd, ctypes.byref(data))
            applied = True
        return applied
    except Exception:
        log.exception("Görev çubuğu saydamlığı uygulanamadı")
        return False


class TaskbarBlur:

    def __init__(self):
        self.on = False
        self.opacity = None
        self.hwnds = []
        self.ticks = 0

    def update(self, cfg):
        want = bool(cfg.get("taskbar_transparent", False))
        try:
            opacity = max(0, min(255, int(cfg.get("taskbar_opacity", 37))))
        except (TypeError, ValueError):
            opacity = 37
        if want:
            if not self.on:
                force_windows_transparency()
            self.ticks += 1
            if not self.hwnds or self.ticks % 5 == 0:
                self.hwnds = get_taskbar_windows()
            self.opacity = opacity
            self.on = True
            self.reapply()
        elif self.on:
            self.off()

    def reapply(self):
        if self.on and self.opacity is not None:
            set_taskbar_transparent(True, self.opacity, self.hwnds)

    def off(self):
        if self.on:
            set_taskbar_transparent(False, 0)
        self.on = False
        self.hwnds = []


def set_dpi_awareness():
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def disable_power_throttling():
    try:
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetProcessInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.SetProcessInformation.restype = wintypes.BOOL
        st = PROCESS_POWER_THROTTLING_STATE(1, 1, 0)
        kernel32.SetProcessInformation(kernel32.GetCurrentProcess(), 4, ctypes.byref(st), ctypes.sizeof(st))
    except Exception:
        pass


def memory_mb():
    if not psapi:
        return 0.0
    pmc = PMC()
    pmc.cb = ctypes.sizeof(pmc)
    if psapi.GetProcessMemoryInfo(ctypes.c_void_p(-1), ctypes.byref(pmc), pmc.cb):
        return pmc.WorkingSetSize / 1048576
    return 0.0


def class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def get_monitors():
    rects = []

    def cb(hmon, hdc, lprect, lparam):
        r = lprect.contents
        rects.append((r.left, r.top, r.right, r.bottom))
        return True

    proc = MONITORENUMPROC(cb)
    user32.EnumDisplayMonitors(None, None, proc, 0)
    rects.sort()
    return rects


def primary_monitor():
    mons = get_monitors()
    for m in mons:
        if m[0] <= 0 < m[2] and m[1] <= 0 < m[3]:
            return m
    if mons:
        return mons[0]
    return (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))


class DesktopHost:

    def __init__(self, parent, defview, raised, mode):
        self.parent = parent
        self.defview = defview
        self.raised = raised
        self.mode = mode

    def valid(self):
        if not self.parent or not user32.IsWindow(self.parent):
            return False
        if self.raised and (not self.defview or not user32.IsWindow(self.defview)
                            or user32.GetParent(self.defview) != self.parent):
            return False
        return True

    def __repr__(self):
        return f"<{self.mode} parent={self.parent} defview={self.defview}>"


def _find_classic_workerw():
    found = []

    def cb(hwnd, _):
        if user32.FindWindowExW(hwnd, None, "SHELLDLL_DefView", None):
            ww = user32.FindWindowExW(None, hwnd, "WorkerW", None)
            if ww:
                found.append(ww)
        return True

    proc = WNDENUMPROC(cb)
    user32.EnumWindows(proc, 0)
    return found[0] if found else None


def detect_desktop_host():
    progman = user32.FindWindowW("Progman", None)
    if not progman:
        return None

    user32.SendMessageTimeoutW(progman, 0x052C, 0xD, 1, SMTO_NORMAL, 1000, None)

    progman_ex = _get_window_long(progman, GWL_EXSTYLE) & 0xFFFFFFFF
    defview = user32.FindWindowExW(progman, None, "SHELLDLL_DefView", None)

    if defview and (progman_ex & WS_EX_NOREDIRECTIONBITMAP):
        return DesktopHost(progman, defview, True, "raised(24H2+)")

    ww = _find_classic_workerw()
    if not ww:
        user32.SendMessageTimeoutW(progman, 0x052C, 0, 0, SMTO_NORMAL, 1000, None)
        time.sleep(0.1)
        ww = _find_classic_workerw()
    if ww:
        return DesktopHost(ww, None, False, "classic-WorkerW")

    defview = user32.FindWindowExW(progman, None, "SHELLDLL_DefView", None)
    if defview:
        return DesktopHost(progman, defview, True, "progman-defview")
    return DesktopHost(progman, None, False, "progman-fallback")


def refresh_system_wallpaper():
    try:
        buf = ctypes.create_unicode_buffer(1024)
        user32.SystemParametersInfoW(SPI_GETDESKWALLPAPER, 1024, buf, 0)
        user32.SystemParametersInfoW(SPI_SETDESKWALLPAPER, 0, buf, SPIF_SENDCHANGE)
    except Exception:
        log.exception("Sistem duvar kağıdı yenilenemedi")


def window_cover(hwnd, own_pid):
    if not hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return 0, None
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return 0, None
    mon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not mon:
        return 0, None
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        return 0, None
    m = mi.rcMonitor
    full = (rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom)
    if not (full or user32.IsZoomed(hwnd)):
        return 0, None
    if dwmapi:
        cloaked = wintypes.DWORD(0)
        if dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4) == 0 and cloaked.value:
            return 0, None
    ex = _get_window_long(hwnd, GWL_EXSTYLE)
    if ex & (WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT):
        return 0, None
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == own_pid:
        return 0, None
    if class_name(hwnd) in COVER_IGNORED_CLASSES:
        return 0, None
    return (2 if full else 1), (m.left, m.top, m.right, m.bottom)


def cover_states(monitors):
    result = {m: 0 for m in monitors}
    own = os.getpid()

    def cb(hwnd, _):
        st, mrect = window_cover(hwnd, own)
        if st and mrect in result and st > result[mrect]:
            result[mrect] = st
        return True

    try:
        proc = WNDENUMPROC(cb)
        user32.EnumWindows(proc, 0)
    except Exception:
        log.exception("Pencere taraması başarısız")
    return result


class KillOnCloseJob:

    def __init__(self):
        self.handle = kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            return
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(self.handle, JobObjectExtendedLimitInformation,
                                         ctypes.byref(info), ctypes.sizeof(info))

    def assign(self, popen):
        if self.handle:
            try:
                kernel32.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(popen._handle)))
            except Exception:
                pass


def find_windows_of_pid(pid, wanted_class=None):
    found = []

    def cb(hwnd, _):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and (wanted_class is None or class_name(hwnd) == wanted_class):
            found.append(hwnd)
        return True

    proc = WNDENUMPROC(cb)
    user32.EnumWindows(proc, 0)
    return found


class MpvInstance:

    def __init__(self, mpv, video, mon, index, job, safe=False):
        self.mpv, self.video, self.mon, self.index, self.job = mpv, video, mon, index, job
        self.safe = safe
        self.proc = None
        self.hwnd = None
        self.pipe = rf"\\.\pipe\yanardag-mpv-{os.getpid()}-{index}"
        self.paused = False

    def start(self):
        l, t, r, b = self.mon
        cfg = read_config() or {}
        args = [
            self.mpv, self.video,
            "--no-config", "--load-scripts=no", "--osc=no", "--osd-level=0",
            "--no-input-default-bindings", "--input-vo-keyboard=no",
            "--cursor-autohide=always",
            "--loop-file=inf", "--keep-open=no", "--idle=no",
            "--sid=no", "--hwdec=auto-safe",
            "--force-window=yes", "--border=no",
            "--keepaspect-window=no", "--panscan=1.0",
            f"--geometry={r - l}x{b - t}",
            f"--title=YanardagWallpaper-{self.index}",
            f"--input-ipc-server={self.pipe}",
        ]
        if not self.safe:
            args += ["--no-window-dragging", "--taskbar-progress=no", "--title-bar=no",
                     "--snap-window=no", "--input-cursor=no"]
        if cfg.get("audio"):
            args += ["--volume=" + str(int(cfg.get("volume", 50)))]
        else:
            args += ["--aid=no", "--mute=yes"]
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     creationflags=subprocess.CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS)
        self.job.assign(self.proc)
        log.info("mpv başladı (pid=%s) monitör=%s", self.proc.pid, self.mon)

    def wait_window(self, timeout=15.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                log.error("mpv kapandı (çıkış kodu %s). Video dosyası açılamıyor olabilir.", self.proc.returncode)
                return None
            wins = [h for h in find_windows_of_pid(self.proc.pid, "mpv") if user32.IsWindowVisible(h)]
            if wins:
                self.hwnd = wins[0]
                return self.hwnd
            time.sleep(0.1)
        log.error("mpv penceresi %s sn içinde bulunamadı", timeout)
        return None

    def embed(self, host):
        hwnd = self.hwnd
        l, t, r, b = self.mon
        w, h = r - l, b - t
        vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        x, y = l - vx, t - vy

        ex = WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        if host.raised:
            ex |= WS_EX_LAYERED
        _set_window_long(hwnd, GWL_EXSTYLE, ex)
        if host.raised:
            user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)

        _set_window_long(hwnd, GWL_STYLE, WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS)
        user32.SetParent(hwnd, host.parent)

        flags = SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW
        if host.raised:
            user32.SetWindowPos(hwnd, host.defview, x, y, w, h, flags)
        else:
            user32.SetWindowPos(hwnd, None, x, y, w, h, flags | SWP_NOZORDER)

        r_ = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r_))
        log.info("mpv gömüldü: hwnd=%s ebeveyn=%s rect=(%d,%d,%d,%d) style=0x%X ex=0x%X",
                 hwnd, user32.GetParent(hwnd), r_.left, r_.top, r_.right, r_.bottom,
                 _get_window_long(hwnd, GWL_STYLE) & 0xFFFFFFFF,
                 _get_window_long(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF)

    def alive(self, host):
        return (self.proc is not None and self.proc.poll() is None and self.hwnd
                and user32.IsWindow(self.hwnd) and user32.GetParent(self.hwnd) == host.parent)

    def command(self, *cmd):
        try:
            with open(self.pipe, "r+b", buffering=0) as f:
                f.write((json.dumps({"command": list(cmd)}) + "\n").encode("utf-8"))
            return True
        except OSError:
            return False

    def set_paused(self, paused):
        if paused != self.paused and self.command("set_property", "pause", paused):
            self.paused = paused

    def kill(self):
        p = self.proc
        if p and p.poll() is None:
            try:
                p.kill()
                p.wait(timeout=3)
            except Exception:
                pass
        self.proc = None
        self.hwnd = None


class MpvEngine:
    def __init__(self, taskbar, note="", force=False):
        self.taskbar = taskbar
        self.note = note
        self.force = force
        self.job = KillOnCloseJob()
        self.instances = []
        self.host = None
        self.monitors = []
        self.video_path = ""
        self.mpv = None
        self.cfg = {}
        self.last_start = 0.0
        self.last_mpv_search = 0.0
        self.installing = False
        self.install_attempts = 0

    def begin_install(self):
        self.installing = True
        set_status("mpv", "installing", self.note)

        def work():
            try:
                path = install_mpv()
                if path:
                    log.info("mpv kuruldu: %s", path)
                else:
                    log.error("mpv kurulamadı")
            except Exception:
                log.exception("mpv kurulum hatası")
            finally:
                self.installing = False
                self.last_mpv_search = 0.0

        threading.Thread(target=work, daemon=True).start()

    def stop_all(self):
        for inst in self.instances:
            inst.kill()
        self.instances = []

    def start_all(self):
        self.stop_all()
        self.last_start = time.monotonic()
        self.host = detect_desktop_host()
        if not self.host:
            log.warning("Masaüstü (Progman) bulunamadı, tekrar denenecek")
            return
        log.info("Masaüstü düzeni: %r (Windows build %s)", self.host, windows_build())
        for i, mon in enumerate(self.monitors):
            inst = None
            for safe in (False, True):
                inst = MpvInstance(self.mpv, self.video_path, mon, i, self.job, safe=safe)
                try:
                    inst.start()
                except OSError:
                    log.exception("mpv başlatılamadı")
                    inst = None
                    break
                if inst.wait_window():
                    break
                inst.kill()
                if not safe:
                    log.info("mpv temel ayarlarla yeniden deneniyor")
            if inst and inst.hwnd:
                self.instances.append(inst)
                try:
                    inst.embed(self.host)
                except Exception:
                    log.exception("mpv masaüstüne yerleştirilemedi")
                    self.stop_all()
                    return

    def healthy(self):
        if not self.host or not self.host.valid():
            return False
        if len(self.instances) != len(self.monitors):
            return False
        return all(inst.alive(self.host) for inst in self.instances)

    def keep_zorder(self):
        if not self.host or not self.host.raised:
            return
        ours = {inst.hwnd for inst in self.instances}
        for hwnd in ours:
            prev = user32.GetWindow(hwnd, GW_HWNDPREV)
            if prev != self.host.defview and prev not in ours:
                user32.SetWindowPos(hwnd, self.host.defview, 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def tick(self):
        cfg = read_config()
        if cfg is None:
            cfg = self.cfg
        self.cfg = cfg
        if cfg.get("quit"):
            return "quit"
        if not self.force and choose_engine(cfg) != "mpv":
            return "switch"

        self.taskbar.update(cfg)

        path = cfg.get("video_path", "") or ""
        if path and not os.path.isfile(path):
            path = ""
        if path != self.video_path:
            log.info("Video değişti: %r", path)
            self.video_path = path
            self.stop_all()
            self.host = None
            if not path:
                refresh_system_wallpaper()
            self.last_start = 0.0

        if not self.video_path:
            set_status("mpv", "idle", self.note)
            return None

        if not self.mpv:
            if self.installing:
                set_status("mpv", "installing", self.note)
                return None
            if time.monotonic() - self.last_mpv_search < 10:
                return None
            self.last_mpv_search = time.monotonic()
            self.mpv = find_mpv(cfg)
            if not self.mpv:
                if self.install_attempts < 2:
                    self.install_attempts += 1
                    log.warning("mpv bulunamadı, kuruluyor (deneme %d)", self.install_attempts)
                    self.begin_install()
                else:
                    log.error("mpv bulunamadı ve kurulamadı")
                    set_status("mpv", "mpv_failed", self.note)
                return None
            log.info("mpv: %s", self.mpv)

        monitors = get_monitors()
        if monitors != self.monitors:
            log.info("Monitörler: %s", monitors)
            self.monitors = monitors
            self.last_start = 0.0
            self.stop_all()

        if not self.healthy():
            set_status("mpv", "starting", self.note)
            if time.monotonic() - self.last_start > 5:
                self.start_all()
            return None

        self.keep_zorder()

        covers = cover_states(self.monitors)
        low = cfg.get("resource_mode", "low") != "high"
        for inst in self.instances:
            st = covers.get(inst.mon, 0)
            inst.set_paused(st == 2 or (st == 1 and low))
        set_status("mpv", "running", self.note)
        return None

    def run(self):
        n = 0
        while True:
            try:
                if n % 3 == 0:
                    res = self.tick()
                    if res:
                        return res
                self.taskbar.reapply()
            except Exception:
                log.exception("mpv tick hatası")
            n += 1
            time.sleep(0.34)


def run_mpv_backend(taskbar, note="", force=False):
    engine = MpvEngine(taskbar, note, force)
    try:
        return engine.run()
    finally:
        engine.stop_all()
        refresh_system_wallpaper()


def import_qt():
    q = types.SimpleNamespace()
    try:
        from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel
        from PyQt6.QtGui import QPixmap
        from PyQt6.QtCore import Qt, QUrl, QTimer, QT_VERSION_STR
        from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
        from PyQt6.QtMultimediaWidgets import QVideoWidget
        q.qt6 = True
        q.name = "PyQt6"
        q.QAudioOutput = QAudioOutput
        q.F_FRAMELESS = Qt.WindowType.FramelessWindowHint
        q.F_TOOL = Qt.WindowType.Tool
        q.A_NATIVE = Qt.WidgetAttribute.WA_NativeWindow
        q.A_TRANSPARENT = Qt.WidgetAttribute.WA_TransparentForMouseEvents
        q.ASPECT_EXPAND = Qt.AspectRatioMode.KeepAspectRatioByExpanding
        q.SMOOTH = Qt.TransformationMode.SmoothTransformation
    except ImportError:
        from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel
        from PyQt5.QtGui import QPixmap
        from PyQt5.QtCore import Qt, QUrl, QTimer, QT_VERSION_STR
        from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent, QMediaPlaylist
        from PyQt5.QtMultimediaWidgets import QVideoWidget
        q.qt6 = False
        q.name = "PyQt5"
        q.QMediaContent = QMediaContent
        q.QMediaPlaylist = QMediaPlaylist
        q.F_FRAMELESS = Qt.FramelessWindowHint
        q.F_TOOL = Qt.Tool
        q.A_NATIVE = Qt.WA_NativeWindow
        q.A_TRANSPARENT = Qt.WA_TransparentForMouseEvents
        q.ASPECT_EXPAND = Qt.KeepAspectRatioByExpanding
        q.SMOOTH = Qt.SmoothTransformation
    q.QApplication, q.QWidget, q.QVBoxLayout, q.QLabel = QApplication, QWidget, QVBoxLayout, QLabel
    q.QPixmap, q.Qt, q.QUrl, q.QTimer, q.QT_VERSION_STR = QPixmap, Qt, QUrl, QTimer, QT_VERSION_STR
    q.QMediaPlayer, q.QVideoWidget = QMediaPlayer, QVideoWidget
    return q


def make_qt_wallpaper(q):
    QApplication, QWidget, QVBoxLayout, QLabel = q.QApplication, q.QWidget, q.QVBoxLayout, q.QLabel
    QPixmap, Qt, QUrl, QTimer = q.QPixmap, q.Qt, q.QUrl, q.QTimer
    QMediaPlayer, QVideoWidget = q.QMediaPlayer, q.QVideoWidget
    QT6 = q.qt6

    class QtWallpaper(QWidget):
        def __init__(self, taskbar):
            super().__init__()
            self.taskbar = taskbar
            self.mon = primary_monitor()
            self.frozen = False
            self.released = False
            self.resume_pos = 0
            self.pending_pos = None
            self.target_pos = 0
            self.still_gen = 0
            self.release_ok = not os.path.exists(RELEASE_OFF)
            self.current_video_path = ""
            self.embedded_parent = None
            self.embed_fails = 0
            self.cfg = {}
            self.tick_count = 0
            self.load_tick = 0
            self.mem_base = None
            self.last_reload = 0
            self.result = None
            self.frame_ok = False
            self.play_started = time.monotonic()
            self._watching = False

            if os.path.exists(RELEASE_MARK):
                remove(RELEASE_MARK)
                touch(RELEASE_OFF)
                self.release_ok = False
                log.warning("Önceki bellek boşaltma tamamlanmadı, güvenli moda (sadece duraklat) geçildi")

            self.setWindowFlags(q.F_FRAMELESS | q.F_TOOL)
            self.setAttribute(q.A_NATIVE, True)
            self.setAttribute(q.A_TRANSPARENT, True)
            self.setStyleSheet("background-color: black;")
            self.resize(self.mon[2] - self.mon[0], self.mon[3] - self.mon[1])

            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)

            self.video_widget = QVideoWidget(self)
            self.video_widget.setAspectRatioMode(q.ASPECT_EXPAND)
            layout.addWidget(self.video_widget)

            self.still = QLabel(self)
            self.still.hide()

            self.player = QMediaPlayer(self)
            if QT6:
                self.audio = q.QAudioOutput(self)
                self.audio.setVolume(0.0)
                self.player.setAudioOutput(self.audio)
                self.player.setLoops(QMediaPlayer.Loops.Infinite)
                self.player.tracksChanged.connect(self.disable_audio)
                self.player.errorOccurred.connect(self.on_player_error)
            else:
                self.player.setMuted(True)
                self.playlist = q.QMediaPlaylist(self)
                self.playlist.setPlaybackMode(q.QMediaPlaylist.CurrentItemInLoop)
                self.player.setPlaylist(self.playlist)
                self.player.error.connect(self.on_player_error)
            self.player.mediaStatusChanged.connect(self.on_status)
            self.player.setVideoOutput(self.video_widget)

            self.timer = QTimer(self)
            self.timer.timeout.connect(self.tick)
            self.timer.start(1000)
            QTimer.singleShot(300, self.tick)

            self.tb_timer = QTimer(self)
            self.tb_timer.timeout.connect(self.taskbar.reapply)
            self.tb_timer.start(300)

        def finish(self, result):
            if self.result is None:
                self.result = result
                QApplication.quit()

        def fail(self, reason):
            if self.result is not None:
                return
            log.error("Qt motoru başarısız (%s) -> mpv'ye geçiliyor", reason)
            mark_qt_failed(self.cfg)
            self.finish("fallback")

        def on_player_error(self, *args):
            msg = ""
            try:
                msg = self.player.errorString()
            except Exception:
                pass
            log.error("Oynatıcı hatası: %s %s", args, msg)
            if self.current_video_path:
                self.fail("oynatıcı hatası: " + msg)

        def tick(self):
            try:
                c = read_config()
                if c is not None:
                    self.cfg = c
                if self.cfg.get("quit"):
                    self.finish("quit")
                    return
                if choose_engine(self.cfg) != "qt":
                    self.finish("switch")
                    return

                mon = primary_monitor()
                if mon != self.mon:
                    log.info("Birincil monitör değişti: %s -> %s", self.mon, mon)
                    self.mon = mon
                    self.resize(mon[2] - mon[0], mon[3] - mon[1])
                    self.embedded_parent = None

                self.tick_count += 1
                self.taskbar.update(self.cfg)
                self.apply_video()
                self.update_cover()
                self.watch_memory()
                self.watch_frames()
            except Exception:
                log.exception("Qt tick hatası")

        def disable_audio(self):
            try:
                self.player.setActiveAudioTrack(-1)
            except Exception:
                pass

        def resizeEvent(self, event):
            still = getattr(self, "still", None)
            if still is not None:
                still.setGeometry(0, 0, self.width(), self.height())
            super().resizeEvent(event)

        def load_video(self, path):
            self.load_tick = self.tick_count
            self.mem_base = None
            self.frame_ok = False
            self.play_started = time.monotonic()
            self.arm_frame_watch()
            url = QUrl.fromLocalFile(path)
            if QT6:
                self.player.setSource(url)
            else:
                self.playlist.clear()
                self.playlist.addMedia(q.QMediaContent(url))

        def arm_frame_watch(self):
            if not QT6 or self._watching:
                return
            try:
                self.video_widget.videoSink().videoFrameChanged.connect(self.on_frame_seen)
                self._watching = True
            except Exception:
                pass

        def on_frame_seen(self, *args):
            self.frame_ok = True
            self._watching = False
            try:
                self.video_widget.videoSink().videoFrameChanged.disconnect(self.on_frame_seen)
            except Exception:
                pass

        def watch_frames(self):
            if not self.current_video_path or self.frozen or self.frame_ok:
                return
            if time.monotonic() - self.play_started > FRAME_TIMEOUT:
                self.fail("ilk kare %d sn içinde gelmedi" % FRAME_TIMEOUT)

        def unload_video(self):
            self.pending_pos = None
            self.player.stop()
            if QT6:
                self.player.setSource(QUrl())
            else:
                self.playlist.clear()

        def start_playback(self, pos):
            self.still_gen += 1
            if QT6:
                self.pending_pos = pos
                self.target_pos = pos
            self.load_video(self.current_video_path)
            if QT6 and self.still.isVisible():
                try:
                    self.video_widget.videoSink().videoFrameChanged.connect(self.on_first_frame)
                except Exception:
                    pass
                gen = self.still_gen
                QTimer.singleShot(8000, lambda: self.finish_still(gen, True))
            self.player.play()

        def on_status(self, status):
            MS = QMediaPlayer.MediaStatus if QT6 else QMediaPlayer
            loaded = status in (MS.LoadedMedia, MS.BufferedMedia)
            if loaded and not QT6:
                self.frame_ok = True
            if self.pending_pos is None or not loaded:
                return
            pos = self.pending_pos
            self.pending_pos = None
            self.player.setPosition(pos)
            if self.frozen:
                self.player.pause()

        def on_first_frame(self, frame):
            try:
                t = frame.startTime()
            except Exception:
                t = -1
            if t >= 0 and abs(t / 1000 - self.target_pos) > 2000:
                return
            try:
                self.video_widget.videoSink().videoFrameChanged.disconnect(self.on_first_frame)
            except Exception:
                pass
            gen = self.still_gen
            QTimer.singleShot(120, lambda: self.finish_still(gen))

        def finish_still(self, gen=None, timeout=False):
            if self.frozen or (gen is not None and gen != self.still_gen):
                return
            if timeout:
                if not self.still.isVisible():
                    return
                log.warning("Devam: ilk kare 8 sn içinde gelmedi, güvenli moda geçildi")
                self.release_ok = False
                touch(RELEASE_OFF)
            remove(RELEASE_MARK)
            try:
                self.video_widget.videoSink().videoFrameChanged.disconnect(self.on_first_frame)
            except Exception:
                pass
            self.still.hide()

        def grab_still(self):
            if self.still.isVisible():
                return True
            try:
                img = self.video_widget.videoSink().videoFrame().toImage()
            except Exception:
                log.exception("Dondurma: kare alınamadı")
                return False
            if img.isNull():
                log.info("Dondurma: kare boş geldi, duraklatılıyor")
                return False
            small = img.scaled(16, 9)
            total = 0
            for y in range(9):
                for x in range(16):
                    c = small.pixelColor(x, y)
                    total += c.red() + c.green() + c.blue()
            if total / (16 * 9 * 3) < 3:
                log.info("Dondurma: kare siyah geldi, duraklatılıyor")
                return False
            dpr = self.devicePixelRatioF()
            tw = max(1, int(self.width() * dpr))
            th = max(1, int(self.height() * dpr))
            img = img.scaled(tw, th, q.ASPECT_EXPAND, q.SMOOTH)
            img = img.copy((img.width() - tw) // 2, (img.height() - th) // 2, tw, th)
            pm = QPixmap.fromImage(img)
            pm.setDevicePixelRatio(dpr)
            self.still.setPixmap(pm)
            self.still.setGeometry(0, 0, self.width(), self.height())
            self.still.show()
            self.still.raise_()
            self.still.repaint()
            return True

        def freeze(self):
            self.frozen = True
            self.resume_pos = self.pending_pos if self.pending_pos is not None else self.player.position()
            if QT6 and self.release_ok and self.grab_still():
                touch(RELEASE_MARK)
                self.unload_video()
                self.released = True
                log.info("Donduruldu, bellek boşaltıldı, konum=%s ms", self.resume_pos)
            else:
                self.player.pause()
                self.released = False

        def thaw(self):
            self.frozen = False
            if self.released:
                self.released = False
                log.info("Devam ediyor, konum=%s ms", self.resume_pos)
                self.start_playback(self.resume_pos)
            else:
                self.player.play()

        def reload_video(self):
            if self.frozen:
                return
            self.freeze()
            self.thaw()

        def watch_memory(self):
            if not self.current_video_path or self.frozen:
                return
            age = self.tick_count - self.load_tick
            if age == 60:
                self.mem_base = memory_mb()
                log.info("Bellek (taban): %.0f MB", self.mem_base)
                return
            if self.mem_base is None or age % 300:
                return
            mem = memory_mb()
            log.info("Bellek: %.0f MB", mem)
            if mem - self.mem_base > 1536 and time.time() - self.last_reload > 600:
                log.warning("Bellek çok arttı, video yeniden yükleniyor")
                self.last_reload = time.time()
                self.reload_video()

        def apply_video(self):
            path = self.cfg.get("video_path", "") or ""
            if path and os.path.isfile(path):
                if path != self.current_video_path:
                    self.current_video_path = path
                    self.frozen = False
                    self.released = False
                    self.pending_pos = None
                    self.still.hide()
                    self.load_video(path)
                    self.player.play()
                self.ensure_embedded()
                set_status("qt", "running")
            else:
                if self.current_video_path or self.isVisible():
                    self.player.stop()
                    self.current_video_path = ""
                    self.frozen = False
                    self.released = False
                    self.pending_pos = None
                    self.still.hide()
                    self.hide()
                set_status("qt", "idle")

        def embed(self, hwnd):
            host = detect_desktop_host()
            if not host:
                return None
            if host.raised and self.cfg.get("engine", "auto") != "qt":
                raise RuntimeError("yükseltilmiş masaüstü düzeni (Win11 24H2+): Qt çizimi görünmez")
            l, t, r, b = self.mon
            ex = (_get_window_long(hwnd, GWL_EXSTYLE) | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
            if host.raised:
                ex |= WS_EX_LAYERED
            _set_window_long(hwnd, GWL_EXSTYLE, ex)
            if host.raised:
                user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)

            style = _get_window_long(hwnd, GWL_STYLE) & 0xFFFFFFFF
            style &= ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME | WS_SYSMENU)
            style |= WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS | WS_CLIPCHILDREN
            _set_window_long(hwnd, GWL_STYLE, style)
            user32.SetParent(hwnd, host.parent)

            prect = wintypes.RECT()
            user32.GetWindowRect(host.parent, ctypes.byref(prect))
            x, y = l - prect.left, t - prect.top
            flags = SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_FRAMECHANGED
            if host.raised:
                user32.SetWindowPos(hwnd, host.defview, x, y, r - l, b - t, flags)
            else:
                user32.SetWindowPos(hwnd, None, x, y, r - l, b - t, flags | SWP_NOZORDER)
            log.info("Qt penceresi gömüldü: %r", host)
            return host.parent

        def ensure_embedded(self):
            if not self.isVisible():
                self.show()
            hwnd = int(self.winId())
            if (self.embedded_parent is not None and user32.IsWindow(self.embedded_parent)
                    and user32.GetParent(hwnd) == self.embedded_parent):
                return
            try:
                parent = self.embed(hwnd)
            except Exception as e:
                self.fail("gömme hatası: %s" % e)
                return
            if not parent:
                self.embed_fails += 1
                if self.embed_fails >= 5:
                    self.fail("masaüstü penceresi bulunamadı")
                return
            self.embed_fails = 0
            self.embedded_parent = parent

        def update_cover(self):
            if not self.current_video_path:
                return
            state = cover_states([self.mon]).get(self.mon, 0)
            low = self.cfg.get("resource_mode", "low") != "high"
            if state == 2 or (state == 1 and low):
                if not self.frozen:
                    self.freeze()
            elif self.frozen:
                self.thaw()

    return QtWallpaper


def run_qt_backend(taskbar):
    try:
        q = import_qt()
    except Exception:
        log.exception("PyQt yüklenemedi")
        mark_qt_failed(read_config() or {})
        return "fallback"
    try:
        app = q.QApplication.instance() or q.QApplication(sys.argv[:1])
        app.setQuitOnLastWindowClosed(False)
        win = make_qt_wallpaper(q)(taskbar)
    except Exception:
        log.exception("Qt penceresi oluşturulamadı")
        mark_qt_failed(read_config() or {})
        return "fallback"
    log.info("Qt motoru başladı (%s, Qt %s)", q.name, q.QT_VERSION_STR)
    set_status("qt", "starting")
    try:
        app.exec()
    except Exception:
        log.exception("Qt döngüsü hatası")
        win.result = win.result or "fallback"
    result = win.result or "quit"
    try:
        win.timer.stop()
        win.tb_timer.stop()
        win.player.stop()
        win.hide()
        win.close()
        app.processEvents()
    except Exception:
        pass
    remove(RELEASE_MARK)
    del win
    return result


def run_wallpaper():
    setup_logging("wallpaper")
    log.info("Arka plan süreci başlıyor (pid=%s, build=%s, python=%s)",
             os.getpid(), windows_build(), sys.version.split()[0])

    mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        log.info("Başka bir kopya zaten çalışıyor, çıkılıyor")
        return
    write_bg_state()
    start_heartbeat()
    set_status("-", "starting")

    try:
        import faulthandler
        global _fault_file
        _fault_file = open(CRASH_LOG, "a")
        faulthandler.enable(file=_fault_file)
    except Exception:
        pass

    set_dpi_awareness()
    disable_power_throttling()
    taskbar = TaskbarBlur()
    note = ""
    force_mpv = False
    last_pref = None
    try:
        while True:
            t0 = time.monotonic()
            cfg = read_config()
            if cfg is None:
                time.sleep(0.2)
                continue
            if cfg.get("quit"):
                break
            pref = cfg.get("engine", "auto")
            if pref != last_pref:
                force_mpv = False
                last_pref = pref
            engine = "mpv" if force_mpv else choose_engine(cfg)
            log.info("Motor: %s (tercih=%s, qt_failed=%s)", engine, pref, cfg.get("qt_failed", False))
            if engine == "qt":
                res = run_qt_backend(taskbar)
            else:
                res = run_mpv_backend(taskbar, note, force_mpv)
            log.info("Motor %s bitti: %s", engine, res)
            if res == "quit":
                break
            if res == "fallback":
                force_mpv = True
                note = "qt_fallback"
            elif res == "switch" and not force_mpv:
                note = ""
            if time.monotonic() - t0 < 0.5:
                time.sleep(0.5)
    finally:
        taskbar.off()
        refresh_system_wallpaper()
        set_status("-", "stopped")
        kernel32.CloseHandle(mutex)


TEXTS = {
    "tr": {
        "title": "Yanardağ Wallpaper Engine",
        "saved": "Kayıtlı: ",
        "no_selection": "Seçim yapılmadı",
        "select_video": "Yüksek Kalite Video Seç",
        "stop_video": "Video Duvar Kağıdını Kapat",
        "blur_on": "Görev Çubuğu Blur: AÇIK",
        "blur_off": "Görev Çubuğu Blur: KAPALI",
        "blur_intensity": "Bulanıklık Yoğunluğu: %",
        "startup_on": "Başlangıçta Çalıştır: AÇIK",
        "startup_off": "Başlangıçta Çalıştır: KAPALI",
        "startup_error": "Başlangıç ayarı değiştirilemedi:\n",
        "resource": "Kaynak Kullanımı",
        "res_low": "Düşük",
        "res_high": "Yüksek",
        "res_low_hint": "Uygulama ekranı kaplayınca video durur, GPU boşalır",
        "res_high_hint": "Büyütülmüş uygulamada devam eder, tam ekranda durur",
        "engine": "Oynatıcı Motoru",
        "eng_auto": "Otomatik",
        "engine_word": "Motor",
        "st_starting": "başlatılıyor...",
        "st_idle": "video seçilmedi",
        "st_running": "çalışıyor",
        "st_installing": "mpv kuruluyor, lütfen bekleyin...",
        "st_mpv_failed": "mpv kurulamadı",
        "st_stopped": "arka plan çalışmıyor",
        "busy_qt_installing": "Qt (PyQt) kuruluyor, lütfen bekleyin...",
        "busy_updating": "Kütüphaneler güncelleniyor...",
        "busy_mpv_installing": "mpv kuruluyor, lütfen bekleyin...",
        "qt_fallback": "Qt bu sistemde çalışmadı, mpv'ye geçildi.",
        "install_mpv": "mpv'yi Kur (tekrar dene)",
        "start_err": "Arka plan süreci başlatılamadı.\nLog: ",
    },
    "en": {
        "title": "Yanardag Wallpaper Engine",
        "saved": "Saved: ",
        "no_selection": "No selection",
        "select_video": "Select High Quality Video",
        "stop_video": "Turn Off Video Wallpaper",
        "blur_on": "Taskbar Blur: ON",
        "blur_off": "Taskbar Blur: OFF",
        "blur_intensity": "Blur Intensity: %",
        "startup_on": "Run on Startup: ON",
        "startup_off": "Run on Startup: OFF",
        "startup_error": "Could not change the startup setting:\n",
        "resource": "Resource Usage",
        "res_low": "Low",
        "res_high": "High",
        "res_low_hint": "Pauses the video and frees GPU when an app covers the screen",
        "res_high_hint": "Plays under maximized apps, stops in true fullscreen",
        "engine": "Playback Engine",
        "eng_auto": "Auto",
        "engine_word": "Engine",
        "st_starting": "starting...",
        "st_idle": "no video selected",
        "st_running": "running",
        "st_installing": "installing mpv, please wait...",
        "st_mpv_failed": "mpv could not be installed",
        "st_stopped": "background process not running",
        "busy_qt_installing": "Installing Qt (PyQt), please wait...",
        "busy_updating": "Updating libraries...",
        "busy_mpv_installing": "Installing mpv, please wait...",
        "qt_fallback": "Qt did not work on this system, switched to mpv.",
        "install_mpv": "Install mpv (retry)",
        "start_err": "Could not start the background process.\nLog: ",
    },
}

GREEN = ("#2FA572", "#106A43")
RED = ("#C23B22", "#8E2312")


def show_error(title, message):
    import tkinter
    from tkinter import messagebox
    root = tkinter.Tk()
    root.withdraw()
    try:
        messagebox.showerror(title, message, parent=root)
    finally:
        root.destroy()


def ensure_customtkinter():
    if has_module("customtkinter"):
        return True
    if not FROZEN and pip_install("customtkinter"):
        importlib.invalidate_caches()
        return has_module("customtkinter")
    show_error(WINDOW_TITLE,
               "customtkinter kurulamadı / could not be installed.\n"
               "İnternet bağlantınızı kontrol edin / check your internet connection.\n\n"
               f"\"{python_exe()}\" -m pip install customtkinter")
    return False


def run_gui():
    import customtkinter as ctk
    from tkinter import filedialog, messagebox

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    class WallpaperManager(ctk.CTk):
        def __init__(self):
            super().__init__()
            self.title(WINDOW_TITLE)
            self.geometry("450x640")
            self.resizable(False, False)
            self.protocol("WM_DELETE_WINDOW", self.on_close)

            cfg = read_config() or {}
            self.video_path = cfg.get("video_path", "") or ""
            self.is_transparent = bool(cfg.get("taskbar_transparent", False))
            try:
                self.taskbar_opacity = max(0, min(255, int(cfg.get("taskbar_opacity", 37))))
            except (TypeError, ValueError):
                self.taskbar_opacity = 37
            self.lang = cfg.get("language", "tr")
            if self.lang not in TEXTS:
                self.lang = "tr"
            self.resource_mode = "high" if cfg.get("resource_mode") == "high" else "low"
            self.engine = cfg.get("engine", "auto")
            if self.engine not in ("auto", "qt", "mpv"):
                self.engine = "auto"

            self.save_job = None
            self.busy = ""
            self._boot_done = False
            self._bg_started = False
            self._spawn_time = 0.0
            self._install_visible = False
            self._qt_thread = False

            self.frm_lang = ctk.CTkFrame(self, fg_color="transparent")
            self.frm_lang.pack(anchor="ne", padx=15, pady=(12, 0))

            self.btn_tr = ctk.CTkButton(self.frm_lang, text="TR", width=35, height=25,
                                        font=("Segoe UI", 12, "bold"), command=lambda: self.set_lang("tr"))
            self.btn_tr.pack(side="left", padx=(0, 4))

            self.btn_en = ctk.CTkButton(self.frm_lang, text="EN", width=35, height=25,
                                        font=("Segoe UI", 12, "bold"), command=lambda: self.set_lang("en"))
            self.btn_en.pack(side="left")

            self.lbl_title = ctk.CTkLabel(self, text="", font=("Segoe UI", 20, "bold"))
            self.lbl_title.pack(pady=(0, 5))

            self.lbl_video = ctk.CTkLabel(self, text="", font=("Segoe UI", 11), text_color="#A9A9A9")
            self.lbl_video.pack(pady=(0, 14))

            self.btn_select = ctk.CTkButton(self, text="", command=self.select_video, height=40,
                                            font=("Segoe UI", 13, "bold"))
            self.btn_select.pack(pady=5, padx=50, fill="x")

            self.btn_stop = ctk.CTkButton(self, text="", command=self.stop_video, height=32,
                                          font=("Segoe UI", 12, "bold"), fg_color="#3A3A3A",
                                          hover_color="#555555")
            self.btn_stop.pack(pady=5, padx=50, fill="x")

            self.btn_taskbar = ctk.CTkButton(self, text="", command=self.toggle_taskbar, height=40,
                                             font=("Segoe UI", 13, "bold"))
            self.btn_taskbar.pack(pady=5, padx=50, fill="x")

            self.lbl_opacity = ctk.CTkLabel(self, text="", font=("Segoe UI", 11), text_color="#A9A9A9")
            self.lbl_opacity.pack(pady=(5, 0))

            self.slider_opacity = ctk.CTkSlider(self, from_=0, to=255, command=self.on_opacity_change)
            self.slider_opacity.set(self.taskbar_opacity)
            self.slider_opacity.pack(pady=(0, 10), padx=50, fill="x")

            self.btn_startup = ctk.CTkButton(self, text="", command=self.toggle_startup, height=40,
                                             font=("Segoe UI", 13, "bold"))
            self.btn_startup.pack(pady=5, padx=50, fill="x")

            self.lbl_resource = ctk.CTkLabel(self, text="", font=("Segoe UI", 12, "bold"))
            self.lbl_resource.pack(pady=(10, 0))

            self.seg_resource = ctk.CTkSegmentedButton(self, values=["Low", "High"], height=32,
                                                       font=("Segoe UI", 13, "bold"),
                                                       command=self.on_resource_change)
            self.seg_resource.pack(pady=5, padx=50, fill="x")

            self.lbl_resource_hint = ctk.CTkLabel(self, text="", font=("Segoe UI", 11), text_color="#A9A9A9")
            self.lbl_resource_hint.pack()

            self.lbl_engine = ctk.CTkLabel(self, text="", font=("Segoe UI", 12, "bold"))
            self.lbl_engine.pack(pady=(10, 0))

            self.seg_engine = ctk.CTkSegmentedButton(self, values=["Auto", "Qt", "mpv"], height=32,
                                                     font=("Segoe UI", 13, "bold"),
                                                     command=self.on_engine_change)
            self.seg_engine.pack(pady=5, padx=50, fill="x")

            self.lbl_status = ctk.CTkLabel(self, text="", font=("Segoe UI", 11), text_color="#A9A9A9",
                                           wraplength=380, justify="center")
            self.lbl_status.pack(pady=(2, 4))

            self.btn_install = ctk.CTkButton(self, text="", command=self.install_mpv_clicked, height=34,
                                             font=("Segoe UI", 12, "bold"), fg_color="#B8860B",
                                             hover_color="#8B6508")

            self.update_ui_texts()
            self.update_slider_state()

            threading.Thread(target=self.boot_worker, daemon=True).start()
            self.after(800, self.poll)

        @property
        def t(self):
            return TEXTS[self.lang]

        def set_btn_state(self, btn, on, on_text, off_text):
            colors = GREEN if on else RED
            btn.configure(text=on_text if on else off_text, fg_color=colors[0], hover_color=colors[1])

        def update_ui_texts(self):
            t = self.t
            self.lbl_title.configure(text=t["title"])
            for btn, code in ((self.btn_tr, "tr"), (self.btn_en, "en")):
                if self.lang == code:
                    btn.configure(fg_color="#1F6AA5", hover_color="#144870")
                else:
                    btn.configure(fg_color="#333333", hover_color="#555555")
            name = os.path.basename(self.video_path) if self.video_path else t["no_selection"]
            if len(name) > 40:
                name = name[:37] + "..."
            self.lbl_video.configure(text=f"{t['saved']}{name}")
            self.btn_select.configure(text=t["select_video"])
            self.btn_stop.configure(text=t["stop_video"])
            self.set_btn_state(self.btn_taskbar, self.is_transparent, t["blur_on"], t["blur_off"])
            pct = int((self.taskbar_opacity / 255) * 100)
            self.lbl_opacity.configure(text=f"{t['blur_intensity']}{pct}")
            self.lbl_resource.configure(text=t["resource"])
            low, high = t["res_low"], t["res_high"]
            is_high = self.resource_mode == "high"
            self.seg_resource.configure(values=[low, high])
            self.seg_resource.set(high if is_high else low)
            self.lbl_resource_hint.configure(text=t["res_high_hint"] if is_high else t["res_low_hint"])
            self.lbl_engine.configure(text=t["engine"])
            auto = t["eng_auto"]
            self.seg_engine.configure(values=[auto, "Qt", "mpv"])
            self.seg_engine.set({"auto": auto, "qt": "Qt", "mpv": "mpv"}[self.engine])
            self.btn_install.configure(text=t["install_mpv"])
            self.check_startup_status()
            self.refresh_status()

        def save_config(self):
            self.save_job = None
            try:
                update_config(
                    video_path=self.video_path,
                    taskbar_transparent=self.is_transparent,
                    taskbar_opacity=int(self.taskbar_opacity),
                    language=self.lang,
                    resource_mode=self.resource_mode,
                    engine=self.engine,
                    quit=False,
                )
            except OSError:
                log.exception("Ayarlar kaydedilemedi")

        def boot_worker(self):
            try:
                if not FROZEN:
                    cfg = read_config() or {}
                    if qt_wanted(cfg) and not qt_package(True):
                        self.busy = "qt_installing"
                        install_qt()
            except Exception:
                log.exception("Qt kurulum hatası")
            self.busy = ""
            self._boot_done = True

            try:
                if not FROZEN and update_due():
                    time.sleep(6)
                    names = ["customtkinter"] + ([qt_package()] if qt_package() else [])
                    outdated = check_outdated(names)
                    if outdated:
                        self.busy = "updating"
                        stop_background()
                        pip_install(*outdated, upgrade=True)
                        importlib.invalidate_caches()
                        qt_package(True)
                        self._bg_started = False
                    mark_update_checked()
            except Exception:
                log.exception("Güncelleme denetimi hatası")
            self.busy = ""

        def start_background(self):
            self.save_config()
            self._spawn_time = time.time()
            if not ensure_background_process():
                messagebox.showerror(WINDOW_TITLE, self.t["start_err"] + LOG_FILE)

        def ensure_background_running(self):
            if self._boot_done and not self.busy:
                self.start_background()

        def poll(self):
            try:
                if self._boot_done and not self._bg_started and self.busy != "updating":
                    self._bg_started = True
                    self.start_background()
                cfg = read_config()
                if cfg is not None:
                    eng = cfg.get("engine", "auto")
                    if eng in ("auto", "qt", "mpv") and eng != self.engine:
                        self.engine = eng
                        self.update_ui_texts()
                self.refresh_status()
            except Exception:
                log.exception("GUI poll hatası")
            self.after(1000, self.poll)

        def refresh_status(self):
            t = self.t
            color = "#A9A9A9"
            show_install = False
            if self.busy:
                text, color = t["busy_" + self.busy], "#E0B040"
            elif not self._boot_done or time.time() - self._spawn_time < 15:
                text = t["st_starting"]
            elif not (heartbeat_fresh() or pid_alive(bg_pid())):
                text, color = t["st_stopped"], "#E05A47"
            else:
                st = read_status() or {}
                eng = {"qt": "Qt", "mpv": "mpv"}.get(st.get("engine"), "-")
                state = st.get("state", "starting")
                text = f"{t['engine_word']}: {eng} — {t.get('st_' + state, state)}"
                if st.get("detail") == "qt_fallback":
                    text += "\n" + t["qt_fallback"]
                if state == "running":
                    color = "#2FA572"
                elif state in ("mpv_failed",):
                    color, show_install = "#E05A47", True
                elif state == "installing":
                    color = "#E0B040"
            self.lbl_status.configure(text=text, text_color=color)
            if show_install and not self._install_visible:
                self.btn_install.pack(pady=(0, 8), padx=50, fill="x", after=self.lbl_status)
                self._install_visible = True
            elif not show_install and self._install_visible:
                self.btn_install.pack_forget()
                self._install_visible = False

        def set_lang(self, lang):
            if lang == self.lang:
                return
            self.lang = lang
            self.save_config()
            self.update_ui_texts()

        def on_close(self):
            if self.save_job:
                self.after_cancel(self.save_job)
                self.save_job = None
                self.save_config()
            self.destroy()

        def select_video(self):
            f = filedialog.askopenfilename(filetypes=[
                ("Video", "*.mp4 *.mkv *.webm *.mov *.avi *.wmv *.m4v *.gif"),
                ("All files", "*.*"),
            ])
            if f:
                self.video_path = os.path.normpath(f)
                self.save_config()
                self.ensure_background_running()
                self.update_ui_texts()

        def stop_video(self):
            self.video_path = ""
            self.save_config()
            self.update_ui_texts()

        def toggle_taskbar(self):
            self.is_transparent = not self.is_transparent
            self.save_config()
            if self.is_transparent:
                self.ensure_background_running()
            self.update_ui_texts()
            self.update_slider_state()

        def on_opacity_change(self, value):
            self.taskbar_opacity = int(value)
            pct = int((self.taskbar_opacity / 255) * 100)
            self.lbl_opacity.configure(text=f"{self.t['blur_intensity']}{pct}")
            if self.save_job:
                self.after_cancel(self.save_job)
            self.save_job = self.after(250, self.save_config)

        def update_slider_state(self):
            if self.is_transparent:
                self.slider_opacity.configure(state="normal")
                self.lbl_opacity.configure(text_color="#A9A9A9")
            else:
                self.slider_opacity.configure(state="disabled")
                self.lbl_opacity.configure(text_color="#444444")

        def on_resource_change(self, value):
            self.resource_mode = "high" if value == self.t["res_high"] else "low"
            self.save_config()
            self.lbl_resource_hint.configure(
                text=self.t["res_high_hint"] if self.resource_mode == "high" else self.t["res_low_hint"])

        def on_engine_change(self, value):
            self.engine = "qt" if value == "Qt" else ("mpv" if value == "mpv" else "auto")
            self.save_config()
            try:
                update_config(qt_failed=False)
            except OSError:
                pass
            cfg = read_config() or {}
            if (not FROZEN and qt_wanted(cfg) and not qt_package(True) and not self._qt_thread
                    and self._boot_done):
                self._qt_thread = True

                def work():
                    self.busy = "qt_installing"
                    try:
                        install_qt()
                    except Exception:
                        log.exception("Qt kurulum hatası")
                    self.busy = ""
                    self._qt_thread = False

                threading.Thread(target=work, daemon=True).start()
            self.ensure_background_running()

        def install_mpv_clicked(self):
            if self.busy:
                return
            self.busy = "mpv_installing"

            def work():
                try:
                    install_mpv()
                except Exception:
                    log.exception("mpv kurulum hatası")
                self.busy = ""

            threading.Thread(target=work, daemon=True).start()

        def check_startup_status(self):
            t = self.t
            self.set_btn_state(self.btn_startup, get_startup_value() is not None,
                               t["startup_on"], t["startup_off"])

        def toggle_startup(self):
            try:
                set_startup(get_startup_value() is None)
            except OSError as e:
                messagebox.showerror(self.t["title"], f"{self.t['startup_error']}{e}")
            self.check_startup_status()

    WallpaperManager().mainloop()


def main():
    if "--wallpaper" in sys.argv:
        run_wallpaper()
        return

    setup_logging("gui")
    if windows_build() < MIN_SUPPORTED_BUILD:
        show_error("Desteklenmeyen Windows Sürümü / Unsupported Windows Version",
                   "Bu uygulama Windows 8.1 / 10 / 11 gerektirir.\n"
                   "This application requires Windows 8.1 / 10 / 11.")
        return

    if not ensure_customtkinter():
        return

    close_previous_manager()
    stop_legacy_background()

    try:
        current = get_startup_value()
        if current is not None and current != startup_command_string():
            set_startup(True)
    except OSError:
        pass

    stop_outdated_background()
    run_gui()


if __name__ == "__main__":
    main()
