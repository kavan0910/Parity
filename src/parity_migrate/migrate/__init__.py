"""Phase 2: migrate & prove equivalence (docs/phase2-plan.md, module map in docs/phase2-architecture.md).

Keep this package __init__ import-free: the top-level CLI imports migrate.cli at start-up, and every
module here should only pull heavy dependencies inside the functions that need them.
"""
