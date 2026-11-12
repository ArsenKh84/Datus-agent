"""Server-side domain services (in-memory; swap for a database): accounts & credits, collaboration, community,
chat turns with approval, collaborative notes, uploaded input images.

Identity is a plain user id (the HTTP layer reads it from the ``X-User-Id`` header). Error codes mirror the
reference product where it exposes them (``invitation_invalid``, ``share_link_expired``, ``readme_conflict``,
``collaborator_limit_reached``, ``cannot_change_owner`` …).
"""

from __future__ import annotations

import base64
import hashlib
import math
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field

from hwbp.models import ProjectContent

ROLES = ("owner", "editor", "viewer")
PLANS = {"free": {"weekly_credits": 10}, "pro": {"weekly_credits": None}, "ultra": {"weekly_credits": None}}
COLLABORATOR_LIMIT = 10
INVITE_TTL_S = 7 * 24 * 3600
INVITE_COOLDOWN_S = 600
USERNAME_RE = re.compile(r"^[a-z0-9_]{3,20}$")
ADJ = ["swift", "brave", "quiet", "bright", "lucky", "clever", "calm", "bold", "keen", "spry"]
NOUN = ["falcon", "otter", "comet", "maple", "piston", "photon", "badger", "rotor", "quartz", "sparrow"]


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.status, self.code, self.message = status, code, message or code


def _now() -> float:
    return time.time()


# ------------------------------------------------------------------------------ accounts, plans, credits
@dataclass
class Profile:
    user_id: str
    username: str | None = None
    bio: str = ""
    profile_color: str = "#22c55e"
    plan: str = "free"
    onboarded: bool = False
    onboarding: dict = field(default_factory=dict)
    email_prefs: dict = field(
        default_factory=lambda: {"marketing": False, "productUpdates": True, "projectActivity": True}
    )
    credits_used: int = 0
    week: int = 0

    def public(self) -> dict:
        return {"userId": self.user_id, "username": self.username, "bio": self.bio, "profileColor": self.profile_color,
                "plan": self.plan, "onboarded": self.onboarded}  # fmt: skip


