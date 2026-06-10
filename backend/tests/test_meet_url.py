import pytest

from app.services.meet_url import InvalidMeetUrl, build_meet_url, parse_native_meeting_id


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://meet.google.com/aex-ihfj-gvg", "aex-ihfj-gvg"),
        ("http://meet.google.com/abc-defg-hij", "abc-defg-hij"),
        ("meet.google.com/abc-defg-hij?authuser=0", "abc-defg-hij"),
        ("abc-defg-hij", "abc-defg-hij"),
        ("  https://meet.google.com/AEX-IHFJ-GVG  ", "aex-ihfj-gvg"),
        ("Join: https://meet.google.com/abc-defg-hij now", "abc-defg-hij"),
    ],
)
def test_parse_valid(value, expected):
    assert parse_native_meeting_id(value) == expected


@pytest.mark.parametrize("value", ["", "https://meet.google.com/", "not-a-code", "ab-cd-ef", None])
def test_parse_invalid(value):
    with pytest.raises(InvalidMeetUrl):
        parse_native_meeting_id(value)


def test_build_meet_url_roundtrip():
    assert build_meet_url("aex-ihfj-gvg") == "https://meet.google.com/aex-ihfj-gvg"
    assert build_meet_url("https://meet.google.com/aex-ihfj-gvg") == "https://meet.google.com/aex-ihfj-gvg"
