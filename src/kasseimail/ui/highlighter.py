"""Jinja2 syntax highlighting for the template editor.

Deliberately small: the three Jinja delimiters, plus enough HTML to tell a tag from text. A full
HTML grammar would be more colour for no more information -- what somebody editing a mail template
needs to see at a glance is where the *fields* are, because those are what break.
"""

import re

from PySide6.QtCore import QRegularExpression
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat

#: chosen to stay legible on a light and a dark Fusion palette alike.
COLOURS = {
    "expression": "#0b6bb5",   # {{ ... }}  -- the fields, the thing you look for
    "statement": "#8a4fbd",    # {% ... %}
    "comment": "#6a8f4a",      # {# ... #}
    "tag": "#a0452c",          # <p ...>
    "attribute": "#7a6a1f",
    "string": "#2e7d32",
}


def _format(colour: str, *, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setForeground(QColor(colour))
    if bold:
        fmt.setFontWeight(QFont.Bold)
    if italic:
        fmt.setFontItalic(True)
    return fmt


class JinjaHighlighter(QSyntaxHighlighter):
    """One highlighter for every part -- subject, HTML, text and TOML.

    `html=False` drops the markup rules, which is what the plain-text body and the subject want:
    an `<` there is a literal `<` and colouring it as a tag would be a lie.
    """

    def __init__(self, document, *, html: bool = True):
        super().__init__(document)

        self.rules = []

        if html:
            self.rules += [
                (QRegularExpression(r"</?\s*[A-Za-z][\w:-]*"), _format(COLOURS["tag"])),
                (QRegularExpression(r"\b([\w-]+)(?=\s*=\s*[\"'])"), _format(COLOURS["attribute"])),
                (QRegularExpression(r"\"[^\"]*\"|'[^']*'"), _format(COLOURS["string"])),
            ]

        # -- Jinja last, so it paints over any HTML rule that matched inside a `{{ ... }}`.
        self.rules += [
            (QRegularExpression(r"\{%-?.*?-?%\}"), _format(COLOURS["statement"], bold=True)),
            (QRegularExpression(r"\{\{-?.*?-?\}\}"), _format(COLOURS["expression"], bold=True)),
        ]

        self.comment_format = _format(COLOURS["comment"], italic=True)
        self.comment_start = re.compile(r"\{#")
        self.comment_end = re.compile(r"#\}")

    def highlightBlock(self, text: str) -> None:
        for pattern, fmt in self.rules:
            match = pattern.globalMatch(text)
            while match.hasNext():
                found = match.next()
                self.setFormat(found.capturedStart(), found.capturedLength(), fmt)

        self._highlight_comments(text)

    def _highlight_comments(self, text: str) -> None:
        """`{# ... #}` spanning several lines.

        Worth the extra state: the starter templates lead with a long comment listing the available
        fields, and without this every line of it is painted as if it were markup.
        """
        start = 0
        if self.previousBlockState() != 1:
            found = self.comment_start.search(text)
            if not found:
                self.setCurrentBlockState(0)
                return
            start = found.start()

        while start >= 0:
            closing = self.comment_end.search(text, start)
            if closing:
                length = closing.end() - start
                self.setCurrentBlockState(0)
            else:
                length = len(text) - start
                self.setCurrentBlockState(1)

            self.setFormat(start, length, self.comment_format)

            if not closing:
                return
            following = self.comment_start.search(text, closing.end())
            start = following.start() if following else -1
