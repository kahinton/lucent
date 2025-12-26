"""Database module for Hindsight."""

from hindsight.db.client import get_pool, init_db, close_db

__all__ = ["get_pool", "init_db", "close_db"]
