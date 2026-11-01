import os
import sys
import json
import winreg
import subprocess
import ctypes
from ctypes import wintypes
import customtkinter as ctk
from tkinter import filedialog, messagebox

if os.name != "nt":
    sys.exit()

# Sadece Windows 10 (1703+) ve Windows 11 destekleniyor.
# WorkerW + SetWindowCompositionAttribute tekniği bu sürümlerden öncesinde çalışmaz.
try:
    _WIN_BUILD = sys.getwindowsversion().build
except Exception:
    _WIN_BUILD = 0
_IS_WIN11 = _WIN_BUILD >= 22000

if _WIN_BUILD and _WIN_BUILD < 15063:
    try:
        messagebox.showerror(
            "Desteklenmeyen Windows Sürümü",
            "Bu uygulama yalnızca Windows 10 (1703+) ve Windows 11 üzerinde çalışır."
        )
    except Exception:
        pass
    sys.exit()

LOCAL_APP_DATA = os.getenv("LOCALAPPDATA")
APP_DIR = os.path.join(LOCAL_APP_DATA, "YanardagWallpaper")
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
PYW_FILE = os.path.join(APP_DIR, "arkaplan.pyw")

os.makedirs(APP_DIR, exist_ok=True)

# Arka plan kodundaki siyah ekran ve çökme hataları giderildi
# --- Windows 11 güncellemeleri ---
# 1) DPI farkındalığı artık SetProcessDpiAwarenessContext (Per-Monitor v2) ile ayarlanıyor.
#    Eski SetProcessDPIAware() Windows 11'de farklı DPI'lı çoklu ekranlarda videonun
#    bulanık/kaymış görünmesine sebep oluyordu.
# 2) WorkerW penceresine gömme işlemi artık yeniden deneme (retry) mantığıyla yapılıyor,
#    çünkü Windows 11'de explorer.exe açılışta WorkerW'yi bazen 500ms'den geç oluşturuyor.
# 3) explorer.exe yeniden başlatıldığında (Windows 11'de kişiselleştirme/tema
#    değişikliklerinde kullanıcılar bunu sık yapıyor) WorkerW handle'ı geçersiz kalıyordu;
#    artık periyodik olarak kontrol edilip otomatik yeniden gömülüyor.
# 4) Görev çubuğu saydamlığı Windows 11'de Başlat menüsü açıldığında veya kabuk kendini
#    yenilediğinde sıfırlanıyor (bu, Microsoft'un kendi forumlarında da doğrulanmış bilinen
#    bir davranış). Artık sadece ayar değiştiğinde değil, saydamlık açıkken birkaç
#    saniyede bir yeniden uygulanıyor.
PYW_CODE = r"""import os
import sys
import json
import ctypes
import winreg
from ctypes import wintypes

os.environ["QT_OPENGL"] = "software"

from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout
from PyQt6.QtCore import Qt, QUrl, QTimer
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtMultimediaWidgets import QVideoWidget

class ACCENTPOLICY(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_uint),
        ("AccentFlags", ctypes.c_uint),
        ("GradientColor", ctypes.c_uint),
        ("AnimationId", ctypes.c_uint)
    ]

class WINDOWCOMPOSITIONATTRIBDATA(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_int),
        ("Data", ctypes.POINTER(ctypes.c_int)),
        ("SizeOfData", ctypes.c_size_t)
    ]

def set_dpi_awareness():
    # Windows 10 1703+ / Windows 11: Per-Monitor v2 - en doğru ölçekleme.
    # Bu çağrı QApplication oluşturulmadan ÖNCE yapılmalı.
    try:
        DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
        ok = ctypes.windll.user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        if ok:
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

def force_windows_transparency():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", 0, winreg.KEY_ALL_ACCESS)
        winreg.SetValueEx(key, "EnableTransparency", 0, winreg.REG_DWORD, 1)
        winreg.CloseKey(key)
    except:
        pass

def set_taskbar_transparent(transparent, opacity):
    try:
        if transparent:
            force_windows_transparency()

        accent = ACCENTPOLICY()
        accent.AccentState = 4 if transparent else 0
        accent.AccentFlags = 2 if transparent else 0

        color = (opacity << 24) | 0x001A1A1A
        accent.GradientColor = color if transparent else 0x00000000

        data = WINDOWCOMPOSITIONATTRIBDATA()
        data.Attribute = 19
        data.Data = ctypes.cast(ctypes.pointer(accent), ctypes.POINTER(ctypes.c_int))
        data.SizeOfData = ctypes.sizeof(accent)

        applied = False
        hwnd = ctypes.windll.user32.FindWindowW("Shell_TrayWnd", None)
        if hwnd:
            ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))
            applied = True

        hwnd_sec = ctypes.windll.user32.FindWindowW("SecondaryTrayWnd", None)
        if hwnd_sec:
            ctypes.windll.user32.SetWindowCompositionAttribute(hwnd_sec, ctypes.byref(data))
            applied = True

        return applied
    except Exception:
        return False

mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "YanardagWallpaper_App_Mutex")
if ctypes.windll.kernel32.GetLastError() == 183:
    sys.exit(0)

def find_workerw():
    try:
        progman = ctypes.windll.user32.FindWindowW("Progman", None)
        if not progman: return 0
        ctypes.windll.user32.SendMessageTimeoutW(progman, 0x052C, 0, 0, 0, 1000, None)
        workerw_hwnd = ctypes.c_ulong(0)

        def enum_callback(hwnd, _):
            shell_dll = ctypes.windll.user32.FindWindowExW(hwnd, 0, "SHELLDLL_DefView", None)
            if shell_dll:
                ww = ctypes.windll.user32.FindWindowExW(0, hwnd, "WorkerW", None)
                if ww: workerw_hwnd.value = ww
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        ctypes.windll.user32.EnumWindows(WNDENUMPROC(enum_callback), 0)
        return workerw_hwnd.value
    except Exception:
        return 0

class HardwareAcceleratedWallpaper(QWidget):
    def __init__(self, screen_geometry):
        super().__init__()
        self.screen_w = screen_geometry.width()
        self.screen_h = screen_geometry.height()
        self.is_paused = False
        self.current_taskbar_state = None
        self.current_opacity = None
        self.taskbar_reapply_counter = 0
        self.workerw_hwnd = 0
        self.embed_attempts = 0

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnBottomHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True) # Siyah ekran yerine şeffaf
        self.setGeometry(0, 0, self.screen_w, self.screen_h)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.video_widget = QVideoWidget(self)
        self.video_widget.setStyleSheet("background-color: transparent;") # Arka planı şeffaf yap
        self.video_widget.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatioByExpanding)
        layout.addWidget(self.video_widget)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.0)

        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video_widget)
        self.player.setLoops(-1)

        self.current_video_path = ""

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.monitor_system)
        self.timer.start(1000)

        self.check_config()

    def check_config(self):
        path = os.path.join(os.getenv("LOCALAPPDATA"), "YanardagWallpaper", "config.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    config = json.load(f)

                saved_path = config.get("video_path", "")
                # Eğer geçerli bir video yolu varsa göster ve oynat
                if saved_path and os.path.exists(saved_path):
                    if saved_path != self.current_video_path:
                        self.current_video_path = saved_path
                        self.player.setSource(QUrl.fromLocalFile(saved_path))
                        self.show() # Sadece video varsa pencereyi göster
                        self.player.play()
                        self.is_paused = False
                else:
                    self.hide() # Video yoksa masaüstünü siyah yapmamak için gizle
                    self.player.stop()
                    self.current_video_path = ""

                taskbar_trans = config.get("taskbar_transparent", False)
                taskbar_opacity = config.get("taskbar_opacity", 37)

                state_changed = (self.current_taskbar_state != taskbar_trans or self.current_opacity != taskbar_opacity)
                self.taskbar_reapply_counter += 1

                # Windows 11: kabuk (explorer.exe) görev çubuğunu Başlat menüsü açılışı gibi
                # olaylarda kendi kendine yeniliyor ve saydamlığı sıfırlıyor. Bu yüzden
                # sadece ayar değiştiğinde değil, saydamlık açıkken birkaç saniyede bir de
                # yeniden uyguluyoruz.
                should_reapply = state_changed or (taskbar_trans and self.taskbar_reapply_counter >= 4)
                if should_reapply:
                    set_taskbar_transparent(taskbar_trans, taskbar_opacity)
                    self.current_taskbar_state = taskbar_trans
                    self.current_opacity = taskbar_opacity
                    self.taskbar_reapply_counter = 0
            except json.JSONDecodeError:
                pass # Dosya o an yazılıyorsa çökmesini engelle
            except Exception:
                pass

    def check_fullscreen(self):
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd: return False

            shell_hwnd = ctypes.windll.user32.GetShellWindow()
            desktop_hwnd = ctypes.windll.user32.GetDesktopWindow()

            if hwnd == shell_hwnd or hwnd == desktop_hwnd:
                return False

            class_name = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetClassNameW(hwnd, class_name, 256)
            if class_name.value in ("WorkerW", "Progman"):
                return False

            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top

            return w >= self.screen_w and h >= self.screen_h
        except:
            return False

    def monitor_system(self):
        self.check_config()
        self.ensure_embedded()
        if not self.current_video_path:
            return

        is_fullscreen = self.check_fullscreen()

        if is_fullscreen and not self.is_paused:
            self.player.pause()
            self.is_paused = True
        elif not is_fullscreen and self.is_paused:
            self.player.play()
            self.is_paused = False

    def ensure_embedded(self):
        # explorer.exe yeniden başlatıldığında (Windows 11'de tema/kişiselleştirme
        # değişikliklerinde sık görülür) WorkerW handle'ı geçersiz kalır ve pencere
        # masaüstünden kopar. Burada durum kontrol edilip gerekirse yeniden gömülür.
        try:
            if not self.workerw_hwnd or not ctypes.windll.user32.IsWindow(self.workerw_hwnd):
                self.embed_into_workerw()
                return
            hwnd = int(self.winId())
            parent = ctypes.windll.user32.GetParent(hwnd)
            if not parent or parent != self.workerw_hwnd:
                self.embed_into_workerw()
        except Exception:
            pass

    def embed_into_workerw(self):
        workerw = find_workerw()
        if workerw:
            self.workerw_hwnd = workerw
            ctypes.windll.user32.SetParent(int(self.winId()), workerw)
            self.setGeometry(0, 0, self.screen_w, self.screen_h)
            # Sadece video mevcutsa görünür yap
            if self.current_video_path:
                self.show()
            self.embed_attempts = 0
        else:
            # Windows 11'de explorer.exe açılışta WorkerW'yi bazen geç oluşturur;
            # bulunana kadar sınırlı sayıda yeniden dene.
            self.embed_attempts += 1
            if self.embed_attempts < 30:
                QTimer.singleShot(500, self.embed_into_workerw)

if __name__ == "__main__":
    set_dpi_awareness()
    app = QApplication(sys.argv)
    screen_geo = app.primaryScreen().geometry()

    win = HardwareAcceleratedWallpaper(screen_geo)
    # win.showFullScreen() YANLIŞTI! Gömülmeden önce çağrıldığı için siyah ekrana sebep oluyordu. Kaldırıldı.
    QTimer.singleShot(500, win.embed_into_workerw)
    sys.exit(app.exec())
"""

