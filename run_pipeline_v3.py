"""
run_pipeline_v3.py -- Run 3: the best parts of Run 1 and Run 2, plus a verifier.

  1. triage   (LLM)    doc type, tier, company match, injection flag
  2. rules    (Python) FINAL include/exclude decision (deterministic, not the LLM's opinion)
  3. extract  (LLM x2) claims + numeric metrics, from evidence documents only
  4. checks   (Python) conflicts, rounding-aware growth/margin reconciliation, derived figures
  5. quarantine (Py)   drop claims contradicted by a higher-tier source
  6. write    (LLM)    brief from claims + checks only (never sees raw documents)
  7. verify   (Python) format, citations, numbers-vs-sources, coverage; up to 2 rewrites
"""
import argparse
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from llm import DEFAULT_MODEL, call_llm
from run_agent import build_user_message, load_documents, save_run
from run_pipeline import (COMMON, Claim, ClaimsOut, Extraction, MetricsOut, TRIAGE_PROMPT,
                          TriageOut, ask_json)

MAX_WORDS = 450       # Snapshot..Open questions
MAX_SNAPSHOT_LINES = 4
STALE_DAYS = 365

TIER_OVERRIDES = {"yahoo finance via yfinance": 3}


def apply_tier_override(name, model_tier):
    for key, tier in TIER_OVERRIDES.items():
        if key in name.lower():
            return tier
    return model_tier

# ---------------------------------------------------------------- prompts
METRIC_KEYS = {
    "revenue_q1_fy27": "cr", "revenue_q1_fy26": "cr", "ebitda_q1_fy27": "cr", "ebitda_q1_fy26": "cr",
    "ebitda_margin_pct_q1_fy27": "pct", "pat_q1_fy27": "cr", "pat_q1_fy26": "cr",
    "order_book_cr_jun26": "cr", "order_book_cr_jun25": "cr",
    "exports_pct_q1_fy27": "pct", "exports_pct_q1_fy26": "pct", "net_debt_to_equity": "x",
    "receivable_days_now": "days", "receivable_days_year_ago": "days", "gst_demand_cr": "cr",
    "promoter_pledge_pct_jun26": "pct", "promoter_pledge_pct_mar26": "pct", "promoter_pledge_pct_jun25": "pct",
    "fii_pct_jun26": "pct", "fii_pct_jun25": "pct", "dii_pct_jun26": "pct", "dii_pct_jun25": "pct",
}

CLAIMS_PROMPT = COMMON + """
TASK: extract faithful CLAIMS from the documents. One claim per fact.
Cover, whenever the documents give it:
 (1) what the company makes or does (copy the source's own description);
 (2) latest results with growth and margin figures;
 (3) order book, exports, capacity, capex;
 (4) guidance and management's explanations;
 (5) input-cost drivers with figures, and how/when costs are passed on, INCLUDING any lag, timing or condition;
 (6) debt, receivables and working capital;
 (7) shareholding, promoter pledge and institutional holding, with ALL periods shown so the trend is visible;
 (8) legal, tax or regulatory items with amounts and the company's stated view;
 (9) share price, with its date.
Rules: keep numbers, dates, periods and qualifiers (lags, conditions, "about", "mostly") exactly as in the source.
Never skip a sentence that contains a figure (if one sentence holds several figures, write one claim per figure). Never merge two facts that carry different qualifiers. Add nothing that is not in the text. No opinions.
kind = "reported_fact" for numbers/events a source reports; "management_claim" for forecasts, explanations and self-assessments.

OUTPUT SHAPE (placeholders only). The key "claims" is required:
{"claims": [ {"doc_id": "S1", "kind": "reported_fact", "text": "faithful statement"} ]}
"""

METRICS_PROMPT = COMMON + f"""
TASK: extract numeric METRICS, one record per figure per document, using ONLY these keys (unit in brackets)
when a document gives the figure: {", ".join(f"{k} [{u}]" for k, u in METRIC_KEYS.items())}.
value = the number exactly as given (do not convert units). stated_yoy_pct = the YoY growth the document
states for that figure, else null. Pledge / FII / DII keys come from shareholding tables (percent).
If two documents give different values for the same key, output BOTH records (one per document).

OUTPUT SHAPE (placeholders only). The key "metrics" is required:
{{"metrics": [ {{"doc_id": "S1", "key": "revenue_q1_fy27", "value": 0.0, "unit": "cr", "stated_yoy_pct": null}} ]}}
"""

