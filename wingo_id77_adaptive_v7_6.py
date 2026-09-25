#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse, csv, json, math, os, sqlite3, tempfile, time
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import requests
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier, HistGradientBoostingClassifier
from wingo_realbet import RealBetExecutor, RealBetError

API_URL = "https://www.88idd.com/server/lottery/getCurrentLotteryInfo?lottery_id=77"
ODDS = {"size":{"Lớn":1.979,"Nhỏ":1.979},"color":{"Đỏ":2.089,"Xanh":2.089,"Tím":4.925},"number":{str(i):9.895 for i in range(10)}}
MODEL_NAMES = ["rolling","markov1","markov2","markov3","logistic","random_forest","hist_gradient_boosting"]
PRIORS = {"rolling":.25,"markov1":.20,"markov2":.18,"markov3":.12,"logistic":.10,"random_forest":.08,"hist_gradient_boosting":.07}
TOP7_ODDS = 9.895
TOP7_COUNT = 7
TOP7_BREAK_EVEN = TOP7_COUNT / TOP7_ODDS

def now(): return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
def size_of(n): return "Nhỏ" if n <= 4 else "Lớn"
def color_of(n):
    if n == 0: return "ĐỏTím"
    if n == 5: return "XanhTím"
    return "Đỏ" if n % 2 == 0 else "Xanh"
def norm(p):
    p=np.clip(np.asarray(p,dtype=float),1e-9,None); return p/p.sum()
def money(v): return f"{float(v):,.0f}".replace(",", ".")

def atomic_csv(path, header, rows):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=path.stem+"_",suffix=".tmp",dir=str(path.parent)); os.close(fd)
    try:
        with open(tmp,"w",newline="",encoding="utf-8-sig") as f:
            w=csv.writer(f); w.writerow(header); w.writerows(rows)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except: pass

def ensure_column(conn, table, column, ddl_type):
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        print(f"[DB MIGRATION] {table}: add column {column}")
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
        conn.commit()

