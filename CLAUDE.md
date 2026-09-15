# kasseimail

A generic bulk mailer over the Microsoft Graph API: a Jinja2 template, a spreadsheet of recipients,
one mail per row. Two front ends — a CLI (`kasseimail`) and a PySide6 window (`kasseimail gui`) —
over one engine.

It is a generalisation of `kpmail` in the sibling `kasseiplan` repo, with the planning app cut out:
no SQLite, no Playwright, no domain concepts. Where a decision here looks odd, `kasseiplan/kasseiplan/mailer.py`
is usually where it was first made and why.

Code, comments, CLI help and GUI labels are **English throughout** — unlike `kasseiplan`, which is
Dutch-facing. This tool has no particular audience.

## Commands

Everything runs through `uv`. There is no `requirements.txt`.

```bash
uv sync --group dev --group gui     # everything
uv run pytest                       # the suite; needs no tenant, no network, no display
uv run kasseimail --help
QT_QPA_PLATFORM=offscreen uv run pytest tests/test_ui.py    # the GUI tests, headless
```

## The shape of it

```
src/kasseimail/
    cli.py          argparse subcommands; cli(argv) -> int
    config.py       settings (flags > env > TOML), absolute paths, the optional-dependency guard
    logs.py         loguru setup — the only place a sink is added outside the GUI
    recipients.py   csv/xlsx -> Recipient rows; header and value normalisation
    templates.py    TemplateSet / Template: discovery, meta.toml, the Jinja environment
    attachments.py  AttachmentSpec: per-row / common / pattern resolution, size budget
    message.py      the Graph message JSON; the inline-vs-upload-session decision
    graph.py        GraphMailer: MSAL device code, send/draft/upload, 429 backoff
    ledger.py       report.csv: append, and read back for --resume
    run.py          SendRun.preflight() and .execute() — the engine both front ends drive
    ui/             everything Qt, and nothing else imports it
```

**Nothing outside `ui/` imports Qt, and `run.py` imports nothing from `ui/`.** That is what lets
`kasseimail send` run on a headless box. `SendRun.execute` takes `progress` and `should_cancel`
callables: the CLI passes a logging printer, the window passes signal emitters.

## The decisions that are load-bearing

**Dry run is the default.** `send` with neither `--drafts` nor `--send` renders to the output
directory and nothing leaves. Fifty mails cannot be recalled and there is no undo to build, so every
affordance points the other way: sending takes a flag you had to type, `--test-to` rehearses the
whole run into one mailbox, `--resume` continues a broken run instead of repeating it, and both
front ends confirm once before drafts or a send.

**Preflight renders every row before anything is sent.** `SendRun.preflight()` touches no network. It
collects rather than stopping at the first problem, because the failures worth catching repeat: a
misspelled column is not one broken mail, it is all of them. `execute()` refuses to start if
preflight found errors and the mode is not a dry run.

**`StrictUndefined`, always.** `{{ frist_name }}` is an error, not a blank. Without it seventy mails
go out opening with "Dear ,".

**Autoescape is decided by a hand-written predicate**, not `select_autoescape(["html"])` — that one
matches the *last* extension and every template ends in `.j2`, so it would read `body.html.j2` as
"not HTML" and render a spreadsheet value raw into the markup.

**Graph, not SMTP.** Microsoft turns SMTP AUTH off by default for existing tenants at the end of 2026.
Sign-in is delegated device code: mail leaves your own mailbox and lands in your Sent Items, and
there is no application permission reaching every mailbox in the tenant.

**Paths are absolute.** `kpmail` kept its token cache at a relative `data/graph_token.json`, so
running it from elsewhere silently started a second device-code login. Everything here is anchored
to the user config and data directories.

**The resume key is `(row, key)`, not the key alone.** An address is deliberately not unique — a
household shares a mailbox and each member gets their own mail — so keying on the address means a
resumed run treats the second person as already done and they never get theirs, silently. The cost
is that editing the spreadsheet between a run and its resume shifts the row numbers.

**Qt changes the C locale.** Constructing a `QApplication` calls `setlocale(LC_ALL, "")`, and
`{{ due | date("%d %B %Y") }}` goes through `strftime` — so the same template would render
"01 March 2026" from the CLI and "01 maart 2026" from the window. `ui/app.py` restores the locale
straight after. If you ever construct the `QApplication` somewhere else, do the same.

**Qt is an optional dependency**, in the `gui` group behind `config.require()`, which uses
`find_spec` rather than `import` because importing PySide6 costs about a second and the guard runs
before any real work.

## Values out of a spreadsheet

`recipients.clean_value` exists for one reason each:

- An **integral float becomes an int** — Excel stores an invoice number as `1001.0`, and
  `{{ invoice_no }}` then puts *1001.0* in front of a customer. Every other check passes while this
  goes wrong, which is why it has its own test.
- A **datetime stays a datetime**, for the `date` filter. Rendered raw it is `2026-03-01 00:00:00`.
- Headers normalise to Jinja-typeable names: `First Name` → `{{ first_name }}`.

## Threading in the window

`ui/worker.py` moves a `SendWorker` onto a `QThread`. Cancel is a `threading.Event` polled *between*
messages — never during one, because stopping mid-message can leave a half-uploaded draft with
nothing in the report about it, and that report is what `--resume` reads.

The device code flow **blocks**, so it runs on the worker thread and asks the GUI thread to show the
dialog through a signal. `GraphMailer.sign_in` takes an `on_device_code` callback for exactly this
reason; `kpmail` `print`s the message, which a window cannot use.

`ui/log_panel.py` registers a loguru sink that emits a Qt signal. loguru calls the sink on whichever
thread logged, and a widget may only be touched from the GUI thread — the queued signal is the
handover. Never write to the widget from the sink.

## Testing

`uv run pytest`. Test names are full sentences and each has a docstring naming the failure it
prevents. The suite pins what fails **quietly** — an unescaped name in an HTML mail, `1001.0` in a
subject, an attachment resolved to the wrong path, a resumed run that sends twice — and deliberately
does not exercise MSAL or Graph, which need a tenant. `tests/conftest.py::FakeMailer` stands in for
the loop around them.

`tests/test_ui.py` skips whole when PySide6 is absent, and runs on Qt's offscreen platform. Its
`dialogs` fixture stubs `QMessageBox`: a modal dialog would block the thread the test pumps events
on and hang the suite forever.
