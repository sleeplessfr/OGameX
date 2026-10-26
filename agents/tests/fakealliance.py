"""A stand-in for the alliance and chat endpoints of the OGameX API (docs/API.md, "Alliances and chat"), shared by several FakeGames.

One AllianceWorld is the whole universe's alliances; each FakeGame with `alliance_stuff` talks to it as its own user id.
Error codes and response shapes follow app/Http/Controllers/Api/V1/AllianceController.php and ChatController.php.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from swarm.client import ApiError

MAX_MEMBERS_IN_GAME = 10 ** 6    # the game itself has no member limit: the agents keep their own


class AllianceWorld:
    def __init__(self, cooldown: float = 0.0):
        self.alliances: dict[int, dict] = {}
        self.apps: dict[int, dict] = {}
        self.chat: list[dict] = []
        self.user_alliance: dict[int, int] = {}
        self.left_at: dict[int, float] = {}
        self.names: dict[int, str] = {}      # user id -> username
        self.cooldown = cooldown
        self.next_id = 1
        self.next_app = 1
        self.next_msg = 1
        self.kicked: list[int] = []

    # -- helpers --
    def _name(self, uid):
        return self.names.get(uid, f"user{uid}")

    def _public(self, a: dict) -> dict:
        return {"id": a["id"], "tag": a["tag"], "name": a["name"], "open": a["open"], "founder_user_id": a["founder"],
                "member_count": len(a["members"]), "external_text": None, "application_text": None, "homepage_url": None}

    def _members(self, a: dict) -> list[dict]:
        return [{"user_id": u, "username": self._name(u), "is_founder": u == a["founder"]} for u in a["members"]]

    def _check_cooldown(self, uid, now):
        if uid in self.left_at and now < self.left_at[uid] + self.cooldown:
            raise ApiError(422, "alliance_cooldown", "You must wait more day(s) before you can create or join an alliance.")

    # -- API --
    def get(self, uid: int, path: str, now: float):
        url = urlparse(path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        route = url.path
        if route == "/alliance":
            aid = self.user_alliance.get(uid)
            if aid is None:
                return {"alliance": None}
            a = self.alliances[aid]
            founder = a["founder"] == uid
            apps = [dict(p, username=self._name(p["user_id"])) for p in self.apps.values()
                    if p["alliance_id"] == aid and p["status"] == "pending"] if founder else None
            return {"alliance": {**self._public(a), "internal_text": None, "members": self._members(a), "ranks": [], "my_user_id": uid,
                                 "my_rank_id": None, "is_founder": founder, "my_permissions": ["edit_applications"] if founder else [],
                                 "applications": apps}}
        if route == "/alliances":
            rows = [self._public(a) for a in self.alliances.values() if not (q.get("open_only") == "1" and not a["open"])]
            return {"alliances": rows, "page": 1, "per_page": 100, "total": len(rows), "total_pages": 1}
        if route.startswith("/alliances/"):
            a = self.alliances.get(int(route.rsplit("/", 1)[1]))
            if a is None:
                raise ApiError(404, "alliance_not_found", "Alliance not found.")
            return {"alliance": {**self._public(a), "members": self._members(a)}}
        if route == "/chat":
            if q.get("alliance") != "1":
                return {"unread_total": 0, "conversations": []}
            aid = self.user_alliance.get(uid)
            if aid is None:
                raise ApiError(422, "not_in_alliance", "The player is not in an alliance.")
            if "since_id" in q and int(q["since_id"]) < 1:
                raise ApiError(422, "validation_failed", "The given data was invalid.")
            since = int(q.get("since_id", 0))
            rows = [m for m in self.chat if m["alliance_id"] == aid and m["id"] > since][-int(q.get("limit", 50)):]
            return {"alliance_id": aid, "messages": [dict(m) for m in rows]}
        raise ApiError(404, "http_error", "The route could not be found.")

    def post(self, uid: int, path: str, body: dict, now: float):
        if path == "/alliance":
            if uid in self.user_alliance:
                raise ApiError(422, "already_in_alliance", "The player is already in an alliance.")
            tag, name = str(body.get("tag", "")), str(body.get("name", ""))
            if not 3 <= len(tag) <= 8 or not 3 <= len(name) <= 30:
                raise ApiError(422, "validation_failed", "The given data was invalid.")
            if any(a["tag"] == tag for a in self.alliances.values()):
                raise ApiError(422, "tag_taken", "Alliance tag is already taken.")
            if any(a["name"] == name for a in self.alliances.values()):
                raise ApiError(422, "name_taken", "Alliance name is already taken.")
            self._check_cooldown(uid, now)
            a = {"id": self.next_id, "tag": tag, "name": name, "open": True, "founder": uid, "members": [uid]}
            self.next_id += 1
            self.alliances[a["id"]] = a
            self.user_alliance[uid] = a["id"]
            return {"alliance": self._public(a)}
        if path.startswith("/alliances/") and path.endswith("/apply"):
            a = self.alliances.get(int(path.split("/")[2]))
            if a is None:
                raise ApiError(404, "alliance_not_found", "Alliance not found.")
            if uid in self.user_alliance:
                raise ApiError(422, "already_in_alliance", "The player is already in an alliance.")
            if not a["open"]:
                raise ApiError(422, "alliance_closed", "Alliance is not accepting applications.")
            if any(p["alliance_id"] == a["id"] and p["user_id"] == uid and p["status"] == "pending" for p in self.apps.values()):
                raise ApiError(422, "application_pending", "You already have a pending application to this alliance.")
            self._check_cooldown(uid, now)
            p = {"id": self.next_app, "alliance_id": a["id"], "user_id": uid, "text": body.get("text"), "status": "pending", "created_at": int(now)}
            self.next_app += 1
            self.apps[p["id"]] = p
            return {"application": dict(p)}
        if path.startswith("/alliance/applications/"):
            _, _, _, pid, action = path.split("/")
            p = self.apps.get(int(pid))
            aid = self.user_alliance.get(uid)
            if p is None or aid is None or p["alliance_id"] != aid:
                raise ApiError(404, "application_not_found", "Application not found.")
            if p["status"] != "pending":
                raise ApiError(422, "application_not_pending", "The application was already handled.")
            if self.alliances[aid]["founder"] != uid:
                raise ApiError(403, "no_permission", "You do not have permission to accept applications")
            if action == "accept":
                if p["user_id"] in self.user_alliance:
                    raise ApiError(422, "already_in_alliance", "Applicant is already in an alliance")
                p["status"] = "accepted"
                self.alliances[aid]["members"].append(p["user_id"])
                self.user_alliance[p["user_id"]] = aid
            else:
                p["status"] = "rejected"
            return {"application": dict(p)}
        if path == "/alliance/leave":
            aid = self.user_alliance.get(uid)
            if aid is None:
                raise ApiError(422, "not_in_alliance", "The player is not in an alliance.")
            a = self.alliances[aid]
            if a["founder"] == uid:
                raise ApiError(422, "founder_protected", "Founder cannot leave alliance.")
            a["members"].remove(uid)
            del self.user_alliance[uid]
            self.left_at[uid] = now
            return {"left": True}
        if path == "/alliance/kick":
            aid = self.user_alliance.get(uid)
            if aid is None:
                raise ApiError(422, "not_in_alliance", "The player is not in an alliance.")
            a = self.alliances[aid]
            target = int(body["user_id"])
            if a["founder"] != uid:
                raise ApiError(403, "no_permission", "You do not have permission to kick members")
            if target == uid:
                raise ApiError(422, "founder_protected", "Cannot kick the alliance founder")
            if target not in a["members"]:
                raise ApiError(404, "member_not_found", "Member not found in alliance")
            a["members"].remove(target)
            del self.user_alliance[target]
            self.kicked.append(target)
            return {"kicked": target}
        if path == "/chat/send":
            aid = self.user_alliance.get(uid)
            if not body.get("alliance") or aid is None:
                raise ApiError(422, "not_in_alliance", "The player is not in an alliance.")
            m = {"id": self.next_msg, "from_user_id": uid, "from": self._name(uid), "to_user_id": None, "alliance_id": aid,
                 "text": body["text"], "reply_to": None, "created_at": int(now)}
            self.next_msg += 1
            self.chat.append(m)
            return {"message": dict(m)}
        raise ApiError(404, "http_error", "The route could not be found.")

    def say(self, uid: int, text: str, now: float):
        """A message put into the alliance chat by the test, as user `uid`."""
        return self.post(uid, "/chat/send", {"alliance": True, "text": text}, now)
