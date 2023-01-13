# pylint: disable=redefined-outer-name
# pylint: disable=unused-argument
# pylint: disable=unused-variable

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from aiohttp_jinja2 import render_string
from faker import Faker
from json2html import json2html
from pytest_mock import MockerFixture
from pytest_simcore.helpers.typing_env import EnvVarsDict
from simcore_service_webserver._constants import RQ_PRODUCT_KEY
from simcore_service_webserver.application_settings import setup_settings
from simcore_service_webserver.email import setup_email
from simcore_service_webserver.login.plugin import setup_login
from simcore_service_webserver.login.utils_email import (
    AttachmentTuple,
    get_template_path,
    render_and_send_mail,
    themed,
)
from simcore_service_webserver.statics_constants import FRONTEND_APPS_AVAILABLE


@pytest.fixture
def mocked_send_email(mocker: MockerFixture) -> MagicMock:
    async def print_mail(cfg, message):
        print("EMAIL----------")
        print(message)
        print("---------------")

    return mocker.patch(
        "simcore_service_webserver.login.utils_email._send_mail",
        spec=True,
        side_effect=print_mail,
    )


@pytest.fixture
def app(app_environment: EnvVarsDict) -> web.Application:
    app_ = web.Application()

    assert setup_settings(app_)
    assert setup_login(app_)  # builds LoginOptions needed for _compose_email
    assert not setup_email(
        app_
    )  # NOTE: it is already init by setup_login, therefore 'setup_email' returns False
    return app_


@pytest.fixture
def http_request(app: web.Application, product_name: str) -> web.Request:
    request = make_mocked_request("GET", "/fake", app=app)
    request[RQ_PRODUCT_KEY] = product_name
    return request


@pytest.mark.parametrize("product_name", FRONTEND_APPS_AVAILABLE)
async def test_render_and_send_mail_for_registration(
    faker: Faker,
    mocked_send_email: MagicMock,
    product_name: str,
    http_request: web.Request,
):
    email = faker.email()  # destination email
    link = faker.url()  # some url link

    await render_and_send_mail(
        http_request,
        from_=f"no-reply@{product_name}.test",
        to=email,
        template=await get_template_path(http_request, "registration_email.jinja2"),
        context={
            "host": http_request.host,
            "link": link,
            "name": email.split("@")[0],
        },
    )

    assert mocked_send_email.called
    mimetext = mocked_send_email.call_args[1]["message"]
    assert mimetext["Subject"]
    assert mimetext["To"] == email


@pytest.mark.parametrize("product_name", FRONTEND_APPS_AVAILABLE)
async def test_render_and_send_mail_for_password(
    faker: Faker,
    mocked_send_email: MagicMock,
    product_name: str,
    http_request: web.Request,
):
    email = faker.email()  # destination email
    link = faker.url()  # some url link

    await render_and_send_mail(
        http_request,
        from_=f"no-reply@{product_name}.test",
        to=email,
        template=await get_template_path(
            http_request, "reset_password_email_failed.jinja2"
        ),
        context={
            "host": http_request.host,
            "reason": faker.text(),
        },
    )

    await render_and_send_mail(
        http_request,
        from_=f"no-reply@{product_name}.test",
        to=email,
        template=await get_template_path(http_request, "reset_password_email.jinja2"),
        context={
            "host": http_request.host,
            "link": link,
        },
    )


@pytest.mark.parametrize("product_name", FRONTEND_APPS_AVAILABLE)
async def test_render_and_send_mail_to_change_email(
    faker: Faker,
    mocked_send_email: MagicMock,
    product_name: str,
    http_request: web.Request,
):
    email = faker.email()  # destination email
    link = faker.url()  # some url link

    await render_and_send_mail(
        http_request,
        from_=f"no-reply@{product_name}.test",
        to=email,
        template=await get_template_path(http_request, "change_email_email.jinja2"),
        context={
            "host": http_request.host,
            "link": link,
        },
    )


@pytest.mark.parametrize("product_name", FRONTEND_APPS_AVAILABLE)
async def test_render_and_send_mail_for_submission(
    faker: Faker,
    mocked_send_email: MagicMock,
    product_name: str,
    http_request: web.Request,
):
    email = faker.email()  # destination email
    data = {"name": faker.first_name(), "surname": faker.last_name()}  # some form

    await render_and_send_mail(
        http_request,
        from_=f"no-reply@{product_name}.test",
        to=email,
        template=await get_template_path(http_request, "service_submission.jinja2"),
        context={
            "user": email,
            "data": json2html.convert(
                json=json.dumps(data), table_attributes='class="pure-table"'
            ),
            "subject": "TEST",
        },
        attachments=[
            AttachmentTuple(
                filename="test_login_utils.py",
                payload=bytearray(Path(__file__).read_bytes()),
            )
        ],
    )


@pytest.mark.skip(reason="DEV")
def test_render_string_from_tmp_file(
    tmp_path: Path, faker: Faker, app: web.Application
):
    http_request = make_mocked_request("GET", "/fake", app=app)

    template_path = themed("templates/osparc.io", "registration_email.jinja2")
    copy_path = tmp_path / template_path.name
    shutil.copy2(template_path, copy_path)

    context = {
        "host": http_request.host,
        "link": faker.url(),
        "name": faker.first_name(),
    }

    expected_page = render_string(
        template_name=f"{template_path}",
        request=http_request,
        context=context,
    )
    got_page = render_string(
        template_name=f"{copy_path}",
        request=http_request,
        context=context,
    )

    assert expected_page == got_page