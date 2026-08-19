import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import time
from datetime import datetime, timedelta, date, time as dtime
import math
import json
from scipy.stats import norm
import os
import random
import io
from fpdf import FPDF
import base64
import uuid
import re
import streamlit.components.v1 as components
from supabase import create_client

# ============ PAGE CONFIG ============
st.set_page_config(
    page_title="NIFTY Options Trading Simulator",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# ==========================================================
# SUPABASE PERSISTENCE LAYER — ANALYTICS + RESUME (v2)
# ==========================================================
APP_VERSION = "v2.2"
APP_BUILD = "2026-08-19-v2.0.4"
SUPABASE_REPORT_BUCKET = "session-reports"


def _normalize_supabase_url(raw_url):
    """
    Return ONLY the Supabase project-root URL required by supabase-py.

    Accepts a normal project URL, a /rest/v1 URL, a Supabase dashboard
    project URL, a bare project reference, or values pasted with labels/quotes.
    """
    raw = str(raw_url or "").strip()
    if not raw:
        return ""

    host_match = re.search(
        r"https?://([a-z0-9-]+)\.supabase\.co",
        raw,
        flags=re.IGNORECASE,
    )
    if host_match:
        return f"https://{host_match.group(1).lower()}.supabase.co"

    dashboard_match = re.search(
        r"(?:dashboard/)?project/([a-z0-9-]{10,})",
        raw,
        flags=re.IGNORECASE,
    )
    if dashboard_match:
        return f"https://{dashboard_match.group(1).lower()}.supabase.co"

    bare = raw.strip().strip('"').strip("'").strip("/")
    if re.fullmatch(r"[a-z0-9-]{10,}", bare, flags=re.IGNORECASE):
        return f"https://{bare.lower()}.supabase.co"

    return ""


def _normalize_supabase_key(raw_key):
    """Extract the actual Supabase API key if labels or quotes were pasted with it."""
    raw = str(raw_key or "").strip()
    if not raw:
        return ""

    modern = re.search(r"(sb_(?:secret|publishable)_[A-Za-z0-9._-]+)", raw)
    if modern:
        return modern.group(1)

    legacy = re.search(r"(eyJ[A-Za-z0-9._-]+)", raw)
    if legacy:
        return legacy.group(1)

    return raw.strip().strip('"').strip("'")


@st.cache_resource
def get_supabase():
    try:
        project_url = _normalize_supabase_url(st.secrets["supabase"]["url"])
        secret_key = _normalize_supabase_key(st.secrets["supabase"]["key"])

        if not project_url or not secret_key:
            print("[Supabase] Invalid or missing project URL / API key in Streamlit Secrets.")
            return None

        return create_client(project_url, secret_key)
    except Exception as exc:
        print(f"[Supabase] Client initialization failed: {exc}")
        return None


supabase = get_supabase()

def supabase_enabled():
    return supabase is not None


def _iso(value):
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _sb_data(response):
    data = getattr(response, "data", None)
    return data if data is not None else []


def test_supabase_connection():
    if not supabase_enabled():
        return False, "Supabase secrets/client are unavailable."
    try:
        supabase.table("participants").select("id").limit(1).execute()
        return True, None
    except Exception as exc:
        print(f"[Supabase] Connection/query test failed: {exc}")
        return False, str(exc)


def get_participant_by_student_id(student_id):
    """Fetch an existing student profile using the stable roll-number key."""
    if not supabase_enabled() or not str(student_id or "").strip():
        return None
    resp = (
        supabase.table("participants")
        .select("id,student_name,student_id,email,last_active_at")
        .eq("student_id", str(student_id).strip())
        .limit(1)
        .execute()
    )
    rows = _sb_data(resp)
    return rows[0] if rows else None


def create_or_get_participant(student_name, student_id, email=""):
    """Create a new student once; otherwise retrieve and refresh the existing profile."""
    if not supabase_enabled():
        return None
    student_name = (student_name or "").strip()
    student_id = (student_id or "").strip()
    email = (email or "").strip().lower()
    existing = get_participant_by_student_id(student_id)
    now = datetime.now().isoformat()
    if existing:
        updates = {"last_active_at": now}
        # Preserve the stored name unless a non-empty corrected name was supplied.
        if student_name and student_name != existing.get("student_name"):
            updates["student_name"] = student_name
        if email and email != (existing.get("email") or ""):
            updates["email"] = email
        try:
            supabase.table("participants").update(updates).eq("id", existing["id"]).execute()
        except Exception:
            pass
        return {**existing, **updates}

    payload = {
        "student_name": student_name,
        "student_id": student_id,
        "email": email or None,
        "last_active_at": now,
    }
    resp = supabase.table("participants").insert(payload).select("id,student_name,student_id,email,last_active_at").execute()
    rows = _sb_data(resp)
    return rows[0] if rows else None


def _next_session_no(participant_id):
    if not supabase_enabled() or not participant_id:
        return 1
    try:
        rows = _sb_data(
            supabase.table("student_sessions")
            .select("id")
            .eq("participant_id", participant_id)
            .execute()
        )
        return len(rows) + 1
    except Exception:
        return 1


def create_session_record():
    """Create one clean analytical row per simulator run."""
    if not supabase_enabled() or not st.session_state.get("participant_id"):
        return None
    participant_id = st.session_state.participant_id
    payload = {
        "participant_id": participant_id,
        "session_no": _next_session_no(participant_id),
        "app_version": APP_VERSION,
        "status": "active",
        "strategy_focus": st.session_state.get("strategy_focus") or "Open practice",
        "starting_capital": float(st.session_state.get("starting_capital", 10000000.0)),
    }
    resp = supabase.table("student_sessions").insert(payload).select("id,session_no").execute()
    rows = _sb_data(resp)
    if not rows:
        return None
    st.session_state.supabase_session_id = rows[0]["id"]
    st.session_state.session_no = rows[0].get("session_no")
    save_progress_snapshot()
    return rows[0]["id"]


def ensure_session_record():
    if st.session_state.get("supabase_session_id"):
        return st.session_state.supabase_session_id
    return create_session_record()


# Orders are intentionally NOT persisted in v2. Only executed trades are analytical records.
def save_order_record(*args, **kwargs):
    return None


def update_order_record(*args, **kwargs):
    return None


def cancel_pending_order_records(*args, **kwargs):
    return None


def _estimate_trade_capital(item, spot):
    """Estimate capital committed at entry; long premium or standalone short margin."""
    qty = int(item.get("quantity", 0) or 0)
    entry = float(item.get("price", item.get("entry_price", 0.0)) or 0.0)
    if item.get("side") == "Buy" or item.get("type") == "FUT":
        if item.get("type") == "FUT":
            return max(abs(float(spot or 0.0) * qty * 0.12), 1.0)
        return max(abs(entry * qty), 1.0)
    try:
        return max(float(calculate_realistic_margin([item], float(spot), int(st.session_state.get("lot_size", 65)))), 1.0)
    except Exception:
        return max(abs(entry * qty), 1.0)


def _strategy_label(item):
    if item.get("strategy_name"):
        return str(item["strategy_name"])
    source = item.get("order_source", "manual")
    return "Manual trade" if source == "manual" else source.replace("_", " ").title()


def save_trade_record(item, current_dt, order_id=None, current_price=None):
    """Persist only an executed trade in an Excel-friendly schema."""
    sid = st.session_state.get("supabase_session_id")
    pid = st.session_state.get("participant_id")
    if not supabase_enabled() or not sid or not pid:
        return None
    capital_used = _estimate_trade_capital(item, current_price)
    trade_no = len(st.session_state.get("tradebook", [])) + 1
    instrument = instrument_label(item.get("strike", 0), item.get("type"))
    payload = {
        "session_id": sid,
        "participant_id": pid,
        "trade_no": trade_no,
        "strategy_name": _strategy_label(item),
        "instrument": instrument,
        "instrument_type": item["type"],
        "strike": None if item["type"] == "FUT" else float(item.get("strike", 0)),
        "side": item["side"],
        "lots": int(item.get("lots", 1)),
        "quantity": int(item.get("quantity", 0)),
        "entry_at": _iso(current_dt),
        "entry_price": float(item.get("price", 0.0)),
        "capital_used": capital_used,
        "pnl": 0.0,
        "return_pct": 0.0,
        "max_drawdown": 0.0,
        "max_drawdown_pct": 0.0,
        "max_profit_seen": 0.0,
        "holding_minutes": 0.0,
        "status": "Open",
    }
    resp = supabase.table("student_trades").insert(payload).select("id").execute()
    rows = _sb_data(resp)
    return rows[0]["id"] if rows else None


def close_trade_record(tradebook_row, current_dt, exit_price, exit_reason):
    trade_id = tradebook_row.get("supabase_trade_id")
    if not supabase_enabled() or not trade_id:
        return
    pnl = float(tradebook_row.get("pnl", 0.0))
    capital_used = max(float(tradebook_row.get("capital_used", 0.0) or 0.0), 1.0)
    holding_minutes = float(tradebook_row.get("holding_minutes", 0.0) or 0.0)
    entry_dt_raw = tradebook_row.get("entry_dt")
    if entry_dt_raw:
        try:
            entry_dt = datetime.fromisoformat(str(entry_dt_raw))
            holding_minutes = max(0.0, (current_dt - entry_dt).total_seconds() / 60.0)
        except Exception:
            pass
    payload = {
        "exit_at": _iso(current_dt),
        "exit_price": float(exit_price),
        "pnl": pnl,
        "return_pct": pnl / capital_used * 100.0,
        "max_drawdown": float(tradebook_row.get("max_drawdown", 0.0) or 0.0),
        "max_drawdown_pct": float(tradebook_row.get("max_drawdown_pct", 0.0) or 0.0),
        "max_profit_seen": float(tradebook_row.get("max_profit_seen", 0.0) or 0.0),
        "holding_minutes": holding_minutes,
        "status": "Closed",
        "exit_reason": exit_reason,
    }
    supabase.table("student_trades").update(payload).eq("id", trade_id).execute()


def update_live_risk_metrics(current_price, current_dt, chain_df):
    """Track trade-level MAE/max profit and session equity drawdown without excessive DB writes."""
    for t in st.session_state.get("tradebook", []):
        if t.get("status") != "Open":
            continue
        typ = t.get("type")
        if typ == "FUT":
            mark = float(current_price)
        else:
            row = chain_df[chain_df["Strike"] == t.get("strike")] if chain_df is not None else pd.DataFrame()
            if len(row) == 0:
                continue
            mark = float(row.iloc[0]["CE Price"] if typ == "CE" else row.iloc[0]["PE Price"])
        sign = 1 if t.get("side") == "Buy" else -1
        pnl_now = sign * (mark - float(t.get("entry_price", 0.0))) * int(t.get("qty", 0))
        t["max_drawdown"] = max(float(t.get("max_drawdown", 0.0)), max(0.0, -pnl_now))
        t["max_profit_seen"] = max(float(t.get("max_profit_seen", 0.0)), pnl_now)
        capital = max(float(t.get("capital_used", 0.0) or 0.0), 1.0)
        t["max_drawdown_pct"] = t["max_drawdown"] / capital * 100.0
        entry_dt_raw = t.get("entry_dt")
        if entry_dt_raw:
            try:
                t["holding_minutes"] = max(0.0, (current_dt - datetime.fromisoformat(str(entry_dt_raw))).total_seconds() / 60.0)
            except Exception:
                pass


def update_session_drawdown(open_pnl):
    equity = float(st.session_state.get("starting_capital", 10000000.0)) + float(st.session_state.get("realized_pnl", 0.0)) + float(open_pnl)
    peak = max(float(st.session_state.get("equity_peak", equity)), equity)
    dd = max(0.0, peak - equity)
    st.session_state.equity_peak = peak
    st.session_state.max_drawdown = max(float(st.session_state.get("max_drawdown", 0.0)), dd)
    st.session_state.max_drawdown_pct = max(
        float(st.session_state.get("max_drawdown_pct", 0.0)),
        (dd / peak * 100.0) if peak > 0 else 0.0,
    )


def finish_session_record(current_price=None, current_day_num=None, status="completed"):
    sid = st.session_state.get("supabase_session_id")
    if not supabase_enabled() or not sid:
        return
    closed = [t for t in st.session_state.get("tradebook", []) if t.get("status") == "Closed"]
    pnls = [float(t.get("pnl", 0.0)) for t in closed]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    realized = float(st.session_state.get("realized_pnl", 0.0))
    starting = float(st.session_state.get("starting_capital", 10000000.0))
    total_pnl = realized
    peak_margin = float(st.session_state.get("peak_margin_used", 0.0))
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    holding = [float(t.get("holding_minutes", 0.0) or 0.0) for t in closed]
    strategies = {str(t.get("strategy_name") or "Manual trade") for t in closed}
    payload = {
        "status": status,
        "ended_at": datetime.now().isoformat(),
        "reflection_note": st.session_state.get("reflection_note") or None,
        "ending_equity": starting + total_pnl,
        "total_pnl": total_pnl,
        "return_pct": (total_pnl / starting * 100.0) if starting > 0 else 0.0,
        "max_drawdown": float(st.session_state.get("max_drawdown", 0.0)),
        "max_drawdown_pct": float(st.session_state.get("max_drawdown_pct", 0.0)),
        "peak_margin_used": peak_margin,
        "return_on_margin_pct": (total_pnl / peak_margin * 100.0) if peak_margin > 0 else 0.0,
        "total_trades": len(closed),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate_pct": (len(wins) / len(closed) * 100.0) if closed else 0.0,
        "avg_profit_trade": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss_trade": (sum(losses) / len(losses)) if losses else 0.0,
        "best_trade_pnl": max(pnls) if pnls else 0.0,
        "worst_trade_pnl": min(pnls) if pnls else 0.0,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0),
        "avg_holding_minutes": (sum(holding) / len(holding)) if holding else 0.0,
        "strategies_tried": len(strategies),
    }
    supabase.table("student_sessions").update(payload).eq("id", sid).execute()
    if status != "active":
        delete_progress_snapshot()



def sync_live_session_metrics(force=False):
    """
    Keep the current student_sessions row current while the market is still active.

    Leaderboard logic uses realized P&L only. Open/unrealized P&L is never written
    into the leaderboard score. To avoid excessive Supabase traffic, this function
    writes only when realized P&L or the number of closed trades changes.
    """
    sid = st.session_state.get("supabase_session_id")
    if not supabase_enabled() or not sid:
        return False

    closed = [
        t for t in st.session_state.get("tradebook", [])
        if t.get("status") == "Closed"
    ]
    realized = float(st.session_state.get("realized_pnl", 0.0))
    closed_count = len(closed)

    sync_key = (round(realized, 8), closed_count)
    if not force and st.session_state.get("_leaderboard_sync_key") == sync_key:
        return False

    pnls = [float(t.get("pnl", 0.0) or 0.0) for t in closed]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]

    starting = float(st.session_state.get("starting_capital", 10000000.0))
    peak_margin = float(st.session_state.get("peak_margin_used", 0.0))
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    holding = [float(t.get("holding_minutes", 0.0) or 0.0) for t in closed]
    strategies = {str(t.get("strategy_name") or "Manual trade") for t in closed}

    payload = {
        # Keep status unchanged; this is only a live analytics refresh.
        "ending_equity": starting + realized,
        "total_pnl": realized,
        "return_pct": (realized / starting * 100.0) if starting > 0 else 0.0,
        "max_drawdown": float(st.session_state.get("max_drawdown", 0.0)),
        "max_drawdown_pct": float(st.session_state.get("max_drawdown_pct", 0.0)),
        "peak_margin_used": peak_margin,
        "return_on_margin_pct": (realized / peak_margin * 100.0) if peak_margin > 0 else 0.0,
        "total_trades": closed_count,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate_pct": (len(wins) / closed_count * 100.0) if closed_count else 0.0,
        "avg_profit_trade": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss_trade": (sum(losses) / len(losses)) if losses else 0.0,
        "best_trade_pnl": max(pnls) if pnls else 0.0,
        "worst_trade_pnl": min(pnls) if pnls else 0.0,
        "profit_factor": (
            gross_profit / gross_loss
            if gross_loss > 0
            else (gross_profit if gross_profit > 0 else 0.0)
        ),
        "avg_holding_minutes": (sum(holding) / len(holding)) if holding else 0.0,
        "strategies_tried": len(strategies),
    }

    try:
        supabase.table("student_sessions").update(payload).eq("id", sid).execute()
        st.session_state["_leaderboard_sync_key"] = sync_key
        return True
    except Exception as exc:
        print(f"[Supabase] Live session metrics sync failed: {exc}")
        return False

def upload_report_record(filepath, filename):
    sid = st.session_state.get("supabase_session_id")
    pid = st.session_state.get("participant_id")
    if not supabase_enabled() or not sid or not pid or not filepath or not os.path.exists(filepath):
        return None
    storage_path = f"{pid}/{sid}/{filename}"
    with open(filepath, "rb") as f:
        supabase.storage.from_(SUPABASE_REPORT_BUCKET).upload(
            path=storage_path,
            file=f,
            file_options={"content-type": "application/pdf", "cache-control": "3600", "upsert": "false"},
        )
    payload = {
        "session_id": sid,
        "participant_id": pid,
        "file_name": filename,
        "storage_path": storage_path,
        "file_size_bytes": os.path.getsize(filepath),
        "report_version": "2.0",
    }
    supabase.table("session_reports").insert(payload).execute()
    return storage_path


def _progress_state_payload():
    """Compact resumable state. Analytics stay normalized in student_sessions/student_trades."""
    keys = [
        "current_index", "speed", "basket", "positions", "tradebook", "pending_limits",
        "realized_pnl", "max_reached_index", "data_loaded", "prev_day_close",
        "start_time", "session_end", "expiry_dt", "scale_factor", "lot_size",
        "prev_scaled_close", "trading_locked", "session_finished", "current_price",
        "T_current", "starting_capital", "peak_margin_used", "session_start_wall",
        "data_source_choice", "day_close_map", "target_nifty_level", "strategy_focus",
        "equity_peak", "max_drawdown", "max_drawdown_pct", "reflection_note", "session_no",
    ]
    state = {}
    for k in keys:
        v = st.session_state.get(k)
        if isinstance(v, (datetime, date, pd.Timestamp)):
            state[k] = _iso(v)
        else:
            try:
                state[k] = json.loads(json.dumps(v, default=_json_serial))
            except Exception:
                pass
    sim = st.session_state.get("simulated_data")
    if sim is not None:
        records = sim.copy()
        if "datetime" in records.columns:
            records["datetime"] = records["datetime"].astype(str)
        state["_sim_records"] = json.loads(records.to_json(orient="records", date_format="iso"))
    return state


def save_progress_snapshot():
    sid = st.session_state.get("supabase_session_id")
    pid = st.session_state.get("participant_id")
    if not supabase_enabled() or not sid or not pid:
        return
    payload = {
        "participant_id": pid,
        "session_id": sid,
        "state_json": _progress_state_payload(),
        "updated_at": datetime.now().isoformat(),
    }
    supabase.table("student_progress").upsert(payload, on_conflict="participant_id").execute()
    try:
        supabase.table("participants").update({"last_active_at": datetime.now().isoformat()}).eq("id", pid).execute()
    except Exception:
        pass


def get_active_progress(participant_id):
    if not supabase_enabled() or not participant_id:
        return None
    resp = (
        supabase.table("student_progress")
        .select("participant_id,session_id,state_json,updated_at")
        .eq("participant_id", participant_id)
        .limit(1)
        .execute()
    )
    rows = _sb_data(resp)
    return rows[0] if rows else None


def restore_progress_snapshot(progress):
    if not progress:
        return False
    state = dict(progress.get("state_json") or {})
    recs = state.pop("_sim_records", None)
    if recs:
        df = pd.DataFrame(recs)
        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"])
        st.session_state.simulated_data = df
        st.session_state.df_day_scaled = df
        st.session_state.df_raw = df
    for k, v in state.items():
        if k in ("start_time", "session_end", "expiry_dt", "session_start_wall") and v:
            try:
                v = datetime.fromisoformat(str(v))
            except Exception:
                pass
        st.session_state[k] = v
    st.session_state.supabase_session_id = progress.get("session_id")
    st.session_state.playing = False  # resume paused, never unexpectedly live
    return True


def delete_progress_snapshot():
    pid = st.session_state.get("participant_id")
    if supabase_enabled() and pid:
        try:
            supabase.table("student_progress").delete().eq("participant_id", pid).execute()
        except Exception:
            pass


def get_student_history(participant_id, limit=20):
    if not supabase_enabled() or not participant_id:
        return []
    resp = (
        supabase.table("student_sessions")
        .select("session_no,started_at,ended_at,status,strategy_focus,total_trades,total_pnl,return_pct,max_drawdown_pct,win_rate_pct,profit_factor")
        .eq("participant_id", participant_id)
        .order("session_no", desc=True)
        .limit(limit)
        .execute()
    )
    return _sb_data(resp)


