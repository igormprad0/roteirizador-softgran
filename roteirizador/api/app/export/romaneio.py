from __future__ import annotations
import csv
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from ..models import Depot, Stop, VehicleRoute
from .maps_link import google_maps_link

_TIPO = {"delivery": "Entrega", "pickup": "Coleta"}
_FONTE = "DejaVuSans"

try:
    pdfmetrics.registerFont(
        TTFont(_FONTE, "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
except Exception:                                     # pragma: no cover
    _FONTE = "Helvetica"


def _hhmm(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}"


def build_csv(routes: list[VehicleRoute], stops: list[Stop]) -> str:
    por_id = {s.external_id: s for s in stops}
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow(["veiculo", "seq", "chegada", "tipo", "documento", "cliente",
                "endereco", "bairro", "cidade", "observacao", "lat", "lon"])
    for r in routes:
        for s in sorted(r.steps, key=lambda x: x.seq):
            st = por_id.get(s.stop_external_id)
            a = st.address if st else None
            w.writerow([
                r.label, s.seq, _hhmm(s.arrival_s), _TIPO.get(s.kind, s.kind),
                (st.doc if st else "") or "", st.cliente_nome if st else "",
                (a.logradouro if a else "") or "", (a.bairro if a else "") or "",
                (a.cidade if a else "") or "", (st.notes if st else "") or "",
                f"{s.lat:.6f}", f"{s.lon:.6f}",
            ])
    return buf.getvalue()


def build_romaneio_pdf(route: VehicleRoute, stops: list[Stop], depot: Depot,
                       profile_label: str, target_date: str) -> bytes:
    por_id = {s.external_id: s for s in stops}
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=15 * mm, bottomMargin=15 * mm,
                            leftMargin=12 * mm, rightMargin=12 * mm,
                            title=f"Romaneio {route.label}")
    styles = getSampleStyleSheet()
    for name in ("Normal", "Title", "Heading2"):
        styles[name].fontName = _FONTE
    celula = styles["Normal"].clone("celula")
    celula.fontSize = 8
    celula.leading = 10

    story = [
        Paragraph(f"Romaneio de rota — {route.label}", styles["Title"]),
        Paragraph(f"{profile_label} · {target_date} · saída de {depot.label} · "
                  f"{len(route.steps)} paradas · {route.distance_m / 1000:.1f} km · "
                  f"{route.duration_s // 3600}h{(route.duration_s % 3600) // 60:02d}",
                  styles["Normal"]),
        Spacer(1, 6 * mm),
    ]

    dados = [["#", "Hora", "Tipo", "Doc", "Cliente", "Endereço", "Obs."]]
    for s in sorted(route.steps, key=lambda x: x.seq):
        st = por_id.get(s.stop_external_id)
        a = st.address if st else None
        endereco = ", ".join(p for p in [
            (a.logradouro if a else None), (a.bairro if a else None),
            (a.cidade if a else None)] if p)
        dados.append([
            str(s.seq), _hhmm(s.arrival_s), _TIPO.get(s.kind, s.kind),
            Paragraph((st.doc if st else "") or "", celula),
            Paragraph(st.cliente_nome if st else s.stop_external_id, celula),
            Paragraph(endereco, celula),
            Paragraph((st.notes if st else "") or "", celula),
        ])

    tabela = Table(dados, repeatRows=1,
                   colWidths=[8 * mm, 14 * mm, 16 * mm, 18 * mm, 45 * mm, 60 * mm, 25 * mm])
    tabela.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), _FONTE),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3a5f")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b0b8c4")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f2f5f9")]),
    ]))
    story.append(tabela)
    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph(
        f'<link href="{google_maps_link(route, depot)}">Abrir rota no Google Maps</link>',
        styles["Normal"]))

    doc.build(story)
    return buf.getvalue()
