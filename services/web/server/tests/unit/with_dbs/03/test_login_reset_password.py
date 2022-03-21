# pylint: disable=redefined-outer-name
# pylint: disable=unused-argument
# pylint: disable=unused-variable

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pytest_simcore.helpers.utils_assert import assert_status
from pytest_simcore.helpers.utils_login import NewUser, parse_link, parse_test_marks
from simcore_service_webserver.db_models import ConfirmationAction, UserStatus
from simcore_service_webserver.login.settings import LoginOptions, get_plugin_options
from simcore_service_webserver.login.storage import AsyncpgStorage, get_plugin_storage
from simcore_service_webserver.login.utils import get_random_string
from yarl import URL

EMAIL, PASSWORD = "tester@test.com", "password"


@pytest.fixture
def client(
    event_loop: asyncio.AbstractEventLoop,
    aiohttp_client,
    web_server: TestServer,
    mock_orphaned_services,
) -> TestClient:
    cli = event_loop.run_until_complete(aiohttp_client(web_server))
    return cli


@pytest.fixture
def cfg(client: TestClient) -> LoginOptions:
    cfg = get_plugin_options(client.app)
    assert cfg
    return cfg


@pytest.fixture
def db(client: TestClient) -> AsyncpgStorage:
    db: AsyncpgStorage = get_plugin_storage(client.app)
    assert db
    return db


async def test_unknown_email(
    client: TestClient,
    cfg: LoginOptions,
    capsys,
):
    reset_url = client.app.router["auth_reset_password"].url_for()

    rp = await client.post(
        reset_url,
        json={
            "email": EMAIL,
        },
    )
    payload = await rp.text()

    assert rp.url.path == reset_url.path
    await assert_status(rp, web.HTTPOk, cfg.MSG_EMAIL_SENT.format(email=EMAIL))

    out, err = capsys.readouterr()
    assert parse_test_marks(out)["reason"] == cfg.MSG_UNKNOWN_EMAIL


async def test_banned_user(client: TestClient, cfg: LoginOptions, capsys):
    reset_url = client.app.router["auth_reset_password"].url_for()

    async with NewUser({"status": UserStatus.BANNED.name}, app=client.app) as user:
        rp = await client.post(
            reset_url,
            json={
                "email": user["email"],
            },
        )

    assert rp.url.path == reset_url.path
    await assert_status(rp, web.HTTPOk, cfg.MSG_EMAIL_SENT.format(**user))

    out, err = capsys.readouterr()
    assert parse_test_marks(out)["reason"] == cfg.MSG_USER_BANNED


async def test_inactive_user(client: TestClient, cfg: LoginOptions, capsys):
    reset_url = client.app.router["auth_reset_password"].url_for()

    async with NewUser(
        {"status": UserStatus.CONFIRMATION_PENDING.name}, app=client.app
    ) as user:
        rp = await client.post(
            reset_url,
            json={
                "email": user["email"],
            },
        )

    assert rp.url.path == reset_url.path
    await assert_status(rp, web.HTTPOk, cfg.MSG_EMAIL_SENT.format(**user))

    out, err = capsys.readouterr()
    assert parse_test_marks(out)["reason"] == cfg.MSG_ACTIVATION_REQUIRED


async def test_too_often(
    client: TestClient, cfg: LoginOptions, db: AsyncpgStorage, capsys
):
    reset_url = client.app.router["auth_reset_password"].url_for()

    async with NewUser(app=client.app) as user:
        confirmation = await db.create_confirmation(
            user, ConfirmationAction.RESET_PASSWORD.name
        )
        rp = await client.post(
            reset_url,
            json={
                "email": user["email"],
            },
        )
        await db.delete_confirmation(confirmation)

    assert rp.url.path == reset_url.path
    await assert_status(rp, web.HTTPOk, cfg.MSG_EMAIL_SENT.format(**user))

    out, err = capsys.readouterr()
    assert parse_test_marks(out)["reason"] == cfg.MSG_OFTEN_RESET_PASSWORD


async def test_reset_and_confirm(client: TestClient, cfg: LoginOptions, capsys):
    async with NewUser(app=client.app) as user:
        reset_url = client.app.router["auth_reset_password"].url_for()
        rp = await client.post(
            reset_url,
            json={
                "email": user["email"],
            },
        )
        assert rp.url.path == reset_url.path
        await assert_status(rp, web.HTTPOk, cfg.MSG_EMAIL_SENT.format(**user))

        out, err = capsys.readouterr()
        confirmation_url = parse_link(out)
        code = URL(confirmation_url).parts[-1]

        # emulates user click on email url
        rp = await client.get(confirmation_url)
        assert rp.status == 200
        assert (
            rp.url.path_qs
            == URL(cfg.LOGIN_REDIRECT)
            .with_fragment("reset-password?code=%s" % code)
            .path_qs
        )

        # api/specs/webserver/v0/components/schemas/auth.yaml#/ResetPasswordForm
        reset_allowed_url = client.app.router["auth_reset_password_allowed"].url_for(
            code=code
        )
        new_password = get_random_string(5, 10)
        rp = await client.post(
            reset_allowed_url,
            json={
                "password": new_password,
                "confirm": new_password,
            },
        )
        payload = await rp.json()
        assert rp.status == 200, payload
        assert rp.url.path == reset_allowed_url.path
        await assert_status(rp, web.HTTPOk, cfg.MSG_PASSWORD_CHANGED)
        # TODO: multiple flash messages

        # Try new password
        logout_url = client.app.router["auth_logout"].url_for()
        rp = await client.post(logout_url)
        assert rp.url.path == logout_url.path
        await assert_status(rp, web.HTTPUnauthorized, "Unauthorized")

        login_url = client.app.router["auth_login"].url_for()
        rp = await client.post(
            login_url,
            json={
                "email": user["email"],
                "password": new_password,
            },
        )
        assert rp.url.path == login_url.path
        await assert_status(rp, web.HTTPOk, cfg.MSG_LOGGED_IN)