def get_student_trades(participant_id, limit=200):
    if not supabase_enabled() or not participant_id:
        return []
    resp = (
        supabase.table("trade_analysis_export")
        .select("*")
        .eq("participant_id", participant_id)
        .order("entry_at", desc=True)
        .limit(limit)
        .execute()
    )
    return _sb_data(resp)


def get_leaderboard():
    if not supabase_enabled():
        return []
    try:
        return _sb_data(supabase.table("leaderboard_top5").select("*").execute())
    except Exception:
        return []

# ==========================================================
# END SUPABASE PERSISTENCE LAYER
# ==========================================================

# ============ GLOBAL CSS ============
APP_CSS = """
<style>
/* Hide default Streamlit elements */
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
header {visibility: hidden;}
.stDeployButton {display: none;}
.stApp > header { display: none !important; }
div[data-testid="stToolbar"] { display: none !important; }

/* Light professional background */
.stApp { background: #f4f6f9 !important; }
section.main > div { padding-top: 0 !important; }
div[data-testid="stAppViewContainer"] > div:first-child { padding-top: 0 !important; }

.main .block-container {
    padding-top: 52px !important;
    max-width: 100% !important;
    padding-left: 10px !important;
    padding-right: 10px !important;
}

/* Fixed header */
.fixed-header {
    position: fixed;
    top: 0; left: 0; right: 0;
    z-index: 999999;
    background: #0a2540;
    color: white;
    padding: 8px 18px;
    border-radius: 0 0 10px 10px;
    box-shadow: 0 2px 10px rgba(0,0,0,0.15);
    text-align: left;
}
.fixed-header h1 {
    margin: 0;
    font-size: 15px;
    font-weight: 700;
    letter-spacing: 0.3px;
    color: #ffffff;
    line-height: 1.2;
}
.fixed-header p {
    margin: 2px 0 0 0;
    font-size: 11px;
    color: #a8c5e2;
    font-weight: 500;
    line-height: 1.2;
}

/* Two-column panels - light */
div[data-testid="stHorizontalBlock"] > div:nth-child(1) > div {
    background: #f7f3eb;
    border-radius: 12px;
    padding: 12px 10px !important;
    border: 1px solid #e8e0d0;
}
div[data-testid="stHorizontalBlock"] > div:nth-child(2) > div {
    background: #ffffff;
    border-radius: 12px;
    padding: 12px 14px !important;
    border: 1px solid #eaeaea;
}

/* Cards */
.card {
    background: #ffffff;
    border-radius: 12px;
    padding: 14px 16px;
    margin-bottom: 12px;
    border: 1px solid #ebe5d8;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
}
.card-beige {
    background: #faf7f0;
    border-radius: 12px;
    padding: 14px 16px;
    margin-bottom: 12px;
    border: 1px solid #e8e0d0;
}

/* NIFTY */
.nifty-symbol {
    font-size: 12px;
    font-weight: 600;
    color: #666;
    letter-spacing: 0.5px;
    margin-bottom: 2px;
    text-transform: uppercase;
}
.nifty-price {
    font-size: 26px;
    font-weight: 700;
    line-height: 1.1;
    margin-bottom: 2px;
    font-variant-numeric: tabular-nums;
}
.nifty-up { color: #00a86b; }
.nifty-down { color: #e74c3c; }
.nifty-change {
    font-size: 13px;
    font-weight: 600;
    margin-bottom: 4px;
    font-variant-numeric: tabular-nums;
}
.nifty-meta {
    font-size: 12px;
    color: #555;
    margin-top: 8px;
    line-height: 1.55;
}
.nifty-meta span {
    font-weight: 600;
    color: #222;
}

/* P&L rows */
.pnl-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 9px 12px;
    border-radius: 10px;
    margin-bottom: 6px;
    background: #ffffff;
    border: 1px solid #ebe5d8;
}
.pnl-label {
    font-size: 12px;
    font-weight: 600;
    color: #555;
    text-transform: uppercase;
    letter-spacing: 0.3px;
}
.pnl-value {
    font-size: 15px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
}
.profit { color: #00a86b !important; }
.loss { color: #e74c3c !important; }

/* Buttons */
.stButton > button {
    border-radius: 8px !important;
    font-weight: 600 !important;
    border: none !important;
    transition: all 0.12s ease;
}
.stButton > button:hover {
    transform: translateY(-1px);
    box-shadow: 0 3px 8px rgba(0,0,0,0.12);
}

.reset-btn-container button {
    background: #c0392b !important;
    color: white !important;
    font-weight: 700 !important;
    border-radius: 8px !important;
    padding: 8px 0 !important;
}

/* Tabs - Zerodha style light */
.stTabs [data-baseweb="tab-list"] {
    gap: 4px;
    background: #ebf0f5;
    padding: 5px;
    border-radius: 8px;
    border: 1px solid #d6dee8;
}
.stTabs [data-baseweb="tab"] {
    border-radius: 6px;
    padding: 9px 16px;
    font-weight: 700;
    font-size: 13px;
    color: #444 !important;
    background: transparent !important;
}
.stTabs [aria-selected="true"] {
    background: #387ed1 !important;
    color: #ffffff !important;
    box-shadow: 0 2px 6px rgba(56,126,209,0.35);
}
.stTabs [data-baseweb="tab"]:hover {
    background: #dce6f5 !important;
    color: #1a1a1a !important;
}

/* Margin box */
.margin-box {
    background: #f8f9fb;
    border-radius: 10px;
    padding: 12px;
    margin: 10px 0;
    border: 1px solid #e8ecf0;
}
.margin-grid {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 10px;
    text-align: center;
}
.margin-label {
    font-size: 10px;
    color: #777;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.4px;
}
.margin-val {
    font-size: 14px;
    font-weight: 700;
    color: #222;
    margin-top: 2px;
    font-variant-numeric: tabular-nums;
}

/* Positions */
.pos-instrument {
    font-weight: 600;
    font-size: 14px;
    color: #1a1a1a;
}
.pos-meta {
    font-size: 12px;
    color: #777;
    margin-top: 2px;
}
.pos-side-buy { color: #00a86b; font-weight: 700; }
.pos-side-sell { color: #e74c3c; font-weight: 700; }

/* Section titles */
.section-title {
    font-size: 14px;
    font-weight: 700;
    color: #1a1a1a;
    margin: 12px 0 8px 0;
    letter-spacing: 0.2px;
}
.subsection-title {
    font-size: 13px;
    font-weight: 700;
    color: #333;
    margin: 8px 0 6px 0;
}

/* Greek boxes */
.greek-box {
    display: inline-block;
    background: #f0f4f8;
    border-radius: 6px;
    padding: 5px 10px;
    margin: 3px 4px;
    font-size: 12px;
    font-weight: 600;
    border: 1px solid #e0e6ed;
}
.greek-label {
    color: #777;
    font-size: 10px;
    text-transform: uppercase;
    margin-right: 4px;
}

.status-banner {
    background: #fff8e6;
    border-left: 4px solid #f0b429;
    border-radius: 6px;
    padding: 10px 14px;
    margin-bottom: 10px;
    font-size: 13px;
    font-weight: 600;
    color: #7a5c00;
}

.stSelectbox label, .stNumberInput label, .stSlider label {
    font-size: 12px !important;
    font-weight: 600 !important;
    color: #444 !important;
}
div[data-testid="stMetricValue"] {
    font-size: 17px;
}

/* ===== Disclaimer banner (non-negotiable, must stay visible) ===== */
.disclaimer-banner {
    background: #fdeeee;
    border: 1px solid #f3c6c6;
    border-left: 4px solid #c0392b;
    border-radius: 8px;
    padding: 7px 14px;
    margin: 8px 0 12px 0;
    font-size: 12px;
    font-weight: 600;
    color: #7a1f1f;
    text-align: center;
    letter-spacing: 0.1px;
}

/* ===== Order entry card ===== */
.order-card {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 12px;
    padding: 14px 16px 6px 16px;
    margin-bottom: 12px;
}
.order-card-title {
    font-size: 13px;
    font-weight: 700;
    color: #387ed1;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    margin-bottom: 8px;
}

/* Moneyness badges */
.money-badge {
    display: inline-block;
    font-size: 10px;
    font-weight: 700;
    padding: 2px 7px;
    border-radius: 5px;
    margin-left: 6px;
    letter-spacing: 0.3px;
    vertical-align: middle;
}
.money-itm { background: #e3f7ec; color: #0a7a4a; border: 1px solid #b9e8cf; }
.money-atm { background: #fff4d6; color: #8a6100; border: 1px solid #f0dca0; }
.money-otm { background: #f0f1f3; color: #666; border: 1px solid #dfe2e6; }

/* Order preview box */
.preview-box {
    background: #f7fafd;
    border: 1px solid #dce8f5;
    border-radius: 10px;
    padding: 10px 14px;
    margin: 8px 0 10px 0;
}
.preview-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 8px;
    text-align: center;
}
.preview-label {
    font-size: 10px;
    color: #6b7a8d;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.3px;
}
.preview-val {
    font-size: 14px;
    font-weight: 700;
    color: #1a1a1a;
    margin-top: 2px;
    font-variant-numeric: tabular-nums;
}
.preview-hint {
    font-size: 11px;
    color: #7a8699;
    margin-top: 6px;
    line-height: 1.4;
}

/* Basket / order-book item cards */
.item-card {
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: #ffffff;
    border: 1px solid #ececec;
    border-radius: 8px;
    padding: 8px 12px;
    margin-bottom: 6px;
}
.item-side-badge {
    display: inline-block;
    font-size: 10px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 5px;
    margin-right: 8px;
    letter-spacing: 0.3px;
}
.item-side-buy { background: #e3f7ec; color: #0a7a4a; }
.item-side-sell { background: #fde9e7; color: #b62f22; }
.item-main { font-size: 13px; font-weight: 600; color: #1a1a1a; }
.item-sub { font-size: 11px; color: #888; margin-top: 1px; }

/* Empty-state box */
.empty-box {
    border: 1.5px dashed #d7dee6;
    border-radius: 10px;
    padding: 16px;
    text-align: center;
    color: #8a94a3;
    font-size: 12px;
    font-weight: 500;
    background: #fbfcfd;
    margin-bottom: 8px;
}

/* Insufficient-margin error card */
.margin-err-box {
    background: #fdeeee;
    border: 1px solid #f3c6c6;
    border-left: 4px solid #c0392b;
    border-radius: 8px;
    padding: 10px 14px;
    margin: 8px 0;
    font-size: 12.5px;
    color: #7a1f1f;
    line-height: 1.5;
}

/* Strategy builder */
.strategy-card {
    background: #f8fbff;
    border: 1px solid #cfe0f5;
    border-radius: 12px;
    padding: 14px 16px 10px 16px;
    margin-bottom: 12px;
}
.strategy-leg-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 7px;
    padding: 6px 12px;
    margin-bottom: 5px;
    font-size: 12.5px;
}
.strategy-note {
    font-size: 11px;
    color: #7a8699;
    margin: 6px 0 4px 0;
    line-height: 1.4;
}

/* Short learning-value hint lines (Greeks, DTE, P&L, hold-vs-exit) */
.hint-line {
    font-size: 11px;
    color: #7a8699;
    line-height: 1.45;
    margin: 4px 0 2px 0;
}
.hint-line b { color: #556; }
.insight-box {
    background: #eef6f0;
    border: 1px solid #cfe8d6;
    border-left: 4px solid #0a7a4a;
    border-radius: 8px;
    padding: 8px 12px;
    margin: 6px 0 10px 0;
    font-size: 12.5px;
    color: #1a4a30;
    line-height: 1.5;
}

/* Glossary tooltips -- hover (desktop) / tap-to-focus (mobile) definitions,
   shown the first time a term appears in the session. */
.glossary-term {
    border-bottom: 1px dotted #0a2540;
    cursor: help;
    position: relative;
    font-weight: 600;
}
.glossary-tip {
    display: none;
    position: absolute;
    bottom: 125%;
    left: 50%;
    transform: translateX(-50%);
    background: #0a2540;
    color: #fff;
    padding: 7px 10px;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 400;
    line-height: 1.4;
    width: 220px;
    z-index: 50;
    box-shadow: 0 4px 10px rgba(0,0,0,0.25);
    text-align: left;
}
.glossary-term:hover .glossary-tip,
.glossary-term:focus .glossary-tip {
    display: block;
}

/* Inline basket-leg editor */
.edit-leg-box {
    background: #fffdf5;
    border: 1px solid #f0e2a8;
    border-radius: 8px;
    padding: 10px 12px 4px 12px;
    margin: -2px 0 8px 0;
}

/* ===== UI refinement: restrained academic trading terminal ===== */
html, body, [class*="css"] { font-family: Inter, "Segoe UI", Arial, sans-serif; }
.stApp { color: #17202a !important; }
.main .block-container { max-width: 1440px !important; padding-left: 18px !important; padding-right: 18px !important; }
.fixed-header { min-height: 48px; padding: 9px 20px; border-radius: 0; box-shadow: 0 1px 4px rgba(10,37,64,0.16); }
.fixed-header h1 { font-size: 16px; letter-spacing: 0.1px; }
.fixed-header p { font-size: 11px; color: #c7d8e8; }
.card, .card-beige, .order-card, .strategy-card, .preview-box, .margin-box { border-radius: 8px; box-shadow: none; }
.card { border-color: #dfe5eb; }
.card-beige { background: #fbfaf7; }
.stButton > button { border-radius: 6px !important; min-height: 38px; box-shadow: none !important; letter-spacing: 0; }
.stButton > button:hover { transform: none; box-shadow: none !important; }
.stTabs [data-baseweb="tab-list"] { border-radius: 6px; padding: 3px; }
.stTabs [data-baseweb="tab"] { border-radius: 4px; }
.section-title { font-size: 13px; text-transform: uppercase; letter-spacing: 0.45px; color: #34495e; margin-top: 16px; }
.subsection-title { color: #425466; }
.hint-line, .strategy-note, .preview-hint { color: #657786; }
.disclaimer-banner { background: #fff7ed; color: #7c4a03; border: 1px solid #efd8b4; border-left: 3px solid #c98a2e; border-radius: 5px; text-align: left; font-weight: 500; padding: 7px 12px; }
div[data-testid="stTextInput"] label, div[data-testid="stNumberInput"] label, div[data-testid="stSelectbox"] label, div[data-testid="stSlider"] label, div[data-testid="stCheckbox"] label, div[data-testid="stFileUploader"] label {
    color: #263645 !important; font-weight: 600 !important;
}
/* Force Streamlit/BaseWeb text inputs into a readable light theme, including browser autofill. */
div[data-testid="stTextInput"] [data-baseweb="input"],
div[data-testid="stNumberInput"] [data-baseweb="input"] {
    background: #ffffff !important;
    border: 1px solid #b9c5d1 !important;
    border-radius: 6px !important;
    box-shadow: none !important;
}
div[data-testid="stTextInput"] input,
div[data-testid="stNumberInput"] input {
    background: #ffffff !important;
    color: #17202a !important;
    caret-color: #17202a !important;
    -webkit-text-fill-color: #17202a !important;
    opacity: 1 !important;
    border: 0 !important;
    border-radius: 6px !important;
}
div[data-testid="stTextInput"] input::placeholder,
div[data-testid="stNumberInput"] input::placeholder {
    color: #8a98a6 !important;
    -webkit-text-fill-color: #8a98a6 !important;
    opacity: 1 !important;
}
div[data-testid="stTextInput"] input:-webkit-autofill,
div[data-testid="stTextInput"] input:-webkit-autofill:hover,
div[data-testid="stTextInput"] input:-webkit-autofill:focus,
div[data-testid="stNumberInput"] input:-webkit-autofill,
div[data-testid="stNumberInput"] input:-webkit-autofill:hover,
div[data-testid="stNumberInput"] input:-webkit-autofill:focus {
    -webkit-box-shadow: 0 0 0 1000px #ffffff inset !important;
    box-shadow: 0 0 0 1000px #ffffff inset !important;
    -webkit-text-fill-color: #17202a !important;
    caret-color: #17202a !important;
    transition: background-color 9999s ease-out 0s;
}
div[data-testid="stTextInput"] [data-baseweb="input"]:focus-within,
div[data-testid="stNumberInput"] [data-baseweb="input"]:focus-within {
    border-color: #387ed1 !important;
    box-shadow: 0 0 0 1px #387ed1 !important;
}
div[data-testid="stForm"] { max-width: 720px; margin: 8px auto 24px auto; padding: 24px 26px 20px 26px; background: #ffffff; border: 1px solid #dfe5eb; border-radius: 9px; box-shadow: 0 8px 24px rgba(10,37,64,0.06); }
.identity-title { max-width: 720px; margin: 20px auto 2px auto; font-size: 22px; font-weight: 700; color: #0a2540; }
.identity-subtitle { max-width: 720px; margin: 0 auto 12px auto; font-size: 13px; color: #5e6c78; line-height: 1.55; }
div[data-testid="stForm"] div[data-testid="stMarkdownContainer"] p { color: #34495e !important; }
div[data-testid="stForm"] [data-testid="stCheckbox"] p { color: #34495e !important; }
div[data-testid="stFormSubmitButton"] button {
    background: #0a2540 !important;
    color: #ffffff !important;
    border: 1px solid #0a2540 !important;
    font-weight: 650 !important;
}
div[data-testid="stFormSubmitButton"] button p,
div[data-testid="stFormSubmitButton"] button span { color: #ffffff !important; }
div[data-testid="stFormSubmitButton"] button:hover { background: #123a60 !important; border-color: #123a60 !important; }

</style>
"""

st.markdown(APP_CSS, unsafe_allow_html=True)


# ============ SESSION STATE INIT ============
# ============ SIMULATION CONSTANTS ============
BAR_MINUTES = 5                 # each simulated bar = 5 minutes of market time
BARS_PER_DAY = 75               # 09:15 -> 15:30 in 5-min bars (375 / 5)
SIM_DAYS = 5                    # one trading week
# 1 real second = 1 simulated minute → 5-min bar advances every 5 real seconds at 1x
TICK_SECONDS_BASE = 5.0
DEFAULT_OPEN_PRICE = 24000.0
VOL_MIN, VOL_MAX = 0.12, 0.18   # annualized vol band for the default GARCH path
TOTAL_EXPIRY_DAYS = 25          # calendar days to option expiry from session start
PERSIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oms_session_state.json")

defaults = {
    'simulated_data': None,
    'current_index': 0,
    'playing': False,  # start stopped; user presses GO LIVE
    'speed': 1.0,
    'last_update': time.time(),
    'basket': [],               # renamed from cart; used for basket orders
    'positions': [],
    'tradebook': [],
    'pending_limits': [],
    'realized_pnl': 0.0,
    'max_reached_index': 0,
    'data_loaded': False,
    'selected_date': None,
    'prev_day_close': None,
    'start_time': None,
    'session_end': None,
    'expiry_dt': None,
    'scale_factor': 1.0,
    'lot_size': 65,
    'target_nifty_level': DEFAULT_OPEN_PRICE,
    'prev_scaled_close': None,
    'trading_locked': False,
    'session_finished': False,
    'report_generated': False,
    'report_path': None,
    'df_raw': None,
    'participant_id': None,
    'student_name': '',
    'student_id': '',
    'student_email': '',
    'supabase_session_id': None,
    'session_no': None,
    'strategy_focus': 'Open practice',
    'reflection_note': '',
    'equity_peak': 10000000.0,
    'max_drawdown': 0.0,
    'max_drawdown_pct': 0.0,
    'df_day_scaled': None,
    'current_price': DEFAULT_OPEN_PRICE,
    'T_current': TOTAL_EXPIRY_DAYS / 365,
    'chain_df': None,
    'starting_capital': 10000000.0,  # 1 Cr
    'peak_margin_used': 0.0,
    'session_start_wall': None,
    'data_source_choice': None,   # 'upload' | 'path' | 'garch'
    'day_close_map': {},          # day_num -> previous day's close for change calc
    'editing_basket_idx': None,   # index of the basket leg currently being edited inline, if any
}

for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ============ HELPER / CACHED FUNCTIONS ============

def _add_day_num(df):
    """Tag each bar with a 1-based sequential trading-day number (Day-1, Day-2, ...)."""
    if df is None or len(df) == 0:
        return df
    df = df.copy()
    df['day_num'] = pd.factorize(df['datetime'].dt.date)[0] + 1
    return df


