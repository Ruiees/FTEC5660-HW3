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
# Solution: a routed pipeline of tools, a document-scout agent, an LLM
# adjudicator and a deterministic verifier.
#
# Why points 1, 5 and 6? Section 3.3 of the brief routes all three to the
# database, so every number can come from SQL and Python instead of from the
# model's head (the tip in Section 3.3 says model arithmetic is the main cause
# of wrong verdicts). They also cover three different things an investor cares
# about: how fast the business grows, how profitable it is, and whether the
# profit is trending the right way. Only points 4, 7, 8, 9 and 11 need
# interviews, so all 10 interviews per company stay unspent.
#
# Per company:
#   1. ROUTE      each chosen point has a fixed rule and a fixed evidence plan
#                 (which query, which benchmark); nothing is left to chance.
#   2. SCOUT      a small tool-calling LangChain agent (list_documents,
#                 read_document) decides which document to open and reports
#                 what management claims. Its output is narrative, not verdicts.
#   3. ACT        sql_query is called directly and the returned tables are
#                 parsed in Python (3 queries, ~10 rows, well under the limit).
#   4. COMPUTE    growth and operating margins are computed in Python from the
#                 returned columns.
#   5. ADJUDICATE the model sees the computed table, the rule text and the
#                 claim, but never my verdict, so its answer is an independent
#                 second opinion rather than an echo of the verifier.
#   6. VERIFY     a deterministic verifier recomputes each verdict from the
#                 same numbers (Fisher's "reflection"). On a conflict the
#                 verifier wins and the prose is regenerated from the figures,
#                 so one misread digit cannot cost a point.
#   7. SUMMARISE  two or three sentences, model prose when it agrees with the
#                 verifier, a template built from the figures otherwise.
# ---------------------------------------------------------------------------

import os
import re
import sys
from dataclasses import dataclass
from typing import Literal

BASIS_YEAR = "2025"          # FY2025 is calendar year 2025
BASE_YEAR = "2023"           # start of the two-year growth window
PRIOR_YEAR = "2024"          # year the margin is compared with
COMPANY_TIMEOUT = 300        # seconds per company (whole run must be < 30 min)
MAX_PARALLEL = 6             # companies analysed at the same time
SCOUT_TIMEOUT = 150          # seconds for the document scout agent
ANNUAL_LETTER = "annual_letter_2025.md"
AGREEMENT = {"covered": 0, "agree": 0}  # adjudicator vs verifier, for the report

# Choose any 3 of the 15 points, e.g. CHOSEN_POINTS = [1, 7, 15]. Your agent
# gives a PASS or FAIL verdict on these 3 points only, with your own rule for each.
CHOSEN_POINTS: list[int] = [1, 5, 6]  ### YOUR CODE HERE

# One rule per chosen point. "field" is the metric the rule reads, "benchmark"
# the number it is compared with, and "text" is what the adjudicator applies.
RULES: dict[int, dict[str, str]] = {
    1: {
        "name": "Market potential for sales growth",
        "field": "growth_2y_pct",
        "benchmark": "median_growth_pct",
        "comparison": ">",
        "text": (
            "PASS if two-year revenue growth from FY2023 to FY2025, in percent, is strictly "
            "greater than the industry median two-year revenue growth. Revenue is the sum of "
            "orders.amount and growth is (FY2025 / FY2023 - 1) * 100."
        ),
    },
    5: {
        "name": "Worthwhile profit margin",
        "field": "margin_2025_pct",
        "benchmark": "median_margin_pct",
        "comparison": ">",
        "text": (
            "PASS if the FY2025 operating margin, in percent, is strictly greater than the "
            "industry median operating margin. Operating margin = (revenue - cost of sales - "
            "selling and admin - R&D) / revenue."
        ),
    },
    6: {
        "name": "Maintaining or improving margins",
        "field": "margin_delta_pp",
        "benchmark": "zero",
        "comparison": ">=",
        "text": (
            "PASS if the FY2025 operating margin is greater than or equal to the FY2024 "
            "operating margin, i.e. the margin did not fall (margin_delta_pp >= 0)."
        ),
    },
}

SQL_CONTEXT = """
SELECT c.name AS company_name, c.industry AS industry,
       m.revenue_growth_2y_pct AS median_growth_pct,
       m.operating_margin_pct AS median_margin_pct
FROM company c
JOIN industry_medians m ON m.industry = c.industry
""".strip()

