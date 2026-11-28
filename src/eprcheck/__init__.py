import difflib
import os
import queue
from dataclasses import dataclass, field
from pathlib import Path, PurePath

import crossfiledialog
import watchdog.events
import watchdog.observers
import watchdog.utils.event_debouncer
from imgui_bundle import imgui
from imgui_bundle import imgui_color_text_edit as imgui_cte

from common import (
    IGNORED_DIRS,
    IGNORED_FILES,
    StyleSettings,
    app,
    stylecheck,
    stylecheck_ui,
    ui,
)


class _FileEventHandler(watchdog.events.FileSystemEventHandler):
    def __init__(self):
        super().__init__()
        self.queue = queue.SimpleQueue[watchdog.events.FileSystemEvent]()

    def on_any_event(self, event: watchdog.events.FileSystemEvent) -> None:
        self.queue.put(event)

    def drain_events(self) -> list[watchdog.events.FileSystemEvent]:
        res = []
        while True:
            try:
                res.append(self.queue.get_nowait())
            except queue.Empty:
                break
        return res

@dataclass
class State:
    codeviewer: stylecheck_ui.CodeViewer
    file_event_observer: watchdog.observers.api.BaseObserver
    file_event_handler: _FileEventHandler | None = None

    force_rebuild_dock_layout = False

    folder_newly_selected: bool = False
    curr_folder: Path | None = None
    curr_file_contents: dict[str, str] = field(default_factory=dict)  # normalized filename to data; used for diffing

    info_popup: str = ""
    info_popup_show_file: str = ""
    info_popup_show_folder: str = ""

def norm_filename(path: str | Path, project_root: str | Path) -> str:
    return PurePath(Path(path).relative_to(project_root)).as_posix()

@dataclass
class PersistentState(StyleSettings, app.AutoPersistentState):
    last_opened_folders: list[str] = field(default_factory=list)

type AppContext = app.MainContext[State, PersistentState]

def _task_open_folder(ctx: AppContext):
    with ctx.lock:
        state = ctx.state
        pstate = ctx.persistent_state

    with ctx.lock:
        ctx.status = app.Status("Selecting folder")
        if len(pstate.last_opened_folders) and os.path.isdir(pstate.last_opened_folders[-1]):
            start_dir = pstate.last_opened_folders[-1]
        else:
            start_dir = None

    folder_name = crossfiledialog.choose_folder("Choose code folder", start_dir)  # ty: ignore[invalid-argument-type]
    if not folder_name:
        ctx.status = None
        return

    with ctx.lock:
        try:
            pstate.last_opened_folders.remove(folder_name)
        except ValueError:
            pass
        pstate.last_opened_folders.append(folder_name)
        ctx.persistent_state_signal_changed()
        state.curr_folder = Path(folder_name)
        state.folder_newly_selected = True
    ctx.status = None

def _task_lint_files(ctx: AppContext, files: list[Path]) -> None:
    with ctx.lock:
        state = ctx.state
        pstate = ctx.persistent_state
        if state.curr_folder is None:
            return
        project_root = state.curr_folder
        cfg = stylecheck.Config(pstate.style_max_line_length,
            pstate.style_require_english_docstrings)

    for f in files:
        with ctx.lock:
            data = app.read_file_bytes(f)
            rel_path = norm_filename(f, project_root)
        msgs = stylecheck.check(rel_path, data, cfg, str(project_root))
        with ctx.lock:
            state.codeviewer.set_file_messages(rel_path, msgs)

