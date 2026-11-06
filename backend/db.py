"""SQLite storage: factor values, signals, positions, trades, equity, meta."""
import json
import sqlite3
import threading
import time

from . import config

_lock = threading.Lock()
_conn = None


def init():
    global _conn
    # 重复调用（uvicorn --reload / 测试）时先关旧连接，否则连接泄漏
    if _conn is not None:
        try:
            _conn.close()
        except Exception:  # noqa: BLE001
            pass
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=10.0)
    _conn.row_factory = sqlite3.Row
    # WAL：读写不互相阻塞（rollback journal 下写事务会卡住所有读，
    # 而持仓循环每 0.5s 读、API 5s 读、因子采集写，必然抢锁）
    # busy_timeout：另一进程（scripts/*.py）持锁时等待而不是立刻抛 database is locked
    for pragma in (
        "PRAGMA journal_mode=WAL",
        "PRAGMA busy_timeout=8000",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA wal_autocheckpoint=1000",
    ):
        try:
            _conn.execute(pragma)
        except sqlite3.Error as exc:  # noqa: BLE001
            log_msg = f"PRAGMA 设置失败 {pragma}: {exc}"
            print(f"[db] {log_msg}")
    with _lock, _conn:
        _conn.executescript("""
        CREATE TABLE IF NOT EXISTS factor_values (
            factor TEXT NOT NULL, symbol TEXT NOT NULL DEFAULT '',
            ts INTEGER NOT NULL, value TEXT NOT NULL,
            PRIMARY KEY (factor, symbol)
        );
        CREATE TABLE IF NOT EXISTS factor_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            factor TEXT NOT NULL, symbol TEXT NOT NULL DEFAULT '',
            ts INTEGER NOT NULL, value TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_fh ON factor_history(factor, symbol, ts);
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL, strategy TEXT NOT NULL, symbol TEXT NOT NULL,
            side TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
            acted INTEGER NOT NULL DEFAULT 0, skip_reason TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
            qty REAL NOT NULL, entry_price REAL NOT NULL,
            stop_price REAL, take_profit REAL, trail_atr REAL,
            opened_at INTEGER NOT NULL, max_hold_sec INTEGER NOT NULL,
            time_exit_at INTEGER, extreme REAL,
            status TEXT NOT NULL DEFAULT 'open',
            closed_at INTEGER, close_price REAL, pnl REAL, close_reason TEXT
        );
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL, strategy TEXT NOT NULL, symbol TEXT NOT NULL,
            side TEXT NOT NULL, qty REAL NOT NULL, price REAL NOT NULL,
            fee REAL NOT NULL DEFAULT 0, kind TEXT NOT NULL,  -- open/close
            position_id INTEGER, pnl REAL, mode TEXT NOT NULL DEFAULT 'paper'
        );
        CREATE TABLE IF NOT EXISTS equity_curve (
            ts INTEGER NOT NULL, equity REAL NOT NULL, balance REAL NOT NULL,
            unrealized REAL NOT NULL DEFAULT 0,
            mode TEXT NOT NULL DEFAULT 'paper',
            PRIMARY KEY (ts, mode)
        );
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL, level TEXT NOT NULL, msg TEXT NOT NULL
        );
        """)
        # additive migrations for pre-existing DBs
        for ddl in (
            "ALTER TABLE positions ADD COLUMN mfe_pct REAL NOT NULL DEFAULT 0",
            "ALTER TABLE positions ADD COLUMN mae_pct REAL NOT NULL DEFAULT 0",
            "ALTER TABLE positions ADD COLUMN entry_factors TEXT",
            "ALTER TABLE positions ADD COLUMN reason TEXT NOT NULL DEFAULT ''",
            # paper/live segregation: history predating the column is paper
            "ALTER TABLE positions ADD COLUMN mode TEXT NOT NULL DEFAULT 'paper'",
            "ALTER TABLE positions ADD COLUMN leverage REAL",
            "ALTER TABLE positions ADD COLUMN extra_margin REAL NOT NULL DEFAULT 0",
            # 成本口径按仓位存：不同策略可能是挂单(maker)或吃单(taker)，
            # 平仓时必须用开仓那一笔的口径，否则单笔盈亏算不准。
            # NULL = 用全局 config.PAPER_FEE_RATE / PAPER_SLIPPAGE。
            "ALTER TABLE positions ADD COLUMN fee_rate REAL",
            "ALTER TABLE positions ADD COLUMN slip_pct REAL",
            "ALTER TABLE equity_curve ADD COLUMN mode TEXT NOT NULL DEFAULT 'paper'",
        ):
            try:
                _conn.execute(ddl)
            except sqlite3.OperationalError:
                pass  # column already exists
        # equity_curve pk migration: (ts) -> (ts, mode) so paper/live points
        # in the same minute never overwrite each other
        row = _conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='equity_curve'").fetchone()
        if row and "PRIMARY KEY (ts, mode)" not in row["sql"]:
            _conn.executescript("""
                ALTER TABLE equity_curve RENAME TO equity_curve_old;
                CREATE TABLE equity_curve (
                    ts INTEGER NOT NULL, equity REAL NOT NULL,
                    balance REAL NOT NULL, unrealized REAL NOT NULL DEFAULT 0,
                    mode TEXT NOT NULL DEFAULT 'paper', PRIMARY KEY (ts, mode));
                INSERT INTO equity_curve
                    SELECT ts, equity, balance, unrealized, mode FROM equity_curve_old;
                DROP TABLE equity_curve_old;
            """)

        # 索引：这些表被高频全表扫描（position_loop 每 0.5s、API 5s、
        # prune 的 WHERE ts<? 也吃不上 idx_fh 的复合前缀），补上后
        # 库再长大也不会把 0.5s 循环拖垮。放在 migration 之后建，
        # 因为 positions/equity_curve 的 mode 列可能刚补上。
        for idx_ddl in (
            "CREATE INDEX IF NOT EXISTS idx_pos_status ON positions(status, mode)",
            "CREATE INDEX IF NOT EXISTS idx_pos_key ON positions(strategy, symbol, status)",
            "CREATE INDEX IF NOT EXISTS idx_sig_ts ON signals(ts)",
            "CREATE INDEX IF NOT EXISTS idx_sig_key ON signals(strategy, symbol, ts)",
            "CREATE INDEX IF NOT EXISTS idx_sig_acted ON signals(acted, ts)",
            "CREATE INDEX IF NOT EXISTS idx_trades_mode ON trades(mode, id)",
            "CREATE INDEX IF NOT EXISTS idx_trades_pos ON trades(position_id)",
            "CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts)",
            "CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts)",
            "CREATE INDEX IF NOT EXISTS idx_fh_ts ON factor_history(ts)",
            "CREATE INDEX IF NOT EXISTS idx_eq_mode ON equity_curve(mode, ts)",
        ):
            try:
                _conn.execute(idx_ddl)
            except sqlite3.OperationalError as exc:  # noqa: BLE001
                print(f"[db] 建索引失败 {idx_ddl}: {exc}")


    # 影子模式（因子打分，不干预实盘）
    _conn.executescript("""
        CREATE TABLE IF NOT EXISTS shadow_signals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            strategy TEXT, symbol TEXT, side TEXT,
            strength REAL, fresh INTEGER,
            px REAL, stop REAL, tp REAL,
            funding REAL, taker REAL, lsr REAL, cvd REAL,
            r1 INTEGER DEFAULT 0, r1_why TEXT,
            r2 INTEGER DEFAULT 0, r2_why TEXT,
            r3 INTEGER DEFAULT 0,
            acted INTEGER DEFAULT 0,
            outcome TEXT, outcome_r REAL, settled_ts INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_shadow_ts ON shadow_signals(ts);
        CREATE INDEX IF NOT EXISTS idx_shadow_open ON shadow_signals(outcome);
    """)