class Accounts:
    def __init__(self, default_plan_for: dict[str, str] | None = None) -> None:
        self.profiles: dict[str, Profile] = {}
        self.leads: list[dict] = []
        self._plans = default_plan_for or {}
        self._lock = threading.Lock()

    def get(self, user_id: str) -> Profile:
        with self._lock:
            if user_id not in self.profiles:
                self.profiles[user_id] = Profile(user_id, plan=self._plans.get(user_id, "free"))
            return self.profiles[user_id]

    def set_username(self, user_id: str, username: str) -> Profile:
        username = username.strip().lower()
        if not USERNAME_RE.match(username):
            raise ServiceError(400, "invalid_username", "3-20 characters: a-z, 0-9, _")
        if any(p.username == username and p.user_id != user_id for p in self.profiles.values()):
            raise ServiceError(409, "username_taken")
        p = self.get(user_id)
        p.username = username
        return p

    def auto_username(self, user_id: str) -> Profile:
        p = self.get(user_id)
        if p.username:
            return p
        h = int(hashlib.sha256(user_id.encode()).hexdigest(), 16)
        for k in range(1000):
            cand = f"{ADJ[(h + k) % len(ADJ)]}_{NOUN[(h // 7 + k) % len(NOUN)]}{(h + k) % 100}"
            if not any(q.username == cand for q in self.profiles.values()):
                p.username = cand
                return p
        raise ServiceError(500, "username_exhausted")  # pragma: no cover

    def set_bio(self, user_id: str, bio: str) -> Profile:
        if len(bio) > 280:
            raise ServiceError(400, "bio_too_long", "max 280 characters")
        p = self.get(user_id)
        p.bio = bio
        return p

    def set_color(self, user_id: str, color: str) -> Profile:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color or ""):
            raise ServiceError(400, "invalid_color", "expected #RRGGBB")
        p = self.get(user_id)
        p.profile_color = color.lower()
        return p

    def complete_onboarding(self, user_id: str, answers: dict) -> Profile:
        p = self.get(user_id)
        p.onboarded, p.onboarding = True, dict(answers or {})
        return p

    def set_email_prefs(self, user_id: str, prefs: dict) -> dict:
        p = self.get(user_id)
        for k in p.email_prefs:
            if k in prefs:
                p.email_prefs[k] = bool(prefs[k])
        return p.email_prefs

    # ---- design credits (weekly, plan based)
    def credits(self, user_id: str, now: float | None = None) -> dict:
        p = self.get(user_id)
        week = int((now or _now()) // (7 * 86400))
        if week != p.week:
            p.week, p.credits_used = week, 0
        quota = PLANS[p.plan]["weekly_credits"]
        remaining = None if quota is None else max(0, quota - p.credits_used)
        return {"plan": p.plan, "weekly_credits_remaining": remaining, "unlimited": quota is None}

    def consume_credit(self, user_id: str) -> dict:
        c = self.credits(user_id)
        if not c["unlimited"]:
            if c["weekly_credits_remaining"] <= 0:
                raise ServiceError(402, "no_credits", "weekly design credits used up")
            self.get(user_id).credits_used += 1
        return self.credits(user_id)

    def set_plan(self, user_id: str, plan: str) -> Profile:
        if plan not in PLANS:
            raise ServiceError(400, "invalid_plan")
        p = self.get(user_id)
        p.plan = plan
        return p

    def add_lead(self, body: dict) -> dict:
        email = (body.get("email") or "").strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise ServiceError(400, "invalid_email")
        lead = {
            "id": uuid.uuid4().hex[:12],
            "email": email,
            "company": body.get("company"),
            "message": body.get("message", ""),
            "ts": _now(),
        }
        self.leads.append(lead)
        return lead


# ------------------------------------------------------------------------------ collaboration
@dataclass
class Invite:
    id: str
    project_id: str
    email: str
    role: str
    token: str
    expires_at: float
    created_at: float
    revoked: bool = False


@dataclass
class ShareLink:
    id: str
    project_id: str
    role: str
    token: str
    expires_at: float | None
    revoked: bool = False


class Collaboration:
    def __init__(self) -> None:
        self.members: dict[str, dict[str, str]] = {}  # project -> {user: role}
        self.invites: dict[str, Invite] = {}
        self.links: dict[str, ShareLink] = {}

    def set_owner(self, project_id: str, user_id: str) -> None:
        self.members.setdefault(project_id, {})[user_id] = "owner"

    def role(self, project_id: str, user_id: str) -> str | None:
        return self.members.get(project_id, {}).get(user_id)

    def require(self, project_id: str, user_id: str, at_least: str) -> str:
        r = self.role(project_id, user_id)
        if r is None or ROLES.index(r) > ROLES.index(at_least):
            raise ServiceError(403, "forbidden", f"requires {at_least} access")
        return r

    def list_members(self, project_id: str) -> list[dict]:
        return [
            {"userId": u, "role": r}
            for u, r in sorted(self.members.get(project_id, {}).items(), key=lambda t: (ROLES.index(t[1]), t[0]))
        ]

    def change_role(self, project_id: str, actor: str, user_id: str, role: str) -> None:
        self.require(project_id, actor, "owner")
        if role not in ("editor", "viewer"):
            raise ServiceError(400, "invalid_role")
        if self.role(project_id, user_id) == "owner":
            raise ServiceError(400, "cannot_change_owner", "The owner's role can't be changed")
        if self.role(project_id, user_id) is None:
            raise ServiceError(404, "member_not_found")
        self.members[project_id][user_id] = role

    def remove_member(self, project_id: str, actor: str, user_id: str) -> None:
        if actor != user_id:
            self.require(project_id, actor, "owner")
        if self.role(project_id, user_id) == "owner":
            raise ServiceError(400, "cannot_change_owner")
        if self.members.get(project_id, {}).pop(user_id, None) is None:
            raise ServiceError(404, "member_not_found")

    def _add_member(self, project_id: str, user_id: str, role: str) -> str:
        cur = self.role(project_id, user_id)
        if cur is not None:  # never downgrade an existing member through a link
            return cur if ROLES.index(cur) <= ROLES.index(role) else role
        if len(self.members.get(project_id, {})) >= COLLABORATOR_LIMIT:
            raise ServiceError(409, "collaborator_limit_reached", "This project has reached its collaborator limit.")
        self.members.setdefault(project_id, {})[user_id] = role
        return role

    def invite(self, project_id: str, actor: str, email: str, role: str, now: float | None = None) -> Invite:
        self.require(project_id, actor, "owner")
        now = now or _now()
        if role not in ("editor", "viewer"):
            raise ServiceError(400, "invalid_role")
        for inv in self.invites.values():
            if (
                inv.project_id == project_id
                and inv.email == email.lower()
                and not inv.revoked
                and now - inv.created_at < INVITE_COOLDOWN_S
            ):
                raise ServiceError(
                    429, "invitation_cooldown", "That email was invited recently. Try again in about 10 minutes."
                )
        inv = Invite(
            uuid.uuid4().hex[:12], project_id, email.lower(), role, secrets.token_urlsafe(24), now + INVITE_TTL_S, now
        )
        self.invites[inv.id] = inv
        return inv

    def revoke_invite(self, project_id: str, actor: str, invite_id: str) -> None:
        self.require(project_id, actor, "owner")
        inv = self.invites.get(invite_id)
        if inv is None or inv.project_id != project_id:
            raise ServiceError(404, "invitation_invalid")
        inv.revoked = True

    def accept_invite(self, user_id: str, token: str, now: float | None = None) -> dict:
        inv = next((i for i in self.invites.values() if i.token == token), None)
        if inv is None or inv.revoked:
            raise ServiceError(404, "invitation_invalid", "This invitation is invalid or has been revoked.")
        if (now or _now()) > inv.expires_at:
            raise ServiceError(
                410, "invitation_expired", "This invitation has expired. Ask the project owner for a new one."
            )
        role = self._add_member(inv.project_id, user_id, inv.role)
        inv.revoked = True  # single use
        return {"projectId": inv.project_id, "role": role}

    def create_link(self, project_id: str, actor: str, role: str, expires_in_h: float | None = None) -> ShareLink:
        self.require(project_id, actor, "owner")
        if role not in ("editor", "viewer"):
            raise ServiceError(400, "invalid_role")
        link = ShareLink(
            uuid.uuid4().hex[:12],
            project_id,
            role,
            secrets.token_urlsafe(24),
            _now() + expires_in_h * 3600 if expires_in_h else None,
        )
        self.links[link.id] = link
        return link

    def revoke_link(self, project_id: str, actor: str, link_id: str) -> None:
        self.require(project_id, actor, "owner")
        link = self.links.get(link_id)
        if link is None or link.project_id != project_id:
            raise ServiceError(404, "share_link_invalid")
        link.revoked = True

    def redeem_link(self, user_id: str, token: str, now: float | None = None) -> dict:
        link = next((s for s in self.links.values() if s.token == token), None)
        if link is None or link.revoked:
            raise ServiceError(404, "share_link_invalid", "This share link is invalid or has been revoked.")
        if link.expires_at is not None and (now or _now()) > link.expires_at:
            raise ServiceError(410, "share_link_expired", "This share link has expired.")
        return {"projectId": link.project_id, "role": self._add_member(link.project_id, user_id, link.role)}

    def projects_of(self, user_id: str) -> list[tuple[str, str]]:
        return [(pid, m[user_id]) for pid, m in self.members.items() if user_id in m]


# ------------------------------------------------------------------------------ community
class Community:
    def __init__(self) -> None:
        self.published: dict[str, dict] = {}  # token -> entry
        self.stars: dict[str, set[str]] = {}  # token -> users
        self.counters = {"projectsCreated": 0, "accountsCreated": 0}

    def publish(
        self, project_id: str, owner: str, content: ProjectContent, revision: int, image_url: str | None = None
    ) -> dict:
        existing = next((e for e in self.published.values() if e["publishedProjectId"] == project_id), None)
        token = existing["token"] if existing else secrets.token_urlsafe(32)
        entry = {"token": token, "publishedProjectId": project_id, "ownerId": owner, "projectName": content.project_name,
                 "projectDescription": content.project_description or "", "nodeCount": len(content.nodes),
                 "contentRevision": revision, "content": content.dump(), "starCount": len(self.stars.get(token, set())),
                 "publishedAt": existing["publishedAt"] if existing else _now(), "thumbnailUrl": image_url}  # fmt: skip
        self.published[token] = entry
        return entry

    def unpublish(self, project_id: str) -> bool:
        for tok, e in list(self.published.items()):
            if e["publishedProjectId"] == project_id:
                del self.published[tok]
                self.stars.pop(tok, None)
                return True
        return False

    def get(self, token: str) -> dict:
        if token not in self.published:
            raise ServiceError(404, "not_found")
        return self.published[token]

    def star(self, token: str, user_id: str) -> dict:
        self.get(token)
        s = self.stars.setdefault(token, set())
        starred = user_id not in s
        (s.add if starred else s.discard)(user_id)
        self.published[token]["starCount"] = len(s)
        return {"starred": starred, "starCount": len(s)}

    def starred(self, user_id: str) -> list[dict]:
        return [
            self._card(self.published[t]) for t, users in self.stars.items() if user_id in users and t in self.published
        ]

    @staticmethod
    def trending_score(entry: dict, now: float | None = None) -> float:
        age_h = max(0.0, ((now or _now()) - entry["publishedAt"]) / 3600)
        return (entry["starCount"] + 1) / math.pow(age_h + 2, 1.5)  # HN-style gravity

    def list(self, page: int = 0, limit: int = 24, sort: str = "trending", q: str | None = None) -> dict:
        items = list(self.published.values())
        if q:
            ql = q.lower()
            items = [e for e in items if ql in e["projectName"].lower() or ql in e["projectDescription"].lower()]
        key = {
            "trending": lambda e: -self.trending_score(e),
            "new": lambda e: -e["publishedAt"],
            "stars": lambda e: (-e["starCount"], -e["publishedAt"]),
        }
        if sort not in key:
            raise ServiceError(400, "invalid_sort")
        items.sort(key=key[sort])
        chunk = items[page * limit : (page + 1) * limit]
        return {
            "projects": [self._card(e) for e in chunk],
            "total": len(items),
            "hasMore": (page + 1) * limit < len(items),
            "searchMode": bool(q),
        }

    def suggest(self, q: str, limit: int = 8) -> list[str]:
        ql = q.lower().strip()
        names = sorted({e["projectName"] for e in self.published.values()})
        starts = [n for n in names if n.lower().startswith(ql)]
        contains = [n for n in names if ql in n.lower() and n not in starts]
        return (starts + contains)[:limit]

    def stats(self) -> dict:
        return {**self.counters, "publishedProjects": len(self.published)}

    @staticmethod
    def _card(e: dict) -> dict:
        return {
            k: e[k]
            for k in (
                "token",
                "publishedProjectId",
                "projectName",
                "projectDescription",
                "nodeCount",
                "starCount",
                "publishedAt",
                "thumbnailUrl",
            )
        }


# ------------------------------------------------------------------------------ notes (collaborative README)
class Notes:
    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}

    def get(self, project_id: str) -> dict:
        return self.docs.setdefault(project_id, {"text": "", "version": 0, "author": None})

    def put(self, project_id: str, text: str, base_version: int, author: str) -> dict:
        cur = self.get(project_id)
        if base_version != cur["version"]:
            raise ServiceError(409, "readme_conflict", f"current version is {cur['version']}")
        cur.update(text=text, version=cur["version"] + 1, author=author)
        return cur


