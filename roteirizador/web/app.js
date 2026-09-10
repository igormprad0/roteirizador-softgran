const CORES = ["#4d9de0","#3ddc84","#f5b942","#e8663d","#b06fdb","#3dd6c4",
               "#e05c8a","#8fd44a"];
const $ = (id) => document.getElementById(id);

// Todo texto que vem do ERP (nome de cliente, endereço, observação, label
// de veículo) passa por aqui antes de entrar em innerHTML. É dado interno,
// mas é texto que ninguém validou: um `"` no meio de um label já escapava
// do atributo em `value="${v.label}"` e quebrava a linha da tabela de
// frota, e uma tag no meio de um nome injetaria HTML na tela.
function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g,
    (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}

const state = {stops: [], run: null, markers: new Map(), layers: [], pickupDropped: 0};

const map = L.map("map").setView([-22.2210, -54.8060], 13);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
  {maxZoom: 19, attribution: "© OpenStreetMap"}).addTo(map);

// Polyline5 do OSRM -> [[lat, lng], ...]
function decodePolyline(str) {
  let index = 0, lat = 0, lng = 0; const out = [];
  while (index < str.length) {
    let shift = 0, result = 0, b;
    do { b = str.charCodeAt(index++) - 63; result |= (b & 0x1f) << shift; shift += 5; }
    while (b >= 0x20);
    lat += (result & 1) ? ~(result >> 1) : (result >> 1);
    shift = 0; result = 0;
    do { b = str.charCodeAt(index++) - 63; result |= (b & 0x1f) << shift; shift += 5; }
    while (b >= 0x20);
    lng += (result & 1) ? ~(result >> 1) : (result >> 1);
    out.push([lat / 1e5, lng / 1e5]);
  }
  return out;
}

async function api(path, opts) {
  const r = await fetch(path, {headers: {"Content-Type": "application/json"}, ...opts});
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

const body = () => ({profile: $("profile").value, date: $("date").value,
                     mode: $("mode").value});

function clearLayers() {
  state.layers.forEach((l) => map.removeLayer(l));
  state.layers = [];
  state.markers.forEach((m) => map.removeLayer(m));
  state.markers.clear();
}

function pinIcon(color, texto) {
  return L.divIcon({className: "", iconSize: [22, 22], iconAnchor: [11, 11],
    html: `<div style="width:22px;height:22px;border-radius:50%;background:${color};
      border:2px solid #0f1720;color:#04121f;font:600 11px/18px system-ui;
      text-align:center">${texto}</div>`});
}

const CORDE = {high: "#3ddc84", medium: "#f5b942", low: "#e8663d", failed: "#c0392b"};

function drawStops(stops) {
  clearLayers();
  const pts = [];
  stops.forEach((s) => {
    if (s.lat == null) return;
    const m = L.marker([s.lat, s.lon], {
      draggable: true, icon: pinIcon(CORDE[s.confidence], s.kind === "pickup" ? "↑" : "↓"),
    }).addTo(map);
    m.bindPopup(`<b>${esc(s.cliente_nome)}</b><br>${esc(s.address)}<br>
      <small>${s.kind === "pickup" ? "Coleta" : "Entrega"} ·
      ${esc(s.confidence)} (${esc(s.source)})${s.days_overdue ? ` · ${s.days_overdue}d atraso` : ""}
      </small><br><small>arraste o pino para corrigir</small>`);
    m.on("dragend", async (e) => {
      const {lat, lng} = e.target.getLatLng();
      await api("/api/geocode/pin", {method: "POST",
        body: JSON.stringify({address_key: s.address_key, lon: lng, lat})});
      s.lat = lat; s.lon = lng; s.confidence = "high"; s.source = "manual";
      m.setIcon(pinIcon(CORDE.high, s.kind === "pickup" ? "↑" : "↓"));
      renderCounts();
    });
    state.markers.set(s.external_id, m);
    pts.push([s.lat, s.lon]);
  });
  if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.15));
}

function renderCounts() {
  const c = {total: state.stops.length, delivery: 0, pickup: 0,
             high: 0, medium: 0, low: 0, failed: 0};
  state.stops.forEach((s) => { c[s.kind]++; c[s.confidence]++; });
  let html = `
    <span class="chip">${c.total} paradas</span>
    <span class="chip">${c.delivery} entregas</span>
    <span class="chip">${c.pickup} coletas</span>
    <span class="chip high">${c.high} alta</span>
    <span class="chip medium">${c.medium} média</span>
    <span class="chip low">${c.low} baixa</span>
    <span class="chip failed">${c.failed} sem local</span>`;
  // Coletas vencidas que não couberam no teto do dia (locação) não podem
  // desaparecer do total mostrado — ver comentário em service.import_stops.
  if (state.pickupDropped > 0) {
    html += `<span class="chip warn">⚠ ${state.pickupDropped} coletas vencidas
      fora do teto do dia (não importadas)</span>`;
  }
  $("counts").innerHTML = html;
}

