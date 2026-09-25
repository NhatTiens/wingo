from pathlib import Path
import json,time,sqlite3
from datetime import datetime, timezone
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
st.set_page_config(page_title="Wingo v7.6 Real Bet Control",page_icon="🎯",layout="wide")
BASE=Path(__file__).resolve().parent
F={"results":BASE/"wingo_88i_id77.csv","bets":BASE/"wingo_paper_bets.csv","pred":BASE/"wingo_predictions.csv","perf":BASE/"wingo_model_performance.csv","cal":BASE/"wingo_calibration.csv","quality":BASE/"wingo_data_quality.csv","state":BASE/"wingo_system_state.csv","top7pred":BASE/"wingo_top7_predictions.csv","top7bets":BASE/"wingo_top7_bets.csv","top7state":BASE/"wingo_top7_state.csv","stratbets":BASE/"wingo_strategy_bets.csv","stratstate":BASE/"wingo_strategy_state.csv","realbets":BASE/"wingo_real_bets.csv","realctl":BASE/"wingo_real_control.csv"}
DB=BASE/"wingo_88i_id77.db"
REALCFG=BASE/"88i_realbet_config.json"
PROFILE=BASE/"88i_browser_profile"
def rd(p):
    if not p.exists():return None
    for _ in range(4):
        try:return pd.read_csv(p,encoding="utf-8-sig")
        except:time.sleep(.08)
    return None
def pct(x):
    try:return f"{float(x)*100:.2f}%"
    except:return "-"
def num(x,d=3):
    try:return f"{float(x):.{d}f}"
    except:return "-"
def money(x):
    try:return f"{float(x):,.0f}".replace(",",".")
    except:return "-"
def now(): return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
def db():
    c=sqlite3.connect(DB,timeout=10);c.execute("PRAGMA busy_timeout=5000");return c
