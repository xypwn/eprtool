import copy
import glob
import mimetypes
import os
import pickle
import re
import typing
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import chardet
import crossfiledialog
import typst
from imgui_bundle import imgui, rich_md
from showinfm import show_in_file_manager

from common import (
    IGNORED_DIRS,
    IGNORED_FILES,
    StyleSettings,
    app,
    stylecheck,
    stylecheck_ui,
    ui,
)

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

TYPST_IMAGE_FORMATS = {
    ".png": "png",
    ".jpg": "jpg",
    ".jpeg": "jpg",
    ".svg": "svg",
    ".webp": "webp",
    #".pdf": "pdf", # won't consider PDF here, since that has a bunch of problems
}

@dataclass
class SingleSubmission:
    stud_name: str
    stud_id: str # submission-local student Moodle ID, not actual ID
    filename: str
    files: dict[str, str|bytes]
    is_manually_added: bool = False

    def stud_key(self) -> str:
        return f"{self.stud_name}_{self.stud_id}"

RE_EXPORTED_SUBM_FOLDER = re.compile(r"^(?P<name>.+)_(?P<id>\d+)$")
RE_SUBM_FILE = re.compile(r"^(?P<name>.+)_(?P<id>\d+)_assignsubmission_file/(?P<file>[^/]+)$")

@dataclass
class SubmissionFilesResult:
    submissions: list[SingleSubmission] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)

@dataclass
class FileKind:
    """Kind of file in the context of the tool
    """
    def __init__(self, filename: str):
        ext = os.path.splitext(filename)[1].lower()
        self.is_text = False
        if mimetypes.types_map.get(ext, "").startswith("text/"):
            self.is_text = True
        self.ext = ext
        self.is_documentation_text = ext in (".md", ".txt")
        if self.is_documentation_text:
            self.is_text = True
        self.is_image = ext in TYPST_IMAGE_FORMATS
        self.is_recognized = (self.is_documentation_text or
                ext in (".py", ".pdf") or
                self.is_image)

    ext: str
    is_text: bool
    is_documentation_text: bool
    is_image: bool
    is_recognized: bool

@dataclass
class ManualSubmInfo:
    stud_name: str = ""
    stud_id: str = ""

