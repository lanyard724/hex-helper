"""Shared pytest fixtures for deterministic Qt top-level widget & timer teardown."""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _cleanup_qt_top_level_widgets() -> Iterator[None]:
    """Stop active timers/animations and close orphaned top-level Qt widgets after each test."""
    yield

    from PySide6.QtCore import QAbstractAnimation, QEvent, QTimer
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return

    try:
        from shiboken6 import isValid as _qt_is_valid
    except ImportError:  # pragma: no cover - fallback when shiboken6 helper unavailable
        def _qt_is_valid(_obj: object) -> bool:
            return True

    for widget in list(app.topLevelWidgets()):
        if widget is None:
            continue
        try:
            if not _qt_is_valid(widget):
                continue
            for timer in widget.findChildren(QTimer):
                try:
                    if _qt_is_valid(timer) and timer.isActive():
                        timer.stop()
                except RuntimeError:
                    pass
            for anim in widget.findChildren(QAbstractAnimation):
                try:
                    if _qt_is_valid(anim) and anim.state() != QAbstractAnimation.State.Stopped:
                        anim.stop()
                except RuntimeError:
                    pass
            widget.hide()
            widget.close()
            widget.deleteLater()
        except RuntimeError:
            pass

    app.sendPostedEvents(None, int(QEvent.Type.DeferredDelete))
    app.processEvents()
