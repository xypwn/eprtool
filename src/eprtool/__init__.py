import traceback
import sys
from dataclasses import dataclass
import copy
import ctypes
import dataclasses
import io
import multiprocessing as mp
import multiprocessing.queues as mpq
import multiprocessing.sharedctypes as mpsct
import multiprocessing.pool as mpp
import os
import queue
import re
import struct
import typing
import unicodedata
import zipfile
import tkinter.filedialog as filedialog

from slimgui import imgui
from slimgui.imgui import StyleVar, WindowFlags, DragDropFlags, MouseButton, ItemFlags, Cond, Col
from slimgui.integrations.glfw import GlfwRenderer
import OpenGL.GL as gl
import chardet
import crossfiledialog
import glfw
import typst
import tomllib
import tomli_w
import platformdirs

TYPST_PRELUDE = """
#import "@preview/codly:2.0.0" as codly

#let stud-name = "{name}"
#let date-fmt = "[day].[month].[year]"
#set document(
  title: "Feedback {title}",
  date: datetime.today(),
)
#set page(
  margin: (top: 1cm, bottom: 1cm, left: 1cm, right: 3cm),
  header: context {
    if counter(page).get().first() > 1 {
      grid(
        columns: (1fr, 1fr, 1fr),
        column-gutter: 1em,
        row-gutter: 0.5em,
        align(left, document.title),
        align(center, stud-name),
        align(right, document.date.display(date-fmt)),
        grid.cell(colspan: 3, line(length: 100%))
      )
    }
  },
  numbering: "1 / 1",
)
#set text(lang: "de")
#set par(justify: true)
#set heading(numbering: none)

#align(center, context {
  set par(spacing: 1em)
  std.title()
  text(14pt, "{tutor}")
  parbreak()
  text(14pt, "Abgabe von "+stud-name)
  parbreak()
  text(14pt, document.date.display(date-fmt))
  line(length: 80%)
})
#v(1em)
"""

def imgui_is_item_double_clicked(mouse_button: MouseButton = MouseButton.LEFT) -> bool:
    return imgui.is_item_clicked(mouse_button) and imgui.is_mouse_double_clicked(mouse_button)

def default_field(x):
    return dataclasses.field(default_factory=lambda: copy.deepcopy(x))

@dataclass
class SubmissionArchive:
    stud_name: str
    stud_id: str
    filename: str
    files: dict[str, str|bytes]

    def folder_name(self) -> str:
        return f"{self.stud_name}_{self.stud_id}"

RE_SUBMISSION_FILE = re.compile(r"^(?P<name>[^ ]+ [^_]+)_(?P<id>\d+)_assignsubmission_file/(?P<file>[^/]+)$")

def submission_archives_from_zip(filename: str) -> list[SubmissionArchive]:
    # Automatically decodes bytes into a string, guessing the encoding.
    def auto_str(b: bytes) -> str:
        enc = chardet.detect(b, include_encodings=["utf-8", "437"])["encoding"]
        assert enc is not None
        return b.decode(enc)

    def submission_archive_to_files(inp: typing.IO[bytes]) -> dict[str, str|bytes]:
        res: dict[str, str|bytes] = {}
        with zipfile.ZipFile(inp) as ar:
            for finfo in ar.filelist:
                filename = finfo.filename
                if not finfo.flag_bits&0x800:
                    # UTF8 flag not set -> try to decode as UTF-8 anyway
                    # (some archivers don't seem to set the flag properly).
                    filename = auto_str(finfo.filename.encode('437'))
                filename = unicodedata.normalize("NFC", filename)
                is_text = False
                if "__MACOSX" in finfo.filename.split("/"):
                    continue
                if finfo.is_dir():
                    continue
                ext = os.path.splitext(filename)[1]
                if ext == ".py":
                    is_text = True
                elif os.path.basename(filename).lower() in ("readme", "readme.md", "readme.txt"):
                    is_text = True
                elif ext == ".pdf":
                    is_text = False
                else:
                    print(f"unrecognized file type for {filename}") # TODO!!!!
                    continue
                if not finfo.is_dir():
                    with ar.open(finfo.filename) as f:
                        if is_text:
                            res[filename] = auto_str(f.read())
                        else:
                            res[filename] = f.read()
        return res

    submissions = []
    with zipfile.ZipFile(filename, "r") as sub:
        for fname in sub.namelist():
            m = RE_SUBMISSION_FILE.match(fname)
            if m is None:
                print(f"skipping file {fname}")
                continue
            if os.path.splitext(fname)[1] != ".zip":
                print(f"invalid submission file type for {fname}")
                continue
            submissions.append(SubmissionArchive(
                m["name"],
                m["id"],
                fname,
                submission_archive_to_files(sub.open(fname)),
            ))
    submissions.sort(key=lambda s: s.folder_name())
    return submissions

