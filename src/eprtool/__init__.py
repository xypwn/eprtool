import copy
import mimetypes
import os
import pickle
import re
import tomllib
import typing
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import chardet
import crossfiledialog
import tomli_w
import typst
from imgui_bundle import imgui, rich_md
from showinfm import show_in_file_manager

from eprtool import app, stylecheck, stylecheck_ui, ui

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

IGNORED_DIRS = (
    "__MACOSX",
    ".venv", "__pycache__", ".idea",
)

IGNORED_FILES = (
    ".DS_Store",
)

TYPST_IMAGE_FORMATS = {
    ".png": "png",
    ".jpg": "jpg",
    ".jpeg": "jpg",
    ".svg": "svg",
    ".pdf": "pdf",
    ".webp": "webp",
}

@dataclass
class SingleSubmission:
    stud_name: str
    stud_id: str
    filename: str
    files: dict[str, str|bytes]
    is_manually_added: bool = False

    def folder_name(self) -> str:
        return f"{self.stud_name}_{self.stud_id}"

RE_SUBMISSION_FILE = re.compile(r"^(?P<name>[^ ]+ [^_]+)_(?P<id>\d+)_assignsubmission_file/(?P<file>[^/]+)$")

@dataclass
class SubmissionFilesResult:
    submissions: list[SingleSubmission] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)
    unrecognized_files: list[str] = field(default_factory=list)

@dataclass
class FileKind:
    """Kind of file in the context of the tool
    """
    def __init__(self, filename: str):
        ext = os.path.splitext(filename)[1].lower()
        base = os.path.basename(filename).lower()
        self.mimetype = None
        self.is_text = False
        if ext in mimetypes.types_map:
            self.mimetype = mimetypes.types_map[ext]
            self.is_text = self.mimetype.startswith("text/")
        self.ext = ext
        self.is_readme = base in ("readme", "readme.md", "readme.txt")
        if self.is_readme:
            self.is_text = True

    mimetype: str | None
    ext: str
    is_text: bool
    is_readme: bool

# Submission may be ZIP or single file
def submission_files_from_zip_or_file(filename: str, stud_name_if_single_subm_file: str|None = None) -> SubmissionFilesResult:
    # Automatically decodes bytes into a string, guessing the encoding.
    def auto_str(b: bytes) -> str:
        enc = chardet.detect(b, include_encodings=["utf-8", "437"])["encoding"]
        assert enc is not None
        return b.decode(enc)

    result = SubmissionFilesResult()

    # Returns single file, or unarchives a ZIP into multiple files.
    def submission_file_to_files(submission_filename: str, inp: typing.IO[bytes]) -> dict[str, str|bytes]:
        def file_to_str_or_bytes(filename: str, f: typing.IO[bytes]) -> str|bytes|None:
            kind = FileKind(filename)
            if (not kind.is_readme and
                kind.ext not in (".py", ".pdf") and
                kind.ext not in TYPST_IMAGE_FORMATS):
                result.unrecognized_files.append(submission_filename+"/"+filename)
                return None
            if kind.is_text:
                return auto_str(f.read()).replace("\r\n", "\n")
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
                if finfo.is_dir():
                    continue
                if any(s in IGNORED_DIRS for s in finfo.filename.split("/")[:-1]):
                    continue
                if os.path.basename(finfo.filename) in IGNORED_FILES:
                    continue
                with ar.open(finfo.filename) as f:
                    data = file_to_str_or_bytes(filename, f)
                    if data is None:
                        continue
                    files[filename] = data
        return files

    if stud_name_if_single_subm_file is not None:
        with open(filename, "rb") as f:
            fname = os.path.basename(filename)
            result.submissions.append(SingleSubmission(
                stud_name_if_single_subm_file,
                stud_name_if_single_subm_file, # name becomes ID in this case (TODO: See if reasonable)
                fname,
                submission_file_to_files(fname, f),
                is_manually_added=True,
            ))
    else:
        with zipfile.ZipFile(filename, "r") as sub:
            for finfo in sub.infolist():
                fname = finfo.filename
                if finfo.is_dir():
                    continue
                m = RE_SUBMISSION_FILE.match(fname)
                if m is None:
                    result.skipped_files.append(fname)
                    continue
                result.submissions.append(SingleSubmission(
                    m["name"],
                    m["id"],
                    fname,
                    submission_file_to_files(fname, sub.open(fname)),
                ))
    return result

