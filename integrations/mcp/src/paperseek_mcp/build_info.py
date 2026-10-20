from __future__ import annotations

from paperseek_app.build_info import COMMUNITY_COMMIT


DISTRIBUTION = "paperseek-mcp"
VERSION = "1.0.0b4"
SERVER_NAME = "io.github.paperseek/paperseek"


def as_dict() -> dict[str, str]:
    return {
        "distribution": DISTRIBUTION,
        "version": VERSION,
        "community_commit": COMMUNITY_COMMIT,
        "server_name": SERVER_NAME,
    }


__all__ = ["COMMUNITY_COMMIT", "DISTRIBUTION", "SERVER_NAME", "VERSION", "as_dict"]
