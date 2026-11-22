import dataclasses
import os
import sys
import threading
import time
import tomllib
import traceback
import typing
from collections import deque
from concurrent import futures
from dataclasses import dataclass, field
from pathlib import Path
from string import templatelib

import platformdirs
import tomli_w
from imgui_bundle import hello_imgui, imgui, immapp, rich_md

from common import ui
from common.ui import ensure_popup_open


@dataclass
class Status:
    msg: templatelib.Template | str

    SpecialProgress = typing.Literal["infinite", "textonly"]
    progress: float | SpecialProgress = "textonly"

class TracedException(Exception):
    def __init__(self, parent: Exception):
        super().__init__(parent)
        self.parent = parent
        self.formatted_tb: list[str] = traceback.format_tb(parent.__traceback__)

@dataclass
class _EmptyBaseState:
    pass

@dataclass
class _EmptyPersistentState:
    pass

State = typing.TypeVar("State", bound=_EmptyBaseState)
PersistentState = typing.TypeVar("PersistentState", bound=_EmptyPersistentState)

@dataclass
class Executor(futures.ThreadPoolExecutor):
    def __init__(self, on_error: typing.Callable[[TracedException], None]):
        super().__init__()
        self.on_error = on_error
    
    def submit(self, fn, /, *args, **kwargs):
        def do_fn(*args, **kwargs):
            try:
                fn(*args, **kwargs)
            except Exception as e:
                self.on_error(TracedException(e))
        super().submit(do_fn, *args, **kwargs)


@dataclass
class MainContext[State, PersistentState]:
    """Context for an app.

    You may replace state and persistent_state in setup().

    The same instance will be passed to draw_gui().
    """
    dirs: platformdirs.PlatformDirsABC

    tpe: futures.ThreadPoolExecutor

    state: State
    persistent_state: PersistentState

    # Always locked during setup() and draw_gui()
    lock: threading.Lock = field(default_factory=threading.Lock)

    errors: deque[TracedException] = field(default_factory=deque)

    status: Status | None = None

    persistent_state_just_changed: bool = False

    _persistent_state_changed_time: float = 0
    _persistent_state_saved: bool = True

    def persistent_state_path(self) -> str:
        return os.path.join(self.dirs.user_config_dir, "persistent_state.toml")

    def persistent_state_signal_changed(self) -> None:
        self._persistent_state_changed_time = imgui.get_time()
        self._persistent_state_saved = False

    def draw_settings(self):
        assert dataclasses.is_dataclass(self.persistent_state)
        fields = self.persistent_state.__dataclass_fields__
        values = self.persistent_state.__dict__
        def field_default_value(field: dataclasses.Field) -> typing.Any:
            if field.default != dataclasses.MISSING:
                return field.default
            elif field.default_factory != dataclasses.MISSING:
                return field.default_factory()
        for k, field in fields.items():
            metadata = field.metadata
            label = metadata.get("label", None)
            if label is None:
                continue
            hint = metadata.get("hint", "")
            tooltip = str(metadata.get("tooltip", ""))
            validate = metadata.get("validate", lambda _: True)
            typ = field.type
            v = values[k]
            changed, v = imgui.input_text_with_hint(label, hint, str(v))
            if changed:
                assert isinstance(typ, type)
                try:
                    v = typ(v)
                    ok = True
                except ValueError:
                    ok = False
                if ok and validate(v):
                    values[k] = v
                    self.persistent_state_signal_changed()
            if imgui.begin_item_tooltip():
                default = field_default_value(field)
                if tooltip and not tooltip.endswith("\n"):
                    tooltip += "\n"
                tooltip += f"Default: {default if default else '""'}"
                imgui.text(tooltip)
                imgui.end_tooltip()
        if imgui.button("Reset all"):
            imgui.open_popup("Reset all##_AppSettingsReset")
        if ui.modal_yesno_text(
            "Reset all##_AppSettingsReset",
            "Reset all settings to their default value?",
        ):
            for k, field in fields.items():
                values[k] = field_default_value(field)
            self.persistent_state_signal_changed()
        imgui.push_style_color(imgui.Col_.text, (1, 1, 1, 0.6))
        imgui.text("(Saved)" if self._persistent_state_saved else "(Waiting)")
        imgui.pop_style_color()

_imgui_font_mono = None
def get_font_mono() -> imgui.ImFont:
    assert isinstance(_imgui_font_mono, imgui.ImFont)
    return _imgui_font_mono