SubmissionFileKindTag = typing.Literal["code", "documentation", "ignore"]

@dataclass
class SubmissionFileInfo:
    custom_filename: str
    filename: str
    kind: SubmissionFileKindTag

@dataclass
class Settings:
    tutor: str = ""
    pdf_annotated_suffix: str = "_kommentiert"

# State that can be passed to tasks
@dataclass
class State:
    dirs: platformdirs.PlatformDirsABC

    subms: list[SubmissionArchive]|None = None
    subm_idx: int = 0
    subm_files_ordered: list[list[SubmissionFileInfo]] = default_field([])
    title: str = ""

    editing_file_info_idx: int|None = None
    focus_editing_file_info: bool = False

    last_exported_folder: str = ""

    settings: Settings = default_field(Settings())
    settings_saved: bool = True
    settings_last_edited_time: float = 0

    def settings_path(self) -> str:
        return os.path.join(self.dirs.user_config_dir, "settings.toml")

@dataclass
class Status:
    SpecialProgress = typing.Literal["infinite", "textonly"]

    msg: str
    progress: float|SpecialProgress = "textonly"

@dataclass
class Task:
    fn: typing.Callable
    args: typing.Iterable

# For use as sentinel value
class Stop:
    def __eq__(self, other):
        return isinstance(other, Stop)
    pass

# Context passed to any task function
@dataclass
class Context:
    msgq: mp.Queue[Status]

class TaskException(BaseException):
    def __init__(self, parent: BaseException):
        super().__init__(parent)
        self.formatted_tb: list[str] = traceback.format_tb(parent.__traceback__)

def _task_worker(ctx: Context, taskq: mp.Queue[Task|Stop], resq: mp.Queue[State|None|TaskException]):
    for task in iter(taskq.get, Stop()):
        assert isinstance(task, Task)
        ok = False
        try:
            res = task.fn(*([ctx]+list(task.args)))
            ok = True
        except BaseException as e:
            resq.put(TaskException(e))
        if ok:
            assert isinstance(res, State|None)
            resq.put(res)

# Main process data of task worker state
class TaskManager:
    def __init__(self):
        # Queue for sending tasks to worker
        self.taskq: mpq.Queue[Task|Stop] = mp.Queue()
        # Result from worker
        self.resq: mpq.Queue[State|None|TaskException] = mp.Queue()
        # Queue for sending status messages from worker
        self.msgq: mpq.Queue[Status] = mp.Queue()
        # Worker process
        self.proc = mp.Process(target=_task_worker, args=(
            Context(self.msgq),
            self.taskq,
            self.resq,
        ))
        # Whether worker is currently executing a task
        self.working = False
        # Current display status
        self.status = Status("")
        # Last error message
        self.error: TaskException|None = None

        self.proc.start()

    def enqueue(self, task: Task):
        self.taskq.put(task)
        self.status = Status("working")
        self.working = True

    def shutdown(self):
        self.taskq.put(Stop())
        self.proc.join()
        self.taskq.close()
        self.resq.close()
        self.msgq.close()
        self.proc.close()