SubmissionFileKindTag = typing.Literal["embed", "separate", "ignore"]

@dataclass
class SubmissionFileInfo:
    custom_filename: str
    filename: str
    kind: SubmissionFileKindTag

@dataclass
class PersistentState:
    group_tutor: str = field(default="",
        metadata={"label": "Group and tutor", "hint": "e.g. EPR01 - Firstname"})
    pdf_suffix: str = field(default="_kommentiert",
        metadata={"label": "PDF suffix", "tooltip": "E.g. for xournalpp: German -> _kommentiert, English -> _annotated"})
    style_max_line_length: int = field(default=79,
        metadata={"label": "Style: Max line length", "tooltip": "Maximum line length (pycodestyle E501)\n0 to disable",
        "validate": lambda v: v >= 0})

@dataclass
class State:
    """State that can be passed to tasks
    """
    codeviewer: stylecheck_ui.CodeViewer

    force_rebuild_dock_layout = False

    subms: list[SingleSubmission]|None = None
    subm_idx: int = 0
    subm_file_cinfos: dict[str, list[SubmissionFileInfo]] = field(default_factory=dict) # id to custom file infos
    excercise: str = ""
    group: str = ""

    manual_subm_name: str = ""

    update_code_preview: bool = False

    editing_file_info_idx: int | None = None
    focus_editing_file_info: bool = False

    last_exported_folder: str = ""

    info_popup: str = ""
    info_popup_show_file: str = ""
    info_popup_show_folder: str = ""

    help_markdown: str = "# Help"

type AppContext = app.MainContext[State, PersistentState]

RE_GUESS_EXCERCISE_GROUP = re.compile(r"^[\w ]+?\d+[\w -]+?(?P<ex>\d+)[\w -]+?(?P<gr>\d+)", re.UNICODE)

def _task_open_submission_or_submissions(ctx: AppContext, stud_name_if_single_subm_file: str|None = "") -> None:
    with ctx.lock:
        state = ctx.state
        ctx.status = app.Status("Choosing file")
        start_dir = ctx.dirs.user_downloads_dir

    single_subm_file = stud_name_if_single_subm_file is not None

    # TODO: Decide which dialog type to use
    #fname = filedialog.askopenfilename(filetypes=(("ZIP Archive", "*.zip"),), title="Open submission archive")
    fname = crossfiledialog.open_file("Open submission archive",
        start_dir, ["*.zip", "*.py", "*.*"] if single_subm_file else "*.zip")

    with ctx.lock:
        if fname == "" or fname is None:
            ctx.status = None
            return

        ctx.status = app.Status("Importing")

        if not single_subm_file:
            # Guess excercise title and group
            m = RE_GUESS_EXCERCISE_GROUP.match(os.path.basename(fname))
            if m is not None:
                state.excercise = f"Uebung {m["ex"]}"
                state.group = f"EPR{m["gr"]}"
            else:
                state.info_popup += "- Unable to auto-detect excercise and group number. Please enter manually.\n"

        submission_files = submission_files_from_zip_or_file(fname, stud_name_if_single_subm_file)
        if not single_subm_file:
            # If we got a submission archive, we want to replace old
            # submissions.
            state.subms = None
            state.subm_file_cinfos = {}
        if state.subms is None:
            state.subms = []
        state.subms += submission_files.submissions
        state.subms.sort(key=lambda s: s.stud_name)

        if single_subm_file:
            state.subm_idx = state.subms.index(submission_files.submissions[0])
        else:
            state.subm_idx = 0

        for subm in submission_files.submissions:
            state.subm_file_cinfos[subm.stud_id] = []
            for fname in sorted(subm.files.keys()):
                state.subm_file_cinfos[subm.stud_id].append(SubmissionFileInfo(
                    fname,
                    fname,
                    "separate" if fname.lower().endswith(".pdf") else "embed",
                    ))

            # If all filenames in a submission share a common first path element,
            # strip that by default.
            strip_first_elem = True
            cinfos = state.subm_file_cinfos[subm.stud_id]
            if len(cinfos):
                common_first_elem = cinfos[0].custom_filename.split("/")[0]
                for finfo in cinfos:
                    elems = finfo.custom_filename.split("/")
                    if len(elems) < 2 or elems[0] != common_first_elem:
                        strip_first_elem = False
                        break
            if strip_first_elem:
                for finfo in cinfos:
                    finfo.custom_filename = "/".join(finfo.custom_filename.split("/")[1:])

        def filename_list_md(names: list[str]) -> str:
            return "".join(f"  - `{n}`\n" for n in names)

        state.update_code_preview = True
        if submission_files.skipped_files:
            state.info_popup += f"- Skipped {len(submission_files.skipped_files)} files in the submission archive that didn't look like submissions:\n"
            state.info_popup += filename_list_md(submission_files.skipped_files)
        if submission_files.unrecognized_files:
            state.info_popup += f"- Failed to recognize type of {len(submission_files.unrecognized_files)} submitted files:\n"
            state.info_popup += filename_list_md(submission_files.unrecognized_files)

        ctx.status = None

