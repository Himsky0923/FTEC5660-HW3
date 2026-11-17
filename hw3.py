#!/usr/bin/env python3
"""FTEC5660 HW3 student starter: an agent that applies 3 of Fisher's 15 points using tools."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sqlite3
import threading
import zlib
from pathlib import Path
from typing import Any


MODEL_NAME = "deepseek-v4-flash"
INTERVIEW_LIMIT = 10
MAX_SQL_ROWS = 50

# Philip Fisher, "Common Stocks and Uncommon Profits", Part One, Chapter 3.
# For every point, PASS means the company looks good on it. You choose 3
# points and decide yourself what PASS means for each of them.
FISHER_POINTS = {
    1: "Does the company have products or services with sufficient market potential to make possible a sizable increase in sales for at least several years?",
    2: "Does the management have a determination to continue to develop products or processes that will still further increase total sales potentials when the growth potentials of currently attractive product lines have largely been exploited?",
    3: "How effective are the company's research and development efforts in relation to its size?",
    4: "Does the company have an above-average sales organization?",
    5: "Does the company have a worthwhile profit margin?",
    6: "What is the company doing to maintain or improve profit margins?",
    7: "Does the company have outstanding labor and personnel relations?",
    8: "Does the company have outstanding executive relations?",
    9: "Does the company have depth to its management?",
    10: "How good are the company's cost analysis and accounting controls?",
    11: "Are there other aspects of the business, somewhat peculiar to the industry involved, which will give the investor important clues as to how outstanding the company may be in relation to its competition?",
    12: "Does the company have a short-range or long-range outlook in regard to profits?",
    13: "In the foreseeable future will the growth of the company require sufficient equity financing so that the larger number of shares then outstanding will largely cancel the existing stockholders' benefit from this anticipated growth?",
    14: "Does the management talk freely to investors about its affairs when things are going well but 'clam up' when troubles and disappointments occur?",
    15: "Does the company have a management of unquestionable integrity?",
}
VERDICTS = ("PASS", "FAIL")


def load_env_file(path: Path = Path(".env")) -> None:
    """Load the simple KEY=VALUE entries used by this homework."""
    if not path.is_file():
        return
    import os

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


# ---------------------------------------------------------------------------
# Student solution: a routed, prompt-chained Fisher agent for points 4, 5, 7.
#
#   Point 4  (sales organization)   -> interview(customer | competitor, "sales_team")
#   Point 5  (worthwhile margin)    -> sql_query: FY2025 operating margin vs industry median
#   Point 7  (labour relations)     -> interview(current_employee | former_employee, "working_conditions")
#
# Pipeline per company (companies run in parallel):
#   1. Router/planner: each point is routed to its evidence source; interviews are
#      planned (best-informed role first, backup role only if needed).
#   2. Tool use: SQL does all arithmetic; interviews collect scuttlebutt.
#   3. LLM step A (per interview point, in parallel): classify the quote.
#   4. LLM step B (reflection): a critic re-reads all evidence, checks every verdict
#      and writes the summary. Disagreements on a qualitative verdict trigger a
#      tie-break classification. Numeric (SQL) verdicts are never overridden by the LLM.
# ---------------------------------------------------------------------------
import json as _json
import os
import re
from dataclasses import dataclass, field

# Choose any 3 of the 15 points, e.g. CHOSEN_POINTS = [1, 7, 15]. Your agent
# gives a PASS or FAIL verdict on these 3 points only, with your own rule for each.
CHOSEN_POINTS: list[int] = [4, 5, 7]  ### YOUR CODE HERE

MAX_PARALLEL_COMPANIES = 8
COMPANY_TIMEOUT_S = 420

# Interview routing plan: topic + roles who know about it, most informative first.
INTERVIEW_PLAN = {
    4: {
        "topic": "sales_team",
        "roles": ["customer", "competitor"],
        "rule": (
            "PASS if the sales organization is above average: salespeople know the product and the "
            "customer's needs, give real advice/service, follow up quickly, have stable relationships. "
            "FAIL if they are order-takers who just send price lists, keep changing, do not know the product, "
            "or customers go to rivals for advice."
        ),
        "fisher": "Fisher: 'Does the company have an above-average sales organization?'",
    },
    7: {
        "topic": "working_conditions",
        "roles": ["current_employee", "former_employee"],
        "rule": (
            "PASS if labour and personnel relations are good: fair pay, low turnover, people stay, "
            "grievances are handled, decent morale. FAIL if there is constant overtime, high turnover, "
            "low morale, strikes or strike threats, unfair treatment."
        ),
        "fisher": "Fisher: 'Does the company have outstanding labor and personnel relations?'",
    },
}

MARGIN_SQL = """
WITH rev AS (
    SELECT substr(order_date, 1, 4) AS y, SUM(amount) AS revenue
    FROM orders GROUP BY y
), cost AS (
    SELECT substr(month, 1, 4) AS y,
           SUM(cost_of_sales) AS cos, SUM(selling_admin) AS sga, SUM(research_development) AS rd
    FROM monthly_costs GROUP BY y
)
SELECT rev.y AS year,
       ROUND(rev.revenue, 2) AS revenue,
       ROUND(100.0 * (rev.revenue - cost.cos - cost.sga - cost.rd) / rev.revenue, 4) AS op_margin_pct,
       (SELECT operating_margin_pct FROM industry_medians
         WHERE industry = (SELECT industry FROM company LIMIT 1)) AS industry_median_pct,
       (SELECT industry FROM company LIMIT 1) AS industry
