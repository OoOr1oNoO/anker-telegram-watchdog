"""Chats that may use the bot: roles, access requests and languages, stored in SQLite.
"""


from typing import TYPE_CHECKING

from .i18n import Lang, lang

if TYPE_CHECKING:
    from .stats import Stats


ACTIVE_ROLES = ("admin", "user")


STATUS_STYLES = ("widget", "ios", "clock", "text")
ACCESS_MODES = {"closed": "acc_closed", "requests": "acc_requests", "open": "acc_open"}


class Users:
    """Chats that get alerts and may use the bot, with roles and languages, in the stats DB.

    Roles: admin (controls the station and the bot), user (alerts and viewing), pending
    (asked for access), blocked (denied). TG_CHAT_ID / TG_ADMIN_IDS from .env are re-applied on
    every start and cannot be changed from the bot; when TG_ADMIN_IDS is empty, the people in
    TG_CHAT_ID are admins (as in older versions, where everyone could control the station).
    """

    def __init__(self, stats: "Stats", env_chats: list[str], env_admins: list[str], default: Lang):
        self.stats = stats
        self.default = default
        self.awaiting_id: dict[tuple[str, str], float] = {}  # (chat, admin) -> time of "Add by ID"
        stats._exec("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, name TEXT, role TEXT, "
                    "lang TEXT, lang_auto TEXT)")
        admins = env_admins or [c for c in env_chats if not c.startswith("-")]
        self.env_ids = set(env_chats) | set(admins)
        for chat in dict.fromkeys(env_chats + admins):
            if chat in admins:
                self.upsert(chat, role="admin")
            elif self.role(chat) not in ACTIVE_ROLES:
                self.upsert(chat, role="user")

    def status_style(self, chat: str) -> str:
        """How 📊 Status looks in this chat: "widget", "ios" (app-like), "clock" (a 24-hour dial) or
        "text" (Telegram apps without widgets)."""
        style = self.stats.meta(f"status_style:{chat}")
        if style in STATUS_STYLES:
            return style
        return "text" if self.stats.meta(f"status_text:{chat}") == "1" else "widget"  # before 2.3

    def set_status_style(self, chat: str, style: str) -> None:
        if style in STATUS_STYLES:
            self.stats.meta(f"status_style:{chat}", style)

    def _row(self, chat: str) -> tuple | None:
        rows = self.stats._exec("SELECT name, role, lang, lang_auto FROM users WHERE id = ?", (chat,))
        return rows[0] if rows else None

    def role(self, chat: str) -> str | None:
        row = self._row(chat)
        return row[1] if row else None

    def who(self, chat: str) -> str:
        row = self._row(chat)
        return f"{row[0]} ({chat})" if row and row[0] else chat

    def upsert(self, chat: str, **fields) -> None:
        if self._row(chat) is None:
            self.stats._exec("INSERT INTO users (id) VALUES (?)", (chat,))
        for key, value in fields.items():
            if key in ("name", "role", "lang", "lang_auto") and value is not None:
                self.stats._exec(f"UPDATE users SET {key} = ? WHERE id = ?", (value, chat))

    def remove(self, chat: str) -> None:
        self.stats._exec("DELETE FROM users WHERE id = ?", (chat,))

    def all(self) -> list[tuple[str, str, str]]:
        """(id, name, role), admins first."""
        rows = self.stats._exec("SELECT id, COALESCE(name, ''), role FROM users")
        order = {"admin": 0, "user": 1, "pending": 2, "blocked": 3}
        return sorted(rows, key=lambda r: (order.get(r[2], 9), r[1].lower(), r[0]))

    def recipients(self) -> list[str]:
        return [r[0] for r in self.all() if r[2] in ACTIVE_ROLES]

    def admins(self) -> list[str]:
        return [r[0] for r in self.all() if r[2] == "admin"]

    def is_admin(self, user: str) -> bool:
        return self.role(user) == "admin"

    def lang(self, chat: str) -> Lang:
        row = self._row(chat)
        return lang((row[2] or row[3]) if row and (row[2] or row[3]) else self.default.code)

    def note(self, chat: str, name: str, code: str | None, private: bool) -> None:
        """Remember the chat name and the Telegram app language (groups keep the first one)."""
        row = self._row(chat)
        if row is None:
            return
        auto = Lang(code).code if code else None
        self.upsert(chat, name=name if name != row[0] else None,
                    lang_auto=auto if auto and (private or not row[3]) and auto != row[3] else None)

    @property
    def access(self) -> str:
        mode = self.stats.meta("setting:access")
        return mode if mode in ACCESS_MODES else "closed"

    @access.setter
    def access(self, mode: str) -> None:
        self.stats.meta("setting:access", mode)


def chat_name(chat: dict, user: dict | None = None) -> str:
    if chat.get("title"):
        return chat["title"]
    user = user or chat
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
    if user.get("username"):
        name = f"{name} @{user['username']}".strip()
    return name or "?"
