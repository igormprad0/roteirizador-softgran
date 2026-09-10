from fastapi.testclient import TestClient
from api.app.main import app

client = TestClient(app)


def test_index_servido_em_app():
    r = client.get("/app/")
    assert r.status_code == 200
    assert "Roteirizador" in r.text


def test_leaflet_vendorizado_e_nao_vem_de_cdn():
    assert client.get("/app/vendor/leaflet.js").status_code == 200
    assert client.get("/app/vendor/leaflet.css").status_code == 200
    html = client.get("/app/").text
    assert "unpkg.com" not in html and "cdn." not in html


def test_app_js_e_css_servidos():
    assert client.get("/app/app.js").status_code == 200
    assert client.get("/app/style.css").status_code == 200


def test_raiz_responde():
    assert client.get("/").status_code == 200
