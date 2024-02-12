import re
from typing import Optional

import packaging.version
import pkg_resources
import requests

from mentat.session_context import SESSION_CONTEXT


def get_changelog() -> Optional[str]:
    try:
        response = requests.get(
            "https://raw.githubusercontent.com/AbanteAI/mentat/main/CHANGELOG.rst"
        )
        if response.status_code == 200:
            return response.text
        else:
            return None
    except Exception:
        return None


def get_latest_changelog(full_changelog: Optional[str] = None) -> Optional[str]:
    if full_changelog is None:
        full_changelog = get_changelog()
    if full_changelog is None:
        return None
    try:
        sections = re.split("\n[^\n]+\n-{1,}\n", full_changelog)
        return sections[1].strip()
    except Exception:
        return None


def check_version():
    ctx = SESSION_CONTEXT.get()

    try:
        response = requests.get("https://pypi.org/pypi/mentat/json")
        data = response.json()
        latest_version = data["info"]["version"]
        current_version = pkg_resources.require("mentat")[0].version

        if packaging.version.parse(current_version) < packaging.version.parse(
            latest_version
        ):
            ctx.stream.send(
                f"Version v{latest_version} of Mentat is available. If pip was used to"
                " install Mentat, upgrade with:",
                style="warning",
            )
            ctx.stream.send("pip install --upgrade mentat", style="warning")
            changelog = get_latest_changelog()
            if changelog:
                ctx.stream.send(
                    "Upgrade for the following features/improvements:", style="warning"
                )
                ctx.stream.send(changelog, style="warning")
    except Exception as err:
        ctx.stream.send(f"Error checking for most recent version: {err}", style="error")
