# tests/test_query_digest.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import query


def test_parse_digest_command():
    topic, channel, mode = query.parse_digest_input("/digest API redesign")
    assert topic == "API redesign"
    assert channel is None
    assert mode == "persistent"


def test_parse_digest_fresh():
    topic, channel, mode = query.parse_digest_input("/digest --fresh auth service")
    assert topic == "auth service"
    assert channel is None
    assert mode == "fresh"


def test_parse_digest_with_channel():
    topic, channel, mode = query.parse_digest_input("#engineering /digest auth service")
    assert topic == "auth service"
    assert channel == "engineering"
    assert mode == "persistent"


def test_is_digest_command():
    assert query.is_digest_command("/digest anything") is True
    assert query.is_digest_command("/digest --fresh X") is True
    assert query.is_digest_command("#ch /digest X") is True
    assert query.is_digest_command("what is X?") is False
    assert query.is_digest_command("#ch some question") is False


def test_parse_digest_strips_whitespace():
    topic, _, _ = query.parse_digest_input("  /digest   my topic  ")
    assert topic == "my topic"
