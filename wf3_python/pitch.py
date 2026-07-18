"""
Pitch generation + localization — the ONE place the LLM is used.
Python version of the n8n "Basic LLM Chain1" (prompt) + "Pitch parser" (robust JSON parse).
"""
import json
import re
from urllib.parse import urlparse
import requests
import config
import factcheck

# Per-segment PARAGRAPH-2 guidance (business-impact, everyday language). The pitch morphs by the lead's
# ICP segment: A = modernize a local/brick business, B = dev pods vs. costly local hiring, C = slow mobile
# store losing checkouts. Written as guidance the model expresses in its own words (not copied verbatim).
SEGMENT_STYLE = {
    "A": ("Warmly note that a cleaner, more modern web presence and easier online booking would help them "
          "serve customers better and streamline day-to-day operations. Then say Granjur Technologies "
          "offers custom development, MVPs (short for Minimum Viable Products) and staff augmentation to "
          "help modernize how they work and engage their community."),
    "B": ("Note that instead of the long, expensive local recruitment cycle, Granjur's dedicated software "
          "pods provide staff augmentation that lowers their engineering overhead and helps them launch "
          "features faster."),
    "C": ("Note that their online store appears to load slowly on phones, and when a store is slow on "
          "mobile, visitors get frustrated and click away to competitors — costing them customer traffic "
          "and sales. Then say Granjur specializes in fixing these website speed problems so they protect "
          "their checkouts. Speak generally about the slow phone speed (Google's mobile speed test); do "
          "NOT state any score or number."),
}

# Role inboxes we must NOT turn into a first name (info@, sales@, ...). Anything else that looks like a
# person's local-part becomes the greeting name (jehad.al-atrash@x.com -> 'Jehad').
_ROLE_LOCALPARTS = {
    "info", "sales", "contact", "hello", "admin", "office", "support", "team", "enquiries", "enquiry",
    "inquiries", "inquiry", "mail", "email", "marketing", "help", "service", "services", "careers",
    "jobs", "hr", "press", "media", "booking", "bookings", "reception", "accounts", "billing", "noreply",
    "no-reply", "webmaster", "postmaster", "general", "orders", "shop", "store", "customerservice",
    # non-English role inboxes (Nordic / German / Romance) so we greet the team, not a role word
    "kundeservice", "kundeteam", "kundesupport", "kundecenter", "kundendienst", "kundedienst", "kontakt",
    "salg", "vertrieb", "bestilling", "firma", "firmapost", "postkasse", "buero", "accueil", "commercial",
    "ventas", "comercial", "atencion", "clientes", "servizio", "assistenza", "clienti", "contatto",
    "hola", "hallo", "ciao", "bonjour", "salut", "post", "kundeportal",
}


def _rep_name(lead):
    """Best-effort representative FIRST name for the greeting (Task: 'add a representative's name').

    Uses the enriched contact name if we have one; otherwise derives it from a PERSONAL email address's
    local-part (the email is scraped from the company's own website during WF-2 enrichment, so it's the
    reliable free source — dedicated people-name scraping from OSM/sites is unreliable). Returns '' for a
    role inbox (info@/sales@) or anything that doesn't look like a person -> we greet the team instead."""
    fn = str(lead.get("full_name") or lead.get("first_name") or "").strip()
    if fn:
        return re.split(r"\s+", fn)[0][:20]
    email = str(lead.get("email") or "").strip().lower()
    if "@" not in email:
        return ""
    local = email.split("@", 1)[0]
    if local in _ROLE_LOCALPARTS:
        return ""
    token = re.sub(r"[^a-z]", "", re.split(r"[._\-+]", local)[0])
    # must look like a name: right length, not a role word, and has a vowel (filters initials like 'jm')
    if token in _ROLE_LOCALPARTS or not (2 <= len(token) <= 15) or not re.search(r"[aeiou]", token):
        return ""
    return token.capitalize()