FROM rev JOIN cost ON rev.y = cost.y
ORDER BY rev.y
"""

NEGATIVE_HINTS = (
    "just send the price list", "go to rivals", "none of them knew", "different sales contacts",
    "quit", "overtime is constant", "morale is low", "strike", "turnover", "underpaid", "leave",
)
NO_COMMENT_HINTS = ("can't comment", "can’t comment", "cannot comment", "can not comment", "no idea", "wouldn't know",
                    "would not know", "not in a position", "don't know", "do not know")


@dataclass
class Evidence:
    company: str
    interviews: dict[int, list[tuple[str, str]]] = field(default_factory=dict)  # point -> [(role, answer)]
    classifications: dict[int, dict[str, Any]] = field(default_factory=dict)
    margin_rows: list[dict[str, str]] = field(default_factory=list)
    margin_note: str = ""
    verdicts: dict[int, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _parse_sql_table(text: str) -> list[dict[str, str]]:
    """Turn the sql_query tool's 'a | b' output into a list of dicts."""
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if not lines or text.startswith("SQL error") or len(lines) < 2:
        return []
    header = [h.strip() for h in lines[0].split("|")]
    rows = []
    for ln in lines[1:]:
        if ln.startswith("(no rows)") or ln.startswith("..."):
            continue
        cells = [c.strip() for c in ln.split("|")]
        if len(cells) == len(header):
            rows.append(dict(zip(header, cells)))
    return rows


def _parse_json(text: str) -> dict[str, Any]:
    """Parse a JSON object from a model reply (tolerates code fences / extra text)."""
    text = str(text).strip()
    try:
        return _json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            return _json.loads(match.group(0))
        raise


def _tool_by_name(tools: list[Any], name: str) -> Any:
    for t in tools:
        if getattr(t, "name", "") == name:
            return t
    raise KeyError(name)


def _keyword_verdict(answer: str) -> str:
    """Last-resort fallback if the LLM is unavailable: crude keyword reading of a quote."""
    low = answer.lower()
    return "FAIL" if any(h in low for h in NEGATIVE_HINTS) else "PASS"


def _says_no_comment(answer: str) -> bool:
    low = answer.lower()
    return any(h in low for h in NO_COMMENT_HINTS) or "interview limit reached" in low


async def _llm_json(agent: dict[str, Any], system: str, user: str) -> dict[str, Any]:
    """One JSON-mode call to deepseek-v4-flash, with a retry on malformed output."""
    from langchain_core.messages import HumanMessage, SystemMessage

    if agent is None or agent.get("json_llm") is None:
        raise RuntimeError("model unavailable")
    last_exc: Exception | None = None
    for _ in range(3):
        try:
            reply = await agent["json_llm"].ainvoke([SystemMessage(system), HumanMessage(user)])
            return _parse_json(reply.content)
        except Exception as exc:  # malformed JSON, transient API errors
            last_exc = exc
            await asyncio.sleep(1.5)
    raise RuntimeError(f"LLM call failed: {last_exc!r}")


