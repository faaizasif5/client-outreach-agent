"""The daily Gmail digest: short, only what needs the user today."""
import json

from outputs import digest


def item(company, route, fit=82, band="strong", **kw):
    base = {"company": company, "title": "Backend Engineer", "route": route, "fit_score": fit, "band": band,
            "reason": "Django and Postgres backend; matches past client API work. " * 4, "contact_email": "jobs@x.io",
            "sources": json.dumps([{"source": "hn", "external_id": "1", "url": f"https://x.test/{company}"}]),
            "people": [{"name": "Sam Founder"}]}
    base.update(kw)
    return base


def ctx(**kw):
    base = {"date": "2026-10-25", "live": True, "errors": [], "notes": [], "uncertain": [], "weekly": [],
            "outreach": {"created": [], "sync": {}, "rejected": {}}}
    base.update(kw)
    return base


def test_nothing_new_is_one_line():
    text = digest.render_digest(ctx())
    assert "Nothing new to send today." in text
    for noise in ("Since last run", "Needs attention", "Worth a manual look", "label", "Claude"):
        assert noise not in text


def test_actions_grouped_with_headline_and_short_reason():
    created = [{"item": item("Acme", "email"), "status": "drafted"},
               {"item": item("Globex", "portal", 72, "possible"), "status": "pending"},
               {"item": item("Initech", "network", 78, "possible"), "status": "to_connect"},
               {"item": item("Hooli", "email"), "status": "suppressed"}]          # not actionable -> hidden
    text = digest.render_digest(ctx(outreach={"created": created, "sync": {}, "rejected": {}}))
    assert "Today: 1 email(s) to review and send in Gmail Drafts, 1 portal application(s) in the tracker, " \
           "1 LinkedIn request(s) in the Network tab." in text
    assert "**Acme**, Backend Engineer (strong 82) → to jobs@x.io" in text
    assert "→ connect with Sam Founder" in text
    assert "Hooli" not in text
    reason_line = next(l for l in text.splitlines() if l.startswith("  Django"))
    assert len(reason_line) < 180                                                   # trimmed


def test_since_last_run_only_nonzero():
    sync = {"gmail_reconcile": {"sent": 2, "discarded": 0, "replied": 1}, "sheet_sync": {"applied": 0},
            "network_sync": {"requested": 1, "connected": 0}, "no_response": 0, "expired_kits": 1}
    text = digest.render_digest(ctx(outreach={"created": [], "sync": sync, "rejected": {}}))
    line = next(l for l in text.splitlines() if l.startswith("- 2 email"))
    assert line == ("- 2 email(s) sent; 1 reply(ies) received; 1 LinkedIn request(s) sent; "
                    "1 pending kit(s) expired")


def test_attention_and_every_uncertain_job_listed():
    unc = [{"company": f"C{n}", "title": "Engineer", "reason": "region_unknown", "sources": "[]",
            "location_text": "Remote"} for n in range(8)]
    for render in (digest.render_digest, digest.render_digest_html):
        text = render(ctx(errors=["source hn: Timeout"],
                          notes=["YC page waas_companies: your session expired. Run: python main.py yc-login"],
                          uncertain=unc))
        assert "Needs attention" in text and "session expired" in text and "source hn: Timeout" in text
        assert text.count("says remote, no region given") == 8 and "more in the full report" not in text


def test_weekly_compact_on_mondays():
    weekly = ["## Weekly stats", "", "Last 7 days: 3 emails sent, 0 drafts discarded", "| a | b |", "| --- | --- |",
              "> **us_auth_stretch**: 40 sent with no positive reply."]
    text = digest.render_digest(ctx(weekly=weekly))
    assert "## This week" in text and "- Last 7 days: 3 emails sent" in text and "| a |" not in text
    assert "- us_auth_stretch: 40 sent" in text


def test_weekly_section_left_out_when_nothing_happened():
    weekly = ["## Weekly stats", "", "Last 7 days: 0 emails sent, 0 drafts discarded, 0 portal applications, "
              "0 drafts expired unreviewed.", "Discard rate: n/a", "", "_No outreach yet._", ""]
    for render in (digest.render_digest, digest.render_digest_html):
        out = render(ctx(weekly=weekly))
        assert "This week" not in out and "No outreach yet" not in out


