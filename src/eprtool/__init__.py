from pathlib import Path
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

from imgui_bundle import imgui, immapp, portable_file_dialogs, hello_imgui, rich_md
import chardet
import crossfiledialog
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

def write_file(path: str|Path, data: bytes|str) -> None:
    path = Path(path)
    os.makedirs(path.parent, exist_ok=True)
    if isinstance(data, bytes):
        with open(path, "wb") as f: f.write(data)
    else:
        with open(path, "w") as f: f.write(data)

def read_file_bytes(path: str|Path) -> bytes:
    with open(path, "rb") as f:
        return f.read()

def read_file_str(path: str|Path) -> str:
    with open(path, "r") as f:
        return f.read()

def get_res_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS"))/"eprtool"/"res"
    else:
        return Path(__file__).parent/"res"

_imgui_font_mono: imgui.ImFont|None = None

def imgui_is_item_double_clicked(mouse_button: imgui.MouseButton = imgui.MouseButton_.left) -> bool:
    return imgui.is_item_clicked(mouse_button) and imgui.is_mouse_double_clicked(mouse_button)

def imgui_object_field_input_text(label: str, obj: object, obj_field: str, hint: str = "", flags: imgui.InputTextFlags = imgui.InputTextFlags_.none) -> bool:
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

    rebuild_dock_layout = True

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

    help_markdown: str = "# Help"

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

    def filename_list_md(names: list[str]) -> str:
        return "``` "+"\n".join(names)+" ```"
    if submission_files.skipped_files:
        state.info_popup += f"- Skipped {len(submission_files.skipped_files)} files in the submission archive that didn't look like submissions: {filename_list_md(submission_files.skipped_files)}\n"
    if submission_files.unrecognized_files:
        state.info_popup += f"- Failed to recognize type of {len(submission_files.unrecognized_files)} submitted files: {filename_list_md(submission_files.unrecognized_files)}\n"

    return state

RE_VALID_EXCERCISE_TITLE = re.compile(r"^[\w\-. ]+$")