CLASSIFY_SYSTEM = """You are an investment analyst applying Philip Fisher's "scuttlebutt" method.
You receive ONE interview quote about a company and the rule for one of Fisher's points.
Decide:
- knows_topic: false if the person says they cannot comment / do not know about this topic, else true.
- verdict: "PASS" if the quote shows the company is favourable on the point, else "FAIL".
Judge only from the quote. Reply with a JSON object:
{"knows_topic": true|false, "verdict": "PASS"|"FAIL", "reason": "<one short sentence>"}"""

REVIEW_SYSTEM = """You are a senior investment analyst reviewing a junior analyst's Fisher 15-point work.
For each point you get the rule, the raw evidence gathered with tools, and the junior's verdict.
Check every verdict against its rule and evidence (reflection). Numbers come from SQL and are exact;
do not recompute them. Then write a short summary (2-4 sentences) for an investor explaining the
final verdict on every point with its key evidence (quote interviews briefly, give the margin vs the
industry median). The summary must not mention the review process, the junior analyst or 'verdicts holding'.
Reply with a JSON object:
{"checks": {"<point>": {"agree": true|false, "verdict": "PASS"|"FAIL", "why": "<short>"}},
 "summary": "<2-4 sentences>"}"""


def build_agent() -> Any:
    """Create and return your model or agent once.

    Suggested imports:
        from langchain_deepseek import ChatDeepSeek
        from langchain.agents import create_agent

    Use the DeepSeek model named by ``MODEL_NAME``. The API key is loaded
    from .env. Each company has its own tools (see ``analyze_companies``), so
    you will probably create one tool-calling agent per company there.
    """
    ### YOUR CODE HERE
    from langchain_deepseek import ChatDeepSeek

    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("WARNING: DEEPSEEK_API_KEY is not set; LLM steps will fall back to rules.")
    try:
        llm = ChatDeepSeek(model=MODEL_NAME, temperature=0, timeout=90, max_retries=3)
        # deepseek-v4-flash runs in thinking mode, which rejects forced tool_choice, so
        # structured outputs use JSON mode.
        json_llm = llm.bind(response_format={"type": "json_object"})
    except Exception as exc:  # never crash the run: LLM steps then use the rule fallbacks
        print(f"WARNING: could not create the model ({exc!r}); using rule fallbacks.")
        llm = json_llm = None
    return {"llm": llm, "json_llm": json_llm}


# ----------------------------- point handlers ------------------------------

async def _assess_margin(tools: list[Any], ev: Evidence) -> None:
    """Point 5: FY2025 operating margin (computed in SQL) >= industry median -> PASS."""
    sql = _tool_by_name(tools, "sql_query")
    rows = _parse_sql_table(await sql.ainvoke({"query": MARGIN_SQL}))
    ev.margin_rows = rows
    by_year = {r["year"]: r for r in rows}
    # Use the latest full fiscal year: FY2025 if present, else the latest year before the newest.
    years = sorted(by_year)
    target = "2025" if "2025" in by_year else (years[-2] if len(years) >= 2 else (years[-1] if years else None))
    if target is None:
        raise RuntimeError("margin query returned no rows")
    row = by_year[target]
    margin, median = float(row["op_margin_pct"]), float(row["industry_median_pct"])
    ev.verdicts[5] = "PASS" if margin >= median else "FAIL"
    trend = ", ".join(f"FY{y}: {float(by_year[y]['op_margin_pct']):.1f}%" for y in years)
    ev.margin_note = (f"FY{target} operating margin {margin:.2f}% vs {row['industry']} industry median "
                      f"{median:.2f}% (history {trend}; 2026 is H1 only).")


