"""Sandbox access rules used by exec_sandbox_command.

The tool must not hand one user's sandbox to another user in the same
organization. These are the pure rules behind that check.
"""

from uuid import uuid4

from lucent.tools.requests import _can_access_sandbox

OWNER = uuid4()
OTHER = uuid4()


def _sandbox(created_by):
    return {"id": str(uuid4()), "created_by": str(created_by) if created_by else None}


def test_owner_may_access_their_sandbox():
    assert _can_access_sandbox(
        _sandbox(OWNER), user_id=OWNER, role="member", memory_scope=None
    )


def test_another_user_in_the_same_org_may_not():
    assert not _can_access_sandbox(
        _sandbox(OWNER), user_id=OTHER, role="member", memory_scope=None
    )


def test_a_scoped_task_session_may_not_reach_another_users_sandbox():
    """A task's key is user-scoped; it must not widen sandbox access."""
    assert not _can_access_sandbox(
        _sandbox(OWNER), user_id=OTHER, role="daemon", memory_scope="user"
    )


def test_admin_keeps_org_wide_access():
    assert _can_access_sandbox(
        _sandbox(OWNER), user_id=OTHER, role="admin", memory_scope=None
    )


def test_an_ownerless_sandbox_is_refused_for_a_scoped_session():
    """Refuse rather than assume safe."""
    assert not _can_access_sandbox(
        _sandbox(None), user_id=OWNER, role="daemon", memory_scope="user"
    )


def test_a_missing_sandbox_is_refused():
    assert not _can_access_sandbox(
        None, user_id=OWNER, role="member", memory_scope=None
    )
