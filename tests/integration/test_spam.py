"""Integration smoke for DM-spam moderation (mark_conversation_spam /
unmark_conversation_spam).

We deliberately do NOT submit a real spam report against the secondary
test account here — every run would generate operator-side moderation
noise on the platform side. The unit tests in ``tests/test_api_methods.py``
and ``tests/test_async_client.py`` exercise the request construction
(method, URL, body shape, header-derived ``idempotency_replayed``)
against mocked transports; this file just confirms the methods are
wired on the live client so the integration suite carries a
remember-this-exists marker into release time.

If you want to perform an actual end-to-end test against staging /
prod, do it ad-hoc with the second integration-tester account and
unmark in the same session.
"""

from __future__ import annotations

from colony_sdk import ColonyClient


class TestSpamSmoke:
    """Smoke check that the spam-moderation methods are reachable.

    See module docstring for why we don't fire real reports here.
    """

    def test_spam_methods_are_present_on_live_client(self, client: ColonyClient) -> None:
        assert callable(client.mark_conversation_spam)
        assert callable(client.unmark_conversation_spam)

    def test_last_response_headers_present_on_live_client(self, client: ColonyClient) -> None:
        # Attribute exists from construction (empty until first request).
        assert isinstance(client.last_response_headers, dict)
        # After any live call, the snapshot should be populated.
        client.get_me()
        assert client.last_response_headers, (
            "last_response_headers should be populated after a real request"
        )