def _parse_raw_lines(lines):
    """Parse whitespace/tab-delimited OHLCV lines: SYMBOL DATE TIME O H L C VOL."""
    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split('\t') if '\t' in line else line.split()
        if len(parts) >= 8:
            try:
                dt = datetime.strptime(f"{parts[1]} {parts[2]}", "%Y%m%d %H:%M")
                records.append({
                    'symbol': parts[0], 'datetime': dt,
                    'open': float(parts[3]), 'high': float(parts[4]),
                    'low': float(parts[5]), 'close': float(parts[6]),
                    'volume': int(parts[7]) if str(parts[7]).isdigit() else 0
                })
            except Exception:
                continue
    return pd.DataFrame(records) if records else None


@st.cache_data(ttl=300)
def load_data_from_path(file_path):
    """Load OHLCV data from a file path on disk."""
    try:
        with open(file_path, 'r') as f:
            lines = f.readlines()
        return _parse_raw_lines(lines)
    except Exception:
        return None


def load_data_from_upload(uploaded_file):
    """Load OHLCV data from a Streamlit UploadedFile."""
    try:
        text = uploaded_file.getvalue().decode('utf-8', errors='ignore')
        return _parse_raw_lines(text.splitlines())
    except Exception:
        return None


def resample_to_bars(df, bar_minutes=BAR_MINUTES):
    """Collapse a finer-grained OHLCV frame down to fixed-width bars."""
    if df is None or len(df) == 0:
        return df
    d = df.set_index('datetime').sort_index()
    agg = {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}
    if 'volume' in d.columns:
        agg['volume'] = 'sum'
    out = d.resample(f'{bar_minutes}min').agg(agg).dropna(subset=['open', 'close']).reset_index()
    return out


def calculate_scale_factor(df, target_level=DEFAULT_OPEN_PRICE):
    if df is not None and len(df) > 0:
        avg_price = df['close'].mean()
        if avg_price > 0:
            return target_level / avg_price
    return 1.0


def scale_data(df, scale_factor):
    if df is not None and scale_factor != 1.0:
        df_scaled = df.copy()
        for col in ['open', 'high', 'low', 'close']:
            if col in df_scaled.columns:
                df_scaled[col] = df_scaled[col] * scale_factor
        return df_scaled
    return df


# ---------- Default path-generation model: discrete-time GARCH(1,1) ----------
# 5-minute bars, annualized volatility band-constrained to [VOL_MIN, VOL_MAX],
# with a random overnight/day-open gap so each session doesn't open at the
# prior session's close (mimics a real market open).

def _simulate_garch_bar_returns(n_bars, bars_per_year, vol_min, vol_max, rng):
    """GARCH(1,1) variance recursion, soft-clipped so annualized vol stays in band."""
    sigma_bar_target = ((vol_min + vol_max) / 2) / np.sqrt(bars_per_year)
    sigma_min = vol_min / np.sqrt(bars_per_year)
    sigma_max = vol_max / np.sqrt(bars_per_year)
    alpha, beta = 0.10, 0.85          # persistence -> volatility clustering
    long_run_var = sigma_bar_target ** 2
    omega = long_run_var * (1 - alpha - beta)

    sigma2 = long_run_var
    prev_r = 0.0
    returns = np.empty(n_bars)
    for i in range(n_bars):
        sigma2 = omega + alpha * prev_r ** 2 + beta * sigma2
        sigma = float(np.clip(np.sqrt(sigma2), sigma_min, sigma_max))
        r = sigma * rng.standard_normal()
        returns[i] = r
        prev_r = r
    return returns


@st.cache_data(ttl=600)
def generate_garch_week_path(start_date, open_price=DEFAULT_OPEN_PRICE, bars_per_day=BARS_PER_DAY,
                              days=SIM_DAYS, vol_min=VOL_MIN, vol_max=VOL_MAX,
                              bar_minutes=BAR_MINUTES, gap_sd=0.004, seed=None):
    """Simulate a one-week 5-minute NIFTY-like path via GARCH(1,1), with gapped daily opens."""
    rng = np.random.default_rng(seed)
    bars_per_year = bars_per_day * 252

    records = []
    cur_date = start_date
    this_open = open_price
    # Implied "previous close" before day 0, so the very first bar also opens on a gap.
    first_gap = rng.normal(0, gap_sd)
    prev_close = open_price / (1 + first_gap)

    for d in range(days):
        gap = first_gap if d == 0 else rng.normal(0, gap_sd)
        this_open = prev_close * (1 + gap)

        returns = _simulate_garch_bar_returns(bars_per_day, bars_per_year, vol_min, vol_max, rng)
        closes = this_open * np.exp(np.cumsum(returns))
        opens = np.concatenate([[this_open], closes[:-1]])

        base_dt = datetime.combine(cur_date, dtime(9, 15))
        wobble = np.abs(rng.normal(0, closes * 0.0004))  # small intrabar high/low noise
        for i in range(bars_per_day):
            o, c = float(opens[i]), float(closes[i])
            hi = max(o, c) + float(wobble[i])
            lo = min(o, c) - float(wobble[i])
            records.append({
                'symbol': 'NIFTY',
                'datetime': base_dt + timedelta(minutes=bar_minutes * (i + 1)),
                'open': round(o, 2), 'high': round(hi, 2),
                'low': round(lo, 2), 'close': round(c, 2),
                'volume': int(rng.integers(500, 5000))
            })
        prev_close = float(closes[-1])
        cur_date = cur_date + timedelta(days=1)  # continuous — all 5 days are treated as trading days

    return pd.DataFrame(records), round(first_gap, 6), round(open_price / (1 + first_gap), 2)

# ---------- IV smile + term-structure surface ----------
# ATM vol rises modestly as expiry nears (front-month effect), and the
# put/call skew (higher IV for OTM puts, lower for OTM calls) steepens
# the closer we get to expiry -- both standard equity-index features.
BASE_ATM_IV = 13.5      # long-dated ATM IV, %
TERM_COEFF = 2.5        # extra ATM IV as expiry approaches
SKEW_SLOPE = 0.9        # linear skew (put side up, call side down)
SKEW_CURV = 0.06        # smile curvature (both wings up vs ATM)

def get_iv_surface(strike, spot, T):
    """Return IV (%) for a given strike/spot/time-to-expiry, with smile + term structure."""
    days_left = max(T * 365, 0.1)
    moneyness_pct = (strike - spot) / max(spot, 1) * 100  # + = OTM call side, - = OTM put side
    term_scale = 1.0 / math.sqrt(days_left + 1)

    atm_iv = BASE_ATM_IV + TERM_COEFF * term_scale
    skew = (-SKEW_SLOPE * moneyness_pct + SKEW_CURV * moneyness_pct ** 2) * term_scale
    iv = atm_iv + skew
    return round(min(max(iv, 8.0), 60.0), 2)

def calculate_option_price(S, K, T, r, q, sigma, option_type='call'):
    MIN_PRICE = 0.05  # exchange tick floor -- live prices never show as zero
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        intrinsic = max(S - K, 0) if option_type == 'call' else max(K - S, 0)
        return max(intrinsic, MIN_PRICE), 0.0, 0.0, 0.0, 0.0
    try:
        d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        if option_type == 'call':
            price = S * math.exp(-q * T) * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
            delta = math.exp(-q * T) * norm.cdf(d1)
        else:
            price = K * math.exp(-r * T) * norm.cdf(-d2) - S * math.exp(-q * T) * norm.cdf(-d1)
            delta = -math.exp(-q * T) * norm.cdf(-d1)
        gamma = math.exp(-q * T) * norm.pdf(d1) / (S * sigma * math.sqrt(T))
        theta = (-(S * sigma * math.exp(-q * T) * norm.pdf(d1)) / (2 * math.sqrt(T))
                 - r * K * math.exp(-r * T) * (norm.cdf(d2) if option_type == 'call' else norm.cdf(-d2))
                 + q * S * math.exp(-q * T) * (norm.cdf(d1) if option_type == 'call' else -norm.cdf(-d1)))
        vega = S * math.exp(-q * T) * math.sqrt(T) * norm.pdf(d1) / 100
        return max(price, MIN_PRICE), delta, gamma, theta / 365, vega
    except Exception:
        return MIN_PRICE, 0.0, 0.0, 0.0, 0.0

def generate_option_chain(spot_price, prev_spot, T):
    base_strike = round(spot_price / 100) * 100
    strikes = list(range(int(base_strike) - 500, int(base_strike) + 600, 100))
    chain_data = []
    for strike in strikes:
        iv = get_iv_surface(strike, spot_price, T) / 100
        call_price, call_delta, call_gamma, call_theta, call_vega = calculate_option_price(
            spot_price, strike, T, 0.068, 0.014, iv, 'call')
        put_price, put_delta, put_gamma, put_theta, put_vega = calculate_option_price(
            spot_price, strike, T, 0.068, 0.014, iv, 'put')
        prev_call, _, _, _, _ = calculate_option_price(prev_spot, strike, T + 1/365, 0.068, 0.014, iv, 'call')
        prev_put, _, _, _, _ = calculate_option_price(prev_spot, strike, T + 1/365, 0.068, 0.014, iv, 'put')
        call_pct = ((call_price - prev_call) / prev_call * 100) if prev_call > 0.1 else 0
        put_pct = ((put_price - prev_put) / prev_put * 100) if prev_put > 0.1 else 0
        chain_data.append({
            'Strike': strike,
            'CE Price': round(call_price, 2),
            'CE %': f"{call_pct:+.1f}%",
            'CE Δ': round(call_delta, 3),
            'CE Γ': round(call_gamma, 4),
            'PE Price': round(put_price, 2),
            'PE %': f"{put_pct:+.1f}%",
            'PE Δ': round(put_delta, 3),
            'PE Γ': round(put_gamma, 4),
            'IV %': round(iv * 100, 1)
        })
    return pd.DataFrame(chain_data)

def get_time_to_expiry(current_dt, expiry_dt):
    """Precise fractional year to expiry -- exact elapsed seconds, not rounded to whole days."""
    seconds_left = (expiry_dt - current_dt).total_seconds()
    min_seconds = 60  # floor at 1 minute so pricing never divides by ~0
    return max(seconds_left, min_seconds) / (365 * 24 * 3600)

def get_moneyness_label(option_type, strike, spot, atm_strike):
    """Simple ITM / ATM / OTM classification for the order-entry preview.
    Purely a usability aid -- does not affect pricing or margin."""
    if strike == atm_strike:
        return "ATM"
    if option_type == "CE":
        return "ITM" if strike < spot else "OTM"
    else:  # PE
        return "ITM" if strike > spot else "OTM"

def create_chart(bar_data, current_price, session_start=None):
    """Candlestick chart of the 5-minute bars revealed so far, labeled Day-1..Day-N
    on a category axis so there is never a gap/break between sessions."""
    fig = go.Figure()
    if len(bar_data) > 0:
        if 'day_num' in bar_data.columns:
            x_labels = bar_data.apply(lambda r: f"Day-{int(r['day_num'])} {r['datetime'].strftime('%H:%M')}", axis=1)
        else:
            x_labels = bar_data['datetime'].dt.strftime('%H:%M')
        fig.add_trace(go.Candlestick(
            x=x_labels,
            open=bar_data['open'], high=bar_data['high'],
            low=bar_data['low'], close=bar_data['close'],
            name="NIFTY",
            increasing_line_color='#00a86b',
            decreasing_line_color='#e74c3c',
            line_width=1, showlegend=False, whiskerwidth=0.3
        ))
    fig.add_hline(y=current_price, line_dash="dash", line_color="#0a2540", opacity=0.4, line_width=1)
    fig.update_layout(
        template='plotly_white',
        height=420,
        margin=dict(l=10, r=10, t=10, b=10),
        xaxis=dict(type='category', rangeslider=dict(visible=True, thickness=0.06),
                   gridcolor='#f0f0f0', nticks=12),
        yaxis=dict(gridcolor='#f0f0f0', tickformat=',.2f'),
        font=dict(size=11),
        plot_bgcolor='white',
        paper_bgcolor='white'
    )
    return fig

def price_option_leg(option_type, strike, spot, T, r=0.068, q=0.014):
    """Price a single CE/PE leg at an arbitrary strike using the same BSM + IV-surface
    machinery as the live option chain. Used by the strategy builder so it can price
    OTM/ITM legs that may sit outside the currently displayed chain window."""
    iv = get_iv_surface(strike, spot, T) / 100
    opt = 'call' if option_type == 'CE' else 'put'
    price, _, _, _, _ = calculate_option_price(spot, strike, T, r, q, iv, opt)
    return round(price, 2)

def build_strategy_legs(legs_def, atm_strike, offset_pts, base_lots, lot_size, spot, T_current):
    """Expand a strategy definition (list of (side, type, strike_key, lot_multiplier))
    into concrete order items, ready to drop into the basket. 'FUT' legs are a synthetic
    long/short underlying position (see STRATEGY_FUT_NOTE) -- not a real stock/futures trade."""
    items = []
    for side, typ, strike_key, mult in legs_def:
        lots = base_lots * mult
        qty = lots * lot_size
        if typ == 'FUT':
            strike = 0
            price = spot
        else:
            if strike_key in ('OTM_CALL', 'UPPER'):
                strike = atm_strike + offset_pts
            elif strike_key in ('OTM_PUT', 'LOWER'):
                strike = atm_strike - offset_pts
            else:  # 'ATM'
                strike = atm_strike
            price = price_option_leg(typ, strike, spot, T_current)
        items.append({
            'side': side, 'type': typ, 'strike': strike, 'lots': lots,
            'quantity': qty, 'price': price, 'order_type': 'MARKET', 'ltp': price
        })
    return items

def instrument_label(strike, typ):
    """Human-readable instrument name for positions/basket rows -- handles the
    synthetic underlying ('FUT') leg used by holdings-based strategies."""
    if typ == 'FUT':
        return "NIFTY (Underlying)"
    return f"NIFTY {strike} {typ}"

# ---------- Glossary tooltips: hover/tap definitions, shown once per session ----------
GLOSSARY = {
    'theta': "Theta (Θ) — how much an option's value is expected to shrink each day, just from time passing, if nothing else changes.",
    'delta': "Delta (Δ) — how much an option's price is expected to move for a 1-point move in the underlying.",
    'gamma': "Gamma (Γ) — how fast Delta itself changes as the underlying moves; largest near ATM strikes close to expiry.",
    'vega': "Vega — how much an option's price is expected to move for a 1 percentage-point change in implied volatility.",
    'moneyness': "Moneyness — whether a strike is in-the-money (ITM), at-the-money (ATM), or out-of-the-money (OTM) relative to the current spot price.",
    'naked short': "Naked short — selling an option with no offsetting position to cap the loss if the market moves against you; margin is charged accordingly.",
    'breakeven': "Breakeven — the underlying price at expiry where the position's total P&L is exactly zero.",
    'margin': "Margin — capital the broker blocks as a safety buffer against potential losses on a position, especially short options.",
}

def glossary_term(key, display_text=None):
    """Render `display_text` (or the term itself) with a hover/tap glossary tooltip
    the first time this term is used anywhere in the session; plain text afterwards,
    so the UI doesn't stay cluttered with dotted-underline decoration everywhere."""
    display_text = display_text or key
    shown = st.session_state.setdefault('_glossary_shown', set())
    if key in shown or key not in GLOSSARY:
        return display_text
    shown.add(key)
    safe_def = GLOSSARY[key].replace('"', '&quot;')
    return (f'<span class="glossary-term" tabindex="0">{display_text}'
            f'<span class="glossary-tip">{safe_def}</span></span>')

# ---------- Payoff-at-expiry diagram (learning-value: makes strategy shape visible) ----------

def _payoff_at_expiry(items, spot_points):
    """Vectorised P&L at expiry for a list of legs, across an array of hypothetical
    spot prices. FUT legs use their stored 'price' as the entry spot; CE/PE legs use
    it as the entry premium. Ignores time value entirely (by definition, at expiry)."""
    spot_points = np.asarray(spot_points, dtype=float)
    payoff = np.zeros_like(spot_points)
    for it in items:
        sign = 1 if it['side'] == 'Buy' else -1
        qty = it.get('quantity', it.get('lots', 1))
        entry = float(it.get('price', it.get('entry_price', 0)))
        if it['type'] == 'FUT':
            payoff += sign * (spot_points - entry) * qty
        elif it['type'] == 'CE':
            intrinsic = np.maximum(spot_points - it['strike'], 0.0)
            payoff += sign * (intrinsic - entry) * qty
        else:  # PE
            intrinsic = np.maximum(it['strike'] - spot_points, 0.0)
            payoff += sign * (intrinsic - entry) * qty
    return payoff

def analyze_payoff(items):
    """
    Exact max profit / max loss / breakeven(s) for a set of legs, computed analytically
    rather than by scanning a chart -- payoff at expiry is piecewise-linear in spot with
    kinks only at strikes, so evaluating at 0, every strike, and a far-out proxy for
    'spot -> infinity' is enough to capture every extreme point and every zero-crossing.
    Returns a dict: max_profit, max_loss (None means unbounded), breakevens (list),
    unlimited_upside (bool), unlimited_downside (bool).
    """
    option_items = [it for it in items if it['type'] in ('CE', 'PE')]
    strikes = sorted(set(it['strike'] for it in option_items))
    has_fut = any(it['type'] == 'FUT' for it in items)
    if not strikes and not has_fut:
        return {'max_profit': 0.0, 'max_loss': 0.0, 'breakevens': [], 'unlimited_upside': False, 'unlimited_downside': False}

    far = (max(strikes) if strikes else 24000) * 5 + 50000  # a safely-past-any-strike proxy for "infinity"
    candidates = sorted(set([0.0] + strikes + [far]))
    cand_arr = np.array(candidates)
    payoff_cand = _payoff_at_expiry(items, cand_arr)

    # Net slope as spot -> +infinity: only long/short CE and FUT legs contribute
    # (PE intrinsic flattens to 0, so puts never create unlimited upside/downside).
    upside_slope = sum(
        (1 if it['side'] == 'Buy' else -1) * it.get('quantity', it.get('lots', 1))
        for it in items if it['type'] in ('CE', 'FUT')
    )
    unlimited_upside = upside_slope > 1e-9   # net long calls/underlying -> unbounded profit
    unlimited_downside = upside_slope < -1e-9  # net short calls/underlying -> unbounded loss

    raw_max = float(np.max(payoff_cand))
    raw_min = float(np.min(payoff_cand))
    max_profit = None if unlimited_upside else raw_max
    max_loss = None if unlimited_downside else raw_min

    breakevens = []
    for i in range(len(candidates) - 1):
        y0, y1 = payoff_cand[i], payoff_cand[i + 1]
        if abs(y0) < 1e-6:
            breakevens.append(candidates[i])
        elif y0 * y1 < 0:
            x0, x1 = candidates[i], candidates[i + 1]
            breakevens.append(x0 + (-y0 / (y1 - y0)) * (x1 - x0))
    if abs(payoff_cand[-1]) < 1e-6:
        breakevens.append(candidates[-1])
    breakevens = sorted(set(round(b, 0) for b in breakevens if b < far))

    return {
        'max_profit': max_profit, 'max_loss': max_loss, 'breakevens': breakevens,
        'unlimited_upside': unlimited_upside, 'unlimited_downside': unlimited_downside,
    }

def render_payoff_diagram(items, spot, title="Payoff at Expiry"):
    """Build a Plotly payoff-at-expiry chart for a set of legs (single order or full
    basket) and return it alongside the analytical max profit/loss/breakeven stats."""
    stats = analyze_payoff(items)
    option_strikes = [it['strike'] for it in items if it['type'] in ('CE', 'PE')]
    anchor_points = option_strikes + [spot]
    lo = max(0, min(anchor_points) - 1000)
    hi = max(anchor_points) + 1000
    spot_range = np.linspace(lo, hi, 250)
    payoff = _payoff_at_expiry(items, spot_range)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=spot_range, y=payoff, mode='lines', name='P&L at expiry',
        line=dict(color='#0a2540', width=2.5),
        fill='tozeroy',
        fillcolor='rgba(0,168,107,0.12)',
    ))
    # Re-shade the loss region red by overlaying a masked negative-only trace
    neg_payoff = np.where(payoff < 0, payoff, 0)
    fig.add_trace(go.Scatter(
        x=spot_range, y=neg_payoff, mode='lines', line=dict(width=0),
        fill='tozeroy', fillcolor='rgba(231,76,60,0.12)', showlegend=False, hoverinfo='skip'
    ))
    fig.add_hline(y=0, line_dash="dash", line_color="#888", line_width=1)
    fig.add_vline(x=spot, line_dash="dot", line_color="#0a2540", opacity=0.6,
                  annotation_text="Spot now", annotation_position="top")
    for be in stats['breakevens']:
        fig.add_vline(x=be, line_dash="dash", line_color="#8a6100", opacity=0.5)
        fig.add_annotation(x=be, y=0, text=f"BE {be:,.0f}", showarrow=False,
                            yshift=14, font=dict(size=10, color="#8a6100"))
    fig.update_layout(
        title=dict(text=title, font=dict(size=13)),
        height=280, margin=dict(l=40, r=20, t=36, b=30),
        xaxis_title="NIFTY at expiry", yaxis_title="P&L (₹)",
        plot_bgcolor='white', paper_bgcolor='white',
        showlegend=False,
    )
    fig.update_xaxes(showgrid=True, gridcolor='#eee')
    fig.update_yaxes(showgrid=True, gridcolor='#eee', zeroline=False)
    return fig, stats

