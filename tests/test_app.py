import os
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_state():
    response = client.get("/state")
    assert response.status_code == 200
    assert "video" in response.json()


def test_upload_rejects_non_video(tmp_path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("hello")

    with open(test_file, "rb") as f:
        response = client.post(
            "/upload",
            files={"file": ("test.txt", f, "text/plain")}
        )

    assert response.status_code == 400
    assert "error" in response.json()


def test_protocol_upload_accepts_txt(tmp_path):
    protocol = tmp_path / "protocol.txt"
    protocol.write_text("5;Сидорова Женя\n6;Красова Анна\n", encoding="utf-8")

    with open(protocol, "rb") as f:
        response = client.post(
            "/protocol/upload",
            files={"file": ("protocol.txt", f, "text/plain")}
        )

    assert response.status_code == 200
    assert response.json()["filename"].endswith(".txt")


def test_protocol_upload_rejects_unsupported_extension(tmp_path):
    protocol = tmp_path / "protocol.json"
    protocol.write_text('{"5":"Сидорова Женя"}', encoding="utf-8")

    with open(protocol, "rb") as f:
        response = client.post(
            "/protocol/upload",
            files={"file": ("protocol.json", f, "application/json")}
        )

    assert response.status_code == 400
    assert "error" in response.json()
