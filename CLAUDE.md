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

**The URL is the sender.** A Graph message carries no `from`: `/me/sendMail` leaves your own mailbox
and `/users/info@example.be/sendMail` leaves that one, and `GraphMailer.base` is the one place that
chooses. So a shared mailbox is a substitution rather than a second code path — but every call has
to use it, because a draft created under `/me` and sent under `/users/...` is a 404 for a message id
that exists, in the other mailbox. Empty means `/me`, and that default is worth guarding: getting it
wrong does not fail, the mail simply comes from the wrong address and nothing says so. `None` and
`""` differ where a run is built — `None` is "this run did not say" and the settings decide, `""` is
a front end saying *my own mailbox*, which is what the window's emptied Send-from box means.

`Mail.Send.Shared` / `Mail.ReadWrite.Shared` are asked for **only when a mailbox is set**; nobody
should consent to "send mail on behalf of others" to send their own mail. The cost is one more
device code the first time. And the scope is not what grants the access — Send As / Send on Behalf
is granted on the mailbox in Exchange, which is why a 403 from a shared mailbox carries its own
explanation rather than Graph's six words repeated once per row.

**Paths are absolute.** `kpmail` kept its token cache at a relative `data/graph_token.json`, so
running it from elsewhere silently started a second device-code login. Everything here is anchored
to the user config and data directories.

**The resume key is `(row, key)`, not the key alone.** An address is deliberately not unique — a
household shares a mailbox and each member gets their own mail — so keying on the address means a
resumed run treats the second person as already done and they never get theirs, silently. The cost
is that editing the spreadsheet between a run and its resume shifts the row numbers.

**Never let `strftime` pick month names.** The process locale is not ours to rely on:
constructing a `QApplication` calls `setlocale(LC_ALL, "")`, and something in the GUI stack changes
it again later, at a moment nothing controls — the same template then renders "01 March 2026" from
the CLI and "01 maart 2026" from the window, on the same machine. `templates.MONTH_NAMES` /
`DAY_NAMES` hold the names and `_format_date` substitutes `%B %b %A %a` (and the locale-dependent
aggregates `%c %x %X`) before strftime ever sees them; the language is a filter argument. Everything
numeric is still strftime's job.

`ui/app.build_application` also restores the locale after constructing the QApplication, and every
QApplication in the project — the test fixture included — goes through it. That is belt and braces
now rather than the fix: the fix is that rendering does not read the locale.

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

## Grouping

`recipients.group()` collapses several rows into one `Recipient` when a group column is given. The
aggregated values land in `fields` as usual, and the rows behind them are kept in `members` /
`source_rows` — both halves matter:

- `fields` gives `{{ stand_number }}` as `"12, 14, 19"`, which is the common case.
- `members` reaches the template as `rows`, so it can lay them out with each stand's size beside it.
  Without that, grouping could only ever produce joined strings.
- **`AttachmentSpec` resolves per member**, not off the aggregated value. A pattern rendered against
  the group would go looking for `stand-12, 14, 19.pdf`. For an ungrouped recipient there is exactly
  one member, so it is the same work it always did.

`auto` is the default aggregator because it needs no configuration and is what people mean: same on
every row → that value, differs → the distinct values joined. It returns the *original* value when
the rows agree, so a date stays a date and `| date(...)` still works.

Two invariants that are easy to break:

- **A blank group value is never grouped.** Those rows would collapse into one recipient keyed on
  the empty string — a single message standing for everybody the file failed to identify.
- **The address is never aggregated.** Graph cannot send to a joined list, so a group takes the
  first address and warns when it spans more than one.

The window keeps `raw_table` (as read) and `table` (grouped) separately, so changing the grouping
never re-reads the file. `set_grouping` blocks the combo's signal deliberately — `_grouping_changed`
clears the aggregators, which is right when a person picks another column and wrong when a template's
`meta.aggregate` is being handed in.

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

## Version and the frozen build

