"""Small, local-first email-to-quote agent for the WWI sample data."""

from .models import Intent, Outcome, RunRecord
from .pipeline import QuotePipeline

__all__ = ["Intent", "Outcome", "QuotePipeline", "RunRecord"]
