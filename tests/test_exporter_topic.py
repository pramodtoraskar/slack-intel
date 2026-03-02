# tests/test_exporter_topic.py
import os, json, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
import exporter


def test_save_topic_report_creates_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    briefing = "## Key Updates\n- thing happened\n## Sources\n- #engineering"
    md_path, json_path = exporter.save_topic_report(
        "api-redesign", "API Redesign", briefing, sources=["engineering"], message_count=5
    )
    assert os.path.exists(md_path)
    assert os.path.exists(json_path)


def test_save_topic_report_json_structure(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    exporter.save_topic_report(
        "auth-service", "auth service", "briefing text",
        sources=["eng", "general"], message_count=12
    )
    json_path = os.path.join(str(tmp_path), "topics", "auth-service.json")
    with open(json_path) as f:
        d = json.load(f)
    assert d["topic"] == "auth service"
    assert d["slug"] == "auth-service"
    assert d["message_count"] == 12
    assert "eng" in d["sources"]
    assert "briefing text" in d["key_notes"]


def test_save_topic_report_writes_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    exporter.save_topic_report(
        "roadmap", "roadmap", "notes", sources=[], message_count=3
    )
    snapshot_dir = os.path.join(str(tmp_path), "topics", "roadmap")
    snapshots = os.listdir(snapshot_dir)
    assert len(snapshots) == 2  # .md + .json snapshot
