"""Where things live, and which setting wins.

Three places can name the tenant, the client id and the mailbox to send from, and they are tried in
this order:

    1. the command line (`--tenant`, `--client-id`, `--mailbox`)
    2. the environment (`KASSEIMAIL_TENANT_ID`, `KASSEIMAIL_CLIENT_ID`, `KASSEIMAIL_MAILBOX`),
       `.env` included
    3. the config file (`kasseimail config path` prints it)

A flag beats an environment that beats a file, which is the order of how deliberate each one is:
the flag was typed for this run, the file was written months ago.

**The paths are absolute.** The tool this one grew out of kept its token cache at a relative
`data/graph_token.json`, so running it from another directory silently started a second device-code
login and left a stray `data/` tree behind. Everything here is anchored to the user's config and
data directories, so it does not matter where you stand when you run it.
"""

import importlib.util
import os
import tomllib

from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from platformdirs import user_config_dir, user_data_dir

# -- at import, so every entry point sees it: the CLI, the GUI, and a test that imports the module
#    directly. `find_dotenv` walks up from the cwd, which is what makes a per-project `.env` work.
load_dotenv(find_dotenv(usecwd=True))


APP_NAME = "kasseimail"

#: Exchange Online accepts 30 messages a minute on client submission. Seventy mails at full speed
#: runs straight into that, and the throttling that follows costs more than the pause would have.
DEFAULT_PAUSE_SECONDS = 2.5

#: what the templates are called inside a template directory. `subject.j2` is required; at least one
#: of the two bodies has to be there.
SUBJECT_FILE = "subject.j2"
HTML_FILE = "body.html.j2"
TEXT_FILE = "body.txt.j2"
META_FILE = "meta.toml"

#: Graph packs an inline attachment base64-encoded into the request body, and that request runs into
#: a limit near 4 MB. Base64 inflates a file by a third, so this is the ceiling on the *files*.
#: Above it the message goes the long way round -- a draft plus an upload session per file.
MAX_INLINE_TOTAL_BYTES = 3 * 1024 * 1024

#: what an upload session itself will carry. Graph's own ceiling for a mail attachment.
MAX_ATTACHMENT_BYTES = 150 * 1024 * 1024


def config_dir() -> Path:
    return Path(user_config_dir(APP_NAME))


def data_dir() -> Path:
    return Path(user_data_dir(APP_NAME))


def config_path() -> Path:
    return config_dir() / "config.toml"


def token_cache_path() -> Path:
    """The MSAL cache. Written 0600; there is a refresh token in it."""
    return data_dir() / "token.json"


def default_template_dir() -> Path:
    return data_dir() / "templates"


