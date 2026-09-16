"""The Graph client, in the parts that do not need a tenant.

Signing in for real needs Microsoft, so what is pinned here is everything around it: the values
people paste in, the errors that used to escape as tracebacks, and the one way out of a device flow
that is already polling.
"""

import time

import pytest

from kasseimail.graph import SCOPES, SHARED_SCOPES, GraphMailer, SignInProblem, describe_sender


def mailer(tenant="00000000-0000-0000-0000-000000000000", client="client-id", cache="token.json",
           mailbox=""):
    return GraphMailer(tenant, client, cache, mailbox=mailbox)


class FakeResponse:
    status_code = 200
    headers: dict = {}
    text = "{}"

    def json(self):
        return {"id": "AAMk-draft", "uploadUrl": "https://upload.example/session"}


class FakeSession:
    """Records the URLs instead of calling them. There is no tenant here to call."""

    def __init__(self):
        self.urls = []

    def request(self, method, url, **kwargs):
        self.urls.append((method, url))
        return FakeResponse()

    def put(self, url, **kwargs):
        self.urls.append(("PUT", url))
        return FakeResponse()


def signed_in(mailbox=""):
    """A mailer past the sign-in, without MSAL: a token that has not expired is simply used."""
    built = mailer(mailbox=mailbox)
    built._session = FakeSession()
    built._token = "an-access-token"
    built._expires_at = time.time() + 3600
    return built


# -- what people paste ---------------------------------------------------------------------------

def test_a_pasted_credential_keeps_no_whitespace_or_quotes():
    """These arrive by paste, out of a browser or a config file. A trailing newline makes MSAL
    reject the authority URL, and the error it gives is about URL formats -- which sends somebody
    checking the tenant id they just read off the screen and found correct."""
    built = GraphMailer(" 1234-abcd \n", '"client-id"', "token.json")

    assert built.tenant_id == "1234-abcd"
    assert built.client_id == "client-id"


def test_a_pasted_mailbox_keeps_no_whitespace_or_quotes():
    """It arrives by paste like the other two, and it goes into a URL path. A trailing space there
    is a 404 from Graph naming a mailbox that looks exactly right on screen."""
    assert mailer(mailbox=' "info@example.be" \n').mailbox == "info@example.be"


def test_no_credentials_at_all_is_refused_by_name():
    with pytest.raises(SignInProblem):
        GraphMailer("", "client", "token.json")

    with pytest.raises(SignInProblem):
        GraphMailer("tenant", "   ", "token.json")


# -- which mailbox the mail leaves from --------------------------------------------------------

def test_without_a_mailbox_every_call_still_goes_to_me():
    """The default is the one worth pinning. Every other failure in this tool announces itself;
    sending from the wrong mailbox does not -- the mail goes out, it just comes from somebody
    else, and nothing in the run or the report says so."""
    built = signed_in()

    built.send_direct({"subject": "x"})
    message_id = built.create_draft({"subject": "x"})
    built.send_draft(message_id)
    built.delete_message(message_id)

    assert [url for _, url in built._session.urls] == [
        "https://graph.microsoft.com/v1.0/me/sendMail",
        "https://graph.microsoft.com/v1.0/me/messages",
        f"https://graph.microsoft.com/v1.0/me/messages/{message_id}/send",
        f"https://graph.microsoft.com/v1.0/me/messages/{message_id}",
    ]


def test_a_mailbox_sends_drafts_and_uploads_through_that_mailbox(tmp_path):
    """The URL is the sender: a message carries no `from`, so `/users/info@.../` is the whole of
    sending as a shared mailbox. Miss one of these and the mail leaves from info@ while its draft
    or its large attachment goes to the signed-in user's own mailbox, where Graph answers 404 for
    a message id that exists -- in the other mailbox."""
    built = signed_in(mailbox="info@example.be")
    attachment = _attachment(tmp_path)

    built.send_direct({"subject": "x"})
    message_id = built.create_draft({"subject": "x"})
    built.upload_attachment(message_id, attachment)
    built.send_draft(message_id)

    base = "https://graph.microsoft.com/v1.0/users/info@example.be"
    assert [url for _, url in built._session.urls] == [
        f"{base}/sendMail",
        f"{base}/messages",
        f"{base}/messages/{message_id}/attachments/createUploadSession",
        "https://upload.example/session",
        f"{base}/messages/{message_id}/send",
    ]


def test_an_address_that_would_break_the_url_is_escaped():
    """A local part may hold a `#` or a `?`, and unescaped those end the path -- the request then
    goes to `/users/some` and Graph refuses something nobody typed."""
    assert signed_in(mailbox="a#b?c@example.be").base == (
        "https://graph.microsoft.com/v1.0/users/a%23b%3Fc@example.be"
    )


