# morning-five-gates

Quality gates for an LLM-agent pipeline. **Treat model output as untrusted input.**

These three scripts come from *The Morning Five*, a multi-tenant job-matching service in which an LLM
agent sweeps job boards, scores roles against a per-client rubric and writes a daily ranked brief.
An LLM will happily surface a posting that closed last week, show the same role three mornings
running, or grow a shortlist into a 38-item inventory nobody reads. None of that raises an
exception, so each failure needed its own gate. Pure Python standard library, no dependencies.

| Gate | Question it answers | Fails (exit 1) when |
|---|---|---|
| `check_links.py` | Is the posting still alive? | an apply link returns 404/410. Bot-walled boards (403/999) are reported as *unverifiable*, never as dead |
| `dedupe_audit.py` | Did we already show this role? | the newest brief re-surfaces a role inside the dedupe window. Also **fails loudly if the brief format drifts** (see below) |
| `brief_audit.py` | Is this still a shortlist? | Tier A/B caps are exceeded, a carried role is stale or un-rescored, a role was tiered without its job description, or the client has stopped applying |

Each exits non-zero so a pipeline step can block delivery on it. `_runs.py` is the shared brief
resolver: every gate reads briefs through it, so they can never disagree about where briefs live.

## The bug that shaped the design

The brief layout changed mid-July 2026. The dedupe audit's regex silently matched nothing and reported
"clean, exit 0" on every brief for about six weeks while duplicates piled up. A zero-match parse and a
genuinely clean brief look identical. The fix is in `dedupe_audit.load_roles`: compare *role-shaped
headings found* against *roles parsed*, and report **PARSER BLIND** when they diverge. A gate that
cannot see its input must say so rather than pass. Every gate that reads files should do this.

## Try it (no setup)

```
cd clients
python dedupe_audit.py demo-client --window 14      # exit 1: 3 within-window duplicates
python brief_audit.py  demo-client --max-a 1         # exit 1: Tier A cap exceeded
python check_links.py  demo-client                   # exit 1: needs network; example.com links 404
cd .. && python -m unittest discover tests           # 7 tests
```

`clients/demo-client/` holds three synthetic briefs (invented roles, no client data) that trip each gate.

```
=== Dedupe audit: demo-client ===
Briefs scanned: 3 (2026-10-01 → 2026-10-03)  |  window: 14 days
🔴 WITHIN-WINDOW DUPLICATES (3) — same role re-surfaced within 14d, should have been deduped:
  • Acme Corp — "Data Analyst"  (surfaced 3×)   ← LATEST BRIEF
[gate] 3 within-window duplicate(s) in the latest brief (2026-10-03) — exit 1.
```

## Scope

Extracted from a working system; the orchestration (prompts, per-client rubrics) is not included.
Built with Claude Code, then hardened by hand against the failures above.

Author: Nishanth Salomon, <https://www.linkedin.com/in/nishanth-salomon-ae>