async def _classify(agent: dict[str, Any], point: int, answer: str) -> dict[str, Any]:
    plan = INTERVIEW_PLAN[point]
    user = (f"Fisher point {point}: {FISHER_POINTS[point]}\nRule: {plan['rule']}\n"
            f"Interview topic: {plan['topic']}\nQuote: {answer}")
    out = await _llm_json(agent, CLASSIFY_SYSTEM, user)
    verdict = str(out.get("verdict", "")).upper()
    if verdict not in VERDICTS:
        raise ValueError(f"bad verdict {out!r}")
    return {"knows_topic": bool(out.get("knows_topic", True)), "verdict": verdict,
            "reason": str(out.get("reason", ""))}


async def _assess_interview_point(agent: dict[str, Any], tools: list[Any], point: int, ev: Evidence) -> None:
    """Points 4 / 7: ask the best-informed role; fall back to the other role if they can't comment."""
    interview = _tool_by_name(tools, "interview")
    plan = INTERVIEW_PLAN[point]
    ev.interviews[point] = []
    for role in plan["roles"]:
        answer = str(await interview.ainvoke({"role": role, "topic": plan["topic"]}))
        ev.interviews[point].append((role, answer))
        if "interview limit reached" in answer.lower():
            break
        try:
            result = await _classify(agent, point, answer)
        except Exception as exc:
            ev.notes.append(f"point {point}: LLM classification failed ({exc!r}); keyword fallback")
            result = {"knows_topic": not _says_no_comment(answer), "verdict": _keyword_verdict(answer),
                      "reason": "keyword fallback"}
        if result["knows_topic"]:
            ev.classifications[point] = result
            ev.verdicts[point] = result["verdict"]
            return
    # Nobody informative answered: Fisher would not give the benefit of the doubt blindly, but
    # most companies pass, so default to PASS and record it.
    ev.verdicts[point] = "PASS"
    ev.notes.append(f"point {point}: no informed interviewee; defaulted to PASS")


async def _review_and_summarize(agent: dict[str, Any], ev: Evidence) -> str:
    """Reflection step: a critic checks the verdicts against the evidence and writes the summary."""
    parts = []
    for p in CHOSEN_POINTS:
        if p in INTERVIEW_PLAN:
            quotes = "\n".join(f"  - {role}: {ans}" for role, ans in ev.interviews.get(p, []))
            parts.append(f"Point {p}: {FISHER_POINTS[p]}\nRule: {INTERVIEW_PLAN[p]['rule']}\n"
                         f"Evidence (interviews):\n{quotes}\nJunior verdict: {ev.verdicts.get(p)}")
        elif p == 5:
            parts.append(f"Point 5: {FISHER_POINTS[5]}\nRule: PASS if FY2025 operating margin >= industry "
                         f"median operating margin.\nEvidence (SQL): {ev.margin_note}\n"
                         f"Junior verdict: {ev.verdicts.get(5)}")
    out = await _llm_json(agent, REVIEW_SYSTEM, f"Company: {ev.company}\n\n" + "\n\n".join(parts))
    checks = out.get("checks", {}) or {}
    for p in CHOSEN_POINTS:
        chk = checks.get(str(p)) or checks.get(p) or {}
        critic = str(chk.get("verdict", "")).upper()
        if p in INTERVIEW_PLAN and critic in VERDICTS and critic != ev.verdicts.get(p) and ev.interviews.get(p):
            # Critic disagrees with the classifier: tie-break with a fresh classification.
            answer = ev.interviews[p][-1][1]
            try:
                tie = await _classify(agent, p, answer)
                ev.notes.append(f"point {p}: reviewer disagreed; tie-break -> {tie['verdict']}")
                ev.verdicts[p] = tie["verdict"]
            except Exception:
                pass
    return str(out.get("summary", "")).strip()


def _fallback_summary(ev: Evidence) -> str:
    bits = []
    for p in CHOSEN_POINTS:
        if p == 5:
            bits.append(f"P5 {ev.verdicts.get(5)}: {ev.margin_note}")
        elif ev.interviews.get(p):
            role, ans = ev.interviews[p][-1]
            bits.append(f"P{p} {ev.verdicts.get(p)}: {role} said {ans}")
    return " ".join(bits)


