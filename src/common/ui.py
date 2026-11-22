"""Application-specific UI utilities"""

import typing
from string import templatelib

from imgui_bundle import imgui, rich_md


def is_item_double_clicked(mouse_button: imgui.MouseButton = imgui.MouseButton_.left) -> bool:
    return imgui.is_item_clicked(mouse_button) and imgui.is_mouse_double_clicked(mouse_button)

def object_field_input_text(label: str, obj: object, obj_field: str, hint: str = "", flags: imgui.InputTextFlags = imgui.InputTextFlags_.none) -> bool:
    s = obj.__dict__[obj_field]
    changed, s_new = imgui.input_text_with_hint(label, hint, s, flags)
    if changed:
        obj.__dict__[obj_field] = s_new
    return changed

def align_button_row(labels: typing.Iterable[str], align: typing.Literal["left", "right", "center"]) -> None:
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

def wrapping_clickable(label: str, *, on_nav_is_clicked: bool = False) -> bool:
    """Somewhat of a mix of button and selectable for wrapping text
    """
    imgui.push_id(label)
    style = imgui.get_style()
    spacing = style.item_spacing/2
    imgui.push_style_var_y(imgui.StyleVar_.item_spacing, 0.0)
    imgui.push_style_var(imgui.StyleVar_.frame_rounding, 0)

    def calc_bb(label: str) -> imgui.internal.ImRect:
        avail = imgui.get_content_region_avail()
        pos = imgui.get_cursor_screen_pos()
        size = imgui.calc_text_size(label, wrap_width=avail.x)
        size.x = avail.x
        return imgui.internal.ImRect(
            pos.x - spacing.x,          pos.y,
            pos.x + size.x + spacing.x, pos.y + size.y + 2*spacing.y,
        )

    bb = calc_bb(label) # bounding box
    imgui.set_cursor_screen_pos(bb.min)
    clicked = imgui.invisible_button("##btn", bb.get_size(), imgui.ButtonFlags_.enable_nav)
    if imgui.is_item_hovered():
        bg = imgui.get_color_u32(imgui.Col_.frame_bg_hovered)
    else:
        bg = 0
    
    if on_nav_is_clicked and imgui.get_current_context().nav_just_moved_to_id == imgui.get_item_id():
        clicked = True
    imgui.get_window_draw_list().add_rect_filled(bb.min, bb.max, bg)
    imgui.set_cursor_screen_pos(bb.min + spacing)
    imgui.text_wrapped(label)
    imgui.set_cursor_screen_pos((bb.min.x + spacing.x, bb.max.y))

    imgui.pop_style_var()
    imgui.pop_style_var()
    imgui.pop_id()
    return clicked

def ensure_popup_open(name: str, flags: int = 0) -> None:
    if not imgui.is_popup_open(name, flags):
        imgui.open_popup(name, flags)

def begin_modal_box(name: str, initial_size: tuple[float, float]) -> bool:
    def avail_size_without_button_row() -> tuple[float, float]:
        style = imgui.get_style()
        cw, ch = imgui.get_content_region_avail()
        ch -= imgui.get_font_size() + 2*style.frame_padding[1]
        return cw, ch

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
    align_button_row(labels, "center")
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

def modal_yesno_text(name: str, text: str, yes_text: str = "Yes", no_text: str = "No") -> bool:
    res = False
    imgui.set_next_window_size_constraints((200, 0), (imgui.FLT_MAX, imgui.FLT_MAX))
    imgui.set_next_window_pos(imgui.get_main_viewport().get_center(), imgui.Cond_.always, (0.5, 0.5))
    if imgui.begin_popup_modal(name, None, imgui.WindowFlags_.always_auto_resize|imgui.WindowFlags_.no_move)[0]:
        imgui.text_wrapped(text)
        align_button_row([yes_text, no_text], "center")
        if imgui.button(yes_text):
            imgui.close_current_popup()
            res = True
        imgui.same_line()
        if imgui.button(no_text):
            imgui.close_current_popup()
        imgui.end_popup()
    return res


_MARKDOWN_NEED_ESC = set(r"\`*_{}[]()#+-.! ")

def _template_markdown(tpl: templatelib.Template) -> str:
    r"""
    >>> _template_markdown(t"{"abc()"}")
    'abc\\(\\)'
    >>> _template_markdown(t"`{"abc()"}`")
    '`abc()`'
    >>> _template_markdown(t"`abc{"()"}`")
    '`abc()`'
    """
    # TODO: Handle code blocks correctly
    code = ""
    curr_codeblock = 0
    for item in tpl:
        if isinstance(item, templatelib.Interpolation):
            s = format(templatelib.convert(item.value, item.conversion), item.format_spec)
            for c in s:
                if curr_codeblock == 0 and c in _MARKDOWN_NEED_ESC:
                    code += "\\"
                code += c
        else:
            s = item
            i = 0
            while i < len(s):
                if s[i] == '\\':
                    i += 2
                    continue
                if s[i] == "`":
                    num_backticks = 0
                    while i < len(s) and s[i] == "`":
                        num_backticks += 1
                        i += 1
                    if curr_codeblock == 0:
                        curr_codeblock = num_backticks
                    elif num_backticks == curr_codeblock:
                        curr_codeblock = 0
                    continue
                i += 1
            code += s
    return code

def markdown(src: templatelib.Template|str) -> None:
    if isinstance(src, str):
        md = src
    else:
        md = _template_markdown(src)
    rich_md.render(md)
