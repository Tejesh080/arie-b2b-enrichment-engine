"""The M7 intelligence layer — business intent in, deterministic configuration out.

`arie.llm` is the machinery for talking to a model. This package is what ARIE
actually asks a model *for*, and — more importantly — what it does not let a
model decide.

The split every module here observes: **the model interprets, ARIE computes.**
A customer's free text about what they sell and who they want is something only
a language model can usefully read. What that interpretation is worth in points,
which thresholds it implies, and whether the result is a legal scoring
configuration are arithmetic and validation, and they happen in pure functions a
model output cannot reach past. ``arie.intelligence.normalization`` is where that
line is drawn: a model expresses relative importance, and a deterministic
normaliser turns it into the exactly-100.0 ceiling allocation
``arie.icp_profiles.validate_config`` demands. A model that tried to award itself
a hundred points per field has nowhere to put the number.
"""