# ---- Strategy catalogue for the "ready-made strategy" builder ----
# Each leg: (side 'Buy'/'Sell', type 'CE'/'PE'/'FUT', strike_key, lot_multiplier)
# strike_key: 'ATM' | 'OTM_CALL' | 'OTM_PUT' | 'LOWER' | 'UPPER' (ignored for 'FUT')
STRATEGY_FUT_NOTE = (
    "Uses a synthetic underlying leg (this app has no real stock/futures holding) — "
    "it is margined like a futures position for simplicity, not purchased outright."
)
STRATEGY_CATALOGUE = {
    "A. Strategies with Existing Holdings": {
        "Protective Put  (Long Asset + Long ATM Put)": [
            ("Buy", "FUT", None, 1), ("Buy", "PE", "ATM", 1),
        ],
        "Protective Call  (Short Asset + Long ATM Call)": [
            ("Sell", "FUT", None, 1), ("Buy", "CE", "ATM", 1),
        ],
        "Covered Call Writing  (Long Asset + Short ATM Call)": [
            ("Buy", "FUT", None, 1), ("Sell", "CE", "ATM", 1),
        ],
        "Covered Put Writing  (Short Asset + Short ATM Put)": [
            ("Sell", "FUT", None, 1), ("Sell", "PE", "ATM", 1),
        ],
    },
    "B. Vertical & Box Spreads": {
        "Bull Call Spread  (Long ATM Call + Short OTM Call)": [
            ("Buy", "CE", "ATM", 1), ("Sell", "CE", "OTM_CALL", 1),
        ],
        "Bear Put Spread  (Long ATM Put + Short OTM Put)": [
            ("Buy", "PE", "ATM", 1), ("Sell", "PE", "OTM_PUT", 1),
        ],
        "Bear Call Spread  (Short ATM Call + Long OTM Call)": [
            ("Sell", "CE", "ATM", 1), ("Buy", "CE", "OTM_CALL", 1),
        ],
        "Bull Put Spread  (Short ATM Put + Long OTM Put)": [
            ("Sell", "PE", "ATM", 1), ("Buy", "PE", "OTM_PUT", 1),
        ],
        "Short Box Spread  (Short ATM Call + Long OTM Call + Short ATM Put + Long OTM Put)": [
            ("Sell", "CE", "ATM", 1), ("Buy", "CE", "OTM_CALL", 1),
            ("Sell", "PE", "ATM", 1), ("Buy", "PE", "OTM_PUT", 1),
        ],
        "Long Box Spread  (Long ATM Call + Short OTM Call + Long ATM Put + Short OTM Put)": [
            ("Buy", "CE", "ATM", 1), ("Sell", "CE", "OTM_CALL", 1),
            ("Buy", "PE", "ATM", 1), ("Sell", "PE", "OTM_PUT", 1),
        ],
    },
    "C. Butterfly Spreads": {
        "Long Butterfly — Call  (Buy Lower + Sell 2x ATM + Buy Upper)": [
            ("Buy", "CE", "LOWER", 1), ("Sell", "CE", "ATM", 2), ("Buy", "CE", "UPPER", 1),
        ],
        "Short Butterfly — Call  (Sell Lower + Buy 2x ATM + Sell Upper)": [
            ("Sell", "CE", "LOWER", 1), ("Buy", "CE", "ATM", 2), ("Sell", "CE", "UPPER", 1),
        ],
        "Long Butterfly — Put  (Buy Lower + Sell 2x ATM + Buy Upper)": [
            ("Buy", "PE", "LOWER", 1), ("Sell", "PE", "ATM", 2), ("Buy", "PE", "UPPER", 1),
        ],
        "Short Butterfly — Put  (Sell Lower + Buy 2x ATM + Sell Upper)": [
            ("Sell", "PE", "LOWER", 1), ("Buy", "PE", "ATM", 2), ("Sell", "PE", "UPPER", 1),
        ],
    },
    "D. Straddles, Strangles, Strips & Straps": {
        "Long Straddle  (Long ATM Call + Long ATM Put)": [
            ("Buy", "CE", "ATM", 1), ("Buy", "PE", "ATM", 1),
        ],
        "Short Straddle  (Short ATM Call + Short ATM Put)": [
            ("Sell", "CE", "ATM", 1), ("Sell", "PE", "ATM", 1),
        ],
        "Long Strangle  (Long OTM Call + Long OTM Put)": [
            ("Buy", "CE", "OTM_CALL", 1), ("Buy", "PE", "OTM_PUT", 1),
        ],
        "Short Strangle  (Short OTM Call + Short OTM Put)": [
            ("Sell", "CE", "OTM_CALL", 1), ("Sell", "PE", "OTM_PUT", 1),
        ],
        "Long Strip  (Long ATM Call + Long 2x ATM Put)": [
            ("Buy", "CE", "ATM", 1), ("Buy", "PE", "ATM", 2),
        ],
        "Short Strip  (Short ATM Call + Short 2x ATM Put)": [
            ("Sell", "CE", "ATM", 1), ("Sell", "PE", "ATM", 2),
        ],
        "Long Strap  (Long 2x ATM Call + Long ATM Put)": [
            ("Buy", "CE", "ATM", 2), ("Buy", "PE", "ATM", 1),
        ],
        "Short Strap  (Short 2x ATM Call + Short ATM Put)": [
            ("Sell", "CE", "ATM", 2), ("Sell", "PE", "ATM", 1),
        ],
    },
}

def compute_position_greeks(positions, spot, T, r=0.068, q=0.014):
    net = {'delta': 0.0, 'gamma': 0.0, 'theta': 0.0, 'vega': 0.0}
    for pos in positions:
        sign = 1 if pos['side'] == 'Buy' else -1
        qty = pos.get('quantity', st.session_state.lot_size)
        if pos['type'] == 'FUT':
            # Synthetic underlying: linear payoff, delta = 1 per share, no gamma/theta/vega.
            net['delta'] += sign * qty
            continue
        iv = get_iv_surface(pos['strike'], spot, T) / 100
        opt = 'call' if pos['type'] == 'CE' else 'put'
        _, delta, gamma, theta, vega = calculate_option_price(spot, pos['strike'], T, r, q, iv, opt)
        net['delta'] += sign * delta * qty
        net['gamma'] += sign * gamma * qty
        net['theta'] += sign * theta * qty
        net['vega'] += sign * vega * qty
    return net

def consolidate_positions(positions, current_price, T_current, chain_df):
    consolidated = {}
    for pos in positions:
        key = f"{pos['strike']}_{pos['type']}"
        if key not in consolidated:
            consolidated[key] = {
                'strike': pos['strike'], 'type': pos['type'],
                'net_qty': 0, 'total_cost': 0.0, 'entries': []
            }
        sign = 1 if pos['side'] == 'Buy' else -1
        qty = pos.get('quantity', st.session_state.lot_size)
        price = pos['entry_price']
        consolidated[key]['net_qty'] += sign * qty
        consolidated[key]['total_cost'] += sign * qty * price
        consolidated[key]['entries'].append({'side': pos['side'], 'qty': qty, 'price': price})

    result = []
    for key, data in consolidated.items():
        if data['net_qty'] != 0:
            avg_price = data['total_cost'] / data['net_qty'] if data['net_qty'] != 0 else 0
            if data['type'] == 'FUT':
                cur_px = float(current_price)  # synthetic underlying marks at spot, not the option chain
            else:
                row = chain_df[chain_df['Strike'] == data['strike']] if chain_df is not None else pd.DataFrame()
                cur_px = 0.0
                if len(row) > 0:
                    cur_px = float(row.iloc[0]['CE Price'] if data['type'] == 'CE' else row.iloc[0]['PE Price'])
            pnl = (cur_px - avg_price) * data['net_qty']
            result.append({
                'strike': data['strike'], 'type': data['type'],
                'net_qty': data['net_qty'], 'avg_price': avg_price,
                'current_price': cur_px, 'pnl': pnl,
                'side': 'Buy' if data['net_qty'] > 0 else 'Sell'
            })
    return result

def calculate_realistic_margin(items, spot, lot_size):
    """
    Broker-style margin: per-leg naked rates with spread benefit.
    - Long options: premium only (already paid) – no extra margin.
    - Short naked (no offsetting long/short of the same type at another strike):
      higher of (premium * 3) or 10% notional + SPAN-like add-on.
    - Defined-risk combinations (a short leg offset by a long OR another short leg
      of the same type at a different strike -- covers verticals, box spreads, and
      butterflies): reduced width-based margin instead of full naked margin, applied
      per unit of quantity actually covered (partial-quantity legs, e.g. a butterfly's
      2x middle leg, are covered piece by piece against each available partner).
    - Short straddle/strangle: modest offset benefit on whatever naked CE/PE
      quantity remains after the above.
    - Synthetic underlying ('FUT') legs: flat futures-style margin (both long and
      short), since this app has no real cash purchase of the underlying — a
      teaching simplification. Note this means a "covered" call/put here is margined
      as (underlying margin + naked short-option margin), not the reduced margin a
      real covered position gets from actually owning the stock — a further
      simplification worth calling out to students.
    """
    if not items:
        return 0.0

    FUT_MARGIN_RATE = 0.12  # simplified futures-style margin for a synthetic underlying leg

    legs = {}
    for item in items:
        key = (item['strike'], item['type'])
        qty = item.get('quantity', item.get('lots', 1) * lot_size)
        sign = 1 if item['side'] == 'Buy' else -1
        prem = float(item.get('price', item.get('entry_price', 0)))
        if key not in legs:
            legs[key] = {'qty': 0, 'premium': prem}
        legs[key]['qty'] += sign * qty
        legs[key]['premium'] = prem

    total_margin = 0.0
    short_legs = []  # mutable remaining-qty trackers
    long_legs = []
    for (strike, typ), data in legs.items():
        qty = data['qty']
        prem = data['premium']
        if typ == 'FUT':
            if qty != 0:
                total_margin += abs(qty) * spot * FUT_MARGIN_RATE
            continue
        if qty > 0:
            long_legs.append({'strike': strike, 'type': typ, 'qty': qty, 'premium': prem})
        elif qty < 0:
            short_legs.append({'strike': strike, 'type': typ, 'qty': abs(qty), 'premium': prem})

    # Pass 1: cover short quantity against an offsetting LONG leg of the same type
    # at a different strike -- the genuine defined-risk case (bull/bear spreads,
    # box spreads, butterflies). The long leg caps the short leg's loss beyond its
    # strike, so margin only needs to cover the strike width, not the full notional.
    for a in short_legs:
        for b in long_legs:
            if a['qty'] <= 0:
                break
            if b['qty'] <= 0 or b['type'] != a['type'] or b['strike'] == a['strike']:
                continue
            width = abs(a['strike'] - b['strike'])
            covered = min(a['qty'], b['qty'])
            total_margin += width * covered * 0.15 + a['premium'] * covered
            a['qty'] -= covered
            b['qty'] -= covered

    # Uncovered short options remain naked. Two short options at different strikes do
    # not cap one another's loss, so they must NOT receive defined-risk spread margin.

    # Whatever short quantity is left after long-leg offsets is genuinely naked.
    for a in short_legs:
        if a['qty'] > 0:
            notional = a['strike'] * a['qty']
            total_margin += max(a['premium'] * a['qty'] * 3.0, notional * 0.10) + spot * 0.015 * a['qty']

    ce_short = sum(a['qty'] for a in short_legs if a['type'] == 'CE')
    pe_short = sum(a['qty'] for a in short_legs if a['type'] == 'PE')
    if ce_short > 0 and pe_short > 0:
        offset_qty = min(ce_short, pe_short)
        total_margin = max(0.0, total_margin - offset_qty * spot * 0.005)

    return round(max(total_margin, 0), 0)


