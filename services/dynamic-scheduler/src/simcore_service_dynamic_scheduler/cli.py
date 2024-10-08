import logging

import typer
from settings_library.utils_cli import (
    create_settings_command,
    create_version_callback,
    print_as_envfile,
)

from ._meta import PROJECT_NAME, __version__
from .core.settings import ApplicationSettings

_logger = logging.getLogger(__name__)

main = typer.Typer(name=PROJECT_NAME)

main.command()(
    create_settings_command(settings_cls=ApplicationSettings, logger=_logger)
)
main.callback()(create_version_callback(__version__))


@main.command()
def echo_dotenv(ctx: typer.Context, *, minimal: bool = True):
    """Generates and displays a valid environment variables file (also known as dot-envfile)

    Usage:
        $ simcore-service-dynamic-scheduler echo-dotenv > .env
        $ cat .env
        $ set -o allexport; source .env; set +o allexport
    """
    assert ctx  # nosec

    settings = ApplicationSettings.create_from_envs()

    print_as_envfile(
        settings,
        compact=False,
        verbose=True,
        show_secrets=True,
        exclude_unset=minimal,
    )
