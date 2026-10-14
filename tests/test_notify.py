"""Outbound notifications: channel resolution, event discovery from receipts + queue,
payload shapes, the file transport, and the dedup watermark across repeated scans."""
from collectors import notify, decisions as D


def test_channel_config_autodetect():
    assert notify.channel_config({"FOREMAN_NOTIFY_WEBHOOK": "https://hooks.slack.com/x"})["kind"] == "slack"
    assert notify.channel_config(
        {"FOREMAN_NOTIFY_WEBHOOK": "https://discord.com/api/webhooks/1/x"})["kind"] == "discord"
    assert notify.channel_config({"FOREMAN_NOTIFY_WEBHOOK": "https://example.com/hook"})["kind"] == "generic"
    assert notify.channel_config({"FOREMAN_NOTIFY_CHANNEL": "slack",
                                  "FOREMAN_NOTIFY_WEBHOOK": "https://x.test/h"})["kind"] == "slack"
    # file target needs no secret; empty env => inert
    assert notify.channel_config({"FOREMAN_NOTIFY_FILE": "/tmp/x.log"})["kind"] == "file"
    assert notify.channel_config({})["kind"] == "none"


def test_build_events_reds_and_approvals(state_dir, foreman_dir, make_receipt, now):
    # a green pair (ignored) and a red pair (surfaced)
    make_receipt("acmeapi", "docs-sync", "2026-09-06T10:00:00Z", "green")
    make_receipt("acmeapi", "security-review", "2026-09-06T09:00:00Z", "red",
                 next_action="triage findings")
    # a queued approval
    D.enqueue(state_dir, "acmeapi", "approve_push",
              {"branch": "foreman/fix", "base": "main"}, "op")

    events = notify.build_events(state_dir, foreman_dir, now=now)
    kinds = {(e["project"], e["cadence"], e["kind"]) for e in events}
    assert ("acmeapi", "security-review", "red") in kinds
    assert not any(e["cadence"] == "docs-sync" for e in events)      # green excluded
    assert any(e["kind"] == "approval" for e in events)


def test_render_slack_has_fallback_text_and_button(now):
    ev = {"kind": "red", "id": "r1", "project": "p", "cadence": "c", "label": "red 2d",
          "reason": "bad", "next_action": "fix it"}
    payload = notify.render_slack(ev, "http://host:8787")
    assert payload["text"]                                            # required fallback
    assert any(b["type"] == "actions" for b in payload["blocks"])
    btn = [b for b in payload["blocks"] if b["type"] == "actions"][0]["elements"][0]
    assert btn["url"].endswith("/#needs-you")


def test_scan_file_channel_and_dedup(state_dir, foreman_dir, spool_dir, make_receipt, now, tmp_path):
    make_receipt("acmeapi", "security-review", "2026-09-06T09:00:00Z", "red")
    log = tmp_path / "notify.log"
    env = {"FOREMAN_NOTIFY_FILE": str(log)}

    out1 = notify.scan_and_notify(state_dir, foreman_dir, spool_dir, now=now, env=env)
    assert out1["channel"] == "file" and len(out1["sent"]) == 1
    assert "security-review" in log.read_text()

    # second scan: already watermarked, nothing re-sent
    out2 = notify.scan_and_notify(state_dir, foreman_dir, spool_dir, now=now, env=env)
    assert out2["sent"] == [] and out2["skipped"] == 1
    assert log.read_text().count("security-review") == 1


def test_scan_noop_without_channel(state_dir, foreman_dir, spool_dir, make_receipt, now):
    make_receipt("acmeapi", "security-review", "2026-09-06T09:00:00Z", "red")
    out = notify.scan_and_notify(state_dir, foreman_dir, spool_dir, now=now, env={})
    assert out["channel"] == "none" and out["sent"] == []


def test_webhook_transport_called(state_dir, foreman_dir, spool_dir, make_receipt, now, monkeypatch):
    make_receipt("acmeapi", "security-review", "2026-09-06T09:00:00Z", "red")
    posted = []
    monkeypatch.setattr(notify, "_post", lambda url, payload, timeout=10.0: posted.append((url, payload)))
    env = {"FOREMAN_NOTIFY_WEBHOOK": "https://hooks.slack.com/services/x"}
    out = notify.scan_and_notify(state_dir, foreman_dir, spool_dir, now=now, env=env)
    assert out["channel"] == "slack" and len(posted) == 1
    assert "blocks" in posted[0][1]


def test_dry_run_sends_nothing(state_dir, foreman_dir, spool_dir, make_receipt, now, tmp_path):
    make_receipt("acmeapi", "security-review", "2026-09-06T09:00:00Z", "red")
    log = tmp_path / "notify.log"
    env = {"FOREMAN_NOTIFY_FILE": str(log)}
    out = notify.scan_and_notify(state_dir, foreman_dir, spool_dir, now=now, env=env, dry_run=True)
    assert len(out["sent"]) == 1 and not log.exists()
    assert notify.load_watermark(spool_dir)["sent"] == {}      # not persisted on dry-run
