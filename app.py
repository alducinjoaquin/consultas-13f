"""
Consultas 13F
App de Streamlit para revisar los 13F de cualquier inversionista o fondo
directamente desde SEC EDGAR, ver sus cambios contra el trimestre anterior
y encontrar las acciones que se repiten entre varios portafolios.
"""

import re
import time
import xml.etree.ElementTree as ET

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

st.set_page_config(page_title="Consultas 13F", page_icon="📊", layout="wide")

# ---------------------------------------------------------------------------
# Configuración SEC
# ---------------------------------------------------------------------------
# La SEC exige un User-Agent con nombre y correo de contacto.
# Se define en Streamlit Cloud > Settings > Secrets:
#   SEC_USER_AGENT = "Tu Nombre tu_correo@dominio.com"
try:
    UA = st.secrets["SEC_USER_AGENT"]
except Exception:
    st.error(
        "Falta configurar SEC_USER_AGENT en Settings > Secrets de Streamlit Cloud. "
        'Ejemplo: SEC_USER_AGENT = "Tu Nombre tu_correo@dominio.com"'
    )
    st.stop()

HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}

# Inversionistas sugeridos (nombre visible -> CIK del fondo que presenta el 13F)
SUGERIDOS = {
    "Warren Buffett · Berkshire Hathaway": "0001067983",
    "Bill Ackman · Pershing Square": "0001336528",
    "Peter Thiel · Thiel Macro": "0001562087",
    "Dan Loeb · Third Point": "0001040273",
    "Bill Gates · Gates Foundation Trust": "0001166559",
    "Stanley Druckenmiller · Duquesne Family Office": "0001536411",
    "Ken Griffin · Citadel Advisors": "0001423053",
    "Jim Simons · Renaissance Technologies": "0001037389",
    "Michael Burry · Scion Asset Management": "0001649339",
    "David Tepper · Appaloosa": "0001656456",
    "Cathie Wood · ARK Investment": "0001697748",
    "Ray Dalio · Bridgewater": "0001350694",
    "Carl Icahn": "0000921669",
    "Seth Klarman · Baupost": "0001061768",
    "Li Lu · Himalaya Capital": "0001709323",
}