# Submission may be ZIP or single file
def submission_files_from_zip_or_file(filename: str, manual_subm: ManualSubmInfo | None = None) -> SubmissionFilesResult:
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

    if manual_subm is not None:
        with open(filename, "rb") as f:
            fname = os.path.basename(filename)
            result.submissions.append(SingleSubmission(
                manual_subm.stud_name,
                manual_subm.stud_id,
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
                m = RE_SUBM_FILE.match(fname)
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
class Settings:
    group_tutor: str = field(default="",
        metadata={"label": "Group and tutor", "hint": "e.g. EPR01 - Firstname"})
    include_feedback_files: str = field(default="*.pdf,*.xslx,*.ods",
        metadata={"label": "Incuded feedback files", "tooltip": "Comma-separated globs (e.g. *.pdf,*.xslx,*.ods)"})

@dataclass
class PersistentState(StyleSettings, Settings, app.AutoPersistentState):
    last_export_folder: str = ""

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

    manual_subm: ManualSubmInfo = field(default_factory=ManualSubmInfo)

    update_code_preview: bool = False

    editing_file_info_idx: int | None = None
    focus_editing_file_info: bool = False

    info_popup: str = ""
    info_popup_show_file: str = ""
    info_popup_show_folder: str = ""

    help_markdown: str = "# Help"

type AppContext = app.MainContext[State, PersistentState]

RE_GUESS_EXCERCISE_GROUP = re.compile(r"^[\w ]+?\d+[\w -]+?(?P<ex>\d+)[\w -]+?(?P<gr>\d+)", re.UNICODE)

def _task_open_submission_or_submissions(ctx: AppContext, manual_subm: ManualSubmInfo | None = None) -> None:
    with ctx.lock:
        state = ctx.state
        ctx.status = app.Status("Choosing file")
        start_dir = ctx.dirs.user_downloads_dir

    single_subm_file = manual_subm is not None

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

        submission_files = submission_files_from_zip_or_file(fname, manual_subm)
        if not single_subm_file:
            # If we got a submission archive, we want to replace old
            # submissions.
            state.subms = None
            state.subm_file_cinfos = {}
        if state.subms is None:
            state.subms = []
        state.subms += submission_files.submissions
        state.subms.sort(key=lambda s: s.stud_name)
        unrecognized_files = []

        if single_subm_file:
            state.subm_idx = state.subms.index(submission_files.submissions[0])
        else:
            state.subm_idx = 0

        for subm in submission_files.submissions:
            state.subm_file_cinfos[subm.stud_id] = []
            for fname in sorted(subm.files.keys()):
                kind = FileKind(fname)
                if not kind.is_recognized:
                    unrecognized_files.append(f"{subm.stud_name}/{fname}")
                subm_kind = "ignore"
                if kind.ext == ".py" or kind.is_documentation_text or kind.is_image:
                    subm_kind = "embed"
                elif kind.ext == ".pdf":
                    subm_kind = "separate"
                state.subm_file_cinfos[subm.stud_id].append(SubmissionFileInfo(
                    fname,
                    fname,
                    subm_kind,
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
        if unrecognized_files:
            state.info_popup += f"- Failed to recognize type of {len(unrecognized_files)} submitted files:\n"
            state.info_popup += filename_list_md(unrecognized_files)

        ctx.status = None

RE_VALID_EXCERCISE_TITLE = re.compile(r"^[\w\-. ]+$")

def _task_export_to_feedback_folder(ctx: AppContext) -> None:
    with ctx.lock:
        state, pstate = ctx.state, ctx.persistent_state

        if RE_VALID_EXCERCISE_TITLE.match(state.excercise) is None:
            raise Exception("Invalid excercise title")
        assert state.subms is not None

        ctx.status = app.Status("Choosing destination")
        start_dir = pstate.last_export_folder or None

    folder_name = crossfiledialog.choose_folder("Choose output folder", start_dir)  # ty: ignore[invalid-argument-type]

    with ctx.lock:
        if folder_name == "" or folder_name is None:
            ctx.status = None
            return

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

        subm_dir = os.path.join(out_dir, subm.stud_key())
        with ctx.lock:
            ctx.status = app.Status(t"Unpacking files and compiling PDFs ({i}/{len(subms)})", i/len(subms))
        typst_code: list[str] = []
        for finfo in subm_file_cinfos[subm.stud_id]:
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
                    elif kind.is_documentation_text:
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
            code += '#v(4cm)\n#""\n' # ensure we have 4cm of space for final remarks

            app.write_file(
                os.path.join(subm_dir, f"{subm.stud_name}_{subm.stud_id}.pdf"),
                typst.compile(code.encode("utf-8")))
            num_pdfs += 1
    
    with ctx.lock:
        pstate.last_export_folder = folder_name
        state.info_popup = f"Exported {len(state.subms)} submissions (generated {num_pdfs} PDFs)"
        state.info_popup_show_folder = out_dir
        ctx.status = None

def _task_pack_feedback_zip(ctx: AppContext) -> None:
    with ctx.lock:
        state, pstate = ctx.state, ctx.persistent_state

        ctx.status = app.Status("Choosing feedback folder")
        start_dir = pstate.last_export_folder or None

    folder_path = crossfiledialog.choose_folder("Choose feedback folder",
        start_dir)  # ty: ignore[invalid-argument-type]
    with ctx.lock:
        if folder_path == "" or folder_path is None:
            ctx.status = None
            return

        re_incl_feedback_files = re.compile(
            "|".join(glob.translate(g) for g in pstate.include_feedback_files.split(",")),
            flags=re.IGNORECASE|re.UNICODE,
        )

        folder_path = Path(folder_path)
        zip_path = folder_path / (folder_path.absolute().name+"_annotated.zip")

        packed_files = 0
        unannotated_pdfs = []

        with zipfile.ZipFile(zip_path, "w") as ar:
            ctx.status = app.Status("Creating archive")

            for subm_path in folder_path.iterdir():
                m = RE_EXPORTED_SUBM_FOLDER.match(subm_path.name)
                if m is None:
                    continue

                stud_name, stud_id = m["name"], m["id"]
                subm_dir_in_zip = f"{stud_name}_{stud_id}_assignsubmission_file"
                feedback_pfx = f"Feedback_{folder_path.absolute().name}_{stud_name}"

                code_pdf_name = f"{stud_name}_{stud_id}.pdf"

                missing_annotated_version: set[str] = {code_pdf_name}

                for p in (subm_path/"files").iterdir():
                    if (subm_path/p.name).is_file():
                        # In subm dir and "files/" -> handed-in PDF
                        # that must be annotated.
                        missing_annotated_version.add(p.name)

                for fpath in subm_path.iterdir():
                    if not fpath.is_file():
                        continue
                    if not re_incl_feedback_files.match(fpath.name):
                        continue
                    if fpath.name in missing_annotated_version:
                        # We want the annotated version, not the base version.
                        continue
                    unannot_name = None
                    for sfx in ("_kommentiert", "_annotated"):
                        if fpath.stem.endswith(sfx):
                            unannot_name = fpath.stem[:-len(sfx)] + fpath.suffix
                            missing_annotated_version.discard(unannot_name)
                            break
                    if unannot_name == code_pdf_name:
                        ar_name = f"{feedback_pfx}.pdf"
                    else:
                        ar_name = f"{feedback_pfx}_{fpath.name}"
                    ar.write(fpath, arcname=f"{subm_dir_in_zip}/{ar_name}")
                    packed_files += 1
                
                unannotated_pdfs += [f"{subm_path.name}/{p}" for p in missing_annotated_version]

        state.info_popup = ""
        if len(unannotated_pdfs):
            state.info_popup += f"> [!WARNING]\n> Found {len(unannotated_pdfs)} unannotated PDFs:\n"
            state.info_popup += "".join(f"> - `{p}`\n" for p in unannotated_pdfs)
            state.info_popup += "\n"
        state.info_popup += f"Successfully created feedback ZIP containing {packed_files} files in folder."
        state.info_popup_show_file = str(zip_path)
        ctx.status = None

def _task_lint_files(ctx: AppContext):
    with ctx.lock:
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

    if imgui.button("Help"):
        imgui.open_popup("Help")
    imgui.set_item_tooltip("Help")
    imgui.same_line()
    if imgui.button("Settings"):
        imgui.open_popup("Settings")
    if imgui.begin_popup("Settings"):
        ctx.draw_settings()
        if imgui.button("Reset UI dock layout"):
            state.force_rebuild_dock_layout = True
        imgui.end_popup()

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
        ui.object_field_input_text("Submitter name", state.manual_subm, "stud_name", right_align_label=True)
        imgui.set_item_tooltip("Submitter's full name")
        ui.object_field_input_text("Submitter ID", state.manual_subm, "stud_id", flags=imgui.InputTextFlags_.chars_decimal, right_align_label=True)
        imgui.set_item_tooltip('Unique Moodle per-submission ID\nCan be found via "Download grading worksheet" in the "ID" column (just the number)')
        imgui.begin_disabled(state.manual_subm.stud_name == "" or state.manual_subm.stud_id == "")
        if imgui.button("Select submission file"):
            ctx.tpe.submit(_task_open_submission_or_submissions, ctx, state.manual_subm)
        imgui.end_disabled()
    else:
        rich_md.push_selectable_text(False)
        rich_md.render("*No submission archive opened*")
        rich_md.pop_selectable_text()
    imgui.end()

    if ctx.persistent_state_just_changed:
        state.update_code_preview = True
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
            help_markdown=app.read_file_str(str(app.get_res_dir()/"EPRTOOL_HELP.md")),
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