def get_root_dir() -> Path:
    """Returns the resource root directory"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS"))  # noqa: B009
    else:
        return Path(__file__).parent.parent

def get_res_dir() -> Path:
    """Returns the common/res directory"""
    return get_root_dir()/"common"/"res"

def write_file(path: str|Path, data: bytes|str) -> None:
    path = Path(path)
    os.makedirs(path.parent, exist_ok=True)
    if isinstance(data, bytes):
        with open(path, "wb") as f:
            f.write(data)
    else:
        with open(path, "w") as f:
            f.write(data)

def read_file_bytes(path: str|Path) -> bytes:
    with open(path, "rb") as f:
        return f.read()

def read_file_str(path: str|Path) -> str:
    with open(path, "r") as f:
        return f.read()

def run(
    setup: typing.Callable[[MainContext], None],
    draw_gui: typing.Callable[[MainContext], None],
    apptitle: str,
    appdirname: str,
    *,
    persistent_state_loaded: typing.Callable[[MainContext], None] | None = None,
) -> None:
    ctx = None
    def tpe_on_error(err: TracedException) -> None:
        assert ctx is not None
        with ctx.lock:
            ctx.status = None
            ctx.errors.append(err)

    with Executor(tpe_on_error) as tpe:
        ctx = MainContext(
            platformdirs.PlatformDirs(appdirname),
            tpe,
            _EmptyBaseState(),
            _EmptyPersistentState(),
        )

        DEBUG_FPS = False
        if DEBUG_FPS:
            fps_cnt = 0
            fps_time = time.time()

        def load_fonts():
            fonts = imgui.get_io().fonts
            fonts.add_font_from_file_ttf(str(get_res_dir()/"DejaVuSans.ttf"), 15)
            global _imgui_font_mono
            _imgui_font_mono = fonts.add_font_from_file_ttf(str(get_res_dir()/"DejaVuSansMono.ttf"), 15)

        def post_init_ig():
            if os.name == "nt": # set immersive dark mode
                import ctypes
                from ctypes import wintypes

                import glfw
                import imgui_bundle.glfw_utils as igglfw
                DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                val = ctypes.c_int(1)
                glfw_win = igglfw.glfw_window_hello_imgui()
                if glfw_win is not None:
                    win32_win = glfw.get_win32_window(glfw_win)
                    ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        wintypes.HWND(win32_win),
                        wintypes.DWORD(DWMWA_USE_IMMERSIVE_DARK_MODE),
                        ctypes.byref(val),
                        ctypes.sizeof(val)
                    )

            io = imgui.get_io()
            io.config_flags |= imgui.ConfigFlags_.nav_enable_keyboard
            io.config_flags |= imgui.ConfigFlags_.nav_enable_gamepad

            with ctx.lock:
                setup(ctx)

            try:
                pstate = tomllib.loads(read_file_str(ctx.persistent_state_path()))
                for k in ctx.persistent_state.__dict__:
                    if k not in pstate:
                        continue
                    ctx.persistent_state.__dict__[k] = pstate[k]
            except FileNotFoundError:
                pass
            
            if persistent_state_loaded is not None:
                with ctx.lock:
                    persistent_state_loaded(ctx)

        def _draw():
            try:
                draw_gui(ctx)
            except Exception as e:  # noqa: BLE001
                ctx.errors.append(TracedException(e))

            ctx.persistent_state_just_changed = False
            if not ctx._persistent_state_saved and imgui.get_time() - ctx._persistent_state_changed_time > 0.5:
                write_file(ctx.persistent_state_path(), tomli_w.dumps(ctx.persistent_state.__dict__))
                ctx._persistent_state_saved = True
                ctx.persistent_state_just_changed = True

            if ctx.status is not None:
                ensure_popup_open("Status")
            imgui.set_next_window_size_constraints((300, 150), (imgui.FLT_MAX, imgui.FLT_MAX))
            imgui.set_next_window_pos(imgui.get_main_viewport().get_center(), imgui.Cond_.always, (0.5, 0.5))
            if imgui.begin_popup_modal("Status", flags=imgui.WindowFlags_.always_auto_resize)[0]:
                if ctx.status is not None:
                    src = ctx.status.msg
                    ui.markdown((templatelib.Template(src) if isinstance(src, str) else src) + t"...")
                    if ctx.status.progress == "infinite":
                        imgui.progress_bar(imgui.get_time() * -0.5)
                    elif isinstance(ctx.status.progress, float):
                        imgui.progress_bar(ctx.status.progress)
                else:
                    imgui.close_current_popup()
                imgui.end_popup()

            if len(ctx.errors):
                ui.ensure_popup_open("Error")
            if ui.begin_modal_box("Error", (400, 400)):
                err = ctx.errors[0]
                if err is not None:
                    tb =  "\n".join(err.formatted_tb)
                    ui.markdown(t"Error: `{err.parent!r}`")
                    ui.markdown(t"``` {tb} ```")
                if ui.end_modal_box():
                    ctx.errors.popleft()

        def draw():
            if DEBUG_FPS:
                nonlocal fps_cnt
                nonlocal fps_time
                fps_cnt += 1
                now = time.time()
                if now - fps_time >= 1:
                    print(f"fps: {fps_cnt/(now - fps_time)}")
                    fps_cnt = 0
                    fps_time = now

            try:
                with ctx.lock:
                    _draw()
            except Exception as e:  # noqa: BLE001
                ctx.errors.append(TracedException(e))

        hello_imgui.set_assets_folder(str(get_res_dir()))
        rich_md.set_assets_folder(str(get_res_dir() / "richmd_assets"))

        rp = hello_imgui.RunnerParams()
        rp.imgui_window_params.enable_viewports = True
        rp.app_window_params.window_title = apptitle
        rp.callbacks.show_gui = draw
        rp.callbacks.load_additional_fonts = load_fonts
        rp.callbacks.post_init = post_init_ig
        rp.fps_idling = hello_imgui.FpsIdling(5)
        rp.ini_filename = os.path.join(ctx.dirs.user_config_dir, "imgui.ini")
        ap = immapp.AddOnsParams()
        md_opts = rich_md.MarkdownOptions()
        ap.with_markdown = True
        ap.with_markdown_options = md_opts
        immapp.run(rp, ap)