function renderStops() {
  $("stops").innerHTML = state.stops.map((s) => `
    <li data-id="${s.external_id}">
      <b><span class="dot ${esc(s.confidence)}"></span>${esc(s.cliente_nome)}</b>
      <small>${s.kind === "pickup" ? "COLETA" : "ENTREGA"} · ${esc(s.address || "sem endereço")}</small>
    </li>`).join("");
  $("stops").querySelectorAll("li").forEach((li) => li.onclick = () => {
    const m = state.markers.get(li.dataset.id);
    if (m) { map.setView(m.getLatLng(), 16); m.openPopup(); }
  });
}

$("btn-import").onclick = async () => {
  $("btn-import").disabled = true;
  try {
    const r = await api("/api/stops/import", {method: "POST", body: JSON.stringify(body())});
    state.stops = r.stops;
    state.pickupDropped = (r.counts && r.counts.pickup_dropped) || 0;
    renderCounts(); renderStops(); drawStops(r.stops);
    $("btn-optimize").disabled = r.stops.length === 0;
    $("panel").hidden = true;
  } catch (e) { alert("Falha ao importar: " + e.message); }
  finally { $("btn-import").disabled = false; }
};

$("btn-optimize").onclick = async () => {
  $("btn-optimize").disabled = true;
  $("btn-optimize").textContent = "Otimizando…";
  try {
    const run = await api("/api/optimize", {method: "POST",
      body: JSON.stringify({...body(), cost_per_km: 3.5})});
    state.run = run; state.stops = run.stops;
    state.pickupDropped = (run.counts && run.counts.pickup_dropped) || 0;
    renderCounts(); renderStops(); drawStops(run.stops);
    drawRoutes(run); renderPanel(run);
  } catch (e) { alert("Falha ao otimizar: " + e.message); }
  finally { $("btn-optimize").disabled = false; $("btn-optimize").textContent = "Otimizar"; }
};

function drawRoutes(run) {
  run.routes.forEach((r, i) => {
    const cor = CORES[i % CORES.length];
    if (r.geometry) {
      const l = L.polyline(decodePolyline(r.geometry),
        {color: cor, weight: 4, opacity: .85}).addTo(map);
      state.layers.push(l);
    }
    r.steps.forEach((st) => {
      const m = state.markers.get(st.stop_external_id);
      if (!m) return;
      m.setIcon(pinIcon(cor, String(st.seq)));
      // Deep link de navegação por parada (§6.3 do spec), vindo pronto do
      // servidor -- mesma função que o romaneio PDF usa, para não existirem
      // duas versões da inversão lat/lon.
      if (st.waze_url) {
        m.setPopupContent(m.getPopup().getContent() +
          `<br><a href="${esc(st.waze_url)}" target="_blank" rel="noopener">`
          + `navegar até aqui (Waze)</a>`);
      }
    });
  });
  const d = run.depot;
  state.layers.push(L.circleMarker([d.lat, d.lon],
    {radius: 9, color: "#fff", fillColor: "#111", fillOpacity: 1})
    .bindPopup(`<b>${d.label}</b><br>${d.address}`).addTo(map));
}

function renderPanel(run) {
  const c = run.comparison;
  const t = run.totals;
  const bom = c.km_saved > 0;
  const faltam = t.stops_unassigned > 0;
  // Cobertura com a MESMA frota, otimizado vs. despacho na ordem de
  // lançamento (sem poder reordenar) -- sempre mostrado, para os dois
  // perfis, lado a lado com o km. Para locação (capacidade 1) a rota já é
  // quase determinada pela física, então o km quase não muda -- o ganho
  // real é aqui: quantas paradas a mesma frota consegue cobrir no dia.
  const maisCobertura = c.optimized_stops > c.baseline_stops;
  $("kpis").innerHTML = `
    <div class="kpi ${faltam ? "warn" : "good"}"><span>Paradas atendidas</span>
      <strong>${t.stops_served} / ${t.stops_total}</strong></div>
    <div class="kpi ${maisCobertura ? "good" : ""}"><span>Paradas com a mesma frota</span>
      <strong>${c.optimized_stops} otimizado vs ${c.baseline_stops} ordem atual</strong></div>
    <div class="kpi"><span>Rota atual</span><strong>${c.baseline_km} km</strong></div>
    <div class="kpi"><span>Rota otimizada</span><strong>${c.optimized_km} km</strong></div>
    <div class="kpi ${bom ? "good" : ""}"><span>Economia</span>
      <strong>${c.km_saved} km (${c.percent_km_saved}%)</strong></div>
    <div class="kpi ${bom ? "good" : ""}"><span>Horas poupadas/dia</span>
      <strong>${c.hours_saved} h</strong></div>
    <div class="kpi ${bom ? "good" : ""}"><span>Economia mensal</span>
      <strong>R$ ${c.monthly_brl_saved.toLocaleString("pt-BR")}</strong></div>
    <div class="kpi"><span>Veículos/viagens</span>
      <strong>${t.vehicles_used}</strong></div>`;
  $("note").textContent = c.approximate ? `⚠ ${c.note}` : "";

  $("routes").innerHTML = run.routes.map((r, i) => `
    <div class="route-row">
      <span class="swatch" style="background:${CORES[i % CORES.length]}"></span>
      <b>${esc(r.label)}</b>
      <span>${r.steps.length} paradas · ${r.distance_km} km · ${r.duration_min} min</span>
      <a href="/api/runs/${run.run_id}/romaneio.pdf?vehicle=${encodeURIComponent(r.vehicle_id)}"
         target="_blank">romaneio PDF</a>
    </div>`).join("") +
    `<div class="route-row"><a href="/api/runs/${run.run_id}/export.csv">baixar CSV</a></div>`;

  const unEl = $("unassigned");
  if (run.unassigned.length) {
    unEl.classList.add("has-items");
    unEl.innerHTML = `⚠ <b>${run.unassigned.length} de ${t.stops_total} paradas não
      atendidas — não entraram em nenhuma rota:</b><br>` +
      run.unassigned.map((u) => `${esc(u.stop_external_id)} (${esc(u.reason)})`).join("; ");
  } else {
    unEl.classList.remove("has-items");
    unEl.innerHTML = "";
  }
  $("panel").hidden = false;
}

