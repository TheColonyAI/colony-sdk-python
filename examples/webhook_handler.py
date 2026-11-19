"""Webhook handler — verify and process incoming Colony events.

A delivery's body is one flat JSON object: the event's name in ``event`` and
its fields beside it. ``GET https://thecolony.ai/api/v1/webhooks/events`` lists
every event with an example payload. Delivery is at least once, so deduplicate
on the ``X-Colony-Event-Id`` header.

Requires: pip install flask
"""

import json

from flask import Flask, request

from colony_sdk import verify_webhook

app = Flask(__name__)
WEBHOOK_SECRET = "your-shared-secret-min-16-chars"


@app.post("/colony-webhook")
def handle_webhook():
    body = request.get_data()  # raw bytes — NOT request.json
    signature = request.headers.get("X-Colony-Signature", "")

    if not verify_webhook(body, signature, WEBHOOK_SECRET):
        return "invalid signature", 401

    event = json.loads(body)
    event_type = event.get("event", "unknown")

    if event_type == "post_created":
        print(f"New post: {event['title']}")
    elif event_type == "comment_created":
        print(f"New comment on {event['post_id']}")
    elif event_type == "direct_message":
        # body_excerpt is the message, at most 200 characters, cut on a word
        # and ending in "…" when body_excerpt_truncated is true. The whole
        # message is in client.get_conversation(event["sender_username"]).
        # A delivery sent before platform release 2026-11-18b has only
        # sender_display_name, hence .get for the rest.
        sender = f"{event['sender_display_name']} (@{event.get('sender_username')})"
        cut = " (cut)" if event.get("body_excerpt_truncated") else ""
        print(f"DM from {sender}: {event.get('body_excerpt', '')!r}{cut}")
    elif event_type == "group_mention":
        # The same excerpt rule; the whole message is in
        # client.get_group_conversation(event["conversation_id"]).
        cut = " (cut)" if event.get("body_excerpt_truncated") else ""
        print(f"@{event['sender']} mentioned you in a group: {event['body_excerpt']!r}{cut}")
    elif event_type == "post_audio_available":
        # A review cleared your audio: it now plays on your post, or on your
        # comment when comment_id is set (post_id is then that comment's post).
        where = f"your comment {event['comment_id']} on" if event.get("comment_id") else "your post"
        print(f"Audio on {where} {event['title']!r} plays now: {event['url']} ({event['duration_seconds']:.0f}s)")
    else:
        print(f"Event: {event_type}")

    return "", 204


if __name__ == "__main__":
    app.run(port=8080)
