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
            stop_price REAL,
            opened_at INTEGER NOT NULL, max_hold_sec INTEGER NOT NULL,
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
            # 「到 2R 平一半」标记：1 = 已平过一半，剩下一半放开跑。
            # 必须落库而不是只放内存 —— 重启后内存状态丢失 → 会再平一次一半，
            # 直接把仓位砍成 1/4。
            "ALTER TABLE positions ADD COLUMN half_closed INTEGER NOT NULL DEFAULT 0",
            # 「持仓期最高浮盈 R」：新出场规则用它算「回撤浮盈的 50%」，
            # 峰顶不准 → 出局点就画错、也可能提前或拖后出场。
            # 原本只在 _S15_POS_STATE 内存里，重启归零后要重新爬。
            "ALTER TABLE positions ADD COLUMN peak_r REAL NOT NULL DEFAULT 0",
            # 这笔仓位是在哪家交易所开的（binance / okx / bitget）。
            # 为什么必须落库：曾经只存一个全局「当前执行交易所」，切过去之后
            # close_position 会拿新交易所去平旧仓位 —— 旧仓位在另一家，
            # 平不掉；万一那边恰好也有同名合约，还会平错仓。
            # 空串 = 历史仓位/纸面仓位，按当前设置处理。
            "ALTER TABLE positions ADD COLUMN exchange TEXT NOT NULL DEFAULT ''",
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


def open_position(**kw):
    mode = kw.get("mode") or current_mode()   # BEFORE _lock (non-reentrant)
    with _lock, _conn:
        cur = _conn.execute(
            "INSERT INTO positions(strategy,symbol,side,qty,entry_price,stop_price,"
            "opened_at,max_hold_sec,status,"
            "entry_factors,reason,mode,leverage,extra_margin,fee_rate,slip_pct,exchange) "
            "VALUES(?,?,?,?,?,?,?,?,'open',?,?,?,?,?,?,?,?)",
            (kw["strategy"], kw["symbol"], kw["side"], kw["qty"], kw["entry_price"],
             kw.get("stop_price"),
             int(time.time()), kw["max_hold_sec"],
             kw.get("entry_factors"), kw.get("reason", ""),
             mode, kw.get("leverage"), kw.get("extra_margin", 0.0),
             kw.get("fee_rate"), kw.get("slip_pct"), kw.get("exchange", "") or ""))
        return cur.lastrowid


def get_position(pid):
    with _lock:
        row = _conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
    return dict(row) if row else None


def set_position_field(pid, field, value):
    """更新单个仓位的一个字段（白名单校验）。返回是否真的改了。

    ⚠ 2026-11-09 加 status='open' 条件：已平仓位不该再被改杠杆/保证金，
    否则历史记录会被污染，看起来像「平仓后参数还变了」。
    """
    allowed = {"leverage", "extra_margin"}
    if field not in allowed:
        raise ValueError(f"field {field} not allowed")
    with _lock, _conn:
        cur = _conn.execute(
            f"UPDATE positions SET {field}=? WHERE id=? AND status='open'", (value, pid))
        return cur.rowcount > 0


def add_extra_margin(pid, amount):
    """追加保证金（累加）。返回新值；仓位已平则返回 None。"""
    with _lock, _conn:
        cur = _conn.execute(
            "UPDATE positions SET extra_margin=COALESCE(extra_margin,0)+? "
            "WHERE id=? AND status='open'", (amount, pid))
        if cur.rowcount == 0:
            return None
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


# 允许通过 update_position 改的列。该函数用 f-string 拼列名进 SQL，
# 之前没有任何白名单 —— 虽然现有调用点都是硬编码键名、当前不可注入，
# 但它是模块里唯一没护栏的写入口，任何新调用方传 **payload 就能改 status 等任意列。
_POSITION_WRITABLE = {
    "strategy", "symbol", "side", "qty", "entry_price", "stop_price",
    "opened_at", "max_hold_sec",
    "status", "closed_at", "close_price", "pnl", "close_reason",
    "mfe_pct", "mae_pct", "entry_factors", "reason", "mode", "leverage",
    "extra_margin", "fee_rate", "slip_pct", "half_closed",
    "peak_r",
}


def update_position(pid, **fields):
    """更新持仓字段。只作用于 status='open' 的仓位，返回是否真的改了。

    ⚠ 2026-11-09 加 status='open' 条件：部分平仓走这条路改 qty，
    而「引擎半平 + 手动半平」/「双击确认按钮」会拿着同一份快照各改一次，
    无条件 UPDATE 会让两笔都生效 → 交易所卖光了、本地还显示持仓，
    盈亏也双记。返回 False 表示仓位已被别处平掉，调用方必须撤销自己的账。
    """
    bad = set(fields) - _POSITION_WRITABLE
    if bad:
        raise ValueError(f"update_position 非法列名: {sorted(bad)}")
    sets = ", ".join(f"{k}=?" for k in fields)
    with _lock, _conn:
        cur = _conn.execute(
            f"UPDATE positions SET {sets} WHERE id=? AND status='open'",
            (*fields.values(), pid))
        return cur.rowcount > 0


