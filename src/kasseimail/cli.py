"""`kasseimail` -- the command line.

    kasseimail send -t welcome -d people.xlsx               # dry run: nothing leaves
    kasseimail send -t welcome -d people.xlsx --drafts      # drafts in Outlook
    kasseimail send -t welcome -d people.xlsx --send        # actually send

**A dry run is the default, and that is not a formality.** Fifty mails cannot be recalled, and the
one thing every bulk mailer gets asked for afterwards is the undo it does not have. So sending takes
a flag you had to type, `--test-to` rehearses the whole run into your own mailbox first, and
`--resume` means a run that broke is continued rather than repeated.

`argparse` and subcommands, `cli(argv) -> int`: argv in, an exit code out, so the whole thing is
callable from a test without a subprocess.
"""

import argparse
import sys

from pathlib import Path

from loguru import logger

from kasseimail import config, logs
from kasseimail.attachments import AttachmentSpec
from kasseimail.graph import GraphMailer, SignInProblem
from kasseimail.ledger import MODE_DRAFTS, MODE_DRY_RUN, MODE_SEND
from kasseimail.recipients import (
    AGGREGATOR_HELP, AGGREGATORS, DEFAULT_AGGREGATOR, DEFAULT_SEPARATOR, GroupSpec,
    RecipientProblem,
)
from kasseimail.recipients import group as group_recipients
from kasseimail.recipients import load as load_recipients
from kasseimail.run import RunProblem, SendRun
from kasseimail.templates import PARTS, TemplateProblem, TemplateSet
from kasseimail.version import __version__


def main() -> None:
    """The console script. Wraps `cli` so a Ctrl-C is a clean exit rather than a traceback."""
    try:
        sys.exit(cli())
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        sys.exit(130)


