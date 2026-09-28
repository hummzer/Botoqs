"""
gold_cli v3 — Self-evolving multi-strategy SMC + Volume Profile + Absorption bot.
Core: Absorption + Volume Profile (buy VAL / sell VAH).
Advanced SMC: iFVG, invalid OB, liquidity sweep vs run, failed auction, breakout.
3 strategies: scalp (1M/5M), intraday (15M/1H), swing (4H/1D).
Self-evolving: tracks per-technique performance, morphs parameters, never backwards.
TraderDev: persists every strategy + stats.
Pairs: XAUUSD, USOIL, NASDAQ, BTCUSD, GBPJPY (only).
"""

import argparse, json, os, sys, hashlib, base64, getpass, time, logging, copy, math
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum
from collections import defaultdict
from statistics import mean, stdev

import numpy as np
import pandas as pd
import yfinance as yf
import ta
import requests
import yaml

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.rule import Rule

console = Console()
logger = logging.getLogger("gold_cli")

# ── Constants ──────────────────────────────────────────────────────────────────
DATA_DIR = Path.home() / ".gold_cli"
STATE_FILE = DATA_DIR / "state.json"
CONFIG_FILE = DATA_DIR / "config.yaml"
TRADERDEV_FILE = DATA_DIR / "traderdev_strategies.json"
ANALYSIS_FILE = Path.home() / "gold_cli" / "analysis.json"

VALID_PAIRS = ["XAUUSD", "USOIL", "NASDAQ", "BTCUSD", "GBPJPY"]
YH_PAIRS = {"XAUUSD": "GC=F", "USOIL": "CL=F", "NASDAQ": "NQ=F", "BTCUSD": "BTC-USD", "GBPJPY": "GBPJPY=X"}

BROKERS = {
    "justmarkets": {"name": "JustMarkets", "platform": "mt5", "rates": {"commission_per_lot": 0.0}},
    "oanda": {"name": "OANDA", "platform": "rest", "rates": {"commission_per_lot": 0.0}},
    "icmarkets": {"name": "IC Markets", "platform": "mt5", "rates": {"commission_per_lot": 3.5}},
    "pepperstone": {"name": "Pepperstone", "platform": "mt5", "rates": {"commission_per_lot": 3.0}},
    "xm": {"name": "XM", "platform": "mt5", "rates": {"commission_per_lot": 0.0}},
    "fbs": {"name": "FBS", "platform": "mt5", "rates": {"commission_per_lot": 0.0}},
    "exness": {"name": "Exness", "platform": "mt5", "rates": {"commission_per_lot": 0.0}},
    "forex_com": {"name": "Forex.com", "platform": "rest", "rates": {"commission_per_lot": 0.0}},
    "alpari": {"name": "Alpari", "platform": "mt5", "rates": {"commission_per_lot": 0.0}},
    "octa": {"name": "OctaFX", "platform": "mt5", "rates": {"commission_per_lot": 0.0}},
}

TIMEFRAMES = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "60m", "4h": "4h", "1d": "1d"}
SCALF_TFS = ["1m", "5m"]; INTRADAY_TFS = ["15m", "1h"]; SWING_TFS = ["4h", "1d"]

LONDON_OPEN_EAT, LONDON_CLOSE_EAT = 10, 14
NY_OPEN_EAT, NY_CLOSE_EAT = 15, 20

