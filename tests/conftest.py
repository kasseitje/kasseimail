"""Fixtures: a template directory and a spreadsheet, both built per test in a tmp_path.

Nothing here reaches a network, a tenant or a display. Everything the suite pins is something that
goes wrong *quietly* -- a body that renders but is wrong, a file resolved to the wrong path, a
resumed run that sends twice -- so the fixtures build the real objects rather than mocks of them.
"""

import datetime

from pathlib import Path

import pytest

from kasseimail.attachments import AttachmentSpec
from kasseimail.recipients import load as load_recipients
from kasseimail.templates import TemplateSet


@pytest.fixture
def template_dir(tmp_path) -> Path:
    directory = tmp_path / "templates"
    directory.mkdir()
    return directory


@pytest.fixture
def make_template(template_dir):
    """Write a template directory out of the parts a test cares about.

    The defaults reference **no variables at all**, so a test that overrides only the HTML body is
    not also testing whatever the default subject happened to need. Every test that cares about a
    field puts that field in the part it is about.
    """

    def build(name="demo", *, subject="A subject\n", html="<p>A body</p>\n",
              text=None, meta=None):
        directory = template_dir / name
        directory.mkdir(parents=True)

        if subject is not None:
            (directory / "subject.j2").write_text(subject, encoding="utf-8")
        if html is not None:
            (directory / "body.html.j2").write_text(html, encoding="utf-8")
        if text is not None:
            (directory / "body.txt.j2").write_text(text, encoding="utf-8")
        if meta is not None:
            (directory / "meta.toml").write_text(meta, encoding="utf-8")

        return TemplateSet(template_dir).get(name)

    return build


@pytest.fixture
def make_csv(tmp_path):
    """A CSV, with the delimiter and encoding a test wants to pin."""

    def build(rows, *, name="people.csv", delimiter=";", encoding="utf-8"):
        path = tmp_path / name
        text = "\n".join(delimiter.join(str(cell) for cell in row) for row in rows) + "\n"
        path.write_text(text, encoding=encoding)
        return path

    return build


@pytest.fixture
def make_xlsx(tmp_path):
    """A workbook, so the Excel-specific value handling can be tested against real openpyxl output."""

    def build(rows, *, name="people.xlsx", sheet="Sheet1", extra_sheets=None):
        import openpyxl

        book = openpyxl.Workbook()
        worksheet = book.active
        worksheet.title = sheet
        for row in rows:
            worksheet.append(row)

        for title, extra_rows in (extra_sheets or {}).items():
            other = book.create_sheet(title)
            for row in extra_rows:
                other.append(row)

        path = tmp_path / name
        book.save(path)
        return path

    return build


@pytest.fixture
def people(make_xlsx):
    """The workbook most tests use: two good rows, one without an address, one namesake.

    The numbers are floats and the date is a datetime because that is what openpyxl hands back --
    testing against hand-built strings would miss exactly the conversions that matter.
    """
    return make_xlsx([
        ["Email", "First Name", "Company", "Invoice No", "Amount", "Due", "attachment"],
        ["jan@example.be", "Jan", "Peeters & Zn", 1001, 1234.5,
         datetime.datetime(2026, 3, 1), "invoices/A-1001.pdf"],
        ["an@example.be", "An", "<Tags> nv", 1002, 87.0,
         datetime.datetime(2026, 3, 15), "invoices/A-1002.pdf"],
        ["", "Nobody", "No Address bv", 1003, 10.0, datetime.datetime(2026, 4, 1), ""],
        ["jan@example.be", "Jan junior", "Peeters & Zn", 1004, 5.0,
         datetime.datetime(2026, 4, 2), ""],
    ])


@pytest.fixture
def table(people):
    return load_recipients(people)


@pytest.fixture
def make_files(tmp_path):
    """Attachments on disk, with a size, next to wherever the spreadsheet is."""

    def build(*names, size=512, directory=None):
        target = Path(directory) if directory else tmp_path
        target.mkdir(parents=True, exist_ok=True)
        written = []
        for name in names:
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"%PDF-1.4 " + b"x" * max(0, size - 9))
            written.append(path)
        return written

    return build


@pytest.fixture
def spec(tmp_path):
    def build(**kwargs):
        kwargs.setdefault("root", tmp_path)
        return AttachmentSpec(**kwargs)

    return build


class FakeMailer:
    """A GraphMailer that records instead of sending.

    The real one is not exercised here on purpose: it needs a tenant, and the parts of it worth
    pinning -- the message shape, the inline/upload decision -- are in `message.py` where they can
    be tested without one. What this stands in for is the *loop* around it.
    """

    def __init__(self, fail_on=()):
        self.sent = []
        self.drafts = []
        self.fail_on = set(fail_on)

    def deliver(self, delivery, *, send, on_chunk=None):
        address = delivery.message["toRecipients"][0]["emailAddress"]["address"]
        if address in self.fail_on:
            from kasseimail.graph import GraphProblem

            raise GraphProblem("Graph 400: the mailbox is full")

        (self.sent if send else self.drafts).append(delivery)
        return "" if send else f"draft-{len(self.drafts)}"

    def cached_account(self):
        return None


@pytest.fixture
def fake_mailer():
    return FakeMailer


# ---------------------------------------------------------------------------------------------
# Qt
# ---------------------------------------------------------------------------------------------
#
# Shared by every GUI module. The Qt imports are inside the fixtures on purpose: this file is
# collected on a machine with no `gui` group installed, where those modules skip themselves and
# these fixtures are simply never asked for.

@pytest.fixture(scope="session")
def qt_app():
    """One QApplication for the session -- Qt refuses a second one in the same process.

    Built through `ui.app.build_application` rather than directly, because constructing a
    QApplication changes the C locale for the whole process and every later test would then render
    dates in the desktop's language. That is the product's own trap, and a fixture that sidestepped
    the fix would reintroduce it across the suite.
    """
    from kasseimail.ui.app import build_application

    yield build_application()


@pytest.fixture
def dialogs(monkeypatch):
    """Record what a message box would have said instead of showing it.

    Two reasons. A modal dialog blocks the thread the test is pumping events on, so a run that
    ends in one would hang the suite forever -- which is the very failure the freeze tests are
    about. And a dialog that was *raised* is often the thing worth asserting: a confirmation
    before sending is a feature, not a side effect.
    """
    from PySide6.QtWidgets import QMessageBox

    shown = []

    def record(kind, answer=QMessageBox.Yes):
        def fake(_parent, title, text, *args, **kwargs):
            shown.append((kind, title, text))
            return answer

        return fake

    for name in ("information", "warning", "critical", "question", "about"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(record(name)))

    # -- the send confirmation builds a QMessageBox rather than calling a static method.
    monkeypatch.setattr(QMessageBox, "exec", lambda self: shown.append(
        ("confirm", self.windowTitle(), self.text())) or QMessageBox.Yes)

    return shown
