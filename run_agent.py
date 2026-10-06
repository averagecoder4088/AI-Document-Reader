"""
run_agent.py -- Research-brief agent (Run 1: single LLM call, Groq)

Usage:
    export GROQ_API_KEY="your-key"
    python run_agent.py --list-models      # see which models your key can use
    python run_agent.py --dry-run          # prints the assembled prompt, no API call
    python run_agent.py --label run1 --model <model-name>
"""
import argparse
from datetime import datetime
from pathlib import Path

from llm import DEFAULT_MODEL, call_llm, list_models


# ---------- 1. Load the documents ----------
def load_documents(pack_dir):
    """Read every .md file in the pack and give each a stable ID (S1, S2, ...)."""
    docs = []
    for i, path in enumerate(sorted(Path(pack_dir).glob("*.md")), start=1):
        docs.append((f"S{i}", path.name, path.read_text(encoding="utf-8")))
    return docs


# ---------- 2. Build the user message ----------
def build_user_message(ticker, today, docs):
    """Wrap each document in tags so the model can see where data starts and ends."""
    parts = [f"TICKER: {ticker}", f"TODAY: {today}", "", "DOCUMENTS:"]
    for doc_id, filename, text in docs:
        # Stop a document from closing its own tag and "escaping" into instruction space
        safe_text = text.replace("</document", "&lt;/document")
        parts.append(f'<document id="{doc_id}" filename="{filename}">')
        parts.append(safe_text.strip())
        parts.append("</document>")
        parts.append("")
    return "\n".join(parts)


# ---------- 3. Save the run (this becomes your test log evidence) ----------
def save_run(label, model, temperature, prompt_file, output):
    Path("runs").mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = Path("runs") / f"{label}_{stamp}.md"
    header = (
        f"<!-- label: {label} | model: {model} | temperature: {temperature} "
        f"| prompt: {prompt_file} | time: {stamp} -->\n\n"
    )
    path.write_text(header + output, encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", default="research_pack")
    ap.add_argument("--prompt", default="system_prompt.md")
    ap.add_argument("--ticker", default="SRVCABLE")
    ap.add_argument("--today", default="23 September 2026")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--label", default="run1")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list-models", action="store_true")
    args = ap.parse_args()

    if args.list_models:
        print("\n".join(list_models()))
        return

    system_prompt = Path(args.prompt).read_text(encoding="utf-8")
    docs = load_documents(args.pack)
    user_message = build_user_message(args.ticker, args.today, docs)

    if args.dry_run:
        print(f"Loaded {len(docs)} documents:")
        for doc_id, filename, _ in docs:
            print(f"  {doc_id}: {filename}")
        print("\n--- USER MESSAGE ---\n")
        print(user_message)
        return

    output = call_llm(system_prompt, user_message, args.model, args.temperature)
    path = save_run(args.label, args.model, args.temperature, args.prompt, output)
    print(output)
    print(f"\n[saved to {path}]")


if __name__ == "__main__":
    main()
