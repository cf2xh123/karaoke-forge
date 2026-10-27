"""Run native widget tests without requiring a display server in CI."""

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def flush_qt_deferred_deletes():
    """Finish widget teardown even though tests do not run QApplication.exec()."""
    yield
    # Do not import or require Qt for CLI-only environments. Widget fixtures run
    # before this finalizer and queue deleteLater(); processEvents() alone does
    # not deliver those DeferredDelete events outside a normal Qt event loop.
    qt_core = sys.modules.get("PySide6.QtCore")
    if qt_core is not None and qt_core.QCoreApplication.instance() is not None:
        qt_core.QCoreApplication.sendPostedEvents(None, qt_core.QEvent.Type.DeferredDelete)