# ── Encryption ────────────────────────────────────────────────────────────────
def _derive_key(pw: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), b"gold_cli_v3_salt", 100_000, dklen=32)
def _encrypt(pt: str, key: bytes) -> str:
    b = pt.encode(); rk = (key*(len(b)//len(key)+1))[:len(b)]
    return base64.b64encode(bytes(x^y for x,y in zip(b,rk))).decode()
def _decrypt(ct: str, key: bytes) -> str:
    b = base64.b64decode(ct.encode()); rk = (key*(len(b)//len(key)+1))[:len(b)]
    return bytes(x^y for x,y in zip(b,rk)).decode()

# ── Enums ──────────────────────────────────────────────────────────────────────
class Side(Enum): BUY="buy"; SELL="sell"
class TradeType(Enum): SCALP="scalp"; INTRADAY="intraday"; SWING="swing"
class Technique(Enum):
    ABSORPTION="absorption"; VOLUME_PROFILE="volume_profile"; ORDER_BLOCK="order_block"
    FVG="fvg"; IFVG="ifvg"; INVALID_OB="invalid_ob"; LIQ_SWEEP="liquidity_sweep"
    LIQ_RUN="liquidity_run"; FAILED_AUCTION="failed_auction"; BREAKOUT="breakout"
    MARKET_STRUCTURE="market_structure"
class MarketState(Enum): TREND_BULL="trend_bull"; TREND_BEAR="trend_bear"; RANGE="range"; BREAKOUT_UP="breakout_up"; BREAKOUT_DOWN="breakout_down"; CHOPPY="choppy"

# ── Data Classes ──────────────────────────────────────────────────────────────
@dataclass
class TradeSignal:
    pair: str; side: Side; entry_price: float; stop_loss: float; take_profit: float
    timeframe: str; strategy: str; trade_type: TradeType; technique: Technique
    rationale: str; timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    risk_amount: float = 0.0; reward_amount: float = 0.0; rr_ratio: float = 0.0
    confidence: float = 0.0; confluence_score: int = 0

@dataclass
class ExecutedTrade:
    pair: str; side: Side; entry_price: float; stop_loss: float; take_profit: float
    lot_size: float; broker: str; ticket: int = 0; status: str = "open"
    entry_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    exit_time: Optional[datetime] = None; exit_price: Optional[float] = None
    pnl: float = 0.0; commissions: float = 0.0; slippage: float = 0.0
    technique: Technique = Technique.MARKET_STRUCTURE; trade_type: TradeType = TradeType.SCALP
    strategy_name: str = "smc_core"; trail_stop: Optional[float] = None
    peak_price: Optional[float] = None; trough_price: Optional[float] = None

@dataclass
class TechniqueStats:
    technique: Technique; trades: int = 0; wins: int = 0; losses: int = 0
    total_pnl: float = 0.0; avg_win: float = 0.0; avg_loss: float = 0.0
    win_rate: float = 0.0; avg_rr: float = 0.0; sharpe: float = 0.0
    max_drawdown: float = 0.0; consec_wins: int = 0; max_consec_wins: int = 0
    consec_losses: int = 0; max_consec_losses: int = 0
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

@dataclass
class StrategyConfig:
    name: str; trade_type: TradeType; active_timeframes: List[str]
    risk_per_trade: float = 10.0; stop_loss_tightness: float = 1.0
    rr_target: float = 2.0; min_confluence: int = 4; max_concurrent: int = 2
    trail_enabled: bool = True; trail_offset: float = 1.0; trail_step: float = 0.5
    use_absorption: bool = True; use_volume_profile: bool = True
    use_order_blocks: bool = True; use_fvg: bool = True; use_ifvg: bool = True
    use_invalid_ob: bool = True; use_liquidity_sweep: bool = True
    use_liquidity_run: bool = True; use_failed_auction: bool = True; use_breakout: bool = True
    weight_absorption: float = 1.0; weight_volume_profile: float = 1.0
    weight_order_block: float = 1.0; weight_fvg: float = 1.0; weight_ifvg: float = 0.8
    weight_invalid_ob: float = 0.8; weight_liquidity_sweep: float = 1.0
    weight_liquidity_run: float = 0.8; weight_failed_auction: float = 0.7
    weight_breakout: float = 0.6; weight_market_structure: float = 1.5

@dataclass
class BotConfig:
    broker: str = "justmarkets"; is_demo: bool = True
    pairs: List[str] = field(default_factory=lambda: VALID_PAIRS.copy())
    strategies: List[StrategyConfig] = field(default_factory=lambda: [
        StrategyConfig(name="smc_core", trade_type=TradeType.INTRADAY, active_timeframes=["15m","1h","4h"],
                       risk_per_trade=10.0, stop_loss_tightness=1.0, rr_target=2.0, min_confluence=4, max_concurrent=2),
        StrategyConfig(name="scalp", trade_type=TradeType.SCALP, active_timeframes=SCALF_TFS,
                       risk_per_trade=5.0, stop_loss_tightness=0.7, rr_target=2.0, min_confluence=3, max_concurrent=3),
        StrategyConfig(name="swing", trade_type=TradeType.SWING, active_timeframes=SWING_TFS,
                       risk_per_trade=25.0, stop_loss_tightness=1.2, rr_target=3.0, min_confluence=5, max_concurrent=1),
    ])
    account_type: str = "cents"; start_balance: float = 100.0
    slippage: float = 2.0; spread_buffer: float = 1.0; commission_per_lot: float = 0.0
    min_rr_after_costs: float = 1.5; max_daily_loss: float = 50.0
    news_filter: bool = True; sentiment_enabled: bool = True; london_session_only: bool = True
    self_evolve: bool = True; evolve_interval_hours: int = 24

# ── State / Persistence ───────────────────────────────────────────────────────
def load_state() -> Dict:
    if STATE_FILE.exists(): return json.loads(STATE_FILE.read_text())
    return {"brokers":{},"master_hash":"","selected_pairs":VALID_PAIRS.copy(),
            "trades":[],"daily_pnl":0.0,"daily_loss":0.0,"start_balance":100.0,
            "current_balance":100.0,"technique_stats":{},"strategy_performance":{},
            "self_evolve_params":{},"consecutive_wins":0,"max_consecutive_wins":0,
            "consecutive_losses":0,"max_consecutive_losses":0,"weekly_pnl":{},
            "weekly_offset":datetime.now(timezone.utc).strftime("%Y-W%W")}
def save_state(s: Dict):
    STATE_FILE.parent.mkdir(parents=True,exist_ok=True)
    STATE_FILE.write_text(json.dumps(s,indent=2,default=str))
def load_config() -> BotConfig:
    if CONFIG_FILE.exists():
        d = yaml.safe_load(CONFIG_FILE.read_text())
        if d and "strategies" in d:
            d["strategies"] = [StrategyConfig(**s) for s in d["strategies"]]
        return BotConfig(**d)
    return BotConfig()
def save_config(c: BotConfig):
    CONFIG_FILE.parent.mkdir(parents=True,exist_ok=True)
    d = asdict(c); d["strategies"] = [asdict(s) for s in d["strategies"]]
    CONFIG_FILE.write_text(yaml.dump(d,default_flow_style=False))
def load_credentials(broker: str) -> Optional[Dict]:
    state = load_state(); brokers = state.get("brokers",{})
    if broker in brokers:
        c = brokers[broker]
        if c.get("password_encrypted"):
            mh = state.get("master_hash",""); master = getpass.getpass("Master passphrase: ").strip()
            if master and hashlib.sha256(master.encode()).hexdigest()==mh:
                try: c["password"] = _decrypt(c["password_encrypted"], _derive_key(master))
                except: return None
        return c
    return None

# ── TraderDev persistence ─────────────────────────────────────────────────────
def load_traderdev() -> Dict:
    if TRADERDEV_FILE.exists(): return json.loads(TRADERDEV_FILE.read_text())
    return {"strategies":[],"last_updated":""}
def save_traderdev(d: Dict):
    TRADERDEV_FILE.parent.mkdir(parents=True,exist_ok=True)
    d["last_updated"] = datetime.now(timezone.utc).isoformat()
    TRADERDEV_FILE.write_text(json.dumps(d,indent=2,default=str))
def register_strategy_in_traderdev(sc: StrategyConfig, stats: Dict):
    td = load_traderdev()
    entry = {"name":sc.name,"trade_type":sc.trade_type.value,"active_timeframes":sc.active_timeframes,
             "risk_per_trade":sc.risk_per_trade,"stop_loss_tightness":sc.stop_loss_tightness,
             "rr_target":sc.rr_target,"min_confluence":sc.min_confluence,
             "technique_weights":{"absorption":sc.weight_absorption,"volume_profile":sc.weight_volume_profile,
                                 "order_block":sc.weight_order_block,"fvg":sc.weight_fvg,"ifvg":sc.weight_ifvg,
                                 "invalid_ob":sc.weight_invalid_ob,"liquidity_sweep":sc.weight_liquidity_sweep,
                                 "liquidity_run":sc.weight_liquidity_run,"failed_auction":sc.weight_failed_auction,
                                 "breakout":sc.weight_breakout,"market_structure":sc.weight_market_structure},
             "performance":stats,"registered_at":datetime.now(timezone.utc).isoformat()}
    for i,e in enumerate(td["strategies"]):
        if e["name"]==sc.name: td["strategies"][i]=entry; break
    else: td["strategies"].append(entry)
    save_traderdev(td)

# ── Setup Wizard ──────────────────────────────────────────────────────────────
def setup_wizard():
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    console.print(Panel.fit("[bold blue]gold_cli v3 setup[/bold blue]\n"
        "Multi-strategy SMC + Volume Profile + Absorption bot",border_style="blue"))
    # Broker
    console.print("\n[bold]Step 1: Select Broker[/bold]")
    t = Table(title="Available Brokers (Top 10)")
    t.add_column("ID",style="cyan"); t.add_column("Broker",style="green")
    t.add_column("Platform",style="yellow"); t.add_column("Comm/lot",style="magenta")
    for bid,bi in BROKERS.items():
        t.add_row(bid,bi["name"],bi["platform"].upper(),f"${bi['rates']['commission_per_lot']:.1f}")
    console.print(t)
    bid = input("\nBroker ID (e.g. justmarkets): ").strip().lower()
    if bid not in BROKERS: console.print(f"[red]Unknown. Available: {', '.join(BROKERS.keys())}[/red]"); return False
    bi = BROKERS[bid]; console.print(f"\n[green]Selected: {bi['name']} ({bi['platform']})[/green]")
    # Credentials
    console.print(f"\n[bold]Step 2: {bi['name']} Credentials[/bold]")
    acct = input("Account number: ").strip()
    pw = getpass.getpass("Trading password: ").strip()
    srv = input(f"Server (default: {bi.get('default_server_demo','demo')}): ").strip() or bi.get('default_server_demo','demo')
    email = input("Email (optional): ").strip()
    if not acct or not pw: console.print("[red]Account and password required.[/red]"); return False
    # Demo/Real
    console.print("\n[bold]Step 3: Account Type[/bold]")
    console.print("[yellow]1[/yellow] Demo  [red]2[/red] Real")
    is_demo = input("Select (1/2): ").strip() != "2"
    # Master
    console.print("\n[bold]Step 4: Master Passphrase[/bold]")
    m = getpass.getpass("Master passphrase: ").strip()
    mc = getpass.getpass("Confirm: ").strip()
    if m!=mc or not m: console.print("[red]Passphrases do not match.[/red]"); return False
    # Account type
    console.print("\n[bold]Step 5: Account Type[/bold]")
    console.print("[yellow]1[/yellow] Cents (100¢=$1)  [green]2[/green] Dollars ($100)")
    atype = "cents" if input("Select (1/2): ").strip()!="2" else "dollars"
    sb = input(f"Starting balance ({'$' if atype=='dollars' else '¢'}, default {'100' if atype=='dollars' else '100'}): ").strip()
    start_bal = float(sb) if sb else 100.0
    # Strategies
    console.print("\n[bold]Step 6: Strategy Selection[/bold]")
    console.print("[green]Strategies:[/green]")
    console.print("  [cyan]smc_core[/cyan] — Absorption + Volume Profile (primary, all TFs)")
    console.print("  [cyan]scalp[/cyan] — 1M/5M fast scalps, tight stops")
    console.print("  [cyan]intraday[/cyan] — 15M/1H intraday with swing potential")
    console.print("  [cyan]swing[/cyan] — 4H/1D multi-day swings")
    console.print("  [cyan]breakout[/cyan] — Breakout + failed auction strategy")
    console.print("  [cyan]liquidity[/cyan] — Liquidity sweep/run specialist")
    si = input("\nComma-separated (default: smc_core,scalp,swing): ").strip()
    all_s = {
        "smc_core":StrategyConfig(name="smc_core",trade_type=TradeType.INTRADAY,active_timeframes=["15m","1h","4h"],
            risk_per_trade=10.0,stop_loss_tightness=1.0,rr_target=2.0,min_confluence=4,max_concurrent=2),
        "scalp":StrategyConfig(name="scalp",trade_type=TradeType.SCALP,active_timeframes=SCALF_TFS,
            risk_per_trade=5.0,stop_loss_tightness=0.7,rr_target=2.0,min_confluence=3,max_concurrent=3),
        "intraday":StrategyConfig(name="intraday",trade_type=TradeType.INTRADAY,active_timeframes=INTRADAY_TFS,
            risk_per_trade=15.0,stop_loss_tightness=1.0,rr_target=2.5,min_confluence=4,max_concurrent=2),
        "swing":StrategyConfig(name="swing",trade_type=TradeType.SWING,active_timeframes=SWING_TFS,
            risk_per_trade=25.0,stop_loss_tightness=1.2,rr_target=3.0,min_confluence=5,max_concurrent=1),
        "breakout":StrategyConfig(name="breakout",trade_type=TradeType.INTRADAY,active_timeframes=["15m","1h"],
            risk_per_trade=10.0,stop_loss_tightness=0.8,rr_target=2.0,min_confluence=3,max_concurrent=2,
            use_breakout=True,use_failed_auction=True,use_absorption=False,use_volume_profile=False),
        "liquidity":StrategyConfig(name="liquidity",trade_type=TradeType.SCALP,active_timeframes=["1m","5m","15m"],
            risk_per_trade=5.0,stop_loss_tightness=0.6,rr_target=1.5,min_confluence=3,max_concurrent=3,
            use_liquidity_sweep=True,use_liquidity_run=True,use_absorption=False,use_volume_profile=False,
            use_order_blocks=False,use_fvg=False),
    }
    sel = [s.strip() for s in si.split(",")] if si else ["smc_core","scalp","swing"]
    sel_strats = [all_s[s] for s in sel if s in all_s]
    if not sel_strats: sel_strats = [all_s["smc_core"]]
    # Pairs
    console.print("\n[bold]Step 7: Pair Selection[/bold]")
    for i,p in enumerate(VALID_PAIRS,1): console.print(f"  [{i}] {p}")
    pi = input("\nNumbers (default: all): ").strip()
    if pi:
        try: sp = [VALID_PAIRS[int(x.strip())-1] for x in pi.split(",") if 0<int(x.strip())<=len(VALID_PAIRS)]
        except: sp = VALID_PAIRS.copy()
    else: sp = VALID_PAIRS.copy()
    if not sp: sp = VALID_PAIRS.copy()
    # Risk params
    console.print("\n[bold]Step 8: Risk Parameters[/bold]")
    slippage = float(input("Slippage (cents, default 2): ") or 2)
    spread_buffer = float(input("Spread buffer (cents, default 1): ") or 1)
    commission = float(input("Commission per lot (cents, default 0): ") or 0)
    min_rr = float(input("Min R:R after costs (default 1.5): ") or 1.5)
    max_loss = float(input(f"Max daily loss ({'¢' if atype=='cents' else '$'}, default {'50' if atype=='cents' else '50'}): ") or (50 if atype=="cents" else 50))
    # Save
    key = _derive_key(pw); state = load_state()
    state["brokers"][bid] = {"broker":bid,"account":acct,"password_encrypted":_encrypt(pw,key),
        "server":srv,"email":email,"platform":bi["platform"],"is_demo":is_demo}
    state["master_hash"] = hashlib.sha256(m.encode()).hexdigest()
    state["selected_pairs"] = sp; state["account_type"] = atype
    state["start_balance"] = start_bal; state["current_balance"] = start_bal
    state["slippage"] = slippage; state["spread_buffer"] = spread_buffer
    state["commission_per_lot"] = commission; state["min_rr_after_costs"] = min_rr
    state["max_daily_loss"] = max_loss; save_state(state)
    config = BotConfig(broker=bid,is_demo=is_demo,pairs=sp,strategies=sel_strats,
        account_type=atype,start_balance=start_bal,slippage=slippage,spread_buffer=spread_buffer,
        commission_per_lot=commission,min_rr_after_costs=min_rr,max_daily_loss=max_loss)
    save_config(config); os.chmod(STATE_FILE,0o600)
    console.print(f"\n[green]✓ Setup complete![/green]")
    console.print(f"  Broker: {bi['name']} ({'Demo' if is_demo else 'Real'})")
    console.print(f"  Account: {'Cents' if atype=='cents' else 'Dollars'} — start: {start_bal}")
    console.print(f"  Strategies: {', '.join(s.name for s in sel_strats)}")
    console.print(f"  Pairs: {', '.join(sp)}")
    console.print(f"  Slippage: {slippage}¢ | Spread buffer: {spread_buffer}¢ | Commission: {commission}¢/lot")
    console.print(f"  Min R:R: {min_rr} | Max daily loss: {max_loss}")
    console.print(f"\n[yellow]⚠ Master passphrase: {m}[/yellow]")
    return True

# ── Data Acquisition ──────────────────────────────────────────────────────────
def fetch_pair_data(pair: str, period: str="14d", interval: str="15m") -> pd.DataFrame:
    yfs = YH_PAIRS.get(pair,pair)
    if interval=="1m" and isinstance(period,str) and period.endswith("d"):
        days = int(period[:-1])
        if days>7:
            all_dfs=[]
            for sd in range(0,days,7):
                cd = min(7,days-sd)
                try:
                    dfc = yf.download(yfs,period=f"{cd}d",interval="1m",progress=False,
                        start=pd.Timestamp.now()-pd.Timedelta(days=sd+cd),
                        end=pd.Timestamp.now()-pd.Timedelta(days=sd))
                    if not dfc.empty: all_dfs.append(dfc)
                except: pass
            if all_dfs:
                df = pd.concat(all_dfs).sort_index().drop_duplicates()
                if isinstance(df.columns,pd.MultiIndex): df.columns=df.columns.get_level_values(0)
                df.index.name="Datetime"; return df
            return pd.DataFrame()
    try:
        df = yf.download(yfs,period=period,interval=interval,progress=False)
        if df.empty: return pd.DataFrame()
        if isinstance(df.columns,pd.MultiIndex): df.columns=df.columns.get_level_values(0)
        df.index.name="Datetime"; return df
    except Exception as e:
        logger.warning(f"Failed fetch {pair} {interval}: {e}"); return pd.DataFrame()

def fetch_all_pairs_timeframes(pairs: List[str], days: int=14) -> Dict[str,Dict[str,pd.DataFrame]]:
    result = {}
    for pair in pairs:
        result[pair] = {}
        for tf,yfi in TIMEFRAMES.items():
            if tf in ("1m","5m"): period=f"{min(days,7)}d"
            elif tf=="15m": period=f"{days}d"
            elif tf=="1h": period=f"{days*2}d"
            elif tf=="4h": period=f"{days*5}d"
            else: period="60d"
            console.print(f"  {pair} {tf} ...",end=" ")
            df = fetch_pair_data(pair,period=period,interval=yfi)
            if not df.empty: result[pair][tf]=df; console.print(f"[green]✓[/green] {len(df)} bars")
            else: console.print("[red]✗[/red]")
    return result

# ═══════════════════════════════════════════════════════════════════════════════
#  VOLUME PROFILE ENGINE
# ═══════════════════════════════════════════════════════════════════════════════
class VolumeProfileEngine:
    def __init__(self, bin_size: float=None): self.bin_size = bin_size
    def compute_profile(self, df: pd.DataFrame, price_col: str="Close",
                        vol_col: str="Volume", start_idx: int=None, end_idx: int=None) -> Dict:
        if start_idx is not None and end_idx is not None: df = df.iloc[start_idx:end_idx+1]
        if df.empty: return {}
        prices=df[price_col].values; vols=df.get(vol_col,pd.Series(np.ones(len(prices)))).values
        pmin,pmax=prices.min(),prices.max()
        bsize=self.bin_size or max(1.0,(pmax-pmin)/200)
        bins=np.arange(pmin-bsize,pmax+bsize,bsize); profile=np.zeros(len(bins)-1)
        for i in range(len(prices)):
            idx=np.searchsorted(bins,prices[i])-1; idx=max(0,min(idx,len(profile)-1))
            profile[idx]+=vols[i] if isinstance(vols,np.ndarray) else 1
        total=profile.sum()
        if total==0: return {}
        poc_idx=np.argmax(profile); poc=(bins[poc_idx]+bins[poc_idx+1])/2
        sorted_dist=np.argsort(np.abs(np.arange(len(profile))-poc_idx))
        cum=0; va_idx=set()
        for ix in sorted_dist:
            if cum/total>=0.70: break
            va_idx.add(ix); cum+=profile[ix]
        va_prices=[(bins[i]+bins[i+1])/2 for i in va_idx]
        return {"poc":round(poc,2),"val":round(min(va_prices),2),"vah":round(max(va_prices),2),
                "total_volume":int(total),"bin_size":round(bsize,2),
                "value_area_width":round(max(va_prices)-min(va_prices),2)}
    def developing_profile(self,df: pd.DataFrame, lookback_bars: int=100) -> Dict:
        return self.compute_profile(df.tail(lookback_bars))
    def is_in_value_area(self,price: float,profile: Dict,tolerance: float=0.0) -> str:
        if not profile: return "unknown"
        val=profile["val"]; vah=profile["vah"]
        if price<val-tolerance: return "discount"
        if price>vah+tolerance: return "premium"
        return "value"
    def buy_sell_zones(self,profile: Dict) -> Dict:
        if not profile: return {}
        val=profile["val"]; vah=profile["vah"]; poc=profile["poc"]
        spread=(vah-val)
        return {"buy_zone_below_val":round(val-spread*0.5,2),"buy_zone_at_val":round(val,2),
                "buy_zone_discount":round(val-spread*0.3,2),
                "sell_zone_above_vah":round(vah+spread*0.5,2),"sell_zone_at_vah":round(vah,2),
                "sell_zone_premium":round(vah+spread*0.3,2),"poc":poc}

# ═══════════════════════════════════════════════════════════════════════════════
#  SMC ENGINE — Advanced concepts
# ═══════════════════════════════════════════════════════════════════════════════
class SMCEngine:
    def swing_points(self,df: pd.DataFrame,lb: int=5,lf: int=5) -> pd.DataFrame:
        df=df.copy(); df["sw_high"]=False; df["sw_low"]=False
        c=df["Close"].values
        for i in range(lb,len(c)-lf):
            w=np.concatenate([c[i-lb:i],[c[i]],c[i+1:i+lf+1]])
            if c[i]==w.max(): df.iloc[i,df.columns.get_loc("sw_high")]=True
            if c[i]==w.min(): df.iloc[i,df.columns.get_loc("sw_low")]=True
        return df
    def structure_bias(self,df: pd.DataFrame,price_col: str="Close") -> str:
        df=self.swing_points(df); valid=df[df["sw_high"]|df["sw_low"]].dropna(subset=["sw_high","sw_low"])
        if len(valid)<4: return "neutral"
        recent=valid.tail(10); highs=recent[recent["sw_high"]][price_col].values; lows=recent[recent["sw_low"]][price_col].values
        if len(highs)>=2 and len(lows)>=2:
            if highs[-1]>highs[-2] and lows[-1]>lows[-2]: return "bullish"
            if highs[-1]<highs[-2] and lows[-1]<lows[-2]: return "bearish"
        return "neutral"
    def structure_breaks(self,df: pd.DataFrame,price_col: str="Close") -> List[Dict]:
        df=self.swing_points(df); valid=df[df["sw_high"]|df["sw_low"]].dropna(subset=["sw_high","sw_low"])
        if len(valid)<4: return []
        events=[]; last_h=None; last_l=None; trend="neutral"
        for idx,row in valid.iterrows():
            pos=df.index.get_loc(idx)
            if row["sw_high"]:
                if last_h is not None and row[price_col]>last_h:
                    if trend=="bearish":
                        events.append({"type":"choch","direction":"bullish","price":row[price_col],"pos":pos,"dt":str(df.iloc[pos].name)}); trend="bullish"
                    elif trend=="neutral": trend="bullish"
                    events.append({"type":"bos","direction":"bullish","price":row[price_col],"pos":pos,"dt":str(df.iloc[pos].name)})
                last_h=row[price_col]
            if row["sw_low"]:
                if last_l is not None and row[price_col]<last_l:
                    if trend=="bullish":
                        events.append({"type":"choch","direction":"bearish","price":row[price_col],"pos":pos,"dt":str(df.iloc[pos].name)}); trend="bearish"
                    elif trend=="neutral": trend="bearish"
                    events.append({"type":"bos","direction":"bearish","price":row[price_col],"pos":pos,"dt":str(df.iloc[pos].name)})
                last_l=row[price_col]
        return events
    def market_state(self,df: pd.DataFrame,price_col: str="Close") -> MarketState:
        df=self.swing_points(df,lb=3,lf=3)
        if len(df)<20: return MarketState.RANGE
        recent=df.tail(20); highs=recent["High"].values; lows=recent["Low"].values; closes=recent[price_col].values
        tr=np.zeros(len(recent)-1); p_dm=np.zeros(len(recent)-1); m_dm=np.zeros(len(recent)-1)
        for i in range(1,len(recent)):
            tr[i-1]=max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1]))
            p_dm[i-1]=max(0,highs[i]-highs[i-1]) if highs[i]-highs[i-1]>lows[i-1]-lows[i] else 0
            m_dm[i-1]=max(0,lows[i-1]-lows[i]) if lows[i-1]-lows[i]>highs[i]-highs[i-1] else 0
        avg_tr=np.mean(tr) if len(tr)>0 else 1
        if avg_tr==0: return MarketState.RANGE
        p_di=100*np.mean(p_dm)/avg_tr; m_di=100*np.mean(m_dm)/avg_tr; adx=abs(p_di-m_di)
        if adx>25: return MarketState.TREND_BULL if p_di>m_di else MarketState.TREND_BEAR
        prev_high=recent["High"].iloc[:-1].max(); prev_low=recent["Low"].iloc[:-1].min(); current=closes[-1]
        if current>prev_high*1.001: return MarketState.BREAKOUT_UP
        if current<prev_low*0.999: return MarketState.BREAKOUT_DOWN
        if adx<15: return MarketState.CHOPPY
        return MarketState.RANGE
    def fvgs(self,df: pd.DataFrame,min_size_pct: float=0.0001) -> List[Dict]:
        fvgs=[]; h=df["High"].values; l=df["Low"].values
        for i in range(1,len(df)-1):
            if h[i-1]<l[i+1]:
                gs,ge=h[i-1],l[i+1]
                if ge-gs>min_size_pct*df["Close"].iloc[i]:
                    fvgs.append({"type":"bullish_fvg","start":gs,"end":ge,"mid":(gs+ge)/2,"pos":i,"dt":str(df.iloc[i].name),"size":round(ge-gs,4),"size_cents":round((ge-gs)*100,2)})
            if l[i-1]>h[i+1]:
                gs,ge=h[i+1],l[i-1]
                if ge-gs>min_size_pct*df["Close"].iloc[i]:
                    fvgs.append({"type":"bearish_fvg","start":gs,"end":ge,"mid":(gs+ge)/2,"pos":i,"dt":str(df.iloc[i].name),"size":round(ge-gs,4),"size_cents":round((ge-gs)*100,2)})
        return fvgs
    def ifvgs(self,df: pd.DataFrame) -> List[Dict]:
        all_f=self.fvgs(df); ifvg_list=[]
        for f in all_f:
            subset=df.iloc[f["pos"]:]
            if len(subset)==0: continue
            if f["type"]=="bullish_fvg":
                if (subset["Low"]<f["start"]).any():
                    ifvg_list.append({**f,"status":"invalidated","invalidated_at":str(subset[subset["Low"]<f["start"]].index[0])})
            else:
                if (subset["High"]>f["end"]).any():
                    ifvg_list.append({**f,"status":"invalidated","invalidated_at":str(subset[subset["High"]>f["end"]].index[0])})
        return ifvg_list
    def order_blocks(self,df: pd.DataFrame,lookback: int=3) -> List[Dict]:
        obs=[]; bodies=abs(df["Close"]-df["Open"]).values; ranges=(df["High"]-df["Low"]).values
        closes=df["Close"].values; opens=df["Open"].values
        for i in range(lookback,len(df)-lookback):
            fm=closes[i+lookback]-closes[i]; fr=df["High"].iloc[i:i+lookback].max()-df["Low"].iloc[i:i+lookback].min()
            if abs(fm)>bodies[i]*2 and fr>0:
                ob=df.iloc[i]; ob_body=abs(closes[i]-opens[i]); ob_range=ranges[i]
                if fm>0 and ob_body>ob_range*0.3:
                    obs.append({"type":"bullish_ob","zone_low":ob["Low"],"zone_high":ob["High"],
                                "body_low":min(opens[i],closes[i]),"body_high":max(opens[i],closes[i]),
                                "pos":i,"dt":str(df.iloc[i].name),"strength":round(abs(fm)/bodies[i],2)})
                elif fm<0 and ob_body>ob_range*0.3:
                    obs.append({"type":"bearish_ob","zone_low":ob["Low"],"zone_high":ob["High"],
                                "body_low":min(opens[i],closes[i]),"body_high":max(opens[i],closes[i]),
                                "pos":i,"dt":str(df.iloc[i].name),"strength":round(abs(fm)/bodies[i],2)})
        obs.sort(key=lambda x:x["strength"],reverse=True); return self._dedupe(obs)
    def invalid_order_blocks(self,df: pd.DataFrame) -> List[Dict]:
        all_obs=self.order_blocks(df); inv=[]
        for ob in all_obs:
            subset=df.iloc[ob["pos"]:]
            if len(subset)==0: continue
            if ob["type"]=="bullish_ob":
                if (subset["Low"]<ob["zone_low"]).any():
                    inv.append({**ob,"status":"invalidated","invalidated_at":str(subset[subset["Low"]<ob["zone_low"]].index[0])})
            else:
                if (subset["High"]>ob["zone_high"]).any():
                    inv.append({**ob,"status":"invalidated","invalidated_at":str(subset[subset["High"]>ob["zone_high"]].index[0])})
        return inv
    def _dedupe(self,zones: List[Dict],tol: float=0.005) -> List[Dict]:
        if not zones: return []
        kept=[]
        for z in zones:
            overlap=False
            for k in kept:
                if z["type"]==k["type"] and abs(z["zone_low"]-k["zone_low"])/max(k["zone_high"]-k["zone_low"],0.0001)<tol:
                    overlap=True; break
            if not overlap: kept.append(z)
        return kept
    def liquidity_analysis(self,df: pd.DataFrame,lookback: int=20) -> Dict:
        df=self.swing_points(df,lb=lookback,lf=lookback); recent=df.tail(lookback)
        highs=recent[recent["sw_high"]]["High"].values; lows=recent[recent["sw_low"]]["Low"].values
        closes=recent["Close"].values; sweep_h=[]; sweep_l=[]; run_h=[]; run_l=[]
        for i in range(1,len(recent)):
            for h in highs[:-1]:
                if recent["High"].iloc[i]>h and closes[i]<h:
                    sub=recent["Close"].iloc[i:min(i+5,len(recent))]
                    if len(sub)>0 and (sub<h).all():
                        sweep_h.append({"level":round(float(h),2),"wick":round(float(recent["High"].iloc[i]),2),
                                        "close":round(float(closes[i]),2),"type":"sweep","reversal_confirmed":True})
                    else:
                        run_h.append({"level":round(float(h),2),"wick":round(float(recent["High"].iloc[i]),2),
                                      "close":round(float(closes[i]),2),"type":"run","breakout":True})
                    break
            for l in lows[:-1]:
                if recent["Low"].iloc[i]<l and closes[i]>l:
                    sub=recent["Close"].iloc[i:min(i+5,len(recent))]
                    if len(sub)>0 and (sub>l).all():
                        sweep_l.append({"level":round(float(l),2),"wick":round(float(recent["Low"].iloc[i]),2),
                                        "close":round(float(closes[i]),2),"type":"sweep","reversal_confirmed":True})
                    else:
                        run_l.append({"level":round(float(l),2),"wick":round(float(recent["Low"].iloc[i]),2),
                                      "close":round(float(closes[i]),2),"type":"run","breakout":True})
                    break
        return {"sweep_highs":sweep_h,"sweep_lows":sweep_l,"run_highs":run_h,"run_lows":run_l,
                "equal_highs":self._eq_levels(highs),"equal_lows":self._eq_levels(lows),
                "current_price":round(float(df["Close"].iloc[-1]),2)}
    def _eq_levels(self,levels: np.ndarray,tol: float=2.0) -> List[float]:
        if len(levels)<2: return []
        clusters=[]
        for lv in levels:
            cl=[x for x in levels if abs(x-lv)<tol]
            if len(cl)>=2: clusters.append(round(np.mean(cl),2))
        return sorted(set(clusters),reverse=True)
    def failed_auctions(self,df: pd.DataFrame,lookback: int=30) -> List[Dict]:
        df=self.swing_points(df,lb=lookback,lf=lookback); fa=[]
        for i in range(lookback,len(df)):
            window=df.iloc[max(0,i-lookback):i+1]
            if len(window)<10: continue
            prev_high=window["High"].iloc[:-1].max(); prev_low=window["Low"].iloc[:-1].min()
            ch=df["High"].iloc[i]; cl=df["Low"].iloc[i]
            if ch>prev_high*1.002 and df["Close"].iloc[i]<prev_high and i+2<len(df):
                if df["High"].iloc[i+1]<df["High"].iloc[i] and df["Low"].iloc[i]>df["Low"].iloc[i+2]:
                    fa.append({"type":"failed_auction_up","price":round(float(df["Close"].iloc[i]),2),
                               "level":round(float(prev_high),2),"pos":i,"dt":str(df.iloc[i].name),"signal":"sell"})
            if cl<prev_low*0.998 and df["Close"].iloc[i]>prev_low and i+2<len(df):
                if df["Low"].iloc[i+1]>df["Low"].iloc[i] and df["High"].iloc[i]<df["High"].iloc[i+2]:
                    fa.append({"type":"failed_auction_down","price":round(float(df["Close"].iloc[i]),2),
                               "level":round(float(prev_low),2),"pos":i,"dt":str(df.iloc[i].name),"signal":"buy"})
        return fa[-10:]
    def breakouts(self,df: pd.DataFrame,lookback: int=20) -> List[Dict]:
        bo=[]; 
        for i in range(lookback,len(df)):
            window=df.iloc[i-lookback:i]; ph=window["High"].max(); pl=window["Low"].min()
            if df["Close"].iloc[i]>ph and df["High"].iloc[i]>ph:
                vol_confirmed = "Volume" in df.columns and df["Volume"].iloc[i]>df["Volume"].iloc[i-1] if "Volume" in df.columns else True
                bo.append({"type":"breakout_up","level":round(float(ph),2),"price":round(float(df["Close"].iloc[i]),2),
                           "pos":i,"dt":str(df.iloc[i].name),"volume_confirmed":vol_confirmed})
            if df["Close"].iloc[i]<pl and df["Low"].iloc[i]<pl:
                vol_confirmed = "Volume" in df.columns and df["Volume"].iloc[i]>df["Volume"].iloc[i-1] if "Volume" in df.columns else True
                bo.append({"type":"breakout_down","level":round(float(pl),2),"price":round(float(df["Close"].iloc[i]),2),
                           "pos":i,"dt":str(df.iloc[i].name),"volume_confirmed":vol_confirmed})
        return bo[-10:]
    def absorption(self,df: pd.DataFrame,lookback: int=20) -> Dict:
        if len(df)<lookback+5: return {"seller_absorption":False,"buyer_absorption":False,"strength":0}
        recent=df.tail(lookback); vols=recent.get("Volume",pd.Series(np.ones(len(recent)))).values
        moves=np.diff(recent["Close"].values); vm=np.abs(moves)*vols[:-1]
        threshold=np.percentile(vm,80) if len(vm)>0 else 0
        bs=0; ss=0
        for i in range(len(moves)):
            if vm[i]>=threshold and abs(moves[i])>0:
                if i+2<len(moves):
                    if moves[i]<0 and moves[i+1]>0 and moves[i+2]>0: bs+=min(vm[i]/threshold,3)
                    elif moves[i]>0 and moves[i+1]<0 and moves[i+2]<0: ss+=min(vm[i]/threshold,3)
        tot=bs+ss
        return {"seller_absorption":ss>0,"buyer_absorption":bs>0,
                "buyer_strength":round(bs,2),"seller_strength":round(ss,2),
                "strength":round(max(bs,ss)/max(tot,1),2),
                "dominant_side":"buyers" if bs>ss else "sellers" if ss>bs else "none"}

# ═══════════════════════════════════════════════════════════════════════════════
#  SELF-EVOLVING SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════
class SelfEvolve:
    def __init__(self,state: Dict):
        self.state=state; self.tech_stats=self._load_tech_stats()
    def _load_tech_stats(self) -> Dict[str,TechniqueStats]:
        raw=self.state.get("technique_stats",{}); result={}
        for tn,td in raw.items():
            try:
                result[tn]=TechniqueStats(technique=Technique(tn),trades=td.get("trades",0),
                    wins=td.get("wins",0),losses=td.get("losses",0),total_pnl=td.get("total_pnl",0.0),
                    avg_win=td.get("avg_win",0.0),avg_loss=td.get("avg_loss",0.0),
                    win_rate=td.get("win_rate",0.0),avg_rr=td.get("avg_rr",0.0),sharpe=td.get("sharpe",0.0),
                    max_drawdown=td.get("max_drawdown",0.0),consec_wins=td.get("consec_wins",0),
                    max_consec_wins=td.get("max_consec_wins",0),consec_losses=td.get("consec_losses",0),
                    max_consec_losses=td.get("max_consec_losses",0))
            except: pass
        return result
    def record_trade(self,technique: Technique,pnl: float,rr: float,won: bool):
        tn=technique.value
        if tn not in self.tech_stats: self.tech_stats[tn]=TechniqueStats(technique=technique)
        ts=self.tech_stats[tn]; ts.trades+=1
        if won:
            ts.wins+=1; ts.total_pnl+=pnl
            ts.avg_win=(ts.avg_win*(ts.wins-1)+pnl)/ts.wins if ts.wins>0 else pnl
            ts.consec_wins+=1; ts.consec_losses=0
            ts.max_consec_wins=max(ts.max_consec_wins,ts.consec_wins)
        else:
            ts.losses+=1; ts.total_pnl+=pnl
            ts.avg_loss=(ts.avg_loss*(ts.losses-1)+abs(pnl))/ts.losses if ts.losses>0 else abs(pnl)
            ts.consec_losses+=1; ts.consec_wins=0
            ts.max_consec_losses=max(ts.max_consec_losses,ts.consec_losses)
        ts.win_rate=ts.wins/ts.trades if ts.trades>0 else 0
        ts.avg_rr=(ts.avg_rr*(ts.trades-1)+rr)/ts.trades if ts.trades>0 else rr
        ts.last_updated=datetime.now(timezone.utc)
        self.state["technique_stats"][tn]=asdict(ts); save_state(self.state)
    def evolve(self,strategy_cfg: StrategyConfig) -> StrategyConfig:
        if not self.state.get("self_evolve",True): return strategy_cfg
        evolved=copy.deepcopy(strategy_cfg)
        target_sharpe=0.5
        for tn,ts in self.tech_stats.items():
            if ts.trades<10: continue
            technique=Technique(tn); old_w=getattr(evolved,f"weight_{tn}",1.0)
            if ts.sharpe>target_sharpe and ts.win_rate>0.45:
                new_w=min(old_w*1.1,2.0)
            elif ts.sharpe<0.2 or ts.win_rate<0.35:
                new_w=max(old_w*0.9,0.3)
            else: new_w=old_w
            setattr(evolved,f"weight_{tn}",round(new_w,2))
        if self.state.get("consecutive_wins",0)>=3:
            evolved.stop_loss_tightness=max(evolved.stop_loss_tightness-0.1,0.5)
        if self.state.get("consecutive_losses",0)>=3:
            evolved.stop_loss_tightness=min(evolved.stop_loss_tightness+0.1,1.5)
        return evolved
    def compute_sharpe(self,returns: List[float]) -> float:
        if len(returns)<2: return 0.0
        if np.std(returns)==0: return 0.0
        return np.mean(returns)/np.std(returns)*math.sqrt(252)
    def update_strategy_stats(self,strategy_name: str,pnl_history: List[float]):
        if strategy_name not in self.state.get("strategy_performance",{}):
            self.state.setdefault("strategy_performance",{})[strategy_name]={
                "pnl_history":[],"sharpe":0.0,"max_drawdown":0.0,"win_rate":0.0,
                "avg_win":0.0,"avg_loss":0.0,"consec_wins":0,"max_consec_wins":0,
                "consec_losses":0,"max_consec_losses":0,"total_trades":0,"total_pnl":0.0}
        sp=self.state["strategy_performance"][strategy_name]
        sp["pnl_history"]=(sp.get("pnl_history",[])+pnl_history)[-100:]
        rets=[r for r in sp["pnl_history"] if r!=0]
        sp["sharpe"]=self.compute_sharpe(rets) if len(rets)>1 else 0.0
        sp["total_trades"]=len(rets); sp["total_pnl"]=sum(sp["pnl_history"])
        wins=[r for r in rets if r>0]; losses=[abs(r) for r in rets if r<0]
        sp["win_rate"]=len(wins)/len(rets) if rets else 0
        sp["avg_win"]=mean(wins) if wins else 0; sp["avg_loss"]=mean(losses) if losses else 0
        # Track max drawdown
        peak=0; dd=0
        for r in sp["pnl_history"]:
            peak=max(peak,r); dd=max(dd,peak-r)
        sp["max_drawdown"]=dd
        save_state(self.state)

# ═══════════════════════════════════════════════════════════════════════════════
#  RISK ENGINE
# ═══════════════════════════════════════════════════════════════════════════════
class RiskEngine:
    def __init__(self,config: BotConfig,state: Dict): self.config=config; self.state=state
    def evaluate(self,signal: TradeSignal,current_price: float) -> Dict:
        s=self.config.slippage; sb=self.config.spread_buffer; cm=self.config.commission_per_lot
        min_rr=self.config.min_rr_after_costs; is_cents=self.config.account_type=="cents"
        costs_per_unit=s+sb+cm
        if signal.side==Side.BUY:
            eff_entry=signal.entry_price+costs_per_unit/100; eff_sl=signal.stop_loss; eff_tp=signal.take_profit
        else:
            eff_entry=signal.entry_price-costs_per_unit/100; eff_sl=signal.stop_loss; eff_tp=signal.take_profit
        risk_dist=abs(eff_entry-eff_sl); reward_dist=abs(eff_tp-eff_entry)
        risk_amt=risk_dist*(100 if is_cents else 1); reward_amt=reward_dist*(100 if is_cents else 1)
        target_risk=self.config.strategies[0].risk_per_trade if self.config.strategies else 10.0
        lot_mult=target_risk/risk_amt if risk_amt>0 else 1.0
        comm_cost=cm*lot_mult; slip_cost=s*lot_mult; spread_cost=sb*lot_mult
        total_costs=comm_cost+slip_cost+spread_cost
        rr_after=(reward_amt-total_costs)/(risk_amt+total_costs) if (risk_amt+total_costs)>0 else 0
        eligible=rr_after>=min_rr and self.state.get("daily_loss",0)<self.config.max_daily_loss
        return {"eligible":eligible,"signal":signal.side.value,"pair":signal.pair,
                "entry":round(eff_entry,2),"sl":round(eff_sl,2),"tp":round(eff_tp,2),
                "risk":round(risk_amt*lot_mult,2),"reward":round(reward_amt*lot_mult,2),
                "rr_after_costs":round(rr_after,2),"costs":round(total_costs,2),
                "lot_mult":round(lot_mult,4),
                "reason":"PASS" if eligible else f"FAIL: R:R {rr_after:.1f}<{min_rr}" if rr_after<min_rr else "FAIL: daily loss limit"}
    def check_daily_limit(self) -> bool:
        return self.state.get("daily_loss",0)>=self.config.max_daily_loss
    def update_pnl(self,pnl: float,technique: Technique="",rr: float=0.0):
        self.state["daily_pnl"]=self.state.get("daily_pnl",0)+pnl
        if pnl<0:
            self.state["daily_loss"]=self.state.get("daily_loss",0)+abs(pnl)
            self.state["consecutive_losses"]=self.state.get("consecutive_losses",0)+1
            self.state["consecutive_wins"]=0
        else:
            self.state["consecutive_wins"]=self.state.get("consecutive_wins",0)+1
            self.state["consecutive_losses"]=0
        self.state["max_consecutive_wins"]=max(self.state.get("max_consecutive_wins",0),self.state["consecutive_wins"])
        self.state["max_consecutive_losses"]=max(self.state.get("max_consecutive_losses",0),self.state["consecutive_losses"])
        self.state["current_balance"]=self.state.get("current_balance",self.config.start_balance)+pnl
        now=datetime.now(timezone.utc); wk=now.strftime("%Y-W%W")
        if wk!=self.state.get("weekly_offset",""):
            self.state["weekly_pnl"]={}; self.state["weekly_offset"]=wk
        self.state["weekly_pnl"][wk]=self.state["weekly_pnl"].get(wk,0)+pnl
        # Record technique stats
        if technique:
            ev=SelfEvolve(self.state); ev.record_trade(technique,pnl,rr,pnl>0)
        # Update strategy stats — find which strategy this trade belongs to
        save_state(self.state)

# ═══════════════════════════════════════════════════════════════════════════════
#  STRATEGY GENERATOR
# ═══════════════════════════════════════════════════════════════════════════════
class StrategyGenerator:
    def __init__(self,config: BotConfig,evolve: SelfEvolve,risk: RiskEngine):
        self.config=config; self.evolve=evolve; self.risk=risk
        self.smc=SMCEngine(); self.vp=VolumeProfileEngine()
    def generate(self,pair: str,data: Dict[str,pd.DataFrame],vp_profiles: Dict[str,Dict],
                 news_events: List[Dict]) -> List[TradeSignal]:
        signals=[]
        for sc in self.config.strategies:
            sigs=self._gen_for_strategy(pair,data,vp_profiles,sc,news_events)
            signals.extend(sigs)
        return signals
    def _gen_for_strategy(self,pair: str,data: Dict,vp_profiles: Dict,strat: StrategyConfig,news: List) -> List[TradeSignal]:
        signals=[]; is_cents=self.config.account_type=="cents"
        risk_unit=100 if is_cents else 1
        now_hour=datetime.now().hour+3
        if self.config.london_session_only:
            if not (LONDON_OPEN_EAT<=now_hour<=LONDON_CLOSE_EAT or NY_OPEN_EAT<=now_hour<=NY_CLOSE_EAT):
                return signals
        highest_tf=strat.active_timeframes[-1] if strat.active_timeframes else "1h"
        ms_df=data.get(highest_tf,pd.DataFrame())
        mstate=self.smc.market_state(ms_df) if not ms_df.empty else MarketState.RANGE
        sbias=self.smc.structure_bias(ms_df) if not ms_df.empty else "neutral"
        entry_tf=strat.active_timeframes[0] if strat.active_timeframes else "15m"
        entry_df=data.get(entry_tf,pd.DataFrame())
        if entry_df.empty: return signals
        cp=float(entry_df["Close"].iloc[-1])
        profile=vp_profiles.get(entry_tf,{}); zones=self.vp.buy_sell_zones(profile) if profile else {}
        techs=self._detect_all(pair,entry_df,data,strat,profile,zones,mstate,sbias)
        scored=self._score(techs,strat,mstate,sbias)
        for tech in scored[:strat.max_concurrent]:
            if tech["score"]<strat.min_confluence: break
            sig=self._build(pair,tech,entry_df,strat,profile,zones,is_cents,risk_unit,news)
            if sig: signals.append(sig)
        return signals
    def _detect_all(self,pair: str,entry_df: pd.DataFrame,data: Dict,strat: StrategyConfig,
                    profile: Dict,zones: Dict,mstate: MarketState,sbias: str) -> List[Dict]:
        techs=[]; smc=self.smc
        # 1. Market Structure
        if strat.weight_market_structure>0:
            breaks=smc.structure_breaks(entry_df); recent_b=breaks[-3:]
            if recent_b:
                direction="buy" if any(b["direction"]=="bullish" for b in recent_b) else "sell"
                techs.append({"technique":Technique.MARKET_STRUCTURE,"direction":direction,
                    "score":int(strat.weight_market_structure*10),
                    "details":f"BOS/CHoCH: {len(recent_b)} breaks, trend={sbias}"})
        # 2. Volume Profile — BUY at VAL / SELL at VAH (core strategy)
        if strat.use_volume_profile and profile and strat.weight_volume_profile>0:
            vp_state=self.vp.is_in_value_area(entry_df["Close"].iloc[-1],profile)
            if vp_state=="discount" and sbias=="bullish":
                techs.append({"technique":Technique.VOLUME_PROFILE,"direction":"buy",
                    "score":int(strat.weight_volume_profile*8),
                    "details":f"Discount zone (VAL={profile['val']}, VAH={profile['vah']}) — BUY at VAL"})
            elif vp_state=="premium" and sbias=="bearish":
                techs.append({"technique":Technique.VOLUME_PROFILE,"direction":"sell",
                    "score":int(strat.weight_volume_profile*8),
                    "details":f"Premium zone (VAL={profile['val']}, VAH={profile['vah']}) — SELL at VAH"})
            elif vp_state=="value":
                techs.append({"technique":Technique.VOLUME_PROFILE,"direction":"buy" if sbias=="bullish" else "sell",
                    "score":int(strat.weight_volume_profile*4),
                    "details":f"At value area (POC={profile['poc']})"})
        # 3. Absorption
        if strat.use_absorption and strat.weight_absorption>0:
            abs_sig=smc.absorption(entry_df)
            if abs_sig["buyer_absorption"] and sbias=="bullish":
                techs.append({"technique":Technique.ABSORPTION,"direction":"buy",
                    "score":int(abs_sig["buyer_strength"]*strat.weight_absorption*5),
                    "details":f"Buyer absorption (strength={abs_sig['buyer_strength']}) — sellers absorbed"})
            if abs_sig["seller_absorption"] and sbias=="bearish":
                techs.append({"technique":Technique.ABSORPTION,"direction":"sell",
                    "score":int(abs_sig["seller_strength"]*strat.weight_absorption*5),
                    "details":f"Seller absorption (strength={abs_sig['seller_strength']}) — buyers absorbed"})
        # 4. FVGs
        if strat.use_fvg and strat.weight_fvg>0:
            fvg_list=smc.fvgs(entry_df); recent_f=[f for f in fvg_list if f["pos"]>=len(entry_df)-30]
            for f in recent_f[-2:]:
                if f["type"]=="bullish_fvg" and sbias=="bullish":
                    techs.append({"technique":Technique.FVG,"direction":"buy","score":int(strat.weight_fvg*5),
                        "details":f"Bullish FVG [{f['start']:.2f}→{f['end']:.2f}] size={f['size']}"})
                if f["type"]=="bearish_fvg" and sbias=="bearish":
                    techs.append({"technique":Technique.FVG,"direction":"sell","score":int(strat.weight_fvg*5),
                        "details":f"Bearish FVG [{f['start']:.2f}→{f['end']:.2f}] size={f['size']}"})
        # 5. iFVGs
        if strat.use_ifvg and strat.weight_ifvg>0:
            ifvg_list=smc.ifvgs(entry_df)
            for iv in ifvg_list[-2:]:
                if iv["type"]=="bullish_fvg" and iv["status"]=="invalidated" and sbias=="bullish":
                    techs.append({"technique":Technique.IFVG,"direction":"buy","score":int(strat.weight_ifvg*6),
                        "details":f"iFVG support at {iv['start']:.2f} (filled bullish FVG)"})
                if iv["type"]=="bearish_fvg" and iv["status"]=="invalidated" and sbias=="bearish":
                    techs.append({"technique":Technique.IFVG,"direction":"sell","score":int(strat.weight_ifvg*6),
                        "details":f"iFVG resistance at {iv['end']:.2f} (filled bearish FVG)"})
        # 6. Invalid OBs
        if strat.use_invalid_ob and strat.weight_invalid_ob>0:
            inv_ob_list=smc.invalid_order_blocks(entry_df)
            for io in inv_ob_list[-2:]:
                if io["type"]=="bullish_ob" and io["status"]=="invalidated" and sbias=="bullish":
                    techs.append({"technique":Technique.INVALID_OB,"direction":"buy","score":int(strat.weight_invalid_ob*6),
                        "details":f"Invalid OB support at {io['zone_low']:.2f}"})
                if io["type"]=="bearish_ob" and io["status"]=="invalidated" and sbias=="bearish":
                    techs.append({"technique":Technique.INVALID_OB,"direction":"sell","score":int(strat.weight_invalid_ob*6),
                        "details":f"Invalid OB resistance at {io['zone_high']:.2f}"})
        # 7. Order Blocks (valid)
        if strat.use_order_blocks and strat.weight_order_block>0:
            ob_list=smc.order_blocks(entry_df)
            for ob in ob_list[:2]:
                if ob["type"]=="bullish_ob" and sbias=="bullish":
                    techs.append({"technique":Technique.ORDER_BLOCK,"direction":"buy",
                        "score":int(strat.weight_order_block*5*ob["strength"]/10),
                        "details":f"Bullish OB [{ob['zone_low']:.2f}→{ob['zone_high']:.2f}] strength={ob['strength']}"})
                if ob["type"]=="bearish_ob" and sbias=="bearish":
                    techs.append({"technique":Technique.ORDER_BLOCK,"direction":"sell",
                        "score":int(strat.weight_order_block*5*ob["strength"]/10),
                        "details":f"Bearish OB [{ob['zone_low']:.2f}→{ob['zone_high']:.2f}] strength={ob['strength']}"})
        # 8. Liquidity Sweep
        if strat.use_liquidity_sweep and strat.weight_liquidity_sweep>0:
            liq=smc.liquidity_analysis(entry_df)
            for sh in liq["sweep_highs"][-2:]:
                if sbias=="bearish":
                    techs.append({"technique":Technique.LIQ_SWEEP,"direction":"sell","score":int(strat.weight_liquidity_sweep*7),
                        "details":f"Liquidity sweep above {sh['level']} (wick={sh['wick']}, closed back)"})
            for sl in liq["sweep_lows"][-2:]:
                if sbias=="bullish":
                    techs.append({"technique":Technique.LIQ_SWEEP,"direction":"buy","score":int(strat.weight_liquidity_sweep*7),
                        "details":f"Liquidity sweep below {sl['level']} (wick={sl['wick']}, closed back)"})
        # 9. Liquidity Run
        if strat.use_liquidity_run and strat.weight_liquidity_run>0:
            liq=smc.liquidity_analysis(entry_df)
            for rh in liq["run_highs"][-1:]:
                if sbias=="bullish":
                    techs.append({"technique":Technique.LIQ_RUN,"direction":"buy","score":int(strat.weight_liquidity_run*6),
                        "details":f"Liquidity run above {rh['level']} (breakout confirmed)"})
            for rl in liq["run_lows"][-1:]:
                if sbias=="bearish":
                    techs.append({"technique":Technique.LIQ_RUN,"direction":"sell","score":int(strat.weight_liquidity_run*6),
                        "details":f"Liquidity run below {rl['level']} (breakout confirmed)"})
        # 10. Failed Auction
        if strat.use_failed_auction and strat.weight_failed_auction>0:
            fa_list=smc.failed_auctions(entry_df)
            for fa in fa_list[-2:]:
                if fa["signal"]=="buy" and sbias=="bullish":
                    techs.append({"technique":Technique.FAILED_AUCTION,"direction":"buy","score":int(strat.weight_failed_auction*7),
                        "details":f"Failed auction down at {fa['level']} (rejected)"})
                if fa["signal"]=="sell" and sbias=="bearish":
                    techs.append({"technique":Technique.FAILED_AUCTION,"direction":"sell","score":int(strat.weight_failed_auction*7),
                        "details":f"Failed auction up at {fa['level']} (rejected)"})
        # 11. Breakout
        if strat.use_breakout and strat.weight_breakout>0:
            bo_list=smc.breakouts(entry_df)
            for bo in bo_list[-2:]:
                if bo["type"]=="breakout_up" and mstate in (MarketState.BREAKOUT_UP,MarketState.TREND_BULL):
                    techs.append({"technique":Technique.BREAKOUT,"direction":"buy","score":int(strat.weight_breakout*6),
                        "details":f"Breakout above {bo['level']} at {bo['price']}"})
                if bo["type"]=="breakout_down" and mstate in (MarketState.BREAKOUT_DOWN,MarketState.TREND_BEAR):
                    techs.append({"technique":Technique.BREAKOUT,"direction":"sell","score":int(strat.weight_breakout*6),
                        "details":f"Breakout below {bo['level']} at {bo['price']}"})
        # ── Rules: when NOT to use OBs / FVGs ──
        # If market is choppy, reduce confidence in OB/FVG
        if mstate==MarketState.CHOPPY:
            for t in techs:
                if t["technique"] in (Technique.ORDER_BLOCK,Technique.FVG,Technique.IFVG):
                    t["score"]=int(t["score"]*0.5)
        # If no clear structure bias, reduce all technique scores
        if sbias=="neutral":
            for t in techs:
                if t["technique"] not in (Technique.LIQ_SWEEP,Technique.LIQ_RUN):
                    t["score"]=int(t["score"]*0.6)
        return techs
    def _score(self,techs: List[Dict],strat: StrategyConfig,mstate: MarketState,sbias: str) -> List[Dict]:
        scored=[]
        for tech in techs:
            direction=tech["direction"]; base=tech["score"]
            if sbias=="bullish" and direction=="sell": base=int(base*0.5)
            elif sbias=="bearish" and direction=="buy": base=int(base*0.5)
            same_dir=sum(1 for t in techs if t["direction"]==direction)
            base+=same_dir*2
            if mstate==MarketState.TREND_BULL and direction=="buy": base+=3
            elif mstate==MarketState.TREND_BEAR and direction=="sell": base+=3
            elif mstate==MarketState.CHOPPY: base=int(base*0.6)
            scored.append({**tech,"final_score":base,"confidence":min(base/20,1.0)})
        scored.sort(key=lambda x:x["final_score"],reverse=True); return scored
    def _build(self,pair: str,tech: Dict,entry_df: pd.DataFrame,strat: StrategyConfig,
                profile: Dict,zones: Dict,is_cents: bool,risk_unit: float,news: List) -> Optional[TradeSignal]:
        direction=tech["direction"]; side=Side.BUY if direction=="buy" else Side.SELL
        cp=float(entry_df["Close"].iloc[-1]); tf=strat.active_timeframes[0]
        entry=cp; sl=cp; tp=cp
        t=tech["technique"]
        if t==Technique.MARKET_STRUCTURE:
            breaks=self.smc.structure_breaks(entry_df); recent=breaks[-2:]
            if not recent: return None
            if side==Side.BUY:
                entry=cp; lows=entry_df["Low"].tail(10).min()
                sl=lows-(self.config.spread_buffer+self.config.slippage)/100*strat.stop_loss_tightness
                tp=entry+(abs(entry-sl)*strat.rr_target)
            else:
                entry=cp; highs=entry_df["High"].tail(10).max()
                sl=highs+(self.config.spread_buffer+self.config.slippage)/100*strat.stop_loss_tightness
                tp=entry-(abs(entry-sl)*strat.rr_target)
        elif t==Technique.VOLUME_PROFILE:
            if not zones: return None
            if side==Side.BUY:
                entry=cp; sl=zones.get("buy_zone_below_val",cp*0.995)-(self.config.spread_buffer+self.config.slippage)/100
                tp=zones.get("sell_zone_at_vah",cp*1.01)
            else:
                entry=cp; sl=zones.get("sell_zone_above_vah",cp*1.005)+(self.config.spread_buffer+self.config.slippage)/100
                tp=zones.get("buy_zone_at_val",cp*0.99)
        elif t in (Technique.FVG,Technique.IFVG):
            all_f=self.smc.fvgs(entry_df)
            recent_f=[f for f in all_f[-10:] if f["type"]==("bullish_fvg" if side==Side.BUY else "bearish_fvg")]
            if not recent_f: return None
            fvg=recent_f[-1]
            if side==Side.BUY:
                entry=max(cp,fvg["start"]); sl=fvg["start"]-(self.config.spread_buffer+self.config.slippage)/100*strat.stop_loss_tightness
                tp=fvg["end"]+abs(fvg["end"]-fvg["start"])*1.5
            else:
                entry=min(cp,fvg["end"]); sl=fvg["end"]+(self.config.spread_buffer+self.config.slippage)/100*strat.stop_loss_tightness
                tp=fvg["start"]-abs(fvg["end"]-fvg["start"])*1.5
        elif t in (Technique.ORDER_BLOCK,Technique.INVALID_OB):
            all_obs=self.smc.order_blocks(entry_df)
            recent_obs=[o for o in all_obs if o["type"]==("bullish_ob" if side==Side.BUY else "bearish_ob")]
            if not recent_obs: return None
            ob=recent_obs[-1]
            if side==Side.BUY:
                entry=min(cp,ob["zone_high"]); sl=ob["zone_low"]-(self.config.spread_buffer+self.config.slippage)/100*strat.stop_loss_tightness
                tp=ob["zone_high"]+abs(ob["zone_high"]-ob["zone_low"])*1.5
            else:
                entry=max(cp,ob["zone_low"]); sl=ob["zone_high"]+(self.config.spread_buffer+self.config.slippage)/100*strat.stop_loss_tightness
                tp=ob["zone_low"]-abs(ob["zone_high"]-ob["zone_low"])*1.5
        elif t==Technique.ABSORPTION:
            entry=cp
            if side==Side.BUY:
                sl=entry_df["Low"].tail(5).min()-(self.config.spread_buffer+self.config.slippage)/100*strat.stop_loss_tightness
                tp=entry+(abs(entry-sl)*strat.rr_target)
            else:
                sl=entry_df["High"].tail(5).max()+(self.config.spread_buffer+self.config.slippage)/100*strat.stop_loss_tightness
                tp=entry-(abs(entry-sl)*strat.rr_target)
        elif t==Technique.LIQ_SWEEP:
            liq=self.smc.liquidity_analysis(entry_df)
            sw=liq.get(f"{'sweep_highs' if side==Side.SELL else 'sweep_lows'}",[])
            if not sw: return None; sw=sw[-1]
            if side==Side.BUY:
                entry=cp; sl=sw["wick"]-(self.config.spread_buffer+self.config.slippage)/100
                tp=sw["level"]+abs(sw["level"]-sw["close"])*2
            else:
                entry=cp; sl=sw["wick"]+(self.config.spread_buffer+self.config.slippage)/100
                tp=sw["level"]-abs(sw["level"]-sw["close"])*2
        elif t==Technique.LIQ_RUN:
            liq=self.smc.liquidity_analysis(entry_df)
            rn=liq.get(f"{'run_highs' if side==Side.BUY else 'run_lows'}",[])
            if not rn: return None; rn=rn[-1]
            if side==Side.BUY:
                entry=rn["close"]; sl=rn["level"]-(self.config.spread_buffer+self.config.slippage)/100
                tp=rn["close"]+abs(rn["close"]-rn["level"])*2
            else:
                entry=rn["close"]; sl=rn["level"]+(self.config.spread_buffer+self.config.slippage)/100
                tp=rn["close"]-abs(rn["close"]-rn["level"])*2
        elif t==Technique.FAILED_AUCTION:
            entry=cp
            if side==Side.BUY:
                sl=cp*0.997-(self.config.spread_buffer+self.config.slippage)/100; tp=entry+(abs(entry-sl)*strat.rr_target)
            else:
                sl=cp*1.003+(self.config.spread_buffer+self.config.slippage)/100; tp=entry-(abs(entry-sl)*strat.rr_target)
        elif t==Technique.BREAKOUT:
            entry=cp
            if side==Side.BUY:
                sl=cp*0.995-(self.config.spread_buffer+self.config.slippage)/100; tp=entry+(abs(entry-sl)*strat.rr_target)
            else:
                sl=cp*1.005+(self.config.spread_buffer+self.config.slippage)/100; tp=entry-(abs(entry-sl)*strat.rr_target)
        else: return None
        entry=round(entry,2); sl=round(sl,2); tp=round(tp,2)
        risk_dist=abs(entry-sl); reward_dist=abs(tp-entry)
        risk_amt=risk_dist*risk_unit; reward_amt=reward_dist*risk_unit
        rr=reward_amt/risk_amt if risk_amt>0 else 0
        if rr<self.config.min_rr_after_costs: return None
        if self.config.news_filter and news:
            pair_news=[e for e in news if pair in e.get("pair","") or pair.split("USD")[0] in e.get("currency","")]
            if pair_news: tech["confidence"]*=0.5
        return TradeSignal(pair=pair,side=side,entry_price=entry,stop_loss=sl,take_profit=tp,
            timeframe=tf,strategy=strat.name,trade_type=strat.trade_type,technique=t,
            rationale=f"{t.value} | {tech['details']} | Confluence: {tech['final_score']}",
            risk_amount=risk_amt,reward_amount=reward_amt,rr_ratio=rr,
            confidence=tech["confidence"],confluence_score=tech["final_score"])

# ═══════════════════════════════════════════════════════════════════════════════
#  TRADE MANAGER
# ═══════════════════════════════════════════════════════════════════════════════
class TradeManager:
    def __init__(self,config: BotConfig,state: Dict,broker_iface):
        self.config=config; self.state=state; self.broker=broker_iface; self.open_trades: Dict[int,ExecutedTrade]={}
    def open_trade(self,signal: TradeSignal,reval: Dict) -> Optional[ExecutedTrade]:
        if not reval.get("eligible"): return None
        lot_size=reval.get("lot_mult",1.0)
        si=self.broker.get_symbol_info(signal.pair) if hasattr(self.broker,'get_symbol_info') else {}
        if si:
            ml=si.get("volume_min",0.01); step=si.get("volume_step",0.01)
            lot_size=max(ml,round(lot_size/step)*step)
        result=self.broker.place_order(signal.pair,signal.side,signal.entry_price,
            signal.stop_loss,signal.take_profit,lot_size) if hasattr(self.broker,'place_order') else {"success":False,"error":"No broker"}
        if not result.get("success"): console.print(f"[red]Trade failed: {result.get('error')}[/red]"); return None
        trade=ExecutedTrade(pair=signal.pair,side=signal.side,entry_price=result.get("price",signal.entry_price),
            stop_loss=signal.stop_loss,take_profit=signal.take_profit,lot_size=lot_size,
            broker=self.config.broker,ticket=result.get("ticket",0),technique=signal.technique,
            trade_type=signal.trade_type,strategy_name=signal.strategy,trail_stop=signal.stop_loss)
        self.open_trades[trade.ticket]=trade
        console.print(f"[green]✓ {signal.trade_type.value} {signal.pair} {signal.side.value} @ {trade.entry_price} (t#{trade.ticket})[/green]")
        console.print(f"  SL: {trade.stop_loss} | TP: {trade.take_profit} | Lot: {trade.lot_size}")
        console.print(f"  Technique: {signal.technique.value} | R:R: {signal.rr_ratio:.1f} | Confluence: {signal.confluence_score}")
        return trade
    def manage(self):
        for ticket,trade in list(self.open_trades.items()):
            si=self.broker.get_symbol_info(trade.pair) if hasattr(self.broker,'get_symbol_info') else {}
            cp=si.get("bid") if trade.side==Side.SELL else si.get("ask") if si else None
            if cp is None: continue
            cp=float(cp)
            if trade.side==Side.BUY: trade.peak_price=max(trade.peak_price or trade.entry_price,cp)
            else: trade.trough_price=min(trade.trough_price or trade.entry_price,cp)
            # Trail
            if self.config.strategies:
                strat=next((s for s in self.config.strategies if s.name==trade.strategy_name),None)
                if strat and strat.trail_enabled:
                    rd=abs(trade.entry_price-trade.stop_loss)
                    if trade.side==Side.BUY:
                        if cp>=trade.entry_price+rd*strat.trail_offset:
                            nsl=trade.entry_price+(self.config.slippage+self.config.spread_buffer)/100
                            if nsl>trade.trail_stop:
                                trade.trail_stop=nsl
                                if hasattr(self.broker,'modify_order'): self.broker.modify_order(ticket,new_sl=nsl)
                                console.print(f"[cyan]Trailing: BE at {nsl}[/cyan]")
                        nt=trade.peak_price-rd*strat.trail_step
                        if nt>trade.trail_stop and trade.peak_price:
                            trade.trail_stop=nt
                            if hasattr(self.broker,'modify_order'): self.broker.modify_order(ticket,new_sl=nt)
                            console.print(f"[cyan]Trailing: SL→{nt}[/cyan]")
                    else:
                        if cp<=trade.entry_price-rd*strat.trail_offset:
                            nsl=trade.entry_price-(self.config.slippage+self.config.spread_buffer)/100
                            if nsl<trade.trail_stop:
                                trade.trail_stop=nsl
                                if hasattr(self.broker,'modify_order'): self.broker.modify_order(ticket,new_sl=nsl)
                                console.print(f"[cyan]Trailing: BE at {nsl}[/cyan]")
                        nt=trade.trough_price+rd*strat.trail_step
                        if nt<trade.trail_stop and trade.trough_price:
                            trade.trail_stop=nt
                            if hasattr(self.broker,'modify_order'): self.broker.modify_order(ticket,new_sl=nt)
                            console.print(f"[cyan]Trailing: SL→{nt}[/cyan]")
            if trade.side==Side.BUY:
                if cp>=trade.take_profit: self._close(ticket,"TP")
                elif cp<=trade.stop_loss: self._close(ticket,"SL")
            else:
                if cp<=trade.take_profit: self._close(ticket,"TP")
                elif cp>=trade.stop_loss: self._close(ticket,"SL")
    def _close(self,ticket: int,reason: str):
        trade=self.open_trades.get(ticket)
        if not trade: return
        si=self.broker.get_symbol_info(trade.pair) if hasattr(self.broker,'get_symbol_info') else {}
        cp=si.get("bid") if trade.side==Side.SELL else si.get("ask") if si else None
        if cp: trade.exit_price=float(cp); trade.exit_time=datetime.now(timezone.utc)
        if trade.exit_price:
            if trade.side==Side.BUY: pnl=(trade.exit_price-trade.entry_price)*trade.lot_size*100
            else: pnl=(trade.entry_price-trade.exit_price)*trade.lot_size*100
            pnl-=trade.commissions+trade.slippage; trade.pnl=round(pnl,2); trade.status="closed"
            console.print(f"[yellow]Closed: {trade.pair} {trade.side.value} | PnL: ${trade.pnl:.0f}¢ ({reason})[/yellow]")
            re=RiskEngine(self.config,self.state); re.update_pnl(pnl,trade.technique,trade.rr_ratio if hasattr(trade,'rr_ratio') else 0)
            # Update strategy stats
            sp=self.state.get("strategy_performance",{}).get(trade.strategy_name,{})
            if sp: sp.setdefault("pnl_history",[]).append(pnl); SelfEvolve(self.state).update_strategy_stats(trade.strategy_name,[pnl])
        if hasattr(self.broker,'close_order'): self.broker.close_order(ticket)
        del self.open_trades[ticket]

# ═══════════════════════════════════════════════════════════════════════════════
#  CLI COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════
def cmd_setup(args): setup_wizard()

def cmd_scan(args):
    config=load_config(); state=load_state()
    console.print(Panel.fit("[bold blue]gold_cli v3 — Multi-Strategy SMC Scan[/bold blue]\n"
        f"Pairs: {', '.join(config.pairs)} | Strategies: {', '.join(s.name for s in config.strategies)}",
        border_style="blue"))
    news=[]
    if config.news_filter:
        console.print("\n[bold]Fetching Forex Factory...[/bold]")
        try:
            from bs4 import BeautifulSoup
            resp=requests.get("https://www.forexfactory.com/calendar",headers={"User-Agent":"Mozilla/5.0"},timeout=10)
            if resp.status_code==200:
                soup=BeautifulSoup(resp.text,"html.parser")
                for row in soup.select("tr"):
                    cells=row.select("td")
                    if len(cells)<6: continue
                    impact=cells[4].get_text(strip=True) if len(cells)>4 else ""
                    if "high" not in impact.lower(): continue
                    news.append({"date":cells[0].get_text(strip=True),"time":cells[1].get_text(strip=True),
                        "currency":cells[2].get_text(strip=True),"pair":cells[3].get_text(strip=True),
                        "impact":impact,"actual":cells[5].get_text(strip=True) if len(cells)>5 else "",
                        "forecast":cells[6].get_text(strip=True) if len(cells)>6 else ""})
                console.print(f"[green]✓ {len(news)} high-impact events[/green]")
        except Exception as e: console.print(f"[yellow]News failed: {e}[/yellow]")
    console.print(f"\n[bold]Fetching data for {len(config.pairs)} pairs...[/bold]")
    all_data=fetch_all_pairs_timeframes(config.pairs,days=14)
    # VP profiles
    vp_profiles={}
    for pair in config.pairs:
        vp_profiles[pair]={}
        all_tfs=set()
        for sc in config.strategies: all_tfs.update(sc.active_timeframes)
        for tf in all_tfs:
            df=all_data.get(pair,{}).get(tf,pd.DataFrame())
            if not df.empty: vp_profiles[pair][tf]=VolumeProfileEngine().compute_profile(df)
    evolve=SelfEvolve(state); risk=RiskEngine(config,state)
    gen=StrategyGenerator(config,evolve,risk)
    all_signals=[]
    for pair in config.pairs:
        pd_data=all_data.get(pair,{})
        if not pd_data: continue
        console.print(f"\n{'#'*60}\n  [bold]ANALYZING: {pair}[/bold]")
        console.print(Rule(style="dim"))
        ms_df=pd_data.get(config.strategies[-1].active_timeframes[-1] if config.strategies else "1d",pd.DataFrame())
        ms=SMCEngine().market_state(ms_df)
        console.print(f"  Market State: [bold]{ms.value}[/bold]")
        signals=gen.generate(pair,pd_data,vp_profiles.get(pair,{}),news)
        for sig in signals:
            cp=float(pd_data.get(sig.timeframe,pd.DataFrame())["Close"].iloc[-1]) if pd_data.get(sig.timeframe) is not None else sig.entry_price
            reval=risk.evaluate(sig,cp)
            if reval["eligible"]:
                console.print(f"\n[green]★ {sig.trade_type.value.upper()} {sig.pair} {sig.side.value.upper()} @ {reval['entry']}[/green]")
                console.print(f"  SL: {reval['sl']} | TP: {reval['tp']} | R:R: {reval['rr_after_costs']:.1f}")
                console.print(f"  Technique: {sig.technique.value} | Confluence: {sig.confluence_score} | Confidence: {sig.confidence:.1%}")
                console.print(f"  Risk: ${reval['risk']:.0f}¢ | Reward: ${reval['reward']:.0f}¢")
                console.print(f"  {sig.rationale}")
                all_signals.append((sig,reval))
            else:
                console.print(f"\n[red]✗ {sig.pair} {sig.side.value} — {reval['reason']}[/red]")
    console.print(f"\n{'#'*60}\n[bold]SUMMARY[/bold]")
    console.print(Rule(style="dim"))
    console.print(f"  Pairs: {len(config.pairs)} | Signals: {len(all_signals)} | Eligible: {sum(1 for _,r in all_signals if r['eligible'])}")
    if all_signals:
        console.print(f"\n[bold]Eligible:[/bold]")
        for sig,reval in all_signals:
            if reval["eligible"]:
                console.print(f"  · {sig.trade_type.value} {sig.pair} {sig.side.value.upper()} — {reval['entry']} → SL {reval['sl']} → TP {reval['tp']} (R:R {reval['rr_after_costs']:.1f})")
    if args.export:
        export={"timestamp":datetime.now(timezone.utc).isoformat(),"config":asdict(config),
                "signals":[{"signal":asdict(s),"evaluation":r} for s,r in all_signals],
                "technique_stats":state.get("technique_stats",{})}
        Path(args.export).write_text(json.dumps(export,indent=2,default=str))
        console.print(f"\n[green]✓ Exported to {args.export}[/green]")

def cmd_watch(args):
    import time as _time
    config=load_config(); state=load_state()
    interval=int(args.interval) if args.interval else 60
    console.print(f"[bold]Watcher — polling every {interval}s[/bold]")
    console.print("[yellow]Ctrl+C to stop[/yellow]\n")
    try:
        while True:
            console.print(Rule(style="dim")); console.print(f"[bold]{datetime.now().strftime('%H:%M:%S')}[/bold]")
            all_data=fetch_all_pairs_timeframes(config.pairs,days=3)
            vp_prof={}
            for pair in config.pairs:
                vp_prof[pair]={}
                all_tfs=set()
                for sc in config.strategies: all_tfs.update(sc.active_timeframes)
                for tf in all_tfs:
                    df=all_data.get(pair,{}).get(tf,pd.DataFrame())
                    if not df.empty: vp_prof[pair][tf]=VolumeProfileEngine().compute_profile(df)
            evolve=SelfEvolve(state); risk=RiskEngine(config,state)
            gen=StrategyGenerator(config,evolve,risk)
            for pair in config.pairs:
                pd_data=all_data.get(pair,{})
                if not pd_data: continue
                signals=gen.generate(pair,pd_data,vp_prof.get(pair,{}),[])
                for sig in signals:
                    cp=float(pd_data.get(sig.timeframe,pd.DataFrame())["Close"].iloc[-1]) if pd_data.get(sig.timeframe) is not None else sig.entry_price
                    reval=risk.evaluate(sig,cp)
                    if reval["eligible"]:
                        console.print(f"\n[green]★ {sig.trade_type.value} {sig.pair} {sig.side.value.upper()} @ {reval['entry']} — R:R {reval['rr_after_costs']:.1f}[/green]")
            _time.sleep(interval)
    except KeyboardInterrupt: console.print("\n[yellow]Stopped.[/yellow]")

def cmd_trades(args):
    state=load_state(); trades=state.get("trades",[])
    if not trades: console.print("[yellow]No trades.[/yellow]"); return
    table=Table(title="Trade History")
    table.add_column("Pair",style="cyan"); table.add_column("Type",style="green")
    table.add_column("Side",style="yellow"); table.add_column("Entry",style="white")
    table.add_column("SL",style="red"); table.add_column("TP",style="green")
    table.add_column("PnL",style="magenta"); table.add_column("Technique",style="blue")
    table.add_column("Status",style="white")
    for t in trades[-30:]:
        table.add_row(t.get("pair",""),t.get("trade_type",""),t.get("side",""),
            f"${t.get('entry_price',0):.2f}",f"${t.get('stop_loss',0):.2f}",
            f"${t.get('take_profit',0):.2f}",f"${t.get('pnl',0):.0f}¢",
            t.get("technique",""),t.get("status",""))
    console.print(table)
    dpnl=state.get("daily_pnl",0)
    console.print(f"\n  Daily PnL: [green]${dpnl:.0f}¢[/green]" if dpnl>=0 else f"\n  Daily PnL: [red]${dpnl:.0f}¢[/red]")
    ts=state.get("technique_stats",{})
    if ts:
        console.print(f"\n[bold]Technique Performance:[/bold]")
        for tn,tdd in ts.items():
            console.print(f"  {tn}: {tdd.get('trades',0)} trades | WR: {tdd.get('win_rate',0):.1%} | "
                          f"Avg R:R: {tdd.get('avg_rr',0):.1f} | Sharpe: {tdd.get('sharpe',0):.2f} | "
                          f"MaxDD: {tdd.get('max_drawdown',0):.0f}¢ | MaxConsecW: {tdd.get('max_consec_wins',0)} | "
                          f"MaxConsecL: {tdd.get('max_consec_losses',0)}")
    sp=state.get("strategy_performance",{})
    if sp:
        console.print(f"\n[bold]Strategy Performance:[/bold]")
        for sn,sd in sp.items():
            console.print(f"  {sn}: Sharpe={sd.get('sharpe',0):.2f} | WR={sd.get('win_rate',0):.1%} | "
                          f"AvgW={sd.get('avg_win',0):.0f}¢ | AvgL={sd.get('avg_loss',0):.0f}¢ | "
                          f"MaxDD={sd.get('max_drawdown',0):.0f}¢ | Trades={sd.get('total_trades',0)}")

def cmd_brokers(args):
    state=load_state(); brokers=state.get("brokers",{})
    if not brokers: console.print("[yellow]No brokers. Run 'gold setup'.[/yellow]"); return
    table=Table(title="Configured Brokers")
    table.add_column("Broker",style="cyan"); table.add_column("Name",style="green")
    table.add_column("Platform",style="yellow"); table.add_column("Account",style="white")
    table.add_column("Server",style="white"); table.add_column("Mode",style="magenta")
    for bid,bd in brokers.items():
        name=BROKERS.get(bid,{}).get("name",bid)
        table.add_row(bid,name,bd.get("platform","").upper(),bd.get("account","***"),
            bd.get("server","***"),"Demo" if bd.get("is_demo") else "Real")
    console.print(table); console.print(f"\n  Total: {len(brokers)} broker(s)")

def cmd_config(args):
    config=load_config()
    details="\n".join(f"  {s.name} ({s.trade_type.value}): TFs={s.active_timeframes} | Risk={s.risk_per_trade} | "
        f"SL tightness={s.stop_loss_tightness} | R:R target={s.rr_target} | Min confluence={s.min_confluence} | "
        f"Max concurrent={s.max_concurrent}" for s in config.strategies)
    tw = config.strategies[0] if config.strategies else StrategyConfig("smc_core",TradeType.INTRADAY)
    weights="\n".join(f"  {t.name}: {getattr(tw,'weight_'+t.name.replace(' ','_'),1.0):.1f}" for t in Technique)
    console.print(Panel.fit(f"[bold]Configuration[/bold]\n\n"
        f"Broker: {config.broker} ({BROKERS.get(config.broker,{}).get('name','?')})\n"
        f"Mode: {'Demo' if config.is_demo else 'Real'}\n"
        f"Account: {config.account_type} (start: {config.start_balance})\n"
        f"Pairs: {', '.join(config.pairs)}\n"
        f"Strategies: {', '.join(s.name for s in config.strategies)}\n"
        f"Slippage: {config.slippage}¢ | Spread buffer: {config.spread_buffer}¢ | Commission: {config.commission_per_lot}¢/lot\n"
        f"Min R:R after costs: {config.min_rr_after_costs} | Max daily loss: {config.max_daily_loss}\n"
        f"London session: {'ON' if config.london_session_only else 'OFF'} | News filter: {'ON' if config.news_filter else 'OFF'}\n"
        f"Self-evolve: {'ON' if config.self_evolve else 'OFF'}\n"
        f"[bold]Strategy Details:[/bold]\n{details}\n"
        f"[bold]Technique Weights:[/bold]\n{weights}\n",border_style="cyan"))

def cmd_pine(args):
    script="""//@version=5
// gold_cli v3 — Self-Evolving Multi-Strategy SMC + Volume Profile
// Pairs: XAUUSD, USOIL, NASDAQ, BTCUSD, GBPJPY
// Techniques: Absorption, VP, OB, FVG, iFVG, Invalid OB, Liq Sweep/Run, Failed Auction, Breakout
strategy("gold_cli v3 SMC+VP", overlay=true, initial_capital=100, default_qty_type=strategy.cash, default_qty_value=100, calc_on_every_tick=true)
// Inputs
risk_per_trade = input.float(10, "Risk per trade", minval=1)
sl_tightness = input.float(1.0, "SL tightness (0.5=tight,1.5=loose)", minval=0.3, maxval=2.0)
rr_target = input.float(2.0, "R:R target", minval=1.0)
use_absorption = input.bool(true, "Absorption")
use_vp = input.bool(true, "Volume Profile (VAL/VAH)")
use_ob = input.bool(true, "Order Blocks")
use_fvg = input.bool(true, "FVGs")
use_ifvg = input.bool(true, "iFVGs")
use_invalid_ob = input.bool(true, "Invalid OBs")
use_liq_sweep = input.bool(true, "Liquidity Sweeps")
use_liq_run = input.bool(true, "Liquidity Runs")
use_failed_auction = input.bool(true, "Failed Auctions")
use_breakout = input.bool(true, "Breakouts")
min_confluence = input.int(4, "Min confluence score")
// Market structure
swing_high = ta.pivothigh(high, 5, 5)
swing_low = ta.pivotlow(low, 5, 5)
hh = ta.change(swing_high) > 0; hl = ta.change(swing_low) > 0
lh = ta.change(swing_high) < 0; ll = ta.change(swing_low) < 0
trend = (hh and hl) ? 1 : (lh and ll) ? -1 : 0
// Volume Profile proxy
vp_val = ta.lowest(low, 50)
vp_vah = ta.highest(high, 50)
vp_poc = ta.ema(close, 20)
price_state = close < vp_val ? "discount" : close > vp_vah ? "premium" : "value"
// FVGs
fvg_bull = ta.pivothigh(high, 1, 1) < ta.pivotlow(low, 1, 1)[2]
fvg_bear = ta.pivotlow(low, 1, 1) > ta.pivothigh(high, 1, 1)[2]
// Absorption
vol_ma = ta.sma(volume, 20)
absorption_buy = volume > vol_ma * 1.5 and close > open and ta.change(close) > 0
absorption_sell = volume > vol_ma * 1.5 and close < open and ta.change(close) < 0
// Liquidity sweep
sweep_high = high > ta.highest(high, 20)[1] and close < ta.highest(high, 20)[1]
sweep_low = low < ta.lowest(low, 20)[1] and close > ta.lowest(low, 20)[1]
// Failed auction
failed_auc_buy = low < ta.lowest(low, 20)[1] and close > open and ta.change(close) > 0
failed_auc_sell = high > ta.highest(high, 20)[1] and close < open and ta.change(close) < 0
// Breakout
breakout_up = close > ta.highest(high, 20)[1]
breakout_down = close < ta.lowest(low, 20)[1]
// Confluence scoring
confluence = 0
confluence += use_absorption and absorption_buy ? 3 : use_absorption and absorption_sell ? 3 : 0
confluence += use_vp and price_state == "discount" and trend == 1 ? 4 : use_vp and price_state == "premium" and trend == -1 ? 4 : 0
confluence += use_fvg and fvg_bull and trend == 1 ? 3 : use_fvg and fvg_bear and trend == -1 ? 3 : 0
confluence += use_ifvg and fvg_bull and close < ta.pivothigh(high, 1, 1) ? 4 : use_ifvg and fvg_bear and close > ta.pivotlow(low, 1, 1) ? 4 : 0
confluence += use_liq_sweep and sweep_low and trend == 1 ? 4 : use_liq_sweep and sweep_high and trend == -1 ? 4 : 0
confluence += use_liq_run and breakout_up and trend == 1 ? 3 : use_liq_run and breakout_down and trend == -1 ? 3 : 0
confluence += use_failed_auction and failed_auc_buy and trend == 1 ? 4 : use_failed_auction and failed_auc_sell and trend == -1 ? 4 : 0
confluence += use_breakout and breakout_up and trend == 1 ? 3 : use_breakout and breakout_down and trend == -1 ? 3 : 0
confluence += trend == 1 ? 2 : trend == -1 ? 2 : 0
// Entry
long_entry = confluence >= min_confluence and trend == 1 and price_state == "discount"
short_entry = confluence >= min_confluence and trend == -1 and price_state == "premium"
// Risk management
sl_long = ta.lowest(low, 10) - (input.float(2.0, "Slippage (cents)") / 100)
sl_short = ta.highest(high, 10) + (input.float(2.0, "Slippage (cents)") / 100)
rr_long = (ta.highest(high, 50) - close) / (close - sl_long) * sl_tightness
rr_short = (close - ta.lowest(low, 50)) / (sl_short - close) * sl_tightness
if long_entry and rr_long >= rr_target
    strategy.entry("Long", strategy.long, comment="gold_cli v3")
    strategy.exit("Long Exit", "Long", stop=sl_long, limit=close + (close-sl_long)*rr_target,
                  trail_points=trail_points, trail_offset=trail_points)
if short_entry and rr_short >= rr_target
    strategy.entry("Short", strategy.short, comment="gold_cli v3")
    strategy.exit("Short Exit", "Short", stop=sl_short, limit=close - (sl_short-close)*rr_target,
                  trail_points=trail_points, trail_offset=trail_points)
// Plots
plot(trend == 1 ? vp_poc : na, color=color.green, title="Bias Up")
plot(trend == -1 ? vp_poc : na, color=color.red, title="Bias Down")
plotshape(long_entry, "SMC Long", shape.triangleup, location.belowbar, color.green, size.size.small)
plotshape(short_entry, "SMC Short", shape.triangledown, location.abovebar, color.red, size.size.small)
plot(vp_val, "VAL", color=color.green, linewidth=1)
plot(vp_vah, "VAH", color=color.red, linewidth=1)
"""
    if args.output:
        Path(args.output).write_text(script)
        console.print(f"[green]✓ PineScript v5 exported to {args.output}[/green]")
        console.print(f"  Import into TradingView Pine Editor or trader dev for backtesting.")
    else: console.print(script)

def cmd_traderdev(args):
    td=load_traderdev()
    console.print(Panel.fit("[bold]TraderDev — Persisted Strategies[/bold]"))
    if not td["strategies"]: console.print("[yellow]No strategies persisted yet.[/yellow]"); return
    table=Table(title="Strategy Registry")
    table.add_column("Name",style="cyan"); table.add_column("Type",style="green")
    table.add_column("TFs",style="yellow"); table.add_column("Sharpe",style="magenta")
    table.add_column("WR",style="green"); table.add_column("AvgW",style="white")
    table.add_column("AvgL",style="red"); table.add_column("MaxDD",style="red")
    table.add_column("Trades",style="white")
    for s in td["strategies"]:
        p=s.get("performance",{})
        table.add_row(s["name"],s.get("trade_type",""),",".join(s.get("active_timeframes",[])),
            f"{p.get('sharpe',0):.2f}",f"{p.get('win_rate',0):.1%}",
            f"{p.get('avg_win',0):.0f}",f"{p.get('avg_loss',0):.0f}",
            f"{p.get('max_drawdown',0):.0f}",str(p.get('total_trades',0)))
    console.print(table)
    console.print(f"\n  Total strategies: {len(td['strategies'])}")

def cmd_sharpe(args):
    state=load_state(); sp=state.get("strategy_performance",{}); ts=state.get("technique_stats",{})
    config=load_config()
    console.print(Panel.fit("[bold]Performance Dashboard[/bold]\n"))
    console.print(f"  Consecutive wins: [green]{state.get('consecutive_wins',0)}[/green] (max: {state.get('max_consecutive_wins',0)})")
    console.print(f"  Consecutive losses: [red]{state.get('consecutive_losses',0)}[/red] (max: {state.get('max_consecutive_losses',0)})")
    console.print(f"  Current balance: ${state.get('current_balance',config.start_balance):.2f}")
    console.print(f"  Daily PnL: ${state.get('daily_pnl',0):.0f}¢ | Daily loss: ${state.get('daily_loss',0):.0f}¢")
    wk=state.get("weekly_offset",""); wpnl=state.get("weekly_pnl",{}).get(wk,0)
    console.print(f"  Weekly PnL ({wk}): ${wpnl:.0f}¢")
    if sp:
        console.print(f"\n[bold]Strategy Sharpe Ratios:[/bold]")
        for sn,sd in sp.items():
            console.print(f"  {sn}: Sharpe={sd.get('sharpe',0):.2f} | WR={sd.get('win_rate',0):.1%} | "
                          f"AvgW=${sd.get('avg_win',0):.0f}¢ | AvgL=${sd.get('avg_loss',0):.0f}¢ | "
                          f"MaxDD=${sd.get('max_drawdown',0):.0f}¢ | Trades={sd.get('total_trades',0)}")
    if ts:
        console.print(f"\n[bold]Technique Breakdown:[/bold]")
        for tn,tdd in ts.items():
            console.print(f"  {tn}: {tdd.get('trades',0)} trades | WR={tdd.get('win_rate',0):.1%} | "
                          f"AvgR:R={tdd.get('avg_rr',0):.1f} | Sharpe={tdd.get('sharpe',0):.2f} | "
                          f"MaxDD={tdd.get('max_drawdown',0):.0f}¢ | MaxConsecW={tdd.get('max_consec_wins',0)} | "
                          f"MaxConsecL={tdd.get('max_consec_losses',0)}")

def cmd_man(args):
    """Display comprehensive manual for gold_cli v3."""
    script = f"""\
╔{'═'*58}╗
║  gold_cli v3 — Manual                                   ║
╚{'═'*58}╝

USAGE
  python3 -m gold_cli <command> [options]

COMMANDS
  setup       Interactive wizard — broker, credentials, strategies, pairs, risk
  scan        Full scan: 5 pairs × 6 timeframes × 3 strategies
  watch       Realtime watcher — polls every N seconds (-i 30)
  trades      Trade history + technique stats + strategy performance
  brokers     List configured brokers
  config      Full configuration + technique weights
  pine        Export PineScript v5 for TradingView/trader dev (-o file.pine)
  traderdev   TraderDev: persisted strategy registry
  sharpe      Performance dashboard: Sharpe, win rates, drawdowns
  mt5-connect Connect to MT5 and show account info
  man         This manual

───────────────────────────────────────────────────────────────
QUICK START
───────────────────────────────────────────────────────────────

  1. python3 -m gold_cli setup
     → Select broker (justmarkets), enter account/password/server
     → Choose strategies: smc_core, scalp, swing
     → Select pairs (all 5 or specific numbers)
     → Set risk: slippage=2¢, spread=1¢, min R:R=1.5

  2. pip3 install MetaTrader5   (for live MT5 trading)

  3. python3 -m gold_cli scan
     → Scans XAUUSD, USOIL, NASDAQ, BTCUSD, GBPJPY
     → Detects: absorption, volume profile, FVGs, OBs, iFVGs,
       invalid OBs, liquidity sweeps/runs, failed auctions, breakouts
     → Evaluates risk: slippage + spread + commission before entry
     → Filters by min R:R after costs

  4. python3 -m gold_cli watch -i 30
     → Realtime scanner, polls every 30 seconds
     → Alerts when confluence score ≥ threshold

  5. python3 -m gold_cli mt5-connect
     → Connects to MT5 broker, shows account balance/equity

───────────────────────────────────────────────────────────────
3 STRATEGIES (run concurrently)
───────────────────────────────────────────────────────────────

  smc_core (intraday)
    Timeframes: 15M, 1H, 4H
    Risk: 10¢/$10 per trade | SL tightness: 1.0× | R:R target: 2.0
    Min confluence: 4 | Max concurrent: 2
    → Core strategy: absorption + volume profile (buy VAL / sell VAH)

  scalp
    Timeframes: 1M, 5M
    Risk: 5¢/$5 per trade | SL tightness: 0.7× (tight) | R:R: 2.0
    Min confluence: 3 | Max concurrent: 3
    → Fast scalps, quick in/out, tight stops

  swing
    Timeframes: 4H, 1D
    Risk: 25¢/$25 per trade | SL tightness: 1.2× (wide) | R:R: 3.0
    Min confluence: 5 | Max concurrent: 1
    → Multi-day holds, wider stops, bigger targets

───────────────────────────────────────────────────────────────
11 SMC TECHNIQUES
───────────────────────────────────────────────────────────────

  1. MARKET_STRUCTURE   — BOS + CHoCH detection
  2. VOLUME_PROFILE     — VAL/VAH buy/sell zones (PRIMARY)
  3. ABSORPTION         — Buyer/seller absorption with strength scoring
  4. FVG                — Fair Value Gaps (3-candle imbalance)
  5. IFVG               — Invalidated FVGs → support/resistance
  6. INVALID_OB         — Broken order blocks → reversal zones
  7. ORDER_BLOCK        — Valid bullish/bearish OBs with strength
  8. LIQ_SWEEP          — Stop hunts that reverse (trap)
  9. LIQ_RUN            — Genuine breakouts with follow-through
  10. FAILED_AUCTION    — Price rejected at level, reverses
  11. BREAKOUT           — Volume-confirmed breakouts

Each technique has a weight. Self-evolve adjusts weights based on
performance (Sharpe, win rate). Good performers → weight up. Poor → weight down.

───────────────────────────────────────────────────────────────
VOLUME PROFILE (Core Strategy)
───────────────────────────────────────────────────────────────

  POC  = Point of Control (most volume traded at this price)
  VAL  = Value Area Low (bottom of 70% volume area)
  VAH  = Value Area High (top of 70% volume area)

  BUY  when price at DISCOUNT (below VAL) + bullish structure
  SELL when price at PREMIUM (above VAH) + bearish structure
  WAIT when price inside value area (chop)

  Buy zones (ascending priority):
    buy_zone_below_val   — below VAL, deep discount
    buy_zone_discount    — 30% into value area from bottom
    buy_zone_at_val      — at VAL itself

  Sell zones (descending priority):
    sell_zone_above_vah  — above VAH, deep premium
    sell_zone_premium    — 30% into value area from top
    sell_zone_at_vah     — at VAH itself

───────────────────────────────────────────────────────────────
WHEN TO USE / NOT USE TECHNIQUES
───────────────────────────────────────────────────────────────

  USE Order Blocks when:
    → HTF bias aligns | OB strength > 10 | Approaching from premium(discount)
    → Absorption confirmed at OB zone

  USE FVGs when:
    → Fresh FVG in trend direction | Price retracing into FVG
    → FVG aligns with higher TF structure

  USE iFVGs / Invalid OBs when:
    → Price already violated level (filled FVG / broken OB)
    → Price returning to retest invalidated zone | HTF supports reversal

  USE Liquidity Sweeps when:
    → Clear wick beyond known high/low | Quick reversal inside
    → Absorption at sweep point

  USE Liquidity Runs when:
    → Clean close beyond level WITH volume | Follow-through candles
    → HTF supports continuation

  USE Failed Auctions when:
    → Price attempts break, fails, reverses | Long rejection wick
    → HTF structure supports reversal

  USE Breakouts when:
    → Volume-confirmed close beyond consolidation | Follow-through
    → HTF trend aligns

  DON'T use OBs/FVGs when:
    → Market choppy (ADX < 15) | No clear HTF bias
    → Multiple conflicting FVGs/OBs at same level | News imminent

───────────────────────────────────────────────────────────────
RISK MODEL
───────────────────────────────────────────────────────────────

  Costs per trade = slippage + spread_buffer + commission
  Entry adjusted: BUY → entry + costs | SELL → entry - costs
  R:R calculated AFTER all costs
  Trade only if R:R after costs ≥ min_rr (default 1.5)

  Daily loss limit: max daily loss before bot stops trading
  Trail: move SL to breakeven at 1R, then trail by trail_step each R

───────────────────────────────────────────────────────────────
SELF-EVOLVING RULES
───────────────────────────────────────────────────────────────

  Per technique (after 10+ trades):
    Sharpe > 0.5 AND win rate > 45% → weight × 1.1 (cap 2.0)
    Sharpe < 0.2 OR win rate < 35% → weight × 0.9 (floor 0.3)

  Per strategy:
    3+ consecutive wins → tighten SL (× 0.9, floor 0.5)
    3+ consecutive losses → loosen SL (× 1.1, cap 1.5)

  Never backwards: only evolve toward improving Sharpe.

───────────────────────────────────────────────────────────────
PAIRS (hardcoded — no others allowed)
───────────────────────────────────────────────────────────────

  XAUUSD  → Yahoo: GC=F      → MT5: XAUUSD
  USOIL   → Yahoo: CL=F      → MT5: UKOIL
  NASDAQ  → Yahoo: NQ=F      → MT5: NAS100
  BTCUSD  → Yahoo: BTC-USD   → MT5: BTCUSD
  GBPJPY  → Yahoo: GBPJPY=X  → MT5: GBPJPY

───────────────────────────────────────────────────────────────
BROKERS (Top 10)
───────────────────────────────────────────────────────────────

  justmarkets  MT5   icmarkets    MT5   xm         MT5
  oanda        REST  pepperstone  MT5   fbs        MT5
  exness       MT5   forex_com    REST  alpari     MT5
  octa         MT5

───────────────────────────────────────────────────────────────
PERFORMANCE TARGETS
───────────────────────────────────────────────────────────────

  Weekly: 100-1000¢ growth (cents) | $100-$500 / 2 weeks (dollars)
  Positive Sharpe ratio | Avg win > avg loss
  Max consecutive wins > max consecutive losses
  Minimum drawdown

───────────────────────────────────────────────────────────────
FILES
───────────────────────────────────────────────────────────────

  ~/.gold_cli/state.json              — credentials, trades, stats, weights
  ~/.gold_cli/config.yaml             — bot configuration
  ~/.gold_cli/traderdev_strategies.json — TraderDev registry
  ~/.gold_cli/credentials.json        — legacy (v2)

  ~/gold_cli/analysis.json            — last scan export (with --export)
"""
    console.print(Text(script, style="bold green"))

def main():
    parser=argparse.ArgumentParser(prog="gold_cli",
        description="v3: Self-evolving multi-strategy SMC + Volume Profile + Absorption bot")
    sub=parser.add_subparsers(dest="command",required=True)
    sub.add_parser("setup",help="Interactive wizard").set_defaults(func=cmd_setup)
    p=sub.add_parser("scan",help="Scan all pairs"); p.add_argument("--export","-e"); p.set_defaults(func=cmd_scan)
    p=sub.add_parser("watch",help="Realtime watcher"); p.add_argument("-i","--interval",default="60"); p.set_defaults(func=cmd_watch)
    sub.add_parser("trades",help="Trade history").set_defaults(func=cmd_trades)
    sub.add_parser("brokers",help="Configured brokers").set_defaults(func=cmd_brokers)
    sub.add_parser("config",help="Show config").set_defaults(func=cmd_config)
    p=sub.add_parser("pine",help="Export PineScript v5"); p.add_argument("-o","--output"); p.set_defaults(func=cmd_pine)
    sub.add_parser("traderdev",help="TraderDev strategy registry").set_defaults(func=cmd_traderdev)
    sub.add_parser("sharpe",help="Performance dashboard").set_defaults(func=cmd_sharpe)
    sub.add_parser("man",help="Comprehensive manual").set_defaults(func=cmd_man)
    args=parser.parse_args(); args.func(args)

if __name__=="__main__": main()