def draw_gui(ctx: AppContext) -> None:
    state = ctx.state

    if imgui.button("Select folder"):
        ctx.tpe.submit(_task_open_folder, ctx)
    imgui.same_line()
    if imgui.button("Settings"):
        imgui.open_popup("Settings")
    if imgui.begin_popup("Settings"):
        ctx.draw_settings()
        imgui.end_popup()

    dock_id = imgui.get_id("MainDock")
    if state.force_rebuild_dock_layout or imgui.internal.dock_builder_get_node(dock_id) is None:
        imgui.internal.dock_builder_add_node(dock_id)
        main_up = imgui.internal.dock_builder_split_node(dock_id, imgui.Dir.up, 0.7)

        top_left = imgui.internal.dock_builder_split_node(main_up.id_at_dir, imgui.Dir.left, 0.3)
        imgui.internal.dock_builder_dock_window("Browser", top_left.id_at_dir)
        imgui.internal.dock_builder_dock_window("Code", top_left.id_at_opposite_dir)

        imgui.internal.dock_builder_dock_window("Messages", main_up.id_at_opposite_dir)

        imgui.internal.dock_builder_finish(dock_id)
        state.force_rebuild_dock_layout = False
    imgui.dock_space(dock_id)

    # TODO: This may cause duplicate linting (see comment on stylecheck._g_lock).
    if ctx.persistent_state_just_changed and state.curr_folder is not None:
        ctx.tpe.submit(_task_lint_files, ctx,
            [Path(state.curr_folder)/fname for fname in state.codeviewer.get_files()])

    if state.folder_newly_selected and state.curr_folder is not None:
        state.curr_file_contents = {}
        state.codeviewer.clear()
        py_files = []
        for dirpath, dirs, files in os.walk(state.curr_folder):
            dirs[:] = [n for n in dirs if n not in IGNORED_DIRS]
            files[:] = [n for n in files if n not in IGNORED_FILES]
            for f in files:
                if os.path.splitext(f)[1].lower() == ".py":
                    py_files.append(Path(dirpath)/f)
        for f in py_files:
            name, data = norm_filename(f, state.curr_folder), app.read_file_str(f)
            state.curr_file_contents[name] = data
            state.codeviewer.set_file(name, data)
        state.file_event_observer.unschedule_all()
        if state.file_event_handler is not None:
            del state.file_event_handler
        state.file_event_handler = _FileEventHandler()
        state.file_event_observer.schedule(state.file_event_handler, state.curr_folder, recursive=True)
        ctx.tpe.submit(_task_lint_files, ctx, py_files)
    state.folder_newly_selected = False

    imgui.begin("Browser")
    state.codeviewer.draw_file_tree("Tree")
    imgui.end()

    imgui.begin("Code")
    imgui.push_font(app.get_font_mono(), 0)
    state.codeviewer.draw("SourceCode")
    imgui.pop_font()
    imgui.end()

    imgui.begin("Messages")
    state.codeviewer.draw_messages("Messages")
    imgui.end()

    if state.file_event_handler is not None:
        evts = state.file_event_handler.drain_events()
        if state.curr_folder is not None:
            # It seems events like to get duplicated (or even triplicated)
            # on Windows. We don't want that, so we use sets.
            to_reload = set()
            deleted = set()
            for evt in evts:
                if evt.is_directory:
                    continue
                src = evt.src_path
                if isinstance(src, bytes):
                    src = src.decode()
                src = Path(src)
                dest = evt.dest_path
                if isinstance(dest, bytes):
                    dest = dest.decode()
                dest = Path(dest)
                match evt.event_type:
                    case "created" | "modified":
                        to_reload.add(src)
                    case "moved":
                        if src.is_relative_to(state.curr_folder):
                            deleted.add(src)
                        if dest.is_relative_to(state.curr_folder):
                            to_reload.add(dest)
                    case "deleted":
                        deleted.add(src)
            to_reload -= deleted
            to_reload = sorted(to_reload)
            deleted = sorted(deleted)
            for f in deleted:
                name = norm_filename(f, state.curr_folder)
                state.curr_file_contents.pop(name, None)
                state.codeviewer.set_file(name, None)
            for f in to_reload:
                name, data = norm_filename(f, state.curr_folder), app.read_file_str(f)
                old_data = state.curr_file_contents.get(name, None)
                state.curr_file_contents[name] = data
                state.codeviewer.set_file(name, data)

                if len(to_reload) == 1:
                    # Go to most recent change in single reloaded file.
                    state.codeviewer.select_file(norm_filename(to_reload[0], state.curr_folder))
                    if old_data is not None:
                        # Find last modification in diff; go there.
                        ops = difflib.SequenceMatcher(None, old_data, data).get_opcodes()
                        ops = [op for op in ops if op[0] != "equal"]
                        if len(ops):
                            op = ops[0]
                            pos = op[4] # first change end offset
                            pos = min(pos, len(data))
                            line_idx = data[:pos].count("\n")
                            state.codeviewer.te.set_cursor(imgui_cte.TextEditor.DocPos(line_idx+1, 0))
            if len(to_reload):
                ctx.tpe.submit(_task_lint_files, ctx, to_reload)

def main() -> None:
    observer = None
    def setup(ctx: AppContext) -> None:
        nonlocal observer
        observer = watchdog.observers.Observer()
        observer.start()

        state = State(
            stylecheck_ui.CodeViewer(),
            observer,
        )
        pstate = PersistentState()
        ctx.state = state
        ctx.persistent_state = pstate

    def pstate_loaded(ctx: AppContext) -> None:
        state = ctx.state
        pstate = ctx.persistent_state
        if len(pstate.last_opened_folders):
            open_folder = Path(pstate.last_opened_folders[-1])
            if open_folder.is_dir():
                state.curr_folder = open_folder
                state.folder_newly_selected = True
            else:
                pstate.last_opened_folders.pop(-1)

    app.run(setup, draw_gui, "EPR Checker", "eprchecker",
        persistent_state_loaded=pstate_loaded)

    assert isinstance(observer, watchdog.observers.api.BaseObserver)
    observer.stop()
    observer.join()

if __name__ == "__main__":
    main()