def _task_open_submission_zip(ctx: Context, state: State) -> State:
    ctx.msgq.put(Status("choosing file"))

    # TODO: Decide which dialog type to use
    #fname = filedialog.askopenfilename(filetypes=(("ZIP Archive", "*.zip"),), title="Open submission archive")
    fname = crossfiledialog.open_file("Open submission archive",
        state.dirs.user_downloads_dir, "*.zip")
    if fname != "":
        state.subms = submission_archives_from_zip(fname)
        state.subm_files_ordered = []
        for i, subm in enumerate(state.subms):
            state.subm_files_ordered.append([])
            for fname in sorted(subm.files.keys()):
                state.subm_files_ordered[i].append(SubmissionFileInfo(
                    fname,
                    fname,
                    "code" if fname.lower().endswith(".py") else "documentation",
                    ))
        # If all filenames in a submission share a common first path element,
        # strip that by default.
        for i, _ in enumerate(state.subms):
            strip_first_elem = True
            ordered_finfos = state.subm_files_ordered[i]
            if len(ordered_finfos):
                common_first_elem = ordered_finfos[0].custom_filename.split("/")[0]
                for finfo in ordered_finfos:
                    elems = finfo.custom_filename.split("/")
                    if len(elems) < 2 or elems[0] != common_first_elem:
                        strip_first_elem = False
                        break
            if strip_first_elem:
                for finfo in ordered_finfos:
                    finfo.custom_filename = "/".join(finfo.custom_filename.split("/")[1:])
    return state

RE_VALID_TITLE = re.compile(r"^[\w\-. ]+$")

def _task_export_to_feedback_folder(ctx: Context, state: State) -> State:
    if RE_VALID_TITLE.match(state.title) is None:
        raise Exception("Invalid title")
    assert state.subms is not None
    ctx.msgq.put(Status("choosing destination"))
    folder_name = crossfiledialog.choose_folder("Choose output folder",
        os.path.dirname(state.last_exported_folder) if state.last_exported_folder != "" else None)
    if folder_name == "":
        return state
    tomldata = {"submissions": {}}
    out_dir = os.path.join(folder_name, state.title)
    for i, subm in enumerate(state.subms):
        subm_dir = os.path.join(out_dir, subm.folder_name())
        tomldata["submissions"][subm.stud_id] = {
            "stud-name": subm.stud_name,
            "stud-id": subm.stud_id,
            "files": [],
        }
        typst_code = ""
        for finfo in state.subm_files_ordered[i]:
            tomldata["submissions"][subm.stud_id]["files"].append({
                "name": finfo.filename,
                "custom-name": finfo.custom_filename,
                "kind": finfo.kind,
            })
            fdata = state.subms[i].files[finfo.filename]
            if finfo.kind == "ignore":
                continue
            if isinstance(fdata, bytes):
                file_path = os.path.join(subm_dir, finfo.custom_filename)
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                with open(file_path, "wb") as f:
                    assert isinstance(fdata, bytes)
                    f.write(fdata)
            else:
                assert isinstance(fdata, str)
                if typst_code == "":
                    typst_code += TYPST_PRELUDE.replace(
                        "{title}", state.title).replace(
                        "{name}", subm.stud_name).replace(
                        "{tutor}", state.settings.tutor)
                else:
                    typst_code += f"#pagebreak()\n"
                lang = "py"
                if not finfo.custom_filename.endswith(".py"):
                    lang = "md" # TODO: more precise guessing
                typst_code += f"== `{finfo.custom_filename}`\n"
                typst_code += f"#codly.new(```{lang}\n{fdata}\n```)\n"
        if typst_code != "":
            file_path = os.path.join(subm_dir, f"{subm.stud_name}_{subm.stud_id}_code.pdf")
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            ctx.msgq.put(Status(f"compiling code for {subm.stud_name} ({i}/{len(state.subms)})", i/len(state.subms)))
            out = typst.compile(typst_code.encode("utf-8"))
            with open(file_path, "wb") as f:
                f.write(out)
    state.last_exported_folder = out_dir
    with open(os.path.join(out_dir, "eprtool.toml"), "wb") as f:
        tomli_w.dump(tomldata, f)
    return state

