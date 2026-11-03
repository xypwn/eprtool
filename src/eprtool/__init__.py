import pkgutil
import time
import traceback
import sys
from dataclasses import dataclass, field
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
from slimgui.imgui import StyleVar, WindowFlags, DragDropFlags, MouseButton, ItemFlags, Cond, Col, FLT_MAX, ChildFlags
from slimgui.integrations.glfw import GlfwRenderer
import OpenGL.GL as gl
import chardet
import crossfiledialog
import glfw
import typst
import tomllib
import tomli_w
import platformdirs
from showinfm import show_in_file_manager

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

def write_file(path: str, data: bytes|str) -> None:
    dir = os.path.dirname(path)
    if dir:
        os.makedirs(dir, exist_ok=True)
    if isinstance(data, bytes):
        with open(path, "wb") as f: f.write(data)
    else:
        with open(path, "w") as f: f.write(data)

def read_file_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()

def read_file_str(path: str) -> str:
    with open(path, "r") as f:
        return f.read()

_imgui_font_mono: imgui.Font|None = None
def font_mono() -> imgui.Font:
    assert _imgui_font_mono is not None
    return _imgui_font_mono

def imgui_is_item_double_clicked(mouse_button: MouseButton = MouseButton.LEFT) -> bool:
    return imgui.is_item_clicked(mouse_button) and imgui.is_mouse_double_clicked(mouse_button)

def imgui_object_field_input_text(label: str, obj: object, obj_field: str, hint: str = "", flags: imgui.InputTextFlags = imgui.InputTextFlags.NONE) -> bool:
    s = obj.__dict__[obj_field]
    changed, s_new = imgui.input_text_with_hint(label, hint, s, flags)
    if changed:
        obj.__dict__[obj_field] = s_new
    return changed

def imgui_align_button_row(labels: typing.Iterable[str], align: typing.Literal["left", "right", "center"]) -> None:
    style = imgui.get_style()
    labels = list(labels)

    if align != "left":
        width = 0
        for s in labels:
            width += imgui.calc_text_size(s)[0]
        width += 2*style.frame_padding[0]*len(labels)
        width += style.item_spacing[0]*(len(labels)-1)

    cx, cy = imgui.get_cursor_screen_pos()
    match align:
        case "right":
            cx += imgui.get_content_region_avail()[0]
            cx -= width
        case "center":
            cx += imgui.get_content_region_avail()[0]/2
            cx -= width/2
    imgui.set_cursor_screen_pos((cx, cy))

def default_field(x):
    return dataclasses.field(default_factory=lambda: copy.deepcopy(x))

TYPST_IMAGE_FORMATS = {
    ".png": "png",
    ".jpg": "jpg",
    ".jpeg": "jpg",
    ".svg": "svg",
    ".pdf": "pdf",
    ".webp": "webp",
}

@dataclass
class SubmissionArchive:
    stud_name: str
    stud_id: str
    filename: str
    files: dict[str, str|bytes]

    def folder_name(self) -> str:
        return f"{self.stud_name}_{self.stud_id}"

RE_SUBMISSION_FILE = re.compile(r"^(?P<name>[^ ]+ [^_]+)_(?P<id>\d+)_assignsubmission_file/(?P<file>[^/]+)$")

@dataclass
class SubmissionFilesResult:
    submissions: list[SubmissionArchive] = default_field([])
    skipped_files: list[str] = default_field([])
    unrecognized_files: list[str] = default_field([])