# ------------------------------------------------------------------------------ uploaded input images
class InputImages:
    MAX_BYTES = 40 * 1024 * 1024

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}

    def add(self, data_url_or_b64: str, mime: str | None = None, draft_key: str | None = None) -> dict:
        m = re.match(r"data:([\w/+.-]+);base64,(.*)$", data_url_or_b64 or "", re.S)
        mime, b64 = (m.group(1), m.group(2)) if m else (mime or "image/png", data_url_or_b64 or "")
        try:
            data = base64.b64decode(b64, validate=False)
        except Exception as e:  # noqa: BLE001
            raise ServiceError(400, "invalid_image") from e
        if not data:
            raise ServiceError(400, "invalid_image", "empty image")
        if len(data) > self.MAX_BYTES:
            raise ServiceError(413, "image_too_large", "max 40 MB")
        if mime not in ("image/png", "image/jpeg", "image/webp", "image/x-portable-graymap", "image/x-portable-pixmap"):
            raise ServiceError(415, "unsupported_image_type")
        digest = hashlib.sha256(data).hexdigest()
        iid = uuid.uuid4().hex
        self.items[iid] = {
            "id": iid,
            "mimeType": mime,
            "hash": digest,
            "bytes": data,
            "draftKey": draft_key,
            "url": f"/api/input-images/{iid}",
        }
        return {k: v for k, v in self.items[iid].items() if k != "bytes"}

    def get(self, image_id: str) -> dict:
        if image_id not in self.items:
            raise ServiceError(404, "input_image_expired", "The attached image expired — please re-attach it.")
        return self.items[image_id]