def _task_pack_feedback_zip(ctx: Context, state: State) -> None:
    ctx.msgq.put(Status("choosing feedback folder"))
    folder_name = crossfiledialog.choose_folder("Choose feedback folder",
        state.last_exported_folder if state.last_exported_folder != "" else None)
    if folder_name == "":
        return
    title = os.path.basename(folder_name)
    if RE_VALID_TITLE.match(title) is None:
        raise Exception("Invalid title")
    with open(os.path.join(folder_name, "eprtool.toml"), "rb") as f:
        tomldata = tomllib.load(f)

    def filename_with_suffix(fname: str, suffix: str) -> str:
        root, ext = os.path.splitext(fname)
        return root+suffix+ext

    zip_name = os.path.join(folder_name, title+"_annotated.zip")
    with zipfile.ZipFile(zip_name, "w") as ar:
        for subm in tomldata["submissions"].values():
            subm_dir = os.path.join(folder_name, f"{subm["stud-name"]}_{subm["stud-id"]}")
            subm_zip_dir = f"{subm["stud-name"]}_{subm["stud-id"]}_assignsubmission_file"
            have_code = False
            for finfo in subm["files"]:
                if finfo["kind"] == "code":
                    have_code = True
                elif finfo["kind"] == "documentation":
                    ar.write(os.path.join(subm_dir, filename_with_suffix(finfo["custom-name"], state.settings.pdf_annotated_suffix)),
                        arcname=f"{subm_zip_dir}/Feedback_{title}_{subm["stud-name"]}_{finfo["custom-name"]}")
                else:
                    assert finfo["kind"] == "ignore"
            if have_code:
                    ar.write(os.path.join(subm_dir, filename_with_suffix("code.pdf", state.settings.pdf_annotated_suffix)),
                        arcname=f"{subm_zip_dir}/Feedback_{title}_{subm["stud-name"]}_code.pdf")