**The version comes from the git tag, through `hatch-vcs`, and from nowhere else.** `pyproject.toml`
declares it dynamic; there is no literal to drift. `[tool.hatch.build.hooks.vcs]` writes
`src/kasseimail/_version.py` (generated, gitignored) at install time, and `version.py` reads it
first, then `importlib.metadata`, then `git describe`.

That order exists for the frozen build: **PyInstaller bundles no package metadata**, so
`importlib.metadata` finds nothing inside an executable and `_version.py` is the only thing that
works there. `_from_git` refuses to run when `sys.frozen` is set — the directory an exe was started
from may be somebody else's repository.

`uv` does not rebuild just because a tag appeared, so `_version.py` goes stale the moment you tag.
`scripts/build_exe.py` runs `uv sync --reinstall-package kasseimail` before every build for exactly
that reason; by hand it is the same command.

`kasseimail.spec` builds **two programs from one analysis** — `pyinstaller_entry.py` dispatches on
`argv[0]`, so `kasseimail-gui.exe` opens the window and `kasseimail.exe` is the CLI, sharing one copy
of Qt. onedir rather than onefile: a onefile Qt bundle unpacks ~300 MB to temp on every launch.

Three things there are easy to undo by accident:

- `templates_builtin/` is **data**, so nothing in the import graph points at it. `collect_data_files`
  puts it where `templates.BUILTIN_DIR` looks; without it a frozen build opens on an empty template
  list.
- A **windowed build on Windows has no console**, and Python sets `sys.stderr` to `None` there.
  `logs.setup()` skips its sink in that case — handing `None` to loguru raises, and the window would
  die before anything was on screen.
- The Windows version resource takes **four plain integers**, so `version_tuple()` cuts
  `0.1.0.post1.dev0+g78eb379` down to `(0, 1, 0, 0)`. The full string goes in the text fields beside
  it.

A Windows `.exe` must be built on Windows; PyInstaller cannot cross-compile. The spec itself is
platform-neutral, and building on Linux is a decent check that it still works.

## Freezing

**The GUI thread never waits on a worker.** `QThread.wait()` there is a plain block -- no repaints,
no events -- and `quit()` does nothing to a slot that is still running, so it waits out whatever the
worker is doing. A message in flight plus a throttling pause was easily ten seconds of a window the
desktop offers to kill.

So closing is *deferred*: `closeEvent` refuses the event, cancels the workers, says so on screen, and
`_close_if_idle` closes for real when they report they have stopped. `STOP_TIMEOUT_MS` is the
backstop for a worker wedged in a socket read; past it the thread is **orphaned into
`worker._ORPHANED` rather than dropped**, because destroying a running QThread aborts the process,
and `ui/app.run_gui` then `os._exit`s rather than abort during interpreter shutdown.

Everything a worker waits on is sliced so a cancel lands during it, not after:

- `run.interruptible_sleep` for the pause between messages -- the default 2.5s, and far more when
  somebody is careful about throttling.
- the same for Graph's `Retry-After`, which can be minutes.
- `SendWorker._on_device_code` watching `_cancel` while it waits for the dialog.

And no result dialog is raised while `_stopping`: a modal box on a window that is already going away
holds the close open until somebody dismisses it.

`tests/test_freezing.py` measures the *longest stretch in which the event loop did not turn*, not
whether a call returned quickly -- a blocking `wait()` returns eventually too.

## Testing

`uv run pytest`. Test names are full sentences and each has a docstring naming the failure it
prevents. The suite pins what fails **quietly** — an unescaped name in an HTML mail, `1001.0` in a
subject, an attachment resolved to the wrong path, a resumed run that sends twice — and deliberately
does not exercise MSAL or Graph, which need a tenant. `tests/conftest.py::FakeMailer` stands in for
the loop around them.

`tests/test_ui.py` skips whole when PySide6 is absent, and runs on Qt's offscreen platform. Its
`dialogs` fixture stubs `QMessageBox`: a modal dialog would block the thread the test pumps events
on and hang the suite forever.