def init_db(path, bankroll, stake):
    c=sqlite3.connect(path,timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")

    c.execute("CREATE TABLE IF NOT EXISTS results(issue TEXT PRIMARY KEY,number INTEGER,size TEXT,color TEXT,collected_at TEXT,source TEXT)")

    c.execute("""CREATE TABLE IF NOT EXISTS adaptive_predictions(
        issue TEXT PRIMARY KEY,created_at TEXT,sample_size INTEGER,ensemble_probs_json TEXT,
        model_probs_json TEXT,weights_json TEXT,p_large REAL,p_small REAL,p_red REAL,p_green REAL,p_violet REAL,
        side_pick TEXT,side_probability REAL,confidence_label TEXT,actual_number INTEGER,resolved_at TEXT)""")

    c.execute("CREATE TABLE IF NOT EXISTS paper_settings(id INTEGER PRIMARY KEY CHECK(id=1),initial_bankroll REAL,default_stake REAL,created_at TEXT)")
    c.execute("INSERT OR IGNORE INTO paper_settings VALUES(1,?,?,?)",(bankroll,stake,now()))

    c.execute("""CREATE TABLE IF NOT EXISTS paper_bets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,issue TEXT,market TEXT,selection TEXT,probability REAL,odds REAL,stake REAL,
        estimated_edge REAL,confidence_label TEXT,status TEXT DEFAULT 'OPEN',actual_number INTEGER,payout REAL,profit REAL,
        created_at TEXT,resolved_at TEXT,UNIQUE(issue,market))""")

    # Migration cho DB cũ từ v4/v5. Không xóa dữ liệu cũ.
    for col, typ in [
        ("confidence_label", "TEXT"),
        ("status", "TEXT DEFAULT 'OPEN'"),
        ("actual_number", "INTEGER"),
        ("payout", "REAL"),
        ("profit", "REAL"),
        ("created_at", "TEXT"),
        ("resolved_at", "TEXT"),
    ]:
        ensure_column(c, "paper_bets", col, typ)

    # Migration defensive cho adaptive_predictions nếu từng có schema cũ.
    for col, typ in [
        ("ensemble_probs_json", "TEXT"),
        ("model_probs_json", "TEXT"),
        ("weights_json", "TEXT"),
        ("p_large", "REAL"),
        ("p_small", "REAL"),
        ("p_red", "REAL"),
        ("p_green", "REAL"),
        ("p_violet", "REAL"),
        ("side_pick", "TEXT"),
        ("side_probability", "REAL"),
        ("confidence_label", "TEXT"),
        ("actual_number", "INTEGER"),
        ("resolved_at", "TEXT"),
    ]:
        ensure_column(c, "adaptive_predictions", col, typ)

    # v6 schema: data quality, regime, calibration/meta state.
    for col, typ in [
        ("regime_label", "TEXT"),
        ("regime_features_json", "TEXT"),
        ("data_health", "TEXT"),
        ("drift_score", "REAL"),
        ("calibration_ece", "REAL"),
    ]:
        ensure_column(c, "adaptive_predictions", col, typ)

    c.execute("""CREATE TABLE IF NOT EXISTS data_quality_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        current_issue TEXT,
        api_rows INTEGER,
        new_rows INTEGER,
        gap_count INTEGER,
        duplicate_count INTEGER,
        stale_flag INTEGER,
        drift_seconds REAL,
        health TEXT,
        detail_json TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS system_state(
        id INTEGER PRIMARY KEY CHECK(id=1),
        updated_at TEXT,
        current_issue TEXT,
        regime_label TEXT,
        regime_features_json TEXT,
        data_health TEXT,
        drift_score REAL,
        calibration_ece REAL,
        ensemble_accuracy_50 REAL,
        ensemble_accuracy_100 REAL,
        weights_json TEXT
    )""")
    c.execute("INSERT OR IGNORE INTO system_state(id) VALUES(1)")

    # v7: mô hình chọn 7 số + paper progression x4.
    c.execute("""CREATE TABLE IF NOT EXISTS top7_predictions(
        issue TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        selected_numbers_json TEXT NOT NULL,
        hit_probability REAL NOT NULL,
        break_even_probability REAL NOT NULL,
        estimated_roi REAL NOT NULL,
        actual_number INTEGER,
        hit INTEGER,
        resolved_at TEXT
    )""")

    # v7.1: TOP7 Bet Gate metrics. Migration an toàn cho DB v7 cũ.
    for col, typ in [
        ("calibrated_probability", "REAL"),
        ("bottom3_json", "TEXT"),
        ("consensus_score", "REAL"),
        ("consensus_strong_count", "INTEGER"),
        ("stability_score", "REAL"),
        ("regime_label", "TEXT"),
        ("regime_samples", "INTEGER"),
        ("regime_hit_rate", "REAL"),
        ("live_samples", "INTEGER"),
        ("live_hit_rate", "REAL"),
        ("calibration_ece_top7", "REAL"),
        ("data_health", "TEXT"),
        ("drift_score", "REAL"),
        ("gate_score", "REAL"),
        ("gate_pass", "INTEGER"),
        ("gate_reasons_json", "TEXT")
    ]:
        ensure_column(c, "top7_predictions", col, typ)

    c.execute("""CREATE TABLE IF NOT EXISTS top7_bets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        issue TEXT UNIQUE NOT NULL,
        level INTEGER NOT NULL,
        selected_numbers_json TEXT NOT NULL,
        stake_per_number REAL NOT NULL,
        total_stake REAL NOT NULL,
        odds REAL NOT NULL,
        hit_probability REAL NOT NULL,
        estimated_roi REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'OPEN',
        actual_number INTEGER,
        payout REAL,
        profit REAL,
        bankroll_before REAL,
        bankroll_after REAL,
        session_profit_after REAL,
        created_at TEXT NOT NULL,
        resolved_at TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS top7_state(
        id INTEGER PRIMARY KEY CHECK(id=1),
        initial_bankroll REAL NOT NULL,
        bankroll REAL NOT NULL,
        base_stake_per_number REAL NOT NULL,
        multiplier REAL NOT NULL,
        max_level INTEGER NOT NULL,
        stop_profit REAL NOT NULL,
        stop_loss REAL NOT NULL,
        session_profit REAL NOT NULL,
        current_level INTEGER NOT NULL,
        active INTEGER NOT NULL,
        stop_reason TEXT,
        updated_at TEXT NOT NULL
    )""")

    c.execute("""INSERT OR IGNORE INTO top7_state(
        id,initial_bankroll,bankroll,base_stake_per_number,multiplier,max_level,
        stop_profit,stop_loss,session_profit,current_level,active,stop_reason,updated_at
    ) VALUES(1,525000,525000,3000,4,3,52500,441000,0,1,1,NULL,?)""",(now(),))

    # v7.4: ba chiến thuật paper độc lập chạy trên cùng một TOP7 signal.
    # Đây là LIVE PAPER TEST: theo kết quả thật, nhưng KHÔNG gửi lệnh cược lên website.
    c.execute("""CREATE TABLE IF NOT EXISTS strategy_bets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        issue TEXT NOT NULL,
        strategy TEXT NOT NULL,
        step_index INTEGER NOT NULL,
        unit_multiplier REAL NOT NULL,
        selected_numbers_json TEXT NOT NULL,
        stake_per_number REAL NOT NULL,
        total_stake REAL NOT NULL,
        odds REAL NOT NULL,
        hit_probability REAL NOT NULL,
        estimated_roi REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'OPEN',
        actual_number INTEGER,
        payout REAL,
        profit REAL,
        bankroll_before REAL,
        bankroll_after REAL,
        session_profit_after REAL,
        created_at TEXT NOT NULL,
        resolved_at TEXT,
        UNIQUE(issue,strategy)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS strategy_state(
        strategy TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        pattern_json TEXT NOT NULL,
        initial_bankroll REAL NOT NULL,
        bankroll REAL NOT NULL,
        base_stake_per_number REAL NOT NULL,
        current_step INTEGER NOT NULL DEFAULT 0,
        session_profit REAL NOT NULL DEFAULT 0,
        total_stake REAL NOT NULL DEFAULT 0,
        max_total_stake REAL NOT NULL DEFAULT 0,
        peak_bankroll REAL NOT NULL,
        max_drawdown REAL NOT NULL DEFAULT 0,
        settled INTEGER NOT NULL DEFAULT 0,
        wins INTEGER NOT NULL DEFAULT 0,
        losses INTEGER NOT NULL DEFAULT 0,
        current_win_streak INTEGER NOT NULL DEFAULT 0,
        longest_win_streak INTEGER NOT NULL DEFAULT 0,
        current_loss_streak INTEGER NOT NULL DEFAULT 0,
        longest_loss_streak INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )""")

    strategy_seed = [
        ('flat','FLAT 1',[1]),
        ('1123','1-1-2-3',[1,1,2,3]),
        ('1326','1-3-2-6',[1,3,2,6]),
    ]
    for key,label,pattern in strategy_seed:
        c.execute("""INSERT OR IGNORE INTO strategy_state(
            strategy,label,pattern_json,initial_bankroll,bankroll,base_stake_per_number,
            current_step,session_profit,total_stake,max_total_stake,peak_bankroll,max_drawdown,
            settled,wins,losses,current_win_streak,longest_win_streak,current_loss_streak,longest_loss_streak,updated_at
        ) VALUES(?,?,?,?,?,?,0,0,0,0,?,0,0,0,0,0,0,0,0,?)""",
        (key,label,json.dumps(pattern),525000,525000,3000,525000,now()))

    # v7.6: runtime control + real-bet audit. Default is STOPPED/DISARMED.
    c.execute("""CREATE TABLE IF NOT EXISTS runtime_control(
        id INTEGER PRIMARY KEY CHECK(id=1),
        tool_running INTEGER NOT NULL DEFAULT 0,
        real_bet_armed INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'STOPPED',
        started_at TEXT,
        ended_at TEXT,
        session_start_balance REAL,
        current_balance REAL,
        session_profit REAL NOT NULL DEFAULT 0,
        session_bets INTEGER NOT NULL DEFAULT 0,
        current_step INTEGER NOT NULL DEFAULT 0,
        current_win_streak INTEGER NOT NULL DEFAULT 0,
        current_loss_streak INTEGER NOT NULL DEFAULT 0,
        longest_loss_streak INTEGER NOT NULL DEFAULT 0,
        day_key TEXT,
        day_start_balance REAL,
        stop_reason TEXT,
        last_error TEXT,
        updated_at TEXT NOT NULL
    )""")
    c.execute("""INSERT OR IGNORE INTO runtime_control(
        id,tool_running,real_bet_armed,status,session_profit,session_bets,current_step,
        current_win_streak,current_loss_streak,longest_loss_streak,updated_at
    ) VALUES(1,0,0,'STOPPED',0,0,0,0,0,0,?)""",(now(),))

    # v7.6: các STOP/timing setting được chốt tại thời điểm người dùng bấm START.
    # NULL nghĩa là writer dùng default CLI. DB cũ được migrate không mất dữ liệu.
    for col, typ in [
        ("stop_consecutive_losses", "INTEGER"),
        ("stop_profit_pct", "REAL"),
        ("stop_balance_floor_pct", "REAL"),
        ("stop_max_minutes", "REAL"),
        ("bet_second", "INTEGER"),
    ]:
        ensure_column(c, "runtime_control", col, typ)

    c.execute("""CREATE TABLE IF NOT EXISTS real_bets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        issue TEXT UNIQUE NOT NULL,
        step_index INTEGER NOT NULL,
        unit_multiplier REAL NOT NULL,
        selected_numbers_json TEXT NOT NULL,
        stake_per_number REAL NOT NULL,
        total_stake REAL NOT NULL,
        odds REAL NOT NULL,
        hit_probability REAL,
        status TEXT NOT NULL,
        actual_number INTEGER,
        payout REAL,
        profit REAL,
        balance_before REAL,
        balance_after_submit REAL,
        ticket_text TEXT,
        error_text TEXT,
        created_at TEXT NOT NULL,
        resolved_at TEXT
    )""")

    c.commit()
    return c

def session():
    s=requests.Session(); s.headers.update({"User-Agent":"Mozilla/5.0","Accept":"application/json, text/plain, */*","Referer":"https://www.88idd.com/","Cache-Control":"no-cache"})
    return s

def fetch(s):
    r=s.get(API_URL,timeout=20); r.raise_for_status(); return r.json()

def history(payload):
    d=payload.get("data") or {}; stats=(d.get("statisticsInfo") or {}).get("statisticDataList") or {}
    out=[]
    for x in stats.get("bigSmall") or []:
        issue=str(x.get("issue","")).strip(); v=str(x.get("value","")).strip()
        if issue and v.isdigit() and 0<=int(v)<=9:
            n=int(v); out.append((issue,n,size_of(n),color_of(n)))
    li=str(d.get("last_issue","")).strip(); on=str(d.get("open_numbers","")).strip()
    if li and on.isdigit() and 0<=int(on)<=9 and not any(r[0]==li for r in out):
        n=int(on); out.append((li,n,size_of(n),color_of(n)))
    return sorted(out,key=lambda r:r[0])

def settle(conn, issue, n, args=None):
    conn.execute("UPDATE adaptive_predictions SET actual_number=?,resolved_at=? WHERE issue=? AND actual_number IS NULL",(n,now(),issue))
    settle_top7(conn,issue,n)
    settle_strategy_bets(conn,issue,n)
    if args is not None:
        settle_real_bet(conn,issue,n,args)
    for bid,market,sel,odds,stake in conn.execute("SELECT id,market,selection,odds,stake FROM paper_bets WHERE issue=? AND status='OPEN'",(issue,)).fetchall():
        won=(size_of(n)==sel) if market=="size" else ((sel=="Tím" and n in(0,5)) or (sel=="Đỏ" and n in(0,2,4,6,8)) or (sel=="Xanh" and n in(1,3,5,7,9))) if market=="color" else str(n)==str(sel)
        payout=stake*odds if won else 0; profit=payout-stake
        conn.execute("UPDATE paper_bets SET status=?,actual_number=?,payout=?,profit=?,resolved_at=? WHERE id=?",("WIN" if won else "LOSS",n,payout,profit,now(),bid))
    conn.commit()

def upsert(conn, rows, args=None):
    new=0
    for issue,n,sz,col in rows:
        cur=conn.execute("INSERT OR IGNORE INTO results VALUES(?,?,?,?,?,?)",(issue,n,sz,col,now(),API_URL))
        new += int(cur.rowcount==1); settle(conn,issue,n,args)
    conn.commit(); return new

def timing(payload):
    d=payload.get("data") or {}; nxt=d.get("next_issue_info") or {}
    sec=int(d.get("second",0) or 0); close=int(d.get("close_second",3) or 3); mx=int(nxt.get("second",27) or 27)
    return {"second":sec,"close":close,"max":mx,"issue":str(d.get("issue","")),"last":str(d.get("last_issue","")),"name":str(d.get("name",""))}

def wait_to_target(s, info, target, poll=.12):
    sec=info["second"]
    wait=(sec-target) if sec>target else (0 if sec==target else sec+info["close"]+(info["max"]-target))
    if wait>0: time.sleep(max(0,wait-.8))
    start=time.monotonic(); last=None
    while time.monotonic()-start<4:
        p=fetch(s); last=p; sec=timing(p)["second"]
        if sec<=target: return p, target-sec
        time.sleep(poll)
    return last,None

def rolling(x):
    pieces=[]; ws=[]
    for w,wt in ((50,.25),(100,.35),(300,.40)):
        if len(x)>=20:
            xx=x[-min(w,len(x)):]; counts=np.bincount(xx,minlength=10).astype(float); pieces.append((counts+1)/(counts.sum()+10)); ws.append(wt)
    if not pieces:return np.repeat(.1,10)
    ws=np.array(ws)/sum(ws); return norm(sum(p*w for p,w in zip(pieces,ws)))

def markov(x,order,alpha=1.5):
    if len(x)<=order:return np.repeat(.1,10),0
    ctx=tuple(x[-order:]); counts=np.zeros(10); m=0
    for i in range(order,len(x)):
        if tuple(x[i-order:i])==ctx: counts[x[i]]+=1; m+=1
    return norm((counts+alpha)/(counts.sum()+10*alpha)),m

def feature(x,i):
    if i<50:return None
    h=x[:i]; f=[]
    for lag in range(1,11):
        n=int(h[-lag]); f += [1.0 if n==k else 0.0 for k in range(10)]
    for w in (10,20,50):
        xx=h[-w:]; f += (np.bincount(xx,minlength=10)/len(xx)).tolist()
        f += [float(np.mean(xx>=5)),float(np.mean(np.isin(xx,[0,2,4,6,8]))),float(np.mean(np.isin(xx,[0,5])))]
    for o in (1,2,3):
        p,m=markov(h,o); f+=p.tolist()+[min(m,500)/500]
    return np.asarray(f,float)

def ml_outputs(x):
    if len(x)<320:return {}
    X=[]; y=[]
    for i in range(50,len(x)):
        X.append(feature(x,i)); y.append(x[i])
    X=np.vstack(X); y=np.asarray(y); fv=feature(np.append(x,0),len(x))
    models={
        "logistic":LogisticRegression(max_iter=1000,C=.7),
        "random_forest":RandomForestClassifier(n_estimators=180,max_depth=7,min_samples_leaf=4,max_features="sqrt",random_state=42,n_jobs=-1),
        "hist_gradient_boosting":HistGradientBoostingClassifier(max_iter=120,learning_rate=.05,max_leaf_nodes=15,l2_regularization=1.0,random_state=42)
    }
    out={}
    for name,m in models.items():
        m.fit(X,y); raw=m.predict_proba(fv.reshape(1,-1))[0]; p=np.zeros(10)
        for cls,pr in zip(m.classes_,raw): p[int(cls)]=pr
        out[name]=norm(p)
    return out

def model_outputs(x):
    out={"rolling":rolling(x)}
    for o in (1,2,3): out[f"markov{o}"]=markov(x,o)[0]
    out.update(ml_outputs(x)); return out

def live_metrics(conn, lookback=300):
    rows=conn.execute("SELECT model_probs_json,actual_number FROM adaptive_predictions WHERE actual_number IS NOT NULL ORDER BY issue DESC LIMIT ?",(lookback,)).fetchall()
    a={n:{"n":0,"ll":0.0,"br":0.0,"ok":0,"top3":0} for n in MODEL_NAMES}
    for js,actual in rows:
        try: mp=json.loads(js)
        except: continue
        actual=int(actual); al=int(actual>=5)
        for n,p in mp.items():
            if n not in a: continue
            p=norm(p); pl=float(p[5:].sum()); z=a[n]; z["n"]+=1; z["ll"]+=-math.log(max(p[actual],1e-9)); z["br"]+=(pl-al)**2; z["ok"]+=int((pl>=.5)==bool(al)); z["top3"]+=int(actual in np.argsort(p)[-3:])
    out={}
    for n,z in a.items():
        k=z["n"]; out[n]={"n":k,"number_logloss":z["ll"]/k if k else None,"size_brier":z["br"]/k if k else None,"size_accuracy":z["ok"]/k if k else None,"top3_accuracy":z["top3"]/k if k else None}
    return out

def weights(active, metrics, min_live=30):
    active=list(active); prior=np.array([PRIORS.get(n,.05) for n in active],float); prior/=prior.sum()
    elig=[n for n in active if metrics.get(n,{}).get("n",0)>=min_live]
    if not elig:return dict(zip(active,prior))
    bestll=min(metrics[n]["number_logloss"] for n in elig); bestbr=min(metrics[n]["size_brier"] for n in elig)
    ls=[]
    for n in active:
        m=metrics.get(n,{}); k=m.get("n",0)
        if k<min_live or m.get("number_logloss") is None: ls.append(.15); continue
        score=math.exp(-1.4*(m["number_logloss"]-bestll)-2.5*(m["size_brier"]-bestbr))
        score*=.35+.65*min(1,math.sqrt(k/300)); ls.append(score)
    ls=np.array(ls,float); ls/=ls.sum(); maxn=max(metrics.get(n,{}).get("n",0) for n in active)
    blend=min(.85,max(0,(maxn-min_live)/200)*.85); fin=(1-blend)*prior+blend*ls; fin/=fin.sum()
    return dict(zip(active,fin))

def summary(p):
    return {"p_large":float(p[5:].sum()),"p_small":float(p[:5].sum()),"p_red":float(p[[0,2,4,6,8]].sum()),"p_green":float(p[[1,3,5,7,9]].sum()),"p_violet":float(p[[0,5]].sum())}

def ensemble_accuracy(conn,n=100):
    rows=conn.execute("SELECT side_pick,actual_number FROM adaptive_predictions WHERE actual_number IS NOT NULL ORDER BY issue DESC LIMIT ?",(n,)).fetchall()
    if not rows:return None,0
    ok=sum(1 for pick,a in rows if pick==size_of(int(a))); return ok/len(rows),len(rows)

def confidence(conn,p):
    a50,n50=ensemble_accuracy(conn,50); a100,n100=ensemble_accuracy(conn,100)
    if n100<30:return "EXPERIMENTAL"
    if p>=.58 and (a50 or 0)>=.54 and (a100 or 0)>=.525:return "HIGH"
    if p>=.55 and (a50 or 0)>=.51:return "MEDIUM"
    return "LOW"


def normalized_entropy(x):
    x=np.asarray(x,dtype=int)
    if len(x)==0:return 1.0
    counts=np.bincount(x,minlength=10).astype(float); p=counts[counts>0]/counts.sum()
    h=float(-(p*np.log2(p)).sum()); return h/math.log2(10)

def js_divergence(p,q):
    p=norm(p); q=norm(q); m=.5*(p+q)
    def kl(a,b): return float(np.sum(a*np.log(np.clip(a/b,1e-12,None))))
    return .5*kl(p,m)+.5*kl(q,m)

def detect_regime(x):
    x=np.asarray(x,dtype=int)
    w=x[-min(100,len(x)):]
    if len(w)<10:
        return "INSUFFICIENT", {"n":len(w),"p_large":None,"switch_rate":None,"entropy":None}
    p_large=float(np.mean(w>=5))
    sides=(w>=5).astype(int)
    switch_rate=float(np.mean(sides[1:]!=sides[:-1])) if len(sides)>1 else .5
    ent=normalized_entropy(w)
    if p_large>=.60: label="LARGE_BIAS"
    elif p_large<=.40: label="SMALL_BIAS"
    elif switch_rate<=.38: label="STREAKY"
    elif switch_rate>=.62: label="CHOPPY"
    elif ent<.92: label="LOW_ENTROPY"
    else: label="BALANCED"
    return label,{"n":int(len(w)),"p_large":p_large,"switch_rate":switch_rate,"entropy":ent}

def drift_score(x):
    x=np.asarray(x,dtype=int)
    if len(x)<100:return 0.0,{"large_z":0.0,"js":0.0}
    recent=x[-50:]
    baseline=x[-250:-50] if len(x)>=250 else x[:-50]
    if len(baseline)<30:return 0.0,{"large_z":0.0,"js":0.0}
    pr=float(np.mean(recent>=5)); pb=float(np.mean(baseline>=5)); pooled=(pr*len(recent)+pb*len(baseline))/(len(recent)+len(baseline))
    se=math.sqrt(max(pooled*(1-pooled)*(1/len(recent)+1/len(baseline)),1e-12)); z=abs(pr-pb)/se
    cr=np.bincount(recent,minlength=10)+1; cb=np.bincount(baseline,minlength=10)+1
    js=js_divergence(cr,cb)
    score=float(min(1.0,.35*(z/3.0)+.65*(js/.12)))
    return score,{"large_z":float(z),"js":float(js),"recent_large":pr,"baseline_large":pb}

def issue_parts(issue):
    s=str(issue)
    if len(s)>=9 and s[:8].isdigit() and s[8:].isdigit(): return s[:8],int(s[8:])
    return None,None

def data_health(conn,current_issue,api_rows,new_rows,drift_seconds):
    rows=conn.execute("SELECT issue FROM results ORDER BY issue DESC LIMIT 60").fetchall()
    issues=[str(r[0]) for r in reversed(rows)]
    gaps=0
    for a,b in zip(issues[:-1],issues[1:]):
        da,sa=issue_parts(a); db,sb=issue_parts(b)
        if da==db and sa is not None and sb is not None and sb-sa!=1:gaps+=1
    duplicates=max(0,int(api_rows)-len(set(issues[-min(api_rows,len(issues)):]))) if api_rows else 0
    stale=0
    if issues and current_issue:
        dc,sc=issue_parts(current_issue); dl,sl=issue_parts(issues[-1])
        if dc==dl and sc is not None and sl is not None and sc<=sl: stale=1
    if gaps>0 or stale: health="WARN"
    elif drift_seconds is not None and abs(float(drift_seconds))>1: health="WARN"
    else: health="OK"
    detail={"stored_recent":len(issues),"gaps":gaps,"stale":stale,"drift_seconds":drift_seconds}
    return health,gaps,duplicates,stale,detail

def calibration_stats(conn,lookback=500):
    rows=conn.execute("SELECT side_probability,side_pick,actual_number FROM adaptive_predictions WHERE actual_number IS NOT NULL ORDER BY issue DESC LIMIT ?",(lookback,)).fetchall()
    bins=[(.50,.52),(.52,.54),(.54,.56),(.56,.58),(.58,.60),(.60,1.01)]
    out=[]; total=max(len(rows),1); ece=0.0
    for lo,hi in bins:
        grp=[]
        for prob,pick,a in rows:
            prob=float(prob)
            if lo<=prob<hi:
                correct=int(str(pick)==size_of(int(a))); grp.append((prob,correct))
        if grp:
            avgp=float(np.mean([g[0] for g in grp])); acc=float(np.mean([g[1] for g in grp])); gap=acc-avgp; n=len(grp); ece+=(n/total)*abs(gap)
        else: avgp=acc=gap=None; n=0
        out.append({"bucket":f"{lo:.2f}-{min(hi,1.0):.2f}","n":n,"avg_pred":avgp,"actual_accuracy":acc,"gap":gap})
    return out,float(ece)

def regime_model_metrics(conn,regime,lookback=300):
    rows=conn.execute("SELECT model_probs_json,actual_number FROM adaptive_predictions WHERE actual_number IS NOT NULL AND regime_label=? ORDER BY issue DESC LIMIT ?",(regime,lookback)).fetchall()
    a={n:{"n":0,"ll":0.0,"br":0.0,"ok":0} for n in MODEL_NAMES}
    for js,actual in rows:
        try: mp=json.loads(js)
        except: continue
        actual=int(actual); al=int(actual>=5)
        for n,p in mp.items():
            if n not in a: continue
            p=norm(p); pl=float(p[5:].sum()); z=a[n]; z["n"]+=1; z["ll"]+=-math.log(max(p[actual],1e-9)); z["br"]+=(pl-al)**2; z["ok"]+=int((pl>=.5)==bool(al))
    out={}
    for n,z in a.items():
        k=z["n"]; out[n]={"n":k,"number_logloss":z["ll"]/k if k else None,"size_brier":z["br"]/k if k else None,"size_accuracy":z["ok"]/k if k else None}
    return out

def meta_weights(active,global_metrics,regime_metrics,min_live=30,min_regime=15):
    base=weights(active,global_metrics,min_live)
    active=list(active)
    eligible=[n for n in active if regime_metrics.get(n,{}).get("n",0)>=min_regime]
    if not eligible:return base
    bestll=min(regime_metrics[n]["number_logloss"] for n in eligible); bestbr=min(regime_metrics[n]["size_brier"] for n in eligible)
    scores=[]
    for n in active:
        m=regime_metrics.get(n,{}); k=m.get("n",0)
        if k<min_regime or m.get("number_logloss") is None:scores.append(.25); continue
        score=math.exp(-1.2*(m["number_logloss"]-bestll)-2.0*(m["size_brier"]-bestbr)); score*=.4+.6*min(1.0,math.sqrt(k/100)); scores.append(score)
    scores=np.asarray(scores,float); scores/=scores.sum()
    reg=dict(zip(active,scores)); blend=min(.55,max(regime_metrics.get(n,{}).get("n",0) for n in active)/100*.55)
    final={n:(1-blend)*base[n]+blend*reg[n] for n in active}; s=sum(final.values()); return {n:v/s for n,v in final.items()}

def update_system_state(conn,issue,regime,features,health,drift,ece,w):
    a50,n50=ensemble_accuracy(conn,50); a100,n100=ensemble_accuracy(conn,100)
    conn.execute("""UPDATE system_state SET updated_at=?,current_issue=?,regime_label=?,regime_features_json=?,data_health=?,drift_score=?,calibration_ece=?,ensemble_accuracy_50=?,ensemble_accuracy_100=?,weights_json=? WHERE id=1""",
        (now(),issue,regime,json.dumps(features),health,drift,ece,a50,a100,json.dumps(w)))
    conn.commit()

def save_prediction(conn,issue,x,outs,w,regime,regime_features,health,drift,ece):
    p=norm(sum(w[n]*outs[n] for n in outs)); s=summary(p); pick="Lớn" if s["p_large"]>=s["p_small"] else "Nhỏ"; prob=max(s["p_large"],s["p_small"]); conf=confidence(conn,prob)
    cur=conn.execute("""INSERT OR IGNORE INTO adaptive_predictions(
        issue,created_at,sample_size,ensemble_probs_json,model_probs_json,weights_json,
        p_large,p_small,p_red,p_green,p_violet,side_pick,side_probability,confidence_label,
        actual_number,resolved_at,regime_label,regime_features_json,data_health,drift_score,calibration_ece
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (issue,now(),len(x),json.dumps(p.tolist()),json.dumps({n:norm(v).tolist() for n,v in outs.items()}),json.dumps(w),s["p_large"],s["p_small"],s["p_red"],s["p_green"],s["p_violet"],pick,prob,conf,None,None,regime,json.dumps(regime_features),health,drift,ece))
    conn.commit(); return p,s,pick,prob,conf,bool(cur.rowcount)

def configure_top7(conn,args):
    # v7.1: không dừng theo stop-profit/stop-loss/max-level.
    # Giữ các cột cũ trong DB để tương thích, nhưng không dùng làm gate.
    conn.execute("""UPDATE top7_state SET
        base_stake_per_number=?, multiplier=?, active=1, stop_reason=NULL, updated_at=?
        WHERE id=1""",
        (args.top7_base_stake,args.top7_multiplier,now()))
    if args.top7_reset:
        conn.execute("""UPDATE top7_state SET
            initial_bankroll=?, bankroll=?, session_profit=0, current_level=1,
            active=1, stop_reason=NULL, updated_at=? WHERE id=1""",
            (args.top7_bankroll,args.top7_bankroll,now()))
    conn.commit()


def top7_state(conn):
    row=conn.execute("""SELECT initial_bankroll,bankroll,base_stake_per_number,multiplier,max_level,
        stop_profit,stop_loss,session_profit,current_level,active,stop_reason
        FROM top7_state WHERE id=1""").fetchone()
    if not row:return None
    keys=["initial_bankroll","bankroll","base_stake_per_number","multiplier","max_level","stop_profit","stop_loss","session_profit","current_level","active","stop_reason"]
    return dict(zip(keys,row))


def configure_strategies(conn,args):
    """Cấu hình live paper test 3 chiến thuật. Không giới hạn bankroll."""
    defs={
        "flat":("FLAT 1",[1]),
        "1123":("1-1-2-3",[1,1,2,3]),
        "1326":("1-3-2-6",[1,3,2,6]),
    }
    for key,(label,pattern) in defs.items():
        row=conn.execute("SELECT strategy FROM strategy_state WHERE strategy=?",(key,)).fetchone()
        if not row:
            conn.execute("""INSERT INTO strategy_state(
                strategy,label,pattern_json,initial_bankroll,bankroll,base_stake_per_number,
                current_step,session_profit,total_stake,max_total_stake,peak_bankroll,max_drawdown,
                settled,wins,losses,current_win_streak,longest_win_streak,current_loss_streak,longest_loss_streak,updated_at
            ) VALUES(?,?,?,?,?,?,0,0,0,0,?,0,0,0,0,0,0,0,0,?)""",
            (key,label,json.dumps(pattern),args.strategy_bankroll,args.strategy_bankroll,args.strategy_base_stake,args.strategy_bankroll,now()))
        else:
            conn.execute("""UPDATE strategy_state SET label=?,pattern_json=?,base_stake_per_number=?,updated_at=?
                WHERE strategy=?""",(label,json.dumps(pattern),args.strategy_base_stake,now(),key))
    if args.strategy_reset:
        conn.execute("DELETE FROM strategy_bets")
        for key,(label,pattern) in defs.items():
            conn.execute("""UPDATE strategy_state SET
                label=?,pattern_json=?,initial_bankroll=?,bankroll=?,base_stake_per_number=?,
                current_step=0,session_profit=0,total_stake=0,max_total_stake=0,
                peak_bankroll=?,max_drawdown=0,settled=0,wins=0,losses=0,
                current_win_streak=0,longest_win_streak=0,current_loss_streak=0,longest_loss_streak=0,
                updated_at=? WHERE strategy=?""",
                (label,json.dumps(pattern),args.strategy_bankroll,args.strategy_bankroll,args.strategy_base_stake,
                 args.strategy_bankroll,now(),key))
    conn.commit()


def strategy_states(conn):
    rows=conn.execute("""SELECT strategy,label,pattern_json,initial_bankroll,bankroll,base_stake_per_number,
        current_step,session_profit,total_stake,max_total_stake,peak_bankroll,max_drawdown,
        settled,wins,losses,current_win_streak,longest_win_streak,current_loss_streak,longest_loss_streak
        FROM strategy_state ORDER BY CASE strategy WHEN 'flat' THEN 1 WHEN '1123' THEN 2 WHEN '1326' THEN 3 ELSE 9 END""").fetchall()
    keys=["strategy","label","pattern_json","initial_bankroll","bankroll","base_stake_per_number",
          "current_step","session_profit","total_stake","max_total_stake","peak_bankroll","max_drawdown",
          "settled","wins","losses","current_win_streak","longest_win_streak","current_loss_streak","longest_loss_streak"]
    return [dict(zip(keys,r)) for r in rows]


def place_strategy_bets(conn,issue,gate,args):
    """Đặt 3 paper bet song song khi TOP7 Gate PASS."""
    if not args.strategy_test_enabled:
        return []
    if not gate["passed"]:
        return []
    placed=[]
    for st in strategy_states(conn):
        key=st["strategy"]
        if conn.execute("SELECT 1 FROM strategy_bets WHERE issue=? AND strategy=?",(issue,key)).fetchone():
            continue
        try: pattern=[float(x) for x in json.loads(st["pattern_json"])]
        except Exception: pattern=[1.0]
        if not pattern: pattern=[1.0]
        step=max(0,min(int(st["current_step"]),len(pattern)-1))
        unit=float(pattern[step])
        stake_per=float(st["base_stake_per_number"])*unit
        total=stake_per*TOP7_COUNT
        conn.execute("""INSERT INTO strategy_bets(
            issue,strategy,step_index,unit_multiplier,selected_numbers_json,stake_per_number,total_stake,
            odds,hit_probability,estimated_roi,status,bankroll_before,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?, 'OPEN',?,?)""",
        (issue,key,step,unit,json.dumps(gate["selected"]),stake_per,total,TOP7_ODDS,
         gate["calibrated_prob"],gate["estimated_roi"],float(st["bankroll"]),now()))
        placed.append(key)
    conn.commit()
    return placed


def settle_strategy_bets(conn,issue,n):
    bets=conn.execute("""SELECT id,strategy,step_index,selected_numbers_json,stake_per_number,total_stake,odds
        FROM strategy_bets WHERE issue=? AND status='OPEN' ORDER BY id""",(issue,)).fetchall()
    if not bets:
        return
    for bid,key,step,js,stake_per,total,odds in bets:
        st=conn.execute("""SELECT label,pattern_json,bankroll,session_profit,total_stake,max_total_stake,
            peak_bankroll,max_drawdown,settled,wins,losses,current_win_streak,longest_win_streak,
            current_loss_streak,longest_loss_streak FROM strategy_state WHERE strategy=?""",(key,)).fetchone()
        if not st:
            continue
        (label,pattern_json,bankroll,session_profit,cum_stake,max_stake,peak_bankroll,max_dd,
         settled,wins,losses,cws,lws,cls,lls)=st
        try: selected=[int(x) for x in json.loads(js)]
        except Exception: selected=[]
        try: pattern=[float(x) for x in json.loads(pattern_json)]
        except Exception: pattern=[1.0]
        if not pattern: pattern=[1.0]
        won=int(n) in selected
        payout=float(stake_per)*float(odds) if won else 0.0
        profit=payout-float(total) if won else -float(total)
        bankroll_after=float(bankroll)+profit
        session_profit_after=float(session_profit)+profit
        cum_stake_after=float(cum_stake)+float(total)
        max_stake_after=max(float(max_stake),float(total))
        peak_after=max(float(peak_bankroll),bankroll_after)
        max_dd_after=max(float(max_dd),peak_after-bankroll_after)

        if won:
            next_step=(int(step)+1) if int(step)+1 < len(pattern) else 0
            wins=int(wins)+1; cws=int(cws)+1; lws=max(int(lws),cws); cls=0
        else:
            next_step=0
            losses=int(losses)+1; cls=int(cls)+1; lls=max(int(lls),cls); cws=0
        settled=int(settled)+1

        conn.execute("""UPDATE strategy_bets SET status=?,actual_number=?,payout=?,profit=?,bankroll_after=?,
            session_profit_after=?,resolved_at=? WHERE id=?""",
            ("WIN" if won else "LOSS",int(n),payout,profit,bankroll_after,session_profit_after,now(),bid))
        conn.execute("""UPDATE strategy_state SET bankroll=?,current_step=?,session_profit=?,total_stake=?,
            max_total_stake=?,peak_bankroll=?,max_drawdown=?,settled=?,wins=?,losses=?,
            current_win_streak=?,longest_win_streak=?,current_loss_streak=?,longest_loss_streak=?,updated_at=?
            WHERE strategy=?""",
            (bankroll_after,next_step,session_profit_after,cum_stake_after,max_stake_after,peak_after,max_dd_after,
             settled,wins,losses,cws,lws,cls,lls,now(),key))
    conn.commit()


def export_strategies(conn,args):
    atomic_csv(args.strategy_bets_csv,
        ["issue","strategy","step_index","unit_multiplier","selected_numbers_json","stake_per_number","total_stake",
         "odds","hit_probability","estimated_roi","status","actual_number","payout","profit","bankroll_before",
         "bankroll_after","session_profit_after","created_at","resolved_at"],
        conn.execute("""SELECT issue,strategy,step_index,unit_multiplier,selected_numbers_json,stake_per_number,total_stake,
            odds,hit_probability,estimated_roi,status,actual_number,payout,profit,bankroll_before,bankroll_after,
            session_profit_after,created_at,resolved_at FROM strategy_bets ORDER BY id""").fetchall())
    rows=[]
    for st in strategy_states(conn):
        try: pattern=json.loads(st["pattern_json"])
        except Exception: pattern=[1]
        step=max(0,min(int(st["current_step"]),len(pattern)-1)) if pattern else 0
        next_unit=float(pattern[step]) if pattern else 1.0
        out=dict(st)
        out["next_unit_multiplier"]=next_unit
        out["next_stake_per_number"]=float(st["base_stake_per_number"])*next_unit
        out["next_total_stake"]=out["next_stake_per_number"]*TOP7_COUNT
        out["roi"]=(float(st["session_profit"])/float(st["total_stake"])) if float(st["total_stake"] or 0)>0 else 0.0
        rows.append(out)
    if rows:
        hdr=list(rows[0].keys())
        atomic_csv(args.strategy_state_csv,hdr,[[r.get(k) for k in hdr] for r in rows])



def runtime_control(conn):
    row=conn.execute("""SELECT tool_running,real_bet_armed,status,started_at,ended_at,
        session_start_balance,current_balance,session_profit,session_bets,current_step,
        current_win_streak,current_loss_streak,longest_loss_streak,day_key,day_start_balance,
        stop_reason,last_error,stop_consecutive_losses,stop_profit_pct,stop_balance_floor_pct,
        stop_max_minutes,bet_second,updated_at FROM runtime_control WHERE id=1""").fetchone()
    if not row:return None
    keys=["tool_running","real_bet_armed","status","started_at","ended_at","session_start_balance",
          "current_balance","session_profit","session_bets","current_step","current_win_streak",
          "current_loss_streak","longest_loss_streak","day_key","day_start_balance","stop_reason",
          "last_error","stop_consecutive_losses","stop_profit_pct","stop_balance_floor_pct",
          "stop_max_minutes","bet_second","updated_at"]
    return dict(zip(keys,row))

def _runtime_num(ctl,key,default,cast=float):
    try:
        v=ctl.get(key) if ctl else None
        if v is None:return cast(default)
        return cast(v)
    except Exception:
        return cast(default)


def stop_real_tool(conn,reason,error=None,status="AUTO_STOPPED"):
    conn.execute("""UPDATE runtime_control SET tool_running=0,real_bet_armed=0,status=?,ended_at=?,
        stop_reason=?,last_error=?,updated_at=? WHERE id=1""",(status,now(),str(reason),str(error) if error else None,now()))
    conn.commit()
    print(f"[REAL BET] STOP: {reason}" + (f" | {error}" if error else ""))


def _local_day_key():
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _started_seconds_ago(ts):
    if not ts:return 0.0
    try:
        d=datetime.fromisoformat(str(ts))
        if d.tzinfo is None:d=d.replace(tzinfo=timezone.utc)
        return max(0.0,(datetime.now(timezone.utc)-d.astimezone(timezone.utc)).total_seconds())
    except Exception:return 0.0


def ensure_real_session(conn,args,executor):
    ctl=runtime_control(conn)
    if not ctl or not int(ctl["tool_running"] or 0) or not int(ctl["real_bet_armed"] or 0):
        return False
    if not args.real_bet_enabled:
        stop_real_tool(conn,"REAL_BET_DISABLED_BY_CLI")
        return False
    ok,reason=executor.configured()
    if not ok:
        stop_real_tool(conn,"REAL_BET_CONFIG_NOT_READY",reason,status="ERROR")
        return False
    try:
        bal=float(executor.read_balance())
    except Exception as exc:
        stop_real_tool(conn,"BALANCE_READ_FAILED",exc,status="ERROR")
        return False
    day=_local_day_key()
    old_day=str(ctl.get("day_key") or "")
    if ctl.get("session_start_balance") is None:
        day_start=bal if old_day!=day or ctl.get("day_start_balance") is None else float(ctl["day_start_balance"])
        conn.execute("""UPDATE runtime_control SET status='RUNNING',session_start_balance=?,current_balance=?,
            day_key=?,day_start_balance=?,last_error=NULL,updated_at=? WHERE id=1""",(bal,bal,day,day_start,now()))
        conn.commit()
        print(f"[REAL BET] STARTED | balance={money(bal)}")
    else:
        day_start=bal if old_day!=day or ctl.get("day_start_balance") is None else float(ctl["day_start_balance"])
        conn.execute("UPDATE runtime_control SET status='RUNNING',current_balance=?,day_key=?,day_start_balance=?,updated_at=? WHERE id=1",
                     (bal,day,day_start,now()))
        conn.commit()
    return True


def real_gate_check(gate,args):
    failed=[]
    if not gate.get("passed"): failed.append("TOP7_GATE")
    if str(gate.get("data_health") or "")!="OK": failed.append("DATA_HEALTH")
    if float(gate.get("calibrated_prob") or 0)<args.real_calibrated_min: failed.append("CALIBRATED")
    if float(gate.get("consensus_score") or 0)<args.real_consensus_min: failed.append("CONSENSUS")
    if float(gate.get("stability_score") or 0)<args.real_stability_min: failed.append("STABILITY")
    if float(gate.get("drift_score") or 999)>args.real_max_drift: failed.append("DRIFT")
    live_n = int(gate.get("live_samples") or 0)
    if live_n >= args.real_live_required_samples:
        if float(gate.get("live_hit_rate") or 0) < args.real_live_min_hit:
            failed.append("LIVE_HIT")

    regime_n = int(gate.get("regime_samples") or 0)
    if regime_n >= args.real_regime_required_samples:
        if float(gate.get("regime_hit_rate") or 0) < args.real_regime_min_hit:
            failed.append("REGIME_HIT")
    return len(failed)==0,failed


def real_auto_stop_reason(ctl,args,balance=None):
    if ctl is None:return "NO_CONTROL_STATE"

    loss_limit=max(1,_runtime_num(ctl,"stop_consecutive_losses",args.real_stop_consecutive_losses,int))
    if int(ctl.get("current_loss_streak") or 0)>=loss_limit:
        return f"LOSS_STREAK_{int(ctl.get('current_loss_streak') or 0)}"

    start=ctl.get("session_start_balance")
    if start is not None and float(start)>0 and balance is not None:
        start=float(start); balance=float(balance)
        profit_pct=max(0.0,_runtime_num(ctl,"stop_profit_pct",args.real_stop_profit_pct,float))
        floor_pct=max(0.0,min(1.0,_runtime_num(ctl,"stop_balance_floor_pct",args.real_stop_balance_floor_pct,float)))
        if profit_pct>0 and balance>=start*(1.0+profit_pct):
            return "BALANCE_PROFIT_TARGET"
        if floor_pct>0 and balance<=start*floor_pct:
            return "BALANCE_FLOOR_LIMIT"

    max_minutes=max(0.0,_runtime_num(ctl,"stop_max_minutes",args.real_stop_max_minutes,float))
    if max_minutes>0 and _started_seconds_ago(ctl.get("started_at"))>=max_minutes*60:
        return "MAX_SESSION_TIME"
    return None


def settle_real_bet(conn,issue,n,args):
    row=conn.execute("""SELECT id,step_index,selected_numbers_json,stake_per_number,total_stake,odds,status
        FROM real_bets WHERE issue=? AND status='OPEN'""",(issue,)).fetchone()
    if not row:return
    bid,step,js,stake_per,total,odds,status=row
    try:selected=[int(x) for x in json.loads(js)]
    except Exception:selected=[]
    won=int(n) in selected
    payout=float(stake_per)*float(odds) if won else 0.0
    profit=payout-float(total) if won else -float(total)
    ctl=runtime_control(conn) or {}
    spl=float(ctl.get("session_profit") or 0)+profit
    bets=int(ctl.get("session_bets") or 0)+1
    cws=int(ctl.get("current_win_streak") or 0)
    cls=int(ctl.get("current_loss_streak") or 0)
    lls=int(ctl.get("longest_loss_streak") or 0)
    if won:
        next_step=int(step)+1 if int(step)+1<4 else 0
        cws+=1; cls=0
    else:
        next_step=0; cws=0; cls+=1; lls=max(lls,cls)

    # balance_after_submit đã trừ stake. Khi kỳ resolve, website cộng payout nếu WIN.
    curbal=ctl.get("current_balance")
    expected_balance=(float(curbal)+payout) if curbal is not None else None

    conn.execute("""UPDATE real_bets SET status=?,actual_number=?,payout=?,profit=?,resolved_at=? WHERE id=?""",
                 ("WIN" if won else "LOSS",int(n),payout,profit,now(),bid))
    conn.execute("""UPDATE runtime_control SET session_profit=?,session_bets=?,current_step=?,
        current_win_streak=?,current_loss_streak=?,longest_loss_streak=?,current_balance=COALESCE(?,current_balance),updated_at=? WHERE id=1""",
                 (spl,bets,next_step,cws,cls,lls,expected_balance,now()))
    conn.commit()
    ctl=runtime_control(conn)
    reason=real_auto_stop_reason(ctl,args,balance=ctl.get("current_balance") if ctl else None)
    if reason and ctl and int(ctl.get("tool_running") or 0):
        stop_real_tool(conn,reason)


def maybe_place_real_bet(conn,issue,gate,args,executor):
    ctl=runtime_control(conn)
    if not ctl or not int(ctl.get("tool_running") or 0) or not int(ctl.get("real_bet_armed") or 0):
        return "STOPPED"
    if conn.execute("SELECT 1 FROM real_bets WHERE issue=?",(issue,)).fetchone():
        return "EXISTS"
    if conn.execute("SELECT 1 FROM real_bets WHERE status IN ('OPEN','UNKNOWN') LIMIT 1").fetchone():
        return "WAIT_OPEN_BET"
    if not ensure_real_session(conn,args,executor):
        return "NOT_READY"
    ctl=runtime_control(conn)
    balance=float(ctl.get("current_balance") or 0)
    reason=real_auto_stop_reason(ctl,args,balance=balance)
    if reason:
        stop_real_tool(conn,reason)
        return reason
    ok,failed=real_gate_check(gate,args)
    if not ok:
        return "REAL_GATE_SKIP:"+",".join(failed)
    ctl=runtime_control(conn)
    reason=real_auto_stop_reason(ctl,args,balance=balance)
    if reason:
        stop_real_tool(conn,reason)
        return reason
    pattern=[1,1,2,3]
    step=max(0,min(int(ctl.get("current_step") or 0),len(pattern)-1))
    unit=float(pattern[step])
    stake_per=float(args.real_base_stake)*unit
    total=stake_per*TOP7_COUNT
    if balance<total:
        stop_real_tool(conn,"INSUFFICIENT_BALANCE",f"balance={balance:.0f}, required={total:.0f}")
        return "INSUFFICIENT_BALANCE"
    created=now()
    conn.execute("""INSERT INTO real_bets(issue,step_index,unit_multiplier,selected_numbers_json,stake_per_number,
        total_stake,odds,hit_probability,status,balance_before,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (str(issue),step,unit,json.dumps(gate["selected"]),stake_per,total,TOP7_ODDS,gate.get("calibrated_prob"),"PREPARED",balance,created))
    conn.commit()
    try:
        result=executor.place_top7(str(issue),gate["selected"],stake_per,total)
        if bool(result.get("dry_run")):
            conn.execute("""UPDATE real_bets SET status='DRY_RUN',balance_after_submit=?,ticket_text=?,error_text=? WHERE issue=?""",
                         (result.get("balance_after"),result.get("ticket_text"),"DRY_RUN: submit không được click",str(issue)))
            conn.commit()
            stop_real_tool(conn,"DRY_RUN_COMPLETE","Config vẫn dry_run=true; chưa đặt tiền thật.",status="STOPPED")
            return "DRY_RUN"
        if not result.get("ok") or not result.get("confirmed"):
            conn.execute("UPDATE real_bets SET status='UNKNOWN',error_text=? WHERE issue=?",("Submission not confirmed",str(issue)))
            conn.commit(); stop_real_tool(conn,"UNKNOWN_SUBMISSION","Không xác nhận được ticket",status="ERROR")
            return "UNKNOWN"
        conn.execute("""UPDATE real_bets SET status='OPEN',balance_after_submit=?,ticket_text=?,error_text=NULL WHERE issue=?""",
                     (result.get("balance_after"),result.get("ticket_text"),str(issue)))
        if result.get("balance_after") is not None:
            conn.execute("UPDATE runtime_control SET current_balance=?,updated_at=? WHERE id=1",(float(result["balance_after"]),now()))
        conn.commit()
        return "PLACED"
    except RealBetError as exc:
        status="UNKNOWN" if exc.may_have_submitted else "ERROR"
        conn.execute("UPDATE real_bets SET status=?,error_text=? WHERE issue=?",(status,str(exc),str(issue)));conn.commit()
        stop_real_tool(conn,"REAL_BET_EXECUTOR_ERROR",exc,status="ERROR")
        return status
    except Exception as exc:
        conn.execute("UPDATE real_bets SET status='ERROR',error_text=? WHERE issue=?",(str(exc),str(issue)));conn.commit()
        stop_real_tool(conn,"REAL_BET_UNEXPECTED_ERROR",exc,status="ERROR")
        return "ERROR"


def export_real_bets(conn,args):
    atomic_csv(args.real_bets_csv,
        ["issue","step_index","unit_multiplier","selected_numbers_json","stake_per_number","total_stake","odds",
         "hit_probability","status","actual_number","payout","profit","balance_before","balance_after_submit",
         "ticket_text","error_text","created_at","resolved_at"],
        conn.execute("""SELECT issue,step_index,unit_multiplier,selected_numbers_json,stake_per_number,total_stake,odds,
            hit_probability,status,actual_number,payout,profit,balance_before,balance_after_submit,ticket_text,error_text,
            created_at,resolved_at FROM real_bets ORDER BY id""").fetchall())
    ctl=runtime_control(conn)
    if ctl:
        atomic_csv(args.real_control_csv,list(ctl.keys()),[list(ctl.values())])


def top7_selection(p):
    p=norm(p)
    selected=[int(k) for k in np.argsort(p)[::-1][:TOP7_COUNT]]
    bottom=[int(k) for k in np.argsort(p)[:3]]
    hit_prob=float(sum(p[k] for k in selected))
    estimated_roi=(hit_prob*TOP7_ODDS-TOP7_COUNT)/TOP7_COUNT
    return selected,bottom,hit_prob,estimated_roi


def top7_live_stats(conn,n=100,regime=None):
    if regime is None:
        rows=conn.execute("""SELECT hit FROM top7_predictions
            WHERE hit IS NOT NULL ORDER BY issue DESC LIMIT ?""",(int(n),)).fetchall()
    else:
        rows=conn.execute("""SELECT hit FROM top7_predictions
            WHERE hit IS NOT NULL AND regime_label=? ORDER BY issue DESC LIMIT ?""",(str(regime),int(n))).fetchall()
    if not rows:return {"n":0,"hit_rate":None}
    hits=sum(int(r[0]) for r in rows)
    return {"n":len(rows),"hit_rate":hits/len(rows)}


def top7_calibration(conn,current_raw,lookback=500):
    rows=conn.execute("""SELECT hit_probability,hit FROM top7_predictions
        WHERE hit IS NOT NULL ORDER BY issue DESC LIMIT ?""",(int(lookback),)).fetchall()
    rows=[(float(p),int(h)) for p,h in rows if p is not None and h is not None]
    if not rows:
        return float(current_raw),None,0,0

    # ECE riêng cho TOP7.
    edges=[.65,.70,.725,.75,.775,.80,.825,.85,.90,1.01]
    ece=0.0; total=len(rows)
    for lo,hi in zip(edges[:-1],edges[1:]):
        g=[(p,h) for p,h in rows if lo<=p<hi]
        if g:
            avgp=sum(p for p,_ in g)/len(g); acc=sum(h for _,h in g)/len(g)
            ece += len(g)/total*abs(acc-avgp)

    # Calibration cục bộ quanh xác suất hiện tại, có shrinkage về raw probability.
    local=[(p,h) for p,h in rows if abs(p-current_raw)<=.025]
    use=local if len(local)>=8 else rows[-min(100,len(rows)):]
    prior_strength=20.0
    calibrated=(sum(h for _,h in use)+prior_strength*float(current_raw))/(len(use)+prior_strength)
    return float(calibrated),float(ece),len(rows),len(use)


def top7_consensus(model_outs,ensemble_bottom,consensus_fraction=.70):
    active=list(model_outs.keys())
    if not active:return 0.0,0,{},0
    votes={int(k):0 for k in ensemble_bottom}
    for _,p in model_outs.items():
        b=set(int(k) for k in np.argsort(norm(p))[:3])
        for k in votes:
            votes[k]+=int(k in b)
    required=max(1,int(math.ceil(len(active)*float(consensus_fraction))))
    fractions={k:v/len(active) for k,v in votes.items()}
    score=float(np.mean(list(fractions.values()))) if fractions else 0.0
    strong=sum(1 for v in votes.values() if v>=required)
    return score,strong,votes,required


def top7_stability(x,current_selected):
    x=np.asarray(x,dtype=int); cur=set(int(k) for k in current_selected)
    scores=[]; detail={}
    for w in (50,100,300):
        if len(x)<20: continue
        xx=x[-min(w,len(x)):]
        counts=np.bincount(xx,minlength=10).astype(float)+1.0
        p=counts/counts.sum(); sel=set(int(k) for k in np.argsort(p)[::-1][:TOP7_COUNT])
        overlap=len(cur & sel)/TOP7_COUNT
        scores.append(overlap); detail[str(min(w,len(x)))]=sorted(sel)
    return (float(np.mean(scores)) if scores else 0.0),detail


def top7_gate(conn,p,model_outs,x,regime,health,drift,args):
    selected,bottom,raw_prob,est_roi=top7_selection(p)
    calibrated,ece,calibration_total_n,cal_n=top7_calibration(
        conn,raw_prob,args.top7_calibration_lookback
    )
    calibrated_roi=(calibrated*TOP7_ODDS-TOP7_COUNT)/TOP7_COUNT

    cons,strong,votes,required=top7_consensus(
        model_outs,bottom,args.top7_consensus_fraction
    )
    stability,stability_detail=top7_stability(x,selected)

    # v7.2:
    # Live hit dùng rolling window tối đa 80 kỳ. Khi có kỳ mới,
    # mẫu mới được thêm và mẫu cũ nhất tự rơi khỏi cửa sổ.
    # Khi đủ 80 mẫu, live hit trở thành hard gate.
    live_gate=top7_live_stats(conn,min(80,int(args.top7_live_lookback)))

    # Regime hit cũng dùng rolling window tối đa 80 mẫu đúng regime.
    # Hard gate vẫn kích hoạt từ 50 mẫu; n không bao giờ vượt 80.
    reg=top7_live_stats(conn,min(80,int(args.top7_regime_lookback)),regime=regime)

    live_rate=live_gate["hit_rate"]
    regime_rate=reg["hit_rate"]

    statuses={}

    def hard_status(name, ok):
        statuses[name]="PASS" if bool(ok) else "FAIL"

    hard_status("data_health_ok", str(health)=="OK")
    hard_status(
        "calibrated_probability_ok",
        calibrated>=args.top7_calibrated_min
    )
    hard_status(
        "consensus_ok",
        cons>=args.top7_consensus_min
    )
    hard_status(
        "stability_ok",
        stability>=args.top7_stability_min
    )
    hard_status(
        "drift_ok",
        float(drift)<=args.top7_max_drift
    )

    if live_gate["n"] < args.top7_live_activate_samples:
        statuses["live_hit_ok"]="WARMUP"
    else:
        hard_status(
            "live_hit_ok",
            live_rate is not None and live_rate>=args.top7_live_min_hit
        )

    if reg["n"] < args.top7_regime_activate_samples:
        statuses["regime_hit_ok"]="WARMUP"
    else:
        hard_status(
            "regime_hit_ok",
            regime_rate is not None and regime_rate>=args.top7_regime_min_hit
        )

    # Chỉ FAIL mới chặn. WARMUP không chặn.
    failed=[k for k,v in statuses.items() if v=="FAIL"]
    passed=(len(failed)==0)

    # checks bool giữ để tương thích dashboard/file cũ.
    # WARMUP được xem là True đối với quyết định gate nhưng trạng thái thật
    # vẫn nằm trong "statuses".
    checks={k:(v!="FAIL") for k,v in statuses.items()}

    # Các chỉ số dưới đây vẫn được theo dõi nhưng KHÔNG là hard gate:
    # Raw P(hit), Bottom3 probability, ECE/calibration error,
    # live decline và Gate Score.
    components={
        "probability":min(
            1.0,
            max(
                0.0,
                (calibrated-TOP7_BREAK_EVEN)/
                max(.001,.85-TOP7_BREAK_EVEN)
            )
        ),
        "consensus":cons,
        "stability":stability,
        "regime":regime_rate if regime_rate is not None else 0.0,
        "live":live_rate if live_rate is not None else 0.0,
        "calibration":max(
            0.0,
            1.0-(ece if ece is not None else 1.0)/
            max(args.top7_max_ece*2,.001)
        ),
        "health":1.0 if health=="OK" else 0.0,
        "drift":max(
            0.0,
            1.0-float(drift)/
            max(args.top7_max_drift*2,.001)
        ),
    }
    gate_score=100.0*sum(components.values())/len(components)

    return {
        "selected":selected,
        "bottom":bottom,
        "raw_prob":raw_prob,
        "calibrated_prob":calibrated,
        "raw_estimated_roi":est_roi,
        "estimated_roi":calibrated_roi,
        "consensus_score":cons,
        "consensus_strong_count":strong,
        "consensus_votes":votes,
        "consensus_required_votes":required,
        "stability_score":stability,
        "stability_detail":stability_detail,
        "regime_samples":reg["n"],
        "regime_hit_rate":regime_rate,
        "live_samples":live_gate["n"],
        "live_hit_rate":live_rate,
        "top7_ece":ece,
        "calibration_total_n":calibration_total_n,
        "calibration_local_n":cal_n,
        "gate_score":gate_score,
        "checks":checks,
        "statuses":statuses,
        "passed":passed,
        "failed":failed,
        "components":components
    }

def save_top7_prediction(conn,issue,gate,regime,health,drift):
    cur=conn.execute("""INSERT OR IGNORE INTO top7_predictions(
        issue,created_at,selected_numbers_json,hit_probability,break_even_probability,estimated_roi,
        calibrated_probability,bottom3_json,consensus_score,consensus_strong_count,stability_score,
        regime_label,regime_samples,regime_hit_rate,live_samples,live_hit_rate,calibration_ece_top7,
        data_health,drift_score,gate_score,gate_pass,gate_reasons_json
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (issue,now(),json.dumps(gate["selected"]),gate["raw_prob"],TOP7_BREAK_EVEN,gate["estimated_roi"],
         gate["calibrated_prob"],json.dumps(gate["bottom"]),gate["consensus_score"],gate["consensus_strong_count"],gate["stability_score"],
         regime,gate["regime_samples"],gate["regime_hit_rate"],gate["live_samples"],gate["live_hit_rate"],gate["top7_ece"],
         health,float(drift),gate["gate_score"],int(gate["passed"]),json.dumps({"failed":gate["failed"],"checks":gate["checks"],"statuses":gate["statuses"],"votes":gate["consensus_votes"],"stability":gate["stability_detail"]})))
    conn.commit()
    return bool(cur.rowcount)


def place_top7_bet(conn,issue,gate,args):
    if not args.top7_enabled:return False,"DISABLED"
    if not gate["passed"]:return False,"GATE_FAIL:"+",".join(gate["failed"])
    if conn.execute("SELECT 1 FROM top7_bets WHERE issue=?",(issue,)).fetchone():return False,"EXISTS"
    st=top7_state(conn)
    if not st:return False,"NO_STATE"
    level=max(1,int(st["current_level"]))
    stake=float(st["base_stake_per_number"])*(float(st["multiplier"])**(level-1))
    total=stake*TOP7_COUNT
    # v7.3: paper progression không còn giới hạn theo bankroll.
    # Bankroll chỉ là số dư mô phỏng và có thể âm; LOSS vẫn tiếp tục x4.
    conn.execute("""INSERT INTO top7_bets(
        issue,level,selected_numbers_json,stake_per_number,total_stake,odds,hit_probability,
        estimated_roi,status,bankroll_before,created_at
    ) VALUES(?,?,?,?,?,?,?,?, 'OPEN',?,?)""",
        (issue,level,json.dumps(gate["selected"]),stake,total,TOP7_ODDS,gate["calibrated_prob"],gate["estimated_roi"],float(st["bankroll"]),now()))
    conn.commit();return True,"PLACED"

def settle_top7(conn,issue,n):
    # Prediction live: chỉ chấm sau khi kết quả thực sự xuất hiện.
    row=conn.execute("SELECT selected_numbers_json FROM top7_predictions WHERE issue=? AND hit IS NULL",(issue,)).fetchone()
    if row:
        try:selected=json.loads(row[0])
        except:selected=[]
        hit=int(int(n) in [int(x) for x in selected])
        conn.execute("UPDATE top7_predictions SET actual_number=?,hit=?,resolved_at=? WHERE issue=?",(int(n),hit,now(),issue))

    bet=conn.execute("""SELECT id,level,selected_numbers_json,stake_per_number,total_stake,odds
        FROM top7_bets WHERE issue=? AND status='OPEN'""",(issue,)).fetchone()
    if not bet:
        conn.commit();return
    bid,level,js,stake,total,odds=bet
    try:selected=json.loads(js)
    except:selected=[]
    won=int(n) in [int(x) for x in selected]
    st=top7_state(conn)
    bankroll_before=float(st["bankroll"])
    payout=float(stake)*float(odds) if won else 0.0
    profit=payout-float(total) if won else -float(total)
    bankroll_after=bankroll_before+profit
    session_profit=float(st["session_profit"])+profit
    # v7.1: thắng reset level 1; thua luôn tăng level x4.
    # Không stop theo P/L, max level hoặc streak.
    next_level=1 if won else int(level)+1
    active=1;reason=None

    conn.execute("""UPDATE top7_bets SET status=?,actual_number=?,payout=?,profit=?,bankroll_after=?,
        session_profit_after=?,resolved_at=? WHERE id=?""",
        ("WIN" if won else "LOSS",int(n),payout,profit,bankroll_after,session_profit,now(),bid))
    conn.execute("""UPDATE top7_state SET bankroll=?,session_profit=?,current_level=?,active=?,stop_reason=?,updated_at=? WHERE id=1""",
        (bankroll_after,session_profit,next_level,1,reason,now()))
    conn.commit()


def top7_loss_streak_stats(conn):
    rows=conn.execute("""SELECT issue,status FROM top7_bets
        WHERE status IN ('WIN','LOSS') ORDER BY id""").fetchall()
    current=0
    longest=0
    longest_end_issue=None
    for issue,status in rows:
        if status=='LOSS':
            current+=1
            if current>longest:
                longest=current
                longest_end_issue=str(issue)
        else:
            current=0
    return {
        "current_loss_streak":int(current),
        "longest_loss_streak":int(longest),
        "longest_loss_streak_end_issue":longest_end_issue,
        "settled_bets":len(rows),
    }


def export_top7(conn,args):
    atomic_csv(args.top7_predictions_csv,
        ["issue","created_at","selected_numbers_json","bottom3_json","hit_probability","calibrated_probability","break_even_probability","estimated_roi","consensus_score","consensus_strong_count","stability_score","regime_label","regime_samples","regime_hit_rate","live_samples","live_hit_rate","calibration_ece_top7","data_health","drift_score","gate_score","gate_pass","gate_reasons_json","actual_number","hit","resolved_at"],
        conn.execute("""SELECT issue,created_at,selected_numbers_json,bottom3_json,hit_probability,calibrated_probability,break_even_probability,estimated_roi,consensus_score,consensus_strong_count,stability_score,regime_label,regime_samples,regime_hit_rate,live_samples,live_hit_rate,calibration_ece_top7,data_health,drift_score,gate_score,gate_pass,gate_reasons_json,actual_number,hit,resolved_at FROM top7_predictions ORDER BY issue""").fetchall())
    atomic_csv(args.top7_bets_csv,
        ["issue","level","selected_numbers_json","stake_per_number","total_stake","odds","hit_probability","estimated_roi","status","actual_number","payout","profit","bankroll_before","bankroll_after","session_profit_after","created_at","resolved_at"],
        conn.execute("SELECT issue,level,selected_numbers_json,stake_per_number,total_stake,odds,hit_probability,estimated_roi,status,actual_number,payout,profit,bankroll_before,bankroll_after,session_profit_after,created_at,resolved_at FROM top7_bets ORDER BY id").fetchall())
    st=top7_state(conn)
    if st:
        streaks=top7_loss_streak_stats(conn)
        state_out=dict(st)
        state_out.update(streaks)
        state_out["rolling_window_max"]=80
        state_out["unlimited_progression"]=1
        atomic_csv(args.top7_state_csv,list(state_out.keys()),[list(state_out.values())])
    export_strategies(conn,args)
    export_real_bets(conn,args)

def paper_candidate(p,mode,threshold,min_edge):
    s=summary(p)
    if mode=="off":return None
    if mode=="size":
        sel="Lớn" if s["p_large"]>=s["p_small"] else "Nhỏ"; pr=max(s["p_large"],s["p_small"]); od=ODDS["size"][sel]
        return {"market":"size","selection":sel,"probability":pr,"odds":od,"edge":pr*od-1} if pr>=threshold else None
    cand=[]
    if mode in ("color","best"):
        for sel,pr in (("Đỏ",s["p_red"]),("Xanh",s["p_green"]),("Tím",s["p_violet"])):
            od=ODDS["color"][sel]; cand.append({"market":"color","selection":sel,"probability":pr,"odds":od,"edge":pr*od-1})
    if mode in ("number","best"):
        k=int(np.argmax(p)); pr=float(p[k]); od=ODDS["number"][str(k)]; cand.append({"market":"number","selection":str(k),"probability":pr,"odds":od,"edge":pr*od-1})
    cand=[c for c in cand if c["edge"]>=min_edge]
    return max(cand,key=lambda c:c["edge"]) if cand else None

def place_bet(conn,issue,c,stake,conf):
    if not c:return False
    if conn.execute("SELECT 1 FROM paper_bets WHERE issue=? AND market=?",(issue,c["market"])).fetchone():return False
    conn.execute("INSERT INTO paper_bets(issue,market,selection,probability,odds,stake,estimated_edge,confidence_label,status,created_at) VALUES(?,?,?,?,?,?,?,?,'OPEN',?)",
                 (issue,c["market"],c["selection"],c["probability"],c["odds"],stake,c["edge"],conf,now()))
    conn.commit(); return True

def export_all(conn,args):
    atomic_csv(args.csv,["issue","number","size","color","collected_at"],conn.execute("SELECT issue,number,size,color,collected_at FROM results ORDER BY issue").fetchall())
    atomic_csv(args.bets_csv,["issue","market","selection","probability","odds","stake","estimated_edge","confidence_label","status","actual_number","payout","profit","created_at","resolved_at"],
               conn.execute("SELECT issue,market,selection,probability,odds,stake,estimated_edge,confidence_label,status,actual_number,payout,profit,created_at,resolved_at FROM paper_bets ORDER BY id").fetchall())
    atomic_csv(args.predictions_csv,["issue","created_at","sample_size","p_large","p_small","p_red","p_green","p_violet","side_pick","side_probability","confidence_label","regime_label","data_health","drift_score","calibration_ece","actual_number","resolved_at","weights_json"],
               conn.execute("SELECT issue,created_at,sample_size,p_large,p_small,p_red,p_green,p_violet,side_pick,side_probability,confidence_label,regime_label,data_health,drift_score,calibration_ece,actual_number,resolved_at,weights_json FROM adaptive_predictions ORDER BY issue").fetchall())
    m=live_metrics(conn,args.performance_lookback)
    atomic_csv(args.performance_csv,["model","n","number_logloss","size_brier","size_accuracy","top3_accuracy"],[[n,m[n]["n"],m[n]["number_logloss"],m[n]["size_brier"],m[n]["size_accuracy"],m[n]["top3_accuracy"]] for n in MODEL_NAMES])
    cal,ece=calibration_stats(conn,args.calibration_lookback)
    atomic_csv(args.calibration_csv,["bucket","n","avg_pred","actual_accuracy","gap","ece"],[[r["bucket"],r["n"],r["avg_pred"],r["actual_accuracy"],r["gap"],ece] for r in cal])
    atomic_csv(args.quality_csv,["created_at","current_issue","api_rows","new_rows","gap_count","duplicate_count","stale_flag","drift_seconds","health","detail_json"],
               conn.execute("SELECT created_at,current_issue,api_rows,new_rows,gap_count,duplicate_count,stale_flag,drift_seconds,health,detail_json FROM data_quality_log ORDER BY id").fetchall())
    st=conn.execute("SELECT updated_at,current_issue,regime_label,regime_features_json,data_health,drift_score,calibration_ece,ensemble_accuracy_50,ensemble_accuracy_100,weights_json FROM system_state WHERE id=1").fetchone()
    atomic_csv(args.state_csv,["updated_at","current_issue","regime_label","regime_features_json","data_health","drift_score","calibration_ece","ensemble_accuracy_50","ensemble_accuracy_100","weights_json"],[st] if st else [])
    export_top7(conn,args)

def cycle(conn,issue,args,health="OK",drift_seconds=0.0,real_executor=None):
    rows=conn.execute("SELECT number FROM results ORDER BY issue").fetchall(); x=np.asarray([r[0] for r in rows],int)
    if len(x)<20: print("[MODEL] chưa đủ dữ liệu"); return
    regime,regfeat=detect_regime(x); drift,driftfeat=drift_score(x); regfeat.update({"drift_features":driftfeat})
    outs=model_outputs(x); global_met=live_metrics(conn,args.performance_lookback); reg_met=regime_model_metrics(conn,regime,args.performance_lookback)
    w=meta_weights(outs.keys(),global_met,reg_met,args.min_live_predictions,args.min_regime_predictions)
    cal,ece=calibration_stats(conn,args.calibration_lookback)
    p,s,pick,prob,conf,isnew=save_prediction(conn,issue,x,outs,w,regime,regfeat,health,drift,ece)
    update_system_state(conn,issue,regime,regfeat,health,drift,ece,w)
    gate=top7_gate(conn,p,outs,x,regime,health,drift,args)
    gate["data_health"]=health
    gate["drift_score"]=float(drift)
    top7_new=save_top7_prediction(conn,issue,gate,regime,health,drift)
    strategy_placed=place_strategy_bets(conn,issue,gate,args)
    real_status="SCHEDULED" if real_executor is not None else "DISABLED"
    top7_nums=gate["selected"]; top7_prob=gate["raw_prob"]; top7_roi=gate["estimated_roi"]
    c=paper_candidate(p,args.paper_market,args.signal_threshold,args.min_edge); placed=place_bet(conn,issue,c,args.stake,conf)
    a50,n50=ensemble_accuracy(conn,50); a100,n100=ensemble_accuracy(conn,100)
    print("\n"+"="*84); print(f"Kỳ {issue} | n={len(x)} | prediction_new={isnew}")
    print(f"Regime={regime} | Health={health} | Drift={drift:.3f} | ECE={ece:.3f}")
    print(f"Lớn {s['p_large']*100:.2f}% | Nhỏ {s['p_small']*100:.2f}% | {conf}")
    print(f"Đỏ {s['p_red']*100:.2f}% | Xanh {s['p_green']*100:.2f}% | Tím {s['p_violet']*100:.2f}%")
    print("Top số:",", ".join(f"{k}={p[k]*100:.2f}%" for k in np.argsort(p)[::-1][:3]))
    print("Meta weights:",", ".join(f"{n}:{w[n]*100:.1f}%" for n in sorted(w,key=w.get,reverse=True)))
    if n50: print(f"Live50={a50*100:.2f}% (n={n50})")
    if n100: print(f"Live100={a100*100:.2f}% (n={n100})")
    t7s40=top7_live_stats(conn,40); t7s80=top7_live_stats(conn,80); strat_states=strategy_states(conn)
    print(f"TOP7: {top7_nums} | bottom3={gate['bottom']} | raw={top7_prob*100:.2f}% | calibrated={gate['calibrated_prob']*100:.2f}% | BE={TOP7_BREAK_EVEN*100:.2f}%")
    print(f"TOP7 gate: {'BET' if gate['passed'] else 'SKIP'} | score={gate['gate_score']:.1f}/100 | consensus={gate['consensus_score']*100:.1f}% strong={gate['consensus_strong_count']} | stability={gate['stability_score']*100:.1f}% | ECE={gate['top7_ece'] if gate['top7_ece'] is not None else '-'}")
    if gate['failed']: print("TOP7 gate fail:", ", ".join(gate['failed']))
    if t7s40["n"]: print(f"TOP7 live40={t7s40['hit_rate']*100:.2f}% (n={t7s40['n']})")
    if t7s80["n"]: print(f"TOP7 rolling80={t7s80['hit_rate']*100:.2f}% (n={t7s80['n']})")
    print("3-strategy live paper:", ",".join(strategy_placed) if strategy_placed else ("SKIP" if not gate["passed"] else "EXISTS"))
    for stx in strat_states:
        try: pat=json.loads(stx["pattern_json"])
        except Exception: pat=[1]
        step=max(0,min(int(stx["current_step"]),len(pat)-1)) if pat else 0
        unit=float(pat[step]) if pat else 1.0
        nxt=float(stx["base_stake_per_number"])*unit*TOP7_COUNT
        roi=(float(stx["session_profit"])/float(stx["total_stake"])*100) if float(stx["total_stake"] or 0)>0 else 0.0
        print(f"  {stx['label']}: P/L={money(stx['session_profit'])} ROI={roi:.2f}% bankroll={money(stx['bankroll'])} next={money(nxt)} step={step+1}/{len(pat)}")
    print("Paper:", "PLACED "+c["market"]+"/"+c["selection"] if placed else "NO BET")
    print("REAL:", real_status)
    print("="*84)
    return gate

def effective_real_bet_second(conn,args):
    ctl=runtime_control(conn)
    sec=_runtime_num(ctl,"bet_second",args.real_bet_second,int)
    return max(1,min(26,int(sec)))


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",default="wingo_88i_id77.db"); ap.add_argument("--csv",default="wingo_88i_id77.csv")
    ap.add_argument("--bets-csv",default="wingo_paper_bets.csv"); ap.add_argument("--predictions-csv",default="wingo_predictions.csv"); ap.add_argument("--performance-csv",default="wingo_model_performance.csv")
    ap.add_argument("--trigger-second",type=int,default=20); ap.add_argument("--poll",type=float,default=.12)
    ap.add_argument("--paper-market",choices=["off","size","color","number","best"],default="size")
    ap.add_argument("--signal-threshold",type=float,default=.55); ap.add_argument("--min-edge",type=float,default=.05)
    ap.add_argument("--stake",type=float,default=50000); ap.add_argument("--bankroll",type=float,default=1000000)
    ap.add_argument("--performance-lookback",type=int,default=300); ap.add_argument("--min-live-predictions",type=int,default=30)
    ap.add_argument("--min-regime-predictions",type=int,default=15); ap.add_argument("--calibration-lookback",type=int,default=500)
    ap.add_argument("--calibration-csv",default="wingo_calibration.csv"); ap.add_argument("--quality-csv",default="wingo_data_quality.csv"); ap.add_argument("--state-csv",default="wingo_system_state.csv")
    ap.add_argument("--top7-enabled",action=argparse.BooleanOptionalAction,default=True)
    ap.add_argument("--top7-bankroll",type=float,default=525000)
    ap.add_argument("--top7-base-stake",type=float,default=3000,help="Tiền mỗi số ở level 1")
    ap.add_argument("--top7-multiplier",type=float,default=4.0)
    # Các arg legacy còn giữ để DB cũ tương thích, nhưng v7.1 không dùng để dừng bet.
    ap.add_argument("--top7-max-level",type=int,default=999)
    ap.add_argument("--top7-stop-profit",type=float,default=0)
    ap.add_argument("--top7-stop-loss",type=float,default=0)
    ap.add_argument("--top7-min-prob",type=float,default=.75,help="Raw P(hit) tối thiểu")
    ap.add_argument("--top7-calibrated-min",type=float,default=.72)
    ap.add_argument("--top7-bottom3-max",type=float,default=.25)
    ap.add_argument("--top7-consensus-fraction",type=float,default=.70)
    ap.add_argument("--top7-consensus-min",type=float,default=.55,help="Consensus score tối thiểu cho hard gate")
    ap.add_argument("--top7-consensus-strong-count",type=int,default=2)
    ap.add_argument("--top7-stability-min",type=float,default=.75)
    ap.add_argument("--top7-gate-min-live",type=int,default=30)
    ap.add_argument("--top7-live-min-hit",type=float,default=.72)
    ap.add_argument("--top7-live-lookback",type=int,default=80,help="Rolling window TOP7 live, tối đa 80 mẫu")
    ap.add_argument("--top7-live-activate-samples",type=int,default=80,help="Khi rolling window đủ 80 mẫu, live_hit trở thành hard gate")
    ap.add_argument("--top7-live-decline-tolerance",type=float,default=.03)
    ap.add_argument("--top7-regime-min-samples",type=int,default=15)
    ap.add_argument("--top7-regime-min-hit",type=float,default=.72)
    ap.add_argument("--top7-regime-activate-samples",type=int,default=50,help="Đủ mẫu trong regime này mới thành hard gate")
    ap.add_argument("--top7-regime-lookback",type=int,default=80,help="Rolling window theo regime, tối đa 80 mẫu")
    ap.add_argument("--top7-calibration-lookback",type=int,default=500)
    ap.add_argument("--top7-max-ece",type=float,default=.06)
    ap.add_argument("--top7-max-drift",type=float,default=.45)
    ap.add_argument("--top7-gate-score-min",type=float,default=80.0)
    ap.add_argument("--top7-reset",action="store_true",help="Reset bankroll/session top7 về vốn ban đầu")
    ap.add_argument("--top7-predictions-csv",default="wingo_top7_predictions.csv")
    ap.add_argument("--top7-bets-csv",default="wingo_top7_bets.csv")
    ap.add_argument("--top7-state-csv",default="wingo_top7_state.csv")
    ap.add_argument("--strategy-test-enabled",action=argparse.BooleanOptionalAction,default=True,help="Chạy song song FLAT, 1-1-2-3, 1-3-2-6")
    ap.add_argument("--strategy-bankroll",type=float,default=525000,help="Vốn mô phỏng riêng cho mỗi chiến thuật")
    ap.add_argument("--strategy-base-stake",type=float,default=3000,help="Base stake mỗi số cho 1 unit")
    ap.add_argument("--strategy-reset",action="store_true",help="Xóa lịch sử test 3 chiến thuật và reset về vốn ban đầu")
    ap.add_argument("--strategy-bets-csv",default="wingo_strategy_bets.csv")
    ap.add_argument("--strategy-state-csv",default="wingo_strategy_state.csv")
    # v7.6 real betting: 1-1-2-3 only. Runtime starts STOPPED; dashboard START is required.
    ap.add_argument("--real-bet-enabled",action=argparse.BooleanOptionalAction,default=True)
    ap.add_argument("--real-resume-after-restart",action=argparse.BooleanOptionalAction,default=False,help="Mặc định false: writer restart/reboot sẽ DISARM real bet")
    ap.add_argument("--real-config",default="88i_realbet_config.json")
    ap.add_argument("--real-profile-dir",default="88i_browser_profile",help="Chỉ dùng khi login.mode=persistent; manual_each_run không cần profile")
    ap.add_argument("--real-base-stake",type=float,default=3000)
    ap.add_argument("--real-bet-second",type=int,default=10,help="API countdown second để submit real bet; dashboard có thể override khi START")
    ap.add_argument("--real-calibrated-min",type=float,default=.72)
    ap.add_argument("--real-consensus-min",type=float,default=.60)
    ap.add_argument("--real-stability-min",type=float,default=.80)
    ap.add_argument("--real-max-drift",type=float,default=.40)
    ap.add_argument("--real-live-min-hit",type=float,default=.73)
    ap.add_argument("--real-live-required-samples",type=int,default=80)
    ap.add_argument("--real-regime-min-hit",type=float,default=.73)
    ap.add_argument("--real-regime-required-samples",type=int,default=50)
    ap.add_argument("--real-stop-consecutive-losses",type=int,default=5)
    ap.add_argument("--real-stop-profit-pct",type=float,default=1.0,help="1.0 = +100 phần trăm so với balance lúc START")
    ap.add_argument("--real-stop-balance-floor-pct",type=float,default=.50,help="0.50 = dừng khi còn 50 phần trăm balance lúc START")
    ap.add_argument("--real-stop-max-minutes",type=float,default=90)
    ap.add_argument("--real-bets-csv",default="wingo_real_bets.csv")
    ap.add_argument("--real-control-csv",default="wingo_real_control.csv")
    ap.add_argument("--once",action="store_true"); args=ap.parse_args()
    conn=init_db(args.db,args.bankroll,args.stake); configure_top7(conn,args); configure_strategies(conn,args); s=session()
    if not args.real_resume_after_restart:
        conn.execute("""UPDATE runtime_control SET tool_running=0,real_bet_armed=0,status='STOPPED_RESTART',ended_at=?,
            stop_reason='SERVICE_RESTART_REQUIRES_MANUAL_START',updated_at=? WHERE id=1""",(now(),now())); conn.commit()
    real_executor=RealBetExecutor(args.real_config,args.real_profile_dir) if args.real_bet_enabled else None
    print("WINGO ADAPTIVE v7.6.3 TOP7 · VISIBLE MANUAL LOGIN · REAL 1-1-2-3 | writer process"); print("API:",API_URL)
    print(f"3 strategy paper test: vốn mô phỏng={money(args.strategy_bankroll)}/strategy | base={money(args.strategy_base_stake)}/số")
    print("Patterns: FLAT=[1] | 1-1-2-3 | 1-3-2-6. WIN đi bước kế tiếp; LOSS reset; hết pattern reset.")
    print("Balanced Gate v7.6: Live/Regime rolling tối đa 80. Paper: FLAT, 1-1-2-3, 1-3-2-6.")
    print("REAL BET 1-1-2-3: Chromium HIỂN THỊ + login thủ công mỗi lần chạy; prediction second20, real submit default second10; START/END tại dashboard.")
    # v7.6.2: login diễn ra TRƯỚC vòng prediction để không đợi tới giây cược mới mở browser.
    # Runtime vẫn STOPPED/DISARMED sau restart; login thành công KHÔNG tự đặt cược.
    if real_executor is not None:
        try:
            login_balance = float(real_executor.prepare_login())
            # Login thành công nhưng vẫn chưa ARM real bet.
            # Chỉ cập nhật trạng thái sẵn sàng + balance, đồng thời xóa lỗi browser cũ
            # để dashboard không tiếp tục hiển thị lỗi stale từ lần chạy trước.
            conn.execute(
                """UPDATE runtime_control
                   SET tool_running=0,
                       real_bet_armed=0,
                       status='STOPPED_READY',
                       current_balance=?,
                       stop_reason='AWAITING_MANUAL_START',
                       last_error=NULL,
                       updated_at=?
                   WHERE id=1""",
                (login_balance, now()),
            )
            conn.commit()
            print(f"[88I LOGIN] READY | balance={money(login_balance)} | waiting for START TOOL")
        except Exception as exc:
            print(f"[88I LOGIN] FAILED: {exc}")
            print("[88I LOGIN] Writer dừng. Chạy lại writer để mở Chromium sạch và đăng nhập lại.")
            real_executor.close()
            conn.close()
            raise SystemExit(2)

    try:
        while True:
            payload=fetch(s); info=timing(payload)
            if not args.once:
                payload,drift=wait_to_target(s,info,args.trigger_second,args.poll); info=timing(payload)
            rows=history(payload); new=upsert(conn,rows,args)
            health,gaps,dups,stale,detail=data_health(conn,info["issue"],len(rows),new,drift if not args.once else 0.0)
            conn.execute("INSERT INTO data_quality_log(created_at,current_issue,api_rows,new_rows,gap_count,duplicate_count,stale_flag,drift_seconds,health,detail_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (now(),info["issue"],len(rows),new,gaps,dups,stale,(drift if not args.once else 0.0),health,json.dumps(detail))); conn.commit()
            print(f"[PREDICT] second={info['second']:02d} current={info['issue']} new={new} health={health}")
            gate=cycle(conn,info["issue"],args,health,(drift if not args.once else 0.0),real_executor=real_executor)
            export_all(conn,args)
            if args.once: break

            # Prediction được tạo sớm ở trigger-second (default 20). Real bet chờ tới countdown second 10.
            if real_executor is not None and gate is not None:
                ctl=runtime_control(conn)
                if ctl and int(ctl.get("tool_running") or 0) and int(ctl.get("real_bet_armed") or 0):
                    bet_sec=effective_real_bet_second(conn,args)
                    latest=fetch(s); li=timing(latest)
                    if li["issue"]==info["issue"]:
                        if li["second"]<=bet_sec:
                            pbet=latest; bi=li
                        else:
                            pbet,_=wait_to_target(s,li,bet_sec,args.poll)
                            bi=timing(pbet) if pbet else li
                        if bi["issue"]==info["issue"] and bi["second"]<=bet_sec and bi["second"]>int(bi.get("close",3)):
                            status=maybe_place_real_bet(conn,info["issue"],gate,args,real_executor)
                            print(f"[REAL @ second={bi['second']:02d}] {status}")
                            export_real_bets(conn,args)
                        else:
                            print(f"[REAL] SKIP timing/issue changed | expected={info['issue']} got={bi['issue']} second={bi['second']}")

            # Chờ rời khỏi prediction trigger để tránh xử lý lặp cùng kỳ.
            while True:
                time.sleep(.35); p2=fetch(s)
                t2=timing(p2)
                if t2["issue"]!=info["issue"] or t2["second"]!=args.trigger_second: break
    except KeyboardInterrupt:
        export_all(conn,args); print("\nStopped")
    finally:
        if real_executor is not None:
            try: real_executor.close()
            except Exception: pass
        conn.close()
if __name__=="__main__": main()