RECALL_PROMPT = COMMON + """
TASK: an earlier pass extracted claims but missed some figures. For EACH listed figure, write one faithful claim that
contains it, based on the sentence it appears in (keep every qualifier). Skip a figure only if it is a date, an ID, a page number or a regulation/section number.

OUTPUT SHAPE (placeholders only). The key "claims" is required:
{"claims": [ {"doc_id": "S1", "kind": "reported_fact", "text": "faithful statement"} ]}
"""

ADDENDUM = """

# PIPELINE NOTE (Run 3)
You do not receive raw documents. You receive: SOURCES (with the final include/exclude decision made by code),
CLAIMS (evidence from included sources only) and PYTHON CHECKS (arithmetic computed by code; treat as verified).
Cite the doc ID attached to each claim. Use the numbers in PYTHON CHECKS instead of computing your own.
In Sources, list every source with the status and reason given. Report conflicts and manipulation attempts
shown in PYTHON CHECKS / SOURCES in Open questions. A verifier will check your output against the evidence
and may send you a list of problems to fix. It also checks that key findings from PYTHON CHECKS (reconciliation
arithmetic, growth gaps, changes in institutional holding or pledge) are actually covered in the right section.
"""


# ---------------------------------------------------------------- step 2: deterministic include rules
INJECTION_PATTERNS = [
    r"ignore\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+instructions",
    r"disregard\s+(?:all\s+)?(?:previous|prior|above|earlier)",
    r"note\s+for\s+(?:ai|llm|assistants?)",
    r"for\s+ai\s+assistants?",
    r"do\s+not\s+mention\s+(?:this|the)\s+(?:note|message|instruction)",
    r"(?:summaris|summariz)ation\s+tools",
]


def detect_injection(text):
    """Code-level scan for text that tries to give orders to an AI. Does not depend on the LLM noticing."""
    for pat in INJECTION_PATTERNS:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(0)
    return None

def parse_front(text):
    meta = {}
    for line in text.splitlines()[:8]:
        m = re.match(r"^(source|published|type):\s*(.+)$", line.strip())
        if m:
            meta[m.group(1)] = m.group(2).strip()
    return meta


