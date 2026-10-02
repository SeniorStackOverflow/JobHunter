from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode
from uuid import UUID


@dataclass(frozen=True)
class Panel:
    """Presentation links only; route handlers enforce authentication and ownership."""

    is_admin: bool
    profile_id: UUID | None = None

    @property
    def base(self) -> str:
        return "/admin" if self.is_admin else "/app"

    def view(self, view: str, **params: str | int) -> str:
        query: dict[str, str | int] = {"view": view}
        if self.profile_id is not None:
            query["profile_id"] = str(self.profile_id)
        query.update(params)
        if self.is_admin and view == "accounts":
            query.pop("view")
            return "/admin/accounts" + (f"?{urlencode(query)}" if query else "")
        return f"{self.base}?{urlencode(query)}"

    def application(self, application_id: UUID | str, action: str | None = None) -> str:
        suffix = f"/{action}" if action else ""
        return f"{self.base}/applications/{application_id}{suffix}"

    def resume(self, resume_id: UUID | str, action: str) -> str:
        url = f"{self.base}/resumes/{resume_id}/{action}"
        if action == "file" and self.is_admin and self.profile_id is not None:
            url += f"?{urlencode({'profile_id': str(self.profile_id)})}"
        return url

    def action(self, action: str) -> str:
        if action in {"profiles", "logout", "invites", "review-learning/influence"}:
            return f"{self.base}/{action}"
        if self.is_admin:
            return f"{self.base}/{action}"
        profile_actions = {
            "profile": "update",
            "preferences": "preferences",
            "resumes": "resumes",
            "default": "default",
        }
        if action in profile_actions:
            return f"{self.base}/profiles/{self.profile_id}/{profile_actions[action]}"
        raise ValueError(f"unsupported account panel action: {action}")

    def source_selection(self, source_id: UUID | str) -> str:
        if self.is_admin:
            return f"/admin/profile-sources/{source_id}/selection"
        return f"/app/profiles/{self.profile_id}/sources/{source_id}"

    def source_categories(self, source_id: UUID | str) -> str:
        if self.is_admin:
            return f"/admin/profile-sources/{source_id}/categories"
        return f"/app/profiles/{self.profile_id}/sources/{source_id}/categories"

    def auto_send(self, paused: bool) -> str:
        if self.is_admin:
            return f"/admin/pause/{str(paused).lower()}"
        return f"/app/profiles/{self.profile_id}/auto-send/{'pause' if paused else 'resume'}"

    @property
    def gmail_connect(self) -> str:
        return "/admin/oauth/gmail/connect" if self.is_admin else "/app/gmail/connect"

    @property
    def gmail_disconnect(self) -> str:
        return "/admin/oauth/gmail/disconnect" if self.is_admin else "/app/gmail/disconnect"