def current_mode():
    return get_meta("mode", "paper")


# -- factors ----------------------------------------------------------------

def set_factor(factor, symbol, value, keep_history=True):
    ts = int(time.time())
    payload = json.dumps(value, ensure_ascii=False)
    with _lock, _conn:
        _conn.execute("REPLACE INTO factor_values(factor,symbol,ts,value) VALUES(?,?,?,?)",
                      (factor, symbol or "", ts, payload))
        if keep_history:
            _conn.execute("INSERT INTO factor_history(factor,symbol,ts,value) VALUES(?,?,?,?)",
                          (factor, symbol or "", ts, payload))


def get_factor(factor, symbol="", max_age_sec=None):
    with _lock:
        row = _conn.execute("SELECT ts,value FROM factor_values WHERE factor=? AND symbol=?",
                            (factor, symbol or "")).fetchone()
    if not row:
        return None
    if max_age_sec and time.time() - row["ts"] > max_age_sec:
        return None
    return json.loads(row["value"])


def get_factor_ts(factor, symbol=""):
    with _lock:
        row = _conn.execute("SELECT ts FROM factor_values WHERE factor=? AND symbol=?",
                            (factor, symbol or "")).fetchone()
    return row["ts"] if row else 0


def factor_history(factor, symbol="", limit=200):
    with _lock:
        rows = _conn.execute(
            "SELECT ts,value FROM factor_history WHERE factor=? AND symbol=? "
            "ORDER BY ts DESC LIMIT ?", (factor, symbol or "", limit)).fetchall()
    return [{"ts": r["ts"], "value": json.loads(r["value"])} for r in reversed(rows)]