def read_config_file(path: Path | None = None) -> dict:
    """The config file as a dict, or an empty one. A broken file is a hard error.

    Not a silent fallback to the defaults: a typo in the TOML would then look exactly like a
    missing tenant id, and the message you would get is about signing in rather than about the
    line you mistyped.
    """
    target = Path(path) if path else config_path()
    if not target.is_file():
        return {}

    try:
        return tomllib.loads(target.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise SystemExit(f"Could not read {target}: {exc}")


@dataclass
class Settings:
    """Everything resolved, with the source of each value remembered for `kasseimail config show`."""

    tenant_id: str | None = None
    client_id: str | None = None
    #: the mailbox mail leaves from, when that is not the signed-in user's own. Empty means `/me`
    #: -- see `graph.GraphMailer`. Sending from another mailbox needs Send As or Send on Behalf on
    #: it, granted in Exchange, on top of the delegated scopes.
    mailbox: str = ""
    template_dir: Path = field(default_factory=default_template_dir)
    token_cache: Path = field(default_factory=token_cache_path)
    pause: float = DEFAULT_PAUSE_SECONDS
    sources: dict[str, str] = field(default_factory=dict)

    def require_credentials(self) -> None:
        """Refuse before the network is touched, and say where to put them."""
        missing = [
            name
            for name, value in (("tenant id", self.tenant_id), ("client id", self.client_id))
            if not value
        ]
        if not missing:
            return

        raise SystemExit(
            f"No {' and no '.join(missing)} configured.\n\n"
            "Set them in one of these, in order of precedence:\n"
            "    --tenant / --client-id on the command line\n"
            "    KASSEIMAIL_TENANT_ID / KASSEIMAIL_CLIENT_ID in the environment or a .env file\n"
            f"    {config_path()}\n\n"
            "The README, 'Setting up the app registration', says where to get them."
        )


def load_settings(
    *,
    tenant_id: str | None = None,
    client_id: str | None = None,
    mailbox: str | None = None,
    template_dir: str | Path | None = None,
    pause: float | None = None,
    config_file: Path | None = None,
) -> Settings:
    """Flags beat the environment beats the config file. Nothing here touches the network."""
    stored = read_config_file(config_file)
    settings = Settings()

    def pick(flag, env_name, stored_key, default):
        if flag is not None:
            return flag, "command line"
        env_value = os.getenv(env_name)
        if env_value:
            return env_value, env_name
        if stored.get(stored_key) is not None:
            return stored[stored_key], "config file"
        return default, "default"

    settings.tenant_id, settings.sources["tenant_id"] = pick(
        tenant_id, "KASSEIMAIL_TENANT_ID", "tenant_id", None
    )
    settings.client_id, settings.sources["client_id"] = pick(
        client_id, "KASSEIMAIL_CLIENT_ID", "client_id", None
    )

    raw_mailbox, settings.sources["mailbox"] = pick(
        mailbox, "KASSEIMAIL_MAILBOX", "mailbox", ""
    )
    settings.mailbox = (raw_mailbox or "").strip()

    directory, settings.sources["template_dir"] = pick(
        str(template_dir) if template_dir else None,
        "KASSEIMAIL_TEMPLATES",
        "template_dir",
        str(default_template_dir()),
    )
    settings.template_dir = Path(directory).expanduser().resolve()

    raw_pause, settings.sources["pause"] = pick(
        pause, "KASSEIMAIL_PAUSE", "pause", DEFAULT_PAUSE_SECONDS
    )
    settings.pause = float(raw_pause)

    settings.token_cache = token_cache_path()
    return settings


def write_config_file(values: dict, path: Path | None = None) -> Path:
    """Save the tenant and client id so the next run -- and the GUI -- find them.

    Hand-rolled rather than through a TOML writer: this file holds four scalars, and the standard
    library only reads TOML. A dependency to write eight lines is not worth it.
    """
    target = Path(path) if path else config_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    merged = read_config_file(target) | {k: v for k, v in values.items() if v is not None}

    lines = ["# kasseimail configuration. Written by `kasseimail config set`.", ""]
    for key, value in sorted(merged.items()):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            lines.append(f"{key} = {value}")
        else:
            lines.append(f'{key} = "{value}"')

    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


# ---------------------------------------------------------------------------------------------
# optional dependencies
# ---------------------------------------------------------------------------------------------

#: packages that only one part of the tool needs, with what for. They sit in a dependency group so
#: a headless machine running `kasseimail send` from cron does not install 100 MB of widgets.
OPTIONAL_REQUIREMENTS = {
    "PySide6": ("gui", "draws the desktop window"),
}


def require(*modules: str) -> None:
    """Refuse to start when an optional package is missing, and say which command installs it.

    `kasseimail gui` ships with every install and Qt does not, so on most machines the command
    exists and cannot work. Without this the first sign would be an ImportError from somewhere in
    the widget tree, which names a module rather than a thing to do.

    **`find_spec` and not `import`.** Importing PySide6 costs the better part of a second and this
    runs before any real work; the question here is only whether the package *is there*.
    """
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    if not missing:
        return

    groups = sorted({OPTIONAL_REQUIREMENTS[name][0] for name in missing})
    lines = ["kasseimail needs packages that are not part of the base install:", ""]
    lines += [f"    {name:<12} {OPTIONAL_REQUIREMENTS[name][1]}" for name in missing]
    lines += ["", "Install them with:", ""]
    lines += [f"    uv sync --group {group}" for group in groups]
    lines += [
        "",
        "They are kept apart on purpose: a machine that only sends mail from a script has no",
        "display, and Qt is a large download for a window it will never open.",
    ]
    raise SystemExit("\n".join(lines))
