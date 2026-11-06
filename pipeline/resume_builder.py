"""Tailored resumes (spec section 11a). Claude never edits LaTeX: it picks a summary, an order of
pre-approved bullet IDs and up to 8 skills to list first. Code renders the template, escapes
Claude's text, compiles with Tectonic, and rejects the result unless every check passes:
1 page, known bullet IDs, profile skills only, and a summary with no number, company or skill
that isn't in the profile. On any failure the standard PDF is attached instead.

Templates: candidate/latex/<key>.tex.tmpl with %%SUMMARY%%, %%BULLETS:<section>%%, %%SKILLS_CORE%%
markers (plain markers instead of Jinja: jinja2 is not in the spec's library list).
Bullet bank: candidate/bullets.yaml, built from your own .tex resumes (bullets you wrote)."""
import json
import re
import shutil
import subprocess
import zlib
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
LATEX_DIR = ROOT / "candidate" / "latex"
BULLETS = ROOT / "candidate" / "bullets.yaml"
STOPWORDS = {"i", "a", "an", "the", "and", "or", "with", "for", "of", "in", "on", "to", "at", "as", "by",
             "my", "me", "we", "us", "available", "strong", "strongest", "full", "python", "backend",
             "frontend", "engineer", "developer", "remote", "contractor"}
PDFTEX_ONLY = [(r"\input{glyphtounicode}", r"\ifdefined\pdfgentounicode\input{glyphtounicode}\fi"),
               (r"\pdfgentounicode=1", r"\ifdefined\pdfgentounicode\pdfgentounicode=1\fi")]
LATEX_SPECIAL = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
                 "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def escape(text: str) -> str:
    return "".join(LATEX_SPECIAL.get(c, c) for c in text)


def _balanced(s: str, start: int) -> int:
    """Index just past the brace group that opens at s[start] == '{'."""
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    raise ValueError("unbalanced braces")


def _items(block: str) -> list[str]:
    out, pos = [], 0
    while True:
        i = block.find(r"\resumeItem{", pos)
        if i == -1:
            return out
        open_brace = i + len(r"\resumeItem")
        end = _balanced(block, open_brace)
        out.append(block[open_brace + 1:end - 1].strip())
        pos = end


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower().split()[0]) if text.strip() else "section"


def to_plain(latex: str) -> str:
    s = re.sub(r"\\(textbf|emph|textit|underline)\{([^}]*)\}", r"\2", latex)
    s = re.sub(r"\\href\{[^}]*\}\{([^}]*)\}", r"\1", s)
    s = s.replace(r"\%", "%").replace(r"\&", "&").replace(r"\#", "#").replace("---", "—").replace("--", "–")
    return re.sub(r"\s+", " ", s).strip()


def parse_resume(tex: str) -> dict:
    """{'template': str with markers, 'sections': {id: [bullet latex]}, 'summary': str, 'core': [skills]}"""
    for old, new in PDFTEX_ONLY:
        tex = tex.replace(old, new)
    split = tex.find(r"\begin{document}")
    if split == -1:
        raise ValueError("no \\begin{document}")
    preamble, body = tex[:split], tex[split:]
    parsed = _parse_body(body)
    parsed["template"] = preamble + parsed["template"]
    return parsed


def _parse_body(tex: str) -> dict:
    m = re.search(r"\\section\{Summary\}.*?\\small\{\\item\{(.*?)\}\}", tex, re.S)
    if not m:
        raise ValueError("no Summary section with \\small{\\item{...}}")
    summary = m.group(1).strip()
    tex = tex[:m.start(1)] + "\n%%SUMMARY%%\n" + tex[m.end(1):]
    sections, out, pos = {}, [], 0
    for block in re.finditer(r"\\resumeItemListStart(.*?)\\resumeItemListEnd", tex, re.S):
        before = tex[pos:block.start()]
        heading = re.findall(r"\\href\{[^}]*\}\{([^}]+)\}|\\resumeSubheading\s*\{([^}]+)\}", before)
        name = next((a or b for a, b in reversed(heading)), f"section{len(sections) + 1}")
        sid = _slug(name)
        sections[sid] = _items(block.group(1))
        out.append(tex[pos:block.start()] + f"\\resumeItemListStart\n%%BULLETS:{sid}%%\n  \\resumeItemListEnd")
        pos = block.end()
    tex = "".join(out) + tex[pos:]
    core = re.search(r"\\textbf\{Core\}\{:\s*(.*?)\}\s*\\\\", tex)
    if not core:
        raise ValueError("no \\textbf{Core}{: ...} skills line")
    skills = [s.strip() for s in core.group(1).split(",") if s.strip()]
    tex = tex[:core.start(1)] + "%%SKILLS_CORE%%" + tex[core.end(1):]
    return {"template": tex, "sections": sections, "summary": summary, "core": skills}