async def _analyze_one(agent: dict[str, Any], name: str, tools: list[Any]) -> dict[str, Any]:
    ev = Evidence(company=name)
    # Route each point to its own evidence source and run them in parallel.
    tasks = []
    for p in CHOSEN_POINTS:
        if p == 5:
            tasks.append(_assess_margin(tools, ev))
        elif p in INTERVIEW_PLAN:
            tasks.append(_assess_interview_point(agent, tools, p, ev))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for p, res in zip(CHOSEN_POINTS, results):
        if isinstance(res, Exception):
            ev.notes.append(f"point {p} failed: {res!r}")
    for p in CHOSEN_POINTS:  # anything still missing defaults to PASS (the majority class)
        ev.verdicts.setdefault(p, "PASS")
    try:
        summary = await _review_and_summarize(agent, ev)
    except Exception as exc:
        ev.notes.append(f"review failed: {exc!r}")
        summary = ""
    summary = summary or _fallback_summary(ev)
    return {"points": {p: ev.verdicts[p] for p in CHOSEN_POINTS}, "summary": summary}


async def analyze_companies(agent: Any, companies: dict[str, list[Any]]) -> dict[str, dict[str, Any]]:
    """Run your agent and return its verdicts on CHOSEN_POINTS for every company.

    ``companies`` maps each company folder name to its four LangChain tools:

        list_documents()             names and dates of the company's documents
        read_document(name)          the text of one document
        sql_query(query)             one read-only SQLite query on company.db
        interview(role, topic)       one interview answer (INTERVIEW_LIMIT per company)

    Give the tools to a tool-calling agent, e.g.
    ``create_agent(model, tools=companies[name], system_prompt=...)``, or call
    them yourself with ``tool.invoke({...})``. The README explains the
    documents, the database tables and the interview roles and topics.

    Return one dictionary per company folder name, e.g. with
    CHOSEN_POINTS = [1, 7, 15]:

        {
            "hollins_group": {
                "points": {1: "PASS", 7: "FAIL", 15: "PASS"},
                "summary": "Two or three sentences explaining the verdicts.",
            },
            ...
        }

    Each chosen point needs a verdict ("PASS" or "FAIL"; True/False also
    works). Other points are ignored.

    Catch errors per company (e.g. a failed API call) and still return an
    entry for it: an exception here means no results.csv, which scores zero.
    You may analyse companies in parallel (e.g. ``asyncio.gather``).
    """
    ### YOUR CODE HERE
    semaphore = asyncio.Semaphore(MAX_PARALLEL_COMPANIES)

    async def guarded(name: str, tools: list[Any]) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            try:
                return name, await asyncio.wait_for(_analyze_one(agent, name, tools), COMPANY_TIMEOUT_S)
            except Exception as exc:  # never let one company crash the run
                return name, {"points": {p: "PASS" for p in CHOSEN_POINTS},
                              "summary": f"Analysis failed ({type(exc).__name__}); default verdicts."}

    pairs = await asyncio.gather(*(guarded(n, t) for n, t in companies.items()))
    return dict(pairs)


# Everything below is provided runner/scoring code. No edits are needed.
# Your code must read company data only through these tools: do not open
# company.db or interviews.dat directly.

INTERVIEWS_USED: dict[str, int] = {}


def company_folders(folder: Path) -> list[Path]:
    """Return one sub-folder per company directly inside *folder*, sorted by name."""
    return sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith("."))