def sec_get(url, params=None):
    """GET a la SEC respetando su límite de 10 solicitudes por segundo."""
    time.sleep(0.15)
    r = requests.get(url, headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r


def cik10(cik):
    return str(int(str(cik).strip())).zfill(10)


# ---------------------------------------------------------------------------
# Datos EDGAR
# ---------------------------------------------------------------------------
@st.cache_data(ttl=24 * 3600, show_spinner=False)
def buscar_fondos(texto):
    """Busca fondos por nombre en EDGAR. Regresa lista de (nombre, cik)."""
    r = sec_get(
        "https://www.sec.gov/cgi-bin/browse-edgar",
        params={
            "action": "getcompany",
            "company": texto,
            "type": "13F-HR",
            "owner": "include",
            "count": "40",
        },
    )
    html = r.text
    resultados = []

    # Caso 1: varios resultados (tabla de compañías)
    for cik, nombre in re.findall(
        r'CIK=(\d{10})[^"]*"[^>]*>\d{10}</a>\s*</td>\s*<td[^>]*>(.*?)</td>',
        html,
        flags=re.S,
    ):
        nombre = re.split(r"<br", nombre, flags=re.I)[0]
        nombre = re.sub(r"<.*?>", " ", nombre)
        nombre = re.sub(r"\s+", " ", nombre).strip()
        resultados.append((nombre, cik))

    # Caso 2: un solo resultado (EDGAR abre directo la página del fondo)
    if not resultados:
        m_nombre = re.search(r'class="companyName">\s*([^<]+)', html)
        m_cik = re.search(r"CIK=(\d{10})", html)
        if m_nombre and m_cik:
            resultados.append((m_nombre.group(1).strip(), m_cik.group(1)))

    vistos, unicos = set(), []
    for nombre, cik in resultados:
        if cik not in vistos:
            vistos.add(cik)
            unicos.append((nombre, cik))
    return unicos


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def filings_13f(cik):
    """Lista de 13F-HR del fondo, uno por trimestre, del más reciente al más viejo."""
    data = sec_get(f"https://data.sec.gov/submissions/CIK{cik10(cik)}.json").json()
    nombre = data.get("name", cik)
    rec = pd.DataFrame(data["filings"]["recent"])
    df = rec[rec["form"] == "13F-HR"][["accessionNumber", "filingDate", "reportDate"]]
    df = df.sort_values("filingDate", ascending=False)
    df = df.drop_duplicates("reportDate", keep="first").reset_index(drop=True)
    return nombre, df


def _hijo(el, nombre):
    for c in el.iter():
        if c.tag.split("}")[-1] == nombre:
            return (c.text or "").strip()
    return ""


def parsear_info_table(contenido, fecha_presentacion):
    root = ET.fromstring(contenido)
    filas = []
    for it in root.iter():
        if it.tag.split("}")[-1] != "infoTable":
            continue
        filas.append(
            {
                "emisor": _hijo(it, "nameOfIssuer"),
                "clase": _hijo(it, "titleOfClass"),
                "cusip": _hijo(it, "cusip").upper(),
                "valor": float(_hijo(it, "value") or 0),
                "acciones": float(_hijo(it, "sshPrnamt") or 0),
                "opcion": _hijo(it, "putCall").capitalize(),
            }
        )
    if not filas:
        return None
    df = pd.DataFrame(filas)
    # Antes del 3 de enero de 2023 el valor venía en miles de dólares
    if fecha_presentacion < "2023-01-03":
        df["valor"] *= 1000
    df = df.groupby(["cusip", "opcion"], as_index=False).agg(
        emisor=("emisor", "first"),
        clase=("clase", "first"),
        valor=("valor", "sum"),
        acciones=("acciones", "sum"),
    )
    return df


@st.cache_data(ttl=7 * 24 * 3600, show_spinner=False)
def posiciones(cik, accession, fecha_presentacion):
    """Descarga y procesa la tabla de posiciones (information table) de un 13F."""
    acc = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/"
    items = sec_get(base + "index.json").json()["directory"]["item"]
    xmls = [i["name"] for i in items if i["name"].lower().endswith(".xml")]
    xmls.sort(key=lambda n: "primary_doc" in n.lower())  # primary_doc al final
    for nombre in xmls:
        try:
            df = parsear_info_table(sec_get(base + nombre).content, fecha_presentacion)
        except ET.ParseError:
            continue
        if df is not None:
            return df
    return pd.DataFrame(columns=["cusip", "opcion", "emisor", "clase", "valor", "acciones"])


@st.cache_data(ttl=30 * 24 * 3600, show_spinner=False)
def tickers_openfigi(cusips):
    """Convierte CUSIP a ticker con OpenFIGI (gratis, sin llave, ~25 consultas/min)."""
    mapa = {}
    lista = list(cusips)
    for i in range(0, len(lista), 10):
        lote = lista[i:i + 10]
        try:
            r = requests.post(
                "https://api.openfigi.com/v3/mapping",
                json=[{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in lote],
                timeout=30,
            )
            if r.status_code == 429:
                time.sleep(6)
                continue
            for c, res in zip(lote, r.json()):
                datos = res.get("data") or []
                if datos:
                    mapa[c] = datos[0].get("ticker", "")
        except Exception:
            pass
        if i + 10 < len(lista):
            time.sleep(2.5)
    return mapa


def agregar_tickers(df, columna_orden="valor", top=40):
    df = df.copy()
    if df.empty:
        df["ticker"] = ""
        return df
    principales = tuple(
        df.sort_values(columna_orden, ascending=False)["cusip"].drop_duplicates().head(top)
    )
    mapa = tickers_openfigi(principales)
    df["ticker"] = df["cusip"].map(mapa).fillna("")
    return df


# ---------------------------------------------------------------------------
# Lógica de análisis
# ---------------------------------------------------------------------------
def con_pesos(df, incluir_opciones):
    df = df.copy()
    if not incluir_opciones:
        df = df[df["opcion"] == ""]
    total = df["valor"].sum()
    df["peso"] = df["valor"] / total * 100 if total else 0.0
    return df


def estatus(f):
    if f["acciones_ant"] == 0:
        return "Nueva"
    if f["acciones"] == 0:
        return "Vendida"
    if f["acciones"] > f["acciones_ant"]:
        return "Aumentó"
    if f["acciones"] < f["acciones_ant"]:
        return "Redujo"
    return "Sin cambio"


def cambios(actual, anterior):
    llave = ["cusip", "opcion"]
    ant = anterior[llave + ["acciones", "valor", "emisor", "clase"]].rename(
        columns={"acciones": "acciones_ant", "valor": "valor_ant",
                 "emisor": "emisor_ant", "clase": "clase_ant"}
    )
    m = actual.merge(ant, on=llave, how="outer")
    m["emisor"] = m["emisor"].fillna(m["emisor_ant"])
    m["clase"] = m["clase"].fillna(m["clase_ant"])
    for c in ["acciones", "acciones_ant", "valor", "valor_ant", "peso"]:
        m[c] = m[c].fillna(0)
    m["estatus"] = m.apply(estatus, axis=1) if len(m) else pd.Series(dtype=str)
    m["cambio_%"] = (
        (m["acciones"] - m["acciones_ant"]) / m["acciones_ant"].where(m["acciones_ant"] > 0) * 100
    )
    return m.drop(columns=["emisor_ant", "clase_ant"])


def coincidencias(largo, nombres, minimo):
    piv = largo.pivot_table(index="cusip", columns="inversionista", values="peso", aggfunc="sum")
    cols = [n for n in nombres if n in piv.columns]
    piv = piv[cols]
    res = pd.DataFrame(index=piv.index)
    res["emisor"] = largo.groupby("cusip")["emisor"].first()
    res["num_inversionistas"] = piv.notna().sum(axis=1)
    res["peso_promedio"] = piv.mean(axis=1)
    res = res.join(piv)
    res = res[res["num_inversionistas"] >= minimo]
    res = res.sort_values(["num_inversionistas", "peso_promedio"], ascending=False)
    return res.reset_index(), cols


# ---------------------------------------------------------------------------
# Lista abierta de inversionistas (se guarda en la URL para no perderla)
# ---------------------------------------------------------------------------
if "lista" not in st.session_state:
    st.session_state.lista = {}
    guardados = st.query_params.get("ciks", "")
    for c in [x for x in guardados.split(",") if x.strip().isdigit()]:
        try:
            nombre, _ = filings_13f(c)
            st.session_state.lista[cik10(c)] = nombre
        except Exception:
            pass


def guardar_lista():
    st.query_params["ciks"] = ",".join(st.session_state.lista.keys())


def agregar(cik, nombre=None):
    cik = cik10(cik)
    if nombre is None:
        nombre, _ = filings_13f(cik)
    st.session_state.lista[cik] = nombre
    guardar_lista()


# ---------------------------------------------------------------------------
# Barra lateral
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Mi lista")

    st.subheader("Buscar fondo")
    texto = st.text_input("Nombre del fondo", placeholder="ej. Pershing, Duquesne, Baupost")
    if texto:
        with st.spinner("Buscando en EDGAR..."):
            try:
                encontrados = buscar_fondos(texto)
            except Exception as e:
                encontrados = []
                st.error(f"No pude consultar EDGAR: {e}")
        if encontrados:
            opciones = {f"{n} ({c})": (n, c) for n, c in encontrados}
            elegido_b = st.selectbox("Resultados", list(opciones))
            if st.button("Agregar a mi lista", width="stretch"):
                n, c = opciones[elegido_b]
                agregar(c, n)
                st.rerun()
        else:
            st.caption("Sin resultados. Prueba con el nombre del fondo, no de la persona.")

    st.subheader("Sugeridos")
    sug = st.selectbox("Inversionista", ["—"] + list(SUGERIDOS))
    if sug != "—" and st.button("Agregar sugerido", width="stretch"):
        try:
            agregar(SUGERIDOS[sug])
            st.rerun()
        except Exception as e:
            st.error(f"No pude agregarlo: {e}")

    with st.expander("Agregar por CIK"):
        cik_manual = st.text_input("CIK", placeholder="0001336528")
        if st.button("Agregar CIK") and cik_manual.strip().isdigit():
            try:
                agregar(cik_manual)
                st.rerun()
            except Exception as e:
                st.error(f"CIK no válido o sin 13F: {e}")

    st.divider()
    if st.session_state.lista:
        for c, n in list(st.session_state.lista.items()):
            col1, col2 = st.columns([5, 1])
            col1.write(n)
            if col2.button("✕", key=f"del_{c}"):
                del st.session_state.lista[c]
                guardar_lista()
                st.rerun()
        st.caption("Guarda esta página en favoritos: la lista queda en la dirección.")
    else:
        st.caption("Tu lista está vacía.")


# ---------------------------------------------------------------------------
# Pantalla principal
# ---------------------------------------------------------------------------
st.title("Consultas 13F")

if not st.session_state.lista:
    st.info("Agrega inversionistas desde el menú lateral (flecha arriba a la izquierda).")
    st.stop()

tab_port, tab_coin = st.tabs(["Portafolio", "Coincidencias"])

# ----------------------------- Portafolio ---------------------------------
with tab_port:
    nombres = {n: c for c, n in st.session_state.lista.items()}
    elegido = st.selectbox("Inversionista", list(nombres))
    cik = nombres[elegido]

    try:
        _, fil = filings_13f(cik)
    except Exception as e:
        fil = pd.DataFrame()
        st.error(f"No pude leer los filings: {e}")

    if fil.empty:
        st.warning("Este fondo no tiene 13F-HR recientes.")
    else:
        periodo = st.selectbox("Trimestre (fecha de corte)", fil["reportDate"].tolist())
        i = int(fil.index[fil["reportDate"] == periodo][0])
        f_act = fil.loc[i]
        incluir_op = st.toggle("Incluir opciones (calls/puts)", value=False)

        with st.spinner("Descargando posiciones..."):
            act = con_pesos(posiciones(cik, f_act["accessionNumber"], f_act["filingDate"]), incluir_op)
            if i + 1 < len(fil):
                f_ant = fil.loc[i + 1]
                ant = con_pesos(
                    posiciones(cik, f_ant["accessionNumber"], f_ant["filingDate"]), incluir_op
                )
            else:
                ant = act.iloc[0:0]
            tabla = agregar_tickers(cambios(act, ant))

        vivas = tabla[tabla["estatus"] != "Vendida"].sort_values("valor", ascending=False)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Valor reportado (USD)", f"{vivas['valor'].sum() / 1e9:,.2f} mil mill.")
        c2.metric("Posiciones", f"{len(vivas):,}")
        c3.metric("Nuevas", int((tabla["estatus"] == "Nueva").sum()))
        c4.metric("Vendidas", int((tabla["estatus"] == "Vendida").sum()))
        st.caption(f"Presentado el {f_act['filingDate']} · corte {f_act['reportDate']}")

        top = vivas.head(15).copy()
        top["etiqueta"] = top["ticker"].where(top["ticker"] != "", top["emisor"])
        fig = px.bar(
            top.iloc[::-1], x="peso", y="etiqueta", orientation="h",
            labels={"peso": "Peso en el portafolio (%)", "etiqueta": ""},
            title="Principales posiciones",
        )
        fig.update_layout(height=480, margin=dict(l=10, r=10, t=50, b=10))
        st.plotly_chart(fig, width="stretch")

        movs = ["Nueva", "Aumentó", "Redujo", "Sin cambio"]
        filtro = st.multiselect("Filtrar por movimiento", movs, default=movs)
        mostrar = vivas[vivas["estatus"].isin(filtro)].copy()
        mostrar["valor_mm"] = mostrar["valor"] / 1e6
        st.dataframe(
            mostrar[["ticker", "emisor", "clase", "opcion", "acciones", "valor_mm",
                     "peso", "estatus", "cambio_%"]],
            hide_index=True,
            width="stretch",
            column_config={
                "ticker": "Ticker",
                "emisor": "Emisor",
                "clase": "Clase",
                "opcion": "Opción",
                "acciones": st.column_config.NumberColumn("Acciones", format="%d"),
                "valor_mm": st.column_config.NumberColumn("Valor (USD mm)", format="%.1f"),
                "peso": st.column_config.NumberColumn("Peso %", format="%.2f"),
                "estatus": "Movimiento",
                "cambio_%": st.column_config.NumberColumn("Cambio acciones %", format="%.1f"),
            },
        )

        salidas = tabla[tabla["estatus"] == "Vendida"].sort_values("valor_ant", ascending=False)
        with st.expander(f"Salidas del trimestre ({len(salidas)})"):
            salidas = salidas.assign(valor_ant_mm=salidas["valor_ant"] / 1e6)
            st.dataframe(
                salidas[["ticker", "emisor", "clase", "acciones_ant", "valor_ant_mm"]],
                hide_index=True,
                width="stretch",
                column_config={
                    "ticker": "Ticker",
                    "emisor": "Emisor",
                    "clase": "Clase",
                    "acciones_ant": st.column_config.NumberColumn("Acciones previas", format="%d"),
                    "valor_ant_mm": st.column_config.NumberColumn(
                        "Valor previo (USD mm)", format="%.1f"),
                },
            )

        st.download_button(
            "Descargar CSV",
            tabla.to_csv(index=False).encode("utf-8"),
            file_name=f"13F_{cik}_{periodo}.csv",
            mime="text/csv",
        )

# ----------------------------- Coincidencias ------------------------------
with tab_coin:
    todos = {n: c for c, n in st.session_state.lista.items()}
    sel = st.multiselect("Inversionistas a comparar", list(todos), default=list(todos))

    if len(sel) < 2:
        st.info("Elige al menos dos inversionistas.")
    else:
        if len(sel) > 2:
            minimo = st.slider("Mínimo de inversionistas que la tienen", 2, len(sel), 2)
        else:
            minimo = 2
        largos = []
        progreso = st.progress(0.0, text="Descargando portafolios...")
        for k, n in enumerate(sel):
            c = todos[n]
            try:
                _, fil_c = filings_13f(c)
                if not fil_c.empty:
                    f = fil_c.iloc[0]
                    df = con_pesos(posiciones(c, f["accessionNumber"], f["filingDate"]), False)
                    df = df.groupby("cusip", as_index=False).agg(
                        emisor=("emisor", "first"), peso=("peso", "sum"))
                    df["inversionista"] = n
                    df["corte"] = f["reportDate"]
                    largos.append(df)
            except Exception as e:
                st.warning(f"No pude leer {n}: {e}")
            progreso.progress((k + 1) / len(sel), text=f"Listo: {n}")
        progreso.empty()

        if largos:
            largo = pd.concat(largos, ignore_index=True)
            cortes = largo.groupby("inversionista")["corte"].first()
            if cortes.nunique() > 1:
                st.caption("Ojo: no todos tienen el mismo trimestre de corte. "
                           + " · ".join(f"{k}: {v}" for k, v in cortes.items()))

            res, cols_peso = coincidencias(largo, sel, minimo)
            res = agregar_tickers(res, columna_orden="peso_promedio")

            st.subheader(f"{len(res)} acciones en común")
            config = {
                "ticker": "Ticker",
                "emisor": "Emisor",
                "num_inversionistas": st.column_config.NumberColumn("# Inversionistas", format="%d"),
                "peso_promedio": st.column_config.NumberColumn("Peso promedio %", format="%.2f"),
            }
            for s in cols_peso:
                config[s] = st.column_config.NumberColumn(s[:28] + " %", format="%.2f")
            st.dataframe(
                res[["ticker", "emisor", "num_inversionistas", "peso_promedio"] + cols_peso],
                hide_index=True, width="stretch", column_config=config,
            )

            if not res.empty:
                mapa = res.head(20).copy()
                mapa["etiqueta"] = mapa["ticker"].where(mapa["ticker"] != "", mapa["emisor"])
                heat = mapa.set_index("etiqueta")[cols_peso]
                fig2 = px.imshow(
                    heat, text_auto=".1f", aspect="auto", color_continuous_scale="Blues",
                    labels={"color": "Peso %", "x": "", "y": ""},
                    title="Peso de cada acción en cada portafolio (top 20)",
                )
                fig2.update_layout(height=40 * len(heat) + 160,
                                   margin=dict(l=10, r=10, t=50, b=10))
                st.plotly_chart(fig2, width="stretch")

            st.download_button(
                "Descargar coincidencias CSV",
                res.to_csv(index=False).encode("utf-8"),
                file_name="13F_coincidencias.csv",
                mime="text/csv",
            )

st.caption("Fuente: SEC EDGAR (13F-HR). Tickers vía OpenFIGI. El 13F solo muestra posiciones "
           "largas en valores 13(f) y se publica hasta 45 días después del cierre del trimestre.")
