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
  is worse than sending three mails.

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

## The window

```bash
kasseimail gui
```

One window: the template list and an editor with a live preview rendered against a real row on top,
the spreadsheet below it with the preflight result per row, and the run controls and the log at the
bottom. The log pane shows the same loguru lines the CLI prints, live, while a run goes on in a
worker thread — Cancel stops it between messages, never in the middle of one.

## Output

Every run writes to `--out` (default `out/`):

```
out/
    report.csv        one row per recipient; what --resume reads. Semicolons and a BOM, for Excel
    run.log           everything, including what the console did not show
    messages/         the rendered bodies, 0007-jan-example-be.html and .txt
```

## Development

```bash
uv run pytest
```

The suite pins the things that fail *quietly*: an unescaped name in an HTML mail, an Excel number
rendering as `1001.0`, a template typo that would render a blank, an attachment resolved to the
wrong path, a resumed run that sends twice. It needs no tenant, no network and no Qt.
