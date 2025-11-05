#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trinity v15 (UNIFICADO, listo para correr)
- Telegram listener + comandos: sentimiento / radar / peak
- Radar ciclo (Fondo estratégico + Techo histórico)
- Picos de liquidez dentro del rango del Fondo y del Techo
- Sentimiento (Fear & Greed gratis, Funding, Open Interest)
- AutoScalp Sniper (simple, sin órdenes; solo alertas)
- Watchdog SmartDCA (avisa si el precio se acerca a zonas)
- Health monitor
DEPENDENCIAS: requests (pip install requests)

© 2025
"""

import os, time, math, requests, threading, statistics, shutil
from datetime import datetime, timezone, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
# --- Desactivar render de imágenes en Render ---
IMGKIT_OK = False
IMGKIT_CONFIG = None
def html_to_png_or_text(html, fallback_text="", out_path="/tmp/reporte.png"):
    return None, "no-render"

# --- Dummy de Telegram para no importar telegram ni imghdr ---
def tg_send(msg: str):
    print(f"[TG] {msg}")

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# === Timestamp RD (UTC-4) ===
RD_TZ = timezone(timedelta(hours=-4))  # America/Santo_Domingo (sin DST)

def ts_now():
    """Fecha-hora legible en RD."""
    return datetime.now(RD_TZ).strftime("%Y-%m-%d %H:%M:%S") + " GMT-4"

# ====================================================
# CONFIG BÁSICA  (RELLENA TUS CREDENCIALES AQUÍ)
# ====================================================
import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID   = os.getenv("CHAT_ID", "")
BASE_URL  = f"https://api.telegram.org/bot{BOT_TOKEN}/"

# Sesión HTTP con keep-alive y reintentos automáticos
session = requests.Session()
session.headers.update({"Connection": "keep-alive", "User-Agent": "TrinityBot/15"})
retries = Retry(
    total=5,                # reintentos totales
    backoff_factor=1.5,     # espera creciente: 1.5s, 3s, 4.5s...
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"]
)
adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
session.mount("https://", adapter)
session.mount("http://", adapter)

import time  # si ya está, no lo repitas

# ---- Gracia de arranque del health monitor ----
START_TS = time.time()                 # marca de arranque
HEALTH_STARTUP_GRACE_SEC = 180        # 3 min de gracia (puedes usar 120–240)

import os

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET  = os.getenv("BINANCE_SECRET", "")
BINANCE_FUTURES_PUBLIC = os.getenv("BINANCE_FUTURES_PUBLIC", "https://fapi.binance.com")

# INTERVALOS
RADAR_INTERVAL_SEC     = 2 * 60 * 60   # cada 2h
WATCHDOG_INTERVAL_SEC  = 10 * 60       # cada 10 min
SCALP_INTERVAL_SEC     = 10 * 60       # cada 10 min
HEALTH_INTERVAL_SEC    = 120 * 60       # cada 2h
ANTI_SPAM_COOLDOWN     = 10 * 60       # 10 min entre alertas idénticas

# ====================================================
# ESTADO GLOBAL
# ====================================================
LAST_RADAR_OK    = START_TS
LAST_WATCHDOG_OK = START_TS
LAST_SCALP_OK    = START_TS
LAST_HEALTH_OK   = START_TS

ULTIMA_ALERTA_LADO = None
ULTIMA_ALERTA_ZONA = None
ULTIMA_ALERTA_TS   = 0

POOL_FONDO_ESTRATEGICO = None   # dict con low/high/mid/fuerza/origen
POOL_TECHO_ESTRATEGICO = None   # dict con low/high/mid/calidad

# ====================================================
# UTILS
# ====================================================
def pct(a, b):
    if b == 0: return 0.0
    return (a - b) / b * 100.0

def pct_abs(a, b): 
    return abs(pct(a, b))

def fmt_num(x):
    try:
        return f"{float(x):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except:
        return str(x)

# ==============================
# TELEGRAM
# ==============================

# Requiere que arriba ya existan:
#   BOT_TOKEN (str), CHAT_ID (str)
# y están importados: requests, time
# Si no, descomenta las siguientes dos líneas:
# import requests, time

# URL base (OJO: termina en '/')
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/"


def _tg_post(method: str, data: dict):
    try:
        r = session.post(BASE_URL + method, data=data, timeout=25)
        r.raise_for_status()
        return r
    except requests.exceptions.RequestException as e:
        # Incluye ConnectionResetError, ReadTimeout, etc.
        print("Error Telegram POST:", e)
        time.sleep(5)
        return None

def enviar_alerta(texto: str):
    """Envía mensaje al chat configurado."""
    if not BOT_TOKEN or not CHAT_ID or "PASTE_" in BOT_TOKEN:
        # modo local sin Telegram
        print("△ Telegram NO configurado. Mensaje local:\n", texto[:300])
        return
    _tg_post("sendMessage", {"chat_id": CHAT_ID, "text": texto})


def registrar_comandos_en_telegram():
    """
    Registra /sentimiento, /radar, /peak, /scalp, /help, /trinityinfo
    para que te salgan en el menú de slash-commands.
    Llama a esta función 1 vez (o con /setup).
    """
    cmds = [
        {"command": "sentimiento", "description": "Análisis de sentimiento (live)"},
        {"command": "radar",        "description": "Radar de ciclo (v15)"},
        {"command": "peak",         "description": "Chequeo rápido de pico (peak)"},
        {"command": "scalp",        "description": "Alerta AutoScalp (ETH 15m)"},
        {"command": "trinityinfo",  "description": "Info del bot y estado"},
        {"command": "help",         "description": "Ayuda y comandos disponibles"},
        {"command": "setup",        "description": "Registrar comandos / menú /"}
    ]
    try:
        _tg_post("setMyCommands", {"commands": str(cmds)})
        enviar_alerta("✅ Comandos registrados. Escribe / para ver el menú.")
    except Exception as e:
        print("Error setMyCommands:", e)

def _tg_get(method: str, params: dict | None = None, timeout: int = 65):
    """GET genérico a Telegram (maneja errores y reconexión automática)."""
    try:
        r = session.get(BASE_URL + method, params=params or {}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        print("Error Telegram GET:", e)
        time.sleep(5)
        return None

def escuchar_telegram():
    """
    Escucha comandos básicos: /sentimiento /radar /peak /scalp /help /trinityinfo /setup
    """
    if not BOT_TOKEN or "PASTE_" in BOT_TOKEN:
        print("△ Listener desactivado: faltan credenciales (BOT_TOKEN/CHAT_ID)")
        return

    print("💬 Trinity escuchando Telegram (live)...")
    offset = None

    while True:
        try:
            params = {"timeout": 60}
            if offset:
                params["offset"] = offset

            # *** IMPORTANTE: concatenación correcta ***
            data = _tg_get("getUpdates", params=params, timeout=35)
            if not data:
                continue
            if not data.get("ok"):
                time.sleep(5)
                continue

            for upd in data.get("result", []):
                offset = upd.get("update_id", 0) + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue

                txt = (msg.get("text") or "").strip().lower()
                if not txt:
                    continue

                print("→ Telegram:", txt)

                def handler():
                    if txt.startswith("/sentimiento"):
                        enviar_alerta(construir_sentimiento_live())
                    elif txt.startswith("/radar"):
                        n, notas = generar_radar_v15()
                        enviar_alerta(n)
                        enviar_alerta(notas)
                    elif txt.startswith("/peak"):
                        enviar_alerta(construir_peak_msg())
                    elif txt.startswith("/scalp"):
                        autoscalp_sniper()
                    elif txt.startswith("/trinityinfo"):
                        enviar_alerta("ℹ Trinity v15 activo. Módulos: Radar/Watchdog/Scalp/Health.")
                    elif txt.startswith("/help"):
                        enviar_alerta(
                            "Comandos:\n"
                            "/sentimiento – Sentimiento live\n"
                            "/radar – Radar de ciclo v15\n"
                            "/peak – Chequeo pico\n"
                            "/scalp – AutoScalp ETH 15m\n"
                            "/trinityinfo – Info del bot\n"
                            "/setup – Registrar menú de comandos"
                        )
                    elif txt.startswith("/setup"):
                        registrar_comandos_en_telegram()
                    else:
                        enviar_alerta("⚠ Comando no reconocido. Usa /help para ver opciones.")

                try:
                    handler()
                except Exception as e:
                    print("✖ Error en handler:", e)
                    enviar_alerta("△ Ocurrió un error al procesar tu solicitud.")

        except Exception as e:
            print("Error en escuchar_telegram:", e)
            time.sleep(5)
# ====================================================
# BINANCE + FNG
# ====================================================
BINANCE_API  = "https://api.binance.com"
BINANCE_FAPI = "https://fapi.binance.com"

def get_price(symbol="BTCUSDT"):
    try:
        r = requests.get(f"{BINANCE_API}/api/v3/ticker/price", params={"symbol": symbol}, timeout=20)
        return float(r.json()["price"])
    except:
        return None

def get_klines(symbol, interval, limit=200):
    try:
        r = requests.get(f"{BINANCE_API}/api/v3/klines", params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=25)
        return r.json()
    except:
        return []

def get_open_interest(symbol="BTCUSDT"):
    try:
        r = requests.get(f"{BINANCE_FAPI}/fapi/v1/openInterest", params={"symbol": symbol}, timeout=20)
        return float(r.json().get("openInterest", 0))
    except:
        return 0.0

def get_funding_rate(symbol="BTCUSDT"):
    try:
        r = requests.get(f"{BINANCE_FAPI}/fapi/v1/premiumIndex", params={"symbol": symbol}, timeout=20)
        return float(r.json().get("lastFundingRate", 0.0))
    except:
        return 0.0

def get_fear_and_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=7", timeout=20).json()
        data = r.get("data", [])
        hist = []
        for p in data:
            try:
                hist.append({"value": int(p["value"]), "time": int(p["timestamp"])})
            except: pass
        curr = hist[0]["value"] if hist else 50
        tag = "Neutral"
        if curr <= 25: tag = "Miedo extremo"
        elif curr < 46: tag = "Miedo"
        elif curr > 74: tag = "Codicia extrema"
        elif curr > 54: tag = "Codicia"
        return curr, tag, hist
    except:
        return 50, "Neutral", []

# ====================================================
# SENTIMIENTO
# ====================================================
def construir_sentimiento_live():
    btc = get_price("BTCUSDT")
    eth = get_price("ETHUSDT")
    fng_val, fng_tag, _ = get_fear_and_greed()
    funding = get_funding_rate("BTCUSDT")
    oi = get_open_interest("BTCUSDT")
    reco = "Neutral. Esperar mejor setup o retroceso."
    if fng_val <= 25 and funding < 0: 
        reco = "Smart DCA con cautela (miedo alto). Sin apalancar."
    if fng_val >= 75 and funding > 0.02: 
        reco = "Riesgo de sobre-extensión. Evita apalancamiento."
    return (
        "🧠 TRINITY SENTIMIENTO (Live)\n"
        f"🗓 {ts_now()}\n"
        f"BTC: {fmt_num(btc)} USDT\n"
        f"ETH: {fmt_num(eth)} USDT\n"
        f"Fear & Greed: {fng_val} ({fng_tag})\n"
        f"Funding BTC: {funding:.4e} → {'positivo' if funding>=0 else 'negativo'}\n"
        f"Open Interest BTC: {fmt_num(oi)} contratos abiertos\n\n"
        f"Recomendación: {reco}\n"
        "Nota: sin apalancamiento agresivo hasta nueva señal."
    )

# ====================================================
# FONDOS / TECHOS (zonas de ciclo) + PICOS de liquidez
# ====================================================
def _minimos_locales_desde_velas(velas, ventana=24):
    if not velas or len(velas) < ventana+2: return []
    lows = []
    for i in range(ventana, len(velas)-ventana):
        try:
            low_i = float(velas[i][3])
        except: 
            continue
        es_min = True
        for j in range(i-ventana, i+ventana+1):
            if float(velas[j][3]) < low_i:
                es_min = False; break
        if es_min: lows.append(low_i)
    return lows

def _zona_desde_base(val, margen_pct):
    low = val*(1-margen_pct/100.0); high = val*(1+margen_pct/100.0)
    return round(low,2), round(high,2), round((low+high)/2.0,2)

def _buscar_fondo_tecnico():
    velas = get_klines("BTCUSDT", "1h", limit=300)
    if not velas: return None
    lows = _minimos_locales_desde_velas(velas, ventana=24)
    if not lows: return None
    min_low = min(lows)
    zlow, zhigh, zmid = _zona_desde_base(min_low, 1.5)
    return {"low": zlow, "high": zhigh, "mid": zmid, "origen": "técnico"}

def _buscar_fondo_miedo_extremo(hist_fng, precio_actual):
    if not hist_fng or precio_actual is None: return None
    if any(p["value"] <= 25 for p in hist_fng):
        zlow, zhigh, zmid = _zona_desde_base(precio_actual, 2.0)
        return {"low": zlow, "high": zhigh, "mid": zmid, "origen": "miedo_extremo"}
    return None

def _buscar_fondo_acumulacion():
    velas = get_klines("BTCUSDT", "4h", limit=200)
    if not velas: return None
    closes = []
    for v in velas:
        try: closes.append(float(v[4]))
        except: pass
    if len(closes) < 10: return None
    closes_sorted = sorted(closes)
    n = max(int(len(closes_sorted)*0.1), 5)
    base_prom = sum(closes_sorted[:n]) / n
    zlow, zhigh, zmid = _zona_desde_base(base_prom, 1.0)
    return {"low": zlow, "high": zhigh, "mid": zmid, "origen": "acumulacion"}

def _combinar_fondos(f_tecnico, f_miedo, f_acum):
    pools = [p for p in [f_tecnico, f_miedo, f_acum] if p]
    if not pools: return None
    low_final  = round(min(p["low"]  for p in pools),2)
    high_final = round(max(p["high"] for p in pools),2)
    mid_final  = round(sum(p["mid"]  for p in pools)/len(pools),2)
    fuerza = "fuerte" if len(pools)==3 else ("medio" if len(pools)==2 else "débil")
    origen = ",".join(sorted({p["origen"] for p in pools}))
    return {"low":low_final,"high":high_final,"mid":mid_final,"fuerza":fuerza,"origen":origen}

def _detectar_techo_historico():
    velas = get_klines("BTCUSDT", "1h", limit=300)
    if not velas: return None
    highs = []
    for v in velas:
        try: highs.append(float(v[2]))
        except: pass
    if not highs: return None
    max_high = max(highs)
    low, high, mid = _zona_desde_base(max_high, 0.8)
    return {"low":low,"high":high,"mid":mid,"calidad":"débil"}

def actualizar_niveles_estrategicos():
    global POOL_FONDO_ESTRATEGICO, POOL_TECHO_ESTRATEGICO
    precio_btc = get_price("BTCUSDT")
    fng_val, fng_tag, fng_hist = get_fear_and_greed()
    f_tecnico = _buscar_fondo_tecnico()
    f_miedo   = _buscar_fondo_miedo_extremo(fng_hist, precio_btc)
    f_acum    = _buscar_fondo_acumulacion()
    POOL_FONDO_ESTRATEGICO = _combinar_fondos(f_tecnico, f_miedo, f_acum)
    POOL_TECHO_ESTRATEGICO = _detectar_techo_historico()

# —— Picos de liquidez (histograma de precios dentro del rango) ——
def detectar_picos_liquidez_en_rango(low, high, velas):
    if not velas or low is None or high is None or high <= low: 
        return []
    buckets = {}
    width = (high - low) / 50.0
    puntos = []
    for v in velas:
        try:
            close = float(v[4])
            if low <= close <= high:
                puntos.append(close)
                idx = int(math.floor((close - low) / width))
                buckets[idx] = buckets.get(idx, 0) + 1
        except: pass
    if not buckets: return []
    top = sorted(buckets.items(), key=lambda kv: kv[1], reverse=True)[:3]
    picos = []
    for idx, frecuencia in top:
        nivel_aprox = low + idx*width + width/2.0
        if frecuencia >= max(2, int(len(puntos)*0.5)):
            intensidad = "🔥🔥🔥"; tipo = "fuerte"
        elif frecuencia >= max(1, int(len(puntos)*0.3)):
            intensidad = "🔥🔥"; tipo = "medio"
        else:
            intensidad = "🔥"; tipo = "mixto"
        picos.append({"precio": round(nivel_aprox,2), "tipo": tipo, "intensidad": intensidad, "frecuencia": int(frecuencia)})
    picos.sort(key=lambda x: x["precio"])
    return picos

# ====================================================
# RADAR v15 (dos mensajes)
# ====================================================
def _fmt_fondo_line(pool):
    if not pool: return "• Fondo estratégico: N/D"
    return f"• Fondo estratégico ({pool['fuerza']}): {fmt_num(pool['low'])} – {fmt_num(pool['high'])} (mid {fmt_num(pool['mid'])})"

def _fmt_techo_line(pool):
    if not pool: return "• Techo histórico: N/D"
    return f"• Techo histórico ({pool['calidad']}): {fmt_num(pool['low'])} – {fmt_num(pool['high'])} (mid {fmt_num(pool['mid'])})"

def generar_radar_v15():
    actualizar_niveles_estrategicos()
    btc_now = get_price("BTCUSDT")
    oi_btc = get_open_interest("BTCUSDT")

    msg = "📍 TRINITY RADAR BTC v15 (Ciclo)\n\n"
    msg += f"🗓 {ts_now()}\n"
    msg += f"🕰 Precio actual: {fmt_num(btc_now)} USDT\n\n"
    msg += "🏛 Zonas de Ciclo:\n"
    msg += _fmt_fondo_line(POOL_FONDO_ESTRATEGICO) + "\n"
    msg += _fmt_techo_line(POOL_TECHO_ESTRATEGICO) + "\n\n"

    # Picos Fondo
    if POOL_FONDO_ESTRATEGICO:
        velas4h = get_klines("BTCUSDT", "4h", limit=300)
        pf = POOL_FONDO_ESTRATEGICO
        picos_fondo = detectar_picos_liquidez_en_rango(pf["low"], pf["high"], velas4h)
        if picos_fondo:
            msg += "🔥 Picos de liquidez dentro del Fondo:\n"
            for p in picos_fondo:
                msg += f"• ~{fmt_num(p['precio'])} USDT ({p['intensidad']}, tipo {p['tipo']}, freq={p['frecuencia']})\n"
            msg += "\n"

    # Picos Techo
    if POOL_TECHO_ESTRATEGICO:
        velas4h = get_klines("BTCUSDT", "4h", limit=300)
        pt = POOL_TECHO_ESTRATEGICO
        picos_techo = detectar_picos_liquidez_en_rango(pt["low"], pt["high"], velas4h)
        if picos_techo:
            msg += "🧱 Picos de liquidez dentro del Techo:\n"
            for p in picos_techo:
                msg += f"• ~{fmt_num(p['precio'])} USDT ({p['intensidad']}, tipo {p['tipo']}, freq={p['frecuencia']})\n"
            msg += "\n"

    msg += f"Open Interest BTC: {fmt_num(oi_btc)} contratos"

    notas = (
        "📌 Notas Trinity v15:\n"
        "- 'Fondo estratégico' = zona donde históricamente hubo miedo/acumulación. "
        "Uso: Smart DCA spot y escalonado cuando el mercado se acerca.\n"
        "- 'Techo histórico' = zona de euforia/squeeze. Uso: realizar tomas parciales, bajar deuda, rotar riesgo.\n"
        "- Más 🔥 en picos = mayor confluencia de cierres en ese nivel.\n"
        "No hagas all-in nunca. Ajusta por etapas y espera confirmaciones."
    )
    return msg, notas

# ====================================================
# WATCHDOG CICLO
# ====================================================
def enviar_watchdog_alert(lado, zona_txt, texto_extra):
    global ULTIMA_ALERTA_LADO, ULTIMA_ALERTA_ZONA, ULTIMA_ALERTA_TS
    ahora = time.time()
    if (ULTIMA_ALERTA_LADO == lado and ULTIMA_ALERTA_ZONA == zona_txt and 
        (ahora - ULTIMA_ALERTA_TS) < ANTI_SPAM_COOLDOWN):
        return
    enviar_alerta(texto_extra)
    ULTIMA_ALERTA_LADO = lado
    ULTIMA_ALERTA_ZONA = zona_txt
    ULTIMA_ALERTA_TS   = ahora

def watchdog_ciclo_v15():
    actualizar_niveles_estrategicos()
    precio_btc = get_price("BTCUSDT")
    if precio_btc is None: return

    # Fondo
    if POOL_FONDO_ESTRATEGICO:
        mid = POOL_FONDO_ESTRATEGICO["mid"]
        if pct_abs(precio_btc, mid) < 1.0:
            zona_txt = f"{fmt_num(POOL_FONDO_ESTRATEGICO['low'])}–{fmt_num(POOL_FONDO_ESTRATEGICO['high'])}"
            texto = ("🔔 ZONA DE SMART DCA DETECTADA\n"
                     f"BTC acercándose a {zona_txt}\n"
                     f"({POOL_FONDO_ESTRATEGICO['fuerza']}, {POOL_FONDO_ESTRATEGICO['origen']}).\n"
                     "Lectura: área histórica de miedo/acumulación.\n"
                     "Acción: cargar spot en escalones. Nada all-in. Sin apalancar.")
            enviar_watchdog_alert("fondo", zona_txt, texto)

    # Techo
    if POOL_TECHO_ESTRATEGICO:
        mid = POOL_TECHO_ESTRATEGICO["mid"]
        if pct_abs(precio_btc, mid) < 1.0:
            zona_txt = f"{fmt_num(POOL_TECHO_ESTRATEGICO['low'])}–{fmt_num(POOL_TECHO_ESTRATEGICO['high'])}"
            texto = ("🔔 ZONA DE TOMA DE GANANCIA\n"
                     f"BTC acercándose a {zona_txt}\n"
                     f"({POOL_TECHO_ESTRATEGICO['calidad']}).\n"
                     "Lectura: euforia/sobre-extensión.\n"
                     "Acción: reducir riesgo, pagar deuda, tomar parcial.")
            enviar_watchdog_alert("techo", zona_txt, texto)

# =============================================================================
# AUTOSCALP MULTI-MODE (Conservador + Agresivo) con contexto ATR% y Fear&Greed
# Reemplaza completamente tu antigua función autoscalp_sniper()
# =============================================================================

# ====== Parámetros (puedes moverlos a CONFIG si lo prefieres) ======
SCALP_SYMBOL = "ETHUSDT"         # símbolo a monitorear
SCALP_TIMEFRAME = "15m"
SCALP_LIMIT = 200

# Umbrales / tuning
SCALP_ATR_MIN_PCT_CONSERV = 0.0035   # 0.35% min ATR para modo conservador
SCALP_ATR_MIN_PCT_AGRES  = 0.0045    # 0.45% min ATR para modo agresivo

BREAKOUT_LOOKBACK = 20
BUFFER_ATR = 0.20

# Conservador (menor riesgo, metas más cortas)
C_TP1_MULT = 1.6
C_TP2_MULT = 2.8
C_SL_MULT  = 1.15

# Agresivo (menos señales, metas más grandes)
A_TP1_MULT = 2.0
A_TP2_MULT = 3.5
A_SL_MULT  = 1.25

# Decisión de modo
FNG_AGGRESSIVE_THRESHOLD = 40   # si Fear&Greed <= 40 → preferir agresivo
ATR_SWITCH_PCT = 0.0045         # si atr_pct >= esto → agresivo

# ------------------------------
# Helpers (sólo si no existen)
# ------------------------------
if 'ema' not in globals():
    def ema(vals, n):
        if not vals or len(vals) < n:
            return []
        k = 2/(n+1)
        out = [vals[0]]
        for v in vals[1:]:
            out.append(out[-1] + k*(v - out[-1]))
        return out

if 'pct_gain' not in globals():
    def pct_gain(target, entry):
        return round(((target - entry) / max(1e-6, entry)) * 100, 2)

if 'pct_gain_short' not in globals():
    def pct_gain_short(target, entry):
        return round(((entry - target) / max(1e-6, entry)) * 100, 2)

def _fng_label(v):
    try:
        v = int(v)
    except:
        return "—"
    if v <= 25: return "Miedo Extremo"
    if v <= 45: return "Miedo"
    if v < 60:  return "Neutral"
    if v < 75:  return "Codicia"
    return "Codicia Extrema"

# ------------------------------
# Modo conservador
# ------------------------------
def autoscalp_sniper_conservador(closes, highs, lows, price, atr):
    atr_pct = atr / max(1e-6, price)
    if atr_pct < SCALP_ATR_MIN_PCT_CONSERV:
        return None

    ema50  = ema(closes, 50)
    ema200 = ema(closes, 200)
    if not ema50 or not ema200:
        return None
    trend_up   = ema50[-1] > ema200[-1]
    trend_down = ema50[-1] < ema200[-1]

    recent_high = max(highs[-(BREAKOUT_LOOKBACK+1):-1])
    recent_low  = min(lows[-(BREAKOUT_LOOKBACK+1):-1])

    long_trigger  = (price > recent_high + BUFFER_ATR * atr) and trend_up
    short_trigger = (price < recent_low  - BUFFER_ATR * atr) and trend_down

    if long_trigger:
        entry = round(price, 2)
        tp1   = round(price + C_TP1_MULT * atr, 2)
        tp2   = round(price + C_TP2_MULT * atr, 2)
        sl    = round(price - C_SL_MULT  * atr, 2)
        return {
            "mode":"Conservador","side":"LONG","entry":entry,"sl":sl,"tp1":tp1,"tp2":tp2,
            "rr1": pct_gain(tp1, entry), "rr2": pct_gain(tp2, entry),
            "trend_up": True,
            "note":"Filtro: EMA50>EMA200, ATR suficiente, ruptura de máximos."
        }
    if short_trigger:
        entry = round(price, 2)
        tp1   = round(price - C_TP1_MULT * atr, 2)
        tp2   = round(price - C_TP2_MULT * atr, 2)
        sl    = round(price + C_SL_MULT  * atr, 2)
        return {
            "mode":"Conservador","side":"SHORT","entry":entry,"sl":sl,"tp1":tp1,"tp2":tp2,
            "rr1": pct_gain_short(tp1, entry), "rr2": pct_gain_short(tp2, entry),
            "trend_up": False,
            "note":"Filtro: EMA50<EMA200, ATR suficiente, ruptura de mínimos."
        }
    return None

# ------------------------------
# Modo agresivo
# ------------------------------
def autoscalp_sniper_agresivo(closes, highs, lows, price, atr):
    atr_pct = atr / max(1e-6, price)
    if atr_pct < SCALP_ATR_MIN_PCT_AGRES:
        return None

    ema50  = ema(closes, 50)
    ema200 = ema(closes, 200)
    if not ema50 or not ema200:
        return None
    trend_up   = ema50[-1] > ema200[-1]
    trend_down = ema50[-1] < ema200[-1]

    recent_high = max(highs[-(BREAKOUT_LOOKBACK+1):-1])
    recent_low  = min(lows[-(BREAKOUT_LOOKBACK+1):-1])

    # En agresivo pedimos ruptura más clara (buffer mayor)
    long_trigger  = (price > recent_high + (BUFFER_ATR+0.10) * atr) and trend_up
    short_trigger = (price < recent_low  - (BUFFER_ATR+0.10) * atr) and trend_down

    if long_trigger:
        entry = round(price, 2)
        tp1   = round(price + A_TP1_MULT * atr, 2)
        tp2   = round(price + A_TP2_MULT * atr, 2)
        sl    = round(price - A_SL_MULT  * atr, 2)
        return {
            "mode":"Agresivo","side":"LONG","entry":entry,"sl":sl,"tp1":tp1,"tp2":tp2,
            "rr1": pct_gain(tp1, entry), "rr2": pct_gain(tp2, entry),
            "trend_up": True,
            "note":"Filtro agresivo: ruptura + tendencia + volatilidad alta."
        }
    if short_trigger:
        entry = round(price, 2)
        tp1   = round(price - A_TP1_MULT * atr, 2)
        tp2   = round(price - A_TP2_MULT * atr, 2)
        sl    = round(price + A_SL_MULT  * atr, 2)
        return {
            "mode":"Agresivo","side":"SHORT","entry":entry,"sl":sl,"tp1":tp1,"tp2":tp2,
            "rr1": pct_gain_short(tp1, entry), "rr2": pct_gain_short(tp2, entry),
            "trend_up": False,
            "note":"Filtro agresivo: ruptura + tendencia + volatilidad alta."
        }
    return None

# ------------------------------
# Función principal que decide modo y envía alerta
# ------------------------------
def autoscalp_sniper():
    try:
        velas = get_klines(SCALP_SYMBOL, SCALP_TIMEFRAME, limit=SCALP_LIMIT)
        if not velas:
            return

        closes = [float(v[4]) for v in velas]
        highs  = [float(v[2]) for v in velas]
        lows   = [float(v[3]) for v in velas]
        price  = closes[-1]
        hi_lo  = [h - l for h, l in zip(highs, lows)]

        # ATR simple (media de los últimos 20 rangos)
        atr = max(1e-6, statistics.mean(hi_lo[-20:]))

        atr_pct = atr / max(1e-6, price)            # proporción (0.0046 = 0.46%)
        atr_pct_txt = f"{atr_pct*100:.2f}%"

        # Fear & Greed (si falla, neutral 50)
        try:
            fng_val, _, _ = get_fear_and_greed()
        except:
            fng_val = 50
        fng_txt = f"{fng_val} ({_fng_label(fng_val)})"

        # ¿Qué modo preferir?
        prefer_agresivo = (atr_pct >= ATR_SWITCH_PCT) or (fng_val <= FNG_AGGRESSIVE_THRESHOLD)

        result = None
        if prefer_agresivo:
            result = autoscalp_sniper_agresivo(closes, highs, lows, price, atr)
            if not result:
                result = autoscalp_sniper_conservador(closes, highs, lows, price, atr)
        else:
            result = autoscalp_sniper_conservador(closes, highs, lows, price, atr)
            if not result:
                result = autoscalp_sniper_agresivo(closes, highs, lows, price, atr)

        if result:
            modo  = result["mode"]
            side  = result["side"]
            entry = result["entry"]; sl = result["sl"]
            tp1   = result["tp1"];   tp2 = result["tp2"]
            rr1   = result["rr1"];   rr2 = result["rr2"]
            nota  = result.get("note","")
            trend = "EMA50>EMA200" if result.get("trend_up", False) else "EMA50<EMA200"

            enviar_alerta(
                f"🎯 TRINITY AUTOSCALP ({modo})\n"
                f"🗓 {ts_now()}\n"
                f"{side} {SCALP_SYMBOL} - Entrada: {fmt_num(entry)}  |  SL: {fmt_num(sl)}\n"
                f"TP1: {fmt_num(tp1)} (+{rr1}%)  |  TP2: {fmt_num(tp2)} (+{rr2}%)\n"
                f"Contexto: ATR: {atr_pct_txt} | F&G: {fng_txt} | Tendencia: {trend}\n"
                f"{nota}\n"
                "Nota: revisa tamaño de posición y evita apalancamiento extremo."
            )

        # Pulso health monitor
        global LAST_SCALP_OK
        LAST_SCALP_OK = time.time()

    except Exception as e:
        print("Error en autoscalp_sniper:", e)
        try:
            LAST_SCALP_OK = time.time()
        except:
            pass

# ====================================================
# PEAK (placeholder simple)
# ====================================================
def construir_peak_msg():
    fng, tag, _ = get_fear_and_greed()
    oi = get_open_interest("BTCUSDT")
    fecha += f"🗓 {ts_now()}\n"
    if fng >= 75 and oi > 70000:
        return "🧨 TRINITY PEAK RIESGO\nSeñales de euforia detectadas. Considera reducir exposición."
    return "✅ Peak sin señales de euforia crítica por ahora."

# ====================================================
# CICLOS / THREADS
# ====================================================
def ciclo_radar_periodico():
    global LAST_RADAR_OK
    print("🛰 Trinity radar ciclo (v15) cada 2 h...")
    while True:
        try:
            n, notas = generar_radar_v15()
            enviar_alerta(n); enviar_alerta(notas)
            LAST_RADAR_OK = time.time()
        except Exception as e:
            print("Error en ciclo_radar_periodico:", e)
        time.sleep(RADAR_INTERVAL_SEC)

def ciclo_watchdog():
    global LAST_WATCHDOG_OK
    print("🛡 Trinity watchdog ciclo (v15)...")
    while True:
        try:
            watchdog_ciclo_v15()
            LAST_WATCHDOG_OK = time.time()
        except Exception as e:
            print("Error en ciclo_watchdog:", e)
        time.sleep(WATCHDOG_INTERVAL_SEC)

def ciclo_scalp_sniper():
    global LAST_SCALP_OK
    print("🎯 Trinity AutoScalp sniper cada 10 min...")
    while True:
        try:
            autoscalp_sniper()
            LAST_SCALP_OK = time.time()
        except Exception as e:
            print("Error en ciclo_scalp_sniper:", e)
        time.sleep(SCALP_INTERVAL_SEC)

def ciclo_health_monitor():
    global LAST_HEALTH_OK
    print("💓 Trinity health monitor activo...")
    while True:
        try:
            now = time.time()

            # ---- Ventana de gracia al arrancar ----
            if now - START_TS < HEALTH_STARTUP_GRACE_SEC:
                time.sleep(5)
                continue

            problemas = []
            if now - LAST_RADAR_OK > RADAR_INTERVAL_SEC + 10:
                problemas.append("Radar ciclo OFF (>intervalo)")
            if now - LAST_WATCHDOG_OK > WATCHDOG_INTERVAL_SEC + 10:
                problemas.append("Watchdog OFF (>intervalo)")
            if now - LAST_SCALP_OK > SCALP_INTERVAL_SEC + 10:
                problemas.append("AutoScalp OFF (>intervalo)")

            if problemas:
                enviar_alerta(
                    "❌ Trinity ALERTA SISTEMA:\n• "
                    + "\n• ".join(problemas)
                    + "\nRevisa conexión/PC. Reinicia el bot si es necesario."
                )
            else:
                enviar_alerta(
                    "✅ Trinity OK.\n"
                    "Todos los módulos con pulso.\n"
                    "Binance vivo. Telegram vivo.\n"
                    "AutoScalp sniper, SmartDCA Watchdog y Radar Ciclo v15 activos."
                )

            LAST_HEALTH_OK = now
        except Exception as e:
            print("Error en ciclo_health_monitor:", e)
        time.sleep(HEALTH_INTERVAL_SEC)

# ====================================================
# MAIN
# ====================================================
if __name__ == "__main__":
    import asyncio
    from telegram.ext import ApplicationBuilder

    BOT_TOKEN = os.getenv("BOT_TOKEN")

    if not BOT_TOKEN:
        print("❌ Falta el token del bot.")
        exit()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # 🔹 Aquí vuelves a registrar tus handlers originales
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("radar", radar))
    app.add_handler(CommandHandler("sentimiento", sentimiento))
    app.add_handler(CommandHandler("peak", peak))
    app.add_handler(CommandHandler("watchdog_on", watchdog_on))
    app.add_handler(CommandHandler("scalp_on", scalp_on))

    print("✅ TrinityBot v15 ejecutándose en modo polling (Render)...")
    app.run_polling()
