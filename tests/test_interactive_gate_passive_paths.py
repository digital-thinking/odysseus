"""Regression for idle UI polls that must not count as foreground activity."""

from src.interactive_gate import should_track_interactive_request


def test_email_unread_state_is_passive_like_urgency_state():
    assert should_track_interactive_request("/api/email/urgency-state") is False
    assert should_track_interactive_request("/api/email/unread-state") is False


def test_real_interactive_paths_still_tracked():
    assert should_track_interactive_request("/api/chat_stream") is True
    assert should_track_interactive_request("/api/email/messages") is True
    assert should_track_interactive_request("/api/tasks", method="POST") is True


def test_options_never_tracked():
    assert should_track_interactive_request("/api/email/unread-state", method="OPTIONS") is False
    assert should_track_interactive_request("/api/chat_stream", method="OPTIONS") is False


def test_background_polls_marked_passive_do_not_count_as_activity():
    """The calendar reminder poller runs every 60 s in every open tab; it must
    not stop scheduled background tasks."""
    assert should_track_interactive_request("/api/notes", "GET", passive_poll=True) is False
    assert should_track_interactive_request("/api/notes", "GET") is True
    # only reads can be marked background
    assert should_track_interactive_request("/api/notes", "POST", passive_poll=True) is True
