"""
PushPlus WeChat push integration.
"""
import time

import requests

PUSHPLUS_API_URL = "https://www.pushplus.plus/send"


def _try_send(payload: dict) -> bool:
    """Single attempt to POST."""
    try:
        resp = requests.post(PUSHPLUS_API_URL, json=payload, timeout=15)
        data = resp.json()
        code = data.get("code", -1)
        if code == 200:
            return True
        print(f"[PushPlus] API error (code={code}): {data}")
        return False
    except requests.RequestException as e:
        print(f"[PushPlus] Network error: {e}")
        return False


def send_report(
    user_token: str,
    title: str,
    content: str,
    topic_token: str = "",
    template: str = "markdown",
) -> bool:
    """
    Send via PushPlus. Retries up to 2 times with 3s delay to avoid rate limiting.
    """
    payload = {
        "token": user_token,
        "title": title,
        "content": content,
        "template": template,
    }

    print(f"[PushPlus] Sending: {title}")

    for attempt in range(1, 4):  # 3 attempts
        if _try_send(payload):
            print(f"[PushPlus] OK")
            return True
        if attempt < 3:
            time.sleep(3)  # Wait between retries to avoid rate limiting

    print(f"[PushPlus] FAILED after 3 attempts")
    return False


def send_alert(
    user_token: str,
    title: str,
    content: str,
    topic_token: str = "",
) -> bool:
    """Send a short alert (plain text)."""
    return send_report(user_token, title, content, template="txt")