def draw_gui(state: State, task_mgr: TaskManager) -> None:
    imgui.begin_disabled(task_mgr.working)
    if imgui.button("Open submission ZIP"):
        task_mgr.enqueue(Task(_task_open_submission_zip, (state,)))
        # TODO: This is a very hacky way of doing the popup
        imgui.open_popup("Status##TaskProgress")
    imgui.same_line()
    imgui.begin_disabled(state.subms is None)
    if imgui.button("Export to feedback folder"):
        task_mgr.enqueue(Task(_task_export_to_feedback_folder, (state,)))
        imgui.open_popup("Status##TaskProgress")
    imgui.end_disabled()
    imgui.same_line()
    if imgui.button("Pack feedback ZIP"):
        task_mgr.enqueue(Task(_task_pack_feedback_zip, (state,)))
        imgui.open_popup("Status##TaskProgress")
    imgui.end_disabled()

    if imgui.tree_node("Settings"):
        for k, v in state.settings.__dict__.items():
            v_changed, v_new = imgui.input_text(k, v)
            if v_changed:
                state.settings.__dict__[k] = v_new
                state.settings_last_edited_time = imgui.get_time()
                state.settings_saved = False
        imgui.push_style_color(Col.TEXT, (1, 1, 1, 0.6))
        imgui.text("(Saved)" if state.settings_saved else "(Waiting)")
        imgui.pop_style_color()
        imgui.tree_pop()
        imgui.spacing()
    if not state.settings_saved and imgui.get_time() - state.settings_last_edited_time > 0.5:
        os.makedirs(os.path.dirname(state.settings_path()), exist_ok=True)
        with open(state.settings_path(), "wb") as f:
            tomli_w.dump(state.settings.__dict__, f)
            state.settings_saved = True

    if state.subms is not None:
        title_changed, title = imgui.input_text_with_hint("Title", "e.g. Uebung 0", state.title)
        if title_changed:
            state.title = title
        imgui.begin_child("submissions", (150, 0), imgui.ChildFlags.BORDERS|imgui.ChildFlags.RESIZE_X)
        for i, s in enumerate(state.subms):
            if imgui.selectable(f"{s.stud_name} ({s.stud_id})", i == state.subm_idx, imgui.SelectableFlags.SELECT_ON_NAV)[0]:
                state.subm_idx = i
                state.editing_file_info_idx = None
        imgui.end_child()
        imgui.same_line()
        if state.subm_idx < len(state.subms):
            subm = state.subms[state.subm_idx]
            ordered_finfos = state.subm_files_ordered[state.subm_idx]
            imgui.begin_child("contents", (0, 0), imgui.ChildFlags.BORDERS)
            imgui.push_text_wrap_pos()
            imgui.text(f"Submitter: {subm.stud_name} ({s.stud_id})")
            imgui.text(f"Submission filename: {"/".join(subm.filename.split("/")[1:])}")
            imgui.separator_text("Files")
            imgui.push_item_flag(ItemFlags.ALLOW_DUPLICATE_ID, True)
            for i, info in enumerate(ordered_finfos):
                if state.editing_file_info_idx != i:
                    imgui.selectable(f"{info.custom_filename} [{info.kind}]", False)
                    if imgui_is_item_double_clicked():
                        state.editing_file_info_idx = i
                        state.focus_editing_file_info = True
                    imgui.set_item_tooltip(f"Drag to reorder\nDouble-click to edit\nOriginal filename: {info.filename}")
                    # Hacky-ass dragging code pulled from dearimgui demo. Might break
                    # when dragging too quickly, but it seems to work well enough.
                    if imgui.is_item_active() and not imgui.is_item_hovered():
                        i_next = i + (-1 if imgui.get_mouse_drag_delta(MouseButton.LEFT)[1] < 0.0 else 1)
                        if 0 <= i_next < len(ordered_finfos):
                            ordered_finfos[i], ordered_finfos[i_next] = \
                                ordered_finfos[i_next], ordered_finfos[i]
                            imgui.reset_mouse_drag_delta()
                else:
                    any_active = False
                    cn_changed, cn = imgui.input_text("##CustomFilename", info.custom_filename)
                    if cn_changed:
                        info.custom_filename = cn if cn != "" else info.filename
                    any_active = any_active or imgui.is_item_active()
                    if state.focus_editing_file_info:
                        imgui.set_keyboard_focus_here(-1)
                    imgui.set_item_tooltip("Empty filename to reset")
                    imgui.same_line()
                    file_kind_tags: list[SubmissionFileKindTag] = ["code", "documentation", "ignore"]
                    fk_changed, fk = imgui.combo("##Kind", file_kind_tags.index(info.kind), file_kind_tags)
                    if fk_changed:
                        info.kind = file_kind_tags[fk]
                    any_active = any_active or imgui.is_item_active()
                    if not state.focus_editing_file_info and not any_active and imgui.is_window_focused():
                        state.editing_file_info_idx = None
                    state.focus_editing_file_info = False
            imgui.pop_item_flag()
            imgui.pop_text_wrap_pos()
            imgui.end_child()

    imgui.set_next_window_pos(imgui.get_main_viewport().get_center(), Cond.ALWAYS, (0.5, 0.5))
    if imgui.begin_popup_modal("Error", flags=WindowFlags.ALWAYS_AUTO_RESIZE)[0]:
        if task_mgr.error is not None:
            imgui.push_text_wrap_pos()
            imgui.text(f"Error: {task_mgr.error}")
            imgui.pop_text_wrap_pos()
            tb = "\n".join(task_mgr.error.formatted_tb)
            imgui.text(tb)
        if imgui.button("Close", (0, 0)):
            imgui.close_current_popup()
        imgui.same_line()
        if imgui.button("Copy to clipboard", (0, 0)):
            imgui.set_clipboard_text(f"Error: {task_mgr.error}\n{tb}")
        imgui.end_popup()

    # Handle tasks
    imgui.set_next_window_pos(imgui.get_main_viewport().get_center(), Cond.ALWAYS, (0.5, 0.5))
    if imgui.begin_popup_modal("Status##TaskProgress", flags=WindowFlags.ALWAYS_AUTO_RESIZE)[0]:
        imgui.text(task_mgr.status.msg+"...")
        if task_mgr.status.progress == "infinite":
            imgui.progress_bar(imgui.get_time() * -0.5)
        elif isinstance(task_mgr.status.progress, float):
            imgui.progress_bar(task_mgr.status.progress)
        if not task_mgr.working:
            imgui.close_current_popup()
        imgui.end_popup()
    if task_mgr.working:
        try:
            task_mgr.status = task_mgr.msgq.get_nowait()
        except queue.Empty:
            pass
        try:
            v = task_mgr.resq.get_nowait()
            if isinstance(v, State):
                # Copy all fields to state (deepcopy not necessary since data was pickled)
                for k, f in v.__dict__.items():
                    state.__dict__[k] = f
            elif v is None:
                pass
            elif isinstance(v, TaskException):
                task_mgr.error = v
                imgui.open_popup("Error")
            else:
                raise Exception(f"unknown task result type {type(v)}")
            task_mgr.working = False
        except queue.Empty:
            pass

