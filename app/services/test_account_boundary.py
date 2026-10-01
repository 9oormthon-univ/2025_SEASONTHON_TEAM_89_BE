"""Database-backed separation of ordinary Kakao users and synthetic test users.

No JWT claim or client-provided flag grants access across this boundary.
"""
from sqlalchemy import text

TEST_ACCOUNT_PREFIX = "test-account:"


def is_test_account(kakao_id: object) -> bool:
    return isinstance(kakao_id, str) and kakao_id.startswith(TEST_ACCOUNT_PREFIX)


def account_row(db, user_id: str):
    row = db.execute(text(
        "SELECT user_id, kakao_id, group_id, is_active FROM users WHERE user_id = :uid"
    ), {"uid": user_id}).fetchone()
    if row is None or not row.is_active or not row.kakao_id:
        raise ValueError("USER_NOT_FOUND")
    return row


def ensure_pair_domain(db, first_id: str, second_id: str) -> None:
    if is_test_account(account_row(db, first_id).kakao_id) != is_test_account(account_row(db, second_id).kakao_id):
        raise ValueError("GROUP_DOMAIN_MISMATCH")


def ensure_group_domain(db, actor_id: str, group_id: str) -> None:
    """Reject mixed/corrupt groups, including stale membership and users.group_id rows."""
    domain = is_test_account(account_row(db, actor_id).kakao_id)
    creator = db.execute(text(
        "SELECT u.kakao_id FROM family_groups fg LEFT JOIN users u ON u.user_id = fg.creator_id "
        "WHERE fg.id = :gid"
    ), {"gid": group_id}).fetchone()
    if creator is None or not creator.kakao_id or is_test_account(creator.kakao_id) != domain:
        raise ValueError("GROUP_DOMAIN_MISMATCH")
    members = db.execute(text(
        "SELECT u.kakao_id FROM group_members gm LEFT JOIN users u ON u.user_id = gm.user_id "
        "WHERE gm.group_id = :gid UNION ALL SELECT kakao_id FROM users WHERE group_id = :gid"
    ), {"gid": group_id}).fetchall()
    if any(not row.kakao_id or is_test_account(row.kakao_id) != domain for row in members):
        raise ValueError("GROUP_DOMAIN_MISMATCH")


def ensure_actor_group(db, actor_id: str):
    row = account_row(db, actor_id)
    groups = {membership.group_id for membership in db.execute(text(
        "SELECT group_id FROM group_members WHERE user_id = :uid"
    ), {"uid": actor_id}).fetchall()}
    if row.group_id:
        groups.add(row.group_id)
    for group_id in groups:
        # A null users.group_id must not hide an opposite-domain stale membership on leave/delete.
        ensure_group_domain(db, actor_id, group_id)
    return row.group_id


def recipient_token_allowed(db, recipient_id: str, token: str) -> bool:
    """A stale/shared token associated with the opposite domain is never sent a test alert."""
    if not token:
        return False
    domain = is_test_account(account_row(db, recipient_id).kakao_id)
    owners = db.execute(text("SELECT kakao_id FROM users WHERE device_token = :token"), {"token": token}).fetchall()
    return bool(owners) and all(row.kakao_id and is_test_account(row.kakao_id) == domain for row in owners)


def isolated_recipients(db, sender_id: str, recipients):
    ensure_actor_group(db, sender_id)
    result = []
    for recipient in recipients:
        ensure_pair_domain(db, sender_id, recipient.user_id)
        if recipient_token_allowed(db, recipient.user_id, recipient.device_token):
            result.append(recipient)
    return result
