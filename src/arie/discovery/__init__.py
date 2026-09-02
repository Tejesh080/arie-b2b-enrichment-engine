"""Discovery Pivot: "tell me what you sell and I will find the opportunities
worth your attention" — the front half ARIE was missing.

Everything downstream of candidate promotion (scoring, confidence, evidence,
research materiality, recommendations, feedback) is the existing M1-M7 engine,
untouched. This package owns only what is new: turning a targeting profile
into search intent, running that intent through a discovery provider,
deduplicating and cheaply screening the results, and promoting the survivors
into the canonical lead pipeline that already exists.
"""

from __future__ import annotations
