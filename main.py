from dataclasses import dataclass
import copy
import ctypes
import dataclasses
import io
import multiprocessing as mp
import multiprocessing.queues as mpq
import multiprocessing.sharedctypes as mpsct
import os
import queue
import re
import struct
import typing
import unicodedata
import zipfile

from slimgui import imgui
from slimgui.imgui import StyleVar, WindowFlags, DragDropFlags, MouseButton, ItemFlags
from slimgui.integrations.glfw import GlfwRenderer
import OpenGL.GL as gl
import chardet
import crossfiledialog
import glfw
import typst

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
  text(14pt, "EPR 02/15 - Darwin")
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

# State that can be passed to sub-processes
@dataclass
class State:
    subms: list[SubmissionArchive]|None = None
    subm_idx: int = 0
    subm_files_ordered: list[list[SubmissionFileInfo]] = default_field([])
    title: str = ""

    editing_file_info_idx: int|None = None
    focus_editing_file_info: bool = False

# Main process state
@dataclass
class MainState:
    active_subprocess: mp.Process|None = None
    subprocess_name: str = ""
    subprocess_msgqueue: mp.Queue|None = None
    subprocess_msg: str = ""

def export_to_feedback_folder(state: State, msgq: mp.Queue) -> None:
    assert state.subms is not None
    msgq.put("choosing destination")
    folder_name = crossfiledialog.choose_folder("Choose output folder", None)
    if folder_name == "":
        msgq.put(None)
        return
    out_dir = os.path.join(folder_name, state.title)
    for i, subm in enumerate(state.subms):
        subm_dir = os.path.join(out_dir, subm.folder_name())
        typst_code = ""
        for finfo in state.subm_files_ordered[i]:
            fdata = state.subms[i].files[finfo.filename]
            if finfo.kind == "ignore":
                continue
            if isinstance(fdata, bytes):
                file_path = os.path.join(subm_dir, finfo.custom_filename)
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                with open(file_path, "wb") as f:
                    assert isinstance(fdata, bytes)
                    f.write(fdata)
            elif isinstance(fdata, str):
                assert isinstance(fdata, str)
                if typst_code == "":
                    typst_code += TYPST_PRELUDE.replace(
                        "{title}", state.title).replace(
                        "{name}", subm.stud_name)
                else:
                    typst_code += f"#pagebreak()\n"
                lang = "py"
                if not finfo.custom_filename.endswith(".py"):
                    lang = "md" # TODO: more precise guessing
                typst_code += f"== `{finfo.custom_filename}`\n"
                typst_code += f"#codly.new(```{lang}\n{fdata}\n```)\n"
            else:
                assert "unreachable"
        if typst_code != "":
            file_path = os.path.join(subm_dir, f"{subm.stud_name}_{subm.stud_id}_code.pdf")
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            msgq.put(f"compiling code for {subm.stud_name}")
            out = typst.compile(typst_code.encode("utf-8"))
            with open(file_path, "wb") as f:
                f.write(out)
    msgq.put(None)

def draw_gui(state: State, main_state: MainState) -> None:
    if imgui.button("Open submission ZIP"):
        fname = crossfiledialog.open_file("Open submission archive", None, "*.zip")
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
    imgui.same_line()
    imgui.begin_disabled(state.subms is None)
    if imgui.button("Export to feedback folder"):
        msgq = mp.Queue()
        p = mp.Process(target=export_to_feedback_folder, args=(state, msgq))
        p.start()
        main_state.active_subprocess = p
        main_state.subprocess_msgqueue = msgq
        main_state.subprocess_name = "export feedback folder"
    imgui.end_disabled()
    imgui.same_line()
    imgui.begin_disabled()
    if imgui.button("Pack feedback ZIP"):
        pass
    imgui.end_disabled()


    if main_state.active_subprocess is not None:
        imgui.progress_bar(imgui.get_time() * -0.5, overlay=f"{main_state.subprocess_name}: {main_state.subprocess_msg}")
        assert main_state.subprocess_msgqueue is not None
        try:
            msg = main_state.subprocess_msgqueue.get(False)
            if msg is not None:
                main_state.subprocess_msg = msg
            else:
                main_state.active_subprocess.join()
                main_state.active_subprocess = None
                main_state.subprocess_msgqueue = None
                main_state.subprocess_name = ""
        except queue.Empty:
            pass

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

    state = State()
    main_state = MainState()

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
        draw_gui(state, main_state)
        imgui.end()

        imgui.render()
        renderer.render(imgui.get_draw_data())
        glfw.swap_buffers(glfw_window)

    renderer.shutdown()
    imgui.destroy_context(None)

if __name__ == "__main__":
    mp.freeze_support()
    main()