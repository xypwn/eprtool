import os
import queue
import typing
from collections import deque
from dataclasses import dataclass, field

import crossfiledialog
import watchdog.events
import watchdog.observers
import watchdog.utils.event_debouncer
from imgui_bundle import imgui
from imgui_bundle import imgui_color_text_edit as imgui_cte

from eprtool import app, stylecheck, stylecheck_ui, ui


class _FileEventHandler(watchdog.events.FileSystemEventHandler):
    def __init__(self):
        super().__init__()
        self.queue = queue.SimpleQueue[watchdog.events.FileSystemEvent]()

    def on_any_event(self, event: watchdog.events.FileSystemEvent) -> None:
        self.queue.put(event)

    def drain_events(self) -> list[watchdog.events.FileSystemEvent]:
        # It seems events like to get duplicated (or even triplicated)
        # on Windows. We don't want that!
        res = []
        seen = set()
        while True:
            try:
                evt = self.queue.get_nowait()
                if evt not in seen:
                    res.append(evt)
                    seen.add(evt)
            except queue.Empty:
                break
        return res

@dataclass
class State:
    codeviewer: stylecheck_ui.CodeViewer
    file_event_observer: watchdog.observers.api.BaseObserver
    file_event_handler: _FileEventHandler

    force_rebuild_dock_layout = False

    folder_newly_selected: bool = False
    curr_folder: str = ""

    info_popup: str = ""
    info_popup_show_file: str = ""
    info_popup_show_folder: str = ""

@dataclass
class PersistentState:
    last_opened_folders: list[str] = field(default_factory=list)

type AppContext = app.MainContext[State, PersistentState]

def _task_open_folder(ctx: AppContext):
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
        pstate.last_opened_folders.append(folder_name)
        ctx.persistent_state_signal_changed()
        state.curr_folder = folder_name
        state.folder_newly_selected = True
    ctx.status = None

def draw_gui(ctx: AppContext) -> None:
    state = ctx.state

    if imgui.button("Select folder"):
        ctx.tpe.submit(_task_open_folder, ctx)

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

    if state.folder_newly_selected:
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

    evts = state.file_event_handler.drain_events()
    for evt in evts:
        print(evt)
        if not evt.is_directory and evt.event_type in ("modified", "created", "deleted"):
            #print(evt)
            pass

def main() -> None:
    observer = None
    def setup(ctx: app.MainContext) -> None:
        event_handler = _FileEventHandler()
        nonlocal observer
        observer = watchdog.observers.Observer()
        observer.schedule(event_handler, ".", recursive=True)
        observer.start()

        state = State(
            stylecheck_ui.CodeViewer(),
            observer,
            event_handler,
        )
        pstate = PersistentState()
        ctx.state = state
        ctx.persistent_state = pstate

        state.codeviewer.set_file("hello/hello/test.py", """print("hello world hello")""")
        state.codeviewer.set_file("hello/test2.py", """print("hello world 2")""")
        state.codeviewer.set_file("test4.py", """print("hello world 4")""")
        state.codeviewer.set_file("hello/hello/test1.py", """print("hello world 1")""")

    app.run(setup, draw_gui, "EPR Checker", "eprchecker")

    assert isinstance(observer, watchdog.observers.api.BaseObserver)
    observer.stop()
    observer.join()

if __name__ == "__main__":
    main()