def _task_export_to_feedback_folder(ctx: Context, state: State) -> State:
    if RE_VALID_EXCERCISE_TITLE.match(state.excercise) is None:
        raise Exception("Invalid excercise title")
    assert state.subms is not None
    ctx.msgq.put(Status("choosing destination"))
    folder_name = crossfiledialog.choose_folder("Choose output folder",
        os.path.dirname(state.last_exported_folder) if state.last_exported_folder != "" else None)  # ty: ignore[invalid-argument-type]
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
        state.last_exported_folder if state.last_exported_folder != "" else None)  # ty: ignore[invalid-argument-type]
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

    state.info_popup = ""
    if packed_files != total_files:
        state.info_popup += f"> [!WARNING]\n> Found {total_files-packed_files}/{total_files} unannotated PDFs!\n\n"
    state.info_popup += f"Successfully added feedback ZIP to folder."
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
    imgui.same_line()
    if imgui.button("?"):
        imgui.open_popup("Help")
    imgui.set_item_tooltip("Help")

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
        imgui.push_style_color(imgui.Col_.text, (1, 1, 1, 0.6))
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

        dock_id = imgui.get_id("MainDock")
        if state.rebuild_dock_layout:
            imgui.internal.dock_builder_add_node(dock_id)
            sp = imgui.internal.dock_builder_split_node(dock_id, imgui.Dir.left, 0.4)
            imgui.internal.dock_builder_dock_window("Submissions", sp.id_at_dir)
            imgui.internal.dock_builder_dock_window("Contents", sp.id_at_opposite_dir)
            imgui.internal.dock_builder_finish(dock_id)
            state.rebuild_dock_layout = False
        imgui.dock_space(dock_id)

        imgui.begin("Submissions")
        imgui.separator_text(f"{len(state.subms)} submissions")
        for i, s in enumerate(state.subms):
            if imgui.selectable(f"{s.stud_name} ({s.stud_id})", i == state.subm_idx, imgui.SelectableFlags_.select_on_nav)[0]:
                state.subm_idx = i
                state.editing_file_info_idx = None
        imgui.end()
        imgui.begin("Contents")
        if state.subm_idx < len(state.subms):
            subm = state.subms[state.subm_idx]
            ordered_finfos = state.subm_files_ordered[state.subm_idx]
            imgui.push_text_wrap_pos()
            imgui.text(f"Submitter: {subm.stud_name} ({s.stud_id})")
            imgui.text(f"Submission filename: {"/".join(subm.filename.split("/")[1:])}")
            imgui.separator_text("Files")
            imgui.push_item_flag(imgui.ItemFlags_.allow_duplicate_id, True)
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
                        i_next = i + (-1 if imgui.get_mouse_drag_delta(imgui.MouseButton_.left)[1] < 0.0 else 1)
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
                    file_kind_tags: list[str] = ["embed", "separate", "ignore"]
                    fk_changed, fk = imgui.combo("##Kind", file_kind_tags.index(info.kind), file_kind_tags)
                    if fk_changed:
                        info.kind = file_kind_tags[fk]  # ty: ignore[invalid-assignment]
                    any_active = any_active or imgui.is_item_active()
                    if not state.focus_editing_file_info and not any_active and imgui.is_window_focused():
                        state.editing_file_info_idx = None
                    state.focus_editing_file_info = False
            imgui.pop_item_flag()
            imgui.pop_text_wrap_pos()
        imgui.end()

    def avail_size_without_button_row() -> tuple[float, float]:
        style = imgui.get_style()
        cw, ch = imgui.get_content_region_avail()
        ch -= imgui.get_font_size() + 2*style.frame_padding[1]
        return cw, ch

    def begin_modal_box(name: str, initial_size: tuple[float, float]|None = None) -> bool:
        initial_size = initial_size or (400, 300)
        style = imgui.get_style()
        imgui.set_next_window_size(initial_size, imgui.Cond_.appearing)
        imgui.set_next_window_pos(imgui.get_main_viewport().get_center(), imgui.Cond_.always, (0.5, 0.5))
        imgui.set_next_window_size_constraints((75, 75), (imgui.FLT_MAX, imgui.FLT_MAX))
        if not imgui.begin_popup_modal(name, flags=imgui.WindowFlags_.no_scrollbar)[0]:
            return False
        imgui.begin_child("##Content", avail_size_without_button_row())
        return True
    def end_modal_box(extra_button_labels: list[str]|None = None) -> typing.Literal["", "Close"]|str:
        imgui.end_child()
        labels = ["Close"] + (extra_button_labels or [])
        imgui_align_button_row(labels, "center")
        pressed = ""
        for i, label in enumerate(labels):
            if i != 0:
                imgui.same_line()
            if imgui.button(label):
                pressed = label
        if pressed == "Close":
            imgui.close_current_popup()
        imgui.end_popup()
        return pressed

    if begin_modal_box("Help", (600, 500)):
        rich_md.render(state.help_markdown)
        end_modal_box()

    if begin_modal_box("Error"):
        if task_mgr.error is not None:
            tb =  "\n".join(task_mgr.error.formatted_tb)
            rich_md.render(f"Error: `{task_mgr.error}`")
            rich_md.render(f"``` {tb} ```")
        end_modal_box()

    if state.info_popup:
        if begin_modal_box("Info"):
            rich_md.render(state.info_popup)
            button_labels = []
            if state.info_popup_show_file:
                button_labels.append("Show in file manager")
            if state.info_popup_show_folder:
                button_labels.append("Open folder")
            match end_modal_box(button_labels):
                case "Show in file manager":
                    show_in_file_manager(state.info_popup_show_file)
                case "Open folder":
                    show_in_file_manager(state.info_popup_show_folder)
                case "Close":
                    state.info_popup = ""
                    state.info_popup_show_file = ""
                    state.info_popup_show_folder = ""

    # Handle tasks
    imgui.set_next_window_pos(imgui.get_main_viewport().get_center(), imgui.Cond_.always, (0.5, 0.5))
    if imgui.begin_popup_modal("Status", flags=imgui.WindowFlags_.always_auto_resize)[0]:
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
    state = State(
        platformdirs.PlatformDirs("eprtool"),
        help_markdown=read_file_str(str(get_res_dir()/"HELP.md")))
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

        draw_gui(state, task_mgr)

    def load_fonts():
        fonts = imgui.get_io().fonts
        fonts.add_font_from_file_ttf(str(get_res_dir()/"DejaVuSans.ttf"), 15)
        _font_mono = fonts.add_font_from_file_ttf(str(get_res_dir()/"DejaVuSansMono.ttf"), 15)

    def post_init_ig():
        io = imgui.get_io()
        io.config_flags |= imgui.ConfigFlags_.nav_enable_keyboard
        io.config_flags |= imgui.ConfigFlags_.nav_enable_gamepad

    rp = hello_imgui.RunnerParams()
    rp.imgui_window_params.enable_viewports = True
    rp.app_window_params.window_title = "EPR Tool"
    rp.callbacks.show_gui = draw
    rp.callbacks.load_additional_fonts = load_fonts
    rp.callbacks.post_init = post_init_ig
    rp.fps_idling = hello_imgui.FpsIdling(5)
    rp.ini_disable = True
    ap = immapp.AddOnsParams()
    md_opts = rich_md.MarkdownOptions()
    ap.with_markdown = True
    ap.with_markdown_options = md_opts
    immapp.run(rp, ap)

    task_mgr.shutdown()

if __name__ == "__main__":
    mp.freeze_support()
    main()