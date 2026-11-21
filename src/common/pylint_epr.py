import re
from typing import TYPE_CHECKING

from astroid import nodes
from pylint.checkers import BaseChecker, BaseRawFileChecker
from pylint.interfaces import INFERENCE
from simplemma import LanguageDetector

if TYPE_CHECKING:
    from pylint.lint import PyLinter

RE_AUTHOR_VARIABLE = re.compile(r"^\d+, [\w-]+(, \d+, [\w-]+)*$", re.UNICODE)
RE_AI_USAGE_COMMENT = re.compile(r"^# ?(generated|made|done|written) (with|without) (support of )?AI.*$", re.UNICODE|re.IGNORECASE)
# FIXME: Doesn't detect if string delims match.
# TODO: Decide if accepting any credits is good enough or if the string must contain specific elements.
RE_AI_USAGE_CREDITS = re.compile(r"^__credits__\s+=\s+[\"\'\[\(]", re.UNICODE)
AMBIGUOUS_VARIABLE_NAMES = {"l": "1", "O": "0"}

def is_language_and_not_other(langdet: LanguageDetector, lang: str, text: str) -> tuple[bool, float]:
    props = langdet.proportion_in_each_language(text)
    prop = props.get(lang, 0)
    anti_prop = max(props[k] for k in props if k != lang)
    # constants are arbitrary
    return prop >= 0.9 and not anti_prop >= 0.7, prop-anti_prop

class EprChecker(BaseChecker):
    name = "epr-checker"
    msgs = {  # noqa: RUF012
        "C3301": (
            "Missing __author__ variable",
            "missing-author-variable",
            "Used when __author__ variable is missing.",
        ),
        "C3302": (
            "Duplicate __author__ variable",
            "duplicate-author-variable",
            "Used when __author__ variable is declared more than once.",
        ),
        "C3303": (
            "Incorrect __author__ variable assignment",
            "incorrect-author-variable-assignment",
            "Used when __author__ variable is assigned to a string literal incorrectly.",
        ),
        "C3304": (
            'Malformed __author__ value %r (format should be "1234567, Lastname" or "1234567, Lastname1, 7654321, Lastname2" for multiple authors)',
            "malformed-author-value",
            "Used if __author__ string is formatted incorrectly.",
        ),
        "C3311": (
            "Ambiguous variable name %r (may be confused with %r)",
            "ambiguous-variable-name",
            "Used if a variable has a possibly confusing name.",
        ),
        "C3321": (
            "Likely German docstring (confidence: %.2f)",
            "wrong-language-in-docstring",
            "Used if a docstring is suspected to be written in German.",
        ),
        "C3323": (
            "Likely German string comment (confidence: %.2f)",
            "wrong-language-in-string-comment",
            "Used if a string comment is suspected to be written in German.",
        ),
    }
    options = ()

    def __init__(self, linter: PyLinter | None = None) -> None:
        assert linter is not None
        super().__init__(linter)
        self.langdet = LanguageDetector(("en", "de"))

    def _visit_docnode(self, n: nodes.Const) -> None:
        if not isinstance(n.value, str):
            return
        is_de, confidence = is_language_and_not_other(self.langdet, "de", n.value)
        if is_de:
            self.add_message("wrong-language-in-docstring", node=n, args=(confidence,), confidence=INFERENCE)
    
    def visit_module(self, module: nodes.Module) -> None:
        if module.doc_node is not None:
            self._visit_docnode(module.doc_node)

        found = False
        for n in module.body:
            if not (isinstance(n, nodes.Assign) and
                    any(isinstance(t, nodes.AssignName) and
                        t.name == "__author__" for t in n.targets)):
                continue
            if found:
                self.add_message("duplicate-author-variable", node=n)
                return
            found = True
            if (len(n.targets) != 1 or
                not isinstance(n.targets[0], nodes.AssignName) or
                not isinstance(n.value, nodes.Const) or
                not isinstance(n.value.value, str)):
                self.add_message("incorrect-author-variable-assignment", node=n)
                return
            v = n.value.value
            if RE_AUTHOR_VARIABLE.match(v) is None:
                self.add_message("malformed-author-value", node=n.value, args=(v,))
                return
        if not found:
            self.add_message("missing-author-variable", node=module)
            return

    def visit_classdef(self, n: nodes.ClassDef) -> None:
        if n.doc_node is not None:
            self._visit_docnode(n.doc_node)

    def visit_functiondef(self, n: nodes.FunctionDef) -> None:
        if n.doc_node is not None:
            self._visit_docnode(n.doc_node)

    def visit_assignname(self, n: nodes.AssignName) -> None:
        if n.name in AMBIGUOUS_VARIABLE_NAMES:
            self.add_message("ambiguous-variable-name", node=n, args=(n.name, AMBIGUOUS_VARIABLE_NAMES[n.name]))

    def visit_expr(self, n: nodes.Expr) -> None:
        if (isinstance(n.value, nodes.Const) and
            isinstance(n.value.value, str)):
            is_de, confidence = is_language_and_not_other(self.langdet, "de", n.value.value)
            if is_de:
                self.add_message("wrong-language-in-string-comment", node=n, args=(confidence,), confidence=INFERENCE)