def main():
    glfw.init()
    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, glfw.TRUE)
    glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
    glfw.window_hint(glfw.VISIBLE, True)
    glfw_window = glfw.create_window(width=600, height=600, title="EPR Tool", monitor=None, share=None)
    glfw.make_context_current(glfw_window)

    # TODO: Limit frame rate

    imgui.create_context()
    io = imgui.get_io()
    io.config_flags |= imgui.ConfigFlags.NAV_ENABLE_KEYBOARD
    renderer = GlfwRenderer(glfw_window)

    state = State(platformdirs.PlatformDirs("eprtool"))
    try:
        with open(state.settings_path(), "rb") as f:
            settings = tomllib.load(f)
            for k in state.settings.__dict__:
                if k not in settings:
                    continue
                state.settings.__dict__[k] = settings[k]
    except FileNotFoundError:
        pass

    task_mgr = TaskManager()

    while not glfw.window_should_close(glfw_window):
        glfw.poll_events()
        gl.glClear(int(gl.GL_COLOR_BUFFER_BIT)|int(gl.GL_DEPTH_BUFFER_BIT))
        renderer.new_frame()
        imgui.new_frame()

        viewport = imgui.get_main_viewport()
        imgui.push_style_var(StyleVar.WINDOW_PADDING, (5, 5))
        imgui.set_next_window_pos(viewport.pos)
        imgui.set_next_window_size(viewport.size)
        imgui.begin("##MainWindow", flags=
            WindowFlags.NO_TITLE_BAR|WindowFlags.NO_COLLAPSE|
            WindowFlags.NO_RESIZE|WindowFlags.NO_MOVE|
            WindowFlags.NO_BRING_TO_FRONT_ON_FOCUS|WindowFlags.NO_NAV)
        imgui.pop_style_var()
        draw_gui(state, task_mgr)
        imgui.end()

        imgui.render()
        renderer.render(imgui.get_draw_data())
        glfw.swap_buffers(glfw_window)

    task_mgr.shutdown()
    renderer.shutdown()
    imgui.destroy_context(None)

if __name__ == "__main__":
    mp.freeze_support()
    main()