def get_pythonw_path():
    if getattr(sys, 'frozen', False):
        return "pythonw"
    else:
        return sys.executable.replace("python.exe", "pythonw.exe")

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

class WallpaperManager(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Yanardağ Wallpaper Engine")
        self.geometry("450x380")
        self.resizable(False, False)

        # Dil sözlüğü (TR / EN)
        self.texts = {
            "tr": {
                "title": "Yanardağ Wallpaper Engine",
                "saved": "Kayıtlı: ",
                "no_selection": "Seçim yapılmadı",
                "select_video": "Yüksek Kalite Video Seç",
                "blur_on": "Görev Çubuğu Blur: AÇIK",
                "blur_off": "Görev Çubuğu Blur: KAPALI",
                "blur_intensity": "Bulanıklık Yoğunluğu: %",
                "startup_on": "Başlangıçta Çalıştır: AÇIK",
                "startup_off": "Başlangıçta Çalıştır: KAPALI"
            },
            "en": {
                "title": "Yanardag Wallpaper Engine",
                "saved": "Saved: ",
                "no_selection": "No selection",
                "select_video": "Select High Quality Video",
                "blur_on": "Taskbar Blur: ON",
                "blur_off": "Taskbar Blur: OFF",
                "blur_intensity": "Blur Intensity: %",
                "startup_on": "Run on Startup: ON",
                "startup_off": "Run on Startup: OFF"
            }
        }

        # Verileri Yükle
        self.config_data = self.load_config()
        self.video_path = self.config_data.get("video_path", "")
        self.is_transparent = self.config_data.get("taskbar_transparent", False)
        self.taskbar_opacity = self.config_data.get("taskbar_opacity", 37)
        self.lang = self.config_data.get("language", "tr")

        # Arayüz Elemanları
        self.lbl_title = ctk.CTkLabel(self, text="", font=("Segoe UI", 20, "bold"))
        self.lbl_title.pack(pady=(20, 5))

        # Sağ Üstteki Dil Değiştirme Butonu
        self.btn_lang = ctk.CTkButton(self, text="", width=35, height=25, font=("Segoe UI", 12, "bold"), fg_color="#333333", hover_color="#555555", command=self.toggle_lang)
        self.btn_lang.place(relx=0.96, rely=0.04, anchor="ne")

        self.lbl_video = ctk.CTkLabel(self, text="", font=("Segoe UI", 11), text_color="#A9A9A9")
        self.lbl_video.pack(pady=(0, 20))

        self.btn_select = ctk.CTkButton(self, text="", command=self.select_video, height=40, font=("Segoe UI", 13, "bold"))
        self.btn_select.pack(pady=5, padx=50, fill="x")

        self.btn_taskbar = ctk.CTkButton(self, text="", command=self.toggle_taskbar, height=40, font=("Segoe UI", 13, "bold"))
        self.btn_taskbar.pack(pady=5, padx=50, fill="x")

        self.lbl_opacity = ctk.CTkLabel(self, text="", font=("Segoe UI", 11), text_color="#A9A9A9")
        self.lbl_opacity.pack(pady=(5, 0))

        self.slider_opacity = ctk.CTkSlider(self, from_=0, to=255, command=self.on_opacity_change)
        self.slider_opacity.set(self.taskbar_opacity)
        self.slider_opacity.pack(pady=(0, 10), padx=50, fill="x")

        self.btn_startup = ctk.CTkButton(self, text="", command=self.toggle_startup, height=40, font=("Segoe UI", 13, "bold"))
        self.btn_startup.pack(pady=5, padx=50, fill="x")

        # Tüm arayüz yazılarını seçili dile göre güncelle
        self.update_ui_texts()
        self.update_slider_state()

    def update_ui_texts(self):
        t = self.texts[self.lang]

        self.lbl_title.configure(text=t["title"])

        self.btn_lang.configure(text="TR" if self.lang == "tr" else "EN")

        video_name = os.path.basename(self.video_path) if self.video_path else t["no_selection"]
        self.lbl_video.configure(text=f"{t['saved']}{video_name}")

        self.btn_select.configure(text=t["select_video"])

        if self.is_transparent:
            self.btn_taskbar.configure(text=t["blur_on"], fg_color="#2FA572", hover_color="#106A43")
        else:
            self.btn_taskbar.configure(text=t["blur_off"], fg_color="#C23B22", hover_color="#8E2312")

        pct = int((self.taskbar_opacity / 255) * 100)
        self.lbl_opacity.configure(text=f"{t['blur_intensity']}{pct}")

        self.check_startup_status()

    def toggle_lang(self):
        self.lang = "en" if self.lang == "tr" else "tr"
        self.save_config()
        self.update_ui_texts()

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except:
                return {}
        return {}

    def save_config(self):
        with open(CONFIG_FILE, "w", encoding="utf-8") as j:
            json.dump({
                "video_path": self.video_path,
                "taskbar_transparent": self.is_transparent,
                "taskbar_opacity": int(self.taskbar_opacity),
                "language": self.lang
            }, j)

    def select_video(self):
        f = filedialog.askopenfilename(filetypes=[("Video", "*.mp4 *.avi *.mov *.mkv")])
        if f:
            self.video_path = f
            self.save_config()
            t = self.texts[self.lang]
            self.lbl_video.configure(text=f"{t['saved']}{os.path.basename(f)}")

    def toggle_taskbar(self):
        self.is_transparent = not self.is_transparent
        self.save_config()
        t = self.texts[self.lang]
        if self.is_transparent:
            self.btn_taskbar.configure(text=t["blur_on"], fg_color="#2FA572", hover_color="#106A43")
        else:
            self.btn_taskbar.configure(text=t["blur_off"], fg_color="#C23B22", hover_color="#8E2312")
        self.update_slider_state()

    def on_opacity_change(self, value):
        self.taskbar_opacity = int(value)
        pct = int((self.taskbar_opacity / 255) * 100)
        t = self.texts[self.lang]
        self.lbl_opacity.configure(text=f"{t['blur_intensity']}{pct}")
        self.save_config()

    def update_slider_state(self):
        if self.is_transparent:
            self.slider_opacity.configure(state="normal")
            self.lbl_opacity.configure(text_color="#A9A9A9")
        else:
            self.slider_opacity.configure(state="disabled")
            self.lbl_opacity.configure(text_color="#444444")

    def check_startup_status(self):
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        t = self.texts[self.lang]
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
                winreg.QueryValueEx(key, "YanardagWallpaper")
                self.btn_startup.configure(text=t["startup_on"], fg_color="#2FA572", hover_color="#106A43")
        except FileNotFoundError:
            self.btn_startup.configure(text=t["startup_off"], fg_color="#C23B22", hover_color="#8E2312")

    def toggle_startup(self):
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        t = self.texts[self.lang]
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS) as key:
                try:
                    winreg.QueryValueEx(key, "YanardagWallpaper")
                    winreg.DeleteValue(key, "YanardagWallpaper")
                    self.btn_startup.configure(text=t["startup_off"], fg_color="#C23B22", hover_color="#8E2312")
                except FileNotFoundError:
                    winreg.SetValueEx(key, "YanardagWallpaper", 0, winreg.REG_SZ, f'"{get_pythonw_path()}" "{PYW_FILE}"')
                    self.btn_startup.configure(text=t["startup_on"], fg_color="#2FA572", hover_color="#106A43")
        except:
            pass

if __name__ == "__main__":
    with open(PYW_FILE, "w", encoding="utf-8") as f:
        f.write(PYW_CODE)

    pythonw_cmd = get_pythonw_path()
    try:
        subprocess.Popen([pythonw_cmd, PYW_FILE], creationflags=subprocess.CREATE_NO_WINDOW)
    except:
        sys.exit(1)

    app = WallpaperManager()
    app.mainloop()