RE_VALID_EXCERCISE_TITLE = re.compile(r"^[\w\-. ]+$")

def _task_export_to_feedback_folder(ctx: AppContext) -> None:
    with ctx.lock:
        state, pstate = ctx.state, ctx.persistent_state

        if RE_VALID_EXCERCISE_TITLE.match(state.excercise) is None:
            raise Exception("Invalid excercise title")
        assert state.subms is not None

        ctx.status = app.Status("Choosing destination")
        start_dir = os.path.dirname(state.last_exported_folder) if state.last_exported_folder != "" else None

    folder_name = crossfiledialog.choose_folder("Choose output folder", start_dir)  # ty: ignore[invalid-argument-type]

    with ctx.lock:
        if folder_name == "" or folder_name is None:
            ctx.status = None
            return

        tomldata = {
            "excercise": state.excercise,
            "group": state.group,
            "submissions": {},
        }
        num_pdfs = 0
        out_dir = os.path.join(folder_name, f"{state.excercise}_{state.group}")
        subms = copy.deepcopy(state.subms)
        subm_file_cinfos = copy.deepcopy(state.subm_file_cinfos)

    for i, subm in enumerate(subms):
        with ctx.lock:
            typst_prelude_code = TYPST_PRELUDE.replace(
                "{title}", state.excercise).replace(
                "{name}", subm.stud_name).replace(
                "{tutor}", pstate.group_tutor)

        subm_dir = os.path.join(out_dir, subm.folder_name())
        tomldata["submissions"][subm.stud_id] = {
            "stud-name": subm.stud_name,
            "stud-id": subm.stud_id,
            "files": [],
        }
        with ctx.lock:
            ctx.status = app.Status(t"Unpacking files and compiling PDFs ({i}/{len(subms)})", i/len(subms))
        typst_code: list[str] = []
        for finfo in subm_file_cinfos[subm.stud_id]:
            tomldata["submissions"][subm.stud_id]["files"].append({
                "name": finfo.filename,
                "custom-name": finfo.custom_filename,
                "kind": finfo.kind,
            })
            fdata = subm.files[finfo.filename]
            if finfo.kind == "ignore":
                continue

            app.write_file(os.path.join(subm_dir, "files", finfo.custom_filename), fdata)
            if finfo.kind == "embed":
                code = ""
                code += f"== `{finfo.custom_filename}`\n"
                kind = FileKind(finfo.custom_filename)
                if isinstance(fdata, str):
                    if kind.ext == ".py":
                        lang = "py"
                    elif kind.ext == ".txt":
                        lang = "txt"
                    elif kind.is_readme:
                        lang = "md"
                    else:
                        raise Exception(f"cannot embed text file {finfo.custom_filename!r} with unknown type")
                    code += f"#codly.new(```{lang}\n{fdata}\n```)\n"
                else:
                    assert isinstance(fdata, bytes)
                    if kind.ext not in TYPST_IMAGE_FORMATS:
                        raise Exception(f"cannot embed image file {finfo.custom_filename!r} with unknown type")
                    code += f"#image(bytes(({"".join(str(b)+"," for b in fdata)})),\nwidth: 100%, format: \"{TYPST_IMAGE_FORMATS[kind.ext]}\")\n"
                typst_code.append(code)
            else:
                assert finfo.kind == "separate"
                app.write_file(os.path.join(subm_dir, finfo.custom_filename), fdata)
        if len(typst_code):
            code = typst_prelude_code
            code += "#pagebreak()\n".join(typst_code)
            app.write_file(
                os.path.join(subm_dir, f"{subm.stud_name}_{subm.stud_id}.pdf"),
                typst.compile(code.encode("utf-8")))
            num_pdfs += 1
    
    with ctx.lock:
        state.last_exported_folder = out_dir
        app.write_file(os.path.join(out_dir, "eprtool.toml"),
            tomli_w.dumps(tomldata))
        state.info_popup = f"Exported {len(state.subms)} submissions (generated {num_pdfs} PDFs)"
        state.info_popup_show_folder = out_dir
        ctx.status = None