def ensure_runtime():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS runtime_control(
            id INTEGER PRIMARY KEY CHECK(id=1),tool_running INTEGER NOT NULL DEFAULT 0,real_bet_armed INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'STOPPED',started_at TEXT,ended_at TEXT,session_start_balance REAL,current_balance REAL,
            session_profit REAL NOT NULL DEFAULT 0,session_bets INTEGER NOT NULL DEFAULT 0,current_step INTEGER NOT NULL DEFAULT 0,
            current_win_streak INTEGER NOT NULL DEFAULT 0,current_loss_streak INTEGER NOT NULL DEFAULT 0,longest_loss_streak INTEGER NOT NULL DEFAULT 0,
            day_key TEXT,day_start_balance REAL,stop_reason TEXT,last_error TEXT,updated_at TEXT NOT NULL)""")
        cols={r[1] for r in c.execute("PRAGMA table_info(runtime_control)").fetchall()}
        for name,typ in [
            ('stop_consecutive_losses','INTEGER'),
            ('stop_profit_pct','REAL'),
            ('stop_balance_floor_pct','REAL'),
            ('stop_max_minutes','REAL'),
            ('bet_second','INTEGER'),
        ]:
            if name not in cols:
                c.execute(f"ALTER TABLE runtime_control ADD COLUMN {name} {typ}")
        c.execute("""INSERT OR IGNORE INTO runtime_control(id,tool_running,real_bet_armed,status,session_profit,session_bets,current_step,current_win_streak,current_loss_streak,longest_loss_streak,updated_at)
            VALUES(1,0,0,'STOPPED',0,0,0,0,0,0,?)""",(now(),))

def read_runtime():
    ensure_runtime()
    with db() as c:
        r=c.execute("""SELECT tool_running,real_bet_armed,status,started_at,ended_at,session_start_balance,current_balance,session_profit,session_bets,current_step,current_win_streak,current_loss_streak,longest_loss_streak,day_key,day_start_balance,stop_reason,last_error,stop_consecutive_losses,stop_profit_pct,stop_balance_floor_pct,stop_max_minutes,bet_second,updated_at FROM runtime_control WHERE id=1""").fetchone()
    keys=['tool_running','real_bet_armed','status','started_at','ended_at','session_start_balance','current_balance','session_profit','session_bets','current_step','current_win_streak','current_loss_streak','longest_loss_streak','day_key','day_start_balance','stop_reason','last_error','stop_consecutive_losses','stop_profit_pct','stop_balance_floor_pct','stop_max_minutes','bet_second','updated_at']
    return dict(zip(keys,r)) if r else {}

DEFAULT_REAL_SETTINGS={
    'bet_second':10,
}

def start_tool(settings):
    ensure_runtime()
    with db() as c:
        c.execute("""UPDATE runtime_control SET tool_running=1,real_bet_armed=1,status='START_REQUESTED',started_at=?,ended_at=NULL,
            session_start_balance=NULL,current_balance=NULL,session_profit=0,session_bets=0,current_step=0,current_win_streak=0,current_loss_streak=0,longest_loss_streak=0,
            stop_reason=NULL,last_error=NULL,stop_consecutive_losses=NULL,stop_profit_pct=NULL,
            stop_balance_floor_pct=NULL,stop_max_minutes=NULL,bet_second=?,updated_at=? WHERE id=1""",
            (now(),int(settings['bet_second']),now()))

def end_tool(reason='MANUAL_END',status='STOPPED'):
    ensure_runtime()
    with db() as c:
        c.execute("""UPDATE runtime_control SET tool_running=0,real_bet_armed=0,status=?,ended_at=?,stop_reason=?,updated_at=? WHERE id=1""",(status,now(),reason,now()))

def parse_start_settings(second_text):
    errs=[]
    try:
        sec=int(str(second_text).strip()) if str(second_text).strip() else 10
    except ValueError:
        return {'bet_second':10},['Giây đặt cược: không phải số nguyên hợp lệ']
    if not 4<=sec<=25: errs.append('Giây đặt cược: phải từ 4 đến 25')
    return {'bet_second':sec},errs

def config_status():
    cfg_exists = REALCFG.exists()
    profile_exists = PROFILE.exists()
    enabled = False
    dry_run = True
    headless = False
    login_mode = "manual_each_run"
    msg = "OK"
    if not cfg_exists:
        msg = "Thiếu 88i_realbet_config.json"
    else:
        try:
            x=json.loads(REALCFG.read_text(encoding='utf-8'))
            enabled = bool(x.get('enabled'))
            dry_run = bool(x.get('dry_run', True))
            headless = bool(x.get('headless', False))
            login_mode = str((x.get('login') or {}).get('mode') or 'manual_each_run').strip().lower()
        except Exception as e:
            msg = f"Lỗi đọc config: {e}"
    profile_required = (login_mode == "persistent")
    return {
        "cfg_exists": cfg_exists,
        "profile_exists": profile_exists,
        "profile_required": profile_required,
        "enabled": enabled,
        "dry_run": dry_run,
        "headless": headless,
        "login_mode": login_mode,
        "msg": msg,
    }

st.title("🎯 Wingo Adaptive v7.6.3 · VISIBLE REAL BET CONTROL")
st.caption("TOP7 · Real: 2.000 → 6.000 → 8.000 → 10.000đ/số · Chromium hiển thị · login thủ công mỗi lần chạy · START/END")
ensure_runtime()
ctl=read_runtime()
_cfg=config_status()
cfg_enabled=bool(_cfg["enabled"])
cfg_dry=bool(_cfg["dry_run"])
cfg_msg=str(_cfg["msg"])
profile_ok=bool(_cfg["profile_exists"])
profile_required=bool(_cfg["profile_required"])
login_mode=str(_cfg["login_mode"])
cfg_headless=bool(_cfg["headless"])
cfg_file_ok=bool(_cfg["cfg_exists"])
st.markdown("### 🎮 Điều khiển tool")
cc=st.columns([1.2,1.2,1.2,1,1,1,1])
cc[0].metric("Tool",str(ctl.get('status','STOPPED')))
cc[1].metric("Real Bet","ARMED" if int(ctl.get('real_bet_armed') or 0) else "DISARMED")
cc[2].metric("Số dư web",money(ctl.get('current_balance')))
cc[3].metric("Step",f"{min(max(int(ctl.get('current_step') or 0),0),3)+1}/4")
cc[4].metric("Session P/L",money(ctl.get('session_profit')))
cc[5].metric("Real bets",int(ctl.get('session_bets') or 0))
cc[6].metric("Loss streak",f"{int(ctl.get('current_loss_streak') or 0)}/{int(ctl.get('longest_loss_streak') or 0)}")
if ctl.get('stop_reason'): st.caption(f"Stop reason: {ctl.get('stop_reason')}")
if ctl.get('last_error'): st.error(str(ctl.get('last_error')))

st.caption(f"Config: {REALCFG}")
st.caption(f"Login mode: {login_mode}")

if not cfg_file_ok:
    st.error("Không tìm thấy 88i_realbet_config.json trong đúng thư mục dashboard.")
elif not cfg_enabled:
    st.warning("Config có enabled=false. Hãy đổi enabled=true.")
elif login_mode == "manual_each_run" and cfg_headless:
    st.error("manual_each_run yêu cầu headless=false để Chromium hiện ra cho bạn đăng nhập.")
elif profile_required and not profile_ok:
    st.warning("Login mode=persistent nhưng thiếu 88i_browser_profile.")
elif cfg_dry:
    st.info("DRY RUN: Chromium vẫn hiển thị nhưng không click submit thật.")
else:
    st.success("REAL BET VISIBLE đang bật: dry_run=false, Chromium sẽ hiển thị thao tác đặt cược thật sau khi bạn START.")
if login_mode == "manual_each_run":
    st.info("Mỗi lần chạy WRITER sẽ mở Chromium sạch và chờ bạn đăng nhập trực tiếp trên 88i. Sau LOGIN OK, dashboard sẽ hiện số dư và trạng thái STOPPED_READY; bạn vẫn phải bấm START.")
else:
    st.caption(f"Profile: {PROFILE}")
st.markdown("#### ⚙️ Thời điểm đặt cược")
second_txt=st.text_input("Đặt cược ở countdown giây",value="",placeholder="Mặc định 10")
start_settings,start_errors=parse_start_settings(second_txt)
if start_errors:
    st.error(" · ".join(start_errors))
st.caption(f"Submit tại countdown second {start_settings['bet_second']}. Cược thật chỉ dừng vì thiếu tiền cho 7 số hoặc lỗi vận hành không thể xác nhận lệnh; END/EMERGENCY STOP vẫn hoạt động.")
confirm=st.checkbox("Tôi xác nhận START sẽ cho phép cược thật 7 số với mức 2.000 → 6.000 → 8.000 → 10.000đ/số khi config dry_run=false và TOP7 Gate PASS.")

start_blockers=[]
if not confirm: start_blockers.append("chưa tick xác nhận")
if not cfg_file_ok: start_blockers.append("thiếu config")
elif not cfg_enabled: start_blockers.append("enabled=false")
if login_mode == "manual_each_run" and cfg_headless: start_blockers.append("manual login nhưng headless=true")
if profile_required and not profile_ok: start_blockers.append("thiếu 88i_browser_profile")
if start_errors: start_blockers.append("giây đặt cược không hợp lệ")

if start_blockers:
    st.caption("START đang khóa vì: " + " · ".join(start_blockers))
else:
    st.caption("START READY")

b1,b2,b3=st.columns(3)
if b1.button("▶ START TOOL",type="primary",use_container_width=True,disabled=bool(start_blockers)):
    start_tool(start_settings); st.success("Đã gửi START. Writer sẽ đọc số dư và đặt cược theo quy tắc mới khi TOP7 Gate PASS."); st.rerun()
if b2.button("■ END TOOL",use_container_width=True):
    end_tool('MANUAL_END','STOPPED'); st.warning("Đã END: không tạo lệnh mới. Lệnh đã submit trước đó vẫn được theo dõi."); st.rerun()
if b3.button("⛔ EMERGENCY STOP",use_container_width=True):
    end_tool('MANUAL_EMERGENCY_STOP','EMERGENCY_STOP'); st.error("Emergency stop đã kích hoạt."); st.rerun()
st.markdown("---")
with st.sidebar:
    refresh=st.selectbox("Refresh",[2,5,10,30],index=1,format_func=lambda x:f"{x} giây")
    window=st.selectbox("Lịch sử",[50,100,200,500,1000,"Tất cả"],index=2)
    st.markdown("---")
    for k,p in F.items():st.caption(("✅ " if p.exists() else "— ")+p.name)
@st.fragment(run_every=refresh)
def render():
    res,bet,pred,perf,cal,quality,state,top7pred,top7bets,top7state,stratbets,stratstate,realbets,realctl=[rd(F[k]) for k in ["results","bets","pred","perf","cal","quality","state","top7pred","top7bets","top7state","stratbets","stratstate","realbets","realctl"]]
    if res is None or res.empty:st.warning("Chưa có results CSV");return
    res["issue"]=res["issue"].astype(str);res["number"]=pd.to_numeric(res["number"],errors="coerce");res=res.dropna(subset=["number"]);res["number"]=res["number"].astype(int)
    latest=res.iloc[-1]; stt=state.iloc[-1] if state is not None and len(state) else None
    c=st.columns(8)
    c[0].metric("Tổng kỳ",len(res));c[1].metric("Kỳ",latest["issue"]);c[2].metric("Số",int(latest["number"]));c[3].metric("KQ",latest.get("size","-"))
    c[4].metric("Regime",stt["regime_label"] if stt is not None else "-");c[5].metric("Health",stt["data_health"] if stt is not None else "-")
    c[6].metric("Drift",num(stt["drift_score"]) if stt is not None else "-");c[7].metric("ECE",num(stt["calibration_ece"]) if stt is not None else "-")
    tabs=st.tabs(["🧠 Prediction","7️⃣ TOP7","💵 REAL","🧭 Regime","🎯 Calibration","⚖️ Models","🩺 Data Health","💰 Paper","📊 History"])
    with tabs[0]:
        if pred is None or pred.empty:st.info("Chưa có predictions")
        else:
            lp=pred.iloc[-1]; cc=st.columns(6)
            for col,label,key in zip(cc,["Lớn","Nhỏ","Đỏ","Xanh","Tím","Confidence"],["p_large","p_small","p_red","p_green","p_violet","confidence_label"]):col.metric(label,lp[key] if key=="confidence_label" else pct(lp[key]))
            st.write(f"**Side:** {lp['side_pick']} {pct(lp['side_probability'])} · **Regime:** {lp.get('regime_label','-')} · **Health:** {lp.get('data_health','-')}")
            r=pred[pred["actual_number"].notna()].copy()
            if len(r):
                r["actual_number"]=pd.to_numeric(r["actual_number"],errors="coerce");r=r.dropna(subset=["actual_number"]);r["actual_side"]=r["actual_number"].astype(int).apply(lambda n:"Lớn" if n>=5 else "Nhỏ");r["correct"]=r["side_pick"]==r["actual_side"]
                a=st.columns(4)
                for col,nm,label in [(a[0],50,"Acc 50"),(a[1],100,"Acc 100"),(a[2],300,"Acc 300")]:
                    q=r.tail(nm);col.metric(label,f"{q['correct'].mean()*100:.2f}%" if len(q) else "-",f"n={len(q)}")
                a[3].metric("Live",len(r))
                st.dataframe(r.tail(50)[["issue","side_pick","side_probability","regime_label","confidence_label","actual_number","actual_side","correct"]].sort_values("issue",ascending=False),use_container_width=True,hide_index=True)
    with tabs[1]:
        st.subheader("TOP7 BET GATE")
        with st.expander("Ngưỡng Balanced Gate v7.4", expanded=False):
            st.markdown("""
