from scripts.phone_transcript import summary


def test_failed_call_can_publish_speech_without_changing_verdict(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_SHA", "abc")
    monkeypatch.setenv("E2E_PASSCODE", "1234")
    (tmp_path / "transcript.txt").write_text("Incorrect passcode. 1234 </pre>")
    text = summary(tmp_path, {"result": "failed", "received_digits": None})
    assert "**failed**" in text and "**none**" in text
    assert "Incorrect passcode. [redacted] &lt;/pre&gt;" in text


def test_success_survives_missing_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_SHA", "abc")
    text = summary(tmp_path, {"result": "passed", "received_digits": "9"})
    assert "**passed**" in text and "**9**" in text
    assert "Transcript unavailable." in text
