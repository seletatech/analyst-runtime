"""
Entry point for running analyst_runtime as a module: python -m analyst_runtime
"""

from analyst_runtime.cli.commands import app

if __name__ == "__main__":
    app()
