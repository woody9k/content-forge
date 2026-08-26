"""Prompt construction. Kept in one place so you can tune the output
without touching the plumbing."""
from __future__ import annotations

VOICE_SYSTEM = """You are a forensic style analyst. You read a person's \
writing and produce a reusable style guide precise enough that another writer \
could produce indistinguishable prose from it. You describe what IS on the \
page, never what you think good writing should be."""


def voice_profile_prompt(topic_name: str, samples: str, notes: str = "") -> str:
    extra = f"\n\nAdditional notes from the author about their voice:\n{notes}\n" if notes.strip() else ""
    return f"""Below are writing samples from a single author, for the topic area "{topic_name}".

Analyse them and write a VOICE PROFILE in Markdown. Be concrete and specific — cite
actual patterns you observe, with short quoted examples pulled from the samples.

Use exactly these sections:

## Voice in one paragraph
## Tone and stance
(Formality level, warmth, humour, confidence, how they treat the reader — peer? student? skeptic?)
## Sentence mechanics
(Typical and range of sentence length, rhythm, fragment use, punctuation habits —
em dashes, semicolons, parentheticals. Active vs passive. Paragraph length.)
## Vocabulary and diction
(Register, jargon tolerance, recurring words and phrases, contractions, profanity,
words this author demonstrably avoids.)
## Structural habits
(How they open. How they close. Headings, lists, transitions, use of examples,
analogies, anecdotes, data.)
## Signature moves
(3–6 distinctive things this author does that a mimic must reproduce.)
## Do not
(Specific things absent from these samples that would break the illusion —
name the clichés, the AI tells, the constructions this author never uses.)
## Calibration examples
(3 short before/after pairs: a generic sentence, then that sentence rewritten in
this author's voice.)

Output ONLY the Markdown profile. No preamble, no commentary, no code fences.{extra}

===== BEGIN SAMPLES =====
{samples}
===== END SAMPLES ====="""


def article_system(voice_profile: str) -> str:
    if not voice_profile.strip():
        return ("You are a professional writer. Write clear, substantive prose with "
                "no filler and no AI boilerplate.")
    return f"""You are ghostwriting as a specific author. The style guide below was \
derived from their own writing. Follow it as a binding constraint — it outranks \
your default habits in every case where they conflict. Reproduce their rhythm, \
diction and structural moves. Do not drift back toward generic assistant prose.

===== AUTHOR VOICE PROFILE =====
{voice_profile}
===== END VOICE PROFILE ====="""


def article_prompt(*, topic_name: str, topic_desc: str, audience: str,
                   user_prompt: str, fmt: str, word_target: int,
                   with_references: bool, prior_titles: list[str]) -> str:
    ctx = [f'Topic area: "{topic_name}"']
    if topic_desc.strip():
        ctx.append(f"Topic description: {topic_desc.strip()}")
    if audience.strip():
        ctx.append(f"Intended audience: {audience.strip()}")
    if prior_titles:
        ctx.append("Already published in this topic (do not repeat these angles):\n"
                   + "\n".join(f"  - {t}" for t in prior_titles[:25]))

    if with_references:
        refs = """
RESEARCH AND REFERENCES — REQUIRED:
- Use WebSearch and WebFetch to ground every factual claim, statistic, date,
  price, name and quotation in a real, currently-live source.
- Prefer primary sources and recognised publications. Verify a claim across two
  sources when it is load-bearing.
- Cite inline with a bracketed number, e.g. `[1]`, at the point of the claim.
- End the article with a `## References` section: a numbered list, each entry
  `Publication — Article title — full URL`.
- Never invent a URL, a study, or a quotation. If you cannot source a claim,
  cut the claim.
"""
    else:
        refs = """
REFERENCES:
- No research pass required. Write from general knowledge.
- Do not fabricate specific statistics, dates, studies, prices, or quotations.
  Where a hard number would normally go, either omit it or write in general terms.
"""

    return f"""Write a {fmt} of roughly {word_target} words.

{chr(10).join(ctx)}

THE BRIEF:
{user_prompt.strip()}
{refs}
OUTPUT FORMAT:
- Markdown only. Start with a single `# ` H1 title, then the body.
- No YAML frontmatter, no code fences wrapping the whole document,
  no preamble like "Here is your article", no closing commentary.
- Use `##` subheads where the piece genuinely needs them; do not over-segment
  a short piece.

Write the piece now, in the author's voice."""


def suggest_prompt(*, topic_name: str, topic_desc: str, audience: str,
                   prior_titles: list[str], count: int, with_research: bool) -> str:
    prior = ("\n\nAlready covered — propose genuinely new angles, not restatements:\n"
             + "\n".join(f"  - {t}" for t in prior_titles[:40])) if prior_titles else ""
    research = ("""
Use WebSearch to check what is currently being discussed, searched for, and
argued about in this space right now, and let that shape the suggestions.
Prefer ideas with a real hook to the present moment.
""" if with_research else "")

    return f"""Topic area: "{topic_name}"
Description: {topic_desc or "(none given)"}
Audience: {audience or "(not specified)"}{prior}
{research}
Propose {count} article ideas for this topic area. Each must be specific enough
to write from immediately — no vague category names.

Return ONLY a JSON array, no prose around it, no code fence. Each element:

{{
  "title": "the working headline",
  "angle": "one or two sentences on what this piece actually argues or delivers",
  "why": "why this earns a reader's attention right now",
  "format": "blog post | how-to | listicle | opinion | explainer | review | interview",
  "words": 900
}}"""
