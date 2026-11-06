"""Public package import without eagerly loading application startup."""


def main() -> int:
    """Run the CLI only when the caller invokes the entry point."""
    from .app.cli import main as cli_main

    return cli_main()


__all__ = ["main"]
