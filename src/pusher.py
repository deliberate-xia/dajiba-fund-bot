"""
PushPlus WeChat push integration.
"""
import json
import time

import requests

PUSHPLUS_API_URL = "http://www.pushplus.plus/send"


def _post_with_retry(payload: dict, max_retries: int = 3) -> bool:
    """POST to PushPlus API with retry logic."""
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                PUSHPLUS_API_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            data = resp.json()
            if data.get("code") == 200:
                return True
            print(f"[PushPlus] API error (attempt {attempt}/{max_retries}): {data.get('msg')}")
        except requests.RequestException as e:
            print(f"[PushPlus] Network error (attempt {attempt}/{max_retries}): {e}")
        if attempt < max_retries:
            time.sleep(2 ** attempt)
    return False


def send_report(
    user_token: str,
    title: str,
    content: str,
    topic_token: str = "",
    template: str = "markdown",
) -> bool:
    """
    Send a Markdown report via PushPlus.

    Args:
        user_token: PushPlus user token
        title: Message title (shown in WeChat notification)
        content: Markdown-formatted message body
        topic_token: Optional topic/channel token
        template: "markdown" | "html" | "txt"

    Returns:
        True if sent successfully.
    """
    payload = {
        "token": user_token,
        "title": title,
        "content": content,
        "template": template,
    }
    if topic_token:
        payload["topic"] = topic_token

    print(f"[PushPlus] Sending report: {title}")
    ok = _post_with_retry(payload)
    print(f"[PushPlus] Send {'OK' if ok else 'FAILED'}")
    return ok


def send_alert(
    user_token: str,
    title: str,
    content: str,
    topic_token: str = "",
) -> bool:
    """
    Send a short alert (plain text).
    """
    return send_report(user_token, title, content, topic_token, template="txt")