def _task_pack_feedback_zip(ctx: AppContext) -> None:
    state, pstate = ctx.state, ctx.persistent_state

    with ctx.lock:
        ctx.status = app.Status("Choosing feedback folder")
        start_dir = state.last_exported_folder if state.last_exported_folder != "" else None

    folder_name = crossfiledialog.choose_folder("Choose feedback folder",
        start_dir)  # ty: ignore[invalid-argument-type]
    with ctx.lock:
        if folder_name == "" or folder_name is None:
            ctx.status = None
            return

        tomldata = tomllib.loads(app.read_file_str(os.path.join(folder_name, "eprtool.toml")))

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
                        file_path = os.path.join(subm_dir, filename_with_suffix(finfo["custom-name"], pstate.pdf_suffix))
                        if os.path.isfile(file_path):
                            ar.write(file_path,
                                arcname=f"{subm_zip_dir}/Feedback_{excercise}_{subm["stud-name"]}_{finfo["custom-name"].replace("/", "_")}")
                            packed_files += 1
                    else:
                        assert finfo["kind"] == "ignore"
                if have_pdf:
                    total_files += 1
                    file_path = os.path.join(subm_dir, filename_with_suffix(f"{subm["stud-name"]}_{subm["stud-id"]}.pdf", pstate.pdf_suffix))
                    if os.path.isfile(file_path):
                        ar.write(file_path,
                            arcname=f"{subm_zip_dir}/Feedback_{excercise}_{subm["stud-name"]}.pdf")
                        packed_files += 1

        state.info_popup = ""
        if packed_files != total_files:
            state.info_popup += f"> [!WARNING]\n> Found {total_files-packed_files}/{total_files} unannotated PDFs!\n\n"
        state.info_popup += "Successfully added feedback ZIP to folder."
        state.info_popup_show_file = zip_path
        ctx.status = None

def _task_lint_files(ctx: AppContext):
    state, pstate = ctx.state, ctx.persistent_state

    # FIXME: Handle case where new lint task is launched before results completely came in

    with ctx.lock:
        file_data: list[tuple[str, bytes]] = []
        if state.subms is not None and state.subm_idx < len(state.subms):
            subm = state.subms[state.subm_idx]
            cinfos = state.subm_file_cinfos[subm.stud_id]
            for i, finfo in enumerate(cinfos):
                fname = finfo.custom_filename
                fdata = subm.files[finfo.filename]
                if Path(fname).suffix == ".py" and isinstance(fdata, str):
                    file_data.append((fname, fdata.encode(errors="ignore")))
        cfg = stylecheck.Config(pstate.style_max_line_length)

    for i, (fname, data) in enumerate(file_data):
        #with ctx.lock:
            #ctx.status = app.Status("Linting", i/len(subm.files))
        msgs = stylecheck.check(fname, data, cfg)
        with ctx.lock:
            state.codeviewer.set_file_messages(fname, msgs)

