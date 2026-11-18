"""EPR-conformant style checking
"""

import contextlib
import io
import sys
import typing
from dataclasses import dataclass

import pycodestyle
import pylint.lint
import pylint.message
import pylint.reporters

from eprtool import pylint_epr

_PYLINT_EPR_MESSAGE_IDS = list((pylint_epr.EprChecker.msgs | pylint_epr.EprRawFileChecker.msgs).keys())

_PYLINT_MESSAGE_IDS = _PYLINT_EPR_MESSAGE_IDS+[
    # Conventions
    "C0103", # invalid-name
    "C0112", # empty-docstring
    "C0114", # missing-module-docstring
    "C0115", # missing-class-docstring
    "C0116", # missing-function-docstring
    "C0198", # bad-docstring-quotes
    #"C0199", # docstring-first-line-empty
    #"C0301", # line-too-long
    "C0410", # multiple-imports
    "C0411", # wrong-import-order
    "C0412", # ungrouped-imports
    "C0413", # wrong-import-position
    "C2401", # non-ascii-name
    "C2701", # import-private-name
    "W3201", # bad-dunder-name
    # Errors
    "E0001", # syntax-error
    "E0102", # function-redefined
    "E0211", # no-method-argument
    "E0213", # no-self-argument
    "E2513", # invalid-character-esc
    # Warnings
    "W0104", # pointless-statement
    "W0107", # unnecessary-pass
    "W0201", # attribute-defined-outside-init
    "W0222", # signature-differs
    "W0231", # super-init-not-called
    "W0301", # unnecessary-semicolon
    "W0311", # bad-indentation
    "W0401", # wildcard-import
    "W0404", # reimported
    "W0603", # global-statement
    "W0622", # redefined-builtin
    "W0702", # bare-except
    "W0705", # duplicate-except
    # Fatal
    "F",
]

@dataclass(frozen=True)
class Message:
    """Style checking error message
    """
    filename: str
    line: int
    column: int
    end_line: int | None
    end_column: int | None

    source: typing.Literal["pylint", "pycodestyle"]

    msg_id: str # internal error code (E1234)
    msg: str # readable description

    @classmethod
    def from_pylint(cls, msg: pylint.message.Message) -> "Message":
        return Message(
            msg.path,
            msg.line,
            msg.column + 1,
            msg.end_line,
            msg.end_column + 1 if msg.end_column is not None else None,
            "pylint",
            msg.msg_id,
            msg.msg,
        )

    def __str__(self) -> str:
        return f"{self.source}: {self.filename}:{self.line}:{self.column}: {self.msg_id} {self.msg}"

    def __lt__(self, other: "Message") -> bool:
        # Compare field-by-field, respecing None
        for k in Message.__dataclass_fields__:
            a = self.__dict__[k]
            b = other.__dict__[k]
            if a is None:
                return True
            if b is None:
                return False
            if a < b:
                return True
            if b < a:
                return False
        return False


class _PEP8MessageReport(pycodestyle.BaseReport):
    """pycodestyle report for our Message class
    """
    def __init__(self, options):
        super().__init__(options)
        self.eprmessages: list[Message] = []
        self.override_filename: str | None = None

    def error(self, line_number, offset, text, check):
        code = super().error(line_number, offset, text, check)
        if code:
            self.eprmessages.append(Message(
                self.override_filename or self.filename,
                line_number,
                offset+1,
                None,
                None,
                "pycodestyle",
                code,
                text[5:]
            ))
        return code

@dataclass
class Config:
    max_line_length: int = 79 # 0 to disable

_DEFAULT_CONFIG = Config()

@contextlib.contextmanager
def context_stdin(data: bytes):
    prev_stdin = sys.stdin
    sys.stdin = io.TextIOWrapper(io.BytesIO(data))
    yield
    sys.stdin = prev_stdin

def check(filename: str, file_data: bytes | None = None, cfg: Config = _DEFAULT_CONFIG) -> list[Message]:
    if file_data is None:
        with open(filename, "rb") as f:
            file_data = f.read()

    msgs: list[Message] = []
    with context_stdin(file_data):
        reporter = pylint.reporters.CollectingReporter()
        pylint.lint.Run(("--load-plugins=eprtool.pylint_epr,pylint.extensions.docstyle,pylint.extensions.dunder,pylint.extensions.private_import",
            "--disable=all",
            f"--enable={",".join(_PYLINT_MESSAGE_IDS)}",
            "--from-stdin", # TODO: Support stdin and file paths
            filename),
            reporter=reporter,
            exit=False)
        msgs += [Message.from_pylint(msg) for msg in reporter.messages]

    with context_stdin(file_data):
        ignore = pycodestyle.DEFAULT_IGNORE.split(",")
        if cfg.max_line_length == 0:
            ignore.append("E501")
        style_guide = pycodestyle.StyleGuide(quiet=True,
            max_line_length=cfg.max_line_length,
            ignore=ignore)
        report = style_guide.init_report(_PEP8MessageReport)
        assert isinstance(report, _PEP8MessageReport)
        report.override_filename = filename
        report.filename = filename
        checker = style_guide.checker_class("-", options=style_guide.options)
        checker.check_all()
        msgs += report.eprmessages

    msgs.sort()

    return msgs