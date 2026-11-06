"""Tailored resumes (spec 11a): template + bullet bank + checks. Fake data only."""
import shutil

import pytest

from pipeline import resume_builder as rb
from schemas.ai_output import TailoredResume
from tests.conftest import FIXTURES


@pytest.fixture
def latex(tmp_path, profile):
    d = tmp_path / "latex"
    d.mkdir()
    shutil.copy(FIXTURES / "resume_fixture.tex", d / "fullstack.tex")
    bank = rb.build_templates(d, tmp_path / "bullets.yaml", profile)
    return d, bank


def spec(**kw):
    base = {"resume_key": "fullstack", "summary": "Full-stack engineer with 3+ years building Vue.js and Django apps.",
            "bullet_ids": ["bookingapp-2", "bookingapp-1"], "skills_first": ["Django", "Redis"]}
    base.update(kw)
    return TailoredResume.model_validate(base)


def test_template_has_slots_and_bank_has_own_bullets(latex):
    d, bank = latex
    tmpl = (d / "fullstack.tex.tmpl").read_text(encoding="utf-8")
    assert "%%SUMMARY%%" in tmpl and "%%BULLETS:example%%" in tmpl and "%%BULLETS:bookingapp%%" in tmpl
    assert "%%SKILLS_CORE%%" in tmpl and r"\newcommand{\resumeItemListStart}" in tmpl   # preamble untouched
    assert r"\ifdefined\pdfgentounicode" in tmpl                                    # Tectonic-safe
    assert [b["plain"] for b in bank.values()][-1] == "Cut API latency by 30% with Redis caching."


def test_render_orders_bullets_escapes_text_keeps_untouched_sections(latex):
    d, bank = latex
    out = rb.render("fullstack", spec(summary="I like R&D at 100% & more_stuff"), bank, d)
    assert r"R\&D at 100\% \& more\_stuff" in out
    assert out.index("Cut API latency") < out.index("room-booking flow")      # Claude's order
    assert "Shipped features for 5 client projects." in out                     # default bullets kept
    assert "{: Django, Redis, Python, TypeScript, Vue.js, PostgreSQL}" in out


@pytest.mark.parametrize("change,problem", [
    ({"bullet_ids": ["bookingapp-9"]}, "unknown bullet id"),
    ({"skills_first": ["Kubernetes"]}, "skill not in profile"),
    ({"summary": "Engineer who cut costs by 65%."}, "number not in profile"),
    ({"summary": "Engineer who worked at Google on Django."}, "not in the profile: Google"),
    ({"summary": "Engineer using Kubernetes and Django."}, "not in the profile: Kubernetes"),
    ({"summary": "One. Two. Three. Four."}, "4 sentences"),
])
def test_checks_reject(latex, profile, change, problem):
    d, bank = latex
    meta = __import__("json").loads((d / "fullstack.meta.json").read_text(encoding="utf-8"))
    problems = rb.check_spec(spec(**change), "fullstack", profile, bank, meta)
    assert any(problem in p for p in problems), problems


def test_good_spec_passes(latex, profile):
    d, bank = latex
    meta = __import__("json").loads((d / "fullstack.meta.json").read_text(encoding="utf-8"))
    assert rb.check_spec(spec(), "fullstack", profile, bank, meta) == []


def fake_compile(pages):
    def compile_fn(tex, out_dir, compiler, filename):
        out_dir.mkdir(parents=True, exist_ok=True)
        pdf = out_dir / filename
        pdf.write_bytes(b"%PDF " + b"/Type /Page\n" * pages + b"/Type /Pages")
        return pdf, ""
    return compile_fn


def test_build_two_pages_rejected_falls_back(latex, profile, cfg, tmp_path, monkeypatch):
    d, bank = latex
    monkeypatch.setattr(rb, "compiler_path", lambda cfg: "fake")
    out = rb.build("k1", "fullstack", spec(), profile, cfg, tmp_path / "app", d, bank, compile_fn=fake_compile(2))
    assert out["status"] == "rejected" and "2 pages" in out["problems"][0]
    ok = rb.build("k1", "fullstack", spec(), profile, cfg, tmp_path / "app", d, bank, compile_fn=fake_compile(1))
    assert ok["status"] == "used" and ok["pdf"].endswith(cfg["outreach"]["attachment_filename"])
    assert (tmp_path / "app" / "tailoring.json").exists()     # what the company saw is stored


def test_unknown_resume_key_rejected(latex, profile, cfg, tmp_path):
    d, bank = latex
    out = rb.build("k1", "dotnet", spec(resume_key="dotnet"), profile, cfg, tmp_path, d, bank)
    assert out["status"] == "rejected" and "no template" in out["problems"][0]


def test_count_pages_handles_compressed_streams():
    import zlib
    body = zlib.compress(b"<< /Type /Page >> << /Type /Page >> << /Type /Pages /Count 2 >>")
    pdf = b"%PDF-1.5\n1 0 obj << /Length 10 >>\nstream\n" + body + b"\nendstream\n"
    assert rb.count_pages(pdf) == 2


@pytest.mark.skipif(not rb.compiler_path({}), reason="tectonic not installed")
def test_real_compile_is_one_page(latex, profile, cfg, tmp_path):
    d, bank = latex
    out = rb.build("k1", "fullstack", spec(), profile, cfg, tmp_path / "real", d, bank)
    assert out["status"] == "used", out["problems"]
