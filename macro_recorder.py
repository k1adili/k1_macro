import json
import math
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from pynput import keyboard, mouse


APP_DIR = Path(__file__).resolve().parent
MACRO_DIR = APP_DIR / "macros"
FORMAT_VERSION = 1


def is_finite_number(value):
    if not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def key_to_data(key):
    if isinstance(key, keyboard.Key):
        return {"type": "special", "value": key.name}
    if isinstance(key, keyboard.KeyCode):
        if key.vk is not None:
            return {"type": "vk", "value": key.vk}
        if key.char is not None:
            return {"type": "char", "value": key.char}
    return None


def key_from_data(data):
    if data["type"] == "special":
        return keyboard.Key[data["value"]]
    if data["type"] == "char":
        return keyboard.KeyCode.from_char(data["value"])
    if data["type"] == "vk":
        return keyboard.KeyCode.from_vk(data["value"])
    raise ValueError(f"نوع کلید ناشناخته است: {data['type']}")


def validate_macro(data):
    if not isinstance(data, dict) or data.get("version") != FORMAT_VERSION:
        raise ValueError("نسخهٔ فایل ماکرو پشتیبانی نمی‌شود.")
    if not isinstance(data.get("name"), str) or not isinstance(data.get("actions"), list):
        raise ValueError("ساختار فایل ماکرو معتبر نیست.")
    for action in data["actions"]:
        if not isinstance(action, dict) or action.get("type") not in {
            "key", "move", "click", "scroll"
        }:
            raise ValueError("یکی از رویدادهای فایل ماکرو معتبر نیست.")
        timestamp = action.get("t")
        if not is_finite_number(timestamp) or timestamp < 0:
            raise ValueError("زمان یکی از رویدادهای ماکرو معتبر نیست.")
        if action["type"] == "key":
            if action.get("action") not in {"press", "release"}:
                raise ValueError("رویداد صفحه‌کلید معتبر نیست.")
            key_data = action.get("key")
            if not isinstance(key_data, dict) or key_data.get("type") not in {
                "special", "char", "vk"
            }:
                raise ValueError("اطلاعات کلید معتبر نیست.")
            if key_data["type"] == "char" and (
                not isinstance(key_data.get("value"), str)
                or len(key_data["value"]) != 1
            ):
                raise ValueError("مقدار کلید متنی معتبر نیست.")
            if key_data["type"] == "vk" and (
                not isinstance(key_data.get("value"), int)
                or isinstance(key_data["value"], bool)
            ):
                raise ValueError("مقدار کد کلید معتبر نیست.")
            key_from_data(key_data)
        elif action["type"] in {"move", "click", "scroll"}:
            coordinate_keys = (
                ("x", "y", "dx", "dy")
                if action["type"] == "scroll"
                else ("x", "y")
            )
            if any(
                not is_finite_number(action.get(key))
                for key in coordinate_keys
            ):
                raise ValueError("مختصات یکی از رویدادهای موس معتبر نیست.")
            if action["type"] == "click":
                if action.get("button") not in mouse.Button.__members__ or not isinstance(
                    action.get("pressed"), bool
                ):
                    raise ValueError("رویداد کلیک معتبر نیست.")