FALLBACK = {
    "pitch_subject": "Follow up from Granjur Technologies",
    "pitch_body": "Hi,\n\nI noticed your recent technical updates and wanted to see how you handle engineering workflows. Open to a quick look at our dev pods?",
}


def _clean_company_name(name):
    """Compound scraped names like 'AUM Framing & Gallery / Dry Creek Gold Leaf' read as automated in a
    greeting. Keep only the PRIMARY brand (the part before a ' / ', ' | ', ' - ' style separator)."""
    name = str(name or "").strip()
    for sep in (" / ", " | ", " — ", " – ", " ; "):
        if sep in name:
            name = name.split(sep)[0].strip()
    return name


def _language_instruction(region):
    """ALL regions (GCC/UK/AU/CN/...) are pitched in professional ENGLISH now — foreign-language
    translation was removed (forcing a 3B model to write a foreign script + strict JSON caused
    token-bleeding). Kept as a function for backward compatibility."""
    return "professional English"


def _known_facts(lead):
    """Only REAL, verified signals about the prospect. The model may reference THESE and nothing else,
    so it can't invent an industry/business-model that turns out to be wrong."""
    facts = []
    site_desc = str(lead.get("site_description") or "").strip()
    emp = lead.get("employee_count")
    tech = [str(t).strip() for t in (lead.get("tech_stack") or []) if str(t).strip()]
    jobs = lead.get("active_job_posts") or []
    lh = lead.get("lighthouse_mobile")
    if site_desc:
        # the company's OWN words from their homepage — the most reliable "what they do" signal
        facts.append(f"- What they say about themselves (from their homepage - the most reliable signal): {site_desc}")
    # NOTE: the raw OSM/data category tag (company_desc) is deliberately NOT fed to the model — it is
    # noisy (e.g. "radiotechnics") and the model quotes it verbatim, inventing a wrong industry. We keep
    # it only for the fact-checker (to catch the model parroting it). See factcheck.analyze(category=...).
    if emp not in (None, ""):
        facts.append(f"- Approx. team size: {emp}")
    if tech:
        facts.append(f"- Website tech detected: {', '.join(tech[:6])}")
    titles = [j.get("title") for j in jobs if isinstance(j, dict) and j.get("title")]
    if titles:
        facts.append(f"- Currently hiring: {', '.join(titles[:3])}")
    if lh not in (None, ""):
        # We do NOT expose the raw number to the model: Lighthouse scores fluctuate run-to-run, so the
        # email must not state a figure. It speaks generally and the appended LIVE report link shows the
        # exact current score. Say "Google's mobile speed test", never "Lighthouse" (jargon).
        facts.append("- Their store is slow on phones (verified by Google's mobile speed test)")
    return "\n".join(facts) if facts else "- (nothing verified beyond the trigger below - keep company references general)"


def _domain_of(lead):
    """Best available domain for the prospect (for the Google speed-test report link)."""
    dom = str(lead.get("domain") or "").strip().lower()
    if not dom:
        url = str(lead.get("website_url") or "").strip()
        if url:
            host = urlparse(url if "://" in url else "http://" + url).netloc.lower()
            dom = host[4:] if host.startswith("www.") else host
    return dom or None


def psi_report_url(lead_or_domain):
    """The PUBLIC Google PageSpeed/Lighthouse report — anyone can open it and see the live score.
    This is the verifiable source we drop into the email so the claim needs no 'trust me'."""
    dom = lead_or_domain if isinstance(lead_or_domain, str) else _domain_of(lead_or_domain)
    return f"https://pagespeed.web.dev/analysis?url=https://{dom}" if dom else None


