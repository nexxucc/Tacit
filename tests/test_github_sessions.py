"""Connection reuse must not cache eligibility or carry cookies between users."""

import httpx
from tacit_api.github import GitHub


def test_eligibility_is_fresh_and_cookies_are_operation_scoped():
    calls = []
    owner = {"id": 1, "login": "owner", "type": "User"}

    def respond(request):
        calls.append(request)
        if request.url.path == "/user/installations":
            assert "cookie" not in request.headers
            if len(calls) > 2:
                return httpx.Response(200, json={"installations": []})
            return httpx.Response(
                200,
                headers={"Set-Cookie": "operation=private; Path=/"},
                json={
                    "installations": [
                        {"id": 10, "app_id": 100, "account": owner, "suspended_at": None}
                    ]
                },
            )
        assert request.headers["Authorization"] == "Bearer first-user"
        assert request.headers["Cookie"] == "operation=private"
        return httpx.Response(200, json={"repositories": []})

    github = GitHub("fixture", 100, httpx.MockTransport(respond))
    assert len(github.eligibility("first-user")) == 1
    assert github.eligibility("first-user") == ()  # Recheck the same token after removal.
    assert github.eligibility("second-user") == ()
    assert len(calls) == 4
