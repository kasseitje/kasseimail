"""Signing in to Microsoft 365, and handing messages to Graph.

**Delegated, not app-only.** You sign in once with a device code as yourself, the mail leaves your
mailbox and lands in your own Sent Items -- so "I never got it" is a question you can answer. An
app-only registration would need an administrator to grant `Mail.Send` as an *application*
permission, and that permission reaches every mailbox in the tenant unless somebody remembers to
scope it.

**Graph and not SMTP.** SMTP AUTH with basic authentication still works today, but Microsoft turns
it off by default for existing tenants at the end of 2026 and removes it after 2027. Graph also
needs no per-mailbox SMTP AUTH, files the message in Sent Items, and can leave a draft instead of
sending -- which is what makes a rehearsal possible.

**The URL is the sender.** A message carries no `from`: `/me/sendMail` leaves your own mailbox and
`/users/info@example.be/sendMail` leaves that one. Which of the two you get is `mailbox`, and
everything else about the request is identical, so a shared mailbox is one substitution rather than
a second code path. Exchange decides the header from the rights you actually hold -- Send As puts
the shared mailbox in `From:`, Send on Behalf puts you there "on behalf of" it -- and those rights
are granted on the mailbox itself, which no Graph scope can stand in for.
"""

import os
import time

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from loguru import logger

from kasseimail.attachments import Attachment
from kasseimail.message import FILE_ATTACHMENT, _content_type

GRAPH = "https://graph.microsoft.com/v1.0"

#: `Mail.Send` sends, `Mail.ReadWrite` leaves a draft. Both delegated.
SCOPES = ["Mail.Send", "Mail.ReadWrite"]

#: the same two for a mailbox that is not yours, asked for **only when one is configured**. Nobody
#: should have to consent to "send mail on behalf of others" to send their own mail, which is the
#: same reasoning as the module docstring's case against an app-only registration. The cost is one
#: extra consent the first time a mailbox is set; afterwards MSAL redeems the cached refresh token
#: for either set without prompting again.
SHARED_SCOPES = ["Mail.Send.Shared", "Mail.ReadWrite.Shared"]

#: how often a message is retried after a 429 or a 503.
MAX_RETRIES = 4

#: re-ask for a token when this little of its life is left. A run of several hundred throttled
#: messages outlives one token, and the tool it grew out of took a single token before the loop and
#: would 401 halfway through with no way back.
TOKEN_REFRESH_MARGIN = 300

#: Graph wants upload chunks a multiple of 320 KiB. This is 5 MiB, well inside the 60 MiB ceiling.
UPLOAD_CHUNK = 16 * 320 * 1024


class GraphProblem(RuntimeError):
    """Graph refused something. The message is the one Graph gave, unwrapped."""


class SignInProblem(RuntimeError):
    """Signing in did not finish. Never raised for a network blip -- that is a GraphProblem."""


@dataclass
class Account:
    """Who the cached token belongs to, for a window to show and a CLI to print."""

    username: str
    tenant_id: str


