"""One-shot migration: topic-owned voices become standalone voices.

Before, each topic carried its own voice under `topics/<t>/voice/`. Voices are
now first-class and live in `voices/<slug>/`, so one voice can write across
many topics and one topic can draw on many voices.

This script promotes every topic-owned voice into a standalone one, moves its
samples and profile, and back-fills the `voice` field on that topic's existing
articles so they keep rendering with the voice they were actually written in.

    python migrate_voices.py             # dry run — prints the plan, changes nothing
    python migrate_voices.py --apply     # do it

Idempotent: a topic with no `voice/` folder is skipped, so re-running after a
successful pass is a no-op. Stop the service first — it is not safe to move
folders under a running process.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from app.config import CONTENT_ROOT
from app import storage


def _topic_name(topic_dir: Path) -> str:
    """Display name from topic.json, falling back to the folder name."""
    try:
        data = json.loads((topic_dir / "topic.json").read_text(encoding="utf-8"))
        return (data.get("name") or "").strip() or topic_dir.name
    except (OSError, json.JSONDecodeError):
        return topic_dir.name


def _free_voice_slug(base: str, planned: set[str]) -> str:
    """A slug not already taken on disk or earlier in this same run."""
    slug = storage.slugify(base, "voice")
    if slug not in planned and not storage.voice_dir(slug).exists():
        return slug
    n = 2
    while (f"{slug}-{n}" in planned
           or storage.voice_dir(f"{slug}-{n}").exists()):
        n += 1
    return f"{slug}-{n}"


def plan() -> list[dict]:
    """What the migration would do, without doing any of it."""
    topics_root = CONTENT_ROOT / "topics"
    if not topics_root.is_dir():
        return []

    steps, planned_slugs = [], set()
    for tdir in sorted(topics_root.iterdir()):
        if not tdir.is_dir():
            continue
        old_voice = tdir / "voice"
        if not old_voice.is_dir():
            continue                                  # already migrated

        old_profile = old_voice / "profile.md"
        old_samples = old_voice / "samples"
        all_files = ([f for f in sorted(old_samples.iterdir()) if f.is_file()]
                     if old_samples.is_dir() else [])
        sample_files = [f for f in all_files
                        if f.suffix.lower() in storage.SAMPLE_SUFFIXES]
        # Files the app would not have read are still the user's, and are
        # carried across rather than dropped.
        other_files = [f for f in all_files if f not in sample_files]
        has_profile = old_profile.is_file()

        if not has_profile and not all_files:
            # An empty scaffold carries nothing worth a voice of its own.
            steps.append({"topic": tdir.name, "action": "drop-empty",
                          "voice": None, "samples": 0, "other": 0,
                          "articles": []})
            continue

        name = _topic_name(tdir)
        vslug = _free_voice_slug(name, planned_slugs)
        planned_slugs.add(vslug)

        # Only articles that would actually render in this voice get relinked:
        # with no profile there is nothing to apply, so they stay generic.
        articles = []
        if has_profile:
            adir = tdir / "articles"
            if adir.is_dir():
                for sub in sorted(adir.iterdir()):
                    meta = sub / "meta.json"
                    if not (sub.is_dir() and meta.is_file()):
                        continue
                    try:
                        data = json.loads(meta.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    if not data.get("voice"):
                        articles.append(sub.name)

        steps.append({"topic": tdir.name, "action": "promote", "voice": vslug,
                      "voice_name": name, "profile": has_profile,
                      "samples": len(sample_files),
                      "other": len(other_files), "articles": articles})
    return steps


def _remove_if_empty(old_voice: Path, topic: str) -> None:
    """Delete the drained voice/ folder, but never anything still in it.

    Everything the migration knows about has been moved by this point. If a
    file is still here it is something we did not account for, and losing it
    to a blind rmtree is not an acceptable outcome for a one-shot migration.
    """
    leftovers = [p for p in old_voice.rglob("*") if p.is_file()]
    if leftovers:
        names = ", ".join(str(p.relative_to(old_voice)) for p in leftovers[:5])
        more = f" (+{len(leftovers) - 5} more)" if len(leftovers) > 5 else ""
        print(f"  ! {topic}: left {old_voice} in place — still holds "
              f"{len(leftovers)} file(s): {names}{more}")
        return
    shutil.rmtree(old_voice)


def apply(steps: list[dict]) -> None:
    for s in steps:
        tdir = storage.topic_dir(s["topic"])
        old_voice = tdir / "voice"

        if s["action"] == "drop-empty":
            _remove_if_empty(old_voice, s["topic"])
            print(f"  {s['topic']}: removed empty voice/ scaffold")
            continue

        vslug = s["voice"]
        v = storage.create_voice(
            s["voice_name"],
            f"Migrated from the {s['topic']} topic.")
        # create_voice picks its own slug on collision; the plan already
        # reserved a free one, so these agree — but trust the real one.
        vslug = v.slug

        if s["profile"]:
            shutil.move(str(old_voice / "profile.md"),
                        str(storage.voice_profile_path(vslug)))

        old_samples = old_voice / "samples"
        if old_samples.is_dir():
            dest = storage.voice_samples_dir(vslug)
            dest.mkdir(parents=True, exist_ok=True)
            # Move everything, not just the extensions the app recognises.
            # A file the user put here by hand is theirs; a migration has no
            # business deleting it just because we would not have read it.
            for f in sorted(old_samples.iterdir()):
                shutil.move(str(f), str(dest / f.name))

        for aslug in s["articles"]:
            meta = storage.article_dir(s["topic"], aslug) / "meta.json"
            data = json.loads(meta.read_text(encoding="utf-8"))
            data["voice"] = vslug
            data.setdefault("styles", [])
            meta.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                            encoding="utf-8")

        _remove_if_empty(old_voice, s["topic"])
        print(f"  {s['topic']}: -> voice '{vslug}' "
              f"({s['samples']} samples, profile={'yes' if s['profile'] else 'no'}, "
              f"{len(s['articles'])} articles relinked)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually move things; without it this is a dry run")
    args = ap.parse_args()

    print(f"content root: {CONTENT_ROOT}")
    steps = plan()
    if not steps:
        print("Nothing to migrate — no topic owns a voice/ folder.")
        return 0

    print(f"\n{len(steps)} topic(s) to migrate:\n")
    for s in steps:
        if s["action"] == "drop-empty":
            print(f"  {s['topic']}: empty voice/ scaffold, would be removed")
        else:
            print(f"  {s['topic']}: -> voice '{s['voice']}' "
                  f"(\"{s['voice_name']}\")")
            other = (f", other files carried across: {s['other']}"
                     if s["other"] else "")
            print(f"      profile: {'yes' if s['profile'] else 'no'}, "
                  f"samples: {s['samples']}{other}, "
                  f"articles to relink: {len(s['articles'])}")

    if not args.apply:
        print("\nDry run — nothing changed. Re-run with --apply to do it.")
        return 0

    print("\napplying:")
    apply(steps)
    print("\nDone. Back up or commit the content root if you version it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