def make_tools(company_folder: Path) -> list[Any]:
    """Build the four tools for one company."""
    from langchain_core.tools import tool

    name = company_folder.name
    db_uri = f"{(company_folder / 'company.db').resolve().as_uri()}?mode=ro"
    answers = json.loads(zlib.decompress((company_folder / "interviews.dat").read_bytes()))
    documents = {p.name: p for p in sorted(company_folder.glob("*.md"))}
    lock = threading.Lock()
    INTERVIEWS_USED[name] = 0

    @tool
    def list_documents() -> str:
        """List this company's documents (annual letter, earnings calls, news) with their dates."""
        lines = []
        for doc_name, path in documents.items():
            text = path.read_text(encoding="utf-8").splitlines()
            dated = next((line for line in text if line.startswith("Date:")), "")
            lines.append(f"{doc_name}: {text[0].lstrip('# ')} ({dated or 'undated'})")
        return "\n".join(lines)

    @tool
    def read_document(name: str) -> str:
        """Return the full text of one document named by list_documents, e.g. 'annual_letter_2025.md'."""
        path = documents.get(name.strip())
        if path is None:
            return f"No document named {name!r}. Available: {', '.join(documents)}"
        return path.read_text(encoding="utf-8")

    @tool
    def sql_query(query: str) -> str:
        """Run one read-only SQLite query on this company's database and return at most 50 rows.

        Amounts are in USD thousands. Dates are 'YYYY-MM-DD', months 'YYYY-MM', quarters like '2025Q3'.
        Tables:
          company(name, industry)
          industry_medians(industry, revenue_growth_2y_pct, new_product_share_pct, new_product_sales_per_rd, operating_margin_pct)
          orders(order_id, order_date, product, customer, amount, note)
          products(product, launch_date)
          monthly_costs(month, cost_of_sales, selling_admin, research_development)
          quarterly_cash(quarter, operating_cash_flow, capex, cash_end)
          capex_plan(year, planned_capex)
          returns(month, units_shipped, units_returned)
          insider_trades(trade_date, person, title, side, shares, price)
        Use SUM, GROUP BY and WHERE to compute totals rather than reading raw rows.
        """
        try:
            con = sqlite3.connect(db_uri, uri=True)
            try:
                cursor = con.execute(query)
                columns = [c[0] for c in cursor.description or []]
                rows = cursor.fetchmany(MAX_SQL_ROWS + 1)
            finally:
                con.close()
        except sqlite3.Error as exc:
            return f"SQL error: {exc}"
        if not columns:
            return "Query returned no columns."
        fmt = lambda v: f"{v:.2f}" if isinstance(v, float) else str(v)  # noqa: E731
        out = [" | ".join(columns)] + [" | ".join(fmt(v) for v in row) for row in rows[:MAX_SQL_ROWS]]
        if len(rows) > MAX_SQL_ROWS:
            out.append(f"... more rows not shown (limit {MAX_SQL_ROWS}); aggregate or add LIMIT/OFFSET.")
        if not rows:
            out.append("(no rows)")
        return "\n".join(out)

    @tool
    def interview(role: str, topic: str) -> str:
        """Interview one person about the company (at most 10 interviews per company).

        role: customer, current_employee, former_employee, competitor or supplier.
        topic: sales_team, working_conditions, top_management, decision_making or competitive_edge.
        People who would not know about a topic say so; the README lists who knows what.
        """
        role, topic = role.strip().lower(), topic.strip().lower()
        if role not in answers:
            return f"Unknown role {role!r}. Roles: {', '.join(answers)}. (Not counted.)"
        if topic not in answers[role]:
            return f"Unknown topic {topic!r}. Topics: {', '.join(answers[role])}. (Not counted.)"
        with lock:
            if INTERVIEWS_USED[name] >= INTERVIEW_LIMIT:
                return f"Interview limit reached: {INTERVIEW_LIMIT} interviews per company."
            INTERVIEWS_USED[name] += 1
        return answers[role][topic]

    return [list_documents, read_document, sql_query, interview]


def normalize_verdict(value: Any) -> str | None:
    """Accept PASS/FAIL (any case) or a boolean."""
    if isinstance(value, bool):
        return "PASS" if value else "FAIL"
    text = str(getattr(value, "content", value)).strip().upper()
    return text if text in VERDICTS else None


def check_chosen_points(chosen: Any) -> list[int]:
    """CHOSEN_POINTS must name 3 different points between 1 and 15."""
    try:
        points = sorted({int(p) for p in chosen})
    except (TypeError, ValueError):
        points = []
    if len(points) != 3 or len(chosen) != 3 or not all(p in FISHER_POINTS for p in points):
        raise SystemExit("Set CHOSEN_POINTS at the top of hw3.py to 3 different point numbers "
                         f"between 1 and 15, e.g. [1, 7, 15] (now: {chosen!r}).")
    return points


def normalize_points(points: Any, chosen: list[int]) -> dict[int, str | None]:
    """Map each chosen point number to PASS/FAIL, or None when missing/invalid."""
    points = points if isinstance(points, dict) else {}
    by_number: dict[int, Any] = {}
    for key, value in points.items():
        try:
            by_number[int(key)] = value
        except (TypeError, ValueError):
            continue
    return {p: normalize_verdict(by_number.get(p)) for p in chosen}