def _opening_fact(lead, result):
    """The single most reliable fact the pitch MUST open on (keeps sentence 1 always grounded)."""
    site_desc = str(lead.get("site_description") or "").strip()
    tech = [str(t).strip() for t in (lead.get("tech_stack") or []) if str(t).strip()]
    if site_desc:
        return f'their homepage description: "{site_desc[:180]}"'
    if tech:
        return f"the technology their site runs on: {', '.join(tech[:3])}"
    return f"the verified trigger: {result['trigger']}"


def _build_prompt(lead, result):
    company_name = _clean_company_name(lead.get("company_name"))   # strip compound 'A / B' names
    first_name = _rep_name(lead)                                    # scraped/derived representative name
    facts = _known_facts(lead)
    opener = _opening_fact(lead, result)
    style = SEGMENT_STYLE.get(result.get("segment", "A"), SEGMENT_STYLE["A"])

    if first_name:
        greeting = f"Hello {first_name},"
        who = f"- Representative to greet: {first_name}\n- Company: {company_name or '(unknown - do not invent one)'}"
    elif company_name:
        greeting = f"Hello to the team at {company_name},"
        who = f"- Company: {company_name} (no personal name known - greet the whole team)"
    else:
        greeting = "Hello there,"
        who = "- (neither a contact name nor a company name is known)"

    # ENGLISH-ONLY. All regions (GCC/UK/AU/CN) are pitched in professional English — no translation.
    return f"""SYSTEM:
You are an elite, empathetic B2B growth copywriter for Granjur Technologies. Write ONE short, friendly, jargon-free cold email in clear, professional English that a NON-technical business owner instantly understands. Focus on real business impact (losing customers or traffic, saving time, modernizing bookings), not backend tech.

GREETING: begin PARAGRAPH 1 with EXACTLY this line, then continue the sentence:
{greeting}
Never write a bracketed or parenthesised placeholder such as [Name], [Company], or (unknown).

STRUCTURE — write EXACTLY THREE short paragraphs, one blank line between each:
  PARAGRAPH 1 (1-2 warm sentences): the greeting above, then show you looked at them by naming ONE real thing they do — taken ONLY from the HOMEPAGE CONTEXT / KNOWN FACTS below. Positive, about what they do.
  PARAGRAPH 2: {style}
  PARAGRAPH 3: end with exactly ONE soft, natural question, worded like — "How do you think we might be able to assist in this area? Your insights would be greatly appreciated."

RULES:
1. Plain language only — never say "technical optimization", "codebase audits", or "Lighthouse scores".
2. Do NOT invent facts. NEVER guess or label their industry/category (for example, do NOT call them a "radiotechnics company") and NEVER make up a number, percentage, or statistic. Use only what is in HOMEPAGE CONTEXT / KNOWN FACTS.
3. No empty filler ("As someone who...", "I wanted to reach out"), no ALL-CAPS, no bracketed placeholders.
4. NO sign-off, signature, or your own name — the system appends the company signature automatically. End right after the CTA question.
5. Keep the whole email between {config.PITCH_MIN_WORDS} and {config.PITCH_MAX_WORDS} words.

RECIPIENT:
{who}
- Verified trigger: {result['trigger']}

HOMEPAGE CONTEXT (sentence 1 must tie to this; invent nothing):
{opener}

KNOWN FACTS (the only prospect facts you may state):
{facts}

LANGUAGE: Write the ENTIRE subject and body in clear, professional English only.

OUTPUT: Return ONLY a valid JSON object, no markdown or commentary:
{{"pitch_subject": "Quick question regarding {company_name or 'your website'}", "pitch_body": "..."}}
Stop immediately after the closing brace."""


