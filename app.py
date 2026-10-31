import re
from datetime import date
import pandas as pd
import requests
from dateutil.relativedelta import relativedelta
from fastapi import FastAPI, HTTPException, Query

app = FastAPI(title="Tennis Abstract Analyst", version="0.1.0")
BASE_WTA = "https://www.tennisabstract.com/cgi-bin/wplayer.cgi?p="
BASE_ATP = "https://www.tennisabstract.com/cgi-bin/player.cgi?p="
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.tennisabstract.com/"}
BUCKETS = [f"{s} - {v}" for s in ("Hard","Clay","Grass") for v in ("Indoor","Outdoor","Unknown")]
ALIASES = {
"date":["date"],"tournament":["tournament","event"],"surface":["surface"],"round":["round","rnd"],
"opponent":["opponent","opponent name"],"score":["score","result"],"aces":["aces","ace"],
"dfs":["dfs","double faults","double fault"],"first_in":["1stin","first in","first serve in"],
"first_won":["1stwon","first won","1st serve points won"],"second_won":["2ndwon","second won","2nd serve points won"],
"serve_pts":["svpts","serve points","service points"],"sv_games_won":["svgmswon","service games won","holds"],
"sv_games":["svgms","service games"],"bp_saved":["bpsaved","break points saved"],"bp_faced":["bpfaced","break points faced"],
"ret_pts_won":["retptswon","return points won"],"ret_pts":["retpts","return points"],
"ret_games_won":["retgmswon","return games won","breaks"],"ret_games":["retgms","return games"],
"venue":["venue","conditions","indoor/outdoor"]}
def norm(x): return re.sub(r"[^a-z0-9]","",str(x).lower())
def col_for(cols,key):
    for a in ALIASES[key]:
        for c in cols:
            if norm(c)==norm(a) or (len(norm(a))>3 and norm(a) in norm(c)): return c
    return None
def num(x):
    if pd.isna(x) or re.search(r"\d+\s*/\s*\d+",str(x)): return None
    m=re.search(r"-?\d+(?:\.\d+)?",str(x))
    return float(m.group()) if m else None
def frac(x):
    m=re.search(r"(\d+)\s*/\s*(\d+)",str(x))
    return (float(m.group(1)),float(m.group(2))) if m else (None,None)
def pct(a,b): return round(100*a/b,2) if pd.notna(a) and pd.notna(b) and b else None
def retrieve(player, tour="auto"):
    slug=re.sub(r"[^A-Za-z0-9]","",player)
    if tour not in ("auto", "atp", "wta"):
        raise HTTPException(400, "tour must be auto, atp or wta")
    # Tennis Abstract uses player.cgi for ATP and wplayer.cgi for WTA.
    # Auto tries the ATP route first, then the WTA route if no usable profile is found.
    urls = ([("atp", "https://www.tennisabstract.com/cgi-bin/player.cgi?p="+slug),
             ("wta", "https://www.tennisabstract.com/cgi-bin/wplayer.cgi?p="+slug)]
            if tour == "auto" else
            [(tour, ("https://www.tennisabstract.com/cgi-bin/player.cgi?p=" if tour == "atp" else "https://www.tennisabstract.com/cgi-bin/wplayer.cgi?p=")+slug)])
    last_error = None
    tables = None
    url = None
    for candidate_tour, candidate_url in urls:
        try:
            res=requests.get(candidate_url,headers=HEADERS,timeout=40)
            res.raise_for_status()
            candidate_tables=pd.read_html(res.text)
            if candidate_tables:
                tables=candidate_tables
                url=candidate_url
                break
        except Exception as e:
            last_error=e
    if tables is None:
        raise HTTPException(502,f"Tennis Abstract retrieval failed for {tour} profile: {type(last_error).__name__}: {last_error}")
    rows=[]
    for t in tables:
        cols=list(t.columns); dc=col_for(cols,"date"); oc=col_for(cols,"opponent")
        if dc is None or oc is None: continue
        for _,r in t.iterrows():
            z={"player":player}
            for k in ALIASES:
                c=col_for(cols,k)
                if c is not None: z[k]=r[c]
            if "date" not in z: continue
            z["date"]=pd.to_datetime(z["date"],errors="coerce")
            for k in ["aces","dfs","first_in","first_won","second_won","serve_pts","sv_games_won","sv_games","bp_saved","bp_faced","ret_pts_won","ret_pts","ret_games_won","ret_games"]:
                if k in z:
                    n,d=frac(z[k]); z[k+"_n"]=n; z[k+"_d"]=d; z[k]=num(z[k])
            rows.append(z)
    if not rows: raise HTTPException(502,"No recognizable match-log table. Tennis Abstract markup may have changed or this profile exposes no match log.")
    df=pd.DataFrame(rows).dropna(subset=["date"])
    keys=[k for k in ["date","tournament","opponent","score"] if k in df]
    return df.drop_duplicates(subset=keys).sort_values("date",ascending=False),url