# Submission may be ZIP or single file
def submission_files_from_zip(filename: str) -> SubmissionFilesResult:
    # Automatically decodes bytes into a string, guessing the encoding.
    def auto_str(b: bytes) -> str:
        enc = chardet.detect(b, include_encodings=["utf-8", "437"])["encoding"]
        assert enc is not None
        return b.decode(enc)

    result = SubmissionFilesResult()

    # Returns single file, or unarchives a ZIP into multiple files.
    def submission_file_to_files(submission_filename: str, inp: typing.IO[bytes]) -> dict[str, str|bytes]:
        def file_to_str_or_bytes(filename: str, f: typing.IO[bytes]) -> str|bytes|None:
            ext = os.path.splitext(filename)[1].lower()
            if ext == ".py":
                is_text = True
            elif os.path.basename(filename).lower() in ("readme", "readme.md", "readme.txt"):
                is_text = True
            elif ext == ".pdf" or ext in TYPST_IMAGE_FORMATS:
                is_text = False
            else:
                result.unrecognized_files.append(submission_filename+"/"+filename)
                return None
            if is_text:
                return auto_str(f.read())
            else:
                return f.read()

        files: dict[str, str|bytes] = {}

        if os.path.splitext(submission_filename)[1].lower() != ".zip":
            data = file_to_str_or_bytes(submission_filename, inp)
            if data is not None:
                files[submission_filename] = data
            return files

        with zipfile.ZipFile(inp) as ar:
            for finfo in ar.filelist:
                filename = finfo.filename
                if not finfo.flag_bits&0x800:
                    # UTF8 flag not set -> try to decode as UTF-8 anyway
                    # (some archivers don't seem to set the flag properly).
                    filename = auto_str(finfo.filename.encode('437'))
                filename = unicodedata.normalize("NFC", filename)
                if any(s in ("__MACOSX", ".DS_Store") for s in finfo.filename.split("/")):
                    continue
                if finfo.is_dir():
                    continue
                with ar.open(finfo.filename) as f:
                    data = file_to_str_or_bytes(filename, f)
                    if data is None:
                        continue
                    files[filename] = data
        return files

    with zipfile.ZipFile(filename, "r") as sub:
        for fname in sub.namelist():
            m = RE_SUBMISSION_FILE.match(fname)
            if m is None:
                result.skipped_files.append(fname)
                continue
            result.submissions.append(SubmissionArchive(
                m["name"],
                m["id"],
                fname,
                submission_file_to_files(fname, sub.open(fname)),
            ))
    result.submissions.sort(key=lambda s: s.folder_name())
    return result

SubmissionFileKindTag = typing.Literal["embed", "separate", "ignore"]

@dataclass
class SubmissionFileInfo:
    custom_filename: str
    filename: str
    kind: SubmissionFileKindTag

@dataclass
class Settings:
    group_tutor: str = field(default="", metadata={"label": "Group and tutor", "hint": "e.g. EPR01 - Firstname"})
    pdf_suffix: str = field(default="_kommentiert", metadata={"label": "PDF suffix", "tooltip": "E.g. for xournalpp: German -> _kommentiert, English -> _annotated"})

# State that can be passed to tasks
@dataclass
class State:
    dirs: platformdirs.PlatformDirsABC

    subms: list[SubmissionArchive]|None = None
    subm_idx: int = 0
    subm_files_ordered: list[list[SubmissionFileInfo]] = default_field([])
    excercise: str = ""
    group: str = ""

    editing_file_info_idx: int|None = None
    focus_editing_file_info: bool = False

    last_exported_folder: str = ""

    info_popup: str = ""
    info_popup_show_file: str = ""
    info_popup_show_folder: str = ""

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

RE_GUESS_EXCERCISE_GROUP = re.compile(r"^[\w ]+?\d+[\w -]+?(?P<ex>\d+)[\w -]+?(?P<gr>\d+)", re.UNICODE)

