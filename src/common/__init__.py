from dataclasses import dataclass, field

IGNORED_DIRS = (
    "__MACOSX",
    ".git",
    ".venv",
    "__pycache__",
    ".idea",
    ".vscode",
)

IGNORED_FILES = (
    ".DS_Store",
)

@dataclass
class StyleSettings:
    style_max_line_length: int = field(default=79,
        metadata={"label": "Style: Max line length", "tooltip": "Maximum line length (pycodestyle E501)\n0 to disable",
        "validate": lambda v: v >= 0})
    style_require_english_docstrings: bool = field(default=True,
        metadata={"label": "Style: Require English docstrings", "tooltip": "Whether to require docstrings to be in English"})
