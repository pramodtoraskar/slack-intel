# tests/test_build_topic_digest.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import analyzer


def test_topic_prompt_includes_topic():
    prompt = analyzer._topic_prompt("auth service", "msg1\nmsg2", prior_notes=None)
    assert "auth service" in prompt


def test_topic_prompt_no_prior_notes():
    prompt = analyzer._topic_prompt("auth service", "msg1\nmsg2", prior_notes=None)
    assert "Prior Intelligence" not in prompt


def test_topic_prompt_with_prior_notes():
    prompt = analyzer._topic_prompt(
        "auth service", "msg1\nmsg2",
        prior_notes="old notes here"
    )
    assert "Prior Intelligence" in prompt
    assert "old notes here" in prompt


def test_topic_prompt_includes_messages():
    prompt = analyzer._topic_prompt("X", "alpha\nbeta", prior_notes=None)
    assert "alpha" in prompt
    assert "beta" in prompt


def test_slugify():
    assert analyzer._slugify("API Redesign!") == "api-redesign"
    assert analyzer._slugify("auth service") == "auth-service"
    assert analyzer._slugify("Q2  roadmap") == "q2-roadmap"
