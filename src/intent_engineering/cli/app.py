import typer

app = typer.Typer(
    name="intent",
    help="Intent Engineering: evidence-backed intent, drift, and reconciliation.",
    no_args_is_help=True,
)


@app.callback()
def default() -> None:
    """Run the Intent Engineering command group."""


def main() -> None:
    app()


if __name__ == "__main__":
    main()
