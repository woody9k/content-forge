"""Folder-backed store. The filesystem IS the database.

Layout under CONTENT_ROOT:

    topics/
      <topic-slug>/
        topic.json              name, description, timestamps
        voice/
          samples/              raw writing samples the user drops in
            *.md *.txt *.docx
          profile.md            generated voice profile for this topic
        ideas.json              cached topic suggestions
        articles/
          <article-slug>/
            article.md          the article body (markdown)
            meta.json           title, prompt, status, timestamps, references
            export.docx         generated on demand
"""
from __future__ import annotations

import json
import re
import shutil
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import CONTENT_ROOT

SAMPLE_SUFFIXES = {".md", ".txt", ".markdown", ".docx", ".rst"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(value: str, fallback: str = "untitled") -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    value = re.sub(r"[-\s]+", "-", value).strip("-")
    return value[:80] or fallback


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

def topics_root() -> Path:
    p = CONTENT_ROOT / "topics"
    p.mkdir(parents=True, exist_ok=True)
    return p


def topic_dir(slug: str) -> Path:
    return topics_root() / slug


def voice_dir(slug: str) -> Path:
    return topic_dir(slug) / "voice"


def samples_dir(slug: str) -> Path:
    return voice_dir(slug) / "samples"


def profile_path(slug: str) -> Path:
    return voice_dir(slug) / "profile.md"


def articles_dir(slug: str) -> Path:
    return topic_dir(slug) / "articles"


def article_dir(topic: str, article: str) -> Path:
    return articles_dir(topic) / article


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------

@dataclass
class Topic:
    slug: str
    name: str
    description: str = ""
    audience: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    @property
    def has_voice(self) -> bool:
        return profile_path(self.slug).exists()

    @property
    def sample_count(self) -> int:
        d = samples_dir(self.slug)
        if not d.exists():
            return 0
        return len([f for f in d.iterdir() if f.suffix.lower() in SAMPLE_SUFFIXES])

    @property
    def article_count(self) -> int:
        d = articles_dir(self.slug)
        if not d.exists():
            return 0
        return len([f for f in d.iterdir() if f.is_dir()])


@dataclass
class Article:
    slug: str
    topic: str
    title: str
    prompt: str = ""
    status: str = "draft"          # draft | generating | ready | error
    with_references: bool = False
    word_target: int = 900
    format: str = "blog post"
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    error: str = ""
    job_id: str = ""

    @property
    def path(self) -> Path:
        return article_dir(self.topic, self.slug) / "article.md"


# --------------------------------------------------------------------------
# topics
# --------------------------------------------------------------------------

def create_topic(name: str, description: str = "", audience: str = "") -> Topic:
    slug = slugify(name, "topic")
    d = topic_dir(slug)
    if d.exists():
        n = 2
        while topic_dir(f"{slug}-{n}").exists():
            n += 1
        slug = f"{slug}-{n}"
        d = topic_dir(slug)
    (d / "voice" / "samples").mkdir(parents=True, exist_ok=True)
    (d / "articles").mkdir(parents=True, exist_ok=True)
    t = Topic(slug=slug, name=name.strip(), description=description.strip(),
              audience=audience.strip())
    _write_json(d / "topic.json", asdict(t))
    return t


def get_topic(slug: str) -> Topic | None:
    f = topic_dir(slug) / "topic.json"
    if not f.exists():
        return None
    data = _read_json(f, None)
    if not data:
        return None
    data.setdefault("audience", "")
    return Topic(**{k: v for k, v in data.items()
                    if k in Topic.__dataclass_fields__})


def update_topic(slug: str, **changes) -> Topic | None:
    t = get_topic(slug)
    if not t:
        return None
    for k, v in changes.items():
        if k in Topic.__dataclass_fields__ and k not in ("slug", "created_at"):
            setattr(t, k, v)
    t.updated_at = now_iso()
    _write_json(topic_dir(slug) / "topic.json", asdict(t))
    return t


def list_topics() -> list[Topic]:
    out = []
    for d in sorted(topics_root().iterdir()):
        if d.is_dir():
            t = get_topic(d.name)
            if t:
                out.append(t)
    out.sort(key=lambda t: t.name.lower())
    return out


def delete_topic(slug: str) -> bool:
    d = topic_dir(slug)
    if not d.exists():
        return False
    shutil.rmtree(d)
    return True


# --------------------------------------------------------------------------
# voice
# --------------------------------------------------------------------------

def list_samples(topic: str) -> list[dict]:
    d = samples_dir(topic)
    if not d.exists():
        return []
    out = []
    for f in sorted(d.iterdir()):
        if f.suffix.lower() in SAMPLE_SUFFIXES:
            out.append({"name": f.name, "size": f.stat().st_size,
                        "words": len(read_sample(f).split())})
    return out


def read_sample(path: Path) -> str:
    """Plain text out of a sample file, .docx included."""
    if path.suffix.lower() == ".docx":
        try:
            import docx  # type: ignore
            doc = docx.Document(str(path))
            return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception:
            return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def save_sample(topic: str, filename: str, data: bytes) -> str:
    d = samples_dir(topic)
    d.mkdir(parents=True, exist_ok=True)
    safe = Path(filename).name
    if Path(safe).suffix.lower() not in SAMPLE_SUFFIXES:
        safe += ".txt"
    dest = d / safe
    n = 2
    while dest.exists():
        dest = d / f"{Path(safe).stem}-{n}{Path(safe).suffix}"
        n += 1
    dest.write_bytes(data)
    return dest.name


def delete_sample(topic: str, name: str) -> bool:
    f = samples_dir(topic) / Path(name).name
    if f.exists() and f.is_file():
        f.unlink()
        return True
    return False


def collect_samples_text(topic: str, max_chars: int = 60_000) -> str:
    """Concatenate every sample, capped so the prompt stays sane."""
    parts, total = [], 0
    for f in sorted(samples_dir(topic).glob("*")):
        if f.suffix.lower() not in SAMPLE_SUFFIXES:
            continue
        text = read_sample(f).strip()
        if not text:
            continue
        chunk = f"\n\n===== SAMPLE: {f.name} =====\n{text}"
        if total + len(chunk) > max_chars:
            chunk = chunk[: max(0, max_chars - total)]
            parts.append(chunk)
            break
        parts.append(chunk)
        total += len(chunk)
    return "".join(parts).strip()


def get_voice_profile(topic: str) -> str:
    p = profile_path(topic)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def save_voice_profile(topic: str, text: str) -> None:
    p = profile_path(topic)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------
# articles
# --------------------------------------------------------------------------

def create_article(topic: str, title: str, prompt: str, *,
                   with_references: bool = False, word_target: int = 900,
                   fmt: str = "blog post") -> Article:
    slug = slugify(title, "article")
    d = article_dir(topic, slug)
    if d.exists():
        n = 2
        while article_dir(topic, f"{slug}-{n}").exists():
            n += 1
        slug = f"{slug}-{n}"
        d = article_dir(topic, slug)
    d.mkdir(parents=True, exist_ok=True)
    a = Article(slug=slug, topic=topic, title=title.strip(), prompt=prompt.strip(),
                with_references=with_references, word_target=word_target, format=fmt)
    _write_json(d / "meta.json", asdict(a))
    (d / "article.md").write_text("", encoding="utf-8")
    return a


def get_article(topic: str, slug: str) -> Article | None:
    f = article_dir(topic, slug) / "meta.json"
    if not f.exists():
        return None
    data = _read_json(f, None)
    if not data:
        return None
    return Article(**{k: v for k, v in data.items()
                      if k in Article.__dataclass_fields__})


def save_article_meta(a: Article) -> None:
    a.updated_at = now_iso()
    _write_json(article_dir(a.topic, a.slug) / "meta.json", asdict(a))


def list_articles(topic: str) -> list[Article]:
    d = articles_dir(topic)
    if not d.exists():
        return []
    out = []
    for sub in d.iterdir():
        if sub.is_dir():
            a = get_article(topic, sub.name)
            if a:
                out.append(a)
    out.sort(key=lambda a: a.created_at, reverse=True)
    return out


def read_article_body(topic: str, slug: str) -> str:
    f = article_dir(topic, slug) / "article.md"
    return f.read_text(encoding="utf-8") if f.exists() else ""


def write_article_body(topic: str, slug: str, body: str) -> None:
    d = article_dir(topic, slug)
    d.mkdir(parents=True, exist_ok=True)
    (d / "article.md").write_text(body, encoding="utf-8")


def delete_article(topic: str, slug: str) -> bool:
    d = article_dir(topic, slug)
    if not d.exists():
        return False
    shutil.rmtree(d)
    return True


# --------------------------------------------------------------------------
# ideas
# --------------------------------------------------------------------------

def get_ideas(topic: str) -> list[dict]:
    return _read_json(topic_dir(topic) / "ideas.json", [])


def save_ideas(topic: str, ideas: list[dict]) -> None:
    _write_json(topic_dir(topic) / "ideas.json", ideas)
