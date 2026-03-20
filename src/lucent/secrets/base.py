"""Abstract interface for secret storage providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class SecretScope:
    """Ownership scope for a secret.

    organization_id is always required. Exactly one of owner_user_id or
    owner_group_id should be set to associate the secret with an owner.
    """

    organization_id: str
    owner_user_id: str | None = None
    owner_group_id: str | None = None


class SecretProvider(ABC):
    """Abstract base class for secret storage backends.

    Implementations must never log or include secret values in error messages.
    """

    @abstractmethod
    async def get(self, key: str, scope: SecretScope) -> str | None:
        """Retrieve a secret value by key and scope.

        Returns None if the secret does not exist.
        """
        ...

    @abstractmethod
    async def set(self, key: str, value: str, scope: SecretScope) -> None:
        """Store or update a secret value."""
        ...

    @abstractmethod
    async def delete(self, key: str, scope: SecretScope) -> bool:
        """Delete a secret. Returns True if it existed."""
        ...

    @abstractmethod
    async def list_keys(self, scope: SecretScope) -> list[str]:
        """List all secret key names visible to the given scope."""
        ...