def _task_open_submission_zip(ctx: Context, state: State) -> State:
    ctx.msgq.put(Status("choosing file"))

    # TODO: Decide which dialog type to use
    #fname = filedialog.askopenfilename(filetypes=(("ZIP Archive", "*.zip"),), title="Open submission archive")
    fname = crossfiledialog.open_file("Open submission archive",
        state.dirs.user_downloads_dir, "*.zip")
    if fname == "" or fname is None:
        return state

    # Guess excercise title and group
    m = RE_GUESS_EXCERCISE_GROUP.match(os.path.basename(fname))
    if m is not None:
        state.excercise = f"Uebung {m["ex"]}"
        state.group = f"EPR{m["gr"]}"

    submission_files = submission_files_from_zip(fname)
    state.subms = submission_files.submissions
    state.subm_files_ordered = []
    for i, subm in enumerate(state.subms):
        state.subm_files_ordered.append([])
        for fname in sorted(subm.files.keys()):
            state.subm_files_ordered[i].append(SubmissionFileInfo(
                fname,
                fname,
                "separate" if fname.lower().endswith(".pdf") else "embed",
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

    if submission_files.skipped_files:
        state.info_popup += f"- Skipped {len(submission_files.skipped_files)} files in the submission archive that didn't look like submissions: {submission_files.skipped_files}\n"
    if submission_files.unrecognized_files:
        state.info_popup += f"- Failed to recognize type of {len(submission_files.unrecognized_files)} submitted files: {submission_files.unrecognized_files}\n"

    return state

RE_VALID_EXCERCISE_TITLE = re.compile(r"^[\w\-. ]+$")

def _task_export_to_feedback_folder(ctx: Context, state: State) -> State:
    if RE_VALID_EXCERCISE_TITLE.match(state.excercise) is None:
        raise Exception("Invalid excercise title")
    assert state.subms is not None
    ctx.msgq.put(Status("choosing destination"))
    folder_name = crossfiledialog.choose_folder("Choose output folder",
        os.path.dirname(state.last_exported_folder) if state.last_exported_folder != "" else None)
    if folder_name == "" or folder_name is None:
        return state
    tomldata = {
        "excercise": state.excercise,
        "group": state.group,
        "submissions": {},
    }
    num_pdfs = 0
    out_dir = os.path.join(folder_name, f"{state.excercise}_{state.group}")
    for i, subm in enumerate(state.subms):
        subm_dir = os.path.join(out_dir, subm.folder_name())
        tomldata["submissions"][subm.stud_id] = {
            "stud-name": subm.stud_name,
            "stud-id": subm.stud_id,
            "files": [],
        }
        ctx.msgq.put(Status(f"Unpacking files and compiling PDFs ({i}/{len(state.subms)})", i/len(state.subms)))
        typst_code: list[str] = []
        for finfo in state.subm_files_ordered[i]:
            tomldata["submissions"][subm.stud_id]["files"].append({
                "name": finfo.filename,
                "custom-name": finfo.custom_filename,
                "kind": finfo.kind,
            })
            fdata = state.subms[i].files[finfo.filename]
            if finfo.kind == "ignore":
                continue

            write_file(os.path.join(subm_dir, "files", finfo.custom_filename), fdata)
            if finfo.kind == "embed":
                code = ""
                code += f"== `{finfo.custom_filename}`\n"
                ext = os.path.splitext(finfo.custom_filename)[1].lower()
                if isinstance(fdata, str):
                    lang = "py"
                    if not ext == ".py":
                        lang = "md" # TODO: more precise guessing
                    code += f"#codly.new(```{lang}\n{fdata}\n```)\n"
                else:
                    assert isinstance(fdata, bytes)
                    code += f"#image(bytes(({"".join(str(b)+"," for b in fdata)})),\nwidth: 100%, format: \"{TYPST_IMAGE_FORMATS[ext]}\")\n"
                typst_code.append(code)
            else:
                assert finfo.kind == "separate"
                write_file(os.path.join(subm_dir, finfo.custom_filename), fdata)
        if len(typst_code):
            code = TYPST_PRELUDE.replace(
                        "{title}", state.excercise).replace(
                        "{name}", subm.stud_name).replace(
                        "{tutor}", state.settings.group_tutor)
            code += "#pagebreak()\n".join(typst_code)
            write_file(
                os.path.join(subm_dir, f"{subm.stud_name}_{subm.stud_id}.pdf"),
                typst.compile(code.encode("utf-8")))
            num_pdfs += 1
    state.last_exported_folder = out_dir
    write_file(os.path.join(out_dir, "eprtool.toml"),
        tomli_w.dumps(tomldata))
    state.info_popup = f"Exported {len(state.subms)} submissions (generated {num_pdfs} PDFs)"
    state.info_popup_show_folder = out_dir
    return state

def _task_pack_feedback_zip(ctx: Context, state: State) -> State:
    ctx.msgq.put(Status("choosing feedback folder"))
    folder_name = crossfiledialog.choose_folder("Choose feedback folder",
        state.last_exported_folder if state.last_exported_folder != "" else None)
    if folder_name == "" or folder_name is None:
        return state

    tomldata = tomllib.loads(read_file_str(os.path.join(folder_name, "eprtool.toml")))

    excercise, group = tomldata["excercise"], tomldata["group"]
    assert isinstance(excercise, str)
    assert isinstance(group, str)

    def filename_with_suffix(fname: str, suffix: str) -> str:
        root, ext = os.path.splitext(fname)
        return root+suffix+ext

    total_files = 0
    packed_files = 0

    zip_path = os.path.join(folder_name, os.path.basename(folder_name)+"_annotated.zip")
    with zipfile.ZipFile(zip_path, "w") as ar:
        for subm in tomldata["submissions"].values():
            subm_dir = os.path.join(folder_name, f"{subm["stud-name"]}_{subm["stud-id"]}")
            subm_zip_dir = f"{subm["stud-name"]}_{subm["stud-id"]}_assignsubmission_file"
            have_pdf = False
            for finfo in subm["files"]:
                if finfo["kind"] == "embed":
                    have_pdf = True
                elif finfo["kind"] == "separate":
                    total_files += 1
                    file_path = os.path.join(subm_dir, filename_with_suffix(finfo["custom-name"], state.settings.pdf_suffix))
                    if os.path.isfile(file_path):
                        ar.write(file_path,
                            arcname=f"{subm_zip_dir}/Feedback_{excercise}_{subm["stud-name"]}_{finfo["custom-name"].replace("/", "_")}")
                        packed_files += 1
                else:
                    assert finfo["kind"] == "ignore"
            if have_pdf:
                total_files += 1
                file_path = os.path.join(subm_dir, filename_with_suffix(f"{subm["stud-name"]}_{subm["stud-id"]}.pdf", state.settings.pdf_suffix))
                if os.path.isfile(file_path):
                    ar.write(file_path,
                        arcname=f"{subm_zip_dir}/Feedback_{excercise}_{subm["stud-name"]}.pdf")
                    packed_files += 1

    state.info_popup = f"Successfully added feedback ZIP to folder."
    if packed_files != total_files:
        state.info_popup += f"\n\nWARNING: Found {total_files-packed_files}/{total_files} unannotated PDFs!"
    state.info_popup_show_file = zip_path

    return state

def draw_gui(state: State, task_mgr: TaskManager) -> None:
    imgui.begin_disabled(task_mgr.working)
    if imgui.button("Open submission ZIP"):
        task_mgr.enqueue(Task(_task_open_submission_zip, (state,)))
        # TODO: This is a very hacky way of doing the popup
        imgui.open_popup("Status")
    imgui.same_line()
    imgui.begin_disabled(state.subms is None)
    if imgui.button("Export to feedback folder"):
        task_mgr.enqueue(Task(_task_export_to_feedback_folder, (state,)))
        imgui.open_popup("Status")
    imgui.end_disabled()
    imgui.same_line()
    if imgui.button("Pack feedback ZIP"):
        task_mgr.enqueue(Task(_task_pack_feedback_zip, (state,)))
        imgui.open_popup("Status")
    imgui.end_disabled()

    if imgui.tree_node("Settings"):
        for k, v in state.settings.__dict__.items():
            metadata = Settings.__dataclass_fields__[k].metadata
            label = metadata.get("label", k)
            hint = metadata.get("hint", "")
            tooltip = metadata.get("tooltip", "")
            v_changed, v_new = imgui.input_text_with_hint(label, hint, v)
            if v_changed:
                state.settings.__dict__[k] = v_new
                state.settings_last_edited_time = imgui.get_time()
                state.settings_saved = False
            if tooltip:
                imgui.set_item_tooltip(tooltip)
        imgui.push_style_color(Col.TEXT, (1, 1, 1, 0.6))
        imgui.text("(Saved)" if state.settings_saved else "(Waiting)")
        imgui.pop_style_color()
        imgui.tree_pop()
        imgui.spacing()
    if not state.settings_saved and imgui.get_time() - state.settings_last_edited_time > 0.5:
        os.makedirs(os.path.dirname(state.settings_path()), exist_ok=True)
        write_file(state.settings_path(), tomli_w.dumps(state.settings.__dict__))
        state.settings_saved = True

    if state.subms is not None:
        imgui_object_field_input_text("Excercise", state, "excercise", "e.g. Uebung 0")
        imgui_object_field_input_text("Group", state, "group", "e.g. EPR01")
        imgui.begin_child("submissions", (150, 0), imgui.ChildFlags.BORDERS|imgui.ChildFlags.RESIZE_X)
        imgui.separator_text(f"{len(state.subms)} submissions")
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
                    file_kind_tags: list[SubmissionFileKindTag] = ["embed", "separate", "ignore"]
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

    imgui.set_next_window_size((400, 300), Cond.APPEARING)
    imgui.set_next_window_pos(imgui.get_main_viewport().get_center(), Cond.ALWAYS, (0.5, 0.5))
    if imgui.begin_popup_modal("Error", flags=WindowFlags.NO_SCROLLBAR)[0]:
        tb = ""
        if task_mgr.error is not None:
            tb =  "\n".join(task_mgr.error.formatted_tb)
            imgui.push_font(font_mono(), 0)
            imgui.push_text_wrap_pos()
            imgui.text(f"Error: {task_mgr.error}")
            imgui.pop_text_wrap_pos()
            style = imgui.get_style()
            tbwin_w, tbwin_h = imgui.get_content_region_avail()
            tbwin_h -= imgui.get_font_size() + 2*style.frame_padding[1]
            imgui.begin_child("Error Traceback", (tbwin_w, tbwin_h), ChildFlags.NONE, WindowFlags.HORIZONTAL_SCROLLBAR)
            imgui.text(tb)
            imgui.end_child()
            imgui.pop_font()
        imgui_align_button_row(["Close", "Copy to clipboard"], "center")
        if imgui.button("Close", (0, 0)):
            imgui.close_current_popup()
        imgui.same_line()
        if imgui.button("Copy to clipboard", (0, 0)):
            imgui.set_clipboard_text(f"Error: {task_mgr.error}\n{tb}")
        imgui.end_popup()

    if state.info_popup:
        imgui.set_next_window_size_constraints((300, 0), (FLT_MAX, FLT_MAX))
        imgui.set_next_window_pos(imgui.get_main_viewport().get_center(), Cond.ALWAYS, (0.5, 0.5))
        if imgui.begin_popup_modal("Info", flags=WindowFlags.ALWAYS_AUTO_RESIZE)[0]:
            imgui.push_text_wrap_pos()
            imgui.text(state.info_popup)
            imgui.pop_text_wrap_pos()
            button_labels = ["Close"]
            if state.info_popup_show_file:
                button_labels = ["Show in file manager"]+button_labels
            if state.info_popup_show_folder:
                button_labels = ["Open folder"]+button_labels
            imgui_align_button_row(button_labels, "center")
            if imgui.button("Close", (0, 0)):
                state.info_popup = ""
                state.info_popup_show_file = ""
                state.info_popup_show_folder = ""
                imgui.close_current_popup()
            if state.info_popup_show_file:
                imgui.same_line()
                if imgui.button("Show in file manager"):
                    show_in_file_manager(state.info_popup_show_file)
            if state.info_popup_show_folder:
                imgui.same_line()
                if imgui.button("Open folder"):
                    show_in_file_manager(state.info_popup_show_folder)
            imgui.end_popup()

    # Handle tasks
    imgui.set_next_window_pos(imgui.get_main_viewport().get_center(), Cond.ALWAYS, (0.5, 0.5))
    if imgui.begin_popup_modal("Status", flags=WindowFlags.ALWAYS_AUTO_RESIZE)[0]:
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

                if v.info_popup:
                    imgui.open_popup("Info")
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

    # Frame limiting
    TARGET_FRAME_RATE: float = 60.0
    last_draw_timestamp: float = time.time()

    imgui.create_context()
    io = imgui.get_io()
    io.config_flags |= imgui.ConfigFlags.NAV_ENABLE_KEYBOARD
    renderer = GlfwRenderer(glfw_window)
    
    # Load fonts
    fontdata = pkgutil.get_data("eprtool", "res/DejaVuSans.ttf")
    assert fontdata is not None
    io.fonts.add_font_from_memory_ttf(fontdata)
    fontdata = pkgutil.get_data("eprtool", "res/DejaVuSansMono.ttf")
    global _imgui_font_mono
    assert fontdata is not None
    _imgui_font_mono = io.fonts.add_font_from_memory_ttf(fontdata)

    state = State(platformdirs.PlatformDirs("eprtool"))
    try:
        settings = tomllib.loads(read_file_str(state.settings_path()))
        for k in state.settings.__dict__:
            if k not in settings:
                continue
            state.settings.__dict__[k] = settings[k]
    except FileNotFoundError:
        pass

    task_mgr = TaskManager()

    DEBUG_FPS = False
    if DEBUG_FPS:
        fps_cnt = 0
        fps_time = time.time()

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

    while not glfw.window_should_close(glfw_window):
        now = time.time()
        time_to_draw = now - last_draw_timestamp
        num_frames_to_draw = time_to_draw * TARGET_FRAME_RATE
        if time_to_draw < 0:
            print(f"skipped {-num_frames_to_draw} frames due to over-draw")
            last_draw_timestamp = now
        elif time_to_draw >= 1:
            print(f"dropped {num_frames_to_draw} frames due to under-draw")
            last_draw_timestamp = now
        elif num_frames_to_draw >= 1:
            draw()
            last_draw_timestamp += 1.0/TARGET_FRAME_RATE
        else:
            time.sleep(time_to_draw)

    task_mgr.shutdown()
    renderer.shutdown()
    imgui.destroy_context(None)

if __name__ == "__main__":
    mp.freeze_support()
    main()