// ---- frota e depósito ------------------------------------------------------
const hhmm = (s) => `${String(Math.floor(s / 3600)).padStart(2, "0")}:` +
                    `${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}`;
const secs = (v) => { const [h, m] = v.split(":").map(Number); return h * 3600 + m * 60; };

$("btn-fleet").onclick = async () => {
  const profile = $("profile").value;
  const [f, d] = await Promise.all([
    api(`/api/fleet?profile=${profile}`), api(`/api/depot?profile=${profile}`)]);

  const dep = d.depot || d.suggested ||
    {label: "Depósito", lon: -54.8060, lat: -22.2210, address: ""};
  $("depot-box").innerHTML = `<p><b>Depósito:</b>
    <input id="dep-label" value="${esc(dep.label)}">
    <input id="dep-lon" type="number" step="0.0001" value="${dep.lon}">
    <input id="dep-lat" type="number" step="0.0001" value="${dep.lat}">
    ${d.depot ? "" : "<em>(sugerido pelo cadastro — confira)</em>"}</p>`;

  const salvos = new Map(f.fleet.map((v) => [String(v.erp_id_veiculo ?? v.id), v]));
  const linhas = f.suggested.map((s) => {
    const v = salvos.get(String(s.erp_id_veiculo)) || {};
    return {id: v.id || `V${s.erp_id_veiculo}`, label: v.label || s.label,
            placa: v.placa || s.placa, capacity: v.capacity ?? 10,
            trips: v.trips ?? 1, shift_start_s: v.shift_start_s ?? 25200,
            shift_end_s: v.shift_end_s ?? 64800, enabled: v.enabled ?? false,
            erp_id_veiculo: s.erp_id_veiculo};
  });
  $("fleet-table").querySelector("tbody").innerHTML = linhas.map((v, i) => `
    <tr data-i="${i}">
      <td><input type="checkbox" class="f-on" ${v.enabled ? "checked" : ""}></td>
      <td><input class="f-label" value="${esc(v.label)}"></td>
      <td>${esc(v.placa || "")}</td>
      <td><input type="number" class="f-cap" min="1" value="${v.capacity}"></td>
      <td><input type="number" class="f-trips" min="1" max="20" value="${v.trips}"></td>
      <td><input type="time" class="f-ini" value="${hhmm(v.shift_start_s)}"></td>
      <td><input type="time" class="f-fim" value="${hhmm(v.shift_end_s)}"></td>
    </tr>`).join("");
  $("fleet-dialog").dataset.base = JSON.stringify(linhas);
  $("fleet-dialog").showModal();
};

$("fleet-cancel").onclick = () => $("fleet-dialog").close();

$("fleet-save").onclick = async () => {
  const profile = $("profile").value;
  const base = JSON.parse($("fleet-dialog").dataset.base);
  const fleet = [...$("fleet-table").querySelectorAll("tbody tr")].map((tr) => {
    const v = base[+tr.dataset.i], q = (c) => tr.querySelector(c);
    return {...v, enabled: q(".f-on").checked, label: q(".f-label").value,
            capacity: +q(".f-cap").value, trips: +q(".f-trips").value,
            shift_start_s: secs(q(".f-ini").value), shift_end_s: secs(q(".f-fim").value)};
  }).filter((v) => v.enabled);

  await api("/api/depot", {method: "PUT", body: JSON.stringify({profile, depot: {
    label: $("dep-label").value, lon: +$("dep-lon").value,
    lat: +$("dep-lat").value, address: ""}})});
  await api("/api/fleet", {method: "PUT", body: JSON.stringify({profile, fleet})});
  $("fleet-dialog").close();
};

// ---- bootstrap -------------------------------------------------------------
(async () => {
  const ps = await api("/api/profiles");
  $("profile").innerHTML = ps.map((p) =>
    `<option value="${esc(p.profile)}">${esc(p.label)}</option>`).join("");
})();
