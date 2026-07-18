"""
Pitch fact-checker — the "does this pitch actually match the facts?" gate.

WHY: a 3B model, given freedom, editorialises ("strong online presence"), invents stats
("recover 30% of revenue") or names the wrong platform ("your WordPress site" when they run
Shopify). One wrong fact kills a cold email. So after the model writes a pitch we run these
HIGH-PRECISION checks (few false alarms) over each sentence:

  * bad number   — a %/$/×/2-digit figure that isn't in the known facts (invented metric)
  * wrong tech   — names a platform/framework the site does NOT use
  * flourish     — unverifiable editorialising ("clearly", "leading provider", "strong presence")
  * quoted tag   — parrots the raw category tag verbatim

Used two ways:
  * pitch.py  — score each attempt, keep the best-grounded one, retry a weak one.
  * dashboard — highlight the flagged sentences red so a human reviewer SEES the problem.

Deliberately conservative: we only flag what we're fairly sure is wrong, so a clean sentence
never lights up red. It won't catch every subtle slip — it's a reviewer aid, not a censor.
"""
import re

# Platforms / frameworks we can recognise by name in a pitch. If the pitch names one of these
# and it's NOT in the detected tech stack, that's a fabricated technical claim.
TECH_VOCAB = [
    "shopify", "woocommerce", "wordpress", "wix", "squarespace", "magento", "bigcommerce",
    "webflow", "drupal", "joomla", "react", "angular", "vue", "next.js", "nextjs", "laravel",
    "django", "rails", "shopware", "prestashop", "salesforce", "hubspot", "wpengine",
]

# Unverifiable editorialising a small model reaches for. Also stripped by pitch._clean.
FLOURISH = [
    r"strong (online |digital |web )?presence", r"it'?s clear", r"clearly", r"as someone who",
    r"leading (provider|brand|company)", r"top[- ]notch", r"world[- ]class", r"cutting[- ]edge",
    r"we all know", r"needless to say", r"without a doubt", r"i can tell", r"obviously",
]
_FLOURISH_RE = re.compile("|".join(FLOURISH), re.I)

# Words that are normal sales/English vocabulary — never treated as a factual "claim" about them.
_GENERIC_OK = {
    "store", "site", "website", "online", "mobile", "checkout", "customers", "shoppers",
    "revenue", "sales", "team", "business", "brand", "brands", "help", "helps", "build",
    "development", "engineers", "engineering", "product", "products", "app", "apps", "chat",
    "quick", "great", "nice", "worth", "open", "phone", "phones", "speed", "performance",
    "experience", "growth", "hiring", "hire", "work", "working", "granjur", "technologies",
}
_STOP = {
    "the", "and", "you", "your", "youre", "their", "they", "them", "with", "that", "this",
    "have", "has", "which", "from", "some", "into", "about", "would", "could", "want", "wanted",
    "noticed", "came", "across", "using", "looks", "look", "share", "insights", "helpful",
    "might", "how", "for", "are", "our", "was", "were", "can", "will", "one", "thing", "stood",
    "out", "usually", "slows", "who", "there", "here", "hope", "message", "email", "finds",
    "well", "just", "also", "like", "make", "makes", "see", "know", "understand", "understands",
    "importance", "optimized", "impacting", "potentially", "losing", "recover", "lost",
    "addressing", "issue", "improve", "overall", "user", "enhance", "believe", "discussing",
    "together", "hearing", "thoughts", "forward", "looking", "open", "discuss", "shopping",
    "smartphones", "checkout", "process",
}


def _sentences(text):
    parts = re.split(r"(?<=[.!?。！？])\s+", (text or "").strip())
    return [p.strip() for p in parts if p.strip()]


def _is_about_prospect(s):
    return bool(re.search(r"\b(you|your|you're|their|they|them)\b", s, re.I))


def _claim_numbers(text):
    """Numbers that read as a *metric*: %, $, ×N multiplier, or a standalone figure >= 10."""
    nums = set()
    for m in re.finditer(r"\$\s?\d[\d,]*|\d+\s?%|\d+\s?x\b|\b\d{2,}\b", text, re.I):
        nums.add(re.sub(r"[^\d]", "", m.group(0)))
    return {n for n in nums if n}


def _fact_blob(facts):
    """One lowercased string of everything we actually know, for substring lookups."""
    bits = [str(facts.get("site_description") or ""), str(facts.get("trigger") or ""),
            str(facts.get("category") or ""), str(facts.get("lighthouse") or ""),
            str(facts.get("employee_count") or "")]
    bits += [str(t) for t in (facts.get("tech_stack") or [])]
    bits += [str(j) for j in (facts.get("jobs") or [])]
    return " ".join(bits).lower()


def analyze(body, facts):
    """Return {score: 0..1, sentences: [{text, status, note}]}.
    status: 'ungrounded' (red), 'grounded' (green, cites a real fact), 'neutral' (about us / no claim).
    """
    facts = facts or {}
    blob = _fact_blob(facts)
    known_tech = " ".join(str(t).lower() for t in (facts.get("tech_stack") or []))
    # {100, 10} are scale denominators ("41 out of 100", "x/10"), not invented metrics — always allow.
    known_nums = _claim_numbers(blob) | set(re.findall(r"\d+", blob)) | {"100", "10"}
    fact_words = {w for w in re.findall(r"[a-z]{4,}", blob)} - _STOP

    out = []
    red = 0
    for s in _sentences(body):
        # ignore any URL when checking (a domain like 'shop2024.com' must not read as an invented number),
        # but keep the original sentence text for display.
        s_ck = re.sub(r"https?://\S+", " ", s)
        low = s_ck.lower()
        note = None
        # --- red checks (high precision) --- all run on the URL-stripped sentence
        bad_num = _claim_numbers(s_ck) - known_nums
        wrong_tech = [t for t in TECH_VOCAB if re.search(rf"\b{re.escape(t)}\b", low)
                      and t not in known_tech and t not in blob]
        flourish = _FLOURISH_RE.search(s_ck)
        cat = str(facts.get("category") or "").lower()
        quoted_cat = [w for w in re.findall(r"[a-z]{5,}", cat) if w in low and w not in blob.replace(cat, "")]
        if bad_num:
            note = f"invented number: {', '.join(sorted(bad_num))}"
        elif wrong_tech:
            note = f"names tech they don't use: {', '.join(wrong_tech)}"
        elif flourish:
            note = f"unverifiable claim: “{flourish.group(0)}”"
        elif quoted_cat:
            note = f"parrots raw category tag: {', '.join(quoted_cat)}"
        if note:
            out.append({"text": s, "status": "ungrounded", "note": note})
            red += 1
            continue
        # --- green: a prospect sentence that cites a real fact word ---
        if _is_about_prospect(s_ck):
            content = ({w for w in re.findall(r"[a-z]{4,}", low)} - _STOP - _GENERIC_OK)
            if content & fact_words or any(t in low for t in known_tech.split()):
                out.append({"text": s, "status": "grounded", "note": None})
                continue
        out.append({"text": s, "status": "neutral", "note": None})

    total = len(out) or 1
    return {"score": (total - red) / total, "sentences": out}