def _call_ollama(prompt):
    resp = requests.post(
        config.OLLAMA_URL,
        json={
            "model": config.OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "format": "json",  # force a single valid JSON object (fixes split/truncated pitch bodies)
            "options": {"temperature": config.OLLAMA_TEMPERATURE},
        },
        timeout=config.OLLAMA_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("response", "")


def _extract_first_json(text):
    """Grab the {...} object. With Ollama format=json the whole response is one object,
    so match greedily from the first { to the last } (fixes the earlier truncation bug)."""
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    raw = re.sub(r",\s*([\]}])", r"\1", match.group(0)).strip()  # drop trailing commas
    try:
        # strict=False tolerates raw newlines/tabs inside strings (small models do this)
        return json.loads(raw, strict=False)
    except json.JSONDecodeError:
        return None


_PLACEHOLDER_RE = re.compile(r"\[[^\]\n]{0,40}\]")   # [Your Name], [Jaycar], [Company Name]...
_SIGNOFF_RE = re.compile(
    r"\n+\s*(best|regards|kind regards|best regards|sincerely|cheers|thanks|thank you|warm regards)\b.*$",
    re.I | re.S)
# the banned "hope you're well" pleasantry (rule 5) — strip it if the small model emits it anyway
_FILLER_RE = re.compile(
    r"(?:i\s+hope\s+(?:this\s+(?:message|email)\s+finds\s+you\s+well"
    r"|you(?:'re|\s+are)?\s+(?:doing\s+well|well))"
    r"|hope\s+you(?:'re|\s+are)?\s+(?:doing\s+well|well)"
    r"|i\s+trust\s+you\s+are\s+well)[.!,]?\s*",
    re.I)
# throat-clearing SENTENCES the small model loves — drop the whole sentence (rule 5/B). Anchored to a
# sentence boundary so it fires even when it's not the first sentence.
_OPENER_FILLER_RE = re.compile(
    r"(?i)(?:^|(?<=[.!?])\s+)"
    r"(?:as\s+someone\s+who[^.!?]*[.!?]"
    r"|i\s+(?:just\s+)?wanted\s+to\s+(?:share|reach\s+out)[^.!?]*[.!?]"
    r"|i\s+came\s+across[^.!?]*[.!?])\s*")
# unverifiable editorialising anywhere in the text (rule B) — strip the offending phrase
_FLOURISH_RE = re.compile(
    r"\b(?:it'?s\s+clear\s+that|it'?s\s+clear|clearly|obviously|"
    r"you\s+(?:clearly\s+)?have\s+a\s+strong\s+(?:online\s+|digital\s+|web\s+)?presence|"
    r"as\s+a\s+leading\s+(?:provider|brand|company))\b[,]?\s*", re.I)
# prompt-leak: the model sometimes parrots the internal word "angle" ("Our angle is that ...")
_ANGLE_LEAK_RE = re.compile(r"\b(?:our|my|the)\s+angle\s+is\s+(?:that\s+)?", re.I)
# Generation is ENGLISH-ONLY now (translation is decoupled/post-generation). So ANY foreign-script
# character in the model's output is a token-BLEED artifact (qwen occasionally drops in Chinese/Arabic/
# Cyrillic mid-sentence). Strip those ranges so a clean English pitch reaches grounding + the translator.
_FOREIGN_SCRIPT_RE = re.compile(
    "["
    "Ѐ-ӿ"                        # Cyrillic
    "֐-׿"                        # Hebrew
    "؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿"  # Arabic
    "฀-๿"                        # Thai
    "　-ヿㇰ-ㇿ"         # CJK punct + Hiragana/Katakana
    "㐀-䶿一-鿿豈-﫿"  # CJK ideographs
    "가-힯ᄀ-ᇿ"         # Hangul
    "]+")


def _clean(text):
    """Safety net for small-model slips: strip leftover [placeholders], banned filler, unverifiable
    flourish, and any trailing sign-off/signature (the footer already supplies the signature)."""
    if not text:
        return text
    text = text.replace("*", "")                  # strip markdown emphasis (*italic* / **bold**) the model leaks
    text = _PLACEHOLDER_RE.sub("", text)          # remove bracketed placeholders the model shouldn't emit
    text = _SIGNOFF_RE.sub("", text)              # drop a dangling "Best,\n<name>" style sign-off block
    text = _OPENER_FILLER_RE.sub("", text)        # drop "As someone who...," / "I wanted to share..." openers
    text = _FILLER_RE.sub("", text)               # drop banned "I hope you're doing well" filler
    text = _FLOURISH_RE.sub("", text)             # drop "it's clear" / "strong online presence" editorialising
    text = _ANGLE_LEAK_RE.sub("", text)           # drop the "Our angle is that ..." prompt leak
    text = _FOREIGN_SCRIPT_RE.sub("", text)       # strip foreign-script BLEED (generation is English-only)
    text = re.sub(r"[ \t]{2,}", " ", text)        # collapse double spaces left behind
    text = re.sub(r" +([.,!?;:])", r"\1", text)   # remove space before punctuation
    text = re.sub(r"(?m)^[ \t]+", "", text)       # trim leading spaces a removal may have left on a line
    text = re.sub(r"(\n\n)[-–—,;:]\s*", r"\1", text)  # drop an orphan separator left at a paragraph start
    text = re.sub(r"\n{3,}", "\n\n", text)        # tidy paragraph spacing
    text = re.sub(r"[.!?]\s*(?=[.!?])", "", text)  # collapse doubled ".." a phrase removal can leave
    text = re.sub(r"(?:^|(?<=[.!?]))\s*\.\s*", " ", text)  # drop a lone "." left when a sentence emptied
    # a removal can leave a lowercase sentence start — recapitalise the first letter of each sentence
    # (allow a closing quote/bracket between the sentence-ender and the next word)
    text = re.sub(r"(^|[.!?]['\"’)\]]?\s+)([a-z])",
                  lambda m: m.group(1) + m.group(2).upper(), text.strip())
    text = re.sub(r"[ \t]{2,}", " ", text)        # final tidy of double spaces (keep newlines intact)
    return text.strip()


def _clean_subject(text):
    """Tidy a subject line: strip surrounding quotes and the leading/trailing dots or ellipsis the small
    model sometimes leaks (e.g. it echoes the schema's '...'). Returns '' if nothing usable remains."""
    s = _clean(str(text or "").strip())
    s = s.strip().strip("\"'“”‘’").strip()
    s = re.sub(r"^[.…\s]+", "", s)
    s = re.sub(r"[.…\s]+$", "", s)
    return s.strip()


def _wordcount(text):
    return len(re.findall(r"\S+", text or ""))


def _enforce_length(body, max_words):
    """Hard cap the body (#2): if the model ran long, keep whole leading sentences within the cap
    but ALWAYS preserve the final question (the CTA), so trimming never cuts the ask."""
    if _wordcount(body) <= max_words:
        return body
    sents = factcheck._sentences(body)
    if len(sents) <= 1:
        return body
    cta = sents[-1] if sents[-1].rstrip().endswith("?") else None
    reserve = _wordcount(cta) if cta else 0
    pool = sents[:-1] if cta else sents
    kept, used = [], 0
    for s in pool:
        w = _wordcount(s)
        if kept and used + w > max_words - reserve:
            break
        kept.append(s)
        used += w
    if not kept:
        kept = [pool[0]]
    if cta:
        kept.append(cta)
    return " ".join(kept)


def _facts_for_check(lead, result):
    """The verified facts, shaped for factcheck.analyze() (same source of truth as the prompt)."""
    jobs = lead.get("active_job_posts") or []
    return {
        "site_description": lead.get("site_description"),
        "category": lead.get("company_desc"),
        "tech_stack": lead.get("tech_stack") or [],
        "employee_count": lead.get("employee_count"),
        "jobs": [j.get("title") for j in jobs if isinstance(j, dict) and j.get("title")],
        "lighthouse": lead.get("lighthouse_mobile"),
        "trigger": result.get("trigger"),
    }


# ---------------------------------------------------------------------------------------------------
# TEMPLATE PITCH (approved 2026-07). The pitch is now DETERMINISTIC — no LLM — so it is always clean,
# jargon-free, fast, and never hallucinates. Only the greeting + opener line vary per company; the body
# and the calendar CTA are fixed copy. The [Book a call] button is appended by outreach.py.
# ---------------------------------------------------------------------------------------------------
MIDDLE_PARAGRAPH = (
    "A cleaner, more modern web presence and easier online booking would help you serve customers "
    "better and streamline your day-to-day operations. At Granjur Technologies, we build easy-to-use "
    "websites and simple digital tools that handle your booking, scheduling, and daily admin work for you.")
CTA_PARAGRAPH = (
    "I'd love to share how we help businesses in your area automate these tasks so your team can focus on "
    "customers, not admin work. If that would be useful, grab 10 minutes on my calendar whenever suits you:")

# markers that mean the scraped homepage "description" is nav/boilerplate junk, not real prose.
_OPENER_JUNK = ("|", ";", "save the date", "upcoming events", "log in", "register", "opening times",
                "agm", "click here", "read more", "subscribe", "newsletter", "cookie", "©", " menu",
                "»", "→", "checkout", "add to cart", "sign in", "your basket", "skip to", "toggle")


def _prose_opener(desc):
    """Return the homepage description as a clean opener sentence ONLY if it reads like real prose.
    Rejects scraped nav dumps, UI strings (LOG IN / REGISTER), all-caps shouting, and list fragments."""
    d = re.sub(r"\s+", " ", str(desc or "")).strip()
    if not (40 <= len(d) <= 260):
        return None
    low = d.lower()
    if any(m in low for m in _OPENER_JUNK):
        return None
    if len(re.findall(r"\b[A-Z]{3,}\b", d)) >= 2:               # ALL-CAPS nav shouting
        return None
    if not re.search(r"\b(is|are|was|we|our|us|provides?|offers?|based|serving|serves?|specialis|"
                     r"specializ|leading|help|helping|the|and|for|with)\b", low):
        return None                                             # doesn't look like a sentence
    return d.rstrip(" .·-|") + "."


def _opener_line(company, lead):
    """Paragraph-1 opener: the clean homepage description if we have one, else a safe generic line built
    from structured data only (never junk, never tech-shaming, never a quoted UI string)."""
    prose = _prose_opener(lead.get("site_description"))
    if prose:
        return prose
    city = str(lead.get("city") or "").strip()
    # only name the city if it's Latin script — a non-Latin city (e.g. Arabic 'دبي') reads as broken text
    # in an English email, so fall back to the city-free opener instead.
    if city and city.isascii() and city.lower() not in ("unknown", "none", "?"):
        return f"I came across {company} while looking at local businesses in {city}."
    return f"I came across {company} online and wanted to reach out."


def generate_pitch(lead, result=None):
    """Build the approved, jargon-free pitch deterministically (no LLM). Returns
    {pitch_subject, pitch_body, pitch_lang, pitch_localized, pitch_grounding}.

    Only the greeting + one opener line vary per company; the value paragraph and the calendar CTA are
    fixed copy. The [Book a 15-min call] button (Google Calendar) is appended downstream by outreach.py."""
    company = _clean_company_name(lead.get("company_name")) or ""
    first = _rep_name(lead)
    if first:
        greeting = f"Hello {first},"
    elif company:
        greeting = f"Hello team at {company},"
    else:
        greeting = "Hello there,"

    display = company or "your business"
    opener = _opener_line(display, lead)
    body = f"{greeting}\n\n{opener}\n\n{MIDDLE_PARAGRAPH}\n\n{CTA_PARAGRAPH}"
    subject = f"Quick question regarding {company}" if company else "Quick question about your website"

    return {"pitch_subject": subject, "pitch_body": body, "pitch_lang": "en",
            "pitch_localized": False, "pitch_grounding": 1.0}
