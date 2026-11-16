"""Declarative base for ORM models.

No models exist in M0. Profiles, accounts, audit entries and jobs arrive in M1.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all Heraldesk ORM models."""