class EprRawFileChecker(BaseRawFileChecker):
    name = "epr-raw-file-checker"
    msgs = {  # noqa: RUF012
        "C3322": (
            "Likely German comment (confidence: %.2f)",
            "wrong-language-in-comment",
            "Used if a comment is suspected to be written in German.",
        ),
        "C3331": (
            "Missing or malformed AI usage information (expected comment or __credits__)",
            "missing-or-malformed-ai-usage",
            "Used if AI usage information could not be detected.",
        ),
    }
    options = ()

    def __init__(self, linter: PyLinter | None = None) -> None:
        assert linter is not None
        super().__init__(linter)
        self.langdet = LanguageDetector(("en", "de"))

    def _visit_comment(self, comment: str, line_num: int, column_num: int, column_num_end: int) -> None:
        is_de, confidence = is_language_and_not_other(self.langdet, "de", comment.strip())
        if is_de:
            self.add_message("wrong-language-in-comment",
                line=line_num, col_offset=column_num, end_lineno=line_num, end_col_offset=column_num_end,
                args=(confidence,), confidence=INFERENCE)

    def process_module(self, node: nodes.Module) -> None:
        # TODO: Untangle AI usage detection and comment parsing
        ai_info_found = False
        curr_str_delim: bytes|None = None

        with node.stream() as strm:
            for line_idx, line in enumerate(strm):
                # AI usage detection
                if not ai_info_found:
                    line_text = line.decode("utf-8", errors="ignore")
                    if RE_AI_USAGE_COMMENT.match(line_text) or RE_AI_USAGE_CREDITS.match(line_text):
                        ai_info_found = True

                # Comment parsing
                if not (b'"' in line or b"'" in line or b"#" in line):
                    continue
                line = line.rstrip(b"\r\n")
                i =  0
                while i < len(line):
                    if line[i] == ord("\\"):
                        i += 2
                        continue
                    if curr_str_delim is not None:
                        if line[i:].startswith(curr_str_delim):
                            i += len(curr_str_delim)
                            curr_str_delim = None
                        else:
                            i += 1
                        continue
                    str_delim = None
                    if line[i:].startswith(b'"""'):
                        str_delim = b'"""'
                    elif line[i:].startswith(b"'''"):
                        str_delim = b"'''"
                    elif line[i] == ord('"'):
                        str_delim = b'"'
                    elif line[i] == ord("'"):
                        str_delim = b"'"
                    elif line[i] == ord("#"):  # noqa: SIM102
                        if curr_str_delim is None:
                            self._visit_comment(line[i+1:].decode("utf-8", errors="ignore"), line_idx+1, i+1, len(line))
                    if str_delim is not None:
                        curr_str_delim = str_delim
                        i += len(str_delim)
                    else:
                        i += 1

        if not ai_info_found:
            self.add_message("missing-or-malformed-ai-usage", line=1)

def register(linter: PyLinter) -> None:
    linter.register_checker(EprChecker(linter))
    linter.register_checker(EprRawFileChecker(linter))