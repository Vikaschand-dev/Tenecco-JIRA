"""Identifies the current *platform* user.

This starter keeps an anonymous user id in a signed session cookie so it runs
standalone. When embedding into your platform, replace `get_current_user_id`
with your own auth (e.g. decode your JWT / session and return your user id).
Everything else keys Jira connections off whatever string this returns.
"""

import uuid

from fastapi import Request


def get_current_user_id(request: Request) -> str:
    user_id = request.session.get("user_id")
    if not user_id:
        user_id = uuid.uuid4().hex
        request.session["user_id"] = user_id
    return user_id
