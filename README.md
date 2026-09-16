# kasseimail

Bulk mail over the Microsoft Graph API. A Jinja2 template, a spreadsheet of recipients, one mail per
row — from the command line, or from a desktop window.

```bash
kasseimail send -t welcome -d people.xlsx               # dry run: nothing leaves
kasseimail send -t welcome -d people.xlsx --drafts      # drafts in Outlook
kasseimail send -t welcome -d people.xlsx --send        # actually send
kasseimail gui                                          # the window
```

## Why Graph and not SMTP

SMTP AUTH with basic authentication against `smtp.office365.com` still works today, but Microsoft
turns it off by default for existing tenants at the end of 2026 and removes it after 2027. Graph
needs no per-mailbox SMTP AUTH, files the message in your own Sent Items — so *"I never got it"* is
a question you can answer — and can leave a draft instead of sending, which is what makes a
rehearsal possible.

Sign-in is **delegated**: you sign in once with a device code as yourself, and mail leaves your
mailbox. There is no client secret anywhere and no application permission that would reach every
mailbox in the tenant.

## Installing

Everything runs through [uv](https://docs.astral.sh/uv/). There is no `requirements.txt`.

```bash
uv sync                      # the CLI
uv sync --group gui          # and the desktop window
uv sync --group dev          # and the test suite
```

Qt is deliberately not a dependency of the CLI: a machine that sends mail from a cron job has no
display and no business installing 100 MB of widgets. Without the group, `kasseimail gui` refuses to
start and tells you the command to run.

## Setting up the app registration

One-off, in the [Entra admin centre](https://entra.microsoft.com), by someone who can register an
application in your tenant.

1. **Identity → Applications → App registrations → New registration.**
   Name it whatever you like. Supported account types: *Accounts in this organizational directory
   only*. Leave the redirect URI empty.
2. Copy the **Application (client) ID** and the **Directory (tenant) ID** from the overview page.
3. **API permissions → Add a permission → Microsoft Graph → Delegated permissions.**
   Add `Mail.Send` and `Mail.ReadWrite`. `User.Read` is already there and can stay.
   No admin consent is needed for these — each user consents for themselves at first sign-in.
   Add `Mail.Send.Shared` and `Mail.ReadWrite.Shared` as well **only** if you will send from a
   shared mailbox — see [Sending from a shared mailbox](#sending-from-a-shared-mailbox). They are
   asked for at sign-in only when one is configured, so nobody consents to "send mail on behalf of
   others" to send their own mail.
4. **Authentication → Advanced settings → Allow public client flows → Yes.**
   This one gets forgotten, and without it Entra refuses the device code with **AADSTS7000218**.

Then tell kasseimail about it, in any of these — a flag beats the environment beats the file:

```bash
kasseimail config set --tenant-id <TENANT> --client-id <CLIENT>   # written to the config file
```

```bash
cp .env.example .env         # or KASSEIMAIL_TENANT_ID / KASSEIMAIL_CLIENT_ID in the environment
```

```bash
kasseimail login             # device code, once
kasseimail config show       # what is set, and where each value came from
```

The token is cached under your user data directory at mode `0600`. There is a refresh token in it:
anyone who can read that file can send mail as you.

## Templates

A template is a **folder**, and `kasseimail templates path` says where they live.

```
welcome/
    meta.toml          optional — defaults this template carries with it
    subject.j2         required — one line
    body.html.j2       either this...
    body.txt.j2        ...or this, or both
```

```bash
kasseimail templates list
kasseimail templates new welcome
kasseimail templates new reminder --from welcome
kasseimail templates edit welcome --part body.html.j2
```

Every column of the spreadsheet is a variable, lowercased with spaces turned into underscores: a
column headed `First Name` is `{{ first_name }}`. Besides those, a template gets `row`, `email` (the
address the mail is actually going to) and `attachments` (the filenames attached to it).

Three filters exist for spreadsheet data: `{{ due | date("%d %B %Y") }}`, `{{ amount | money }}`
and `{{ note | blank }}`.

Month and day names are **not** taken from the machine's locale — they are written into the tool, so
the same template produces the same text wherever it runs. Pick the language in the template:
`{{ due | date("%d %B %Y", "nl") }}` gives *01 maart 2026*. English, Dutch and French are built in.

**A name that does not exist is an error, not a blank.** `{{ frist_name }}` stops the run instead of
sending seventy mails opening with *"Dear ,"*. That is what `kasseimail validate` is for — it
renders every row and touches no network.

`meta.toml` carries what this template always does, and every line of it is overridable on the
command line:

```toml
description = "Welcome mail for new members"
required    = ["email", "first_name"]                          # checked before anything is sent
attachments = ["handbook.pdf", "invoices/{{ invoice_no }}.pdf"]
cc          = ["secretary@example.be"]
```

### HTML and plain text

Graph's message carries **one** body, not a multipart alternative. So when `body.html.j2` exists the
mail goes out as HTML and the plain-text version does not travel — it is still rendered into the
output directory, where it is what you read to check a run.

Put all styling in `style=""` attributes, not in a `<style>` block: Outlook throws away a stylesheet
in the `<head>` and your mail arrives as unstyled text.

## The recipient list

A `.csv`, `.tsv` or `.xlsx`. The delimiter is sniffed, a BOM is handled, and the first sheet is used
unless you pass `--sheet`.

```bash
kasseimail validate -t welcome -d people.xlsx --columns
```

- The address comes from the `email` column, or from `--email-column`.
- `--key-column` is what identifies a row in the report, and therefore what `--resume` skips on.
  It defaults to the address.
- An integral float becomes an integer on the way in, because Excel stores an invoice number as
  `1001.0` and nobody wants that in front of a customer.
- A row with no address is **skipped and named in the report** — nothing disappears quietly.
- The same address on several rows is a warning, not an error, and gets one mail per row.
  Households share a mailbox, and sending three people one mail that is mostly about somebody else
  is worse than sending three mails. When it *is* one person booking three things, group the rows —
  see below.

## Grouping several rows into one mail

A flea market books stands one row at a time, so the same person turns up three times with three
stand numbers — and should get **one** mail listing all three.

```bash
kasseimail send -t market -d stands.xlsx --group-by email
```

The other columns are combined across the rows of the group. By default (`auto`) a column that reads
the same on every row collapses to that one value and a column that differs becomes the list, so
this needs nothing configured:

| | |
|---|---|
| `{{ first_name }}` | `Jan` — the same on all three rows |
| `{{ stand_number }}` | `12, 14, 19` — they differ, so they list |
| `{{ count }}` | `3` |

Override a column when `auto` is not what you meant:

```bash
kasseimail send -t market -d stands.xlsx --group-by email --aggregate price=sum
```

| aggregator | |
|---|---|
| `auto` | one value if every row agrees, otherwise the distinct values joined *(default)* |
| `first` | the first row's value |
| `list` | every row's value, joined, in order |
| `unique` | the distinct values, joined |
| `sum` | the numbers added up |
| `count` | how many rows had a value |

`--list-separator " / "` changes what joins them.

**A template also gets the rows themselves**, as `rows`, which is what makes grouping more than
joined strings:

```jinja
You booked {{ count }} stands, {{ price | money }} in total:
{% for r in rows %}
  stand {{ r.stand_number }} ({{ r.size }}) — {{ r.price | money }}
{% endfor %}
```

**Attachments resolve per row, not per group.** `--attach-pattern "stand-{{ stand_number }}.pdf"`
against a group of three attaches all three PDFs — rendered against the group it would go looking
for `stand-12, 14, 19.pdf`.

Two things worth knowing:

- **A blank in the group column is never grouped.** Those rows would otherwise collapse into a
  single recipient keyed on nothing — one mail standing for everybody the file failed to identify.
- **The address is never a joined list.** Grouping on something other than the address takes the
  first address in each group and warns when a group spans more than one, because that almost always
  means the group column is not the one you wanted.

A template can carry its own grouping, since one written to say *"your stands are 12, 14 and 19"*
only makes sense against grouped rows:

```toml
group_by = "email"

[aggregate]
price = "sum"
```

## Attachments

Three sources, combined per row:

```bash
--attach handbook.pdf                              # the same file on every mail
--attach-pattern "invoices/{{ invoice_no }}.pdf"   # rendered per row; globs work
--attachment-column attachment                     # paths in a column, separated by ; or |
```

Relative paths resolve against `--attachment-root`, which defaults to the directory the spreadsheet
is in. **A missing file stops the run before it starts** — finding out at message 40 that the PDFs
stop at 39 leaves you with 39 mails you cannot recall. `--allow-missing-attachments` turns that into
a per-row skip.

Under ~3 MB the files ride along inside the request. Above it, the message is created as a draft,
each file goes up through an upload session, and the draft is then sent.

## Running one safely

The order that costs the least when something is wrong:

```bash
kasseimail validate -t welcome -d people.xlsx                          # no network at all
kasseimail send -t welcome -d people.xlsx                              # dry run: out/messages/*
kasseimail send -t welcome -d people.xlsx --drafts --test-to me@ours.be --limit 3
kasseimail send -t welcome -d people.xlsx --send --test-to me@ours.be  # the whole file, to you
kasseimail send -t welcome -d people.xlsx --send                       # the real thing
```

`--test-to` redirects every message to one mailbox but changes nothing else: the subject still names
the real person, and a row without an address is *still* skipped. A rehearsal that quietly fixes the
empty cells is not a rehearsal.

If a run breaks part-way, **do not start it again** — continue it:

```bash
kasseimail send -t welcome -d people.xlsx --send --resume
```

`--resume` reads `out/report.csv` and skips every row already recorded as sent. The report is
appended, never overwritten, so the trace of the first attempt survives the second.

A row is identified by its **row number together with its key**, not by the key alone — an address
is deliberately not unique, and keyed on the address a resumed run would treat the second person at
a shared mailbox as already done. The cost is that editing the spreadsheet between a run and its
resume shifts the row numbers, and those rows go out again: resume against the file you ran with.

`--pause` defaults to 2.5 seconds because Exchange Online passes 30 messages a minute on client
submission. A 429 or 503 is honoured with its `Retry-After` and retried up to four times.

## Sending from a shared mailbox

Mail normally leaves the mailbox you signed in with. To send from another one — a shared
`info@`, say, while you sign in as yourself — name it:

```bash
kasseimail config set --mailbox info@example.be          # the default for every run
kasseimail send -t welcome -d people.xlsx --mailbox info@example.be --send   # just this one
```

`KASSEIMAIL_MAILBOX` works too, and an empty value means your own mailbox again:

```bash
kasseimail config set --mailbox ""
```

In the window it is the **Send from** box in the Delivery group, filled from the configuration and
changeable for one run. Account → Credentials sets the default. The title bar and the confirmation
both name the mailbox mail will come from, beside the account it is signed in with.

**Two permissions, and the one people miss is not the Graph one.** `Mail.Send.Shared` and
`Mail.ReadWrite.Shared` let the *app* act on a mailbox you have been given access to; they do not
give you that access. That is granted on the mailbox itself, by an administrator, in the Exchange
admin centre under **Recipients → Mailboxes → the mailbox → Delegation**:

- **Send as** — the mail comes from `info@example.be`, with nothing about you in the header.
- **Send on behalf** — it comes from `you@example.be on behalf of info@example.be`.
- **Read and manage (Full Access)** — needed for drafts, because a draft has to live in the mailbox
  it will be sent from.

With both Send as and Send on behalf granted, Exchange uses Send as. A newly granted right can take
a while to take effect.

Where things land follows the mailbox, not you: `--drafts` leaves the messages in **its** Drafts,
where the people who share it can look them over, and a send files a copy in **its** Sent Items.

The first run after configuring a mailbox asks for a device code again — the sign-in has two more
permissions to consent to. `kasseimail login` gets that out of the way beforehand. Then rehearse
once, which proves the permission without putting anything in anybody's inbox:

```bash
kasseimail send -t welcome -d people.xlsx --limit 1 --drafts        # appears in info@'s Drafts
kasseimail send -t welcome -d people.xlsx --limit 1 --send --test-to me@ours.be
```

A `403` here is the Exchange side, not the app registration: the error says so and names the
mailbox.

## The window

```bash
kasseimail gui
```

One window: the template list and an editor with a live preview rendered against a real row on top,
the spreadsheet below it with the preflight result per row, and the run controls and the log at the
bottom. The log pane shows the same loguru lines the CLI prints, live, while a run goes on in a
worker thread — Cancel stops it between messages, never in the middle of one.

- The **subject** has its own field above the body preview, because it is a separate field of the
  message and the one line every recipient certainly reads.
- **◀ ▶** step the preview through the recipients one at a time — the first, the last, and the one
  you know is awkward — and the position reads `3 of 42`. They move the table's own selection, so
  the table and the preview can never disagree about which recipient is on screen.
- **Group by** collapses the table to one line per message, and **Combine...** says how each column
  is aggregated. With grouping on, the arrows step between groups, and the Rows column names every
  spreadsheet row each message came from.
- **Send from** picks the mailbox for this run, empty being your own. It starts at whatever
  Account → Credentials holds, and the title bar names whichever it ends up being — see
  [Sending from a shared mailbox](#sending-from-a-shared-mailbox).

## Output

Every run writes to `--out` (default `out/`):

```
out/
    report.csv        one row per recipient; what --resume reads. Semicolons and a BOM, for Excel
    run.log           everything, including what the console did not show
    messages/         the rendered bodies, 0007-jan-example-be.html and .txt
```

## Building a standalone executable

For a machine with no Python on it. **A Windows `.exe` has to be built on Windows** — PyInstaller
freezes the interpreter and libraries of the machine it runs on, and there is no cross-compilation.
Run it on Linux or macOS and you get a working build for that platform instead.

```powershell
git clone https://github.com/...  &&  cd kasseimail
uv sync --group dev --group gui
uv run python scripts/build_exe.py
```

That produces `dist/kasseimail/` with two programs sharing one copy of Qt, and a zip of it next to
them:

| | |
|---|---|
| `kasseimail.exe` | the command line — a console program, because its output is text |
| `kasseimail-gui.exe` | the window — no console, so starting it flashes no black box |

Both come out of one PyInstaller analysis and decide what to do from the name they were started
under, so the bundle carries Qt once rather than twice. `dist\kasseimail\kasseimail-gui.exe` is what
you put a shortcut to; the whole folder has to travel together.

The build script commits nothing and only writes `build/` and `dist/`. Useful flags:

```powershell
uv run python scripts/build_exe.py --dirty      # build with uncommitted changes anyway
uv run python scripts/build_exe.py --no-zip     # leave the folder unpacked
```

It refuses a dirty working tree by default, because the version it bakes in names a commit that
would not contain your changes.

To run PyInstaller yourself, the spec works on any platform:

```bash
uv run pyinstaller kasseimail.spec
```

**Give it an icon** by dropping a `.ico` at `static/kasseimail.ico` before building — the spec picks
it up if it is there and builds without one if it is not. Windows caches icons aggressively, so a
newly built exe may show the old one until you rename it or log out.

Expect roughly 300 MB unpacked and 125 MB zipped. Almost all of it is Qt. The spec excludes the Qt
modules this does not use (QtWebEngine, Quick/QML, 3D, Multimedia, Charts and the rest) — if a
future change starts needing one, the build will fail rather than silently ship a broken bundle, and
the fix is to take that module out of `EXCLUDES` in [`kasseimail.spec`](kasseimail.spec).

UPX compression is deliberately off: it roughly halves the download and is a well-known way to have
a fresh build quarantined by Windows Defender.

## Versioning

The version comes from the **git tag** and nowhere else, through `hatch-vcs`. There is no literal in
`pyproject.toml` to drift from it.

```bash
git tag -a 0.2.0 -m "..."       # bare annotated tags, no `v` prefix
uv sync --reinstall-package kasseimail
uv run kasseimail --version
```

Exactly on a tag you get `0.2.0`; past one, `0.2.0.post1.dev3+g1a2b3c4`, which names the commit. The
`--reinstall-package` matters: `uv` will not rebuild just because a tag appeared, so without it the
version stays at whatever the last sync saw. `scripts/build_exe.py` does that step for you before
every build.

On Windows the version also lands in the exe's **Properties → Details**, so a bug report can name a
build even when nobody thought to run `--version`.

## Development

```bash
uv run pytest
```

The suite pins the things that fail *quietly*: an unescaped name in an HTML mail, an Excel number
rendering as `1001.0`, a template typo that would render a blank, an attachment resolved to the
wrong path, a resumed run that sends twice. It needs no tenant, no network and no Qt.
