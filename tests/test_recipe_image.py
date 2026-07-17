"""Recipe photo upload: save under data/images, serve it, and reject bad files."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import SAME_ORIGIN

_PNG = b"\x89PNG\r\n\x1a\nfake-image-bytes"


def _base(title: str) -> dict[str, str]:
    return {"title": title, "base_servings": "4", "steps": "", "tags": ""}


def test_upload_and_serve(admin_client: TestClient) -> None:
    admin_client.post(
        "/recipes/new",
        data=_base("Photo Recipe"),
        files={"image": ("photo.png", _PNG, "image/png")},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    img = admin_client.get("/recipes/photo-recipe/image")
    assert img.status_code == 200
    assert img.headers["content-type"] == "image/png"
    assert img.content == _PNG
    assert "/recipes/photo-recipe/image" in admin_client.get("/recipes/photo-recipe").text


def test_no_image_is_404(admin_client: TestClient) -> None:
    admin_client.post(
        "/recipes/new", data=_base("No Photo"), headers=SAME_ORIGIN, follow_redirects=False
    )
    assert admin_client.get("/recipes/no-photo/image").status_code == 404


def test_disallowed_extension_ignored(admin_client: TestClient) -> None:
    admin_client.post(
        "/recipes/new",
        data=_base("Bad Ext"),
        files={"image": ("evil.exe", b"MZ\x90\x00", "application/octet-stream")},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert admin_client.get("/recipes/bad-ext/image").status_code == 404
