import re
from datetime import date
from functools import lru_cache

import pandas as pd
import requests
from fastapi import FastAPI, HTTPException, Query

app = FastAPI(title="Tennis Abstract Analyst - Free Data Edition", version="0.2.0")

ARCHIVE = "https://raw.githubusercontent.com/Aneeshers/tennis-sackmann-archive/main"
TOURS = {
    "atp": ("atp", "atp_matches_{year}.csv"),
    "wta": ("wta", "wta_matches_{year}.csv"),
}
HEADERS = {"User-Agent": "Tennis-Stats-Research/1.0 (personal non-commercial use)"}
BUCKETS = [f"{s} - {v}" for s in ("Hard", "Clay", "Grass") for v in ("Indoor", "Outdoor", "Unknown")]

SOURCE_COLUMNS = {
    "tourney_date", "tourney_name", "surface", "round", "winner_name",
    "loser_name", "score",
    "w_ace", "w_df", "w_svpt", "w_1stIn", "w_1stWon", "w_2ndWon", "w_SvGms", "w_bpSaved", "w_bpFaced",
    "l_ace", "l_df", "l_svpt", "l_1stIn", "l_1stWon", "l_2ndWon", "l_SvGms", "l_bpSaved", "l_bpFaced",
}

def load_player_year(tour: str, year: int, player: str):
    """Stream one yearly CSV and retain only this player's rows to limit RAM."""
    folder, filename = TOURS[tour]
    url = f"{ARCHIVE}/{folder}/{filename.format(year=year)}"
    response = requests.get(url, headers=HEADERS, timeout=(10, 90), stream=True)
    if response.status_code == 404:
        return pd.DataFrame()
    response.raise_for_status()
    response.raw.decode_content = True
    target = clean_name(player)
    matches = []
    try:
        chunks = pd.read_csv(
            response.raw,
            chunksize=25000,
            usecols=lambda col: col in SOURCE_COLUMNS,
            low_memory=True,
        )
        for chunk in chunks:
            winners = chunk["winner_name"].fillna("").map(clean_name)
            losers = chunk["loser_name"].fillna("").map(clean_name)
            selected = chunk[(winners == target) | (losers == target)]
            if not selected.empty:
                matches.append(selected.copy())
    finally:
        response.close()
    return pd.concat(matches, ignore_index=True) if matches else pd.DataFrame()

def clean_name(value):
    return re.sub(r"[^a-z]", "", str(value).casefold())

def pct(n, d):
    try:
        if pd.isna(n) or pd.isna(d) or float(d) == 0:
            return None
        return round(100.0 * float(n) / float(d), 2)
    except (TypeError, ValueError):
        return None

def safe_num(row, key):
    value = row.get(key)
    try:
        return float(value) if pd.notna(value) else None
    except (TypeError, ValueError):
        return None

def make_player_rows(matches, player):
    target = clean_name(player)
    rows = []
    for _, m in matches.iterrows():
        winner = clean_name(m.get("winner_name", ""))
        loser = clean_name(m.get("loser_name", ""))
        if target not in (winner, loser):
            continue
        is_winner = target == winner
        prefix = "w_" if is_winner else "l_"
        opp_prefix = "l_" if is_winner else "w_"
        r = {
            "date": pd.to_datetime(str(m.get("tourney_date", "")), format="%Y%m%d", errors="coerce"),
            "tournament": m.get("tourney_name"),
            "round": m.get("round"),
            "opponent": m.get("loser_name" if is_winner else "winner_name"),
            "score": m.get("score"),
            "surface": m.get("surface"),
            "result": "W" if is_winner else "L",
            "aces": safe_num(m, prefix + "ace"),
            "dfs": safe_num(m, prefix + "df"),
        }
        svpt = safe_num(m, prefix + "svpt")
        first_in = safe_num(m, prefix + "1stIn")
        first_won = safe_num(m, prefix + "1stWon")
        second_won = safe_num(m, prefix + "2ndWon")
        r["first_in_pct"] = pct(first_in, svpt)
        r["first_won_pct"] = pct(first_won, first_in)
        r["second_won_pct"] = pct(second_won, (svpt - first_in) if svpt is not None and first_in is not None else None)
        faced_svpt = safe_num(m, opp_prefix + "svpt")
        opp_first_won = safe_num(m, opp_prefix + "1stWon")
        opp_second_won = safe_num(m, opp_prefix + "2ndWon")
        return_won = (faced_svpt - opp_first_won - opp_second_won
                      if None not in (faced_svpt, opp_first_won, opp_second_won) else None)
        r["return_points_won_pct"] = pct(return_won, faced_svpt)
        bp_saved = safe_num(m, prefix + "bpSaved")
        bp_faced = safe_num(m, prefix + "bpFaced")
        r["bp_saved_pct"] = pct(bp_saved, bp_faced)
        # The source does not provide total return games won or total service games won.
        r["service_games_won_pct"] = None
        r["return_games_won_pct"] = None
        # Venue is not supplied in this archive; do not infer indoor/outdoor from event name.
        r["venue_type"] = "Unknown"
        surface = str(r["surface"]).title() if pd.notna(r["surface"]) else "Unknown"
        r["surface_type"] = surface if surface in ("Hard", "Clay", "Grass") else "Unknown"
        r["surface_bucket"] = r["surface_type"] + " - Unknown" if r["surface_type"] != "Unknown" else "Unknown"
        rows.append(r)
    return pd.DataFrame(rows)

