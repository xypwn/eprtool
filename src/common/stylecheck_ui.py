import posixpath
import typing

from imgui_bundle import imgui
from imgui_bundle import imgui_color_text_edit as imgui_cte

from common import stylecheck, ui


def message_to_cte_doc_pos(msg: stylecheck.Message) -> tuple[imgui_cte.TextEditor.DocPos, imgui_cte.TextEditor.DocPos]:
    DocPos = imgui_cte.TextEditor.DocPos
    line, column = msg.line - 1, msg.column - 1
    start = DocPos(line, column)
    if msg.end_line is not None:
        end_line = msg.end_line - 1
    else:
        end_line = line
    if msg.end_column is not None:
        end_column = msg.end_column - 1
    else:
        end_column = column + 1
    end = DocPos(end_line, end_column)
    return start, end

def load_msg_annots(te: imgui_cte.TextEditor, msgs: list[stylecheck.Message]) -> None:
    #ERROR_BG = imgui.IM_COL32(220, 50, 50, 56)
    #ERROR_FG = imgui.IM_COL32(220, 50, 50, 255)
    WARN_BG = imgui.IM_COL32(180, 140, 0, 48)
    WARN_FG = imgui.IM_COL32(180, 140, 0, 255)

    line_markers: dict[int, str] = {}
    for msg in msgs:
        # FIXME: When squiggles overlap, they overwrite eachother at the overlapping portion.
        # Ideally, we would like to keep all squiggle tooltips. We could do that by again
        # auto-merging tooltips like for the line markers (in this case it would be more
        # complicated though).
        tooltip = f"{msg.msg} ({msg.source} {msg.msg_id})"
        start, end = message_to_cte_doc_pos(msg)
        squiggle_not_visible = (
            msg.msg_id == "W391" or # blank line at end of file
            msg.msg_id == "W292" or # no newline at end of file
            start.line < 0 or start.index < 0 or
            (start.line == 0 and start.index == 0) # BOF tends to have a lot of annotations and they can't overlap atm (FIXME)
        )
        if not squiggle_not_visible:
            te.add_squiggle(
                start,
                end,
                2,
                WARN_FG,
                tooltip)
        else:
            if start.line not in line_markers:
                line_markers[start.line] = ""
            line_markers[start.line] += tooltip+"\n"
    for line, tooltip in line_markers.items():
        te.add_marker(line,
            0, WARN_BG,
            "warning", tooltip)

class CodeViewer:
    def __init__(self):
        te = imgui_cte.TextEditor()
        te.set_read_only_enabled(True)
        te.set_show_mini_map_enabled(True)
        te.set_language(imgui_cte.TextEditor.Language.python())
        
        self.te: imgui_cte.TextEditor = te
        self._files: dict[str, str] = {}
        self._messages: dict[str, list[stylecheck.Message]] = {}

        self._sel_file: str | None = None

    def clear(self) -> None:
        self._files = {}
        self._messages = {}

    def set_file(self, fname: str, code: str | None) -> None:
        if fname in self._messages:
            self._messages.pop(fname, None)
        if code is None:
            self._files.pop(fname, None)
            return
        self._files[fname] = code
        if fname == self._sel_file:
            self._load_file(fname)

    def get_files(self) -> list[str]:
        return list(self._files.keys())

    def num_files(self) -> int:
        return len(self._files)

    def set_file_messages(self, fname: str, messages: list[stylecheck.Message]) -> None:
        self._messages[fname] = messages
        if fname == self._sel_file:
            self.te.clear_markers()
            self.te.clear_squiggles()
            load_msg_annots(self.te, self._messages[fname])

    def _load_file(self, fname: str | None) -> None:
        if fname in self._files:
            self.te.set_text(self._files[fname])
        else:
            self.te.set_text("")
        if fname in self._messages:
            load_msg_annots(self.te, self._messages[fname])

    def select_file(self, fname: str | None) -> None:
        if self._sel_file == fname:
            return
        self._load_file(fname)
        self._sel_file = fname

    def get_current_file(self) -> str | None:
        return self._sel_file

    def follow_message(self, msg: stylecheck.Message) -> None:
        self.select_file(msg.filename)
        start, end = message_to_cte_doc_pos(msg)
        self.te.set_cursor(start)
        self.te.select_region(start, end)

    def draw(self, str_id: str) -> None:
        imgui.push_id(str_id)
        self.te.render("##CodeView")
        imgui.pop_id()

    def draw_messages(self, str_id: str, on_click: typing.Callable[[stylecheck.Message], None] | None = None) -> None:
        imgui.push_id(str_id)
        imgui.begin_child("##Messages", (0, 0))
        for filename in sorted(self._messages.keys()):
            imgui.push_id(f"Message/{filename}")
            msgs = self._messages[filename]
            imgui.set_next_item_storage_id(imgui.get_id("Node/"+filename))
            if imgui.tree_node(f"{filename} ({len(msgs)})"):
                for i, msg in enumerate(msgs):
                    imgui.push_id(i)
                    label = f"{msg.filename}:{msg.line}:{msg.column} {msg.msg} ({msg.source} {msg.msg_id})"
                    if ui.wrapping_clickable(label, on_nav_is_clicked=True):
                        self.follow_message(msg)
                        if on_click is not None:
                            on_click(msg)
                    imgui.pop_id()
                imgui.tree_pop()
            imgui.pop_id()
        imgui.end_child()
        imgui.pop_id()

    def draw_file_tree(self, str_id: str, on_click: typing.Callable[[str], None] | None = None) -> None:
        imgui.push_id(str_id)
        imgui.push_style_var(imgui.StyleVar_.indent_spacing, 10)
        paths = sorted(posixpath.normpath(f) for f in self._files)
        type Tree = dict[str, Tree] | None
        tree: Tree = {}
        for path in paths:
            elems = path.split("/")
            node = tree
            for i in range(len(elems)):
                assert isinstance(node, dict)
                if i < len(elems)-1:
                    if elems[i] not in node:
                        node[elems[i]] = {}
                    node = node[elems[i]]
                else:
                    node[elems[i]] = None
    
        def draw_tree(node: Tree, curr_path: str) -> None:
            if isinstance(node, dict):
                for name, child in node.items():
                    child_path = name if curr_path == "" else curr_path+"/"+name
                    if child is not None:
                        flags = (
                            imgui.TreeNodeFlags_.draw_lines_to_nodes |
                            imgui.TreeNodeFlags_.default_open |
                            imgui.TreeNodeFlags_.span_avail_width)
                        if imgui.tree_node_ex(name, flags):
                            draw_tree(child, child_path)
                            imgui.tree_pop()
                    else:
                        if imgui.selectable(name, self._sel_file == child_path)[0]:
                            self.select_file(child_path)
                            if on_click is not None:
                                on_click(child_path)

        draw_tree(tree, "")
        imgui.pop_style_var()
        imgui.pop_id()