# ------------------------------------------------------------------------------ chat turns
TURN_TTL_S = 15 * 60


def diff_plan(old: ProjectContent, new: ProjectContent) -> dict:
    """Express the change old -> new as a reference-format edit plan (patch)."""
    old_nodes = {n.id: n for n in old.nodes}
    new_nodes = {n.id: n for n in new.nodes}
    changed = [
        i
        for i in new_nodes
        if i in old_nodes
        and (old_nodes[i].catalog_id or old_nodes[i].product_name)
        != (new_nodes[i].catalog_id or new_nodes[i].product_name)
    ]
    remove = sorted([i for i in old_nodes if i not in new_nodes] + changed)
    add = [new_nodes[i].dump() for i in new_nodes if i not in old_nodes or i in changed]

    def esig(c):
        return (c.type, c.source, c.target, c.source_pin or "", c.target_pin or "")

    oe, ne = {esig(c): c for c in old.electrical_connections}, {esig(c): c for c in new.electrical_connections}
    om = {(c.source, c.target, c.label): c for c in old.mechanical_connections}
    nm = {(c.source, c.target, c.label): c for c in new.mechanical_connections}
    new_pins = []
    for i in new_nodes:
        if i in old_nodes and i not in changed:
            extra = sorted(set(new_nodes[i].pins or []) - set(old_nodes[i].pins or []))
            if extra:
                new_pins.append({"nodeId": i, "pins": extra})
    summary = []
    if add:
        summary.append("add " + ", ".join(n["name"] for n in add))
    if remove:
        summary.append("remove " + ", ".join(old_nodes[i].name for i in remove))
    return {
        "kind": "plan",
        "plan": "; ".join(summary) or "rewire / re-layout only",
        "addParts": add,
        "removeNodeIds": remove,
        "addElectricalConnections": [ne[k].dump() for k in ne if k not in oe],
        "removeElectricalConnections": [
            {"source": k[1], "target": k[2], "type": k[0], "sourcePin": k[3] or None, "targetPin": k[4] or None}
            for k in oe
            if k not in ne
        ],
        "addMechanicalConnections": [nm[k].dump() for k in nm if k not in om],
        "removeMechanicalConnections": [{"source": k[0], "target": k[1]} for k in om if k not in nm],
        "newPinsUsedOnExistingNodes": new_pins,
    }


