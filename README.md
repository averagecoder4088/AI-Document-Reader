# Research-brief agent (Super Investing, AI Innovator assignment, Part B)

Input: an NSE ticker plus a folder of scraped documents. Output: a one-page Markdown research brief
(Snapshot, Bull case, Bear case, Open questions, Sources) for a retail investor.

Test case: `SRVCABLE` (Sarvottam Cables Ltd, fictional), `research_pack/` (8 documents), today = 23 September 2026.

## How to run

```bash
pip install -r requirements.txt
cp .env.example .env            # then put your key in .env:  GROQ_API_KEY=...
python run_agent.py --list-models                       # which models your key can use
python run_pipeline_v3.py --label final --coverage --model openai/gpt-oss-120b
```

Output goes to `runs/<label>_<timestamp>.md`; a matching `.extract.json` holds every decision the agent made
(which sources were included and why, extracted claims, Python checks, verifier rounds). `final_brief_run6.md` is
the brief submitted in the form.

## Model(s) and why

**`openai/gpt-oss-120b` on Groq (free tier)**, for all submitted runs.

- I started on Gemini Flash. Gemini model names kept being retired under me (`gemini-2.5-flash` returned
  "no longer available to new users"), which broke runs. So all provider code lives in one file, `llm.py`, and I moved
  to Groq. `gpt-oss-120b` was the largest general-purpose chat model available on my key.
- Cost of this choice: it is a *reasoning* model. It spends tokens thinking, and on JSON tasks that sometimes left no
  tokens for the answer (an empty reply). I cap output tokens and use low reasoning effort for JSON steps.
- Groq's JSON mode guarantees valid JSON, not the right keys (the model dropped whole sections). Every JSON step is
  validated with pydantic and retried with the error fed back.
- Temperature 0.0 for extraction, 0.2 for writing. Every run file records the exact model and prompt used.
- The model never silently changes: no fallback chain, so every run is tied to exactly one model.

## How it works

1. **Triage (LLM):** per document: company match, source tier, publication date, injection flag.
2. **Rules (Python):** the *final* include/exclude decision is made by code (wrong company, tier 4, older than 12 months,
   or a prompt-injection attempt detected by a regex scan of the raw text *or* by the model).
3. **Extraction (LLM, two small calls):** claims and numeric metrics, from included documents only. A recall check compares
   every number in the sources with the extracted claims and asks for any missing ones.
4. **Checks (Python):** conflicts between sources; stated growth vs recomputed growth *allowing for rounding*; which
   revenue figure reconciles with the stated EBITDA margin; derived figures (order-book growth, GST vs quarterly profit,
   receivable days, pledge and institutional-holding changes).
5. **Quarantine (Python):** claims that repeat a figure contradicted by a higher-tier source are dropped.
6. **Write (LLM):** the writer never sees raw documents. It gets sources, claims, check results and a list of
   REQUIRED FINDINGS that code built from the checks.
7. **Verify (Python, no LLM):** format and length, every line cited with an included `[S#]` source, every number present
   in the cited source, no advice language, no invented periods, no internal-tool wording, conflicts and manipulation
   attempts reported, key findings in the right section. On failure the exact problem list goes back to the writer
   (up to 3 rewrites, all earlier problems kept in the prompt).

## Files

| File | Purpose |
|---|---|
| `system_prompt.md` | The system prompt (current, v5). `system_prompt_v1.md` ... `system_prompt_v4.md` are earlier versions used in earlier runs |
| `run_pipeline_v3.py` | The final agent (steps 1-7) |
| `run_agent.py` | Run 1 design (single call) and shared helpers |
| `run_pipeline.py` | Run 2 design (extract, check, write) and shared JSON helpers |
| `llm.py` | The only file that talks to the model provider |
| `test_log.md` | Six runs: what went wrong, what changed |
| `final_brief_run6.md` | The submitted brief, exactly as generated (metadata comment line removed, no other edits) |
| `runs/` | Raw outputs and decision logs for the runs |

## Reproducing the test-log runs

Runs 1 and 2 reproduce exactly:
`python run_agent.py --prompt system_prompt_v1.md --label run1` and
`python run_pipeline.py --prompt system_prompt_v1.md --label run2`.
Runs 3-5 were produced by earlier states of `run_pipeline_v3.py` (I extended one file between runs); their prompts are
saved as `system_prompt_v2.md` to `system_prompt_v4.md` and their outputs are in `runs/`. Run 6 is the current file.
Each run is a single sample, so differences between runs show design effects, not statistics.

## Limitations (honest)

- The verifier checks numbers, citations, format and coverage of key findings. It cannot check *framing*: in the submitted
  brief the writer placed the copper price rise in the Bull case, which is a bear-leaning fact.
- Metric keys are fixed to quarterly-results-style reports (revenue, EBITDA, order book, pledge...). Other document types
  would need new keys.
- "Stale" means older than 12 months, which is crude.
- The writer can still make small slips that pass the checks (see "Known issues" below).
- Needs a model that follows instructions; a small local model would likely fail the JSON steps.

## Known issues in the submitted brief

- The copper price rise (~14%) is listed under Bull case; it belongs under Bear case.
- It cites the share price as "on 9 Aug 2026"; the article is dated 9 Aug, the price is from the Friday before.
- Dropped details: net debt/equity 0.35x, export growth (16% to 21%), and the "1-2 quarter lag" on price-variation clauses.
- One non-bracket citation, "(S1)", appears in a Bull-case bullet.
