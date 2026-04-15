from __future__ import annotations
import os
import time, uuid, random
from pathlib import Path
from typing import List
import typer
from ..arango_client import get_db

app = typer.Typer(add_completion=False)

TOPICS = [
    ("CDP discovery", ["cdp", "devtools", "puppeteer", "playwright"], "tabbed"),
    ("Vite proxy alignment", ["proxy", "vite", "backend", "api"], "tabbed"),
    ("pdf.js render cancelation", ["pdfjs", "render", "abort", "canvas"], "tabbed"),
    ("Thumbnails rail/filmstrip", ["thumbnails", "rail", "filmstrip", "cache"], "tabbed"),
    ("Resizable panes + ARIA", ["aria", "a11y", "resizer", "keyboard"], "tabbed"),
    ("Export dropdown rules", ["export", "json", "pdf", "zip"], "tabbed"),
    ("Exact JSON toggle", ["json", "strict", "canonical", "toggle"], "tabbed"),
    ("Pager placement", ["pager", "toolbar", "zoom"], "tabbed"),
    ("Lessons infra", ["codex", "uv", "docker", "arango", "redis"], "infra"),
    ("Gemini JSON reliability", ["gemini", "litellm", "json", "schema"], "pipeline"),
]

SUFFIXES = ["gotchas and fixes", "playbook", "stability guide", "troubleshooting", "pitfalls", "design notes"]

def build_keywords(tags: List[str], scope: str) -> str:
    syn = {"cdp":["chrome","chromium","devtools","browserless","puppeteer","playwright"],"proxy":["vite","backend","target","api","port","8000","8001"],"json":["response_format","schema","structured","wrap_json"],"smokes":["smoke","ci","tests","playwright","puppeteer"]}
    bag:list[str]=[]
    for t in tags or []: bag.append(t); bag.extend(syn.get(t.lower(),[]))
    if scope: bag.append(scope)
    seen=set(); out:list[str]=[]
    for w in bag:
        if w and w not in seen: seen.add(w); out.append(w)
    return ' '.join(out)

@app.command()
def seed(count:int=typer.Option(50), scope:str=typer.Option('', help='optional scope'), batch:str=typer.Option('', help='demo batch id')):
    db=get_db()
    col=db.collection('lessons')
    ts=int(time.time())
    batch_id=batch or uuid.uuid4().hex[:12]
    # Ensure at least one deterministic topic useful for smokes/tests when seeding into the default/tabbed scope
    # Deterministic injections for CI stability:
    # - Ensure at least three CDP lessons so queries like "cdp puppeteer" return k>=3
    # - Also include one Thumbnails lesson early to stabilize related smokes
    inject_cdp_first_n = 3 if (scope == '' or scope == 'tabbed') else 0
    injected_thumbnails = False
    for i in range(count):
        base, base_tags, base_scope = random.choice(TOPICS)
        sc = scope or base_scope
        if sc == "tabbed":
            if inject_cdp_first_n > 0:
                base, base_tags, base_scope = (
                    "CDP discovery",
                    ["cdp", "devtools", "puppeteer", "playwright"],
                    "tabbed",
                )
                inject_cdp_first_n -= 1
            elif not injected_thumbnails:
                base, base_tags, base_scope = (
                    "Thumbnails rail/filmstrip",
                    ["thumbnails", "rail", "filmstrip", "cache"],
                    "tabbed",
                )
                injected_thumbnails = True
        title=f'DEMO[{batch_id}] {base} #{i+1} {random.choice(SUFFIXES)}'
        problem=f'Exploration of {base} within the project: common pitfalls and how to avoid them.'
        playbook='- Identify root cause\n- Apply stable settings and add smokes\n- Document rationale and add graph edges'
        # Keep all base tags for deterministic keyword coverage in smokes/CI
        tags = list(dict.fromkeys(base_tags))
        keywords=build_keywords(tags, sc)
        doc={ 'title':title,'problem':problem,'playbook':playbook,'tags':tags,'keywords':keywords,'scope':sc,'status':'active','added_by':'agent','updated_at':ts,'demo':True,'demo_batch':batch_id }
        from .store import store_lesson
        store_lesson(db, doc)
    print(f'Seeded {count} demo lessons (batch={batch_id}).')

    code_path = os.getenv("GM_SEED_CODE_PATH", "")
    if code_path:
        code_scope = scope or os.getenv("GM_SEED_CODE_SCOPE", "code")
        try:
            from .symbols import TreeSitterUnavailable, collect_symbols
            collect_symbols(scope=code_scope, repo_path=Path(code_path).expanduser(), persist=True)
            print(f"Tree-Sitter enrichment queued for {code_scope}")
        except TreeSitterUnavailable as exc:
            print(f"Tree-Sitter unavailable: {exc}")
        except FileNotFoundError as exc:
            print(f"Seed enrichment skipped (repo missing): {exc}")

        if os.getenv("GM_USE_LSP", "0").lower() in {"1", "true", "yes"}:
            try:
                from .lsp_worker import enrich_symbols
                enrich_symbols(scope=code_scope, repo_root=code_path, languages=None, persist=True)
                print("LSP enrichment queued")
            except Exception as exc:
                print(f"LSP enrichment failed: {exc}")
