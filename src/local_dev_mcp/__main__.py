"""
Module Entrypoint for VSCode debugpy execution.

aliases:
    - ctrl + F5 (in VSCode)
    - uv run python -m local_dev_mcp
"""

from .cli import main

main()
