"""Gold-set evaluation harness (pipeline v3 §1.7): offline Source+build over tests/gold/."""
from .run import EvalReport, FIELDS, Mismatch, load_gold, run_gold

__all__ = ["EvalReport", "FIELDS", "Mismatch", "load_gold", "run_gold"]