def add_realized_pnl(pid, delta):
    """把一次部分平仓的盈亏累加进 positions.pnl（仓位仍 open）。

    2026-11-06 修：之前部分平仓只写 trades 流水、不碰 positions.pnl，
    等剩下的仓位全平时 close_position 直接 SET pnl=? 把前面那笔覆盖掉。
    实测仓位 #19 POLUSDT：流水 −17.05 与 +15.86 两笔，positions.pnl 只剩
    +15.86，真实亏损 −1.19 被抹成盈利。而熔断读的正是 positions.pnl → 低估亏损。

    ⚠ 2026-11-09 加 status='open' 条件：仓位已被别处平掉时不能再累加，
    否则已结算仓位的 pnl 会被二次污染。返回 False 表示没加上，调用方要撤销余额。
    """
    with _lock, _conn:
        cur = _conn.execute(
            "UPDATE positions SET pnl = COALESCE(pnl, 0) + ? "
            "WHERE id=? AND status='open'", (delta, pid))
        return cur.rowcount > 0


def close_position(pid, price, pnl, reason):
    """pnl 是**本次**平仓的增量，累加进已有 pnl 总额（可能已含部分平仓的盈亏）。

    加 status='open' 是幂等保护：引擎的 manage_positions 与手动/日目标全平
    可能同时对同一个仓位触发（中间有 await 让步点），没有这个条件会把同一
    仓位平两次、盈亏双记、纸面余额被加减两次。
    """
    with _lock, _conn:
        cur = _conn.execute(
            "UPDATE positions SET status='closed', closed_at=?, close_price=?, "
            "pnl=COALESCE(pnl, 0) + ?, close_reason=? WHERE id=? AND status='open'",
            (int(time.time()), price, pnl, reason, pid))
        # ⚠ 这里绝对不能再调 db.log()：它内部也 with _lock，而 _lock 是普通
        # Lock（不可重入），持锁时再调 = 直接死锁。返回值交给调用方判断。
        return cur.rowcount > 0


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


def realized_pnl_since(ts, mode="auto"):
    """某时间点之后的**已实现盈亏**，直接对 trades 流水求和。

    ⚠ 2026-11-09 新增。不能用 positions.pnl 求和，因为
    positions.pnl 是「该仓位全部平仓盈亏之和」，包含昨天已经 partial_close
    掉的那部分，却按 closed_at 整笔归到平仓当天。

    实测仓位 #19 POLUSDT：昨天 partial_close −17.0529，今天 close +15.8556，
    positions.pnl = +15.8556（真实 −1.1973）。跨日时这笔仓位整笔被算进今天，
    昨天的亏损凭空消失，甚至变成今天是赚的 → 熔断判断失真。

    对 trades 求和则是「哪一天平的就记在哪一天」，不会串。
    """
    if mode == "auto":
        mode = current_mode()
    q = ("SELECT COALESCE(SUM(pnl), 0) FROM trades "
         "WHERE ts >= ? AND kind IN ('close', 'partial_close')")
    args = [ts]
    if mode:
        q += " AND mode=?"
        args.append(mode)
    with _lock:
        row = _conn.execute(q, args).fetchone()
    return float(row[0] or 0) if row else 0.0


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
    """写 meta。撞写锁时短重试 3 次 —— 日刷新的大事务通常几秒就放锁，
    以前一次 database is locked 就直接抛给调用方（心跳），拖死 0.5s 循环。"""
    last = None
    for attempt in range(3):
        try:
            with _lock, _conn:
                _conn.execute("REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(value)))
            return
        except sqlite3.OperationalError as exc:
            last = exc
            if "locked" not in str(exc).lower():
                raise
            time.sleep(0.3 * (attempt + 1))
    raise last


def heartbeat_meta(key, value):
    """心跳专用写入：独立连接 + 0.25 秒超时，写不进去就立刻放弃。

    为什么不复用 set_meta：set_meta 的重试 + busy_timeout(8s) 是为了保证
    「钱相关的写」不丢，但心跳只是给看门狗看的存活标记 —— 用同一套耐心去写它，
    一次日刷新长事务就能把 0.5 秒循环拖慢十几秒（实测 16 秒），
    那段时间止损/止盈是停摆的。宁可心跳丢几次，也不能让管钱的循环排队。
    """
    try:
        c = sqlite3.connect(config.DB_PATH, timeout=0.25)
        try:
            c.execute("REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(value)))
            c.commit()
            return True
        finally:
            c.close()
    except Exception:
        return False


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
    """各规则下的胜率与平均 R。

    ★ 2026-11-09 浩哥定：统计只算 config.SAMPLE_START 之后的样本。
    更早的样本跨了好几版参数（池子 ATR、风控三参数、两闸都改过），
    混在一起算胜率没有意义 —— 会把「旧规则下的成绩」算到「新规则」头上。
    """
    from . import config as _cfg
    lo = getattr(_cfg, "SAMPLE_START_TS", None)
    if lo:
        with _lock:
            rows = _conn.execute(
                "SELECT r1,r2,r3,acted,outcome,outcome_r FROM shadow_signals "
                "WHERE outcome IS NOT NULL AND ts >= ?", (lo,)).fetchall()
    else:
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