def build_templates(latex_dir: Path | None = None, bullets_path: Path | None = None, profile: dict | None = None) -> dict:
    """One-time: <key>.tex -> <key>.tex.tmpl + <key>.meta.json, and the bullet bank."""
    latex_dir = latex_dir or LATEX_DIR
    bullets_path = bullets_path or BULLETS
    bank: dict[str, dict] = {}
    by_text: dict[str, str] = {}
    for tex_file in sorted(latex_dir.glob("*.tex")):
        key = tex_file.stem
        parsed = parse_resume(tex_file.read_text(encoding="utf-8"))
        defaults = {}
        for sid, items in parsed["sections"].items():
            ids = []
            for n, text in enumerate(items, 1):
                if text not in by_text:
                    bid = f"{sid}-{len([b for b in bank.values() if b['section'] == sid]) + 1}"
                    by_text[text] = bid
                    bank[bid] = {"section": sid, "text": text, "plain": to_plain(text), "from": [key],
                                 "facts": _facts_for(to_plain(text), profile) if profile else []}
                elif key not in bank[by_text[text]]["from"]:
                    bank[by_text[text]]["from"].append(key)
                ids.append(by_text[text])
            defaults[sid] = ids
        (latex_dir / f"{key}.tex.tmpl").write_text(parsed["template"], encoding="utf-8")
        (latex_dir / f"{key}.meta.json").write_text(json.dumps(
            {"summary": parsed["summary"], "core": parsed["core"], "defaults": defaults}, indent=1), encoding="utf-8")
    header = ("# Pre-approved bullet bank (spec 11a): built from your own .tex resumes. Claude may only\n"
              "# reorder/select these IDs; it never writes bullets. Edit or delete entries you don't want.\n")
    bullets_path.write_text(header + yaml.safe_dump(bank, sort_keys=False, allow_unicode=True, width=120),
                            encoding="utf-8")
    return bank


def _facts_for(plain: str, profile: dict) -> list[str]:
    """Profile facts this bullet relies on (best token overlap), for traceability."""
    facts = [f for e in profile.get("experience") or [] for f in e.get("facts") or []]
    facts += [f for p in (profile.get("projects") or {}).values() for f in p.get("facts") or []]
    words = set(re.findall(r"[a-z0-9%]+", plain.lower()))
    scored = sorted(((len(words & set(re.findall(r"[a-z0-9%]+", f.lower()))), f) for f in facts), reverse=True)
    return [f for score, f in scored[:2] if score >= 4]


# ---------------------------------------------------------------- per job

def available(resume_key: str, latex_dir: Path | None = None) -> bool:
    latex_dir = latex_dir or LATEX_DIR
    return (latex_dir / f"{resume_key}.tex.tmpl").is_file() and (latex_dir / f"{resume_key}.meta.json").is_file()


def load_bank(path: Path | None = None) -> dict:
    path = path or BULLETS
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}


def bank_for_prompt(resume_key: str, latex_dir: Path | None = None, bank: dict | None = None) -> str:
    latex_dir = latex_dir or LATEX_DIR
    meta = json.loads((latex_dir / f"{resume_key}.meta.json").read_text(encoding="utf-8"))
    bank = bank if bank is not None else load_bank()
    lines = []
    for sid in meta["defaults"]:
        for bid, b in bank.items():
            if b["section"] == sid:
                lines.append(f"  {bid} [{sid}]: {b['plain']}")
    return "\n".join(lines)


def _profile_words(profile: dict, extra: str) -> tuple[set[str], set[str]]:
    from pipeline.drafter import _profile_text
    text = f"{_profile_text(profile)} {profile.get('location', '')} {extra} " + " ".join(
        e.get("company", "") + " " + e.get("title", "") for e in profile.get("experience") or [])
    text += " " + " ".join(f"{k} {p.get('role', '')} {p.get('type', '')}" for k, p in (profile.get("projects") or {}).items())
    words = {w.lower() for w in re.findall(r"[A-Za-z][\w.+#/-]*", text)}
    numbers = set(re.findall(r"\d+", text))
    return words, numbers


def profile_skills(profile: dict, meta: dict) -> set[str]:
    skills = {s.lower() for group in (profile.get("skills") or {}).values() for s in group or []}
    return skills | {s.lower() for s in meta["core"]}


def check_spec(spec, resume_key: str, profile: dict, bank: dict, meta: dict) -> list[str]:
    problems = []
    sections = set(meta["defaults"])
    for bid in spec.bullet_ids:
        if bid not in bank:
            problems.append(f"unknown bullet id {bid}")
        elif bank[bid]["section"] not in sections:
            problems.append(f"bullet {bid} is not in the {resume_key} layout")
    allowed = profile_skills(profile, meta)
    problems += [f"skill not in profile: {s}" for s in spec.skills_first if s.lower() not in allowed]
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", spec.summary.strip()) if s]
    if len(sentences) > 3:
        problems.append(f"summary has {len(sentences)} sentences (max 3)")
    words, numbers = _profile_words(profile, meta["summary"] + " " + " ".join(meta["core"]))
    problems += [f"summary number not in profile: {n}" for n in re.findall(r"\d+", spec.summary) if n not in numbers]
    for sentence in sentences:
        for i, w in enumerate(re.findall(r"[A-Za-z][\w.+#/-]*", sentence)):
            token = w.rstrip(".,;:")
            if (i > 0 and token[:1].isupper() or re.search(r"[.+#/]", token)) and token.lower() not in words \
                    and token.lower() not in STOPWORDS:
                problems.append(f"summary mentions something not in the profile: {token}")
    return problems