def parse_date(s):
    for fmt in ("%d %B %Y", "%Y-%m-%d", "%d %b %Y"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            pass
    return None


def decide(triage, docs, today):
    """Final include/exclude decision made by CODE, so it is stable run to run."""
    today_dt = parse_date(today)
    by_id = {t.doc_id: t for t in triage}
    out = {}
    for doc_id, filename, text in docs:
        name = parse_front(text).get("source", filename)
        t = by_id.get(doc_id)
        hit = detect_injection(text)                      # code-level detection
        injected = bool(hit) or bool(t and t.injection_attempt)   # ...OR the model noticed it
        tier, pub = (apply_tier_override(name, t.tier), t.published) if t else (0, "")
        age = None
        if t:
            pub_dt = parse_date(pub)
            age = (today_dt - pub_dt).days if (today_dt and pub_dt) else None
        if injected:
            ok, why = False, "contains instructions aimed at an AI (prompt-injection attempt)"
        elif t is None:
            ok, why = False, "not triaged"
        elif not t.company_match:
            ok, why = False, "refers to a different company"
        elif tier >= 4:
            ok, why = False, "tier 4 source (blog / tip sheet / promotional)"
        elif age is not None and age > STALE_DAYS:
            ok, why = False, f"older than 12 months ({pub}); history, not current evidence"
        else:
            ok, why = True, ""
        out[doc_id] = {"name": name, "tier": tier, "published": pub, "included": ok, "reason": why,
                       "injection": injected, "injection_phrase": hit}
    return out


# ---------------------------------------------------------------- step 4: deterministic checks
def half_unit(v):
    s = f"{v:.6f}".rstrip("0")
    decimals = len(s.split(".")[1]) if "." in s else 0
    return 0.5 * 10 ** (-decimals)


def run_checks(metrics, tier):
    by_key = defaultdict(list)
    for m in metrics:
        by_key[m.key].append(m)

    def best(key):
        lst = by_key.get(key, [])
        return min(lst, key=lambda m: tier.get(m.doc_id, 9)) if lst else None

    lines, conflicts = [], []

    # 1. conflicts: same metric, different values
    for key, lst in by_key.items():
        if len({round(m.value, 2) for m in lst}) > 1:
            b = best(key)
            desc = ", ".join(f"{m.doc_id}={m.value:g}" for m in lst)
            lines.append(f"CONFLICT {key}: {desc}. Highest-tier source: {b.doc_id} ({b.value:g}).")
            conflicts.append((key, b, [m for m in lst if round(m.value, 2) != round(b.value, 2)]))

    # 2. stated growth vs recomputed growth, allowing for rounding
    for cur, pri in [("revenue_q1_fy27", "revenue_q1_fy26"), ("pat_q1_fy27", "pat_q1_fy26"),
                     ("ebitda_q1_fy27", "ebitda_q1_fy26")]:
        p = best(pri)
        for m in by_key.get(cur, []):
            if p and m.stated_yoy_pct is not None:
                hc, hp = half_unit(m.value), half_unit(p.value)
                lo = ((m.value - hc) / (p.value + hp) - 1) * 100
                hi = ((m.value + hc) / (p.value - hp) - 1) * 100
                ok = lo - 0.05 <= m.stated_yoy_pct <= hi + 0.05
                lines.append(f"GROWTH {cur} {m.doc_id}: {m.value:g} vs prior {p.value:g} ({p.doc_id}); possible growth "
                             f"{lo:.1f}% to {hi:.1f}% allowing for rounding; stated {m.stated_yoy_pct:g}% -> "
                             + ("CONSISTENT" if ok else "DOES NOT RECONCILE"))

    # 3. which revenue figure reconciles with the stated EBITDA margin (rounding-aware)
    e, mg = best("ebitda_q1_fy27"), best("ebitda_margin_pct_q1_fy27")
    if e and mg:
        for m in by_key.get("revenue_q1_fy27", []):
            he, hr = half_unit(e.value), half_unit(m.value)
            lo = (e.value - he) / (m.value + hr) * 100
            hi = (e.value + he) / (m.value - hr) * 100
            calc = e.value / m.value * 100
            ok = lo - half_unit(mg.value) <= mg.value <= hi + half_unit(mg.value)
            lines.append(f"MARGIN with revenue {m.doc_id}={m.value:g}: EBITDA {e.value:g} ({e.doc_id}) / revenue = "
                         f"{calc:.1f}%; stated margin {mg.value:g}% ({mg.doc_id}) -> "
                         + ("RECONCILES" if ok else "DOES NOT RECONCILE"))

    # 4. derived figures (computed by code so the writer never does arithmetic)
    def ratio(a, b, label, fmt):
        x, y = best(a), best(b)
        if x and y and y.value:
            lines.append(f"DERIVED {label}: " + fmt(x, y))

    ratio("order_book_cr_jun26", "order_book_cr_jun25", "order book growth",
          lambda x, y: f"{x.value:g} vs {y.value:g} = +{(x.value / y.value - 1) * 100:.1f}% ({x.doc_id})")
    ratio("gst_demand_cr", "pat_q1_fy27", "GST demand vs one quarter of profit",
          lambda x, y: f"{x.value:g} cr is about {x.value / y.value * 100:.0f}% of Q1 FY27 PAT of {y.value:g} cr ({x.doc_id}, {y.doc_id})")
    for a, b, label in [("receivable_days_now", "receivable_days_year_ago", "receivable days change"),
                        ("promoter_pledge_pct_jun26", "promoter_pledge_pct_jun25", "promoter pledge change (pct points)"),
                        ("fii_pct_jun26", "fii_pct_jun25", "FII holding change (pct points)"),
                        ("dii_pct_jun26", "dii_pct_jun25", "DII holding change (pct points)")]:
        x, y = best(a), best(b)
        if x and y:
            lines.append(f"DERIVED {label}: {y.value:g} -> {x.value:g} ({x.value - y.value:+.1f}) ({x.doc_id})")
    m26, m25 = best("promoter_pledge_pct_mar26"), best("promoter_pledge_pct_jun25")
    if m26 and m25:
        lines.append(f"DERIVED promoter pledge trend: {m25.value:g} (Jun 2025) -> {m26.value:g} (Mar 2026) ({m26.doc_id})")
    r, e2, p2 = best("revenue_q1_fy27"), best("ebitda_q1_fy27"), best("pat_q1_fy27")
    if r and r.stated_yoy_pct is not None and e2 and e2.stated_yoy_pct is not None:
        lines.append(f"DERIVED growth gap: revenue {r.stated_yoy_pct:+g}% vs EBITDA {e2.stated_yoy_pct:+g}% "
                     f"({r.doc_id}); profit growth is {'lagging' if e2.stated_yoy_pct < r.stated_yoy_pct else 'ahead of'} sales growth")
    return lines or ["No checks could be computed."], conflicts


def quarantine(claims, conflicts):
    """Drop claims that repeat a figure contradicted by a higher-tier source."""
    bad = defaultdict(set)  # doc_id -> strings that must not appear in its claims
    for _key, _best, losers in conflicts:
        for m in losers:
            bad[m.doc_id] |= {f"{m.value:,.0f}", f"{m.value:.0f}"}
            if m.stated_yoy_pct is not None:
                bad[m.doc_id].add(f"{m.stated_yoy_pct:g}%")
    kept, dropped = [], []
    for c in claims:
        (dropped if any(s in c.text for s in bad.get(c.doc_id, ())) else kept).append(c)
    return kept, dropped


# ---------------------------------------------------------------- step 7: verifier (no LLM)
SECTION_NAMES = ["snapshot", "bull case", "bear case", "open questions", "sources"]
MON = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"


def split_sections(brief):
    secs, cur = {}, None
    for line in brief.splitlines():
        if line.strip().startswith("#"):
            hit = [n for n in SECTION_NAMES if n in line.lower()]
            if hit:
                cur = hit[0]
                secs[cur] = []
                continue
        if cur:
            secs[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in secs.items()}


def numbers_in(text):
    t = re.sub(r"\[S\d+(?:\s*,\s*S\d+)*\]", " ", text)
    t = re.sub(r"\bFY\s?\d{2,4}(?:\s?[-\u2013]\s?\d{2,4})?", " ", t)
    t = re.sub(r"\bQ[1-4]\b", " ", t)
    t = re.sub(r"\b\d+-(?:week|month|day|year)s?\b", " ", t, flags=re.I)
    t = re.sub(rf"\b\d{{1,2}}(?:st|nd|rd|th)?[\s-]+{MON}(?:[\s-]+\d{{4}})?", " ", t)
    t = re.sub(rf"\b{MON}\s+\d{{4}}", " ", t)
    t = re.sub(r"\b(?:19|20)\d{2}\b", " ", t)
    t = re.sub(r"\bS\d+\b", " ", t)
    out = []
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", t):
        try:
            out.append(float(m.group().rstrip(",").replace(",", "")))
        except ValueError:
            pass
    return out


def num_ok(n, allowed):
    if n <= 10 and float(n).is_integer():
        return True
    return any(abs(a - n) < 1e-9 or round(a) == n or round(a, 1) == n or round(a, 2) == n for a in allowed)


def verify(brief, decisions, claims, metrics, check_lines, conflicts, injections, coverage=False):
    v = []
    secs = split_sections(brief)
    for name in SECTION_NAMES:
        if name not in secs:
            v.append(f"Missing section: {name}")
    if v:
        return v

    included = {d for d, x in decisions.items() if x["included"]}
    body = "\n".join(secs[k] for k in SECTION_NAMES[:4])
    words = len(body.split())
    if words > MAX_WORDS:
        v.append(f"Too long: {words} words from Snapshot to Open questions (max {MAX_WORDS}). Cut the weakest points.")
    snap_lines = [l for l in secs["snapshot"].splitlines() if l.strip()]
    if len(snap_lines) > MAX_SNAPSHOT_LINES:
        v.append(f"Snapshot has {len(snap_lines)} lines (max {MAX_SNAPSHOT_LINES}).")

    for sec in ("bull case", "bear case"):
        n_bul = len([l for l in secs[sec].splitlines() if l.strip().startswith("-")])
        if n_bul > 5:
            v.append(f"[{sec}] has {n_bul} bullets (max 5). Keep the strongest 3-5.")

    # allowed numbers
    by_doc = defaultdict(list)
    for c in claims:
        by_doc[c.doc_id] += numbers_in(c.text)
    for m in metrics:
        by_doc[m.doc_id] += [m.value] + ([m.stated_yoy_pct] if m.stated_yoy_pct is not None else [])
    glob = [n for l in check_lines for n in numbers_in(l)] + [m.value for m in metrics]
    everything = glob + [n for lst in by_doc.values() for n in lst]

    for sec in ("snapshot", "bull case", "bear case"):
        for line in [l for l in secs[sec].splitlines() if l.strip()]:
            cites = re.findall(r"\bS(\d+)\b", " ".join(re.findall(r"\[(S[\d,\sS]+)\]", line)))
            ids = {f"S{c}" for c in cites}
            if not ids:
                v.append(f"[{sec}] no citation: \"{line.strip()[:70]}...\"")
                continue
            for i in ids:
                if i not in included:
                    v.append(f"[{sec}] cites {i}, which is not an included source: \"{line.strip()[:60]}...\"")
            allowed = glob + [n for i in ids for n in by_doc.get(i, [])]
            for n in numbers_in(line):
                if not num_ok(n, allowed):
                    v.append(f"[{sec}] number {n:g} is not in the cited source(s) {sorted(ids)}: \"{line.strip()[:70]}...\"")
        if re.search(r"\b(strong buy|buy|sell|price target|target price|upside|multibagger)\b", secs[sec], re.I):
            v.append(f"[{sec}] contains investment-advice language (buy/sell/target/upside).")

    for n in numbers_in(secs["open questions"]):
        if not num_ok(n, everything):
            v.append(f"[open questions] number {n:g} does not appear in any source or check.")

    for sec in SECTION_NAMES[:4]:
        for br in re.findall(r"\[([^\]]+)\]", secs[sec]):
            if not re.fullmatch(r"S\d+(?:\s*,\s*S\d+)*", br.strip()):
                v.append(f"[{sec}] \"[{br}]\" is not a source citation. Cite only [S#] sources.")
    if re.search(r"\bpython\b|\bverifier\b", "\n".join(secs[k] for k in SECTION_NAMES[:4]), re.I):
        v.append("The brief mentions internal tooling (PYTHON/verifier). Remove it; the reader only sees sources.")
    pool = " ".join([c.text for c in claims] + list(check_lines) + [x["name"] for x in decisions.values()])
    norm = lambda t: re.sub(r"FY(?:20)?(\d{2})$", r"FY\1", re.sub(r"\s", "", t.upper()))
    known_fy = {norm(t) for t in re.findall(r"FY\s?\d{2,4}", pool, re.I)}
    for t in sorted({norm(t) for t in re.findall(r"FY\s?\d{2,4}", body, re.I)} - known_fy):
        v.append(f"The brief mentions {t}, which no source mentions. Use only periods that appear in the sources.")
    cited_body = set(re.findall(r"\bS\d+\b", "\n".join(secs[k] for k in SECTION_NAMES[:4])))
    unused_ok = {i for i in included - cited_body if any(
        re.search(rf"\b{i}\b", l) and re.search(r"\bunused\b|\bnot used\b", l, re.I)
        for l in secs["sources"].splitlines())}
    for i in sorted(included - cited_body - unused_ok):
        v.append(f"Included source {i} is never used in the brief body (use it, or list it in Sources with the word 'unused').")
    for d in decisions:
        if not re.search(rf"\b{d}\b", secs["sources"]):
            v.append(f"Sources section does not list {d}.")
    oq = secs["open questions"]
    if not any(x["included"] and x["tier"] <= 2 for x in decisions.values()) and not re.search(r"official|filing", oq, re.I):
        v.append("[open questions] Say that no official filings or earnings-call transcripts were included: the figures come from aggregated data and the news items are headlines only.")
    for key, b, losers in conflicts:
        for did in {b.doc_id} | {m.doc_id for m in losers}:
            if not re.search(rf"\b{did}\b", oq):
                v.append(f"Open questions must report the {key} conflict and mention {did}.")
    for did in injections:
        if not re.search(rf"\b{did}\b", oq):
            v.append(f"Open questions must mention the manipulation attempt in {did}.")
    if coverage:
        v += coverage_rules(secs, check_lines)
    return v



def body_text(text):
    """Document text without its front-matter header."""
    if text.lstrip().startswith("---"):
        parts = text.lstrip().split("---", 2)
        if len(parts) == 3:
            return parts[2]
    return text


def missing_figures(evidence, claims, metrics):
    """Recall check: figures that appear in a source document but in none of its extracted claims/metrics."""
    out = {}
    for doc_id, _fn, text in evidence:
        covered = [n for c in claims if c.doc_id == doc_id for n in numbers_in(c.text)]
        for m in metrics:
            if m.doc_id == doc_id:
                covered += [m.value] + ([m.stated_yoy_pct] if m.stated_yoy_pct is not None else [])
        miss = []
        for n in numbers_in(body_text(text)):
            if not num_ok(n, covered) and n not in miss:
                miss.append(n)
        if miss:
            out[doc_id] = miss
    return out


def has_num(section, n):
    return re.search(rf"(?<![\d.]){re.escape(n)}(?!\d)", section) is not None


def coverage_rules(secs, check_lines):
    """Verify that the important findings in PYTHON CHECKS actually reach the right section of the brief."""
    v = []
    oq, bear, bull = secs["open questions"], secs["bear case"], secs["bull case"]
    for l in check_lines:
        m = re.search(r"MARGIN with revenue (S\d+)=([\d.]+): .*?revenue = ([\d.]+)%; stated margin ([\d.]+)%.*DOES NOT RECONCILE", l)
        if m and not has_num(oq, m.group(3)):
            v.append(f"[coverage] Open questions must show the arithmetic that rules out {m.group(1)}'s revenue: "
                     f"EBITDA / revenue = {m.group(3)}%, but the stated margin is {m.group(4)}%.")
        g = re.search(r"growth gap: revenue ([+-][\d.]+)% vs EBITDA ([+-][\d.]+)%.*lagging", l)
        if g:
            for n in g.groups():
                if not has_num(bear, n.lstrip("+-")):
                    v.append(f"[coverage] Bear case must compare revenue growth with profit growth and include {n}% (profit growth lags sales growth).")
        h = re.search(r"DERIVED (FII|DII) holding change.*\(([+-])[\d.]+\)", l)
        if h:
            target, name = (bull, "Bull") if h.group(2) == "+" else (bear, "Bear")
            if not re.search(r"FII|DII|institution", target, re.I):
                v.append(f"[coverage] {h.group(1)} holding {'rose' if h.group(2) == '+' else 'fell'}: mention institutional holding in the {name} case.")
        pl = re.search(r"DERIVED promoter pledge change.*\(-[\d.]+\)", l)
        if pl and "pledge" in bear.lower():
            v.append("[coverage] The promoter pledge fell: do not list it as a risk in the Bear case (put it in the Bull case).")
    if not re.search(r"price|valuation", oq, re.I):
        v.append("[coverage] Open questions must say that current share price / valuation / cash flow are missing from the sources.")
    if re.search(r"which (figure|number|revenue).{0,40}(correct|right)", oq, re.I):
        v.append("[coverage] Do not ask the reader which figure is correct; state which source reconciles, using the arithmetic.")
    for sec in ("snapshot", "bull case", "bear case"):
        for line in secs[sec].splitlines():
            if re.search(r"share|stock", line, re.I) and "\u20b9" in line and not (re.search(MON, line) or re.search(r"as of", line, re.I)):
                v.append(f"[coverage] A share price must be dated (\"as of <date>\") and called not current: \"{line.strip()[:60]}...\"")
    return v

def key_findings(check_lines, decisions, claims):
    """Turn the Python checks into ready-made sentences for the writer. The model composes the brief around them
    instead of having to discover them. Returns [(section, sentence)]."""
    f, lines = [], check_lines
    bad = good = None
    for l in lines:
        m = re.search(r"MARGIN with revenue (S\d+)=([\d.]+): EBITDA ([\d.]+) \((S\d+)\) / revenue = ([\d.]+)%; stated margin ([\d.]+)% .*-> (DOES NOT RECONCILE|RECONCILES)", l)
        if m:
            (bad if m.group(7).startswith("DOES") else None)
            rec = dict(id=m.group(1), rev=float(m.group(2)), ebitda=m.group(3), calc=m.group(5), stated=m.group(6))
            if m.group(7).startswith("DOES"):
                bad = rec
            else:
                good = rec
    if bad and good:
        f.append(("open questions",
                  f"{bad['id']} reports Q1 revenue of ₹{bad['rev']:,.0f} cr but {good['id']} reports ₹{good['rev']:,.0f} cr. "
                  f"With {bad['id']}'s figure, EBITDA / revenue = {bad['calc']}%, not the stated margin of {bad['stated']}%; "
                  f"with {good['id']}'s figure it is {good['calc']}%, which reconciles, so {good['id']} is the reliable figure [{bad['id']}][{good['id']}]."))
    for l in lines:
        g = re.search(r"growth gap: revenue ([+-][\d.]+)% vs EBITDA ([+-][\d.]+)% \((S\d+)\); profit growth is lagging", l)
        if g:
            f.append(("bear case", f"Profit growth lags sales growth: revenue {g.group(1)}% but EBITDA {g.group(2)}% [{g.group(3)}]."))
        r = re.search(r"DERIVED receivable days change: ([\d.]+) -> ([\d.]+) .*\((S\d+)\)", l)
        if r:
            f.append(("bear case", f"Receivable days rose from {float(r.group(1)):g} to {float(r.group(2)):g} [{r.group(3)}]."))
        q = re.search(r"DERIVED GST demand vs one quarter of profit: ([\d.]+) cr is about (\d+)% of Q1 FY27 PAT of ([\d.]+) cr \((S\d+), (S\d+)\)", l)
        if q:
            f.append(("bear case", f"The GST demand of ₹{q.group(1)} cr is about {q.group(2)}% of Q1 FY27 profit (₹{q.group(3)} cr); "
                                   f"add the company's stated view, labelled 'management says' [{q.group(4)}]."))
    if decisions and not any(x["included"] and x["tier"] <= 2 for x in decisions.values()):
        f.append(("open questions", "No official filings or earnings-call transcripts were included; the figures come from aggregated market data and the news items are headlines only (article text was not retrieved)."))
    inst = {}
    for l in lines:
        h = re.search(r"DERIVED (FII|DII) holding change \(pct points\): ([\d.]+) -> ([\d.]+) \(([+-][\d.]+)\) \((S\d+)\)", l)
        if h:
            inst[h.group(1)] = h.groups()
    if inst:
        rose = all(v[3].startswith("+") for v in inst.values())
        txt = " and ".join(f"{k} {'rose' if v[3].startswith('+') else 'fell'} from {v[1]}% to {v[2]}% ({v[3]} pts)" for k, v in inst.items())
        sid = next(iter(inst.values()))[4]
        f.append(("bull case" if rose else "bear case", f"Institutional holding between Jun 2025 and Jun 2026: {txt} [{sid}]."))
    for l in lines:
        pl = re.search(r"DERIVED promoter pledge change \(pct points\): ([\d.]+) -> ([\d.]+) \(([+-][\d.]+)\) \((S\d+)\)", l)
        if pl:
            f.append(("bull case" if pl.group(3).startswith("-") else "bear case",
                      f"Promoter pledge {'fell' if pl.group(3).startswith('-') else 'rose'} from {pl.group(1)}% to {pl.group(2)}% of promoter holding "
                      f"({pl.group(3)} pts) between Jun 2025 and Jun 2026 [{pl.group(4)}]."))
    for d, x in decisions.items():
        if x.get("injection"):
            f.append(("open questions", f"{d} ({x['name']}) contained hidden text instructing AI tools what to say; it was excluded and nothing from it was used [{d}]."))
    price = next((c for c in claims if "\u20b9" in c.text and re.search(r"share|stock", c.text, re.I)), None)
    if price:
        amt = re.search(r"\u20b9([\d,]+)", price.text).group(1)
        f.append(("open questions", f"No current share price, valuation or cash-flow data is in the sources; the only price is ₹{amt} "
                                    f"from {price.doc_id} (article dated {decisions[price.doc_id]['published']}), which is out of date [{price.doc_id}]."))
    elif not any(re.search(r"current price|market cap|p/e", c.text, re.I) for c in claims):
        f.append(("open questions", "No current share price, valuation or cash-flow data is in the sources."))
    return f


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", default="research_pack")
    ap.add_argument("--prompt", default="system_prompt.md")
    ap.add_argument("--ticker", default="SRVCABLE")
    ap.add_argument("--today", default="23 September 2026")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--label", default="run3")
    ap.add_argument("--coverage", action="store_true", help="Run 4: recall check + coverage rules")
    args = ap.parse_args()

    docs = load_documents(args.pack)

    print("Step 1: triage")
    triage = ask_json(TRIAGE_PROMPT, build_user_message(args.ticker, args.today, docs), args.model, TriageOut).triage

    print("Step 2: include/exclude rules (Python)")
    decisions = decide(triage, docs, args.today)
    for d, x in decisions.items():
        print(f"  {d}: {'INCLUDED' if x['included'] else 'EXCLUDED - ' + x['reason']}")
    evidence = [d for d in docs if decisions[d[0]]["included"]]
    ev_msg = build_user_message(args.ticker, args.today, evidence)

    print("Step 3: extraction (claims, metrics) from included documents only")
    claims = ask_json(CLAIMS_PROMPT, ev_msg, args.model, ClaimsOut).claims
    metrics = ask_json(METRICS_PROMPT, ev_msg, args.model, MetricsOut).metrics
    ok_ids = {d for d, x in decisions.items() if x["included"]}
    claims = [c for c in claims if c.doc_id in ok_ids]
    metrics = [m for m in metrics if m.doc_id in ok_ids]

    for doc_id, _fn, text in evidence:
        if "headline" in parse_front(text).get("type", "").lower():
            title = next((l[2:].strip() for l in body_text(text).splitlines() if l.startswith("# ")), "")
            if title:
                claims = [c for c in claims if c.doc_id != doc_id]
                claims.append(Claim(doc_id=doc_id, kind="reported_fact", text=f"Headline reports: {title}"))

    miss = {}
    if args.coverage:
        miss = missing_figures(evidence, claims, metrics)
        if miss:
            print("  recall check: figures in the sources but missing from the claims: "
                  + "; ".join(f"{d}: " + ", ".join(f"{n:g}" for n in ns) for d, ns in miss.items()))
            sub = [d for d in evidence if d[0] in miss]
            note = "\n\nMISSING FIGURES (write one claim for each):\n" + "\n".join(
                f"{d}: " + ", ".join(f"{n:g}" for n in ns) for d, ns in miss.items())
            extra = ask_json(RECALL_PROMPT, build_user_message(args.ticker, args.today, sub) + note,
                             args.model, ClaimsOut).claims
            claims += [c for c in extra if c.doc_id in ok_ids]
        else:
            print("  recall check: no figures missing")

    print("Step 4: Python checks")
    tier = {d: x["tier"] for d, x in decisions.items()}
    check_lines, conflicts = run_checks(metrics, tier)
    claims, dropped = quarantine(claims, conflicts)
    print("\n".join("  " + l for l in check_lines))
    for c in dropped:
        print(f"  QUARANTINED claim from {c.doc_id}: {c.text[:80]}")

    injections = [d for d, x in decisions.items() if x.get("injection")]
    sources_txt = "\n".join(
        f"{d} | {x['name']} | {x['published']} | tier {x['tier']} | "
        + ("INCLUDED" if x["included"] else "EXCLUDED: " + x["reason"]) for d, x in decisions.items())
    claims_txt = "\n".join(f"[{c.doc_id}][{c.kind}] {c.text}" for c in claims)
    findings = key_findings(check_lines, decisions, claims)
    findings_txt = "\n".join(f"[{sec}] {txt}" for sec, txt in findings)
    user = (f"TICKER: {args.ticker}\nTODAY: {args.today}\n\nSOURCES (decided by code):\n{sources_txt}\n\n"
            f"CLAIMS (included sources only):\n{claims_txt}\n\nPYTHON CHECKS:\n" + "\n".join(check_lines)
            + "\n\nREQUIRED FINDINGS (computed by code; include EACH in the section named in brackets; tighten the wording "
              "if you need to, but keep every figure and citation; write rupees as the rupee sign + number + cr):\n" + findings_txt)
    system = Path(args.prompt).read_text(encoding="utf-8") + ADDENDUM

    print("Step 5: write + verify")
    brief = call_llm(system, user, args.model, args.temperature)
    log, seen = [], []
    for rnd in range(4):  # draft + up to 3 rewrites
        violations = verify(brief, decisions, claims, metrics, check_lines, conflicts, injections, args.coverage)
        log.append({"round": rnd, "violations": violations})
        if rnd > 0 and violations and violations == log[rnd - 1]["violations"]:
            print("  verifier stalled: same problems as the previous round; stopping.")
            break
        print(f"  verifier round {rnd}: " + ("PASSED" if not violations else f"{len(violations)} problem(s)"))
        for x in violations:
            print("    -", x)
        if not violations or rnd == 3:
            break
        seen += [x for x in violations if x not in seen]
        fix = (user + "\n\nPREVIOUS DRAFT:\n" + brief + "\n\nA VERIFIER FOUND THESE PROBLEMS. Fix every one and output "
               "the COMPLETE corrected brief in the same format. The list includes problems from EARLIER drafts: the final brief "
               "must have NONE of them, so do not undo earlier fixes:\n- " + "\n- ".join(seen))
        brief = call_llm(system, fix, args.model, args.temperature)

    if log[-1]["violations"]:
        print("\nWARNING: the verifier still reports problems after 3 rewrites (see verifier_log in the .extract.json).")
    status = "PASSED" if not log[-1]["violations"] else "FAILED"
    path = save_run(args.label, f"{args.model} | verifier: {status}", args.temperature, args.prompt, brief)
    path.with_suffix(".extract.json").write_text(json.dumps({
        "decisions": decisions,
        "extraction": Extraction(triage=triage, claims=claims, metrics=metrics).model_dump(),
        "python_checks": check_lines,
        "required_findings": findings,
        "quarantined_claims": [c.model_dump() for c in dropped],
        "recall_missing_figures": {d: ns for d, ns in miss.items()},
        "verifier_log": log,
    }, indent=2), encoding="utf-8")
    print("\n" + brief)
    print(f"\n[saved to {path} and {path.with_suffix('.extract.json')}]")


if __name__ == "__main__":
    main()
