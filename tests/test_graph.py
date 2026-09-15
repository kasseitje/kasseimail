"""The Graph client, in the parts that do not need a tenant.

Signing in for real needs Microsoft, so what is pinned here is everything around it: the values
people paste in, the errors that used to escape as tracebacks, and the one way out of a device flow
that is already polling.
"""

import time

import pytest

from kasseimail.graph import GraphMailer, SignInProblem


def mailer(tenant="00000000-0000-0000-0000-000000000000", client="client-id", cache="token.json"):
    return GraphMailer(tenant, client, cache)


# -- what people paste ---------------------------------------------------------------------------

def test_a_pasted_credential_keeps_no_whitespace_or_quotes():
    """These arrive by paste, out of a browser or a config file. A trailing newline makes MSAL
    reject the authority URL, and the error it gives is about URL formats -- which sends somebody
    checking the tenant id they just read off the screen and found correct."""
    built = GraphMailer(" 1234-abcd \n", '"client-id"', "token.json")

    assert built.tenant_id == "1234-abcd"
    assert built.client_id == "client-id"


def test_no_credentials_at_all_is_refused_by_name():
    with pytest.raises(SignInProblem):
        GraphMailer("", "client", "token.json")

    with pytest.raises(SignInProblem):
        GraphMailer("tenant", "   ", "token.json")


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
