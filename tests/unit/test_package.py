from typer.testing import CliRunner

from intent_engineering import __version__
from intent_engineering.cli.app import app

runner = CliRunner()


def test_package_version_and_cli_help() -> None:
    assert __version__ == "0.1.0"
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Intent Engineering" in result.stdout