# ---------------------------------------------------------------------------------------------
# the parser
# ---------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kasseimail",
        description="Bulk mail over Microsoft Graph: a Jinja2 template, a spreadsheet, one mail "
                    "per row.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Nothing is sent without --send. Start with a dry run, rehearse with --test-to, "
               "then send.",
    )
    parser.add_argument("--version", action="version", version=f"kasseimail {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    common.add_argument("-q", "--quiet", action="store_true", help="warnings and errors only")
    common.add_argument("--templates", metavar="DIR",
                        help="the template directory (default: KASSEIMAIL_TEMPLATES, "
                             "or the user data dir)")
    common.add_argument("--tenant", metavar="ID", help="Entra ID tenant (default: KASSEIMAIL_TENANT_ID)")
    common.add_argument("--client-id", metavar="ID",
                        help="app registration (default: KASSEIMAIL_CLIENT_ID)")
    common.add_argument("--mailbox", metavar="ADDRESS",
                        help="send from this mailbox instead of your own, e.g. a shared "
                             "info@ address. You need 'Send As' or 'Send on behalf' on it. "
                             "(default: KASSEIMAIL_MAILBOX)")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # -- send ---------------------------------------------------------------------------------
    send = sub.add_parser("send", parents=[common], help="render, draft or send a run",
                          formatter_class=argparse.RawDescriptionHelpFormatter,
                          description="One mail per row of the spreadsheet. Without --drafts or "
                                      "--send this only renders: the bodies land in the output "
                                      "directory and nothing leaves the machine.")
    send.add_argument("-t", "--template", required=True, help="template name")
    send.add_argument("-d", "--data", required=True, metavar="FILE", help="a .csv or .xlsx")
    send.add_argument("--sheet", metavar="NAME", help="which sheet of the workbook (default: the first)")
    send.add_argument("--email-column", default="email", metavar="COLUMN",
                      help="the column holding the address (default: email)")
    send.add_argument("--key-column", metavar="COLUMN",
                      help="what identifies a row in the report, for --resume "
                           "(default: the email column)")
    send.add_argument("--group-by", metavar="COLUMN",
                      help="one message per distinct value of this column instead of one per row. "
                           "Use it when the same person appears on several rows.")
    send.add_argument("--aggregate", action="append", default=[], metavar="COLUMN=HOW",
                      help="how to combine a column across a group; repeatable. HOW is one of "
                           + ", ".join(AGGREGATORS)
                           + f" (default: {DEFAULT_AGGREGATOR} -- "
                           + AGGREGATOR_HELP[DEFAULT_AGGREGATOR] + ")")
    send.add_argument("--list-separator", default=DEFAULT_SEPARATOR, metavar="TEXT",
                      help=f"what joins the values of a listed column (default: {DEFAULT_SEPARATOR!r})")

    delivery = send.add_mutually_exclusive_group()
    delivery.add_argument("--drafts", action="store_true",
                          help="leave the messages in your Drafts; you press send")
    delivery.add_argument("--send", action="store_true", dest="do_send",
                          help="actually send them")

    send.add_argument("--test-to", metavar="ADDRESS",
                      help="send every message to this address instead, to rehearse. The real "
                           "name stays in the subject and a row without an address is still "
                           "skipped.")
    send.add_argument("--attach", action="append", default=[], metavar="FILE",
                      help="attach this file to every message; repeatable")
    send.add_argument("--attach-pattern", action="append", default=[], metavar="PATTERN",
                      help="a path rendered per row, e.g. 'invoices/{{ invoice_no }}.pdf'; "
                           "repeatable. Globs are expanded.")
    send.add_argument("--attachment-column", action="append", default=[], metavar="COLUMN",
                      help="a column holding paths, separated by ; or |; repeatable")
    send.add_argument("--attachment-root", metavar="DIR",
                      help="what relative attachment paths are relative to "
                           "(default: the directory the spreadsheet is in)")
    send.add_argument("--allow-missing-attachments", action="store_true",
                      help="send anyway when an attachment is not on disk, instead of refusing")
    send.add_argument("--cc", action="append", default=[], metavar="ADDRESS", help="repeatable")
    send.add_argument("--bcc", action="append", default=[], metavar="ADDRESS", help="repeatable")
    send.add_argument("--reply-to", action="append", default=[], metavar="ADDRESS", help="repeatable")
    send.add_argument("-o", "--out", default="out", metavar="DIR",
                      help="where the rendered bodies, the report and the log go (default: out)")
    send.add_argument("--resume", action="store_true",
                      help="skip the rows already recorded as sent in the report there")
    send.add_argument("--limit", type=int, metavar="N", help="only the first N rows that would go out")
    send.add_argument("--pause", type=float, metavar="SECONDS",
                      help=f"between two messages (default: {config.DEFAULT_PAUSE_SECONDS}). "
                           "Exchange Online passes 30 a minute.")
    send.add_argument("-y", "--yes", action="store_true", help="do not ask before sending")
    send.set_defaults(func=cmd_send)

    # -- validate -----------------------------------------------------------------------------
    validate = sub.add_parser("validate", parents=[common],
                              help="check a template against a spreadsheet, without sending",
                              description="Renders every row, resolves every attachment and checks "
                                          "every address. Touches no network. Exit 1 if anything "
                                          "would fail.")
    validate.add_argument("-t", "--template", required=True)
    validate.add_argument("-d", "--data", required=True, metavar="FILE")
    validate.add_argument("--sheet", metavar="NAME")
    validate.add_argument("--email-column", default="email", metavar="COLUMN")
    validate.add_argument("--key-column", metavar="COLUMN")
    validate.add_argument("--group-by", metavar="COLUMN")
    validate.add_argument("--aggregate", action="append", default=[], metavar="COLUMN=HOW")
    validate.add_argument("--list-separator", default=DEFAULT_SEPARATOR, metavar="TEXT")
    validate.add_argument("--attach", action="append", default=[], metavar="FILE")
    validate.add_argument("--attach-pattern", action="append", default=[], metavar="PATTERN")
    validate.add_argument("--attachment-column", action="append", default=[], metavar="COLUMN")
    validate.add_argument("--attachment-root", metavar="DIR")
    validate.add_argument("--allow-missing-attachments", action="store_true")
    validate.add_argument("--columns", action="store_true",
                          help="also list the columns and what the template asks for")
    validate.set_defaults(func=cmd_validate)

    # -- templates ----------------------------------------------------------------------------
    templates = sub.add_parser("templates", parents=[common], help="manage the template directory")
    template_actions = templates.add_subparsers(dest="action", metavar="ACTION")

    template_actions.add_parser("list", help="every template, with its description")
    template_actions.add_parser("path", help="print the template directory")

    show = template_actions.add_parser("show", help="print a template's files")
    show.add_argument("name")
    show.add_argument("--part", choices=PARTS, help="only this file")

    new = template_actions.add_parser("new", help="create one from the starter, or from another")
    new.add_argument("name")
    new.add_argument("--from", dest="copy_from", metavar="NAME", help="copy this template instead")

    edit = template_actions.add_parser("edit", help="open a template's file in $EDITOR")
    edit.add_argument("name")
    edit.add_argument("--part", choices=PARTS, default="body.html.j2")

    delete = template_actions.add_parser("delete", help="remove a template, files and all")
    delete.add_argument("name")
    delete.add_argument("-y", "--yes", action="store_true")

    templates.set_defaults(func=cmd_templates)

    # -- account and configuration ------------------------------------------------------------
    login = sub.add_parser("login", parents=[common], help="sign in with a device code")
    login.set_defaults(func=cmd_login)

    logout = sub.add_parser("logout", parents=[common], help="forget the cached token")
    logout.set_defaults(func=cmd_logout)

    configure = sub.add_parser("config", parents=[common], help="show or set the configuration")
    config_actions = configure.add_subparsers(dest="action", metavar="ACTION")
    config_actions.add_parser("show", help="every setting, and where it came from")
    config_actions.add_parser("path", help="print the config file path")
    config_set = config_actions.add_parser("set", help="write a setting to the config file")
    config_set.add_argument("--tenant-id")
    config_set.add_argument("--client-id")
    config_set.add_argument("--mailbox", metavar="ADDRESS",
                            help="the mailbox to send from; empty string to send from your own")
    config_set.add_argument("--template-dir")
    config_set.add_argument("--pause", type=float)
    configure.set_defaults(func=cmd_config)

    # -- gui ----------------------------------------------------------------------------------
    gui = sub.add_parser("gui", parents=[common], help="open the desktop window")
    gui.set_defaults(func=cmd_gui)

    return parser


def cli(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    logs.setup(verbose=getattr(args, "verbose", False), quiet=getattr(args, "quiet", False))

    try:
        return args.func(args)
    except (TemplateProblem, RecipientProblem, RunProblem, SignInProblem) as exc:
        # -- the errors that mean "the input is wrong", reported as a message rather than a
        #    traceback. A stack trace here names this file, which is never where the problem is.
        logger.error(str(exc))
        return 1


def _settings(args):
    return config.load_settings(
        tenant_id=getattr(args, "tenant", None),
        client_id=getattr(args, "client_id", None),
        mailbox=getattr(args, "mailbox", None),
        template_dir=getattr(args, "templates", None),
        pause=getattr(args, "pause", None),
    )


def _template_set(args) -> TemplateSet:
    return TemplateSet(_settings(args).template_dir)


def _report(lines) -> None:
    """Print what preflight or a run reported, each line at the level it came with."""
    for level, text in lines:
        logger.log(level.upper(), text)


# ---------------------------------------------------------------------------------------------
# send and validate
# ---------------------------------------------------------------------------------------------

def _build_run(args, settings, mode: str) -> SendRun:
    """Everything `send` and `validate` share: the template, the file, the attachment rules."""
    templates = TemplateSet(settings.template_dir)
    templates.ensure()
    template = templates.get(args.template)

    table = load_recipients(
        args.data,
        sheet=args.sheet,
        email_column=args.email_column,
        key_column=args.key_column,
    )

    # -- the template's own grouping is the default; a flag on the command line overrides it. A
    #    template that writes "your stands are 12, 14 and 19" is written for grouped rows, so it
    #    should not need the right flag typed alongside it every time.
    spec = GroupSpec.parse(
        args.group_by or template.meta.group_by,
        args.aggregate or [f"{column}={how}" for column, how in template.meta.aggregate.items()],
        separator=args.list_separator,
    )
    table = group_recipients(table, spec)

    root = Path(args.attachment_root).expanduser() if args.attachment_root else table.path.parent
    spec = AttachmentSpec(
        columns=[c.strip().lower().replace(" ", "_") for c in args.attachment_column],
        common=list(args.attach),
        # -- the template's own patterns first, then the ones typed for this run.
        patterns=list(template.meta.attachments) + list(args.attach_pattern),
        root=root.resolve(),
        allow_missing=args.allow_missing_attachments,
    )

    return SendRun(
        template=template,
        table=table,
        spec=spec,
        mode=mode,
        out_dir=getattr(args, "out", "out"),
        test_to=getattr(args, "test_to", None),
        resume=getattr(args, "resume", False),
        pause=settings.pause,
        limit=getattr(args, "limit", None),
        cc=getattr(args, "cc", None),
        bcc=getattr(args, "bcc", None),
        reply_to=getattr(args, "reply_to", None),
        mailbox=settings.mailbox,
        settings=settings,
    )


def cmd_validate(args) -> int:
    settings = _settings(args)
    run = _build_run(args, settings, MODE_DRY_RUN)
    found = run.preflight()

    if args.columns:
        logger.info("columns: {}", ", ".join(run.table.headers))
        used = sorted(run.template.variables() - {"row", "email", "attachments"})
        logger.info("the template uses: {}", ", ".join(used) or "no columns")
        unknown = [name for name in used if name not in run.table.headers]
        if unknown:
            logger.warning("not in the spreadsheet: {}", ", ".join(unknown))

    _report(found.summary_lines())
    return 0 if found.ok else 1


def cmd_send(args) -> int:
    settings = _settings(args)
    mode = MODE_SEND if args.do_send else (MODE_DRAFTS if args.drafts else MODE_DRY_RUN)

    # -- before the spreadsheet is read and long before anybody is asked to confirm. Asking "send
    #    seventy messages?", getting a yes, and only then saying there are no credentials wastes
    #    the one moment the person was paying full attention.
    if mode != MODE_DRY_RUN:
        settings.require_credentials()

    run = _build_run(args, settings, mode)
    found = run.preflight()
    _report(found.summary_lines())

    if not found.ok and mode != MODE_DRY_RUN:
        logger.error("Nothing sent. Fix the above, or leave off --send to look at a dry run.")
        return 1

    if not found.sendable:
        logger.warning("No rows to process.")
        return 1

    if mode != MODE_DRY_RUN and not args.yes and not _confirm(run, found, mode):
        logger.info("Nothing sent.")
        return 0

    handler = logs.add_run_log(run.out_dir)
    try:
        summary = run.execute(on_device_code=_print_device_code)
    finally:
        logs.remove(handler)

    return summary.exit_code


def _confirm(run: SendRun, found, mode: str) -> bool:
    """Ask once, naming the number, the template and where it is going.

    Only for drafts and sends, and only interactively -- with no terminal there is nobody to answer
    and `--yes` is how a script says so.
    """
    if not sys.stdin.isatty():
        logger.error("Not a terminal, so there is nobody to confirm. Pass --yes to go ahead.")
        return False

    where = f"all to {run.test_to} (rehearsal)" if run.test_to else "to the addresses in the file"
    verb = "Send" if mode == MODE_SEND else "Create drafts for"

    print()
    print(f"  {verb} {len(found.sendable)} message(s)")
    print(f"  template   {run.template.name}")
    print(f"  recipients {where}")
    if run.mailbox:
        # -- only when it is not your own mailbox: the line is here to make an unusual sender
        #    impossible to miss, and printing it every time is how it stops being read.
        print(f"  from       {run.mailbox}")
    if run.cc or run.bcc:
        print(f"  cc/bcc     {', '.join(run.cc + run.bcc)}")
    print()

    answer = input("Type yes to go ahead: ").strip().lower()
    return answer in ("yes", "y")


def _print_device_code(flow: dict) -> None:
    print()
    print(flow["message"])
    print(flush=True)


# ---------------------------------------------------------------------------------------------
# templates
# ---------------------------------------------------------------------------------------------

def cmd_templates(args) -> int:
    import os
    import subprocess

    templates = _template_set(args)
    action = args.action or "list"

    if action == "path":
        print(templates.ensure())
        return 0

    if action == "list":
        templates.ensure()
        found = templates.list()
        if not found:
            logger.warning("No templates in {}. Make one with `kasseimail templates new NAME`.",
                           templates.directory)
            return 0
        width = max(len(template.name) for template in found)
        for template in found:
            print(f"  {template.name:<{width}}  {template.describe()}")
        return 0

    if action == "new":
        template = templates.create(args.name, copy_from=args.copy_from)
        logger.info("created {}", template.directory)
        for part in PARTS:
            if (template.directory / part).is_file():
                print(f"  {template.directory / part}")
        return 0

    if action == "show":
        template = templates.get(args.name)
        parts = [args.part] if args.part else list(PARTS)
        for part in parts:
            body = template.read_part(part)
            if not body:
                continue
            print(f"# ----- {part} " + "-" * max(0, 60 - len(part)))
            print(body.rstrip())
            print()
        return 0

    if action == "edit":
        template = templates.get(args.name)
        path = template.directory / args.part
        path.touch(exist_ok=True)
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
        return subprocess.call([*editor.split(), str(path)])

    if action == "delete":
        template = templates.get(args.name)
        if not args.yes:
            if not sys.stdin.isatty():
                logger.error("Not a terminal. Pass --yes to delete {}.", args.name)
                return 1
            answer = input(f"Delete {template.directory} and everything in it? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("Left alone.")
                return 0
        templates.delete(args.name)
        logger.info("deleted {}", args.name)
        return 0

    logger.error("Unknown action: {}", action)
    return 1


# ---------------------------------------------------------------------------------------------
# account and configuration
# ---------------------------------------------------------------------------------------------

def cmd_login(args) -> int:
    settings = _settings(args)
    settings.require_credentials()

    # -- with the mailbox, so the one device code already covers the shared permissions. Signing
    #    in without it and configuring the mailbox afterwards means a second sign-in at the worst
    #    moment: in the middle of the first real run.
    mailer = GraphMailer(settings.tenant_id, settings.client_id, settings.token_cache,
                         mailbox=settings.mailbox)

    existing = mailer.cached_account()
    if existing:
        logger.info("already signed in as {}", existing.username)

    mailer.sign_in(on_device_code=_print_device_code)
    account = mailer.cached_account()
    logger.info("signed in as {}", account.username if account else "(unknown)")
    if settings.mailbox:
        logger.info("mail will be sent from {}", settings.mailbox)
    logger.info("token cached at {} (0600)", settings.token_cache)
    return 0


def cmd_logout(args) -> int:
    settings = _settings(args)
    mailer = GraphMailer(settings.tenant_id or "-", settings.client_id or "-",
                         settings.token_cache)
    if mailer.forget():
        logger.info("token cache removed: {}", settings.token_cache)
    else:
        logger.info("nothing cached; you were not signed in")
    return 0


def cmd_config(args) -> int:
    action = args.action or "show"

    if action == "path":
        print(config.config_path())
        return 0

    if action == "set":
        written = config.write_config_file({
            "tenant_id": args.tenant_id,
            "client_id": args.client_id,
            # -- an empty string is a value here and not "unset": it is how you say "send from my
            #    own mailbox again" without editing the file by hand.
            "mailbox": args.mailbox.strip() if args.mailbox is not None else None,
            "template_dir": args.template_dir,
            "pause": args.pause,
        })
        logger.info("written to {}", written)
        return 0

    settings = _settings(args)
    print(f"  config file    {config.config_path()}"
          f"{'' if config.config_path().is_file() else '  (does not exist yet)'}")
    print(f"  templates      {settings.template_dir}   [{settings.sources['template_dir']}]")
    print(f"  token cache    {settings.token_cache}"
          f"{'' if settings.token_cache.is_file() else '  (not signed in)'}")
    print(f"  tenant id      {settings.tenant_id or '(not set)'}   [{settings.sources['tenant_id']}]")
    print(f"  client id      {settings.client_id or '(not set)'}   [{settings.sources['client_id']}]")
    print(f"  send from      {settings.mailbox or '(your own mailbox)'}   "
          f"[{settings.sources['mailbox']}]")
    print(f"  pause          {settings.pause}s   [{settings.sources['pause']}]")

    if settings.tenant_id and settings.client_id:
        mailer = GraphMailer(settings.tenant_id, settings.client_id, settings.token_cache,
                             mailbox=settings.mailbox)
        account = mailer.cached_account()
        print(f"  signed in as   {account.username if account else '(nobody)'}")

    return 0


# ---------------------------------------------------------------------------------------------

def cmd_gui(args) -> int:
    """Hand over to the window. The Qt import happens in there, behind the guard."""
    config.require("PySide6")

    from kasseimail.ui.app import run_gui

    return run_gui(_settings(args))


if __name__ == "__main__":
    main()