class MacroRecorder:
    def __init__(self, root):
        self.root = root
        self.root.title("ضبط و اجرای ماکرو")
        self.root.geometry("560x520")
        self.root.minsize(500, 450)

        MACRO_DIR.mkdir(exist_ok=True)
        self.actions = []
        self.current_name = ""
        self.current_path = None
        self.is_recording = False
        self.worker_kind = None
        self.stop_event = threading.Event()
        self.worker_events = queue.Queue()
        self.action_lock = threading.Lock()
        self.geometry_lock = threading.Lock()
        self.window_rect = (0, 0, 0, 0)
        self.window_focused = True
        self.record_start = 0.0

        self._build_ui()
        self.root.bind("<FocusIn>", lambda _event: self._set_focus(True))
        self.root.bind("<FocusOut>", lambda _event: self._set_focus(False))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_macro_list()
        self._refresh_window_rect()
        self._poll_worker()

    def _build_ui(self):
        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text="ضبط‌کنندهٔ ماکرو",
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="e")
        ttk.Label(
            frame,
            text="حرکت موس و کلیدهای فشرده‌شده را ضبط و دوباره اجرا کنید.",
            wraplength=500,
        ).pack(anchor="e", pady=(4, 10))

        self.status_var = tk.StringVar(value="آماده")
        self.status_label = ttk.Label(frame, textvariable=self.status_var)
        self.status_label.pack(anchor="e", pady=(0, 8))

        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=(0, 12))
        self.record_button = ttk.Button(
            controls, text="● شروع ضبط", command=self.start_recording
        )
        self.record_button.pack(side="right", padx=3)
        self.stop_button = ttk.Button(
            controls, text="■ توقف", command=self.stop_current, state="disabled"
        )
        self.stop_button.pack(side="right", padx=3)
        self.play_button = ttk.Button(
            controls, text="▶ اجرا", command=self.play_macro
        )
        self.play_button.pack(side="right", padx=3)

        settings = ttk.LabelFrame(frame, text="تنظیمات پخش", padding=8)
        settings.pack(fill="x", pady=(0, 12))
        ttk.Label(settings, text="سرعت:").grid(row=0, column=0, sticky="e", padx=4)
        self.speed_var = tk.DoubleVar(value=1.0)
        ttk.Scale(
            settings,
            from_=0.25,
            to=3.0,
            variable=self.speed_var,
            orient="horizontal",
        ).grid(row=0, column=1, sticky="ew", padx=4)
        self.speed_label = ttk.Label(settings, text="1.00×")
        self.speed_label.grid(row=0, column=2, padx=4)
        self.speed_var.trace_add("write", self._update_speed_label)

        ttk.Label(settings, text="تعداد دفعات:").grid(
            row=1, column=0, sticky="e", padx=4, pady=(8, 0)
        )
        self.repeat_var = tk.StringVar(value="1")
        ttk.Spinbox(
            settings, from_=1, to=9999, textvariable=self.repeat_var, width=8
        ).grid(row=1, column=1, sticky="w", padx=4, pady=(8, 0))
        settings.columnconfigure(1, weight=1)

        ttk.Label(frame, text="ماکروهای ذخیره‌شده:").pack(anchor="e")
        self.macro_list = tk.Listbox(frame, height=9, exportselection=False)
        self.macro_list.pack(fill="both", expand=True, pady=5)
        self.macro_list.bind("<<ListboxSelect>>", self._select_macro)

        file_controls = ttk.Frame(frame)
        file_controls.pack(fill="x", pady=(6, 0))
        self.save_button = ttk.Button(
            file_controls, text="ذخیرهٔ ضبط فعلی", command=self.save_macro
        )
        self.save_button.pack(side="right", padx=3)
        self.load_button = ttk.Button(
            file_controls, text="بارگذاری فایل…", command=self.load_macro_file
        )
        self.load_button.pack(side="right", padx=3)

        ttk.Label(
            frame,
            text="رویدادها فقط روی این دستگاه ذخیره می‌شوند؛ از ضبط رمزها و اطلاعات حساس خودداری کنید.",
            wraplength=500,
        ).pack(anchor="e", pady=(10, 0))

    def _set_focus(self, focused):
        self.window_focused = focused

    def _update_speed_label(self, *_args):
        self.speed_label.configure(text=f"{self.speed_var.get():.2f}×")

    def _refresh_window_rect(self):
        try:
            rect = (
                self.root.winfo_rootx(),
                self.root.winfo_rooty(),
                self.root.winfo_width(),
                self.root.winfo_height(),
            )
            with self.geometry_lock:
                self.window_rect = rect
        except tk.TclError:
            return
        self.root.after(150, self._refresh_window_rect)

    def _inside_window(self, x, y):
        with self.geometry_lock:
            left, top, width, height = self.window_rect
        return left <= x < left + width and top <= y < top + height

    def _event_time(self):
        return max(0.0, time.monotonic() - self.record_start)

    def _append_action(self, action):
        with self.action_lock:
            if self.is_recording:
                action["t"] = self._event_time()
                self.actions.append(action)

    def start_recording(self):
        if self.worker_kind is not None or self.is_recording:
            return
        self.actions = []
        self.current_name = ""
        self.current_path = None
        self.record_start = time.monotonic()
        self.is_recording = True
        self.mouse_listener = mouse.Listener(
            on_move=self._on_move,
            on_click=self._on_click,
            on_scroll=self._on_scroll,
        )
        self.keyboard_listener = keyboard.Listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release,
        )
        try:
            self.mouse_listener.start()
            self.keyboard_listener.start()
        except (ImportError, OSError, RuntimeError) as exc:
            self.is_recording = False
            if self.mouse_listener.is_alive():
                self.mouse_listener.stop()
            if self.keyboard_listener.is_alive():
                self.keyboard_listener.stop()
            messagebox.showerror("خطای ضبط", f"شروع ضبط ممکن نشد:\n{exc}")
            return
        self.worker_kind = "recording"
        self.status_var.set("در حال ضبط… (برای پایان «توقف» را بزنید)")
        self.status_label.configure(foreground="#b00020")
        self.record_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.play_button.configure(state="disabled")
        self.save_button.configure(state="disabled")
        self.load_button.configure(state="disabled")

    def _on_move(self, x, y):
        if not self._inside_window(x, y):
            self._append_action({"type": "move", "x": x, "y": y})

    def _on_click(self, x, y, button, pressed):
        if not self._inside_window(x, y):
            self._append_action(
                {
                    "type": "click",
                    "x": x,
                    "y": y,
                    "button": button.name,
                    "pressed": pressed,
                }
            )

    def _on_scroll(self, x, y, dx, dy):
        if not self._inside_window(x, y):
            self._append_action(
                {"type": "scroll", "x": x, "y": y, "dx": dx, "dy": dy}
            )

    def _on_key_press(self, key):
        if not self.window_focused:
            key_data = key_to_data(key)
            if key_data is not None:
                self._append_action(
                    {"type": "key", "action": "press", "key": key_data}
                )

    def _on_key_release(self, key):
        if not self.window_focused:
            key_data = key_to_data(key)
            if key_data is not None:
                self._append_action(
                    {"type": "key", "action": "release", "key": key_data}
                )

    def stop_current(self):
        if self.is_recording:
            self.is_recording = False
            self.mouse_listener.stop()
            self.keyboard_listener.stop()
            self.worker_kind = None
            self.status_var.set(f"ضبط متوقف؛ {len(self.actions)} رویداد ثبت شد.")
            self.status_label.configure(foreground="")
            self.record_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            self.play_button.configure(state="normal")
            self.save_button.configure(state="normal")
            self.load_button.configure(state="normal")
            return
        if self.worker_kind == "playing":
            self.stop_event.set()
            self.status_var.set("در حال توقف پخش…")

    def play_macro(self):
        if self.worker_kind is not None:
            return
        if not self.actions:
            messagebox.showinfo("ماکرو", "ابتدا یک ماکرو ضبط یا بارگذاری کنید.")
            return
        try:
            repeats = int(self.repeat_var.get())
            if not 1 <= repeats <= 9999:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "تنظیم نامعتبر", "تعداد دفعات باید بین ۱ تا ۹۹۹۹ باشد."
            )
            return

        actions = list(self.actions)
        speed = self.speed_var.get()
        self.stop_event.clear()
        self.worker_kind = "playing"
        self.status_var.set("در حال اجرای ماکرو…")
        self.record_button.configure(state="disabled")
        self.play_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.save_button.configure(state="disabled")
        self.load_button.configure(state="disabled")
        threading.Thread(
            target=self._play_worker,
            args=(actions, speed, repeats),
            daemon=True,
        ).start()

    def _play_worker(self, actions, speed, repeats):
        mouse_controller = None
        keyboard_controller = None
        pressed_buttons = set()
        pressed_keys = set()
        error = None
        try:
            mouse_controller = mouse.Controller()
            keyboard_controller = keyboard.Controller()
            ordered_actions = sorted(actions, key=lambda item: item["t"])
            for _ in range(repeats):
                started = time.monotonic()
                for action in ordered_actions:
                    target = started + action["t"] / speed
                    while not self.stop_event.is_set():
                        remaining = target - time.monotonic()
                        if remaining <= 0:
                            break
                        time.sleep(min(remaining, 0.05))
                    if self.stop_event.is_set():
                        break
                    self._execute_action(
                        action,
                        mouse_controller,
                        keyboard_controller,
                        pressed_buttons,
                        pressed_keys,
                    )
                if self.stop_event.is_set():
                    break
        except Exception as exc:
            error = str(exc)
        finally:
            release_errors = []
            if keyboard_controller is not None:
                for key in pressed_keys:
                    try:
                        keyboard_controller.release(key)
                    except Exception as exc:
                        release_errors.append(str(exc))
            if mouse_controller is not None:
                for button in pressed_buttons:
                    try:
                        mouse_controller.release(button)
                    except Exception as exc:
                        release_errors.append(str(exc))
            if release_errors and error is None:
                error = "; ".join(release_errors)
            self.worker_events.put(("error", error) if error else ("done", None))

    @staticmethod
    def _execute_action(
        action,
        mouse_controller,
        keyboard_controller,
        pressed_buttons,
        pressed_keys,
    ):
        kind = action["type"]
        if kind == "move":
            mouse_controller.position = (action["x"], action["y"])
        elif kind == "click":
            mouse_controller.position = (action["x"], action["y"])
            button = mouse.Button[action["button"]]
            if action["pressed"]:
                pressed_buttons.add(button)
                mouse_controller.press(button)
            else:
                mouse_controller.release(button)
                pressed_buttons.discard(button)
        elif kind == "scroll":
            mouse_controller.position = (action["x"], action["y"])
            mouse_controller.scroll(action["dx"], action["dy"])
        elif kind == "key":
            key = key_from_data(action["key"])
            if action["action"] == "press":
                pressed_keys.add(key)
                keyboard_controller.press(key)
            else:
                keyboard_controller.release(key)
                pressed_keys.discard(key)

    def save_macro(self):
        if not self.actions:
            messagebox.showinfo("ذخیره", "رویدادی برای ذخیره وجود ندارد.")
            return
        name = simpledialog.askstring("ذخیرهٔ ماکرو", "نام ماکرو:")
        if name is None:
            return
        name = name.strip()
        if not name:
            messagebox.showerror("نام نامعتبر", "نام ماکرو نمی‌تواند خالی باشد.")
            return
        safe_name = "".join(c for c in name if c not in '<>:"/\\|?*').strip().rstrip(".")
        if not safe_name:
            messagebox.showerror("نام نامعتبر", "این نام برای فایل معتبر نیست.")
            return
        path = MACRO_DIR / f"{safe_name}.json"
        if path.exists() and path != self.current_path and not messagebox.askyesno(
            "جایگزینی ماکرو", f"ماکروی «{safe_name}» وجود دارد. جایگزین شود؟"
        ):
            return
        data = {"version": FORMAT_VERSION, "name": name, "actions": self.actions}
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("خطای ذخیره", f"ذخیرهٔ ماکرو انجام نشد:\n{exc}")
            return
        self.current_name = name
        self.current_path = path
        self._refresh_macro_list(select_path=path)
        self.status_var.set(f"ماکرو «{name}» ذخیره شد.")

    def _refresh_macro_list(self, select_path=None):
        self.macro_files = sorted(MACRO_DIR.glob("*.json"), key=lambda p: p.name.casefold())
        self.macro_list.delete(0, tk.END)
        for path in self.macro_files:
            self.macro_list.insert(tk.END, path.stem)
        if select_path in self.macro_files:
            index = self.macro_files.index(select_path)
            self.macro_list.selection_set(index)
            self.macro_list.activate(index)

    def _select_macro(self, _event=None):
        selected = self.macro_list.curselection()
        if not selected or self.worker_kind is not None:
            return
        path = self.macro_files[selected[0]]
        self._load_macro(path)

    def load_macro_file(self):
        path = filedialog.askopenfilename(
            title="بارگذاری ماکرو",
            filetypes=[("فایل ماکرو", "*.json"), ("همهٔ فایل‌ها", "*.*")],
        )
        if path:
            self._load_macro(Path(path))

    def _load_macro(self, path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            validate_macro(data)
        except (
            OSError,
            json.JSONDecodeError,
            ValueError,
            KeyError,
            TypeError,
            OverflowError,
        ) as exc:
            messagebox.showerror("خطای بارگذاری", f"فایل ماکرو معتبر نیست:\n{exc}")
            return
        self.actions = data["actions"]
        self.current_name = data["name"]
        self.current_path = path
        self.status_var.set(f"ماکرو «{self.current_name}» بارگذاری شد؛ {len(self.actions)} رویداد.")

    def _poll_worker(self):
        try:
            event, detail = self.worker_events.get_nowait()
        except queue.Empty:
            event = None
        if event:
            was_stopped = self.stop_event.is_set()
            self.worker_kind = None
            self.stop_event.clear()
            self.record_button.configure(state="normal")
            self.play_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            self.save_button.configure(state="normal")
            self.load_button.configure(state="normal")
            if event == "error":
                self.status_var.set("پخش با خطا متوقف شد.")
                messagebox.showerror("خطای پخش", f"اجرای ماکرو ناموفق بود:\n{detail}")
            else:
                self.status_var.set("پخش متوقف شد." if was_stopped else "پخش پایان یافت.")
        self.root.after(100, self._poll_worker)

    def _on_close(self):
        if self.is_recording:
            self.is_recording = False
            self.mouse_listener.stop()
            self.keyboard_listener.stop()
        if self.worker_kind == "playing":
            self.stop_event.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    MacroRecorder(root)
    root.mainloop()


if __name__ == "__main__":
    main()