def test_the_shared_permissions_are_asked_for_only_when_a_mailbox_is_set():
    """`Mail.Send.Shared` is 'send mail on behalf of others', and nobody should have to consent to
    that to send their own mail. It is also the reason a run that starts using a shared mailbox
    asks for a device code once more."""
    assert mailer().scopes == SCOPES
    assert mailer(mailbox="info@example.be").scopes == SCOPES + SHARED_SCOPES


def test_a_refusal_from_a_shared_mailbox_says_where_the_rights_come_from():
    """Graph answers a bare 'Access is denied' whether the mailbox does not exist or was never
    shared with you -- and the app registration's permission is not what grants it. Without this
    the same six words come back once per row and the run reads as seventy problems."""
    hint = signed_in(mailbox="info@example.be")._mailbox_hint(403)

    assert "info@example.be" in hint
    assert "Send As" in hint or "Send on behalf" in hint
    # -- your own mailbox refusing is a different problem, and this advice would mislead.
    assert signed_in()._mailbox_hint(403) == ""
    assert signed_in(mailbox="info@example.be")._mailbox_hint(429) == ""


def test_the_sender_is_described_with_both_halves():
    """A confirmation naming only the shared mailbox hides which login is about to be used, and
    one naming only the login hides that the mail is not coming from it."""
    assert describe_sender("", "bino@example.be") == "bino@example.be"
    assert describe_sender("info@example.be", "bino@example.be") == (
        "info@example.be (signed in as bino@example.be)"
    )


def _attachment(tmp_path):
    from kasseimail.attachments import Attachment

    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF-1.4 a small one")
    return Attachment(path=path, size=path.stat().st_size)


# -- errors that used to escape --------------------------------------------------------------------

def test_an_authority_microsoft_rejects_becomes_a_readable_refusal(monkeypatch):
    """MSAL raises a bare ValueError for a tenant it cannot resolve. Unhandled it escapes into
    whatever asked for the account -- and in the window that is the constructor, so a mistyped
    tenant killed the application before it was on screen."""
    import msal

    def refuse(*args, **kwargs):
        raise ValueError("Unable to get authority configuration for ...")

    monkeypatch.setattr(msal, "PublicClientApplication", refuse)

    with pytest.raises(SignInProblem) as refusal:
        mailer(tenant="not-a-tenant").cached_account()

    message = str(refusal.value)
    assert "not-a-tenant" in message
    assert "Directory (tenant) ID" in message


def test_a_network_that_is_not_there_is_also_a_sign_in_problem(monkeypatch):
    """Same reasoning: on a train, asking who is signed in must not take the window down."""
    import msal

    def refuse(*args, **kwargs):
        raise OSError("Name or service not known")

    monkeypatch.setattr(msal, "PublicClientApplication", refuse)

    with pytest.raises(SignInProblem):
        mailer().cached_account()


# -- calling off a device flow ----------------------------------------------------------------------

def test_cancelling_a_device_flow_in_progress_stops_the_polling():
    """`acquire_token_by_device_flow` blocks for about fifteen minutes, and setting `expires_at`
    to 0 is MSAL's own documented way out of the loop. Without it, closing the window during a
    sign-in leaves a thread polling -- and destroying a running QThread turns a clean exit into an
    abort."""
    built = mailer()
    built._flow = {"user_code": "ABCD", "expires_at": time.time() + 900}

    assert built.cancel_sign_in() is True
    assert built._flow["expires_at"] == 0


def test_cancelling_when_nothing_is_signing_in_does_nothing():
    """The window calls this on close whether or not a sign-in is going."""
    assert mailer().cancel_sign_in() is False


def test_a_run_forwards_a_cancel_to_the_mailer_it_is_waiting_on(make_template, table, tmp_path):
    """Cancel during a run that has not reached its first message has nothing to stop between
    messages -- it is still waiting on the code."""
    from kasseimail.run import SendRun

    built = mailer()
    built._flow = {"expires_at": time.time() + 900}
    run = SendRun(template=make_template(), table=table, out_dir=tmp_path, mailer=built)

    assert run.cancel_sign_in() is True
    assert built._flow["expires_at"] == 0


def test_a_run_with_no_mailer_yet_shrugs_off_a_cancel(make_template, table, tmp_path):
    from kasseimail.run import SendRun

    run = SendRun(template=make_template(), table=table, out_dir=tmp_path)

    assert run.cancel_sign_in() is False