# ---------------------------------------------------------------- HTML email + delivery

def test_html_digest_cards_buttons_and_escaping():
    evil = item("<script>alert(1)</script>", "network", people=[{"name": "Sam", "linkedin_url": "https://linkedin.com/in/sam"}])
    created = [{"item": item("Acme", "email"), "status": "drafted"}, {"item": evil, "status": "to_connect"}]
    html = digest.render_digest_html(ctx(outreach={"created": created, "sync": {}, "rejected": {}},
                                         links={"drafts": "https://mail.google.com/mail/u/0/#drafts",
                                                "tracker": "https://docs.google.com/spreadsheets/d/x"}))
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "Open Gmail Drafts" in html and "Founder&#x27;s LinkedIn" in html and "https://linkedin.com/in/sam" in html
    assert ">1</div>" in html and "emails to send" in html
    assert "Emails to send" in html and "LinkedIn requests" in html


def test_html_digest_empty_day():
    assert "Nothing new to send today." in digest.render_digest_html(ctx())


def test_mime_has_text_and_html_parts():
    from outputs.gmail_client import build_mime
    msg = build_mime(sender="me@x.io", to="me@x.io", subject="Job digest", body="plain", html="<b>hi</b>", job_key="d")
    types = [p.get_content_type() for p in msg.walk()]
    assert types[0] == "multipart/alternative" and "text/plain" in types and "text/html" in types


def test_send_only_to_yourself():
    import pytest
    from outputs.gmail_client import GmailClient, build_mime

    class Svc:
        def users(self):
            return self
    client = GmailClient(Svc(), "me@x.io")
    with pytest.raises(ValueError):
        client.send(build_mime(sender="me@x.io", to="founder@acme.com", subject="s", body="b", job_key="k"))


# ---------------------------------------------------------------- full report in the email

def scored(company, decision, fit=70, band="possible", **kw):
    return item(company, "portal", fit, band, job_key=f"src:hn:{company}", decision=decision, confidence="medium",
                evidence=["Listing: Django APIs", "Profile: django, postgresql"], posted_at="2026-10-24T00:00:00+00:00",
                flags="[]", **kw)


def full_ctx():
    return ctx(items=[scored("Acme", "shortlisted", 84, "strong"), scored("Globex", "below_threshold", 58, "skip"),
                      scored("Hooli", "suppressed_company", suppressed_by="email")],
               uncertain=[{"company": "Odin", "title": "Infra", "reason": "region_unknown", "location_text": "Remote",
                           "sources": "[]"}],
               ineligible_counts={"stale": 240, "onsite": 50},
               usage={"scoring": {"model": "sonnet", "effort": "medium", "attempts": 1,
                                  "calls": [{"usage": {"input_tokens": 10, "output_tokens": 5}, "duration_ms": 900}]}},
               outreach={"created": [], "sync": {}, "rejected": {"src:hn:Acme": ["LinkedIn pitch too long (80 words, max 75)"]}})


def test_email_carries_the_full_report():
    for render in (digest.render_digest, digest.render_digest_html):
        out = render(full_ctx())
        assert "Shortlist" in out and "Acme" in out and "strong 84" in out
        assert "Listing: Django APIs" in out and "posted 2026-10-24" in out
        assert "Other scored jobs" in out and "Globex" in out and "below threshold" in out
        assert "LinkedIn pitch too long (80 words, max 75)" in out      # rejected draft, with why
        assert "Hooli" in out and "blocked" in out                      # second role at a blocked company
        assert "stale 240" in out and "onsite 50" in out
        assert "sonnet" in out
        assert "reports/" not in out                                    # no pointer to the .md any more


def test_report_header_says_live_or_dry_run():
    base = {"run_id": "r", "counts": {}, "items": [], "uncertain": [], "ineligible_counts": __import__("collections").Counter()}
    assert "(dry run)" in digest.render({**base, "date": "d", "live": False})
    assert "(dry run)" not in digest.render({**base, "date": "d", "live": True})


def test_tracker_link_always_in_email_even_on_an_empty_day():
    url = "https://docs.google.com/spreadsheets/d/x"
    for render in (digest.render_digest, digest.render_digest_html):
        assert url in render(ctx(links={"tracker": url}))
        assert "spreadsheets" not in render(ctx())                     # no sheet id yet: no broken link
