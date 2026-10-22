"""Picker failures must release the global gate and preserve stable errors."""

import pytest

from learning_app import course_files
from learning_app.errors import AppError


def test_picker_os_failure_releases_gate_for_next_explicit_attempt(monkeypatch):
    def fail(**_kwargs):
        raise OSError("synthetic COM initialization failure")

    monkeypatch.setattr(course_files, "pick_paths", fail)
    with pytest.raises(AppError) as failure:
        course_files.choose_paths(directory=True)
    assert failure.value.code == "PICKER_UNAVAILABLE"
    assert isinstance(failure.value.__cause__, OSError)
    monkeypatch.setattr(course_files, "pick_paths", lambda **_kwargs: [])
    assert course_files.choose_paths(directory=True) == []


def test_picker_busy_does_not_open_another_native_dialog(monkeypatch):
    def forbidden(**_kwargs):
        pytest.fail("a second native dialog must not open")

    monkeypatch.setattr(course_files, "pick_paths", forbidden)
    assert course_files._PICKER.acquire(blocking=False)
    try:
        with pytest.raises(AppError) as failure:
            course_files.choose_paths(directory=True)
        assert failure.value.code == "PICKER_ACTIVE"
    finally:
        course_files._PICKER.release()


def test_picker_known_error_is_preserved_and_next_cancel_can_complete(monkeypatch):
    def fail(**_kwargs):
        raise AppError("FILE_BATCH_TOO_LARGE", "synthetic limit", 422)

    monkeypatch.setattr(course_files, "pick_paths", fail)
    with pytest.raises(AppError) as failure:
        course_files.choose_paths(directory=False)
    assert failure.value.code == "FILE_BATCH_TOO_LARGE"
    monkeypatch.setattr(course_files, "pick_paths", lambda **_kwargs: [])
    assert course_files.choose_paths(directory=False) == []