METRICS = [
    ("Aces per match", "aces"),
    ("Double faults per match", "dfs"),
    ("First serve %", "first_in_pct"),
    ("1st serve points won %", "first_won_pct"),
    ("2nd serve points won %", "second_won_pct"),
    ("Service games won %", "service_games_won_pct"),
    ("Break points saved %", "bp_saved_pct"),
    ("Return points won %", "return_points_won_pct"),
    ("Return games won %", "return_games_won_pct"),
]

def mean_value(frame, column):
    if column not in frame:
        return None
    values = pd.to_numeric(frame[column], errors="coerce").dropna()
    return round(float(values.mean()), 2) if len(values) else None

def report(player, recent_n=10, tour="auto"):
    if tour not in ("auto", "atp", "wta"):
        raise HTTPException(400, "tour must be auto, atp or wta")
    tours_to_try = ["atp", "wta"] if tour == "auto" else [tour]
    current_year = date.today().year
    last_error = None
    selected_tour = None
    frames = []
    for candidate in tours_to_try:
        yearly_player_rows = []
        try:
            for year in range(2021, current_year + 1):
                data = load_player_year(candidate, year, player)
                if not data.empty:
                    yearly_player_rows.append(data)
            if yearly_player_rows:
                combined_player_matches = pd.concat(yearly_player_rows, ignore_index=True)
                candidate_rows = make_player_rows(combined_player_matches, player)
                if not candidate_rows.empty:
                    frames = [candidate_rows]
                    selected_tour = candidate
                    break
        except Exception as exc:
            last_error = exc
    if not frames:
        if last_error:
            raise HTTPException(502, f"Free dataset retrieval failed: {type(last_error).__name__}: {last_error}")
        raise HTTPException(404, "Player not found in the free 2021-present ATP/WTA match archive. Try the exact player name or set tour=atp/wta.")
    df = frames[0].dropna(subset=["date"]).sort_values("date", ascending=False)
    df = df.drop_duplicates(subset=["date", "tournament", "opponent", "score"])
    recent = []
    for _, r in df.head(recent_n).iterrows():
        recent.append({
            "date": r["date"].date().isoformat(),
            "tournament": None if pd.isna(r["tournament"]) else str(r["tournament"]),
            "round": None if pd.isna(r["round"]) else str(r["round"]),
            "opponent": None if pd.isna(r["opponent"]) else str(r["opponent"]),
            "result": r["result"],
            "score": None if pd.isna(r["score"]) else str(r["score"]),
            "surface": None if pd.isna(r["surface"]) else str(r["surface"]),
        })
    out = {}
    today = pd.Timestamp(date.today())
    six_month_cutoff = today - pd.DateOffset(months=6)
    twelve_month_cutoff = today - pd.DateOffset(months=12)
    for bucket in BUCKETS:
        subset = df[df.surface_bucket == bucket].sort_values("date", ascending=False)
        if subset.empty:
            continue
        last_five = subset.head(5)
        six_month = subset[subset.date >= six_month_cutoff]
        twelve_month = subset[subset.date >= twelve_month_cutoff]
        stats = []
        for label, column in METRICS:
            last = mean_value(last_five, column)
            career = mean_value(subset, column)
            if last is None or career is None:
                trend = "N/A - insufficient source statistics"
            elif abs(last - career) <= (0.25 if "per match" in label else 1):
                trend = "Stable"
            elif last > career:
                trend = "Above career surface average"
            else:
                trend = "Below career surface average"
            stats.append({
                "statistic": label,
                "last_5": last,
                "six_month": mean_value(six_month, column),
                "twelve_month": mean_value(twelve_month, column),
                "career": career,
                "trend": trend,
            })
        out[bucket] = {
            "last_five_with_stats": int(last_five.dropna(subset=[c for _, c in METRICS], how="all").shape[0]),
            "sample_sizes": {
                "six_month": int(len(six_month)),
                "twelve_month": int(len(twelve_month)),
                "career_since_2021": int(len(subset)),
            },
            "statistics": stats,
        }
    return {
        "player": player,
        "tour": selected_tour.upper(),
        "source": "Jeff Sackmann tennis datasets (archival GitHub mirror)",
        "source_url": "https://github.com/Aneeshers/tennis-sackmann-archive",
        "source_coverage": "2021-present match archive; source update cadence may vary",
        "license_note": "CC BY-NC-SA 4.0: attribution required; non-commercial use only; share-alike applies.",
        "retrieved_on": date.today().isoformat(),
        "matches_found": int(len(df)),
        "recent_results": recent,
        "surface_venue_reports": out,
        "notes": [
            "N/A means the source does not provide that statistic; it is not zero.",
            "Indoor/outdoor is Unknown because the source does not provide verified venue type.",
            "Only Hard, Clay and Grass are split into surface buckets.",
            "WTA match-level serve statistics are generally absent in this source; unavailable values remain N/A.",
            "Service-games-won percentage and return-games-won percentage are not supplied and are not inferred.",
            "Recent and average statistics are based on available matches since 2021, not a complete career archive.",
        ],
    }

@app.get("/")
def root():
    return {"service": "Tennis Analyst API - Free Data Edition", "docs": "/docs", "example_atp": "/player/Novak%20Djokovic?recent_n=10&tour=atp", "example_wta": "/player/Elena%20Rybakina?recent_n=10&tour=wta"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/player/{player_name}")
def player(
    player_name: str,
    recent_n: int = Query(10, ge=5, le=10),
    tour: str = Query("auto", pattern="^(auto|atp|wta)$"),
):
    return report(player_name, recent_n, tour)