def draw_gui(ctx: AppContext) -> None:
    state = ctx.state

    if imgui.button("Open submission ZIP"):
        ctx.tpe.submit(_task_open_submission_or_submissions, ctx, None)
    imgui.same_line()
    imgui.begin_disabled(state.subms is None)
    if imgui.button("Export to feedback folder"):
        ctx.tpe.submit(_task_export_to_feedback_folder, ctx)
    if state.subms is None:
        imgui.set_item_tooltip("You must open a submission ZIP first")
    imgui.end_disabled()
    imgui.same_line()
    if imgui.button("Pack feedback ZIP"):
        ctx.tpe.submit(_task_pack_feedback_zip, ctx)
    imgui.same_line()
    if imgui.button("?"):
        imgui.open_popup("Help")
    imgui.set_item_tooltip("Help")

    if imgui.tree_node("Settings"):
        ctx.draw_settings()
        if imgui.button("Reset UI dock layout"):
            state.force_rebuild_dock_layout = True
        imgui.tree_pop()
        imgui.spacing()

    if state.subms is not None:
        ui.object_field_input_text("Excercise", state, "excercise", "e.g. Uebung 0")
        ui.object_field_input_text("Group", state, "group", "e.g. EPR01")

    dock_id = imgui.get_id("MainDock")
    if state.force_rebuild_dock_layout or imgui.internal.dock_builder_get_node(dock_id) is None:
        imgui.internal.dock_builder_add_node(dock_id)
        main_left = imgui.internal.dock_builder_split_node(dock_id, imgui.Dir.left, 0.4)

        sp = imgui.internal.dock_builder_split_node(main_left.id_at_dir, imgui.Dir.up, 0.5)
        imgui.internal.dock_builder_dock_window("Submissions", sp.id_at_dir)
        imgui.internal.dock_builder_dock_window("Contents", sp.id_at_opposite_dir)

        sp = imgui.internal.dock_builder_split_node(main_left.id_at_opposite_dir, imgui.Dir.up, 0.7)
        imgui.internal.dock_builder_dock_window("Code preview", sp.id_at_dir)
        imgui.internal.dock_builder_dock_window("Code messages", sp.id_at_opposite_dir)

        imgui.internal.dock_builder_finish(dock_id)
        state.force_rebuild_dock_layout = False
    imgui.dock_space(dock_id)

    imgui.begin("Submissions")
    if state.subms is not None:
        imgui.separator_text(f"{len(state.subms)} submissions")
        for i, s in enumerate(state.subms):
            if imgui.selectable(f"{s.stud_name} ({s.stud_id})", i == state.subm_idx, imgui.SelectableFlags_.select_on_nav)[0]:
                state.subm_idx = i
                state.editing_file_info_idx = None
                state.update_code_preview = True

        imgui.separator_text("Add manual submission")
        imgui.set_next_item_width(-imgui.calc_text_size("Submitter name").x)
        ui.object_field_input_text("Submitter name", state, "manual_subm_name")
        imgui.begin_disabled(state.manual_subm_name == "")
        if imgui.button("Select submission file"):
            ctx.tpe.submit(_task_open_submission_or_submissions, ctx, state.manual_subm_name)
        imgui.end_disabled()
    else:
        rich_md.push_selectable_text(False)
        rich_md.render("*No submission archive opened*")
        rich_md.pop_selectable_text()
    imgui.end()

    if state.update_code_preview and state.subms is not None and state.subm_idx < len(state.subms):
        subm = state.subms[state.subm_idx]
        cinfos = state.subm_file_cinfos[subm.stud_id]
        files = (
            (finfo.custom_filename, subm.files[finfo.filename])
                for finfo in cinfos
        )
        state.codeviewer.clear()
        for fname, data in files:
            if isinstance(data, str) and Path(fname).suffix == ".py":
                state.codeviewer.set_file(fname, data)
        ctx.tpe.submit(_task_lint_files, ctx)
        state.update_code_preview = False

    imgui.begin("Contents")
    if state.subms is not None and state.subm_idx < len(state.subms):
        subm = state.subms[state.subm_idx]
        cinfos = state.subm_file_cinfos[subm.stud_id]
        imgui.push_text_wrap_pos()
        imgui.text(f"Submitter: {subm.stud_name} ({s.stud_id})")
        if subm.is_manually_added:
            subm_filename = subm.filename
        else:
            subm_filename = "/".join(subm.filename.split("/")[1:])
        imgui.text(f"Submission filename: {subm_filename}")
        if subm.is_manually_added:  # noqa: SIM102
            if imgui.button("Delete"):
                imgui.open_popup("Delete submission?")
        imgui.separator_text("Files")
        imgui.push_item_flag(imgui.ItemFlags_.allow_duplicate_id, True)
        for i, info in enumerate(cinfos):
            if state.editing_file_info_idx != i:
                imgui.selectable(f"{info.custom_filename} [{info.kind}]", False)
                if imgui.is_item_clicked():
                    state.codeviewer.select_file(info.custom_filename)

                if ui.is_item_double_clicked():
                    state.editing_file_info_idx = i
                    state.focus_editing_file_info = True
                imgui.set_item_tooltip(f"Drag to reorder\nDouble-click to edit\nOriginal filename: {info.filename}")
                # Hacky-ass dragging code pulled from dearimgui demo. Might break
                # when dragging too quickly, but it seems to work well enough.
                if imgui.is_item_active() and not imgui.is_item_hovered():
                    i_next = i + (-1 if imgui.get_mouse_drag_delta(imgui.MouseButton_.left)[1] < 0.0 else 1)
                    if 0 <= i_next < len(cinfos):
                        cinfos[i], cinfos[i_next] = \
                            cinfos[i_next], cinfos[i]
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

        imgui.set_next_window_pos(imgui.get_main_viewport().get_center(), imgui.Cond_.always, (0.5, 0.5))
        if imgui.begin_popup_modal("Delete submission?", None, imgui.WindowFlags_.always_auto_resize)[0]:
            imgui.text(f"Delete submission for {subm.stud_name} ({subm.stud_id})")
            if imgui.button("Delete", (120, 0)):
                state.subms.pop(state.subm_idx)
                imgui.close_current_popup()
            imgui.set_item_default_focus()
            imgui.same_line()
            if imgui.button("Cancel", (120, 0)):
                imgui.close_current_popup()
            imgui.end_popup()
    imgui.end()

    imgui.begin("Code preview")
    if state.codeviewer.num_files():
        imgui.push_font(app.get_font_mono(), 0)
        imgui.text(f"File: {state.codeviewer.get_current_file()}")
        state.codeviewer.draw("CodePreview")
        imgui.pop_font()
    else:
        ui.markdown("*No file opened for preview*")
    imgui.end()

    imgui.begin("Code messages")
    if state.codeviewer.num_files():
        state.codeviewer.draw_messages("CodeMessages")
    else:
        ui.markdown("*No linted files*")
    imgui.end()

    if ui.begin_modal_box("Help", (600, 500)):
        rich_md.render(state.help_markdown)
        ui.end_modal_box()

    if state.info_popup:
        ui.ensure_popup_open("Info")
    if ui.begin_modal_box("Info", (300, 400)):
        rich_md.render(state.info_popup)
        button_labels = []
        if state.info_popup_show_file:
            button_labels.append("Show in file manager")
        if state.info_popup_show_folder:
            button_labels.append("Open folder")
        match ui.end_modal_box(button_labels):
            case "Show in file manager":
                show_in_file_manager(state.info_popup_show_file)
            case "Open folder":
                show_in_file_manager(state.info_popup_show_folder)
            case "Close":
                state.info_popup = ""
                state.info_popup_show_file = ""
                state.info_popup_show_folder = ""

def main() -> None:
    DEBUG_SAVE_STATE = False

    save_state = None
    def setup(ctx: app.MainContext) -> None:
        ctx.persistent_state = PersistentState()
        ctx.state = State(
            help_markdown=app.read_file_str(str(app.get_res_dir()/"HELP.md")),
            codeviewer=stylecheck_ui.CodeViewer()
        )
        if DEBUG_SAVE_STATE:
            global save_state
            save_state = ctx.state
            print("loading saved state")
            try:
                with open("state.pkl", "rb") as f:
                    save_state = pickle.load(f)
                    ctx.state = save_state
                    assert isinstance(save_state, State)
            except FileNotFoundError:
                pass

    app.run(setup, draw_gui, "EPR Tool", "eprtool")

    if DEBUG_SAVE_STATE:
        print("saving state")
        with open("state.pkl", "wb") as f:
            pickle.dump(save_state, f)

if __name__ == "__main__":
    main()