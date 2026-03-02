# tests/test_dashboard_topics.py
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import dashboard


def test_get_topics_empty(tmp_path):
    topics_dir = tmp_path / "topics"
    topics_dir.mkdir()
    result = dashboard._get_topics(str(tmp_path))
    assert result == []


def test_get_topics_finds_json(tmp_path):
    topics_dir = tmp_path / "topics"
    topics_dir.mkdir()
    (topics_dir / "api-redesign.json").write_text(
        '{"topic":"API Redesign","slug":"api-redesign","generated_at":"2026-02-28T10:00:00+00:00","sources":["eng"],"message_count":5,"key_notes":"notes"}'
    )
    result = dashboard._get_topics(str(tmp_path))
    assert len(result) == 1
    assert result[0]["slug"] == "api-redesign"
    assert result[0]["topic"] == "API Redesign"


def test_slugify_roundtrip():
    import analyzer
    slug = analyzer._slugify("API Redesign")
    assert slug == "api-redesign"