def _json_serial(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(f"Type {type(obj)} not serializable")


def save_session_state():
    """Persist progress and, when needed, the current realized-session analytics."""
    if supabase_enabled():
        try:
            save_progress_snapshot()
        except Exception as exc:
            print(f"[Supabase] Progress snapshot save failed: {exc}")
        try:
            sync_live_session_metrics()
        except Exception as exc:
            print(f"[Supabase] Live session analytics save failed: {exc}")
        return
    keys = [
        'current_index', 'playing', 'speed', 'basket', 'positions', 'tradebook',
        'pending_limits', 'realized_pnl', 'max_reached_index', 'data_loaded',
        'prev_day_close', 'start_time', 'session_end', 'expiry_dt', 'scale_factor',
        'lot_size', 'prev_scaled_close', 'trading_locked', 'session_finished',
        'report_generated', 'report_path', 'current_price', 'T_current',
        'starting_capital', 'peak_margin_used', 'session_start_wall',
        'data_source_choice', 'day_close_map', 'target_nifty_level'
    ]
    payload = {}
    for k in keys:
        if k in st.session_state:
            v = st.session_state[k]
            if isinstance(v, (datetime, date)):
                payload[k] = v.isoformat()
            else:
                try:
                    json.dumps(v, default=_json_serial)
                    payload[k] = v
                except Exception:
                    pass
    if st.session_state.get('simulated_data') is not None:
        try:
            df = st.session_state.simulated_data
            payload['_sim_records'] = df.to_dict(orient='records')
        except Exception:
            pass
    try:
        with open(PERSIST_PATH, 'w') as f:
            json.dump(payload, f, default=_json_serial)
    except Exception:
        pass


def load_session_state():
    """Restore local state only in non-Supabase/local-development mode."""
    if supabase_enabled():
        return False
    if not os.path.exists(PERSIST_PATH):
        return False
    try:
        with open(PERSIST_PATH, 'r') as f:
            payload = json.load(f)
    except Exception:
        return False
    if not payload.get('data_loaded'):
        return False
    if '_sim_records' in payload:
        try:
            recs = payload.pop('_sim_records')
            df = pd.DataFrame(recs)
            if 'datetime' in df.columns:
                df['datetime'] = pd.to_datetime(df['datetime'])
            st.session_state.simulated_data = df
            st.session_state.df_day_scaled = df
            st.session_state.df_raw = df
        except Exception:
            return False
    for k, v in payload.items():
        if k in ('start_time', 'session_end', 'expiry_dt', 'session_start_wall'):
            try:
                st.session_state[k] = datetime.fromisoformat(v) if v else None
            except Exception:
                st.session_state[k] = v
        else:
            st.session_state[k] = v
    if 'cart' in st.session_state and not st.session_state.get('basket'):
        st.session_state.basket = st.session_state.pop('cart', [])
    return True


HOLD_DAYS = 22

def get_trading_day_offsets(n_days, anchor_date=None):
    """Calendar-day offsets (1, 2, 3, ...) that fall on a real weekday, skipping
    Sat/Sun, keeping their original offset number -- e.g. 1,2,3,4,5,8,9,10,11,12,..."""
    anchor = anchor_date or date.today()
    offsets = []
    d = 0
    while len(offsets) < n_days:
        d += 1
        if (anchor + timedelta(days=d)).weekday() < 5:
            offsets.append(d)
    return offsets

def compute_hold_to_expiry_table(spot, hold_days=HOLD_DAYS):
    """
    For every CLOSED position: reprice day-by-day as if it had been held instead
    of exited, using the current spot and the IV surface at that day's residual
    maturity. Day offsets skip real Saturdays/Sundays (provision for weekend
    market holidays) while keeping their original calendar-day numbering, e.g.
    1,2,3,4,5,8,9,10,11,12,... Last row is what the user actually realized on exit.
    Underlying ('FUT') legs are excluded -- they have no time decay to illustrate,
    which is the whole point of this table.
    """
    closed = [t for t in st.session_state.tradebook if t['status'] == 'Closed' and t['type'] in ('CE', 'PE')]
    if not closed:
        return None, []

    day_offsets = get_trading_day_offsets(hold_days)

    labels, columns = [], []
    for i, t in enumerate(closed):
        label = f"{i+1}. {t['strike']}{t['type']} {t['side']}"
        labels.append(label)
        opt = 'call' if t['type'] == 'CE' else 'put'
        sign = 1 if t['side'] == 'Buy' else -1
        qty = t['qty']
        col = []
        for d in day_offsets:
            T_d = d / 365
            iv_d = get_iv_surface(t['strike'], spot, T_d) / 100
            price_d, *_ = calculate_option_price(spot, t['strike'], T_d, 0.068, 0.014, iv_d, opt)
            col.append(sign * (price_d - t['entry_price']) * qty)
        col.append(t['pnl'])  # actual realized P&L, as the final row
        columns.append(col)

    index = [f"Day {d}" for d in day_offsets] + ["Actual (Exit)"]
    table = pd.DataFrame({lbl: col for lbl, col in zip(labels, columns)}, index=index)
    return table, labels

def summarize_hold_vs_exit(hold_table):
    """One-line, plain-language takeaway from the hold-to-expiry table: compares each
    closed trade's actual realized P&L against the P&L it would have shown on the
    last simulated hold-day, and reports how often holding longer would have won."""
    if hold_table is None or len(hold_table) < 2 or hold_table.shape[1] == 0:
        return None
    last_hold_row = hold_table.iloc[-2]   # last hypothetical hold day
    actual_row = hold_table.iloc[-1]      # actual realized P&L
    n = len(last_hold_row)
    better_to_hold = int((last_hold_row > actual_row).sum())
    better_to_exit = int((actual_row >= last_hold_row).sum())
    avg_diff = float((last_hold_row - actual_row).mean())
    if better_to_hold > better_to_exit:
        verdict = f"holding longer would have done better in {better_to_hold} of {n} trades"
    elif better_to_exit > better_to_hold:
        verdict = f"exiting when you did beat holding longer in {better_to_exit} of {n} trades"
    else:
        verdict = f"holding and exiting were about evenly split across your {n} trades"
    return (f"{verdict.capitalize()} — on average, holding to Day {HOLD_DAYS} would have changed "
            f"P&L by ₹{avg_diff:+,.0f} per trade. There's no universal rule here: it depends on "
            f"which way the market moved after you exited, so use this to reflect on your own exits, "
            f"not as a signal to always hold longer.")

def generate_pdf_report():
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", "B", 16)
    pdf.cell(0, 10, "Option Market Simulator - Performance Report", ln=True, align="C")
    pdf.ln(8)
    pdf.set_font("Arial", "", 11)
    student_name = str(st.session_state.get("student_name") or "-")
    student_id = str(st.session_state.get("student_id") or "-")
    pdf.cell(0, 7, f"Student Name: {student_name}", ln=True)
    pdf.cell(0, 7, f"Student ID / Roll No.: {student_id}", ln=True)
    pdf.cell(0, 7, f"Date: {date.today().strftime('%d-%m-%Y')}", ln=True)
    pdf.cell(0, 7, f"App Version: {APP_VERSION}", ln=True)
    pdf.cell(0, 7, f"Session No.: {st.session_state.get('session_no') or '-'}", ln=True)
    pdf.cell(0, 7, f"Learning Focus: {st.session_state.get('strategy_focus') or 'Open practice'}", ln=True)
    pdf.cell(0, 7, f"Total Trades: {len(st.session_state.tradebook)}", ln=True)
    pdf.ln(4)
    closed_pnl = sum(t['pnl'] for t in st.session_state.tradebook if t['status'] == 'Closed')
    open_pnl = 0.0
    if st.session_state.positions and st.session_state.chain_df is not None:
        cons = consolidate_positions(st.session_state.positions, st.session_state.current_price,
                                     st.session_state.T_current, st.session_state.chain_df)
        open_pnl = sum(p['pnl'] for p in cons)
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 8, "P&L Summary", ln=True)
    pdf.set_font("Arial", "", 11)
    pdf.cell(0, 7, f"Realized P&L: {closed_pnl:+.2f}", ln=True)
    pdf.cell(0, 7, f"Open P&L: {open_pnl:+.2f}", ln=True)
    total_pdf_pnl = closed_pnl + open_pnl
    starting_pdf = float(st.session_state.get("starting_capital", 10000000.0))
    pdf.cell(0, 7, f"Total P&L: {total_pdf_pnl:+.2f}", ln=True)
    pdf.cell(0, 7, f"Return: {(total_pdf_pnl / starting_pdf * 100.0) if starting_pdf else 0.0:+.2f}%", ln=True)
    pdf.cell(0, 7, f"Maximum Drawdown: {float(st.session_state.get('max_drawdown', 0.0)):.2f} ({float(st.session_state.get('max_drawdown_pct', 0.0)):.2f}%)", ln=True)
    pdf.ln(4)
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 8, "Tradebook", ln=True)
    pdf.set_font("Arial", "B", 9)
    for col, w in [("Time", 25), ("Instrument", 30), ("Side", 18), ("Qty", 18), ("Entry", 25), ("Exit", 25), ("P&L", 25)]:
        pdf.cell(w, 7, col, 1)
    pdf.ln()
    pdf.set_font("Arial", "", 8)
    for t in st.session_state.tradebook:
        pdf.cell(25, 6, t['entry_time'], 1)
        pdf.cell(30, 6, "Underlying" if t['type'] == 'FUT' else f"{t['strike']} {t['type']}", 1)
        pdf.cell(18, 6, t['side'], 1)
        pdf.cell(18, 6, str(t['qty']), 1)
        pdf.cell(25, 6, f"{t['entry_price']:.2f}", 1)
        pdf.cell(25, 6, f"{t['exit_price']:.2f}" if t['status'] == 'Closed' else "-", 1)
        pdf.cell(25, 6, f"{t['pnl']:+.2f}" if t['status'] == 'Closed' else "Open", 1)
        pdf.ln()

    # Hold-to-Day-22 hypothetical table
    hold_table, labels = compute_hold_to_expiry_table(st.session_state.current_price)
    if hold_table is not None:
        pdf.ln(4)
        pdf.set_font("Arial", "B", 12)
        pdf.cell(0, 8, f"Hypothetical P&L if Held {HOLD_DAYS} Days (vs Actual Exit)", ln=True)
        pdf.set_font("Arial", "", 8)
        pdf.multi_cell(0, 5, "Columns: " + " | ".join(labels))
        pdf.ln(1)
        n_cols = len(labels)
        col_w = min(28, max(18, 180 // max(n_cols, 1)))
        pdf.set_font("Arial", "B", 8)
        pdf.cell(22, 6, "Row", 1)
        for i in range(n_cols):
            pdf.cell(col_w, 6, f"Pos {i+1}", 1)
        pdf.ln()
        pdf.set_font("Arial", "", 7)
        for row_label, row in hold_table.iterrows():
            pdf.cell(22, 6, row_label, 1)
            for val in row:
                pdf.cell(col_w, 6, f"{val:+.1f}", 1)
            pdf.ln()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"performance_report_{timestamp}.pdf"
    filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    try:
        pdf.output(filepath)
    except Exception:
        filepath = os.path.join("/tmp", filename)
        pdf.output(filepath)
    return filepath, filename


def _settle_all_cash(spot, current_dt):
    """Cash-settle every open trade at intrinsic value and finalize analytics."""
    realized_add = 0.0

    for t in st.session_state.get("tradebook", []):
        if t.get("status") != "Open":
            continue

        typ = t.get("type")
        strike = float(t.get("strike", 0.0) or 0.0)
        if typ == "FUT":
            intrinsic = float(spot)
        elif typ == "CE":
            intrinsic = max(float(spot) - strike, 0.0)
        else:
            intrinsic = max(strike - float(spot), 0.0)

        t["exit_time"] = current_dt.strftime("%H:%M:%S")
        t["exit_price"] = float(intrinsic)
        sign = 1 if t.get("side") == "Buy" else -1
        qty = int(t.get("qty", 0) or 0)
        entry = float(t.get("entry_price", 0.0) or 0.0)
        t["pnl"] = sign * (t["exit_price"] - entry) * qty
        t["status"] = "Closed"

        try:
            close_trade_record(t, current_dt, t["exit_price"], "expiry_or_week_end")
        except Exception as exc:
            print(f"[Supabase] Failed to settle trade at week end: {exc}")

        realized_add += float(t["pnl"])

    st.session_state.realized_pnl += realized_add
    _rebuild_positions_from_open_trades()
    return realized_add



def _mark_open_trade(t, current_price, chain_df):
    """Return the current market mark for one open trade."""
    typ = t.get("type")
    if typ == "FUT":
        return float(current_price)

    strike = t.get("strike")
    if chain_df is not None and len(chain_df) > 0 and strike is not None:
        row = chain_df[chain_df["Strike"] == strike]
        if len(row) > 0:
            col = "CE Price" if typ == "CE" else "PE Price"
            return float(row.iloc[0][col])

    # Defensive fallback: if a mark cannot be found, do not fabricate a gain/loss.
    return float(t.get("entry_price", 0.0))


def _close_all_open_trades_at_market(current_price, current_dt, chain_df, reason):
    """
    Close every open trade at the current simulated market mark.

    This deliberately works from the tradebook rather than consolidated positions.
    That prevents offsetting long/short trades from being skipped when net quantity is zero.
    """
    realized_add = 0.0

    for t in st.session_state.get("tradebook", []):
        if t.get("status") != "Open":
            continue

        exit_price = _mark_open_trade(t, current_price, chain_df)
        t["exit_time"] = current_dt.strftime("%H:%M:%S")
        t["exit_price"] = float(exit_price)

        sign = 1 if t.get("side") == "Buy" else -1
        qty = int(t.get("qty", 0) or 0)
        entry_price = float(t.get("entry_price", 0.0) or 0.0)
        t["pnl"] = sign * (t["exit_price"] - entry_price) * qty
        t["status"] = "Closed"

        try:
            close_trade_record(t, current_dt, t["exit_price"], reason)
        except Exception as exc:
            print(f"[Supabase] Failed to close trade record ({reason}): {exc}")

        realized_add += float(t["pnl"])

    st.session_state.realized_pnl += realized_add
    _rebuild_positions_from_open_trades()
    return realized_add


def _rebuild_positions_from_open_trades():
    """Keep the margin/Greeks position list exactly aligned with open tradebook rows."""
    rebuilt = []
    for t in st.session_state.get("tradebook", []):
        if t.get("status") != "Open":
            continue
        qty = int(t.get("qty", 0) or 0)
        lots = int(t.get("lots", 0) or 0)
        if lots <= 0:
            lot_size = max(int(st.session_state.get("lot_size", 65)), 1)
            lots = max(1, int(round(qty / lot_size))) if qty else 1
        rebuilt.append({
            "strike": t.get("strike"),
            "type": t.get("type"),
            "side": t.get("side"),
            "entry_price": float(t.get("entry_price", 0.0) or 0.0),
            "quantity": qty,
            "lots": lots,
        })
    st.session_state.positions = rebuilt


def _close_one_trade_at_market(t, current_price, current_dt, chain_df, reason="manual"):
    """Close exactly one open trade and persist the result."""
    if not t or t.get("status") != "Open":
        return 0.0

    exit_price = _mark_open_trade(t, current_price, chain_df)
    t["exit_time"] = current_dt.strftime("%H:%M:%S")
    t["exit_price"] = float(exit_price)

    sign = 1 if t.get("side") == "Buy" else -1
    qty = int(t.get("qty", 0) or 0)
    entry_price = float(t.get("entry_price", 0.0) or 0.0)
    pnl = sign * (t["exit_price"] - entry_price) * qty
    t["pnl"] = float(pnl)
    t["status"] = "Closed"

    try:
        close_trade_record(t, current_dt, t["exit_price"], reason)
    except Exception as exc:
        print(f"[Supabase] Failed to close trade record ({reason}): {exc}")

    st.session_state.realized_pnl += float(pnl)
    _rebuild_positions_from_open_trades()
    return float(pnl)

def match_pending_limits(current_price, current_dt, chain_df, lot_size):
    """
    Re-evaluate pending LIMIT orders against the live option chain LTP.
    Fills marketable orders (BUY if limit >= LTP, SELL if limit <= LTP).
    Fill price = limit price (standard limit fill convention).
    Returns number of fills.
    """
    if not st.session_state.pending_limits or chain_df is None or st.session_state.trading_locked:
        return 0
    still_pending = []
    filled = 0
    for item in st.session_state.pending_limits:
        row = chain_df[chain_df['Strike'] == item['strike']]
        if len(row) == 0:
            still_pending.append(item)
            continue
        ltp = float(row.iloc[0]['CE Price'] if item['type'] == 'CE' else row.iloc[0]['PE Price'])
        item['ltp'] = ltp
        marketable = (item['side'] == 'Buy' and item['price'] >= ltp) or                      (item['side'] == 'Sell' and item['price'] <= ltp)
        if not marketable:
            still_pending.append(item)
            continue
        # Margin check before fill
        trial = list(st.session_state.positions) + [item]
        req = calculate_realistic_margin(trial, current_price, lot_size)
        if req > st.session_state.starting_capital:
            still_pending.append(item)  # keep pending if margin insufficient
            continue
        # Fill at limit price
        st.session_state.positions.append({
            'strike': item['strike'],
            'type': item['type'],
            'side': item['side'],
            'entry_price': item['price'],
            'quantity': item['quantity'],
            'lots': item['lots']
        })

        order_id = item.get('supabase_order_id')
        trade_id = None
        if supabase_enabled():
            try:
                if order_id:
                    update_order_record(
                        order_id,
                        status="filled",
                        executed_at=_iso(current_dt),
                        fill_price=float(item['price'])
                    )
                else:
                    order_id = save_order_record(
                        item, "filled", current_dt=current_dt,
                        current_price=current_price, fill_price=item['price']
                    )
                trade_id = save_trade_record(item, current_dt, order_id, current_price=current_price)
            except Exception as exc:
                print(f"[Supabase] Failed to persist filled limit trade: {exc}")

        st.session_state.tradebook.append({
            'entry_time': current_dt.strftime('%H:%M:%S'),
            'strike': item['strike'],
            'type': item['type'],
            'side': item['side'],
            'qty': item['quantity'],
            'lots': item['lots'],
            'entry_price': item['price'],
            'entry_dt': _iso(current_dt),
            'strategy_name': _strategy_label(item),
            'capital_used': _estimate_trade_capital(item, current_price),
            'max_drawdown': 0.0,
            'max_drawdown_pct': 0.0,
            'max_profit_seen': 0.0,
            'holding_minutes': 0.0,
            'exit_time': '-',
            'exit_price': 0.0,
            'pnl': 0.0,
            'status': 'Open',
            'supabase_order_id': order_id,
            'supabase_trade_id': trade_id
        })
        filled += 1
    st.session_state.pending_limits = still_pending
    if filled:
        margin_now = calculate_realistic_margin(st.session_state.positions, current_price, lot_size)
        if margin_now > st.session_state.peak_margin_used:
            st.session_state.peak_margin_used = margin_now
        save_session_state()
    return filled


# ============ MAIN APP ============
def main():
    # Keep a local copy synchronized with the persisted/session lot size.
    # Required by pricing, margin calculations, strategy legs and pending-limit matching.
    lot_size = int(st.session_state.get("lot_size", 65))

    # Try restore persisted session on first load
    if not st.session_state.data_loaded:
        load_session_state()
        lot_size = int(st.session_state.get("lot_size", 65))
    # Defensive check: if a corrupted/partial save left data_loaded=True but no
    # actual price path, fall back to the setup screen instead of crashing later.
    if st.session_state.data_loaded and (
        st.session_state.get('simulated_data') is None or len(st.session_state.simulated_data) == 0
    ):
        st.session_state.data_loaded = False

    # Fixed Header
    st.markdown("""
    <div class="fixed-header">
        <h1>NIFTY Options Trading Simulator</h1>
        <p>DRM IMBA 2026 · Academic simulation environment · Build 2.2.0</p>
    </div>
    """, unsafe_allow_html=True)

    # Persistent disclaimer — required to stay visible on every screen (Section 4.1 of guidelines)
    st.markdown("""
    <div class="disclaimer-banner">
        Academic simulation only. Not intended for commercial or live trading use. Prices, margin and P&amp;L are simulated and do not represent a broker, exchange or live market.
    </div>
    """, unsafe_allow_html=True)

    # ===== STUDENT IDENTIFICATION / CLOUD SESSION OWNERSHIP =====
    if supabase_enabled() and not st.session_state.get("participant_id"):
        st.markdown('<div class="card identity-shell">', unsafe_allow_html=True)
        st.markdown("""
        <div class="identity-title">Student Access</div>
        <div class="identity-subtitle">
            Use your Student ID each time you return. Existing students automatically recover their stored profile,
            completed-session history and any unfinished simulator session.
        </div>
        """, unsafe_allow_html=True)
        with st.form("student_identity_form", clear_on_submit=False):
            student_id = st.text_input("Student ID / Roll No. *", value=st.session_state.get("student_id", ""))
            student_email = st.text_input("Email (optional)", value=st.session_state.get("student_email", ""))
            student_name = st.text_input(
                "Full Name (required only on first visit)",
                value=st.session_state.get("student_name", ""),
                help="Returning students are recognised by Student ID and their stored name is retrieved automatically."
            )
            consent = st.checkbox(
                "I understand that my trading activity and performance will be recorded for academic evaluation."
            )
            submitted = st.form_submit_button("Enter Simulator", type="primary", use_container_width=True)

        if submitted:
            db_ok, db_error = test_supabase_connection()
            if not db_ok:
                print(f"[Supabase] Student access blocked: {db_error}")
                st.error(
                    "Cloud student records are temporarily unavailable. "
                    "Please ask the administrator to verify the Supabase Project URL and API key in Streamlit Secrets."
                )
                return
            if not student_id.strip():
                st.error("Please enter your Student ID.")
            elif student_email.strip() and "@" not in student_email:
                st.error("Please enter a valid email address or leave the email field blank.")
            elif not consent:
                st.error("Please confirm the academic-recording notice to continue.")
            else:
                try:
                    existing_profile = get_participant_by_student_id(student_id)
                    if not existing_profile and not student_name.strip():
                        st.error("First-time users must enter their full name.")
                        return
                    profile = create_or_get_participant(
                        student_name or (existing_profile or {}).get("student_name", ""),
                        student_id,
                        student_email
                    )
                    if not profile:
                        raise RuntimeError("Supabase did not return a participant profile.")
                    st.session_state.participant_id = profile["id"]
                    # For a returning roll number, the stored profile becomes the canonical identity.
                    st.session_state.student_name = str(profile.get("student_name") or student_name).strip()
                    st.session_state.student_id = str(profile.get("student_id") or student_id).strip()
                    st.session_state.student_email = str(profile.get("email") or student_email or "").strip().lower()

                    progress = get_active_progress(profile["id"])
                    if progress:
                        restore_progress_snapshot(progress)
                        st.session_state._resume_notice = True
                    st.rerun()
                except Exception:
                    st.error("Could not access your stored student profile. Please verify the Student ID and try again.")
        st.markdown('</div>', unsafe_allow_html=True)
        return
    elif not supabase_enabled():
        st.warning(
            "Cloud progress is unavailable because Supabase credentials could not be loaded. "
            "The simulator will work, but this run will not be retained centrally."
        )

    if st.session_state.pop("_resume_notice", False):
        st.success("Previous unfinished session restored. The market is paused so you can review before continuing.")

    # ===== DATA SOURCE SETUP =====
    if not st.session_state.data_loaded:
        st.markdown('<div class="card">', unsafe_allow_html=True)
        st.markdown("### Session Setup")
        st.caption(
            "By default the simulator generates a one-week 5-minute price path using a "
            "GARCH(1,1) model (annualized vol 12%-18%). Optionally supply your own intraday "
            "data below -- either input overrides the default model."
        )

        prior_sessions = get_student_history(st.session_state.get("participant_id"), limit=10) if supabase_enabled() else []
        prior_completed = [x for x in prior_sessions if x.get("status") == "completed"]
        if prior_completed:
            with st.expander(f"Previous performance · {len(prior_completed)} completed session(s)", expanded=False):
                prev_df = pd.DataFrame(prior_completed)
                cols = [c for c in ["session_no", "strategy_focus", "total_trades", "total_pnl", "return_pct", "max_drawdown_pct", "win_rate_pct", "profit_factor"] if c in prev_df.columns]
                prev_df = prev_df[cols].rename(columns={
                    "session_no": "Session", "strategy_focus": "Learning Focus", "total_trades": "Trades",
                    "total_pnl": "P&L (₹)", "return_pct": "Return %", "max_drawdown_pct": "Max DD %",
                    "win_rate_pct": "Win Rate %", "profit_factor": "Profit Factor"
                })
                st.dataframe(prev_df, use_container_width=True, hide_index=True)
                st.caption("Your detailed executed-trade history remains available in Performance & Progress after a session starts or is resumed.")
        st.markdown("""
        <div class="empty-box" style="text-align:left; border-style:solid; margin-top:4px;">
            <b>In this session you will:</b> watch the NIFTY path move bar by bar, place a call/put
            trade (market or limit), track open vs. realized P&amp;L and margin as time passes,
            and exit or let positions run to expiry — then review a performance summary.
        </div>
        """, unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        with c1:
            data_path = st.text_input("Data file path (optional)", value="", key="setup_path",
                                       placeholder="/path/to/your/data.txt")
        with c2:
            uploaded = st.file_uploader("Or upload data file (optional)", type=["txt", "csv"], key="setup_upload")

        c3, c4, c5 = st.columns(3)
        with c3:
            open_price_input = st.number_input("Opening price", min_value=1.0, value=DEFAULT_OPEN_PRICE, step=50.0, key="setup_open")
        with c4:
            capital_input = st.number_input(
                "Starting capital (₹)", min_value=100000.0, value=float(st.session_state.get('starting_capital', 10000000.0)),
                step=100000.0, format="%.0f", key="setup_capital",
                help="Lower this to make margin limits actually bite during the session — the ₹1 Cr default rarely runs out."
            )
        with c5:
            lot_size_input = st.number_input(
                "Lot size", min_value=1, value=int(st.session_state.get('lot_size', 65)),
                step=5, key="setup_lot_size",
                help="Contract multiplier per lot. NIFTY's exchange-set lot size has varied historically; 65 mirrors a recent value."
            )
        st.caption(
            f"With ₹{capital_input:,.0f} capital and a {lot_size_input}-unit lot, a single naked short option "
            f"typically consumes materially more margin than a long premium position. Use the capital setting "
            f"to make risk limits meaningful for the exercise."
        )

        strategy_focus = st.selectbox(
            "Session learning focus",
            [
                "Open practice",
                "Directional options",
                "Vertical spreads",
                "Volatility strategies",
                "Hedging / protection",
                "Risk and margin discipline",
            ],
            index=0,
            key="setup_strategy_focus",
            help="Used only for progress tracking; it does not constrain which trades you can place."
        )

        if st.button("Start Session", type="primary", use_container_width=True, key="btn_start_session"):
            st.session_state.starting_capital = float(capital_input)
            st.session_state.lot_size = int(lot_size_input)
            st.session_state.strategy_focus = strategy_focus
            st.session_state.equity_peak = float(capital_input)
            st.session_state.max_drawdown = 0.0
            st.session_state.max_drawdown_pct = 0.0
            df = None
            source = "garch"
            user_supplied_but_failed = False
            if uploaded is not None:
                df = load_data_from_upload(uploaded)
                source = "upload"
                if df is None or len(df) == 0:
                    user_supplied_but_failed = True
            elif data_path.strip():
                if os.path.exists(data_path.strip()):
                    df = load_data_from_path(data_path.strip())
                    source = "path"
                    if df is None or len(df) == 0:
                        user_supplied_but_failed = True
                else:
                    st.error(f"Path not found: {data_path}")

            bars = None
            if df is not None and len(df) > 0:
                # ---- Real uploaded/path data ----
                scale_factor = calculate_scale_factor(df, open_price_input)
                df_scaled = scale_data(df, scale_factor)
                bars = _add_day_num(resample_to_bars(df_scaled, BAR_MINUTES))
                if bars is None or len(bars) == 0:
                    # Parsed fine but resampling left nothing usable (e.g. too few/sparse rows)
                    user_supplied_but_failed = True
                    bars = None

            if user_supplied_but_failed:
                st.warning(
                    "⚠️ Your data file couldn't be read into a usable price path (wrong format, "
                    "too few rows, or unrecognised columns) — starting instead with the default "
                    "GARCH(1,1) simulated path so the session can still begin."
                )

            if bars is not None:
                st.session_state.df_raw = df
                st.session_state.scale_factor = scale_factor
                first_gap = np.random.normal(0, 0.004)
                implied_prev_close = round(open_price_input / (1 + first_gap), 2)

                st.session_state.df_day_scaled = bars
                st.session_state.prev_scaled_close = implied_prev_close
                st.session_state.prev_day_close = implied_prev_close
                st.session_state.start_time = bars.iloc[0]['datetime']
                st.session_state.session_end = bars.iloc[-1]['datetime']
                st.session_state.simulated_data = bars
                st.session_state.data_source_choice = source
            else:
                # ---- Default model: GARCH(1,1), one trading week, 5-min bars ----
                start_date = date.today()
                sim_data, first_gap, implied_prev_close = generate_garch_week_path(
                    start_date=start_date, open_price=open_price_input,
                    bars_per_day=BARS_PER_DAY, days=SIM_DAYS,
                    vol_min=VOL_MIN, vol_max=VOL_MAX, bar_minutes=BAR_MINUTES,
                    seed=int(time.time())
                )
                sim_data = _add_day_num(sim_data)
                st.session_state.df_raw = sim_data
                st.session_state.df_day_scaled = sim_data
                st.session_state.scale_factor = 1.0
                st.session_state.prev_scaled_close = implied_prev_close
                st.session_state.prev_day_close = implied_prev_close
                st.session_state.start_time = sim_data.iloc[0]['datetime']
                st.session_state.session_end = sim_data.iloc[-1]['datetime']
                st.session_state.simulated_data = sim_data
                st.session_state.data_source_choice = "garch"

            st.session_state.expiry_dt = (
                st.session_state.start_time.replace(hour=15, minute=30, second=0, microsecond=0)
                + timedelta(days=TOTAL_EXPIRY_DAYS)
            )
            st.session_state.current_index = 0
            st.session_state.max_reached_index = 0
            st.session_state.data_loaded = True
            st.session_state.session_start_wall = datetime.now()
            st.session_state.target_nifty_level = float(open_price_input)
            st.session_state.basket = []

            # Create the permanent cloud session only after the market path is ready.
            if supabase_enabled():
                try:
                    st.session_state.supabase_session_id = None
                    sid = create_session_record()
                    if not sid:
                        raise RuntimeError("Supabase did not return a session ID.")
                except Exception as exc:
                    st.session_state.data_loaded = False
                    st.error(f"Could not start the recorded Supabase session: {exc}")
                    return

            save_session_state()
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        return

    # ===== MAIN SIMULATION STATE =====
    sim = st.session_state.simulated_data
    n_bars = len(sim)
    if n_bars == 0:
        st.error("No simulation data.")
        return

    if st.session_state.current_index >= n_bars:
        st.session_state.current_index = n_bars - 1
    if st.session_state.current_index > st.session_state.max_reached_index:
        st.session_state.max_reached_index = st.session_state.current_index

    current_row = sim.iloc[st.session_state.current_index]
    st.session_state.current_price = float(current_row['close'])
    current_price = st.session_state.current_price
    current_dt = current_row['datetime']
    current_day_num = int(current_row['day_num']) if 'day_num' in sim.columns else (st.session_state.current_index // BARS_PER_DAY) + 1

    # Build / refresh day-close map so change is always vs previous trading day's close
    if (not st.session_state.day_close_map) and sim is not None and len(sim) > 0 and 'day_num' in sim.columns:
        day_closes = {}
        for dnum, g in sim.groupby('day_num'):
            day_closes[int(dnum)] = float(g.iloc[-1]['close'])
        st.session_state.day_close_map = day_closes

    if current_day_num <= 1:
        ref_close = st.session_state.prev_day_close or st.session_state.prev_scaled_close or current_price
    else:
        ref_close = st.session_state.day_close_map.get(
            current_day_num - 1,
            st.session_state.prev_scaled_close or current_price
        )
    prev_close = float(ref_close)
    price_change = current_price - prev_close
    price_pct = (price_change / prev_close) * 100 if prev_close > 0 else 0
    is_up = price_change >= 0

    T_current = get_time_to_expiry(current_dt, st.session_state.expiry_dt)
    st.session_state.T_current = T_current
    days_to_expiry = round(T_current * 365, 1)
    atm_strike = round(current_price / 100) * 100

    # Cash settlement when time-to-expiry is exhausted
    if T_current <= (60 / (365 * 24 * 3600)) and st.session_state.positions and not st.session_state.trading_locked:
        _settle_all_cash(current_price, current_dt)
        st.toast("Options expired — all open positions cash-settled", icon="📅")
        st.session_state.trading_locked = True
        save_session_state()

    # End of simulated week (last bar reached): settle + lock
    if st.session_state.current_index >= n_bars - 1:
        st.session_state.playing = False
        if st.session_state.positions and not st.session_state.trading_locked:
            _settle_all_cash(current_price, current_dt)
            st.toast("Session week complete — all open positions cash-settled", icon="📅")
        cancel_pending_order_records(st.session_state.pending_limits, "week_end")
        st.session_state.pending_limits = []  # cancel unfilled limits at week end
        st.session_state.trading_locked = True

    st.session_state.chain_df = generate_option_chain(current_price, prev_close, T_current)
    chain_df = st.session_state.chain_df

    # Continuous limit-order matching against live LTPs
    n_filled = match_pending_limits(current_price, current_dt, chain_df, int(st.session_state.get("lot_size", 65)))
    if n_filled:
        st.toast(f"✅ {n_filled} limit order(s) filled", icon="📋")

    # Compute open / realized + margin
    open_pnl = 0.0
    cons_pos = []
    if st.session_state.positions:
        cons_pos = consolidate_positions(st.session_state.positions, current_price, T_current, chain_df)
        open_pnl = sum(p['pnl'] for p in cons_pos)
    realized_pnl = st.session_state.realized_pnl
    used_margin = calculate_realistic_margin(st.session_state.positions, current_price, int(st.session_state.get("lot_size", 65)))
    if used_margin > st.session_state.peak_margin_used:
        st.session_state.peak_margin_used = used_margin
    available_margin = max(0.0, st.session_state.starting_capital - used_margin)

    # Learning analytics: update trade MAE/max-profit and portfolio drawdown every visible bar.
    update_live_risk_metrics(current_price, current_dt, chain_df)
    update_session_drawdown(open_pnl)

    # ===== LAYOUT: LEFT + RIGHT =====
    col_left, col_right = st.columns([1, 2], gap="medium")

    # ==================== LEFT SIDEBAR ====================
    with col_left:
        # Prominent trading day + lock status
        _day_extra = ""
        if st.session_state.trading_locked or st.session_state.current_index >= n_bars - 1:
            _day_extra = " &nbsp;·&nbsp; <span style='color:#ffab40'>SESSION CLOSED</span>"
        st.markdown(f"""
        <div style="background:#0a2540;color:#e8f0fe;border-radius:10px;padding:8px 14px;margin-bottom:10px;
                    text-align:center;font-weight:700;font-size:15px;letter-spacing:0.3px;border:1px solid #1e3a5f;">
            TRADING DAY &nbsp;·&nbsp; Day-{current_day_num}{_day_extra}
        </div>
        """, unsafe_allow_html=True)

        # NIFTY Card — change vs previous day's close
        price_cls = "nifty-up" if is_up else "nifty-down"
        change_sign = "+" if is_up else ""
        st.markdown(f"""
        <div class="card">
            <div class="nifty-symbol">NIFTY 50</div>
            <div class="nifty-price {price_cls}">₹{current_price:,.2f}</div>
            <div class="nifty-change {price_cls}">{change_sign}{price_change:.2f} ({change_sign}{price_pct:.2f}%)</div>
            <div class="nifty-meta">
                TIME <span>{current_dt.strftime('%H:%M:%S')}</span><br>
                DTE <span>{days_to_expiry}</span><br>
                PREV DAY CLOSE <span>₹{prev_close:,.2f}</span>
            </div>
        </div>
        """, unsafe_allow_html=True)
        st.markdown(
            f'<div class="hint-line">⏳ <b>DTE (days to expiry)</b> — the closer this gets to 0, '
            f'the faster an option\'s time value decays ({glossary_term("theta")}), and the more its price is driven '
            f'by intrinsic value alone.</div>',
            unsafe_allow_html=True
        )

        # Open / Realized P&L + Margin
        open_cls = "profit" if open_pnl >= 0 else "loss"
        real_cls = "profit" if realized_pnl >= 0 else "loss"
        st.markdown(f"""
        <div class="pnl-row">
            <span class="pnl-label">Open P&L</span>
            <span class="pnl-value {open_cls}">₹{open_pnl:+,.2f}</span>
        </div>
        <div class="pnl-row">
            <span class="pnl-label">Realized P&L</span>
            <span class="pnl-value {real_cls}">₹{realized_pnl:+,.2f}</span>
        </div>
        <div class="pnl-row">
            <span class="pnl-label">{glossary_term('margin', 'Used Margin')}</span>
            <span class="pnl-value">₹{used_margin:,.0f}</span>
        </div>
        <div class="pnl-row">
            <span class="pnl-label">Available Margin</span>
            <span class="pnl-value">₹{available_margin:,.0f}</span>
        </div>
        """, unsafe_allow_html=True)
        st.markdown(
            '<div class="hint-line"><b>Open P&L</b> marks your live positions to the current price — '
            'it moves every bar and isn\'t locked in yet. <b>Realized P&L</b> only changes when you '
            'actually exit a trade.</div>',
            unsafe_allow_html=True
        )

        st.markdown("<br>", unsafe_allow_html=True)

        # ===== PROMINENT GO LIVE / PAUSE =====
        if not st.session_state.playing:
            st.markdown("""
            <style>
            div[data-testid="stButton"] > button[kind="primary"] {
                background: #00a86b !important;
                color: white !important;
                font-size: 16px !important;
                font-weight: 700 !important;
                padding: 12px 0 !important;
                border-radius: 12px !important;
                border: none !important;
                box-shadow: 0 4px 12px rgba(0,168,107,0.35);
            }
            </style>
            """, unsafe_allow_html=True)
            if st.button("GO LIVE", use_container_width=True, type="primary", key="btn_golive",
                         disabled=st.session_state.trading_locked):
                st.session_state.playing = True
                st.session_state.last_update = time.time()
                st.rerun()
        else:
            if st.button("PAUSE", use_container_width=True, key="btn_pause"):
                st.session_state.playing = False
                save_session_state()
                st.rerun()
            st.caption("● LIVE")

        st.markdown("<br>", unsafe_allow_html=True)

        # Speed: 0.25x – 5x ; 1 real sec = 1 sim min → bar every 5s at 1x
        speed = st.slider("Speed", 0.25, 5.0, float(st.session_state.speed), 0.25, key="speed_slider")
        st.session_state.speed = speed
        secs_per_bar = TICK_SECONDS_BASE / speed
        st.caption(f"{speed:.2f}x  •  1 bar (5 sim-min) every {secs_per_bar:.1f}s  •  "
                   f"~{(BARS_PER_DAY * secs_per_bar) / 60:.1f} min real-time per trading day")

        # Jump forward — single dropdown; selection jumps immediately
        jump_options = {
            "— Jump forward —": None,
            "5 Minutes": 1,
            "10 Minutes": 2,
            "30 Minutes": 6,
            "1 Hour": 12,
            "2 Hours": 24,
            "+1 Day": "day",
        }
        jump_choice = st.selectbox("Jump forward", list(jump_options.keys()), key="jump_select")
        jump_val = jump_options[jump_choice]
        if jump_val is not None:
            if jump_val == "day":
                cur_day = st.session_state.current_index // BARS_PER_DAY
                new_idx = min((cur_day + 1) * BARS_PER_DAY, n_bars - 1)
            else:
                new_idx = min(st.session_state.current_index + jump_val, n_bars - 1)
            if new_idx != st.session_state.current_index:
                st.session_state.current_index = new_idx
                if new_idx > st.session_state.max_reached_index:
                    st.session_state.max_reached_index = new_idx
                st.session_state.pop("jump_select", None)
                save_session_state()
                st.rerun()

        st.markdown("<br>", unsafe_allow_html=True)

        # Reset high-contrast
        st.markdown('<div class="reset-btn-container">', unsafe_allow_html=True)
        if st.button("RESET SESSION", use_container_width=True, key="btn_reset"):
            # A reset ends the current run. Close any still-open trades at the current
            # simulated market price so analytics are not left with orphaned Open trades.
            if st.session_state.get("tradebook"):
                _close_all_open_trades_at_market(
                    current_price, current_dt, chain_df, "session_reset"
                )

            cancel_pending_order_records(st.session_state.get("pending_limits", []), "session_reset")

            if supabase_enabled() and st.session_state.get("supabase_session_id"):
                try:
                    finish_session_record(
                        current_price=current_price,
                        current_day_num=current_day_num,
                        status="reset"
                    )
                except Exception as exc:
                    print(f"[Supabase] Failed to finalize reset session: {exc}")

            for key in list(st.session_state.keys()):
                if key not in ['data_loaded', 'df_raw', 'simulated_data', 'df_day_scaled',
                               'start_time', 'session_end', 'expiry_dt', 'scale_factor', 'prev_scaled_close',
                               'prev_day_close', 'lot_size', 'target_nifty_level', 'starting_capital',
                               'data_source_choice', 'day_close_map',
                               'participant_id', 'student_name', 'student_id', 'student_email']:
                    del st.session_state[key]
            st.session_state.playing = False
            st.session_state.current_index = 0
            st.session_state.max_reached_index = 0
            st.session_state.basket = []
            st.session_state.positions = []
            st.session_state.tradebook = []
            st.session_state.pending_limits = []
            st.session_state.realized_pnl = 0.0
            st.session_state.peak_margin_used = 0.0
            st.session_state.trading_locked = False
            st.session_state.session_finished = False
            st.session_state.report_generated = False
            st.session_state.report_path = None
            st.session_state.supabase_session_id = None

            if supabase_enabled():
                try:
                    create_session_record()
                except Exception as exc:
                    st.warning(f"New cloud session could not be created after reset: {exc}")

            try:
                if os.path.exists(PERSIST_PATH):
                    os.remove(PERSIST_PATH)
            except Exception:
                pass
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    # ==================== RIGHT PANEL ====================
    with col_right:
        tab_place, tab_pos, tab_graph, tab_perf, tab_leaderboard = st.tabs([
            "Place Order", "Positions", "Market Chart", "Performance & Progress", "Leaderboard"
        ])

        # ---------- TAB 1: PLACE ORDER ----------
        with tab_place:
            if st.session_state.trading_locked:
                st.warning("Session finished — trading is locked. Generate/download report, or Reset to start a new session.")

            st.markdown('<div class="order-card">', unsafe_allow_html=True)
            st.markdown('<div class="order-card-title">1 &nbsp;·&nbsp; Choose your option</div>', unsafe_allow_html=True)

            # Clean tight order-entry row
            c1, c2, c3, c4, c5, c6 = st.columns([1.0, 0.9, 1.2, 0.8, 1.1, 1.0])
            with c1:
                side = st.radio("Side", ["BUY", "SELL"], horizontal=True, key="ord_side")
            with c2:
                otype = st.selectbox("Type", ["CE", "PE"], key="ord_type")
            with c3:
                strikes = chain_df['Strike'].tolist()
                default_idx = strikes.index(atm_strike) if atm_strike in strikes else 0
                strike = st.selectbox("Strike", strikes, index=default_idx, key="ord_strike")
            with c4:
                lots = st.number_input("Lots", min_value=1, value=1, step=1, key="ord_lots")
            with c5:
                order_type = st.selectbox("Order", ["MARKET", "LIMIT"], key="ord_order")
            with c6:
                row = chain_df[chain_df['Strike'] == strike]
                ltp = float(row.iloc[0]['CE Price'] if otype == 'CE' else row.iloc[0]['PE Price']) if len(row) else 0.0
                st.markdown(
                    f"<div style='padding-top:4px;'><div style='font-size:11px;color:#666;font-weight:600;'>LTP</div>"
                    f"<div style='font-size:18px;font-weight:700;color:#1a1a1a;'>₹{ltp:.2f}</div></div>",
                    unsafe_allow_html=True
                )

            # Moneyness badge for the selected strike -- helps a student see ITM/ATM/OTM at a glance
            money_label = get_moneyness_label(otype, strike, current_price, atm_strike)
            money_cls = {"ITM": "money-itm", "ATM": "money-atm", "OTM": "money-otm"}[money_label]
            st.markdown(
                f"<span style='font-size:12px;color:#666;'>NIFTY {strike} {otype} · "
                f"{glossary_term('moneyness')}</span>"
                f"<span class='money-badge {money_cls}'>{money_label}</span>",
                unsafe_allow_html=True
            )

            limit_price = None
            if order_type == "LIMIT":
                limit_price = st.number_input("Limit Price", min_value=0.05, value=float(round(ltp, 2)), step=0.05, key="limit_px")
                if side == "BUY" and limit_price > ltp:
                    st.caption("⚠️ Your limit is above the current LTP — a buy limit above LTP will fill immediately, like a market order.")
                elif side == "SELL" and limit_price < ltp:
                    st.caption("⚠️ Your limit is below the current LTP — a sell limit below LTP will fill immediately, like a market order.")
                else:
                    st.caption("This limit will wait in the order book until the price reaches it (see 'Order Book' below).")

            # ----- Order preview: premium, breakeven, margin -- before the student commits -----
            _preview_px = limit_price if (order_type == "LIMIT" and limit_price is not None) else ltp
            _qty = lots * lot_size
            _premium_total = _preview_px * _qty
            if otype == "CE":
                _breakeven = strike + _preview_px
            else:
                _breakeven = strike - _preview_px
            _trial_items = [{
                'side': side.title(), 'type': otype, 'strike': strike, 'lots': lots,
                'quantity': _qty, 'price': _preview_px, 'order_type': order_type, 'ltp': ltp
            }]
            _trial_margin = calculate_realistic_margin(
                list(st.session_state.positions) + _trial_items, current_price, lot_size
            )
            _extra_margin_needed = max(0.0, _trial_margin - used_margin)
            _fits = _trial_margin <= st.session_state.starting_capital
            _leg_stats = analyze_payoff(_trial_items)
            _mp_txt = "Unlimited" if _leg_stats['max_profit'] is None else f"₹{_leg_stats['max_profit']:,.0f}"
            _ml_txt = "Unlimited" if _leg_stats['max_loss'] is None else f"₹{_leg_stats['max_loss']:,.0f}"

            st.markdown('<div class="order-card-title" style="margin-top:10px;">2 &nbsp;·&nbsp; Preview before you commit</div>', unsafe_allow_html=True)
            st.markdown(f"""
            <div class="preview-box">
                <div class="preview-grid">
                    <div><div class="preview-label">{'Premium Paid' if side == 'BUY' else 'Premium Received'}</div>
                        <div class="preview-val">₹{_premium_total:,.0f}</div></div>
                    <div><div class="preview-label">{glossary_term('breakeven', 'Breakeven Spot')}</div>
                        <div class="preview-val">₹{_breakeven:,.0f}</div></div>
                    <div><div class="preview-label">Max Profit</div>
                        <div class="preview-val" style="color:#00a86b;">{_mp_txt}</div></div>
                    <div><div class="preview-label">Max Loss</div>
                        <div class="preview-val" style="color:#e74c3c;">{_ml_txt}</div></div>
                    <div><div class="preview-label">Extra Margin Needed</div>
                        <div class="preview-val">₹{_extra_margin_needed:,.0f}</div></div>
                    <div><div class="preview-label">Margin After Order</div>
                        <div class="preview-val" style="color:{'#00a86b' if _fits else '#e74c3c'};">₹{_trial_margin:,.0f}</div></div>
                </div>
                <div class="preview-hint">
                    {'Long ' + otype + ': profit is unlimited above breakeven (call) / below breakeven (put), loss is capped at the premium paid.' if side == 'BUY'
                     else 'Short ' + otype + ': profit is capped at the premium received, loss can be large if the market moves against the position — margin is blocked to cover this.'}
                </div>
            </div>
            """, unsafe_allow_html=True)
            with st.expander("View payoff diagram for this order"):
                fig_leg, _ = render_payoff_diagram(_trial_items, current_price, title=f"{side} {strike} {otype} — Payoff at Expiry")
                st.plotly_chart(fig_leg, use_container_width=True, config={'displayModeBar': False})

            def _build_order_item():
                qty = lots * lot_size
                px = limit_price if order_type == "LIMIT" else ltp
                return {
                    'side': side.title(),
                    'type': otype,
                    'strike': strike,
                    'lots': lots,
                    'quantity': qty,
                    'price': px,
                    'order_type': order_type,
                    'ltp': ltp,
                    'order_source': 'manual'
                }

            def _can_afford(extra_items):
                trial = list(st.session_state.positions) + list(extra_items)
                req = calculate_realistic_margin(trial, current_price, lot_size)
                return req <= st.session_state.starting_capital, req

            def _execute_items(items):
                """Execute marketable orders; queue non-marketable limits; mirror all order events to Supabase."""
                if st.session_state.trading_locked:
                    st.error("Trading is locked for this session.")
                    return 0

                ok, req = _can_afford(items)
                if not ok:
                    shortfall = req - st.session_state.starting_capital
                    if supabase_enabled():
                        for item in items:
                            try:
                                save_order_record(
                                    item,
                                    "rejected",
                                    current_dt=current_dt,
                                    current_price=current_price,
                                    rejection_reason=f"Insufficient margin; shortfall {shortfall:.2f}"
                                )
                            except Exception:
                                pass
                    st.markdown(f"""
                    <div class="margin-err-box">
                        <b>⚠️ Insufficient margin — order not placed.</b><br>
                        Margin required for this order: ₹{req:,.0f} &nbsp;·&nbsp;
                        Your capital: ₹{st.session_state.starting_capital:,.0f} &nbsp;·&nbsp;
                        Short by ≈ ₹{shortfall:,.0f}.<br>
                        Try fewer lots, exit an existing position to free up margin, or switch a
                        {glossary_term('naked short')} leg to a defined-risk structure (a spread) —
                        margin blocked for shorts is higher.
                    </div>
                    """, unsafe_allow_html=True)
                    return 0

                executed = 0
                for item in items:
                    if item['order_type'] == "LIMIT":
                        marketable = (item['side'] == 'Buy' and item['price'] >= item['ltp']) or \
                                     (item['side'] == 'Sell' and item['price'] <= item['ltp'])
                        if not marketable:
                            if supabase_enabled():
                                try:
                                    item['supabase_order_id'] = save_order_record(
                                        item, "pending", current_dt=current_dt, current_price=current_price
                                    )
                                except Exception:
                                    item['supabase_order_id'] = None
                            st.session_state.pending_limits.append(item)
                            continue

                    order_id = None
                    trade_id = None
                    if supabase_enabled():
                        try:
                            order_id = save_order_record(
                                item, "filled", current_dt=current_dt,
                                current_price=current_price, fill_price=item['price']
                            )
                            trade_id = save_trade_record(item, current_dt, order_id, current_price=current_price)
                        except Exception as exc:
                            print(f"[Supabase] Failed to persist executed trade: {exc}")

                    st.session_state.positions.append({
                        'strike': item['strike'],
                        'type': item['type'],
                        'side': item['side'],
                        'entry_price': item['price'],
                        'quantity': item['quantity'],
                        'lots': item['lots']
                    })
                    st.session_state.tradebook.append({
                        'entry_time': current_dt.strftime('%H:%M:%S'),
                        'strike': item['strike'],
                        'type': item['type'],
                        'side': item['side'],
                        'qty': item['quantity'],
                        'lots': item['lots'],
                        'entry_price': item['price'],
                        'entry_dt': _iso(current_dt),
                        'strategy_name': _strategy_label(item),
                        'capital_used': _estimate_trade_capital(item, current_price),
                        'max_drawdown': 0.0,
                        'max_drawdown_pct': 0.0,
                        'max_profit_seen': 0.0,
                        'holding_minutes': 0.0,
                        'exit_time': '-',
                        'exit_price': 0.0,
                        'pnl': 0.0,
                        'status': 'Open',
                        'supabase_order_id': order_id,
                        'supabase_trade_id': trade_id
                    })
                    executed += 1

                margin_now = calculate_realistic_margin(st.session_state.positions, current_price, lot_size)
                if margin_now > st.session_state.peak_margin_used:
                    st.session_state.peak_margin_used = margin_now
                save_session_state()
                return executed

            st.markdown("""
            <style>
            div[data-testid="stButton"] > button[kind="secondary"] {
                border: 1.5px solid #387ed1 !important;
                color: #387ed1 !important;
                background: white !important;
                font-weight: 600 !important;
                border-radius: 6px !important;
                padding: 6px 18px !important;
            }
            div[data-testid="stButton"] > button[kind="secondary"]:hover {
                background: #eef4fc !important;
            }
            </style>
            """, unsafe_allow_html=True)

            btn1, btn2 = st.columns(2)
            with btn1:
                if st.button("Add to Basket", key="btn_add_basket", type="secondary", use_container_width=True,
                             disabled=st.session_state.trading_locked):
                    basket_item = _build_order_item()
                    basket_item['order_source'] = 'basket'
                    st.session_state.basket.append(basket_item)
                    st.toast(f"Added {side} {strike} {otype} x{lots} to basket", icon="✅")
                    st.rerun()
            with btn2:
                if st.button("Execute Now", key="btn_exec_now", type="primary", use_container_width=True,
                             disabled=st.session_state.trading_locked):
                    item = _build_order_item()
                    n = _execute_items([item])
                    if n:
                        st.toast(f"✅ Order executed", icon="🎉")
                        components.html("""
                        <script>
                        const ctx = new (window.AudioContext || window.webkitAudioContext)();
                        const o = ctx.createOscillator();
                        const g = ctx.createGain();
                        o.connect(g); g.connect(ctx.destination);
                        o.frequency.value = 880; o.type = 'sine';
                        g.gain.setValueAtTime(0.15, ctx.currentTime);
                        g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.3);
                        o.start(ctx.currentTime); o.stop(ctx.currentTime + 0.3);
                        </script>
                        """, height=0)
                    st.rerun()

            st.markdown('</div>', unsafe_allow_html=True)  # close order-card

            # ----- STRATEGY BUILDER: pick a ready-made multi-leg strategy -----
            st.markdown('<div class="strategy-card">', unsafe_allow_html=True)
            st.markdown('<div class="order-card-title">Or build a ready-made strategy</div>', unsafe_allow_html=True)

            sc1, sc2 = st.columns([1.1, 1.6])
            with sc1:
                strat_cat = st.selectbox("Category", list(STRATEGY_CATALOGUE.keys()), key="strat_cat")
            with sc2:
                strat_options = list(STRATEGY_CATALOGUE[strat_cat].keys())
                if st.session_state.get("strat_name") not in strat_options:
                    st.session_state["strat_name"] = strat_options[0]  # reset if category just changed
                strat_name = st.selectbox("Strategy", strat_options, key="strat_name")
            legs_def = STRATEGY_CATALOGUE[strat_cat][strat_name]
            uses_fut = any(t == 'FUT' for _, t, _, _ in legs_def)
            uses_wing = any(k in ('OTM_CALL', 'OTM_PUT', 'LOWER', 'UPPER') for _, _, k, _ in legs_def)

            sc3, sc4 = st.columns([1, 1])
            with sc3:
                strat_lots = st.number_input("Base Lots", min_value=1, value=1, step=1, key="strat_lots")
            with sc4:
                if uses_wing:
                    strat_width_n = st.selectbox("Wing width (strikes away from ATM)", [1, 2, 3, 4, 5], index=1, key="strat_width")
                    strat_offset_pts = strat_width_n * 100
                else:
                    strat_offset_pts = 100
                    st.caption("This strategy uses the ATM strike only.")

            strat_items = build_strategy_legs(
                legs_def, atm_strike, strat_offset_pts, int(strat_lots), lot_size, current_price, T_current
            )
            net_premium = sum(
                (it['price'] * it['quantity']) * (1 if it['side'] == 'Buy' else -1) * (1 if it['type'] != 'FUT' else 0)
                for it in strat_items
            )

            st.markdown('<div class="preview-hint" style="margin-bottom:4px;"><b>Legs to be added:</b></div>', unsafe_allow_html=True)
            for it in strat_items:
                side_cls = "item-side-buy" if it['side'] == 'Buy' else "item-side-sell"
                px_txt = f"₹{it['price']:,.2f}" if it['type'] != 'FUT' else f"spot ₹{it['price']:,.2f}"
                st.markdown(f"""
                <div class="strategy-leg-row">
                    <span><span class="item-side-badge {side_cls}">{it['side'].upper()}</span>
                    {instrument_label(it['strike'], it['type'])} &nbsp; {it['lots']} lot</span>
                    <span style="color:#555;">@ {px_txt}</span>
                </div>
                """, unsafe_allow_html=True)

            st.markdown(
                f"<div class='preview-hint'><b>Net option premium (excludes underlying leg):</b> "
                f"{'Debit' if net_premium > 0 else 'Credit'} ≈ ₹{abs(net_premium):,.0f}</div>",
                unsafe_allow_html=True
            )
            _strat_stats = analyze_payoff(strat_items)
            _smp_txt = "Unlimited" if _strat_stats['max_profit'] is None else f"₹{_strat_stats['max_profit']:,.0f}"
            _sml_txt = "Unlimited" if _strat_stats['max_loss'] is None else f"₹{_strat_stats['max_loss']:,.0f}"
            _sbe_txt = ", ".join(f"₹{b:,.0f}" for b in _strat_stats['breakevens']) or "—"
            st.markdown(f"""
            <div class="preview-box" style="margin-top:6px;">
                <div class="preview-grid">
                    <div><div class="preview-label">Max Profit</div>
                        <div class="preview-val" style="color:#00a86b;">{_smp_txt}</div></div>
                    <div><div class="preview-label">Max Loss</div>
                        <div class="preview-val" style="color:#e74c3c;">{_sml_txt}</div></div>
                    <div><div class="preview-label">Breakeven(s)</div>
                        <div class="preview-val" style="font-size:12px;">{_sbe_txt}</div></div>
                </div>
            </div>
            """, unsafe_allow_html=True)
            with st.expander("View payoff diagram for this strategy"):
                fig_strat, _ = render_payoff_diagram(strat_items, current_price, title=f"{strat_name.split('(')[0].strip()} — Payoff at Expiry")
                st.plotly_chart(fig_strat, use_container_width=True, config={'displayModeBar': False})
            if uses_fut:
                st.markdown(f"<div class='strategy-note'>⚠️ {STRATEGY_FUT_NOTE}</div>", unsafe_allow_html=True)
            st.markdown(
                "<div class='strategy-note'>All legs are added as MARKET orders to the basket below — "
                "review, remove a leg if needed, then use <b>Execute Basket</b> to place the whole strategy "
                "at once (margin is checked for the combined position).</div>",
                unsafe_allow_html=True
            )

            if st.button("Add Strategy to Basket", key="btn_add_strategy", type="secondary",
                         use_container_width=True, disabled=st.session_state.trading_locked):
                group_id = str(uuid.uuid4())
                for strategy_item in strat_items:
                    strategy_item['order_source'] = 'strategy'
                    strategy_item['strategy_name'] = strat_name
                    strategy_item['order_group_id'] = group_id
                st.session_state.basket.extend(strat_items)
                st.toast(f"Added {strat_name.split('(')[0].strip()} ({len(strat_items)} legs) to basket", icon="🧩")
                st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)  # close strategy-card

            # BASKET
            st.markdown('<div class="section-title">Basket <span style="font-weight:500;color:#888;font-size:11px;">— add multiple legs, then execute together</span></div>', unsafe_allow_html=True)
            if st.session_state.basket:
                for i, item in enumerate(st.session_state.basket):
                    cols = st.columns([4.3, 0.7, 0.7])
                    with cols[0]:
                        side_cls = "item-side-buy" if item['side'] == 'Buy' else "item-side-sell"
                        st.markdown(
                            f"""<div class="item-card">
                                <div>
                                    <span class="item-side-badge {side_cls}">{item['side'].upper()}</span>
                                    <span class="item-main">{instrument_label(item['strike'], item['type'])} &nbsp;·&nbsp; {item['lots']} lot</span>
                                    <div class="item-sub">₹{item['price']:.2f} · {item['order_type']}</div>
                                </div>
                            </div>""",
                            unsafe_allow_html=True
                        )
                    with cols[1]:
                        if st.button("✏️", key=f"edit_basket_{i}", help="Edit this leg"):
                            st.session_state.editing_basket_idx = None if st.session_state.editing_basket_idx == i else i
                            st.rerun()
                    with cols[2]:
                        if st.button("✕", key=f"rm_basket_{i}", help="Remove this leg"):
                            del st.session_state.basket[i]
                            if st.session_state.editing_basket_idx == i:
                                st.session_state.editing_basket_idx = None
                            st.rerun()

                    if st.session_state.editing_basket_idx == i:
                        st.markdown('<div class="edit-leg-box">', unsafe_allow_html=True)
                        ec1, ec2 = st.columns(2)
                        with ec1:
                            new_lots = st.number_input(
                                "Lots", min_value=1, value=int(item['lots']), step=1, key=f"edit_lots_{i}"
                            )
                        with ec2:
                            if item['type'] != 'FUT':
                                new_price = st.number_input(
                                    "Price (₹)", min_value=0.05, value=float(item['price']), step=0.05, key=f"edit_price_{i}"
                                )
                                st.caption("Saving a changed price turns this into a LIMIT leg at that price.")
                            else:
                                new_price = item['price']
                                st.caption("The underlying leg marks to live spot — price isn't editable here.")
                        esave, ecancel = st.columns(2)
                        with esave:
                            if st.button("Save Changes", key=f"save_edit_{i}", type="primary", use_container_width=True):
                                st.session_state.basket[i]['lots'] = int(new_lots)
                                st.session_state.basket[i]['quantity'] = int(new_lots) * lot_size
                                if item['type'] != 'FUT' and float(new_price) != float(item['price']):
                                    st.session_state.basket[i]['price'] = float(new_price)
                                    st.session_state.basket[i]['order_type'] = 'LIMIT'
                                st.session_state.editing_basket_idx = None
                                st.toast("Leg updated", icon="✏️")
                                st.rerun()
                        with ecancel:
                            if st.button("Cancel", key=f"cancel_edit_{i}", use_container_width=True):
                                st.session_state.editing_basket_idx = None
                                st.rerun()
                        st.markdown('</div>', unsafe_allow_html=True)

                margin_req = calculate_realistic_margin(
                    list(st.session_state.positions) + list(st.session_state.basket),
                    current_price, lot_size
                )
                extra = max(0, margin_req - st.session_state.starting_capital)
                st.markdown(f"""
                <div class="margin-box">
                    <div class="margin-grid">
                        <div><div class="margin-label">Margin Required</div><div class="margin-val">₹{margin_req:,.0f}</div></div>
                        <div><div class="margin-label">Extra Needed</div><div class="margin-val">₹{extra:,.0f}</div></div>
                        <div><div class="margin-label">Available</div><div class="margin-val">₹{available_margin:,.0f}</div></div>
                    </div>
                </div>
                """, unsafe_allow_html=True)

                _basket_stats = analyze_payoff(st.session_state.basket)
                _bmp_txt = "Unlimited" if _basket_stats['max_profit'] is None else f"₹{_basket_stats['max_profit']:,.0f}"
                _bml_txt = "Unlimited" if _basket_stats['max_loss'] is None else f"₹{_basket_stats['max_loss']:,.0f}"
                _bbe_txt = ", ".join(f"₹{b:,.0f}" for b in _basket_stats['breakevens']) or "—"
                st.markdown(f"""
                <div class="preview-box">
                    <div class="preview-grid">
                        <div><div class="preview-label">Combined Max Profit</div>
                            <div class="preview-val" style="color:#00a86b;">{_bmp_txt}</div></div>
                        <div><div class="preview-label">Combined Max Loss</div>
                            <div class="preview-val" style="color:#e74c3c;">{_bml_txt}</div></div>
                        <div><div class="preview-label">Combined Breakeven(s)</div>
                            <div class="preview-val" style="font-size:12px;">{_bbe_txt}</div></div>
                    </div>
                </div>
                """, unsafe_allow_html=True)
                with st.expander("View combined payoff diagram", expanded=False):
                    fig_basket, _ = render_payoff_diagram(st.session_state.basket, current_price, title="Basket — Combined Payoff at Expiry")
                    st.plotly_chart(fig_basket, use_container_width=True, config={'displayModeBar': False})

                st.markdown("""
                <style>
                div[data-testid="stHorizontalBlock"] button[kind="primary"] {
                    background: #df2020 !important;
                    color: white !important;
                    font-weight: 700 !important;
                    border-radius: 6px !important;
                    border: none !important;
                }
                div[data-testid="stHorizontalBlock"] button[kind="primary"]:hover {
                    background: #c01a1a !important;
                }
                </style>
                """, unsafe_allow_html=True)
                bc1, bc2 = st.columns(2)
                with bc1:
                    if st.button("Clear Basket", use_container_width=True, key="btn_clear_basket"):
                        st.session_state.basket = []
                        st.rerun()
                with bc2:
                    if st.button("Execute Basket", use_container_width=True, type="primary", key="btn_exec_basket",
                                 disabled=st.session_state.trading_locked):
                        n = _execute_items(list(st.session_state.basket))
                        st.session_state.basket = []
                        if n:
                            st.toast(f"✅ {n} order(s) executed from basket", icon="🎉")
                        st.rerun()
            else:
                st.markdown(
                    '<div class="empty-box">Basket is empty. Build multi-leg orders (e.g. a spread) '
                    'by choosing an option above and clicking <b>Add to Basket</b> for each leg, '
                    'then execute them together.</div>',
                    unsafe_allow_html=True
                )

            # Order Book (Pending Limits)
            st.markdown('<div class="section-title">Order Book (Pending Limits)</div>', unsafe_allow_html=True)
            st.markdown(
                '<div class="hint-line">A limit order sits here — unfilled — whenever your price hasn\'t '
                'been reached yet: a <b>buy</b> limit fills once the LTP drops to (or below) your price; '
                'a <b>sell</b> limit fills once the LTP rises to (or above) it.</div>',
                unsafe_allow_html=True
            )
            if st.session_state.pending_limits:
                for i, item in enumerate(list(st.session_state.pending_limits)):
                    cols = st.columns([5, 1])
                    with cols[0]:
                        side_cls = "item-side-buy" if item['side'] == 'Buy' else "item-side-sell"
                        gap_to_fill = item.get('ltp', 0) - item['price'] if item['side'] == 'Buy' else item['price'] - item.get('ltp', 0)
                        st.markdown(
                            f"""<div class="item-card">
                                <div>
                                    <span class="item-side-badge {side_cls}">{item['side'].upper()}</span>
                                    <span class="item-main">NIFTY {item['strike']} {item['type']} &nbsp;·&nbsp; {item['lots']} lot @ ₹{item['price']:.2f} LIMIT</span>
                                    <div class="item-sub">Current LTP ₹{item.get('ltp', 0):.2f} · waiting for price to move ₹{abs(gap_to_fill):.2f} more to fill</div>
                                </div>
                            </div>""",
                            unsafe_allow_html=True
                        )
                    with cols[1]:
                        if st.button("Cancel", key=f"cx_lim_{i}", disabled=st.session_state.trading_locked):
                            del st.session_state.pending_limits[i]
                            save_session_state()
                            st.toast("Limit order cancelled", icon="🗑️")
                            st.rerun()
            else:
                st.markdown(
                    '<div class="empty-box">No pending limit orders. A limit order waits here until '
                    'the market price reaches your price.</div>',
                    unsafe_allow_html=True
                )

            chain_container = st.container()
            with chain_container:
                st.markdown('<div class="section-title">Live Option Chain</div>', unsafe_allow_html=True)
                st.caption(f"ATM ₹{atm_strike:,} · DTE {days_to_expiry}d · Live updating")

                display_df = chain_df.copy()
                # Highlight ATM
                def highlight_atm(row):
                    if row['Strike'] == atm_strike:
                        return ['background-color: #e8f4fd'] * len(row)
                    return [''] * len(row)

                st.dataframe(
                    display_df,
                    use_container_width=True,
                    height=340,
                    hide_index=True,
                    column_config={
                        "Strike": st.column_config.NumberColumn("Strike", format="%d"),
                        "CE Price": st.column_config.NumberColumn("CE LTP", format="%.2f"),
                        "CE %": st.column_config.TextColumn("CE %"),
                        "CE Δ": st.column_config.NumberColumn("CE Δ", format="%.3f"),
                        "PE Price": st.column_config.NumberColumn("PE LTP", format="%.2f"),
                        "PE %": st.column_config.TextColumn("PE %"),
                        "PE Δ": st.column_config.NumberColumn("PE Δ", format="%.3f"),
                        "IV %": st.column_config.NumberColumn("IV %", format="%.1f"),
                    }
                )

        # ---------- TAB 2: POSITIONS ----------
        with tab_pos:
            st.markdown('<div class="section-title">Open Trades</div>', unsafe_allow_html=True)
            st.caption(
                "Each executed BUY or SELL remains visible as its own open trade. "
                "Exit closes only that trade; Exit All closes every open trade."
            )

            open_trades = [
                t for t in st.session_state.get("tradebook", [])
                if t.get("status") == "Open"
            ]

            if open_trades:
                for t in open_trades:
                    mark = _mark_open_trade(t, current_price, chain_df)
                    sign = 1 if t.get("side") == "Buy" else -1
                    qty = int(t.get("qty", 0) or 0)
                    entry = float(t.get("entry_price", 0.0) or 0.0)
                    trade_pnl = sign * (float(mark) - entry) * qty
                    pnl_cls = "profit" if trade_pnl >= 0 else "loss"
                    side_cls = "pos-side-buy" if t.get("side") == "Buy" else "pos-side-sell"

                    trade_key = (
                        t.get("supabase_trade_id")
                        or f"{t.get('entry_time','')}_{t.get('strike','')}_{t.get('type','')}_{t.get('side','')}_{qty}"
                    )
                    trade_key = str(trade_key).replace(" ", "_").replace(":", "_")

                    cols = st.columns([6, 2, 1.2])
                    with cols[0]:
                        st.markdown(
                            f"""
                            <div>
                                <div class="pos-instrument">
                                    <span class="{side_cls}">{t.get('side','')}</span>
                                    &nbsp;{instrument_label(t.get('strike'), t.get('type'))}
                                </div>
                                <div class="pos-meta">
                                    Qty {qty:,} · Entry ₹{entry:.2f} · LTP ₹{float(mark):.2f}
                                    · {t.get('strategy_name') or 'Manual trade'}
                                </div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                    with cols[1]:
                        st.markdown(
                            f"<div style='text-align:right;font-weight:700;font-size:16px;' "
                            f"class='{pnl_cls}'>₹{trade_pnl:+,.2f}</div>",
                            unsafe_allow_html=True,
                        )
                    with cols[2]:
                        if st.button(
                            "Exit",
                            key=f"exit_trade_{trade_key}",
                            disabled=st.session_state.trading_locked,
                            use_container_width=True,
                        ):
                            st.session_state.playing = False
                            _close_one_trade_at_market(
                                t, current_price, current_dt, chain_df, "manual"
                            )
                            save_session_state()
                            st.toast("Trade exited", icon="✅")
                            st.rerun()

                if st.button(
                    "Exit All",
                    use_container_width=True,
                    key="btn_exit_all",
                    disabled=st.session_state.trading_locked,
                ):
                    st.session_state.playing = False
                    _close_all_open_trades_at_market(
                        current_price, current_dt, chain_df, "manual_exit_all"
                    )
                    save_session_state()
                    st.toast("All open trades exited", icon="✅")
                    st.rerun()

                # Portfolio-level summary remains netted for risk interpretation.
                st.markdown(f"""
                <div class="pnl-row" style="margin-top:12px; background:#f8f9fb;">
                    <span class="pnl-label">Total Open P&L</span>
                    <span class="pnl-value {'profit' if open_pnl >= 0 else 'loss'}">₹{open_pnl:+,.2f}</span>
                </div>
                """, unsafe_allow_html=True)

                greeks = compute_position_greeks(st.session_state.positions, current_price, T_current)
                st.markdown("**Net Portfolio Greeks**")
                st.markdown(f"""
                <div>
                    <span class="greek-box"><span class="greek-label">Δ</span> {greeks['delta']:+.1f}</span>
                    <span class="greek-box"><span class="greek-label">Γ</span> {greeks['gamma']:+.4f}</span>
                    <span class="greek-box"><span class="greek-label">Θ</span> {greeks['theta']:+.2f}</span>
                    <span class="greek-box"><span class="greek-label">Vega</span> {greeks['vega']:+.2f}</span>
                </div>
                """, unsafe_allow_html=True)

                _open_stats = analyze_payoff(st.session_state.positions)
                _omp_txt = "Unlimited" if _open_stats['max_profit'] is None else f"₹{_open_stats['max_profit']:,.0f}"
                _oml_txt = "Unlimited" if _open_stats['max_loss'] is None else f"₹{_open_stats['max_loss']:,.0f}"
                _obe_txt = ", ".join(f"₹{b:,.0f}" for b in _open_stats['breakevens']) or "—"
                st.markdown("**Combined Payoff at Expiry**")
                st.markdown(f"""
                <div class="preview-box">
                    <div class="preview-grid">
                        <div><div class="preview-label">Max Profit</div>
                            <div class="preview-val" style="color:#00a86b;">{_omp_txt}</div></div>
                        <div><div class="preview-label">Max Loss</div>
                            <div class="preview-val" style="color:#e74c3c;">{_oml_txt}</div></div>
                        <div><div class="preview-label">Breakeven(s)</div>
                            <div class="preview-val" style="font-size:12px;">{_obe_txt}</div></div>
                    </div>
                </div>
                """, unsafe_allow_html=True)
                with st.expander("Show combined payoff diagram", expanded=False):
                    fig_open, _ = render_payoff_diagram(
                        st.session_state.positions,
                        current_price,
                        title="Open Trades — Combined Payoff at Expiry",
                    )
                    st.plotly_chart(
                        fig_open,
                        use_container_width=True,
                        config={"displayModeBar": False},
                    )
            else:
                st.markdown(
                    '<div class="empty-box">No open trades. Use <b>Place Order</b> to BUY or SELL a call/put.</div>',
                    unsafe_allow_html=True,
                )

        # ---------- TAB 3: VIEW GRAPH ----------
        with tab_graph:
            arrow = "▲" if is_up else "▼"
            st.markdown(f"""
            <div style="background:#f8f9fb; padding:10px 16px; border-radius:12px; margin-bottom:10px;
                        display:flex; align-items:center; gap:18px; border:1px solid #eaeaea;">
                <span style="font-weight:700; font-size:16px;">NIFTY 50</span>
                <span style="font-size:24px; font-weight:700;" class="{price_cls}">₹{current_price:,.2f}</span>
                <span class="{price_cls}" style="font-weight:600;">{arrow} {price_pct:+.2f}%</span>
                <span style="color:#777; font-size:13px;">Prev ₹{prev_close:,.2f}</span>
            </div>
            """, unsafe_allow_html=True)

            all_data = sim.iloc[:st.session_state.current_index + 1]
            fig = create_chart(all_data, current_price, session_start=st.session_state.start_time)
            st.plotly_chart(fig, use_container_width=True, config={'displayModeBar': True})

        # ---------- TAB 4: PERFORMANCE AND REPORTS ----------
        with tab_perf:
            st.markdown('<div class="section-title">Current Session</div>', unsafe_allow_html=True)
            total_trades = len(st.session_state.tradebook)
            closed_trades = [t for t in st.session_state.tradebook if t['status'] == 'Closed']
            wins = len([t for t in closed_trades if t['pnl'] > 0])
            win_rate = (wins / len(closed_trades) * 100) if closed_trades else 0.0
            total_now = float(open_pnl + realized_pnl)
            session_return = (total_now / st.session_state.starting_capital * 100.0) if st.session_state.starting_capital else 0.0

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Session", f"#{st.session_state.get('session_no') or '—'}")
            m2.metric("Trades", total_trades)
            m3.metric("Win Rate", f"{win_rate:.1f}%")
            m4.metric("Return", f"{session_return:+.2f}%")

            p1, p2, p3, p4 = st.columns(4)
            p1.metric("Total P&L", f"₹{total_now:+,.0f}")
            p2.metric("Max Drawdown", f"₹{st.session_state.get('max_drawdown', 0.0):,.0f}")
            p3.metric("Max Drawdown %", f"{st.session_state.get('max_drawdown_pct', 0.0):.2f}%")
            peak_m = max(st.session_state.peak_margin_used, 1)
            capital_eff = total_now / peak_m * 100.0
            p4.metric("Return on Margin", f"{capital_eff:+.2f}%")

            st.markdown('<div class="section-title">Executed Trades</div>', unsafe_allow_html=True)
            if st.session_state.tradebook:
                rows = []
                for i, t in enumerate(st.session_state.tradebook, start=1):
                    capital = max(float(t.get('capital_used', 0.0) or 0.0), 1.0)
                    rows.append({
                        "#": i,
                        "Strategy": t.get('strategy_name', 'Manual trade'),
                        "Instrument": instrument_label(t.get('strike', 0), t.get('type')),
                        "Side": t.get('side'),
                        "Qty": t.get('qty'),
                        "Entry": t.get('entry_price'),
                        "Exit": t.get('exit_price') if t.get('status') == 'Closed' else None,
                        "P&L (₹)": t.get('pnl', 0.0),
                        "Return %": float(t.get('pnl', 0.0)) / capital * 100.0 if t.get('status') == 'Closed' else None,
                        "Max DD %": t.get('max_drawdown_pct', 0.0),
                        "Holding (min)": round(float(t.get('holding_minutes', 0.0) or 0.0), 1),
                        "Status": t.get('status'),
                    })
                tb_view = pd.DataFrame(rows)
                st.dataframe(
                    tb_view,
                    use_container_width=True,
                    hide_index=True,
                    height=min(330, 42 + 36 * len(tb_view)),
                    column_config={
                        "Entry": st.column_config.NumberColumn(format="₹%.2f"),
                        "Exit": st.column_config.NumberColumn(format="₹%.2f"),
                        "P&L (₹)": st.column_config.NumberColumn(format="₹%.2f"),
                        "Return %": st.column_config.NumberColumn(format="%.2f%%"),
                        "Max DD %": st.column_config.NumberColumn(format="%.2f%%"),
                    },
                )
            else:
                st.caption("No executed trades yet.")

            # Longitudinal learning dashboard for this student.
            st.markdown('<div class="section-title">My Progress</div>', unsafe_allow_html=True)
            history = get_student_history(st.session_state.get('participant_id')) if supabase_enabled() else []
            completed_history = [h for h in history if h.get('status') == 'completed']
            if completed_history:
                cum_pnl = sum(float(h.get('total_pnl') or 0.0) for h in completed_history)
                hist_trades = sum(int(h.get('total_trades') or 0) for h in completed_history)
                best_session = max(float(h.get('total_pnl') or 0.0) for h in completed_history)
                worst_dd = max(float(h.get('max_drawdown_pct') or 0.0) for h in completed_history)
                h1, h2, h3, h4 = st.columns(4)
                h1.metric("Completed Sessions", len(completed_history))
                h2.metric("Historical Trades", hist_trades)
                h3.metric("Cumulative P&L", f"₹{cum_pnl:+,.0f}")
                h4.metric("Worst Drawdown", f"{worst_dd:.2f}%")

                hist_df = pd.DataFrame(completed_history)
                display_cols = [c for c in ["session_no", "strategy_focus", "total_trades", "total_pnl", "return_pct", "max_drawdown_pct", "win_rate_pct", "profit_factor"] if c in hist_df.columns]
                hist_df = hist_df[display_cols].rename(columns={
                    "session_no": "Session",
                    "strategy_focus": "Learning Focus",
                    "total_trades": "Trades",
                    "total_pnl": "P&L (₹)",
                    "return_pct": "Return %",
                    "max_drawdown_pct": "Max DD %",
                    "win_rate_pct": "Win Rate %",
                    "profit_factor": "Profit Factor",
                })
                st.dataframe(hist_df, use_container_width=True, hide_index=True, height=min(280, 42 + 36 * len(hist_df)))

                student_trade_rows = get_student_trades(st.session_state.get('participant_id'), limit=300)
                if student_trade_rows:
                    td = pd.DataFrame(student_trade_rows)
                    if 'strategy_name' in td.columns and 'pnl' in td.columns:
                        strat = td.groupby('strategy_name', dropna=False).agg(
                            Trades=('pnl', 'count'),
                            Net_PnL=('pnl', 'sum'),
                            Avg_Return=('return_pct', 'mean'),
                            Avg_Max_DD=('max_drawdown_pct', 'mean')
                        ).reset_index().sort_values('Net_PnL', ascending=False)
                        strat = strat.rename(columns={"strategy_name": "Strategy", "Net_PnL": "Net P&L (₹)", "Avg_Return": "Avg Return %", "Avg_Max_DD": "Avg Max DD %"})
                        with st.expander("Strategy-level progress"):
                            st.dataframe(strat, use_container_width=True, hide_index=True)
            else:
                st.caption("Complete your first session to start building a historical performance record.")

            # Net Greeks
            if st.session_state.positions:
                greeks = compute_position_greeks(st.session_state.positions, current_price, T_current)
                st.markdown("**Net Greeks (Open)**")
                st.markdown(f"""
                <div>
                    <span class="greek-box"><span class="greek-label">Δ</span> {greeks['delta']:+.1f}</span>
                    <span class="greek-box"><span class="greek-label">Γ</span> {greeks['gamma']:+.4f}</span>
                    <span class="greek-box"><span class="greek-label">Θ</span> {greeks['theta']:+.2f}</span>
                    <span class="greek-box"><span class="greek-label">Vega</span> {greeks['vega']:+.2f}</span>
                </div>
                """, unsafe_allow_html=True)
                st.markdown(
                    '<div class="hint-line">Δ = P&amp;L per point of NIFTY · Γ = how fast Δ itself moves · '
                    'Θ = P&amp;L lost per day to time decay · Vega = P&amp;L per 1pt move in IV. '
                    '(See the Positions tab for the full explanation.)</div>',
                    unsafe_allow_html=True
                )

            # Hold-to-Day-22 hypothetical (replaces What-If)
            st.markdown(f'<div class="section-title">Hypothetical: Held {HOLD_DAYS} Days vs Actual Exit</div>', unsafe_allow_html=True)
            hold_table, hold_labels = compute_hold_to_expiry_table(current_price)
            if hold_table is not None:
                st.caption("Rows = P&L if each closed position had instead been held to that day. "
                           "Last row = what was actually realized on exit.")
                insight = summarize_hold_vs_exit(hold_table)
                if insight:
                    st.markdown(f'<div class="insight-box">💡 {insight}</div>', unsafe_allow_html=True)
                st.dataframe(
                    hold_table.style.format("{:+.2f}"),
                    use_container_width=True, height=340
                )
            else:
                st.caption("No closed positions yet — this table populates once you exit a trade.")

            # Model documentation
            st.markdown('<div class="section-title">Model Documentation</div>', unsafe_allow_html=True)
            st.caption("Mathematical specification of the GARCH path, Black–Scholes pricing, IV surface, and hold-to-expiry attribution.")
            _model_pdf_candidates = [
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "Model_Math.pdf"),
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts", "Model_Math.pdf"),
                "/home/workdir/artifacts/Model_Math.pdf",
                "/home/workdir/attachments/Model_Math.pdf",
            ]
            _model_pdf = next((p for p in _model_pdf_candidates if os.path.exists(p)), None)
            if _model_pdf:
                with open(_model_pdf, "rb") as _mf:
                    st.download_button(
                        "Download Model Specification (PDF)",
                        _mf,
                        file_name="Model_Math.pdf",
                        mime="application/pdf",
                        key="btn_model_pdf",
                        use_container_width=True,
                    )
            else:
                st.caption("Model PDF not found on disk.")

            # Reflection makes the simulator a learning journal, not only a scorecard.
            st.markdown('<div class="section-title">Session Reflection</div>', unsafe_allow_html=True)
            st.session_state.reflection_note = st.text_area(
                "What did you learn from this session? (optional)",
                value=st.session_state.get("reflection_note", ""),
                max_chars=800,
                placeholder="Example: My directional view was right, but I entered too early and used too much size. Next session I will test a defined-risk spread.",
                key="reflection_input",
            )

            # Finish & Report
            st.markdown('<div class="section-title">Finish Session & Report</div>', unsafe_allow_html=True)
            if not st.session_state.session_finished:
                if st.button("Finish Session & Generate PDF Report", use_container_width=True, type="primary", key="btn_finish"):
                    cancel_pending_order_records(st.session_state.get("pending_limits", []), "finish_session")
                    st.session_state.pending_limits = []
                    # Close every remaining open trade at the current simulated market mark.
                    # Using the tradebook avoids missing offsetting trades whose net position is zero.
                    if any(t.get("status") == "Open" for t in st.session_state.get("tradebook", [])):
                        _close_all_open_trades_at_market(
                            current_price, current_dt, chain_df, "finish_session"
                        )
                    with st.spinner("Generating PDF..."):
                        path, fname = generate_pdf_report()
                        st.session_state.report_path = path
                        st.session_state.report_generated = True
                        st.session_state.session_finished = True
                        st.session_state.trading_locked = True

                        if supabase_enabled():
                            try:
                                finish_session_record(
                                    current_price=current_price,
                                    current_day_num=current_day_num,
                                    status="completed"
                                )
                                upload_report_record(path, fname)
                            except Exception as exc:
                                st.warning(f"PDF generated locally, but cloud report backup failed: {exc}")

                        save_session_state()
                        st.success(f"Report generated: {fname}")
                        if os.path.exists(path):
                            with open(path, "rb") as f:
                                st.download_button("Download PDF Report", f, file_name=fname, mime="application/pdf")
                    st.rerun()
            else:
                st.success("Session finished. Report available.")
                if st.session_state.report_path and os.path.exists(st.session_state.report_path):
                    with open(st.session_state.report_path, "rb") as f:
                        st.download_button("Download PDF Report", f,
                                           file_name=os.path.basename(st.session_state.report_path),
                                           mime="application/pdf")

        # ---------- TAB 5: LEADERBOARD ----------
        with tab_leaderboard:
            st.markdown(
                '<div class="section-title">Top 5 — Best Single-Session Realized P&L</div>',
                unsafe_allow_html=True
            )
            st.caption(
                "Each market run is treated independently. A student's sessions are never added together. "
                "The leaderboard uses that student's highest realized P&L from any session, including an active "
                "session once at least one trade has been closed."
            )

            def _draw_leaderboard():
                lb = get_leaderboard()
                if lb:
                    lb_df = pd.DataFrame(lb)
                    preferred = [
                        c for c in [
                            "rank", "student_id", "student_name",
                            "best_session_profit", "best_session_no",
                            "best_session_status", "closed_trades",
                            "win_rate_pct", "max_drawdown_pct"
                        ]
                        if c in lb_df.columns
                    ]
                    lb_df = lb_df[preferred].rename(columns={
                        "rank": "Rank",
                        "student_id": "Student ID",
                        "student_name": "Student Name",
                        "best_session_profit": "Best Realized P&L (₹)",
                        "best_session_no": "Best Session",
                        "best_session_status": "Session Status",
                        "closed_trades": "Closed Trades",
                        "win_rate_pct": "Win Rate %",
                        "max_drawdown_pct": "Max Drawdown %",
                    })
                    st.dataframe(
                        lb_df,
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "Best Realized P&L (₹)": st.column_config.NumberColumn(format="₹%.2f"),
                            "Win Rate %": st.column_config.NumberColumn(format="%.1f%%"),
                            "Max Drawdown %": st.column_config.NumberColumn(format="%.2f%%"),
                        },
                    )
                    st.caption(
                        "Best single independent session only · realized P&L only · "
                        "open P&L is excluded · refresh interval 10 seconds"
                    )
                else:
                    st.info(
                        "The leaderboard will appear after at least one student closes a trade "
                        "and realizes P&L in a session."
                    )

            if hasattr(st, "fragment"):
                @st.fragment(run_every="10s")
                def _leaderboard_fragment():
                    _draw_leaderboard()
                _leaderboard_fragment()
            else:
                _draw_leaderboard()

        # ===== LOW-FLICKER MARKET CLOCK =====
        # Streamlit reconstructs the page on a full rerun. Redrawing every 5 seconds
        # still produces visible flicker on a large dashboard. We therefore batch
        # market-bar advancement and perform fewer full redraws.
        #
        # At 1x: 1 bar = 5 real seconds, UI redraw approximately every 15 seconds
        # and advances ~3 bars in one pass. Faster speeds redraw more often, but never
        # faster than every 5 seconds. The simulated elapsed time remains consistent.
    if st.session_state.playing and st.session_state.current_index < n_bars - 1:
        seconds_per_bar = TICK_SECONDS_BASE / max(float(st.session_state.speed), 0.1)
        ui_refresh_seconds = max(5.0, min(15.0, seconds_per_bar * 3.0))
        poll_seconds = 1.0

        @st.fragment(run_every=poll_seconds)
        def _market_clock_fragment():
            if not st.session_state.get("playing", False):
                return

            now = time.time()
            last = float(st.session_state.get("last_update", now))
            elapsed = now - last

            if elapsed < ui_refresh_seconds:
                return

            bars_due = max(1, int(elapsed / max(seconds_per_bar, 0.001)))
            new_index = min(
                int(st.session_state.current_index) + bars_due,
                n_bars - 1,
            )

            if new_index == int(st.session_state.current_index):
                return

            st.session_state.current_index = new_index
            st.session_state.max_reached_index = max(
                int(st.session_state.get("max_reached_index", 0)),
                new_index,
            )

            # Preserve unconsumed elapsed time so the simulated clock does not drift.
            consumed = bars_due * seconds_per_bar
            st.session_state.last_update = last + consumed
            if st.session_state.last_update > now:
                st.session_state.last_update = now

            # Save once per visible batch rather than on every hidden bar.
            save_session_state()

            # One full page redraw per batch, not one per individual 5-minute bar.
            st.rerun()

        _market_clock_fragment()

    elif st.session_state.current_index >= n_bars - 1:
        st.session_state.playing = False
        if any(t.get("status") == "Open" for t in st.session_state.get("tradebook", [])) \
                and not st.session_state.trading_locked:
            _settle_all_cash(current_price, current_dt)
        st.session_state.pending_limits = []
        st.session_state.trading_locked = True
        save_session_state()

if __name__ == "__main__":
    main()
