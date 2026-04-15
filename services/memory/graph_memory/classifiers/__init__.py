"""SPARTA classifiers absorbed from /sparta-intent.

Provides AmbiguityPredictor and IntentPredictor for query classification.
These are trained DistilBERT models with pure inference — no retrieval.
"""

from .predictor import AmbiguityPredictor, IntentPredictor

__all__ = ["AmbiguityPredictor", "IntentPredictor"]