@dataclass
class Turn:
    id: str
    prompt: str
    mode: str
    actor: str
    status: str
    base_version: int
    created_at: float
    revision: int = 1
    result_kind: str | None = None
    response_text: str | None = None
    plan: dict | None = None
    ops: list[dict] = field(default_factory=list)
    preview: ProjectContent | None = None
    deadline: float | None = None
    failure_code: str | None = None
    approver: str | None = None

    def public(self) -> dict:
        return {"id": self.id, "prompt": self.prompt, "mode": self.mode, "actorId": self.actor, "status": self.status,
                "baseVersion": self.base_version, "revision": self.revision, "resultKind": self.result_kind,
                "responseText": self.response_text, "plan": self.plan, "ops": self.ops, "deadline": self.deadline,
                "failureCode": self.failure_code, "approverName": self.approver, "createdAt": self.created_at}  # fmt: skip


class ChatLog:
    def __init__(self) -> None:
        self.turns: dict[str, list[Turn]] = {}
        self.epoch: dict[str, int] = {}
        self.rev: dict[str, int] = {}

    def bump(self, project_id: str) -> None:
        self.rev[project_id] = self.rev.get(project_id, 0) + 1

    def add(self, project_id: str, turn: Turn) -> Turn:
        self.turns.setdefault(project_id, []).append(turn)
        self.bump(project_id)
        return turn

    def find(self, project_id: str, turn_id: str) -> Turn:
        t = next((t for t in self.turns.get(project_id, []) if t.id == turn_id), None)
        if t is None:
            raise ServiceError(404, "turn_not_found")
        return t

    def snapshot(self, project_id: str, version: int) -> dict:
        return {"enabled": True, "historyEpoch": self.epoch.get(project_id, 0), "revision": self.rev.get(project_id, 0),
                "version": version, "turns": [t.public() for t in self.turns.get(project_id, [])], "activity": None}  # fmt: skip

    def clear(self, project_id: str) -> dict:
        self.turns[project_id] = []
        self.epoch[project_id] = self.epoch.get(project_id, 0) + 1
        self.bump(project_id)
        return {"cleared": True, "historyEpoch": self.epoch[project_id]}