SQL_REVENUE = f"""
SELECT substr(order_date, 1, 4) AS year, ROUND(SUM(amount), 2) AS revenue
FROM orders
WHERE substr(order_date, 1, 4) IN ('{BASE_YEAR}', '{PRIOR_YEAR}', '{BASIS_YEAR}')
GROUP BY year ORDER BY year
""".strip()

SQL_COSTS = f"""
SELECT substr(month, 1, 4) AS year,
       ROUND(SUM(cost_of_sales), 2) AS cost_of_sales,
       ROUND(SUM(selling_admin), 2) AS selling_admin,
       ROUND(SUM(research_development), 2) AS research_development
FROM monthly_costs
WHERE substr(month, 1, 4) IN ('{BASE_YEAR}', '{PRIOR_YEAR}', '{BASIS_YEAR}')
GROUP BY year ORDER BY year
""".strip()


def parse_table(text: str) -> list[dict[str, str]]:
    """Parse the 'a | b' table that sql_query returns into rows of dicts."""
    lines = [line for line in str(text).splitlines() if line.strip()]
    if len(lines) < 2 or "SQL error" in lines[0]:
        return []
    header = [cell.strip() for cell in lines[0].split("|")]
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        if line.startswith("..."):
            continue
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) != len(header):
            continue
        rows.append(dict(zip(header, cells)))
    return rows


def to_float(value: Any) -> float | None:
    """Tool output is text; convert it to a float or None."""
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}%"


def compare(value: float | None, threshold: float | None, op: str) -> bool:
    if value is None or threshold is None:
        return False
    if op == ">":
        return value > threshold
    if op == ">=":
        return value >= threshold
    if op == "<":
        return value < threshold
    return False


def find_tool(tools: list[Any], name: str, position: int) -> Any:
    """Locate one of the four tools by name, falling back to its position."""
    for tool in tools:
        if getattr(tool, "name", "") == name:
            return tool
    return tools[position]


async def call_tool(tool: Any, payload: dict[str, Any]) -> str:
    """Invoke one of the provided LangChain tools and return its text."""
    result = tool.ainvoke(payload)
    if hasattr(result, "__await__"):
        result = await result
    return str(getattr(result, "content", result))


def build_evidence(rows_context: list[dict[str, str]],
                   rows_revenue: list[dict[str, str]],
                   rows_costs: list[dict[str, str]]) -> dict[str, Any]:
    """Turn the three query results into the figures the rules need."""
    context = rows_context[0] if rows_context else {}
    revenue = {row["year"]: to_float(row["revenue"]) for row in rows_revenue}

    margins: dict[str, float | None] = {}
    for row in rows_costs:
        year = row["year"]
        parts = [to_float(row[key]) for key in
                 ("cost_of_sales", "selling_admin", "research_development")]
        total = sum(parts) if all(part is not None for part in parts) else None
        rev = revenue.get(year)
        margins[year] = (rev - total) / rev * 100 if (rev and total is not None) else None

    growth = None
    if revenue.get(BASIS_YEAR) and revenue.get(BASE_YEAR):
        growth = (revenue[BASIS_YEAR] / revenue[BASE_YEAR] - 1) * 100

    margin_now, margin_prior = margins.get(BASIS_YEAR), margins.get(PRIOR_YEAR)
    delta = margin_now - margin_prior if None not in (margin_now, margin_prior) else None

    return {
        "company": context.get("company_name", "unknown"),
        "industry": context.get("industry", "unknown"),
        "median_growth_pct": to_float(context.get("median_growth_pct")),
        "median_margin_pct": to_float(context.get("median_margin_pct")),
        "revenue": revenue,
        "margins": margins,
        "growth_2y_pct": growth,
        "margin_2025_pct": margin_now,
        "margin_2024_pct": margin_prior,
        "margin_delta_pp": delta,
        "narrative": "",
    }