def all_factors_snapshot():
    with _lock:
        rows = _conn.execute("SELECT factor,symbol,ts,value FROM factor_values").fetchall()
    return [{"factor": r["factor"], "symbol": r["symbol"], "ts": r["ts"],
             "value": json.loads(r["value"])} for r in rows]


# -- signals ----------------------------------------------------------------

def log_signal(strategy, symbol, side, reason, acted, skip_reason=""):
    with _lock, _conn:
        cur = _conn.execute(
            "INSERT INTO signals(ts,strategy,symbol,side,reason,acted,skip_reason) "
            "VALUES(?,?,?,?,?,?,?)",
            (int(time.time()), strategy, symbol, side, reason[:400], int(acted), skip_reason[:200]))
    return cur.lastrowid


def recent_signals(limit=50):
    """信号列表 + 对应仓位的当前状态。

    signals 表只记「当时是否开了仓」（acted），不看仓位后来怎样。
    页面上直接显示 acted 会让人误以为还持仓，所以这里关联出 pos_status：
      open   → 还在持仓
      closed → 已经平掉（附带盈亏）
      None   → 当时就没开仓
    """
    sql = """
    SELECT s.*,
           (SELECT p.status FROM positions p
             WHERE p.symbol = s.symbol
               AND p.opened_at BETWEEN s.ts - 120 AND s.ts + 120
             ORDER BY p.id DESC LIMIT 1) AS pos_status,
           (SELECT p.pnl FROM positions p
             WHERE p.symbol = s.symbol
               AND p.opened_at BETWEEN s.ts - 120 AND s.ts + 120
             ORDER BY p.id DESC LIMIT 1) AS pos_pnl
      FROM signals s
     ORDER BY s.id DESC LIMIT ?
    """
    with _lock:
        rows = _conn.execute(sql, (limit,)).fetchall()
    return [dict(r) for r in rows]


def signal_count_since(strategy, symbol, since_ts):
    with _lock:
        row = _conn.execute(
            "SELECT COUNT(*) n FROM signals WHERE strategy=? AND symbol=? AND ts>=? AND acted=1",
            (strategy, symbol, since_ts)).fetchone()
    return row["n"]


# -- positions / trades -----------------------------------------------------

def open_position(**kw):
    mode = kw.get("mode") or current_mode()   # BEFORE _lock (non-reentrant)
    with _lock, _conn:
        cur = _conn.execute(
            "INSERT INTO positions(strategy,symbol,side,qty,entry_price,stop_price,"
            "take_profit,trail_atr,opened_at,max_hold_sec,time_exit_at,extreme,status,"
            "entry_factors,reason,mode,leverage,extra_margin,fee_rate,slip_pct) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?,?,?,?,?,?)",
            (kw["strategy"], kw["symbol"], kw["side"], kw["qty"], kw["entry_price"],
             kw.get("stop_price"), kw.get("take_profit"), kw.get("trail_atr"),
             int(time.time()), kw["max_hold_sec"], kw.get("time_exit_at"),
             kw["entry_price"], kw.get("entry_factors"), kw.get("reason", ""),
             mode, kw.get("leverage"), kw.get("extra_margin", 0.0),
             kw.get("fee_rate"), kw.get("slip_pct")))
    return cur.lastrowid