def render(resume_key: str, spec, bank: dict, latex_dir: Path | None = None) -> str:
    latex_dir = latex_dir or LATEX_DIR
    template = (latex_dir / f"{resume_key}.tex.tmpl").read_text(encoding="utf-8")
    meta = json.loads((latex_dir / f"{resume_key}.meta.json").read_text(encoding="utf-8"))
    chosen = {}
    for bid in spec.bullet_ids:
        chosen.setdefault(bank[bid]["section"], []).append(bid)
    for sid, default_ids in meta["defaults"].items():
        ids = chosen.get(sid) or default_ids          # a section Claude didn't touch keeps its bullets
        items = "\n".join(f"    \\resumeItem{{{bank[b]['text']}}}" for b in ids)
        template = template.replace(f"%%BULLETS:{sid}%%", items)
    first = [s for s in spec.skills_first]
    rest = [s for s in meta["core"] if s.lower() not in {f.lower() for f in first}]
    template = template.replace("%%SKILLS_CORE%%", ", ".join(escape(s) for s in first + rest))
    return template.replace("%%SUMMARY%%", escape(spec.summary))


def count_pages(pdf: bytes) -> int:
    blobs = [pdf]
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.S):
        try:
            blobs.append(zlib.decompress(m.group(1)))
        except zlib.error:
            pass
    return sum(len(re.findall(rb"/Type\s*/Page(?![s\w])", b)) for b in blobs)


def compiler_path(cfg: dict) -> str | None:
    configured = ROOT / cfg.get("tailoring", {}).get("compiler", "tools/tectonic/tectonic.exe")
    return str(configured) if configured.is_file() else shutil.which("tectonic")


def compile_tex(tex: str, out_dir: Path, compiler: str, filename: str, timeout: int = 300) -> tuple[Path | None, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    src = out_dir / "resume.tex"
    src.write_text(tex, encoding="utf-8")
    proc = subprocess.run([compiler, "-X", "compile", src.name], cwd=out_dir, capture_output=True, text=True,
                          timeout=timeout)
    pdf = out_dir / "resume.pdf"
    if proc.returncode != 0 or not pdf.exists():
        errors = [l for l in (proc.stderr + proc.stdout).splitlines() if "error" in l.lower() and "Fontconfig" not in l]
        return None, "; ".join(errors[:3]) or f"compiler exit {proc.returncode}"
    final = out_dir / filename
    pdf.replace(final)
    return final, ""


def build(job_key: str, resume_key: str, spec, profile: dict, cfg: dict, out_dir: Path,
          latex_dir: Path | None = None, bank: dict | None = None, compile_fn=None) -> dict:
    """{'status': 'used'|'rejected', 'pdf': path|None, 'problems': [...]}. Never raises."""
    latex_dir = latex_dir or LATEX_DIR
    bank = bank if bank is not None else load_bank()
    if not available(resume_key, latex_dir):
        return {"status": "rejected", "pdf": None, "problems": [f"no template for {resume_key}"]}
    meta = json.loads((latex_dir / f"{resume_key}.meta.json").read_text(encoding="utf-8"))
    problems = check_spec(spec, resume_key, profile, bank, meta)
    if problems:
        return {"status": "rejected", "pdf": None, "problems": problems}
    compiler = compiler_path(cfg)
    if not compiler:
        return {"status": "rejected", "pdf": None, "problems": ["no LaTeX compiler (tectonic) found"]}
    try:
        tex = render(resume_key, spec, bank, latex_dir)
        pdf, error = (compile_fn or compile_tex)(tex, out_dir, compiler, cfg["outreach"]["attachment_filename"])
    except Exception as e:
        return {"status": "rejected", "pdf": None, "problems": [f"{type(e).__name__}: {e}"[:300]]}
    if not pdf:
        return {"status": "rejected", "pdf": None, "problems": [f"compile failed: {error}"]}
    pages = count_pages(pdf.read_bytes())
    if pages != 1:
        return {"status": "rejected", "pdf": None, "problems": [f"{pages} pages (must be exactly 1)"]}
    (out_dir / "tailoring.json").write_text(json.dumps({"job_key": job_key, "resume_key": resume_key,
                                                         **spec.model_dump()}, indent=1), encoding="utf-8")
    return {"status": "used", "pdf": str(pdf), "problems": []}