class GraphMailer:
    """A signed-in connection to one mailbox."""

    def __init__(self, tenant_id: str, client_id: str, cache_path: str | Path,
                 mailbox: str = ""):
        # -- stripped, because these arrive by paste. A trailing newline out of a browser or a
        #    stray quote out of a config file makes MSAL reject the authority URL, and the error
        #    it gives is about URL formats rather than about the invisible character.
        tenant_id = (tenant_id or "").strip().strip("\"'")
        client_id = (client_id or "").strip().strip("\"'")

        if not tenant_id or not client_id:
            raise SignInProblem("No tenant id or client id; see `kasseimail config show`.")

        self.tenant_id = tenant_id
        self.client_id = client_id
        self.cache_path = Path(cache_path)
        #: whose mailbox this acts on. Empty is the signed-in user's own -- and that default is
        #: worth keeping in mind, because getting it wrong does not fail: the mail goes out, from
        #: the wrong address, and nothing in the run says so.
        self.mailbox = (mailbox or "").strip().strip("\"'")
        self.scopes = SCOPES + SHARED_SCOPES if self.mailbox else list(SCOPES)

        self._app = None
        self._cache = None
        self._token = None
        self._expires_at = 0.0
        self._session = None
        # -- the in-flight device flow, kept so another thread can call off the polling. See
        #    `cancel_sign_in`.
        self._flow = None
        #: set by a run so that waiting out a 429 can be given up on. Graph can ask for minutes.
        self.should_cancel = None

    # -- signing in ---------------------------------------------------------------------------

    def _build_app(self):
        """The MSAL app and its cache. Kept on the instance so a token can be renewed mid-run."""
        import msal

        if self._app is not None:
            return self._app

        self._cache = msal.SerializableTokenCache()
        if self.cache_path.is_file():
            try:
                self._cache.deserialize(self.cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                # -- a truncated cache is a reason to sign in again, not to stop.
                logger.warning("token cache unreadable ({}); signing in again", exc)

        # -- MSAL fetches the authority's OpenID configuration here, so this is where a wrong
        #    tenant id and an unreachable network both first show up -- as a ValueError naming URL
        #    formats. Left alone it escapes into whatever called us, which in the window is the
        #    constructor, and the window dies before it is on screen.
        try:
            self._app = msal.PublicClientApplication(
                self.client_id,
                authority=f"https://login.microsoftonline.com/{self.tenant_id}",
                token_cache=self._cache,
            )
        except ValueError as exc:
            raise SignInProblem(
                f"Microsoft would not accept the tenant '{self.tenant_id}'.\n\n"
                "It has to be the Directory (tenant) ID from the app registration's overview "
                "page -- a GUID -- or the tenant's domain, like contoso.onmicrosoft.com.\n\n"
                "If it looks right, check that this machine can reach "
                "login.microsoftonline.com.\n\n"
                f"Microsoft said: {exc}"
            ) from exc
        except Exception as exc:
            raise SignInProblem(f"Could not reach Microsoft to sign in: {exc}") from exc

        return self._app

    def cached_account(self) -> Account | None:
        """Who is signed in, without asking anybody to sign in. `None` if nobody is."""
        accounts = self._build_app().get_accounts()
        if not accounts:
            return None
        return Account(username=accounts[0].get("username", "unknown"), tenant_id=self.tenant_id)

    def sign_in(self, on_device_code=None, silent_only: bool = False) -> "GraphMailer":
        """A token, from the cache if possible and from a device code if not.

        `on_device_code` receives the flow dict -- it carries `message`, `user_code` and
        `verification_uri`. The CLI prints the message; the window opens a dialog with the code in
        it. Doing this through a callback rather than a `print` is the whole reason the GUI can use
        this class at all.

        `acquire_token_by_device_flow` **blocks** until the code is entered or expires, so the
        caller decides which thread it happens on. In the window that is the worker thread.
        """
        import requests

        self._session = requests.Session()
        app = self._build_app()

        accounts = app.get_accounts()
        result = app.acquire_token_silent(self.scopes, account=accounts[0]) if accounts else None

        if not result:
            if silent_only:
                raise SignInProblem("Not signed in. Run `kasseimail login` first.")
            result = self._device_flow(app, on_device_code)

        if "access_token" not in result:
            raise SignInProblem(
                "Sign-in failed: " + result.get("error_description", str(result))
            )

        self._remember(result)
        self._save_cache()
        return self

    def _device_flow(self, app, on_device_code) -> dict:
        flow = app.initiate_device_flow(scopes=self.scopes)
        if "user_code" not in flow:
            raise SignInProblem(
                "Could not start the device code flow: "
                + flow.get("error_description", str(flow))
                + "\n\nIs 'Allow public client flows' set to Yes on the app registration? "
                "Without it Entra refuses the device code with AADSTS7000218."
            )

        if on_device_code is not None:
            on_device_code(flow)
        else:
            print("\n" + flow["message"] + "\n", flush=True)

        self._flow = flow
        try:
            # -- blocks, polling, until the code is used or expires: about fifteen minutes. The
            #    caller decides which thread that happens on; in the window it is a worker.
            return app.acquire_token_by_device_flow(flow)
        finally:
            self._flow = None

    def cancel_sign_in(self) -> bool:
        """Call off a device flow that is polling. Safe to call from another thread.

        Setting `expires_at` to 0 is MSAL's own documented way to abort the loop -- it checks the
        key between polls. Without it there is no way to stop: closing the window during a sign-in
        would leave a thread polling for a quarter of an hour, and destroying a running QThread is
        how a clean exit turns into an abort.

        It takes effect on the next poll, so within about five seconds rather than instantly.
        """
        flow = self._flow
        if flow is None:
            return False
        flow["expires_at"] = 0
        return True

    def _remember(self, result: dict) -> None:
        self._token = result["access_token"]
        self._expires_at = time.time() + float(result.get("expires_in", 3600))

    def _save_cache(self) -> None:
        """The cache, readable by nobody else.

        There is a refresh token in this file. Anybody who can read it can send mail as you for as
        long as it lives, so it gets the same 0600 a private key would.
        """
        if self._cache is None or not self._cache.has_state_changed:
            return

        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(self._cache.serialize(), encoding="utf-8")
        os.chmod(self.cache_path, 0o600)

    def forget(self) -> bool:
        """Delete the cached token. Returns whether there was one."""
        if not self.cache_path.is_file():
            return False
        self.cache_path.unlink()
        self._app = self._cache = self._token = None
        return True

    def _fresh_token(self) -> str:
        """The access token, renewed silently when it is about to expire."""
        if self._token and time.time() < self._expires_at - TOKEN_REFRESH_MARGIN:
            return self._token

        app = self._build_app()
        accounts = app.get_accounts()
        result = app.acquire_token_silent(self.scopes, account=accounts[0]) if accounts else None

        if not result or "access_token" not in result:
            if self._token and time.time() < self._expires_at:
                # -- renewal failed but what we hold is still valid; use it and try again later.
                logger.warning("could not renew the token; carrying on with the current one")
                return self._token
            raise SignInProblem("The session expired and could not be renewed. Sign in again.")

        logger.debug("access token renewed")
        self._remember(result)
        self._save_cache()
        return self._token

    # -- talking to Graph ---------------------------------------------------------------------

    @property
    def base(self) -> str:
        """What every call hangs off: your own mailbox, or the one you were given rights on.

        This one substitution is the whole of sending from a shared mailbox. `quote` because an
        address is a path segment here, and a `#` or a `+` in a local part would otherwise end the
        path or become a space.
        """
        if not self.mailbox:
            return f"{GRAPH}/me"
        return f"{GRAPH}/users/{quote(self.mailbox, safe='@.')}"

    def _request(self, method: str, url: str, **kwargs):
        """One call, honouring `Retry-After`.

        Graph throttles with a 429 and Exchange Online with a 503, both naming the seconds to wait.
        Pushing through makes it worse, and a run that dies halfway is exactly what the ledger in
        `ledger.py` exists to recover from -- better not to need it.
        """
        headers = kwargs.pop("headers", {}) | {"Authorization": f"Bearer {self._fresh_token()}"}

        response = None
        for attempt in range(MAX_RETRIES):
            response = self._session.request(method, url, headers=headers, timeout=120, **kwargs)
            if response.status_code not in (429, 503):
                break

            wait = float(response.headers.get("Retry-After", 2 ** attempt * 5))
            logger.warning("throttled by Graph, waiting {:.0f}s", wait)

            # -- sliced, so a cancel lands during the wait rather than after it. Graph is entitled
            #    to ask for several minutes, and that is otherwise several minutes in which the
            #    Cancel button and the window's close box both appear to do nothing.
            from kasseimail.run import interruptible_sleep

            if not interruptible_sleep(wait, self.should_cancel):
                raise GraphProblem("cancelled while waiting out Graph's throttling")

        if response.status_code >= 400:
            raise GraphProblem(
                f"Graph {response.status_code}: {_graph_error(response)}"
                + self._mailbox_hint(response.status_code)
            )
        return response

    def _mailbox_hint(self, status: int) -> str:
        """Why a shared mailbox refuses, added once to the error rather than guessed at.

        Graph answers "Access is denied" whether the mailbox does not exist, or exists and has not
        been shared with you -- and the delegated scope is not what grants that; the rights are
        granted on the mailbox itself. Without this the same six words come back once per row, and
        the run looks like seventy separate failures instead of one misconfiguration.
        """
        if not self.mailbox or status not in (403, 404):
            return ""
        return (
            f"\n\nThis was addressed to {self.mailbox}, which is not the mailbox you signed in "
            "with. Sending from it needs 'Send As' or 'Send on behalf' granted on the mailbox "
            "itself, in the Exchange admin centre -- the Mail.Send.Shared permission on the app "
            "registration does not grant it, and a right just granted can take a while to take "
            "effect.\nLeave the mailbox empty to send from your own again."
        )

    def _post(self, url: str, payload: dict):
        return self._request("POST", url, json=payload,
                             headers={"Content-Type": "application/json"})

    def send_direct(self, message: dict) -> str:
        """Send in one request. Returns an empty id -- a 202 carries no body to take one from."""
        self._post(f"{self.base}/sendMail", {"message": message, "saveToSentItems": True})
        return ""

    def create_draft(self, message: dict) -> str:
        """Leave the message in Drafts. Returns its id, which is how to find it in Outlook.

        In *that mailbox's* Drafts, when one is set: a draft has to live where it will be sent
        from, which is also what makes a shared mailbox reviewable by the people who share it.
        """
        response = self._post(f"{self.base}/messages", message)
        return (response.json() or {}).get("id", "")

    def send_draft(self, message_id: str) -> None:
        self._post(f"{self.base}/messages/{message_id}/send", {})

    def delete_message(self, message_id: str) -> None:
        """Clean up a draft whose attachments failed to upload.

        Without this, a run that trips on one large file leaves half-built drafts behind, and the
        person recovering has to tell them apart from the ones they meant to keep.
        """
        try:
            self._request("DELETE", f"{self.base}/messages/{message_id}")
        except GraphProblem as exc:
            logger.warning("could not remove the incomplete draft {}: {}", message_id, exc)

    # -- large attachments --------------------------------------------------------------------

    def upload_attachment(self, message_id: str, attachment: Attachment, on_chunk=None) -> None:
        """Push one file to a draft through an upload session.

        Used when the files together pass what fits in a request body. The session URL is
        pre-authorised, so the chunk PUTs carry no bearer token -- sending one is what makes Graph
        answer 401 to an otherwise correct upload.
        """
        session = self._post(
            f"{self.base}/messages/{message_id}/attachments/createUploadSession",
            {
                "AttachmentItem": {
                    "attachmentType": "file",
                    "name": attachment.name,
                    "size": attachment.size,
                    "contentType": _content_type(attachment),
                }
            },
        ).json()

        upload_url = session["uploadUrl"]
        total = attachment.size

        with attachment.path.open("rb") as handle:
            sent = 0
            while sent < total:
                chunk = handle.read(UPLOAD_CHUNK)
                if not chunk:
                    break

                last = sent + len(chunk) - 1
                response = self._session.put(
                    upload_url,
                    data=chunk,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {sent}-{last}/{total}",
                    },
                    timeout=300,
                )
                if response.status_code >= 400:
                    raise GraphProblem(
                        f"uploading {attachment.name}: {response.status_code} "
                        f"{_graph_error(response)}"
                    )

                sent = last + 1
                if on_chunk is not None:
                    on_chunk(sent, total)

        logger.debug("uploaded {} ({} bytes) to draft {}", attachment.name, total, message_id)

    def deliver(self, delivery, *, send: bool, on_chunk=None) -> str:
        """One mail, whichever way it has to go. Returns the message id, empty for a direct send."""
        if not delivery.needs_upload_session:
            return self.send_direct(delivery.message) if send else self.create_draft(delivery.message)

        message_id = self.create_draft(delivery.message)
        try:
            for attachment in delivery.upload:
                self.upload_attachment(message_id, attachment, on_chunk=on_chunk)
            if send:
                self.send_draft(message_id)
        except Exception:
            self.delete_message(message_id)
            raise

        return message_id


def describe_sender(mailbox: str, account_name: str) -> str:
    """Who the mail will come from, wherever that has to be shown beside the account.

    Both halves, because both matter: the address that will be on the mail, and the account whose
    rights put it there. A window titled only "info@..." hides which login is about to be used,
    and one titled only with the login hides that the mail is not coming from it.
    """
    if not mailbox:
        return account_name
    return f"{mailbox} (signed in as {account_name})"


def _graph_error(response) -> str:
    """The message out of Graph's error envelope, or the first of whatever it did send."""
    try:
        return response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return (response.text or "")[:200]


def attachment_summary(message: dict) -> str:
    """The attachment names in a built message, for a log line."""
    return ", ".join(
        item.get("name", "?") for item in message.get("attachments", [])
        if item.get("@odata.type") == FILE_ATTACHMENT
    )