def get_position(pid):
    with _lock:
        row = _conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
    return dict(row) if row else None


def set_position_field(pid, field, value):
    """更新单个仓位的一个字段（白名单校验）。"""
    allowed = {"leverage", "extra_margin"}
    if field not in allowed:
        raise ValueError(f"field {field} not allowed")
    with _lock, _conn:
        _conn.execute(f"UPDATE positions SET {field}=? WHERE id=?", (value, pid))


def add_extra_margin(pid, amount):
    """追加保证金（累加），返回新值。"""
    with _lock, _conn:
        _conn.execute("UPDATE positions SET extra_margin=COALESCE(extra_margin,0)+? WHERE id=?",
                      (amount, pid))
        row = _conn.execute("SELECT extra_margin FROM positions WHERE id=?", (pid,)).fetchone()
    return float(row[0] or 0) if row else 0.0


def open_positions(strategy=None, symbol=None, mode="auto"):
    """mode='auto' -> only the ACTIVE trading mode's positions (paper/live
    strictly segregated); mode=None -> all; or an explicit 'paper'/'live'."""
    q = "SELECT * FROM positions WHERE status='open'"
    args = []
    if mode == "auto":
        mode = current_mode()
    if mode:
        q += " AND mode=?"; args.append(mode)
    if strategy:
        q += " AND strategy=?"; args.append(strategy)
    if symbol:
        q += " AND symbol=?"; args.append(symbol)
    with _lock:
        rows = _conn.execute(q, args).fetchall()
    return [dict(r) for r in rows]


def update_position(pid, **fields):
    sets = ", ".join(f"{k}=?" for k in fields)
    with _lock, _conn:
        _conn.execute(f"UPDATE positions SET {sets} WHERE id=?", (*fields.values(), pid))


def close_position(pid, price, pnl, reason):
    with _lock, _conn:
        _conn.execute(
            "UPDATE positions SET status='closed', closed_at=?, close_price=?, pnl=?, "
            "close_reason=? WHERE id=?", (int(time.time()), price, pnl, reason, pid))


def log_trade(strategy, symbol, side, qty, price, fee, kind, position_id, pnl, mode):
    with _lock, _conn:
        _conn.execute(
            "INSERT INTO trades(ts,strategy,symbol,side,qty,price,fee,kind,position_id,pnl,mode) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), strategy, symbol, side, qty, price, fee, kind,
             position_id, pnl, mode))


def recent_trades(limit=100, mode=None):
    q = "SELECT * FROM trades"
    args = []
    if mode == "auto":
        mode = current_mode()
    if mode:
        q += " WHERE mode=?"; args.append(mode)
    q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
    with _lock:
        rows = _conn.execute(q, args).fetchall()
    return [dict(r) for r in rows]


def closed_positions_today(day_start_ts, mode="auto"):
    if mode == "auto":
        mode = current_mode()
    q = "SELECT * FROM positions WHERE status='closed' AND closed_at>=?"
    args = [day_start_ts]
    if mode:
        q += " AND mode=?"; args.append(mode)
    with _lock:
        rows = _conn.execute(q, args).fetchall()
    return [dict(r) for r in rows]


# -- equity / meta / logs ---------------------------------------------------

def record_equity(equity, balance, unrealized, mode=None):
    ts = int(time.time()) // 60 * 60  # minute buckets
    mode = mode or current_mode()     # BEFORE taking _lock (non-reentrant!)
    with _lock, _conn:
        _conn.execute(
            "REPLACE INTO equity_curve(ts,equity,balance,unrealized,mode) VALUES(?,?,?,?,?)",
            (ts, equity, balance, unrealized, mode))


def day_start_equity(mode=None):
    """当日 00:00 之后第一个净值点；没有则返回 0。"""
    import datetime as _dt
    today = int(_dt.datetime.now().replace(hour=0, minute=0, second=0,
                                           microsecond=0).timestamp())
    m = mode or current_mode()
    with _lock:
        row = _conn.execute(
            "SELECT equity FROM equity_curve WHERE mode=? AND ts>=? ORDER BY ts LIMIT 1",
            (m, today)).fetchone()
    return float(row[0]) if row else 0.0