def enrich(df,venue_overrides=None):
    x=df.copy()
    s=x["surface"].astype(str).str.lower() if "surface" in x else pd.Series("",index=x.index)
    x["surface_type"]="Unknown"
    for term in ["Hard","Clay","Grass"]: x.loc[s.str.contains(term.lower()),"surface_type"]=term
    x["venue_type"]="Unknown"
    if "venue" in x:
        v=x["venue"].astype(str).str.lower()
        x.loc[v.str.contains(r"\bindoor\b"),"venue_type"]="Indoor"
        x.loc[v.str.contains(r"\boutdoor\b"),"venue_type"]="Outdoor"
    for event,venue in (venue_overrides or {}).items():
        if venue in ["Indoor","Outdoor"] and "tournament" in x:
            x.loc[x.tournament.astype(str).str.casefold()==event.casefold(),"venue_type"]=venue
    x["surface_bucket"]=x.surface_type+" - "+x.venue_type
    x.loc[x.surface_type.eq("Unknown"),"surface_bucket"]="Unknown"
    x["first_in_pct"]=[pct(n,d) for n,d in zip(x.get("first_in_n",x.get("first_in",pd.Series(index=x.index,dtype=float))),x.get("serve_pts",pd.Series(index=x.index,dtype=float)))]
    x["first_won_pct"]=[pct(n,d) for n,d in zip(x.get("first_won_n",x.get("first_won",pd.Series(index=x.index,dtype=float))),x.get("first_in_n",x.get("first_in",pd.Series(index=x.index,dtype=float))))]
    second=x.get("serve_pts",pd.Series(index=x.index,dtype=float))-x.get("first_in",pd.Series(index=x.index,dtype=float))
    x["second_won_pct"]=[pct(n,d) for n,d in zip(x.get("second_won",pd.Series(index=x.index,dtype=float)),second)]
    for out,n,d in [("service_games_won_pct","sv_games_won","sv_games"),("bp_saved_pct","bp_saved","bp_faced"),("return_points_won_pct","ret_pts_won","ret_pts"),("return_games_won_pct","ret_games_won","ret_games")]:
        x[out]=[pct(a,b) for a,b in zip(x.get(n,pd.Series(index=x.index,dtype=float)),x.get(d,pd.Series(index=x.index,dtype=float)))]
    return x
METRICS=[("Aces per match","aces"),("Double faults per match","dfs"),("First serve %","first_in_pct"),("1st serve points won %","first_won_pct"),("2nd serve points won %","second_won_pct"),("Service games won %","service_games_won_pct"),("Break points saved %","bp_saved_pct"),("Return points won %","return_points_won_pct"),("Return games won %","return_games_won_pct")]
def avg(df,c):
    if c not in df: return None
    a=pd.to_numeric(df[c],errors="coerce").dropna()
    return round(float(a.mean()),2) if len(a) else None
def report(player,recent_n=10,tour="auto"):
    raw,url=retrieve(player,tour); df=enrich(raw); today=pd.Timestamp(date.today())
    cuts=[today-relativedelta(months=6),today-relativedelta(months=12)]
    out={}
    for b in BUCKETS:
        p=df[df.surface_bucket.eq(b)].sort_values("date",ascending=False)
        if p.empty: continue
        usable=p.dropna(subset=[c for _,c in METRICS if c in p],how="all").head(5)
        six=p[p.date>=cuts[0]]; twelve=p[p.date>=cuts[1]]
        stats=[]
        for label,c in METRICS:
            last=avg(usable,c); career=avg(p,c)
            trend="Insufficient data" if last is None or career is None else ("Stable" if abs(last-career)<=(.25 if "per match" in label else 1) else ("Above career surface average" if last>career else "Below career surface average"))
            stats.append({"statistic":label,"last_5":last,"six_month":avg(six,c),"twelve_month":avg(twelve,c),"career":career,"trend":trend})
        results=[]
        for _,r in p.head(recent_n).iterrows():
            results.append({k:(r[k].date().isoformat() if k=="date" and pd.notna(r[k]) else (str(r[k]) if k in r and pd.notna(r[k]) else None)) for k in ["date","tournament","round","opponent","score","surface"]})
        out[b]={"recent_results":results,"last_five_with_stats":len(usable),"sample_sizes":{"six_month":len(six),"twelve_month":len(twelve),"career":len(p)},"statistics":stats}
    return {"player":player,"source":"Tennis Abstract","source_url":url,"retrieved_on":date.today().isoformat(),"match_rows":len(df),"surface_venue_reports":out,"notes":["Null means unavailable, not zero.","Venue is Unknown unless explicit on page or supplied as a verified override.","Last-five statistics use up to five latest matches with at least one usable statistic.","Averages are match-level means."]}
@app.get("/")
def root(): return {"service":"Tennis Abstract Analyst API","docs":"/docs","example":"/player/Antonia%20Ruzic?recent_n=10"}
@app.get("/health")
def health(): return {"status":"ok"}
@app.get("/player/{player_name}")
def player(player_name:str,recent_n:int=Query(10,ge=5,le=10),tour:str=Query("auto",pattern="^(auto|atp|wta)$")): return report(player_name,recent_n,tour)