| Điều kiện | Ngưỡng | Vai trò |
|---|---:|---|
| Data health | OK | Hard gate |
| Calibrated P(hit) | ≥ 72% | Hard gate |
| Consensus | ≥ 55% | Hard gate |
| Stability | ≥ 75% | Hard gate |
| Drift | ≤ 0.45 | Hard gate |
| Live hit | ≥ 72% | Rolling tối đa 80; hard gate khi n = 80 |
| Regime hit | ≥ 72% | Rolling tối đa 80; hard gate khi n ≥ 50 |

Live/Regime luôn dùng tối đa 80 mẫu gần nhất: mẫu mới vào thì mẫu cũ nhất tự rơi khỏi cửa sổ. Raw P(hit), ECE và Gate Score vẫn chỉ để theo dõi.
""")
        st.caption("Rolling n = 0→80. Live hit hard gate khi đủ 80; Regime hit hard gate từ 50 và cả hai không bao giờ vượt n=80.")
        if top7pred is None or top7pred.empty:
            st.info("Chưa có TOP7 prediction. Hãy chạy writer v7.4.")
        else:
            lp=top7pred.iloc[-1]
            try: nums=json.loads(lp["selected_numbers_json"])
            except: nums=[]
            try: bottom=json.loads(lp.get("bottom3_json","[]"))
            except: bottom=[]
            gate_pass=bool(int(float(lp.get("gate_pass",0) or 0)))
            c1=st.columns(7)
            c1[0].metric("TOP7"," · ".join(str(x) for x in nums))
            c1[1].metric("Bottom3"," · ".join(str(x) for x in bottom))
            c1[2].metric("Raw P(hit)",pct(lp.get("hit_probability")))
            c1[3].metric("Calibrated",pct(lp.get("calibrated_probability")))
            c1[4].metric("Consensus",pct(lp.get("consensus_score")))
            c1[5].metric("Stability",pct(lp.get("stability_score")))
            c1[6].metric("Gate score",f"{float(lp.get('gate_score',0)):.1f}/100")

            if gate_pass: st.success("BET GATE: PASS → đủ điều kiện tạo paper bet")
            else: st.warning("BET GATE: SKIP → chưa thỏa toàn bộ điều kiện")

            g=st.columns(6)
            g[0].metric("Regime",str(lp.get("regime_label","-")))
            g[1].metric("Regime hit",pct(lp.get("regime_hit_rate")),f"n={int(float(lp.get('regime_samples',0) or 0))}")
            g[2].metric("Live hit",pct(lp.get("live_hit_rate")),f"n={int(float(lp.get('live_samples',0) or 0))}")
            g[3].metric("TOP7 ECE",num(lp.get("calibration_ece_top7")))
            g[4].metric("Data health",str(lp.get("data_health","-")))
            g[5].metric("Drift",num(lp.get("drift_score")))

            try:
                reason=json.loads(lp.get("gate_reasons_json","{}"))
                failed=reason.get("failed",[])
                checks=reason.get("checks",{})
                statuses=reason.get("statuses",{})
                votes=reason.get("votes",{})

                if failed:
                    st.error("Chưa đạt hard gate: "+", ".join(failed))
                elif gate_pass:
                    st.success("Không có hard gate nào FAIL.")

                if statuses:
                    rows=[]
                    for k,v in statuses.items():
                        rows.append({
                            "Điều kiện":k,
                            "Trạng thái":v,
                            "Có chặn BET?":"Có" if v=="FAIL" else "Không"
                        })
                    check_df=pd.DataFrame(rows)
                else:
                    check_df=pd.DataFrame([
                        {"Điều kiện":k,"Trạng thái":"PASS" if bool(v) else "FAIL","Có chặn BET?":"Không" if bool(v) else "Có"}
                        for k,v in checks.items()
                    ])

                if len(check_df):
                    st.dataframe(check_df,use_container_width=True,hide_index=True)

                warmups=[k for k,v in statuses.items() if v=="WARMUP"]
                if warmups:
                    st.info("Đang WARMUP, chưa dùng để chặn BET: "+", ".join(warmups))

                if votes:
                    st.write("**Bottom3 model votes:**",votes)
            except Exception:
                pass

            resolved=top7pred[top7pred["hit"].notna()].copy()
            if len(resolved):
                resolved["hit"]=pd.to_numeric(resolved["hit"],errors="coerce")
                a=st.columns(4)
                for col,n,label in [(a[0],20,"Hit 20"),(a[1],40,"Hit 40"),(a[2],80,"Rolling 80")]:
                    q=resolved.tail(n); col.metric(label,f"{q['hit'].mean()*100:.2f}%",f"n={len(q)}")
                q80=resolved.tail(80)
                a[3].metric("Window đang dùng",f"{q80['hit'].mean()*100:.2f}%",f"n={len(q80)}/80")

        st.markdown("### 🧪 LIVE PAPER TEST · 3 chiến thuật")
        st.caption("Cả 3 nhận cùng một TOP7 khi Bet Gate PASS. Đây là mô phỏng theo kết quả thật; writer không gửi lệnh cược lên website.")
        if stratstate is None or stratstate.empty:
            st.info("Chưa có strategy state. Chạy writer v7.4 để bắt đầu.")
        else:
            ss=stratstate.copy()
            for col in ["bankroll","session_profit","total_stake","max_total_stake","max_drawdown","roi","next_total_stake","next_stake_per_number"]:
                if col in ss: ss[col]=pd.to_numeric(ss[col],errors="coerce").fillna(0)
            for col in ["settled","wins","losses","current_win_streak","longest_win_streak","current_loss_streak","longest_loss_streak","current_step"]:
                if col in ss: ss[col]=pd.to_numeric(ss[col],errors="coerce").fillna(0).astype(int)

            cards=st.columns(3)
            for i,(_,r) in enumerate(ss.iterrows()):
                with cards[i % 3]:
                    st.markdown(f"#### {r.get('label',r.get('strategy','-'))}")
                    hit=(r.get('wins',0)/r.get('settled',1)*100) if r.get('settled',0)>0 else 0
                    st.metric("P/L",money(r.get("session_profit",0)),f"ROI {float(r.get('roi',0))*100:.2f}%")
                    st.metric("Bankroll",money(r.get("bankroll",0)))
                    st.metric("Kỳ đã chấm",int(r.get("settled",0)),f"Hit {hit:.2f}%")
                    st.metric("Cược kỳ tới",money(r.get("next_total_stake",0)),f"{money(r.get('next_stake_per_number',0))}/số")
                    st.caption(f"Max DD {money(r.get('max_drawdown',0))} · Max bet {money(r.get('max_total_stake',0))} · Loss streak {int(r.get('current_loss_streak',0))}/{int(r.get('longest_loss_streak',0))}")

            table_cols=[c for c in ["label","settled","wins","losses","session_profit","roi","bankroll","total_stake","max_drawdown","max_total_stake","current_step","next_total_stake","longest_win_streak","longest_loss_streak"] if c in ss.columns]
            show=ss[table_cols].copy()
            if "roi" in show: show["roi %"]=(show.pop("roi")*100).round(2)
            st.dataframe(show,use_container_width=True,hide_index=True)

        if stratbets is not None and not stratbets.empty:
            sb=stratbets[stratbets["status"].isin(["WIN","LOSS"])].copy()
            if len(sb):
                sb["profit"]=pd.to_numeric(sb["profit"],errors="coerce").fillna(0)
                sb["created_at"]=pd.to_datetime(sb["created_at"],errors="coerce")
                sb=sb.sort_values(["created_at","issue","strategy"])
                sb["equity"]=sb.groupby("strategy")["profit"].cumsum()
                fig=px.line(sb,x="created_at",y="equity",color="strategy",markers=True,title="Equity · 3 chiến thuật trên cùng TOP7 signals")
                st.plotly_chart(fig,use_container_width=True)
            st.markdown("#### Lệnh strategy gần nhất")
            cols=[c for c in ["issue","strategy","step_index","unit_multiplier","selected_numbers_json","stake_per_number","total_stake","status","actual_number","profit","bankroll_after","created_at","resolved_at"] if c in stratbets.columns]
            st.dataframe(stratbets.tail(120)[cols].sort_index(ascending=False),use_container_width=True,hide_index=True)
    with tabs[2]:
        st.subheader("💵 REAL BET · tăng mức sau mỗi tay thua")
        st.caption("2.000 → 6.000 → 8.000 → 10.000đ/số; thua tiếp giữ mức 10.000đ/số. Thắng trở về 2.000đ/số; TOP7 Gate SKIP giữ nguyên mức.")
        ctl2=read_runtime()
        pattern=[1,3,4,5]; step=max(0,min(int(ctl2.get('current_step') or 0),3)); unit=pattern[step]
        rc=st.columns(6)
        rc[0].metric("Status",str(ctl2.get('status','STOPPED')))
        rc[1].metric("Current step",f"{step+1}/4",f"x{unit}")
        rc[2].metric("Mặc định / số",money(2000*unit))
        rc[3].metric("Tổng 7 số",money(2000*unit*7))
        rc[4].metric("Session P/L",money(ctl2.get('session_profit')))
        rc[5].metric("Balance",money(ctl2.get('current_balance')))
        bsec=int(ctl2.get('bet_second') or DEFAULT_REAL_SETTINGS['bet_second'])
        st.markdown("**Tự dừng:** thiếu tiền cho đủ 7 số. Lỗi login, số dư, chọn số, tổng tiền hoặc xác nhận lệnh cũng dừng để tránh gửi lệnh không kiểm soát.")
        if ctl2.get('stop_reason') == 'INSUFFICIENT_BALANCE':
            st.error(f"Tool đã dừng vì không đủ tiền cược. {ctl2.get('last_error') or ''}")
        elif ctl2.get('stop_reason') and str(ctl2.get('status','')).startswith(('AUTO_STOPPED','ERROR')):
            st.error(f"Tool đã dừng: {ctl2['stop_reason']}. {ctl2.get('last_error') or ''}")
        st.caption(f"Prediction chạy sớm; real submit được lên lịch tại countdown second {bsec}. Không có giới hạn số lệnh.")
        st.markdown("**Cược thật dùng đúng TOP7 Gate:** Health=OK · Calibrated ≥72% · Consensus ≥55% · Stability ≥75% · Drift ≤0,45 · Live hit ≥72% khi đủ 80 mẫu · Regime hit ≥72% khi đủ 50 mẫu.")
        if realbets is None or realbets.empty:
            st.info("Chưa có real bet audit. Có thể START ở dry-run sau khi config selector.")
        else:
            rb=realbets.copy()
            for col in ['stake_per_number','total_stake','payout','profit','balance_before','balance_after_submit']:
                if col in rb: rb[col]=pd.to_numeric(rb[col],errors='coerce')
            def draw_hit(row):
                try:
                    if pd.isna(row['actual_number']):return None
                    return 'Trúng' if int(row['actual_number']) in json.loads(row['selected_numbers_json']) else 'Trượt'
                except (KeyError,TypeError,ValueError):return None
            rb['draw_hit']=rb.apply(draw_hit,axis=1)
            st.caption("Trúng/Trượt so theo số mở thưởng và 7 số đã chọn. Với lệnh UNKNOWN, đây chỉ là kết quả của dãy số; cần lịch sử 88i để xác nhận tiền cược đã được nhận.")
            settled=rb[rb['status'].isin(['WIN','LOSS'])].copy() if 'status' in rb else rb.iloc[0:0]
            if len(settled):
                wins=int((settled['status']=='WIN').sum()); losses=int((settled['status']=='LOSS').sum()); pl=settled['profit'].fillna(0).sum(); ts=settled['total_stake'].fillna(0).sum()
                m=st.columns(4);m[0].metric('Settled',len(settled));m[1].metric('WIN/LOSS',f"{wins}/{losses}");m[2].metric('Hit',f"{wins/len(settled)*100:.2f}%");m[3].metric('P/L',money(pl),f"ROI {pl/ts*100:.2f}%" if ts else '-')
            cols=[c for c in ['issue','step_index','unit_multiplier','selected_numbers_json','stake_per_number','total_stake','status','actual_number','draw_hit','profit','balance_before','balance_after_submit','ticket_text','error_text','created_at','resolved_at'] if c in rb.columns]
            st.dataframe(rb.tail(100)[cols].sort_index(ascending=False),use_container_width=True,hide_index=True)
    with tabs[3]:
        if stt is None:st.info("Chưa có system state")
        else:
            st.subheader(f"Current regime: {stt['regime_label']}")
            try:feat=json.loads(stt["regime_features_json"]);st.json(feat)
            except:pass
            if pred is not None and "regime_label" in pred:
                rp=pred[pred["actual_number"].notna()].copy()
                if len(rp):
                    rp["actual_number"]=pd.to_numeric(rp["actual_number"],errors="coerce");rp=rp.dropna(subset=["actual_number"]);rp["actual_side"]=rp["actual_number"].astype(int).apply(lambda n:"Lớn" if n>=5 else "Nhỏ");rp["correct"]=rp["side_pick"]==rp["actual_side"]
                    g=rp.groupby("regime_label").agg(n=("correct","size"),accuracy=("correct","mean")).reset_index();g["accuracy %"]=(g["accuracy"]*100).round(2)
                    st.dataframe(g,use_container_width=True,hide_index=True);st.plotly_chart(px.bar(g,x="regime_label",y="accuracy %",text_auto=".2f",title="Accuracy theo regime"),use_container_width=True)
    with tabs[4]:
        if cal is None or cal.empty:st.info("Chưa đủ calibration data")
        else:
            for k in ["avg_pred","actual_accuracy","gap","ece","n"]:cal[k]=pd.to_numeric(cal[k],errors="coerce")
            st.metric("Expected Calibration Error",num(cal["ece"].dropna().iloc[0] if cal["ece"].notna().any() else None))
            plot=cal[cal["n"]>0].copy();plot["Predicted %"]=plot["avg_pred"]*100;plot["Actual %"]=plot["actual_accuracy"]*100
            fig=go.Figure();fig.add_trace(go.Scatter(x=plot["Predicted %"],y=plot["Actual %"],mode="lines+markers",name="Observed"));fig.add_trace(go.Scatter(x=[50,70],y=[50,70],mode="lines",name="Perfect"));fig.update_layout(title="Reliability curve",xaxis_title="Predicted confidence %",yaxis_title="Actual accuracy %")
            st.plotly_chart(fig,use_container_width=True);st.dataframe(cal,use_container_width=True,hide_index=True)
    with tabs[5]:
        if pred is not None and len(pred):
            try:w=json.loads(pred.iloc[-1]["weights_json"]);wd=pd.DataFrame({"model":list(w),"weight %":[v*100 for v in w.values()]}).sort_values("weight %",ascending=False);st.plotly_chart(px.bar(wd,x="model",y="weight %",text_auto=".2f",title="Meta weights hiện tại"),use_container_width=True)
            except:pass
        if perf is not None:st.dataframe(perf,use_container_width=True,hide_index=True)
    with tabs[6]:
        if quality is None or quality.empty:st.info("Chưa có data quality log")
        else:
            q=quality.tail(100).copy();st.dataframe(q.sort_index(ascending=False),use_container_width=True,hide_index=True)
            if "health" in q:st.plotly_chart(px.histogram(q,x="health",title="Data health events"),use_container_width=True)
    with tabs[7]:
        if bet is None or bet.empty:st.info("Chưa có paper bets")
        else:
            settled=bet[bet["status"].isin(["WIN","LOSS"])].copy();wins=int((settled["status"]=="WIN").sum());losses=len(settled)-wins;profit=pd.to_numeric(settled["profit"],errors="coerce").fillna(0).sum();stake=pd.to_numeric(settled["stake"],errors="coerce").fillna(0).sum();roi=profit/stake*100 if stake else 0
            b=st.columns(5);b[0].metric("Settled",len(settled));b[1].metric("WIN",wins);b[2].metric("LOSS",losses);b[3].metric("Hit",f"{wins/len(settled)*100:.2f}%" if len(settled) else "-");b[4].metric("P/L",money(profit),f"ROI {roi:.2f}%")
            if len(settled):settled["equity"]=pd.to_numeric(settled["profit"],errors="coerce").fillna(0).cumsum();settled["bet_no"]=range(1,len(settled)+1);st.plotly_chart(px.line(settled,x="bet_no",y="equity",markers=True,title="Paper equity"),use_container_width=True)
            st.dataframe(bet.tail(100).sort_index(ascending=False),use_container_width=True,hide_index=True)
    with tabs[8]:
        h=res if window=="Tất cả" else res.tail(int(window));counts=h["number"].value_counts().reindex(range(10),fill_value=0);st.plotly_chart(px.bar(x=counts.index.astype(str),y=counts.values,text_auto=True,labels={"x":"Số","y":"Số lần"},title="Tần suất 0-9"),use_container_width=True)
        chart=res.tail(50).copy();chart["STT"]=range(1,len(chart)+1);fig=px.line(chart,x="STT",y="number",markers=True,hover_data=["issue","size","color"]);fig.update_yaxes(range=[-.5,9.5],dtick=1);st.plotly_chart(fig,use_container_width=True)
render()