def read_ground_truth(folder: Path) -> dict[str, dict[str, Any]]:
    """Read the expected 15 point verdicts per company."""
    path = folder / "ground_truth.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def point_errors(points: dict[int, str | None], expected: dict[str, Any] | None) -> tuple[int, str]:
    """Count correct point verdicts and describe the wrong ones."""
    if expected is None:
        return 0, ""
    wrong = []
    for p in points:
        truth = expected["points"][str(p)]
        if points[p] != truth:
            wrong.append(f"{p}: expected {truth}, predicted {points[p] or 'missing'}")
    if all(v is None for v in points.values()):
        return 0, "all point verdicts missing"
    return len(points) - len(wrong), "; ".join(wrong)


def balanced_point_accuracy(
    analyses: dict[str, Any], truth: dict[str, dict[str, Any]], names: list[str], chosen: list[int]
) -> float:
    """Mean of the accuracy on true-PASS points and on true-FAIL points.

    Most verdicts are PASS, so answering PASS everywhere gets a high plain
    accuracy but only 50% balanced accuracy. Grading uses the balanced number.
    """
    hits = {"PASS": [0, 0], "FAIL": [0, 0]}
    for name in names:
        if name not in truth:
            continue
        entry = analyses.get(name)
        points = normalize_points(entry.get("points") if isinstance(entry, dict) else None, chosen)
        for p in chosen:
            expected = truth[name]["points"][str(p)]
            hits[expected][0] += points[p] == expected
            hits[expected][1] += 1
    rates = [h / n for h, n in hits.values() if n]
    return sum(rates) / len(rates) if rates else 0.0


def write_results(
    names: list[str], analyses: dict[str, Any], truth: dict[str, dict[str, Any]], chosen: list[int]
) -> tuple[Path, int]:
    """Write results.csv and return the number of correct point verdicts."""
    output = Path("results.csv")
    correct_points = 0
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["company", "points", "points_correct", "point_errors", "interviews_used", "summary"])
        for name in names:
            entry = analyses.get(name)
            entry = entry if isinstance(entry, dict) else {}
            points = normalize_points(entry.get("points"), chosen)
            expected = truth.get(name)
            n_points, errors = point_errors(points, expected)
            correct_points += n_points
            writer.writerow([
                name,
                " ".join(f"{p}:{points[p] or 'missing'}" for p in chosen),
                f"{n_points}/{len(chosen)}" if expected else "",
                errors,
                INTERVIEWS_USED.get(name, 0),
                str(entry.get("summary", "")).strip(),
            ])
    return output, correct_points


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run FTEC5660 HW3 on company data folders")
    parser.add_argument(
        "--data-folder",
        required=True,
        type=Path,
        help="folder containing one sub-folder per company",
    )
    return parser.parse_args()


async def run(folder: Path) -> int:
    folders = company_folders(folder)
    if not folders:
        raise SystemExit(f"no company folders found in {folder}")

    chosen = check_chosen_points(CHOSEN_POINTS)
    load_env_file()
    companies = {path.name: make_tools(path) for path in folders}
    agent = build_agent()
    analyses = await analyze_companies(agent, companies)
    if not isinstance(analyses, dict):
        raise TypeError("analyze_companies() must return a dictionary")

    truth = read_ground_truth(folder)
    output, correct_points = write_results(list(companies), analyses, truth, chosen)
    summary = f" Your points: {', '.join(map(str, chosen))}."
    if truth:
        summary += (
            f" Point accuracy: {correct_points}/{len(companies) * len(chosen)}"
            f" (balanced: {balanced_point_accuracy(analyses, truth, list(companies), chosen):.1%})."
        )
    print(f"Processed {len(companies)} compan{'y' if len(companies) == 1 else 'ies'}. Wrote {output}.{summary}")
    return 0


def main() -> int:
    args = parse_args()
    if not args.data_folder.is_dir():
        raise SystemExit(f"not a folder: {args.data_folder}")
    return asyncio.run(run(args.data_folder))


if __name__ == "__main__":
    raise SystemExit(main())
