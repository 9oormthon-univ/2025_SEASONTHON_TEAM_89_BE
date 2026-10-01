"""Optional, high-entropy test credentials issue normal access tokens, not admin tokens."""
import hashlib
import json
import os
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import HTTPException
from sqlalchemy import text

from app import settings
from app.models.user import User
from app.services.jwt_service import jwt_service
from app.services.test_account_boundary import TEST_ACCOUNT_PREFIX

_SLOT = re.compile(r"[A-Za-z0-9_-]{1,24}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_CODE = re.compile(r"[A-Za-z0-9_-]{22,128}\Z")
_NAMESPACE = uuid5(NAMESPACE_URL, "https://wiheome.ajb.kr/test-account")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def configured_code_hashes():
    raw = os.environ.get("TEST_ACCOUNT_CODES_JSON", "")
    if not raw or len(raw) > 2048:
        return None
    try:
        config = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(config, dict) or not 1 <= len(config) <= 8:
            return None
        if any(not _SLOT.fullmatch(slot) or not isinstance(digest, str) or not _HASH.fullmatch(digest)
               for slot, digest in config.items()):
            return None
        if len(set(config.values())) != len(config):
            return None
        return config
    except (ValueError, TypeError):
        return None


class AttemptLimiter:
    """Fixed-size, process-shared limiter: all failures AND successes consume a permit.

    Reverse proxies must add a shared limit for multi-worker/multi-host deployments.
    X-Forwarded-For is intentionally not accepted as an authoritative client identity.
    """
    def __init__(self, window=300, per_client=10, global_limit=60, max_clients=256, clock=time.monotonic):
        self.window, self.per_client, self.global_limit, self.max_clients = window, per_client, global_limit, max_clients
        self.clock = clock
        self.lock = threading.Lock()
        self.total = deque()
        self.clients = {}

    def check(self, peer: str):
        key = hashlib.sha256(peer.encode("utf-8")).digest()
        with self.lock:
            cutoff = self.clock() - self.window
            while self.total and self.total[0] <= cutoff:
                self.total.popleft()
            for candidate in list(self.clients):
                entries = self.clients[candidate]
                while entries and entries[0] <= cutoff:
                    entries.popleft()
                if not entries:
                    del self.clients[candidate]
            entries = self.clients.get(key)
            if (len(self.total) >= self.global_limit or (entries is not None and len(entries) >= self.per_client)
                    or (entries is None and len(self.clients) >= self.max_clients)):
                raise HTTPException(429, "잠시 후 다시 시도해 주세요.", headers={"Retry-After": str(self.window)})
            now = self.clock()
            self.total.append(now)
            self.clients.setdefault(key, deque()).append(now)


attempt_limiter = AttemptLimiter()


async def read_credentials(request):
    """Bounded request parser with sanitized 422s; FastAPI/Pydantic cannot echo credentials."""
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 2048:
            raise HTTPException(422, "테스트 로그인 입력 형식이 올바르지 않습니다.")
        body.extend(chunk)
    try:
        payload = json.loads(body, object_pairs_hook=_unique_object)
        if not isinstance(payload, dict) or set(payload) - {"code", "device_token"}:
            raise ValueError()
        code, token = payload.get("code"), payload.get("device_token")
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            raise ValueError()
        if token is not None and (not isinstance(token, str) or not 1 <= len(token) <= 255
                                  or any(not 33 <= ord(char) <= 126 for char in token)):
            raise ValueError()
        return code, token
    except (ValueError, TypeError, UnicodeDecodeError):
        raise HTTPException(422, "테스트 로그인 입력 형식이 올바르지 않습니다.") from None


def match_slot(code, config):
    digest = hashlib.sha256(code.encode("ascii")).hexdigest()
    selected = None
    # Check all configured hashes; never log/store/return the submitted credential.
    for slot, expected in sorted(config.items()):
        if secrets.compare_digest(digest, expected):
            selected = slot
    if selected is None:
        raise HTTPException(401, "테스트 계정 코드를 확인해 주세요.")
    return selected


def _seed_family(db, user, slot):
    """Called only in the new-user transaction. Re-login never restores a left/joined group."""
    fixture_marker = TEST_ACCOUNT_PREFIX + "fixture:" + slot
    fixture_id = str(uuid5(_NAMESPACE, fixture_marker))
    if db.query(User).filter(User.user_id == fixture_id).first() is not None:
        # Re-created primary after withdrawal does not hijack an existing fixture's later group.
        return
    fixture = User(user_id=fixture_id, kakao_id=fixture_marker, nickname="테스트 가족 구성원",
                   profile_image=None, group_id=None, warning_count=2, danger_count=1,
                   device_token=None, is_active=True)
    db.add(fixture)
    db.flush()
    for _ in range(10):
        gid = ''.join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(6))
        code = ''.join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(10))
        if db.execute(text("SELECT 1 FROM family_groups WHERE id = :gid OR join_code = :code"), {"gid": gid, "code": code}).first() is None:
            break
    else:
        raise ValueError("demo group allocation failed")
    now = datetime.utcnow()
    db.execute(text("INSERT INTO family_groups (id, group_name, creator_id, join_code, created_at) "
                    "VALUES (:gid, :name, :uid, :code, :now)"),
               {"gid": gid, "name": "위허메 테스트 가족", "uid": user.user_id, "code": code, "now": now})
    for member, creator in ((user, True), (fixture, False)):
        member.group_id = gid
        db.execute(text("INSERT INTO group_members (group_id, user_id, nickname, is_creator, joined_at) "
                        "VALUES (:gid, :uid, :name, :creator, :now)"),
                   {"gid": gid, "uid": member.user_id, "name": member.nickname, "creator": creator, "now": now})
    db.flush()
    # Existing deployments may or may not count INSERTs with triggers. Assign the actual
    # membership count after insertion, never increment it again (which would double-count).
    db.execute(text("UPDATE family_groups SET current_members = "
                    "(SELECT COUNT(*) FROM group_members WHERE group_id = :gid) WHERE id = :gid"),
               {"gid": gid})
    # Existing setting services default missing relations to enabled; no synthetic token can receive push.


def login_test_account(db, slot: str, device_token=None):
    marker = TEST_ACCOUNT_PREFIX + slot
    user = db.query(User).filter(User.kakao_id == marker).first()
    is_new = user is None
    if user is None:
        # Re-login keeps the stored UID; re-creation after withdrawal must NOT revive an old JWT.
        user = User(user_id=str(uuid4()), kakao_id=marker, nickname="테스트 사용자 " + slot,
                    warning_count=0, danger_count=0, is_active=True)
        db.add(user)
        db.flush()
        _seed_family(db, user, slot)
    elif not user.is_active:
        # Deliberate operator deactivation must not be undone by a valid code.
        raise HTTPException(401, "테스트 계정을 사용할 수 없습니다.")
    user.last_login_at = datetime.utcnow()
    if device_token is not None:
        user.device_token = device_token
    db.commit()
    db.refresh(user)
    token = jwt_service.create_access_token({"user_id": user.user_id, "auth_provider": "test", "nickname": user.nickname})
    return {"access_token": token, "token_type": "bearer", "auth_provider": "test",
            "expires_in": settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60, "is_new_user": is_new,
            "user": {"user_id": user.user_id, "nickname": user.nickname, "group_id": user.group_id,
                     "profile_image": user.profile_image, "warning_count": user.warning_count,
                     "danger_count": user.danger_count}}
