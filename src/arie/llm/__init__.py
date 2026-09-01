"""Everything in ARIE that talks to a language model.

Two layers with different scopes, in one package because they share one HTTP
shape and one vendor account:

**The general layer (M7).** ``provider`` defines what "call a model" means;
``deepseek_provider`` and ``fake_provider`` implement it; ``factory`` chooses
between them; ``structured`` fences untrusted business data on the way in and
validates against a Pydantic model on the way out; ``budget`` decides whether
an organization may spend the call; ``service`` is the single seam every M7
feature goes through — budget, call, validate, ledger, in that order.

**The narrow layer (M1 Step 10).** ``deepseek``, ``schema``, ``baseline`` and
``eval`` are one task — extracting buying-intent signals from free text —
wired to a fixed prompt and a closed schema. That module's docstring commits
to never being a general facility, and it still isn't.

It keeps its own ``httpx`` client rather than being refactored onto
``provider``. The duplication is real and is named here rather than hidden:
two files know DeepSeek's request body. It is the cheaper mistake. That module
draws its billable/non-billable line differently — a response whose ``choices``
structure is unreadable is still *billable* there, because DeepSeek charged for
it, whereas a provider raises and loses the token counts — and its frozen
benchmark comparison (``bench/llm_signal_eval.py``) depends on that accounting.
M7's architecture rule is that it adds a layer on top of M1-M6 rather than
rewriting them, and silently changing what an M1 eval counts as billed is
exactly the kind of rewrite that rule exists to prevent.

The invariant both layers share, and the reason the general one is shaped the
way it is: **a model may interpret, extract and explain; it may not establish
facts or take actions.** Provider selection, paid enrichment, budgets,
entitlements, scoring, lead state and every consequential decision stay in
deterministic ARIE code. Nothing here returns anything but text and validated
values.
"""