def apply_rules(evidence: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Deterministic verdicts: this is both the verifier and the fallback."""
    results: dict[int, dict[str, Any]] = {}
    for point in CHOSEN_POINTS:
        rule = RULES[point]
        value = evidence.get(rule["field"])
        threshold = 0.0 if rule["benchmark"] == "zero" else evidence.get(rule["benchmark"])
        passed = compare(value, threshold, rule["comparison"])
        results[point] = {
            "verdict": "PASS" if passed else "FAIL",
            "value": value,
            "threshold": threshold,
        }
    return results


def describe(point: int, evidence: dict[str, Any], verdict: str) -> str:
    """One sentence of evidence for one point, built from the figures only."""
    outcome = "passes" if verdict == "PASS" else "fails"
    if point == 1:
        return (f"Two-year revenue growth of {percent(evidence.get('growth_2y_pct'))} against an "
                f"industry median of {percent(evidence.get('median_growth_pct'))} "
                f"({'beats' if verdict == 'PASS' else 'trails'} the industry), so point 1 {outcome}.")
    if point == 5:
        return (f"FY2025 operating margin of {percent(evidence.get('margin_2025_pct'))} against an "
                f"industry median of {percent(evidence.get('median_margin_pct'))} "
                f"({'sits above' if verdict == 'PASS' else 'sits below'} the median), so point 5 "
                f"{outcome}.")
    delta = evidence.get("margin_delta_pp")
    move = "unknown" if delta is None else f"{delta:+.2f}pp"
    return (f"Operating margin moved {move} between FY2024 "
            f"({percent(evidence.get('margin_2024_pct'))}) and FY2025 "
            f"({percent(evidence.get('margin_2025_pct'))}), so the margin "
            f"{'was held or widened' if verdict == 'PASS' else 'fell'} and point 6 {outcome}.")


def template_summary(evidence: dict[str, Any], verdicts: dict[int, dict[str, Any]]) -> str:
    """Deterministic summary, also used when the model is unavailable."""
    body = " ".join(describe(point, evidence, verdicts[point]["verdict"]) for point in CHOSEN_POINTS)
    passed = sum(item["verdict"] == "PASS" for item in verdicts.values())
    return (f"{evidence.get('company', 'The company')} ({evidence.get('industry', 'unknown')}): "
            f"{body} {passed} of {len(CHOSEN_POINTS)} chosen Fisher points pass on the database.")


def metrics_table(evidence: dict[str, Any]) -> str:
    """Plain-text table for the model; every figure already computed."""
    revenue = evidence.get("revenue", {})
    margins = evidence.get("margins", {})
    delta = evidence.get("margin_delta_pp")
    return "\n".join([
        f"- FY{BASE_YEAR} revenue: {revenue.get(BASE_YEAR, 0):,.0f} (USD thousands)",
        f"- FY{PRIOR_YEAR} revenue: {revenue.get(PRIOR_YEAR, 0):,.0f}",
        f"- FY{BASIS_YEAR} revenue: {revenue.get(BASIS_YEAR, 0):,.0f}",
        f"- Two-year revenue growth FY{BASE_YEAR} to FY{BASIS_YEAR}: "
        f"{percent(evidence.get('growth_2y_pct'))}",
        f"- Industry median two-year growth: {percent(evidence.get('median_growth_pct'))}",
        f"- FY{BASE_YEAR} operating margin: {percent(margins.get(BASE_YEAR))}",
        f"- FY{PRIOR_YEAR} operating margin: {percent(evidence.get('margin_2024_pct'))}",
        f"- FY{BASIS_YEAR} operating margin: {percent(evidence.get('margin_2025_pct'))}",
        f"- Change in operating margin FY{PRIOR_YEAR} to FY{BASIS_YEAR}: "
        + ("n/a" if delta is None else f"{delta:+.2f}pp"),
        f"- Industry median operating margin: {percent(evidence.get('median_margin_pct'))}",
    ])


SYSTEM_PROMPT = (
    "You are a sceptical equity analyst applying Philip Fisher's fifteen points to a company you "
    "have never heard of. You are given figures that a SQL engine has already computed, the exact "
    "rule to apply for each point, and an excerpt of what management says. Apply the rules "
    "literally to the given figures: do not re-derive them, do not round differently, never invent "
    "a number. Management's own words are not evidence, so use the claims only to check whether the "
    "story matches the figures."
)

SCOUT_SYSTEM = (
    "You are a document scout. You have two tools: list_documents and read_document. Open at most "
    "two documents and report, in under 120 words, exactly what management claims about (a) revenue "
    "growth and (b) profit margins, quoting at most one short phrase and naming the document you "
    "used. Do not judge, do not compute, do not invent anything that is not in the text."
)

SCOUT_TASK = (
    "Find what management claims about revenue growth and about profit margins. "
    "Start with list_documents, then read the most relevant document."
)


def build_prompt(evidence: dict[str, Any]) -> str:
    """Adjudication prompt: figures, rules and claims, but never my verdicts."""
    rules = "\n".join(f"Point {p} - {RULES[p]['name']}: {RULES[p]['text']}" for p in CHOSEN_POINTS)
    keys = ", ".join(f'"p{p}"' for p in CHOSEN_POINTS)
    narrative = evidence.get("narrative") or "(no document was read)"
    return (
        f"Company: {evidence.get('company')} (industry: {evidence.get('industry')})\n\n"
        f"Computed figures (amounts in USD thousands):\n{metrics_table(evidence)}\n\n"
        f"Rules to apply, verbatim:\n{rules}\n\n"
        f"What management claims (document excerpt, not evidence):\n"
        f'"""\n{narrative}\n"""\n\n'
        "Reply with a single JSON object and nothing else, in this shape:\n"
        '{"points": {"1": {"verdict": "PASS or FAIL", "reason": "one sentence quoting the '
        'figures you compared"}, ...}, "claims_check": "one sentence on whether the claims match '
        'the figures", "summary": "two or three sentences explaining the verdicts"}\n'
        f"Use exactly these keys under points: {keys}."
    )


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model reply, braces balanced."""
    start = text.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        loaded = json.loads(text[start:index + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(loaded, dict):
                        return loaded
                    break
        start = text.find("{", start + 1)
    return {}


def read_verdicts(payload: dict[str, Any]) -> dict[int, str | None]:
    """Pull PASS/FAIL for each chosen point out of the reply."""
    nested = payload.get("points") if isinstance(payload.get("points"), dict) else {}
    out: dict[int, str | None] = {}
    for point in CHOSEN_POINTS:
        candidates = [nested.get(str(point)), nested.get(f"p{point}"),
                      payload.get(str(point)), payload.get(f"p{point}")]
        verdict = None
        for candidate in candidates:
            if isinstance(candidate, dict):
                candidate = candidate.get("verdict")
            if isinstance(candidate, str) and candidate.strip().upper() in VERDICTS:
                verdict = candidate.strip().upper()
                break
        out[point] = verdict
    return out


@dataclass
class FisherAgent:
    """What build_agent() returns: the model and the adjudication chain."""
    model: Any = None
    analyst: Any = None


def build_agent() -> Any:
    """Create the DeepSeek model and the prompt chain once."""
    ### YOUR CODE HERE
    try:
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_deepseek import ChatDeepSeek
    except Exception as exc:  # pragma: no cover - dependency missing
        print(f"[agent] LangChain unavailable ({exc}); verifier only", file=sys.stderr)
        return FisherAgent()

    if not os.getenv("DEEPSEEK_API_KEY"):
        print("[agent] DEEPSEEK_API_KEY is not set; verifier only", file=sys.stderr)
        return FisherAgent()

    try:
        model = ChatDeepSeek(model=MODEL_NAME, temperature=0, timeout=120, max_retries=2)
    except Exception as exc:  # pragma: no cover - bad key or bad client
        print(f"[agent] could not build {MODEL_NAME} ({exc}); verifier only", file=sys.stderr)
        return FisherAgent()

    # No with_structured_output(): this model runs in thinking mode and rejects
    # a forced tool_choice, so the reply is plain JSON that we parse ourselves.
    prompt = ChatPromptTemplate.from_messages([("system", SYSTEM_PROMPT), ("human", "{prompt}")])
    return FisherAgent(model=model, analyst=prompt | model)


async def scout_claims(agent: FisherAgent, tools: list[Any]) -> str:
    """Step 2: a small tool-calling agent reads the documents and quotes claims."""
    from langchain.agents import create_agent

    scout = create_agent(
        agent.model,
        tools=[find_tool(tools, "list_documents", 0), find_tool(tools, "read_document", 1)],
        system_prompt=SCOUT_SYSTEM,
    )
    result = await asyncio.wait_for(
        scout.ainvoke({"messages": [{"role": "user", "content": SCOUT_TASK}]},
                      config={"recursion_limit": 8}),
        timeout=SCOUT_TIMEOUT,
    )
    messages = result.get("messages") if isinstance(result, dict) else None
    if not messages:
        return ""
    return str(getattr(messages[-1], "content", "")).strip()[:1200]


async def gather_company(agent: FisherAgent, tools: list[Any]) -> dict[str, Any]:
    """Steps 2-4: use the tools, then compute every figure in Python."""
    sql_query = find_tool(tools, "sql_query", 2)
    rows_context = parse_table(await call_tool(sql_query, {"query": SQL_CONTEXT}))
    rows_revenue = parse_table(await call_tool(sql_query, {"query": SQL_REVENUE}))
    rows_costs = parse_table(await call_tool(sql_query, {"query": SQL_COSTS}))
    evidence = build_evidence(rows_context, rows_revenue, rows_costs)

    if getattr(agent, "model", None) is not None:
        try:
            evidence["narrative"] = await scout_claims(agent, tools)
        except Exception as exc:
            print(f"[scout] failed ({exc}); reading the annual letter directly", file=sys.stderr)
    if not evidence["narrative"]:
        try:
            text = await call_tool(find_tool(tools, "read_document", 1), {"name": ANNUAL_LETTER})
            if not text.startswith("No document named"):
                evidence["narrative"] = text[:2200]
        except Exception:
            pass
    return evidence


async def analyse_one(agent: FisherAgent, name: str, tools: list[Any]) -> dict[str, Any]:
    """Run the pipeline for one company and never raise."""
    evidence: dict[str, Any] = {"company": name, "industry": "unknown"}
    try:
        evidence = await gather_company(agent, tools)
    except Exception as exc:
        print(f"[{name}] tool failure: {exc}", file=sys.stderr)

    verified = apply_rules(evidence)                      # step 6, from the figures
    verdicts = {point: item["verdict"] for point, item in verified.items()}
    summary = template_summary(evidence, verified)

    if getattr(agent, "analyst", None) is not None:
        try:
            reply = await agent.analyst.ainvoke({"prompt": build_prompt(evidence)})
            payload = extract_json(str(getattr(reply, "content", reply)))
            judged = read_verdicts(payload)
            covered = sum(value is not None for value in judged.values())
            agree = sum(judged[point] == verdicts[point] for point in CHOSEN_POINTS
                        if judged.get(point) is not None)
            AGREEMENT["covered"] += covered
            AGREEMENT["agree"] += agree
            print(f"[{name}] adjudicator agreed {agree}/{covered} with the verifier", file=sys.stderr)

            if covered == len(CHOSEN_POINTS) and agree == covered:
                text = str(payload.get("summary") or payload.get("claims_check") or "").strip()
                if len(text) > 40:
                    summary = text
            elif covered:
                flags = ", ".join(str(point) for point in CHOSEN_POINTS
                                  if judged.get(point) != verdicts[point])
                print(f"[{name}] verifier overrode the adjudicator on: {flags}", file=sys.stderr)
        except Exception as exc:
            print(f"[{name}] adjudicator failed ({exc}); keeping the verified figures", file=sys.stderr)

    return {"points": verdicts, "summary": summary}


async def analyze_companies(agent: Any, companies: dict[str, list[Any]]) -> dict[str, dict[str, Any]]:
    """Run the agent on every company, in parallel, without raising."""
    ### YOUR CODE HERE
    semaphore = asyncio.Semaphore(MAX_PARALLEL)
    results: dict[str, dict[str, Any]] = {}

    async def worker(name: str, tools: list[Any]) -> None:
        async with semaphore:
            try:
                results[name] = await asyncio.wait_for(
                    analyse_one(agent, name, tools), timeout=COMPANY_TIMEOUT
                )
            except Exception as exc:  # timeout or anything unexpected
                print(f"[{name}] giving up ({exc})", file=sys.stderr)
                results[name] = {"points": {}, "summary": f"Analysis failed: {exc}"}
            print(f"[{name}] done ({len(results)}/{len(companies)})", file=sys.stderr)

    await asyncio.gather(*(worker(name, tools) for name, tools in companies.items()))
    if AGREEMENT["covered"]:
        print(f"[agent] adjudicator vs verifier: {AGREEMENT['agree']}/{AGREEMENT['covered']} "
              "verdicts agreed", file=sys.stderr)
    return results


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