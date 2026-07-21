"""
PushPlus WeChat push integration.
"""
import json
import time

import requests

PUSHPLUS_API_URL = "http://www.pushplus.plus/send"


def _post_with_retry(payload: dict, max_retries: int = 3, fallback_token: str = "") -> bool:
    """POST to PushPlus API with retry logic. Falls back to alternative token on auth failure."""
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                PUSHPLUS_API_URL,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                },
                timeout=15,
            )
            data = resp.json()
            if data.get("code") == 200:
                return True
            msg = data.get("msg", "")
            print(f"[PushPlus] API error (attempt {attempt}/{max_retries}): {msg}")

            # If auth failed and we have a fallback token, try it
            if "认证" in str(msg) and fallback_token:
                payload["token"] = fallback_token
                print(f"[PushPlus] Trying fallback token...")
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
    ok = _post_with_retry(payload, fallback_token=topic_token)
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
