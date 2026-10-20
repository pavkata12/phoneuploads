import json
import re

PASSWORD = "test-password-123"


def login(client, password: str = PASSWORD) -> str:
    page = client.get("/login")
    assert page.status_code == 200, page.text
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match, page.text
    response = client.post(
        "/login",
        data={"csrf_token": match.group(1), "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    home = client.get("/")
    assert home.status_code == 200, home.text
    boot_match = re.search(
        r'<script id="boot" type="application/json">(.*?)</script>',
        home.text,
        re.DOTALL,
    )
    assert boot_match, home.text
    return json.loads(boot_match.group(1))["csrfToken"]


def csrf_headers(token: str) -> dict[str, str]:
    return {"X-CSRF-Token": token}
