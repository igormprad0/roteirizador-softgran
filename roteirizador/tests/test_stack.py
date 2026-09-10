# tests/test_stack.py
import os, httpx, pytest

OSRM = os.environ.get("OSRM_URL", "http://osrm:5000")
VROOM = os.environ.get("VROOM_URL", "http://vroom:3000")

# Centro de Dourados-MS -> Av. Marcelino Pires, em (lon, lat)
A, B = (-54.8060, -22.2210), (-54.8180, -22.2280)

@pytest.mark.stack
def test_osrm_responde_rota_em_dourados():
    r = httpx.get(f"{OSRM}/route/v1/driving/{A[0]},{A[1]};{B[0]},{B[1]}", timeout=30)
    r.raise_for_status()
    body = r.json()
    assert body["code"] == "Ok"
    assert body["routes"][0]["distance"] > 0

@pytest.mark.stack
def test_vroom_resolve_problema_minimo():
    payload = {
        "vehicles": [{"id": 1, "start": list(A), "end": list(A)}],
        "jobs": [{"id": 1, "location": list(B)}],
    }
    r = httpx.post(VROOM, json=payload, timeout=60)
    r.raise_for_status()
    body = r.json()
    assert body["code"] == 0
    assert len(body["routes"]) == 1
