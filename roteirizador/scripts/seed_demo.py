"""Configura frota e depósito para a demo dos dois perfis.
Rodar: docker compose run --rm api python scripts/seed_demo.py"""
from api.app.config import Profile
from api.app.models import Depot, VehicleConfig
from api.app import service

st = service.store()

# Dourados-MS. Conferir no mapa e ajustar arrastando, se necessário.
st.put_depot(Profile.LOCACAO,
             Depot("Matriz — Locação", -54.8060, -22.2210, "Rua Ponta Porã, 1343"))
st.put_depot(Profile.ENTREGA_POSTERIOR,
             Depot("Matriz — Depósito", -54.8120, -22.2260, "Av. Marcelino Pires"))

# Poliguindaste: 1 caçamba por vez -> capacidade 1, várias viagens no dia.
st.put_fleet(Profile.LOCACAO, [
    VehicleConfig(id="MB1513", label="MB 1513", placa="KTD-3645", capacity=1,
                  trips=8, shift_start_s=7 * 3600, shift_end_s=18 * 3600,
                  erp_id_veiculo=2),
    VehicleConfig(id="TRUCK2", label="Truck reserva", placa="—", capacity=1,
                  trips=6, shift_start_s=7 * 3600, shift_end_s=17 * 3600),
])

# Caminhões de material de construção: carga fracionada, 1 viagem longa.
st.put_fleet(Profile.ENTREGA_POSTERIOR, [
    VehicleConfig(id="AEY2862", label="CAM BRANCO", placa="AEY-2862", capacity=14,
                  trips=2, shift_start_s=7 * 3600, shift_end_s=18 * 3600,
                  erp_id_veiculo=5),
    VehicleConfig(id="HQY8879", label="F4000", placa="HQY-8879", capacity=10,
                  trips=2, shift_start_s=7 * 3600, shift_end_s=18 * 3600,
                  erp_id_veiculo=6),
    VehicleConfig(id="IGD9824", label="TRUCK", placa="IGD-9824", capacity=20,
                  trips=2, shift_start_s=7 * 3600, shift_end_s=18 * 3600,
                  erp_id_veiculo=21),
    VehicleConfig(id="HRY0575", label="CAM BRANCO W8150", placa="HRY-0575",
                  capacity=14, trips=2, shift_start_s=7 * 3600,
                  shift_end_s=18 * 3600, erp_id_veiculo=33),
    VehicleConfig(id="RJR3J05", label="SAVEIRO", placa="RJR-3J05", capacity=4,
                  trips=3, shift_start_s=8 * 3600, shift_end_s=17 * 3600,
                  erp_id_veiculo=40),
])

for p in (Profile.LOCACAO, Profile.ENTREGA_POSTERIOR):
    print(p.value, "->", st.get_depot(p).label,
          "|", len(st.get_fleet(p)), "veículos")