def equity_series(limit=2880, mode="auto"):
    if mode == "auto":
        mode = current_mode()
    if mode:
        q = "SELECT * FROM equity_curve WHERE mode=? ORDER BY ts DESC LIMIT ?"
        args = (mode, limit)
    else:
        q = "SELECT * FROM equity_curve ORDER BY ts DESC LIMIT ?"
        args = (limit,)
    with _lock:
        rows = _conn.execute(q, args).fetchall()
    return [dict(r) for r in reversed(rows)]


def set_meta(key, value):
    with _lock, _conn:
        _conn.execute("REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(value)))


def get_meta(key, default=""):
    with _lock:
        row = _conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def log(level, msg):
    with _lock, _conn:
        _conn.execute("INSERT INTO logs(ts,level,msg) VALUES(?,?,?)",
                      (int(time.time()), level, msg[:800]))


def recent_logs(limit=100):
    with _lock:
        rows = _conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def prune(factor_days: int = 14, log_days: int = 30) -> dict:
    """删过期数据，返回各表删除行数。

    factor_history 增长最快（每个因子每次采集一行），保留 14 天足够做近期对比。
    logs 是运维日志，保留 30 天。
    signals / shadow_signals / equity_curve / trades 一律不动 —— 那是交易与分析资产。
    """
    now = int(time.time())
    fc, lc = now - factor_days * 86400, now - log_days * 86400
    with _lock, _conn:
        nf = _conn.execute("DELETE FROM factor_history WHERE ts<?", (fc,)).rowcount
        nl = _conn.execute("DELETE FROM logs WHERE ts<?", (lc,)).rowcount
    return {"factor_history": nf, "logs": nl}

# ── 影子模式 ─────────────────────────────────────────────────

def shadow_insert(**kw):
    cols = ("ts,strategy,symbol,side,strength,fresh,px,stop,tp,funding,"
            "taker,lsr,cvd,r1,r1_why,r2,r2_why,r3,acted")
    keys = ("ts","strategy","symbol","side","strength","fresh","px","stop","tp","funding",
            "taker","lsr","cvd","r1","r1_why","r2","r2_why","r3","acted")
    with _lock, _conn:
        cur = _conn.execute(
            "INSERT INTO shadow_signals(" + cols + ") VALUES(" + ",".join("?"*len(keys)) + ")",
            tuple(kw.get(k) for k in keys))
        return cur.lastrowid


def shadow_pending():
    """未结算的影子信号（30 分钟内、超过 2 分钟避免刚开就判）。"""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM shadow_signals WHERE outcome IS NULL AND ts < ? "
            "ORDER BY id DESC LIMIT 400", (int(time.time()) - 120,)).fetchall()
    return [dict(r) for r in rows]


def shadow_settle(sid, outcome, r_mult, ts=None):
    with _lock, _conn:
        _conn.execute(
            "UPDATE shadow_signals SET outcome=?, outcome_r=?, settled_ts=? WHERE id=?",
            (outcome, float(r_mult), int(ts or time.time()), int(sid)))


def shadow_recent(limit=100):
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM shadow_signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def shadow_stats():
    """各规则下的胜率与平均 R。"""
    with _lock:
        rows = _conn.execute(
            "SELECT r1,r2,r3,acted,outcome,outcome_r FROM shadow_signals "
            "WHERE outcome IS NOT NULL").fetchall()
    def agg(items):
        n = len(items)
        if not n:
            return dict(n=0, win=0.0, avg_r=0.0)
        wins = sum(1 for x in items if (x["outcome_r"] or 0) > 0)
        return dict(n=n, win=round(wins/n*100, 1),
                    avg_r=round(sum(x["outcome_r"] or 0 for x in items)/n, 3))
    rows = [dict(r) for r in rows]
    return {
        "all": agg(rows),
        "r1_pass": agg([r for r in rows if not r["r1"]]),
        "r1_block": agg([r for r in rows if r["r1"]]),
        "r2_pass": agg([r for r in rows if not r["r2"]]),
        "r2_block": agg([r for r in rows if r["r2"]]),
        "r3_half": agg([r for r in rows if r["r3"]]),
        "acted": agg([r for r in rows if r["acted"]]),
        "skipped": agg([r for r in rows if not r["acted"]]),
    }
