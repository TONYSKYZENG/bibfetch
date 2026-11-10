"""PyQt5 desktop front-end and project persistence for bibfetch."""

from .app import MainWindow, main
from .project import EntryRecord, Project

__all__ = ["__version__", "MainWindow", "Project", "EntryRecord", "main"]

__version__ = "1.0.0"
