"""
These tests can be run against external configuration

cd packages/notifications-library
pytest --external-envfile=.my-env --external-support-email=support@email.com  --external-user-email=my@email.com tests/email

"""

# pylint: disable=redefined-outer-name
# pylint: disable=unused-argument
# pylint: disable=unused-variable
# pylint: disable=too-many-arguments
# pylint: disable=too-many-return-statements


import functools
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from faker import Faker
from jinja2 import StrictUndefined
from models_library.api_schemas_webserver.auth import AccountRequestInfo
from models_library.products import ProductName
from models_library.utils.fastapi_encoders import jsonable_encoder
from notifications_library._email import (
    add_attachments,
    compose_email,
    create_email_session,
)
from notifications_library._email_render import render_email_parts
from notifications_library._models import ProductData, UserData
from notifications_library._render import create_render_env_from_package
from notifications_library.payments import PaymentData
from pydantic import EmailStr
from pydantic.json import pydantic_encoder
from pytest_simcore.helpers.typing_env import EnvVarsDict
from settings_library.email import SMTPSettings


def _safe_json_dumps(obj: Any, **kwargs):
    return json.dumps(jsonable_encoder(obj), default=pydantic_encoder, **kwargs)


@pytest.fixture
def ipinfo(faker: Faker) -> dict[str, Any]:
    return {
        "x-real-ip": faker.ipv4(),
        "x-forwarded-for": faker.ipv4(),
        "peername": faker.ipv4(),
    }


@pytest.fixture
def request_form(faker: Faker) -> dict[str, Any]:
    return AccountRequestInfo(
        **AccountRequestInfo.Config.schema_extra["example"]
    ).dict()


@pytest.fixture
def event_extra_data(  # noqa: PLR0911
    event_name: str,
    faker: Faker,
    product_name: ProductName,
    payment_data: PaymentData,
    product: dict[str, Any],
    request_form: dict[str, Any],
    ipinfo: dict[str, Any],
) -> dict[str, Any]:

    code = faker.pystr_format(string_format="######", letters="")
    host_url = f"https://{product_name}.io"

    match event_name:
        case "on_account_form":
            return {
                "host": host_url,
                "name": "support-team",
                "product_info": {
                    k: product.get(k)
                    for k in (
                        "name",
                        "display_name",
                        "support_email",
                        "vendor",
                        "is_payment_enabled",
                    )
                }
                | {"is_payment_enabled": faker.pybool()},
                "request_form": request_form,
                "ipinfo": ipinfo,
                "dumps": functools.partial(_safe_json_dumps, indent=1),
            }
        case "on_change_email":
            return {
                "host": host_url,
                "link": f"{host_url}?change-email={code}",
            }
        case "on_new_code":
            return {
                "host": host_url,
                "code": code,
            }
        case "on_new_invitation":
            return {
                "link": f"{host_url}?invitation={code}",
            }
        case "on_payed":
            return {
                "payment": payment_data,
            }
        case "on_registered":
            return {
                "host": host_url,
                "link": f"{host_url}?registration={code}",
            }

        case "on_reset_password":
            return {
                "host": host_url,
                "success": faker.pybool(),
                "reason": faker.sentence(),
                "link": f"{host_url}?reset-password={code}",
            }
        case "on_unregister":
            return {
                "host": host_url,
                "retention_days": 30,
            }

        case _:
            return {}


@pytest.fixture
def event_attachments(event_name: str, faker: Faker, tmp_path: Path) -> list[Path]:
    paths = []
    match event_name:
        case "on_payed":
            paths.append(tmp_path / "test-payed-invoice.pdf")

    # fill with fake data
    for p in paths:
        p.write_text(faker.text())
    return paths


@pytest.mark.parametrize(
    "event_name",
    [
        "on_account_form",
        "on_change_email",
        "on_new_code",
        "on_new_invitation",
        "on_payed",
        "on_registered",
        "on_reset_password",
        "on_unregister",
    ],
)
async def test_email_event(
    app_environment: EnvVarsDict,
    smtp_mock_or_none: MagicMock | None,
    user_data: UserData,
    user_email: EmailStr,
    product_data: ProductData,
    product_name: ProductName,
    event_name: str,
    event_extra_data: dict[str, Any],
    event_attachments: list[Path],
    tmp_path: Path,
):
    assert user_data.email == user_email
    assert product_data.product_name == product_name

    parts = render_email_parts(
        env=create_render_env_from_package(undefined=StrictUndefined),
        event_name=event_name,
        user=user_data,
        product=product_data,
        # extras
        **event_extra_data,
    )
    assert parts.from_.addr_spec == product_data.support_email
    assert parts.to.addr_spec == user_email

    msg = compose_email(*parts)
    if event_attachments:
        add_attachments(msg, event_attachments)

    # keep copy for comparison
    dump_path = tmp_path / event_name
    if parts.html_content:
        p = dump_path.with_suffix(".html")
        p.write_text(parts.html_content)
    if parts.text_content:
        p = dump_path.with_suffix(".txt")
        p.write_text(parts.text_content)

    async with create_email_session(settings=SMTPSettings.create_from_envs()) as smtp:
        await smtp.send_message(msg)

    # check email was sent
    if smtp_mock_or_none:
        assert smtp_mock_or_none.called
        assert isinstance(smtp, AsyncMock)
        assert smtp.login.called
        assert smtp.send_message.called