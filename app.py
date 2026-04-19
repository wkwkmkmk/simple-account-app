"""
支出申告アプリ
支出を申告・集計する Streamlit アプリケーション

接続先:
  - Streamlit Secrets に SQLITECLOUD_URL が設定されていれば SQLite Cloud を使用
  - 設定がなければローカルの SQLite ファイル（household_expenses.db）を使用
"""
import io
import sqlite3
import hashlib
from calendar import monthrange
from datetime import date, datetime

import altair as alt
import pandas as pd
import streamlit as st

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
DB_PATH = "household_expenses.db"

CATEGORIES = [
    "食費",       # 毎日発生
    "日用品費",   # 頻繁
    "交通費",     # 頻繁
    "雑費",       # 頻繁
    "美容費",     # ときどき
    "趣味費",     # ときどき
    "交際費",     # ときどき
    "被服費",     # ときどき
    "医療費",     # ときどき
    "教育費",     # ときどき
    "水道光熱費", # 月次固定
    "通信費",     # 月次固定
    "住居費",     # 月次固定
    "保険料",     # 月次固定
    "特別費",     # まれ
]

REPORTERS = ["夫", "妻"]

# 費目の区分（固定費 / 変動費 / 特別費）
#   固定費 : 月次で概ね一定額が発生
#   変動費 : 日々の生活で発生、コントロール余地あり
#   特別費 : 非経常的、年に数回
CATEGORY_TYPE: dict[str, str] = {
    "食費":       "変動費",
    "日用品費":   "変動費",
    "交通費":     "変動費",
    "雑費":       "変動費",
    "美容費":     "変動費",
    "趣味費":     "変動費",
    "交際費":     "変動費",
    "被服費":     "変動費",
    "医療費":     "変動費",
    "教育費":     "変動費",
    "水道光熱費": "固定費",
    "通信費":     "固定費",
    "住居費":     "固定費",
    "保険料":     "固定費",
    "特別費":     "特別費",
}

# 日本の二人以上世帯のエンゲル係数目安（総務省 家計調査 概数）
ENGEL_BENCHMARK = 27.0

# ---------------------------------------------------------------------------
# データベース接続
# ---------------------------------------------------------------------------

def _get_secret(key: str, default: str = "") -> str:
    """Secrets から値を取得する。secrets.toml が存在しない場合は default を返す。"""
    try:
        return st.secrets[key]
    except Exception:
        return default


def _connect():
    """
    Secrets の優先順位: SQLite Cloud → Turso → ローカル SQLite
    """
    # ── SQLite Cloud ────────────────────────────────────────────────────────
    url = _get_secret("SQLITECLOUD_URL")
    if url:
        try:
            import sqlitecloud
            return sqlitecloud.connect(url)
        except ImportError:
            st.error("sqlitecloud パッケージが見つかりません。requirements.txt を確認してください。")
            st.stop()

    # ── Turso ───────────────────────────────────────────────────────────────
    turso_url   = _get_secret("TURSO_URL")
    turso_token = _get_secret("TURSO_TOKEN")
    if turso_url and turso_token:
        try:
            import libsql
            conn = libsql.connect("turso_replica.db",
                                  sync_url=turso_url,
                                  auth_token=turso_token)
            conn.sync()  # 起動時にリモートと同期
            return conn
        except ImportError:
            st.error("libsql パッケージが見つかりません。requirements.txt を確認してください。")
            st.stop()

    # ── ローカル SQLite フォールバック ───────────────────────────────────────
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _sync(conn) -> None:
    """Turso の embedded replica 使用時のみリモートと同期する（他は no-op）。"""
    if hasattr(conn, "sync"):
        conn.sync()


def _init_schema(conn) -> None:
    """テーブルと初期データを作成する（べき等）。executescript は使わず個別実行。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT    UNIQUE NOT NULL,
            password_hash TEXT    NOT NULL,
            is_admin      INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT    DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            expense_date TEXT    NOT NULL,
            reporter     TEXT    NOT NULL,
            description  TEXT    NOT NULL,
            category     TEXT    NOT NULL,
            amount       INTEGER NOT NULL CHECK(amount > 0),
            created_at   TEXT    DEFAULT (datetime('now','localtime'))
        )
    """)

    conn.execute(
        "INSERT OR IGNORE INTO users (username, password_hash, is_admin) VALUES (?,?,1)",
        ("admin", _hash("admin123")),
    )
    conn.commit()

    # マイグレーション: reporter カラムが未存在なら追加
    try:
        conn.execute("SELECT reporter FROM users LIMIT 0")
    except Exception:
        conn.execute("ALTER TABLE users ADD COLUMN reporter TEXT NOT NULL DEFAULT ''")
        conn.commit()

    _sync(conn)  # Turso の場合: 初期化完了後にリモートへ反映


@st.cache_resource
def _get_conn():
    """接続を一度だけ作成してキャッシュする（スキーマ初期化も行う）。"""
    conn = _connect()
    _init_schema(conn)
    return conn


def get_db():
    """
    キャッシュされた接続を返す。
    Turso 使用時は毎回 sync() を呼んで最新データを取得する。
    """
    conn = _get_conn()
    _sync(conn)
    return conn


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _hash(pw: str) -> str:
    """パスワードを SHA-256 でハッシュ化する。"""
    return hashlib.sha256(pw.encode()).hexdigest()


def _fetch_dict(cursor) -> dict | None:
    """カーソルから 1 行を取得して辞書に変換する（row_factory 不要）。"""
    row = cursor.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))


def _fetch_col(cursor) -> list:
    """カーソルの単一カラム結果をリストで返す。"""
    return [row[0] for row in cursor.fetchall()]


def _is_unique_error(e: Exception) -> bool:
    """sqlite3 / sqlitecloud どちらの UNIQUE 制約違反も検出する。"""
    msg = str(e).upper()
    return "UNIQUE" in msg or "UNIQUE CONSTRAINT" in msg


# ---------------------------------------------------------------------------
# 認証
# ---------------------------------------------------------------------------

def authenticate(username: str, password: str) -> dict | None:
    """ユーザー認証。成功したらユーザー情報の dict を返す。"""
    cur = get_db().execute(
        "SELECT * FROM users WHERE username=? AND password_hash=?",
        (username, _hash(password)),
    )
    return _fetch_dict(cur)


# ---------------------------------------------------------------------------
# ログインページ
# ---------------------------------------------------------------------------

def page_login() -> None:
    _, col, _ = st.columns([1, 3, 1])
    with col:
        st.markdown("## 🏠 支出申告・集計アプリ")
        st.divider()

        with st.form("login_form"):
            username = st.text_input("ユーザー名")
            password = st.text_input("パスワード", type="password")
            if st.form_submit_button("ログイン", use_container_width=True, type="primary"):
                user = authenticate(username, password)
                if user:
                    st.session_state.user = user
                    st.rerun()
                else:
                    st.error("ユーザー名またはパスワードが正しくありません")


# ---------------------------------------------------------------------------
# 支出申告ページ
# ---------------------------------------------------------------------------

def page_expense_entry() -> None:
    st.header("💰 支出申告")
    db = get_db()

    with st.form("expense_form", clear_on_submit=True):
        col_l, col_r = st.columns(2)

        with col_l:
            expense_date = st.date_input(
                "📅 日付", value=date.today(), format="YYYY/MM/DD"
            )
            _my_reporter = (st.session_state.user or {}).get("reporter", "")
            _default_reporter = _my_reporter if _my_reporter in REPORTERS else REPORTERS[0]
            reporter = st.segmented_control(
                "👤 申告者", options=REPORTERS, default=_default_reporter
            )
            amount = st.number_input(
                "💴 金額（円）",
                min_value=1,
                max_value=10_000_000,
                step=100,
                value=None,
                placeholder="金額を入力",
            )

        with col_r:
            category = st.selectbox("🏷️ 費目", CATEGORIES)
            description = st.text_area(
                "📝 内容（任意）",
                placeholder="内容のメモがあれば入力してください",
                height=148,
            )

        if st.form_submit_button("✅ 申告する", type="primary", use_container_width=True):
            if amount is None:
                st.error("金額を入力してください。")
            else:
                db.execute(
                    "INSERT INTO expenses"
                    " (expense_date, reporter, description, category, amount)"
                    " VALUES (?,?,?,?,?)",
                    (str(expense_date), reporter, description.strip() if description else "", category, int(amount)),
                )
                db.commit()
                _sync(db)
                st.success(
                    f"✅ 申告しました ─ {reporter} ／ {category} ／ ¥{int(amount):,}"
                )

    # 直近の申告
    st.divider()
    st.subheader("直近の申告（最新10件）")
    recent = pd.read_sql_query(
        "SELECT expense_date AS 日付, reporter AS 申告者,"
        " category AS 費目, description AS 内容, amount AS 金額"
        " FROM expenses ORDER BY created_at DESC LIMIT 10",
        db,
    )
    if recent.empty:
        st.info("まだ申告データがありません。")
    else:
        recent["金額"] = recent["金額"].apply(lambda x: f"¥{x:,}")
        st.dataframe(recent, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# 集計ページ
# ---------------------------------------------------------------------------

def page_aggregation() -> None:
    st.header("📊 集計")
    db = get_db()

    now = datetime.now()
    col1, col2 = st.columns(2)
    with col1:
        year = st.selectbox(
            "年",
            list(range(now.year + 1, 2019, -1)),
            index=1,  # now.year が先頭から2番目（降順）
        )
    with col2:
        month = st.selectbox(
            "月",
            list(range(1, 13)),
            index=now.month - 1,
            format_func=lambda m: f"{m}月",
        )

    df = pd.read_sql_query(
        """SELECT expense_date, reporter, category, description, amount
           FROM expenses
           WHERE strftime('%Y', expense_date) = ?
             AND strftime('%m', expense_date) = ?
           ORDER BY expense_date, created_at""",
        db,
        params=(str(year), f"{month:02d}"),
    )

    st.divider()

    if df.empty:
        st.info(f"{year}年{month}月のデータはありません。")
        return

    # ── 合計サマリ ──────────────────────────────────────────────────────────
    st.subheader(f"📌 {year}年{month}月 集計結果")

    husband = int(df.loc[df["reporter"] == "夫", "amount"].sum())
    wife    = int(df.loc[df["reporter"] == "妻", "amount"].sum())
    total   = husband + wife

    m1, m2, m3 = st.columns(3)
    m1.metric("🧔 夫 合計",  f"¥{husband:,}")
    m2.metric("👩 妻 合計",  f"¥{wife:,}")
    m3.metric("💰 総合計",   f"¥{total:,}")

    st.divider()

    # ── 費目別集計 ───────────────────────────────────────────────────────────
    st.subheader("費目別集計")

    pivot = (
        df.groupby(["category", "reporter"])["amount"]
        .sum()
        .unstack(fill_value=0)
        .reset_index()
    )
    pivot.columns.name = None
    for col in ["夫", "妻"]:
        if col not in pivot.columns:
            pivot[col] = 0
    pivot["合計"] = pivot["夫"] + pivot["妻"]
    pivot = pivot.sort_values("合計", ascending=False)

    fmt = pivot.copy().rename(columns={"category": "費目"})
    for col in ["夫", "妻", "合計"]:
        fmt[col] = fmt[col].apply(lambda x: f"¥{x:,}")
    st.dataframe(fmt, use_container_width=True, hide_index=True)

    st.divider()

    # ── 申告データ一覧 ───────────────────────────────────────────────────────
    st.subheader("申告データ一覧")

    detail = df.rename(columns={
        "expense_date": "日付",
        "reporter":     "申告者",
        "category":     "費目",
        "description":  "内容",
        "amount":       "金額（円）",
    })
    detail_fmt = detail.copy()
    detail_fmt["金額（円）"] = detail_fmt["金額（円）"].apply(lambda x: f"¥{x:,}")
    st.dataframe(detail_fmt, use_container_width=True, hide_index=True)

    st.divider()

    # ── CSV エクスポート ─────────────────────────────────────────────────────
    buf = io.BytesIO()
    detail.to_csv(buf, index=False, encoding="utf-8-sig")

    st.download_button(
        label="📥 CSVダウンロード",
        data=buf.getvalue(),
        file_name=f"家計_{year}年{month:02d}月.csv",
        mime="text/csv",
        use_container_width=True,
    )


# ---------------------------------------------------------------------------
# 分析ページ
# ---------------------------------------------------------------------------

def _load_all_expenses(db) -> pd.DataFrame:
    """全支出を取得し、分析で使う派生カラムを付加して返す。"""
    df = pd.read_sql_query(
        "SELECT expense_date, reporter, category, description, amount FROM expenses",
        db,
    )
    if df.empty:
        return df
    df["expense_date"] = pd.to_datetime(df["expense_date"])
    df["year"]       = df["expense_date"].dt.year
    df["month"]      = df["expense_date"].dt.month
    df["year_month"] = df["expense_date"].dt.strftime("%Y-%m")
    df["type"]       = df["category"].map(CATEGORY_TYPE).fillna("変動費")
    return df


def _prev_year_month(ym: str) -> str:
    """'YYYY-MM' の前月を返す。"""
    y, m = map(int, ym.split("-"))
    m -= 1
    if m <= 0:
        m += 12
        y -= 1
    return f"{y}-{m:02d}"


def page_analysis() -> None:
    st.header("📈 分析")
    db = get_db()
    df_all = _load_all_expenses(db)

    if df_all.empty:
        st.info("まだ申告データがありません。支出を登録すると分析できるようになります。")
        return

    tab_now, tab_future = st.tabs(["🔍 現状診断", "🔮 将来ビュー"])

    with tab_now:
        _render_current_analysis(df_all)
    with tab_future:
        _render_future_analysis(df_all)


# --- 現状診断タブ ----------------------------------------------------------

def _render_current_analysis(df_all: pd.DataFrame) -> None:
    months_available = sorted(df_all["year_month"].unique())

    # 既定は直近12ヶ月
    default_start_idx = max(0, len(months_available) - 12)
    c1, c2 = st.columns(2)
    with c1:
        start_ym = st.selectbox(
            "開始月", months_available,
            index=default_start_idx, key="ana_start",
        )
    with c2:
        end_ym = st.selectbox(
            "終了月", months_available,
            index=len(months_available) - 1, key="ana_end",
        )
    if start_ym > end_ym:
        st.error("開始月は終了月以前にしてください。")
        return

    mask = (df_all["year_month"] >= start_ym) & (df_all["year_month"] <= end_ym)
    df = df_all.loc[mask].copy()
    period_months = [m for m in months_available if start_ym <= m <= end_ym]

    if df.empty:
        st.info("選択期間にデータがありません。")
        return

    # ── 期間サマリ（月平均） ────────────────────────────────────────────────
    monthly       = df.groupby("year_month")["amount"].sum()
    food_monthly  = df[df["category"] == "食費"].groupby("year_month")["amount"].sum()
    fixed_monthly = df[df["type"] == "固定費"].groupby("year_month")["amount"].sum()
    engel_series  = (food_monthly.reindex(period_months, fill_value=0)
                     / monthly.reindex(period_months).replace(0, pd.NA) * 100)
    fixed_ratio   = (fixed_monthly.reindex(period_months, fill_value=0)
                     / monthly.reindex(period_months).replace(0, pd.NA) * 100)

    st.subheader("📌 期間サマリ（月平均）")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("月平均支出", f"¥{int(monthly.mean()):,}")
    m2.metric(
        "平均エンゲル係数",
        f"{engel_series.mean():.1f}%" if engel_series.notna().any() else "—",
        help=f"食費÷総支出。日本の二人以上世帯の目安 約{ENGEL_BENCHMARK}%",
    )
    m3.metric(
        "平均固定費率",
        f"{fixed_ratio.mean():.1f}%" if fixed_ratio.notna().any() else "—",
        help="住居・通信・水道光熱・保険を固定費として算出",
    )
    m4.metric("データ件数", f"{len(df):,} 件")

    st.divider()

    # ── エンゲル係数の推移 ─────────────────────────────────────────────────
    st.subheader("🍚 エンゲル係数の推移")
    engel_df = (engel_series.reset_index()
                .rename(columns={"year_month": "年月", 0: "エンゲル係数(%)"}))
    engel_df.columns = ["年月", "エンゲル係数(%)"]
    engel_df["日本平均目安"] = ENGEL_BENCHMARK

    line = alt.Chart(engel_df).mark_line(point=True, color="#d97706").encode(
        x=alt.X("年月:O", sort=period_months, axis=alt.Axis(labelAngle=-45)),
        y=alt.Y("エンゲル係数(%):Q"),
        tooltip=["年月", alt.Tooltip("エンゲル係数(%):Q", format=".1f")],
    )
    bench = alt.Chart(engel_df).mark_rule(
        color="#64748b", strokeDash=[4, 4]
    ).encode(y="日本平均目安:Q")
    st.altair_chart(line + bench, use_container_width=True)
    st.caption(f"点線は総務省家計調査の目安（{ENGEL_BENCHMARK}% 前後）。外食比率が高いと自然に上がる点に注意。")

    st.divider()

    # ── 費目構成比の推移（100%積み上げ面） ─────────────────────────────────
    st.subheader("🧩 費目構成比の推移")
    cat_month = (df.groupby(["year_month", "category"])["amount"].sum()
                   .reset_index())
    stack = alt.Chart(cat_month).mark_area().encode(
        x=alt.X("year_month:O", title="年月",
                sort=period_months, axis=alt.Axis(labelAngle=-45)),
        y=alt.Y("amount:Q", stack="normalize",
                axis=alt.Axis(format=".0%"), title="構成比"),
        color=alt.Color("category:N", title="費目"),
        tooltip=["year_month", "category",
                 alt.Tooltip("amount:Q", format=",", title="金額")],
    )
    st.altair_chart(stack, use_container_width=True)

    st.divider()

    # ── 固定費 / 変動費 / 特別費 ──────────────────────────────────────────
    st.subheader("🏛️ 固定費 / 変動費 / 特別費")
    type_month = (df.groupby(["year_month", "type"])["amount"].sum()
                    .reset_index())
    bar = alt.Chart(type_month).mark_bar().encode(
        x=alt.X("year_month:O", title="年月",
                sort=period_months, axis=alt.Axis(labelAngle=-45)),
        y=alt.Y("amount:Q", title="金額（円）"),
        color=alt.Color(
            "type:N", title="区分",
            scale=alt.Scale(
                domain=["固定費", "変動費", "特別費"],
                range=["#2563eb", "#10b981", "#ef4444"],
            ),
        ),
        tooltip=["year_month", "type",
                 alt.Tooltip("amount:Q", format=",", title="金額")],
    )
    st.altair_chart(bar, use_container_width=True)

    st.divider()

    # ── 夫婦間バランス ────────────────────────────────────────────────────
    st.subheader("👫 夫婦間バランス（費目別）")
    rep_cat = (df.groupby(["category", "reporter"])["amount"].sum()
                 .unstack(fill_value=0))
    for col in ["夫", "妻"]:
        if col not in rep_cat.columns:
            rep_cat[col] = 0
    rep_cat["合計"] = rep_cat["夫"] + rep_cat["妻"]
    rep_cat = rep_cat.sort_values("合計", ascending=False)
    rep_long = rep_cat[["夫", "妻"]].reset_index().melt(
        id_vars="category", var_name="申告者", value_name="金額"
    )
    balance = alt.Chart(rep_long).mark_bar().encode(
        y=alt.Y("category:N", sort=rep_cat.index.tolist(), title="費目"),
        x=alt.X("金額:Q", stack="normalize",
                axis=alt.Axis(format=".0%"), title="構成比"),
        color=alt.Color(
            "申告者:N",
            scale=alt.Scale(domain=["夫", "妻"], range=["#3b82f6", "#ec4899"]),
        ),
        tooltip=["category", "申告者",
                 alt.Tooltip("金額:Q", format=",")],
    )
    st.altair_chart(balance, use_container_width=True)

    st.divider()

    # ── 日別ヒートマップ ──────────────────────────────────────────────────
    st.subheader("📅 日別支出ヒートマップ")
    daily = df.groupby("expense_date")["amount"].sum().reset_index()
    iso = daily["expense_date"].dt.isocalendar()
    daily["iso_year"] = iso.year.astype(int)
    daily["iso_week"] = iso.week.astype(int)
    daily["year_week"] = (
        daily["iso_year"].astype(str) + "-W" + daily["iso_week"].astype(str).str.zfill(2)
    )
    weekday_ja = {0: "月", 1: "火", 2: "水", 3: "木", 4: "金", 5: "土", 6: "日"}
    daily["曜日"] = daily["expense_date"].dt.weekday.map(weekday_ja)

    heat = alt.Chart(daily).mark_rect().encode(
        x=alt.X("year_week:O", title="週", axis=alt.Axis(labelAngle=-45)),
        y=alt.Y("曜日:O", sort=["月", "火", "水", "木", "金", "土", "日"]),
        color=alt.Color("amount:Q", title="支出",
                        scale=alt.Scale(scheme="reds")),
        tooltip=[alt.Tooltip("expense_date:T", title="日付"), "曜日",
                 alt.Tooltip("amount:Q", format=",", title="支出")],
    )
    st.altair_chart(heat, use_container_width=True)

    st.divider()

    # ── 前月比・前年同月比 ────────────────────────────────────────────────
    st.subheader("📊 前月比・前年同月比")
    all_monthly = df_all.groupby("year_month")["amount"].sum()
    latest_ym = end_ym
    latest_amt = int(all_monthly.get(latest_ym, 0))

    prev_ym = _prev_year_month(latest_ym)
    prev_amt = int(all_monthly.loc[prev_ym]) if prev_ym in all_monthly.index else None

    y, m = map(int, latest_ym.split("-"))
    py_ym = f"{y - 1}-{m:02d}"
    py_amt = int(all_monthly.loc[py_ym]) if py_ym in all_monthly.index else None

    c1, c2, c3 = st.columns(3)
    c1.metric(f"{latest_ym} 合計", f"¥{latest_amt:,}")
    if prev_amt is not None and prev_amt > 0:
        d = latest_amt - prev_amt
        c2.metric("前月比", f"¥{d:+,}",
                  delta=f"{(d / prev_amt * 100):+.1f}%", delta_color="inverse")
    else:
        c2.metric("前月比", "—")
    if py_amt is not None and py_amt > 0:
        d = latest_amt - py_amt
        c3.metric("前年同月比", f"¥{d:+,}",
                  delta=f"{(d / py_amt * 100):+.1f}%", delta_color="inverse")
    else:
        c3.metric("前年同月比", "—")
    st.caption("※ 支出は少ないほど良いので、増加を赤・減少を緑で表示しています。")


# --- 将来ビュータブ --------------------------------------------------------

def _render_future_analysis(df_all: pd.DataFrame) -> None:
    today = date.today()
    this_y, this_m = today.year, today.month
    this_ym = f"{this_y}-{this_m:02d}"
    days_in_month = monthrange(this_y, this_m)[1]
    elapsed_days = max(1, min(today.day, days_in_month))

    # ── 今月データと過去3ヶ月 ────────────────────────────────────────────
    df_this = df_all[df_all["year_month"] == this_ym]
    df_var = df_this[df_this["type"] == "変動費"]
    df_fix = df_this[df_this["type"].isin(["固定費", "特別費"])]

    var_so_far = int(df_var["amount"].sum())
    fix_so_far = int(df_fix["amount"].sum())
    total_so_far = var_so_far + fix_so_far

    past_ym = []
    for i in range(1, 4):
        y, m = this_y, this_m - i
        while m <= 0:
            y -= 1
            m += 12
        past_ym.append(f"{y}-{m:02d}")
    past_df = df_all[df_all["year_month"].isin(past_ym)]

    past_fix_avg_series = (past_df[past_df["type"].isin(["固定費", "特別費"])]
                           .groupby("year_month")["amount"].sum())
    past_fix_avg = int(past_fix_avg_series.mean()) if not past_fix_avg_series.empty else 0

    past_total_series = past_df.groupby("year_month")["amount"].sum()
    past_total_avg = int(past_total_series.mean()) if not past_total_series.empty else 0

    # 予測: 変動費は日割り延長、固定費は max(実績, 過去平均)
    var_projection = int(var_so_far * days_in_month / elapsed_days)
    fix_projection = max(fix_so_far, past_fix_avg)
    projection = var_projection + fix_projection

    # ── 月末着地予測 ──────────────────────────────────────────────────────
    st.subheader("🔮 今月の月末着地予測")
    st.caption(
        f"本日 {today.strftime('%Y-%m-%d')} ／ "
        f"{this_m}月は {days_in_month} 日（経過 {elapsed_days} 日）"
    )

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("現時点の支出", f"¥{total_so_far:,}")
    delta_str = (
        f"{projection - past_total_avg:+,}円 vs 過去3ヶ月平均"
        if past_total_avg else None
    )
    m2.metric("月末予測", f"¥{projection:,}", delta=delta_str, delta_color="inverse")
    m3.metric("　うち変動費（日割り）", f"¥{var_projection:,}",
              help="現時点の変動費実績を経過日数で割り、月末日数まで延長")
    m4.metric("　うち固定費（実績/平均）", f"¥{fix_projection:,}",
              help="当月実績と過去3ヶ月平均の大きい方を採用")

    st.caption(
        "変動費：当月実績 × (月の日数 / 経過日数) で延長。"
        "固定費・特別費：まだ未計上のものがある前提で、過去3ヶ月平均を下限にします。"
    )

    st.divider()

    # ── 月次支出と移動平均 ─────────────────────────────────────────────────
    st.subheader("📈 月次支出と移動平均")
    monthly_all = df_all.groupby("year_month")["amount"].sum().sort_index()
    ma = pd.DataFrame({
        "月次合計":       monthly_all,
        "3ヶ月移動平均": monthly_all.rolling(3).mean(),
        "6ヶ月移動平均": monthly_all.rolling(6).mean(),
    }).reset_index().rename(columns={"year_month": "年月"})
    ma_long = ma.melt(id_vars="年月", var_name="系列", value_name="金額").dropna()

    chart_ma = alt.Chart(ma_long).mark_line(point=True).encode(
        x=alt.X("年月:O", axis=alt.Axis(labelAngle=-45)),
        y=alt.Y("金額:Q", title="円"),
        color=alt.Color(
            "系列:N",
            scale=alt.Scale(
                domain=["月次合計", "3ヶ月移動平均", "6ヶ月移動平均"],
                range=["#94a3b8", "#2563eb", "#059669"],
            ),
        ),
        tooltip=["年月", "系列", alt.Tooltip("金額:Q", format=",")],
    )
    st.altair_chart(chart_ma, use_container_width=True)
    st.caption("単月のブレを除いた、生活コストの基準水準を把握するための指標です。")

    st.divider()

    # ── 年間実績と年末予測 ────────────────────────────────────────────────
    st.subheader("🗓️ 年間実績と年末予測")
    this_year_df = df_all[df_all["year"] == this_y]
    year_monthly = (this_year_df.groupby("month")["amount"].sum()
                    .reindex(range(1, 13), fill_value=0))

    ytd_actual = int(year_monthly.loc[:this_m - 1].sum()) if this_m > 1 else 0

    # 残月予測の基準: 直近12ヶ月（当月除く）の平均
    last_12 = (df_all[df_all["year_month"] != this_ym]
               .groupby("year_month")["amount"].sum()
               .sort_index().tail(12))
    monthly_avg_12 = int(last_12.mean()) if not last_12.empty else 0

    remaining_months = max(0, 12 - this_m)
    future_part = projection + monthly_avg_12 * remaining_months
    year_projection = ytd_actual + future_part

    y1, y2, y3 = st.columns(3)
    y1.metric(f"{this_y}年 1月〜{this_m - 1 if this_m > 1 else 0}月 実績",
              f"¥{ytd_actual:,}")
    y2.metric("今月予測+残月(平均)", f"¥{future_part:,}")
    y3.metric("年末着地予測", f"¥{year_projection:,}")

    year_chart_df = pd.DataFrame({
        "月":          [f"{m:02d}月" for m in range(1, 13)],
        "実績":        [int(year_monthly.loc[m]) if m < this_m else 0 for m in range(1, 13)],
        "当月予測":    [projection if m == this_m else 0 for m in range(1, 13)],
        "予測（平均）": [monthly_avg_12 if m > this_m else 0 for m in range(1, 13)],
    })
    year_long = year_chart_df.melt(id_vars="月", var_name="区分", value_name="金額")
    year_long = year_long[year_long["金額"] > 0]

    if not year_long.empty:
        year_chart = alt.Chart(year_long).mark_bar().encode(
            x=alt.X("月:N", sort=[f"{m:02d}月" for m in range(1, 13)]),
            y=alt.Y("金額:Q", title="円"),
            color=alt.Color(
                "区分:N",
                scale=alt.Scale(
                    domain=["実績", "当月予測", "予測（平均）"],
                    range=["#10b981", "#f59e0b", "#94a3b8"],
                ),
            ),
            tooltip=["月", "区分", alt.Tooltip("金額:Q", format=",")],
        )
        st.altair_chart(year_chart, use_container_width=True)

    st.caption(
        "残月は「直近12ヶ月（当月除く）の月平均」を流用した簡易予測です。"
        "ボーナス月・年末の特別費などは考慮しない点に注意。"
    )

    st.divider()

    # ── 気づきコメント ────────────────────────────────────────────────────
    st.subheader("💡 気づきコメント")
    notes = _generate_insights(df_all, this_ym, past_ym, projection, past_total_avg)
    if notes:
        for level, msg in notes:
            if level == "warn":
                st.warning(msg)
            elif level == "good":
                st.success(msg)
            else:
                st.info(msg)
    else:
        st.info("現時点では特筆すべき変化は見当たりません。")


def _generate_insights(
    df_all: pd.DataFrame,
    this_ym: str,
    past_ym: list[str],
    projection: int,
    past_total_avg: int,
) -> list[tuple[str, str]]:
    """ルールベースで家計の気づきを生成する。"""
    notes: list[tuple[str, str]] = []

    # 1) 月末予測の乖離
    if past_total_avg:
        diff_ratio = (projection - past_total_avg) / past_total_avg
        if diff_ratio >= 0.15:
            notes.append(("warn",
                f"今月の月末予測 ¥{projection:,} は過去3ヶ月平均 "
                f"¥{past_total_avg:,} より {diff_ratio * 100:.0f}% 多い見込みです。"))
        elif diff_ratio <= -0.15:
            notes.append(("good",
                f"今月の月末予測 ¥{projection:,} は過去3ヶ月平均 "
                f"¥{past_total_avg:,} より {abs(diff_ratio) * 100:.0f}% 少ない見込みです。"))

    # 2) 費目別の急増
    df_this = df_all[df_all["year_month"] == this_ym]
    df_past = df_all[df_all["year_month"].isin(past_ym)]
    if not df_past.empty and not df_this.empty:
        this_by_cat = df_this.groupby("category")["amount"].sum()
        n_past_months = df_past["year_month"].nunique()
        past_by_cat = df_past.groupby("category")["amount"].sum() / max(n_past_months, 1)
        for cat in this_by_cat.index:
            past_val = float(past_by_cat.get(cat, 0))
            this_val = float(this_by_cat[cat])
            if past_val >= 3000:  # 小さすぎる費目はノイズになるので除外
                r = (this_val - past_val) / past_val
                if r >= 0.30:
                    notes.append(("warn",
                        f"「{cat}」が過去3ヶ月平均（¥{int(past_val):,}）から "
                        f"{r * 100:.0f}% 増えています。"))

    # 3) 直近3ヶ月の連続トレンド
    monthly_all = df_all.groupby("year_month")["amount"].sum().sort_index()
    if len(monthly_all) >= 4:
        last4 = monthly_all.tail(4).values
        diffs = [last4[i + 1] - last4[i] for i in range(3)]
        if all(d > 0 for d in diffs):
            notes.append(("info", "月次支出が3ヶ月連続で増加しています。"))
        elif all(d < 0 for d in diffs):
            notes.append(("good", "月次支出が3ヶ月連続で減少しています 👏"))

    # 4) エンゲル係数の急変
    food_monthly = (df_all[df_all["category"] == "食費"]
                    .groupby("year_month")["amount"].sum())
    if len(monthly_all) >= 4:
        engel = (food_monthly.reindex(monthly_all.index, fill_value=0)
                 / monthly_all.replace(0, pd.NA) * 100).dropna()
        if len(engel) >= 4 and engel.index[-1] == this_ym:
            this_engel = engel.iloc[-1]
            past_engel = engel.iloc[-4:-1].mean()
            if past_engel > 0:
                diff = this_engel - past_engel
                if diff >= 5:
                    notes.append(("info",
                        f"エンゲル係数が直近3ヶ月平均 {past_engel:.1f}% から "
                        f"{this_engel:.1f}% に上昇しています。"))
                elif diff <= -5:
                    notes.append(("info",
                        f"エンゲル係数が直近3ヶ月平均 {past_engel:.1f}% から "
                        f"{this_engel:.1f}% に低下しています。"))

    return notes


# ---------------------------------------------------------------------------
# 申告履歴ページ（編集・削除）
# ---------------------------------------------------------------------------

def page_history() -> None:
    st.header("📋 申告履歴")
    db = get_db()

    now = datetime.now()
    col1, col2 = st.columns(2)
    with col1:
        year = st.selectbox(
            "年",
            list(range(now.year + 1, 2019, -1)),
            index=1,  # now.year が先頭から2番目（降順）
            key="hist_year",
        )
    with col2:
        month = st.selectbox(
            "月", list(range(1, 13)), index=now.month - 1,
            format_func=lambda m: f"{m}月", key="hist_month",
        )

    df = pd.read_sql_query(
        """SELECT id, expense_date, reporter, category, description, amount
           FROM expenses
           WHERE strftime('%Y', expense_date) = ?
             AND strftime('%m', expense_date) = ?
           ORDER BY expense_date DESC, created_at DESC""",
        db,
        params=(str(year), f"{month:02d}"),
    )

    if df.empty:
        st.info(f"{year}年{month}月のデータはありません。")
        return

    # ── 一覧テーブル ─────────────────────────────────────────────────────────
    display = df.rename(columns={
        "expense_date": "日付", "reporter": "申告者",
        "category": "費目", "description": "内容", "amount": "金額（円）",
    }).drop(columns=["id"]).copy()
    display["金額（円）"] = display["金額（円）"].apply(lambda x: f"¥{x:,}")
    st.dataframe(display, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("✏️ 編集・削除")

    # ログインユーザーの属性・権限を取得
    _me          = st.session_state.user or {}
    _is_admin    = bool(_me.get("is_admin", 0))
    _my_reporter = _me.get("reporter", "")

    # 編集・削除できるレコードを絞り込む（admin は全件、一般ユーザーは自分属性のみ）
    row_lookup = df.set_index("id").to_dict("index")
    if _is_admin or not _my_reporter:
        editable_ids = df["id"].tolist()
    else:
        editable_ids = df[df["reporter"] == _my_reporter]["id"].tolist()

    if not editable_ids:
        st.info(f"この月に編集・削除できる申告（{_my_reporter}）がありません。")
        return

    selected_id = st.selectbox(
        "対象の申告を選択",
        options=editable_ids,
        format_func=lambda x: (
            f"{row_lookup[x]['expense_date']}　"
            f"{row_lookup[x]['reporter']}　"
            f"{row_lookup[x]['category']}　"
            f"¥{int(row_lookup[x]['amount']):,}"
            + (f"　({row_lookup[x]['description']})" if row_lookup[x]["description"] else "")
        ),
    )

    if selected_id is None:
        return

    row = row_lookup[selected_id]

    col_form, col_del = st.columns([3, 1])

    # ── 編集フォーム ─────────────────────────────────────────────────────────
    with col_form:
        try:
            current_date = datetime.strptime(row["expense_date"], "%Y-%m-%d").date()
        except Exception:
            current_date = date.today()

        cat_idx = CATEGORIES.index(row["category"]) if row["category"] in CATEGORIES else 0

        with st.form("edit_expense_form"):
            fcol_l, fcol_r = st.columns(2)
            with fcol_l:
                new_date   = st.date_input("📅 日付", value=current_date, format="YYYY/MM/DD")
                # admin のみ申告者を変更可、一般ユーザーは固定表示
                if _is_admin:
                    rep_idx      = REPORTERS.index(row["reporter"]) if row["reporter"] in REPORTERS else 0
                    new_reporter = st.segmented_control("👤 申告者", options=REPORTERS, default=REPORTERS[rep_idx])
                else:
                    st.caption(f"👤 申告者: **{row['reporter']}**")
                    new_reporter = row["reporter"]
                new_amount = st.number_input(
                    "💴 金額（円）", value=int(row["amount"]),
                    min_value=1, max_value=10_000_000, step=100,
                )
            with fcol_r:
                new_category    = st.selectbox("🏷️ 費目", CATEGORIES, index=cat_idx)
                new_description = st.text_area(
                    "📝 内容（任意）",
                    value=row["description"] or "",
                    height=148,
                )

            if st.form_submit_button("💾 保存する", type="primary", use_container_width=True):
                db.execute(
                    "UPDATE expenses"
                    " SET expense_date=?, reporter=?, description=?, category=?, amount=?"
                    " WHERE id=?",
                    (
                        str(new_date), new_reporter or row["reporter"],
                        new_description.strip() if new_description else "",
                        new_category, int(new_amount), selected_id,
                    ),
                )
                db.commit()
                _sync(db)
                st.success("✅ 更新しました。")
                st.rerun()

    # ── 削除（二段階確認） ───────────────────────────────────────────────────
    with col_del:
        st.markdown("　")
        st.markdown("**🗑️ 削除**")

        confirm_key = f"confirm_del_{selected_id}"

        if not st.session_state.get(confirm_key, False):
            if st.button("この申告を削除", type="secondary", use_container_width=True):
                st.session_state[confirm_key] = True
                st.rerun()
        else:
            st.warning("本当に削除しますか？")
            if st.button("✅ 削除する", type="primary", use_container_width=True):
                db.execute("DELETE FROM expenses WHERE id=?", (selected_id,))
                db.commit()
                _sync(db)
                st.session_state.pop(confirm_key, None)
                st.success("削除しました。")
                st.rerun()
            if st.button("キャンセル", use_container_width=True):
                st.session_state.pop(confirm_key, None)
                st.rerun()


# ---------------------------------------------------------------------------
# ユーザー管理ページ（管理者専用）
# ---------------------------------------------------------------------------

def page_user_management() -> None:
    st.header("👥 ユーザー管理")
    db = get_db()

    # ── ユーザー一覧 ────────────────────────────────────────────────────────
    st.subheader("登録ユーザー一覧")
    users_df = pd.read_sql_query(
        "SELECT id, username, reporter, is_admin, created_at FROM users ORDER BY id", db
    )
    users_df.columns = ["ID", "ユーザー名", "属性", "権限", "作成日時"]
    users_df["権限"] = users_df["権限"].map({1: "🔑 管理者", 0: "👤 ユーザー"})
    users_df["属性"] = users_df["属性"].apply(
        lambda v: {"夫": "🧔 夫", "妻": "👩 妻"}.get(v, "─")
    )
    st.dataframe(users_df, use_container_width=True, hide_index=True)

    st.divider()

    # ユーザー名リストを先に取得（複数セクションで再利用）
    all_usernames = _fetch_col(
        db.execute("SELECT username FROM users ORDER BY id")
    )

    col_a, col_b = st.columns(2)

    # ── 新規ユーザー登録 ────────────────────────────────────────────────────
    with col_a:
        st.subheader("新規ユーザー登録")
        with st.form("add_user_form"):
            new_name     = st.text_input("ユーザー名")
            new_pw       = st.text_input("パスワード", type="password")
            new_pw2      = st.text_input("パスワード（確認）", type="password")
            new_reporter = st.segmented_control(
                "属性", options=REPORTERS, default=REPORTERS[0],
                help="申告者として紐付ける属性。自分の属性のレコードのみ編集・削除できます。"
            )
            new_admin    = st.checkbox("管理者権限を付与する（属性制限なし）")

            if st.form_submit_button("登録する", type="primary", use_container_width=True):
                if not new_name or not new_pw:
                    st.error("ユーザー名とパスワードを入力してください。")
                elif new_pw != new_pw2:
                    st.error("パスワードが一致しません。")
                elif len(new_pw) < 6:
                    st.error("パスワードは6文字以上にしてください。")
                else:
                    try:
                        db.execute(
                            "INSERT INTO users (username, password_hash, is_admin, reporter)"
                            " VALUES (?,?,?,?)",
                            (new_name, _hash(new_pw), 1 if new_admin else 0,
                             new_reporter or ""),
                        )
                        db.commit()
                        _sync(db)
                        st.success(f"✅ ユーザー「{new_name}」を登録しました。")
                        st.rerun()
                    except Exception as e:
                        if _is_unique_error(e):
                            st.error("そのユーザー名はすでに使用されています。")
                        else:
                            st.error(f"エラーが発生しました: {e}")

    # ── パスワード変更 ──────────────────────────────────────────────────────
    with col_b:
        st.subheader("パスワード変更")
        with st.form("change_pw_form"):
            target  = st.selectbox("対象ユーザー", all_usernames)
            chg_pw  = st.text_input("新しいパスワード", type="password")
            chg_pw2 = st.text_input("新しいパスワード（確認）", type="password")

            if st.form_submit_button("変更する", use_container_width=True):
                if not chg_pw:
                    st.error("パスワードを入力してください。")
                elif chg_pw != chg_pw2:
                    st.error("パスワードが一致しません。")
                elif len(chg_pw) < 6:
                    st.error("パスワードは6文字以上にしてください。")
                else:
                    db.execute(
                        "UPDATE users SET password_hash=? WHERE username=?",
                        (_hash(chg_pw), target),
                    )
                    db.commit()
                    _sync(db)
                    st.success(f"✅「{target}」のパスワードを変更しました。")

    # ── ユーザー削除 ────────────────────────────────────────────────────────
    st.divider()
    st.subheader("ユーザー削除")

    deletable = [u for u in all_usernames if u != "admin"]
    if deletable:
        del_target = st.selectbox("削除するユーザー", deletable)
        if st.button(f"「{del_target}」を削除する", type="secondary"):
            db.execute("DELETE FROM users WHERE username=?", (del_target,))
            db.commit()
            _sync(db)
            st.success(f"✅ ユーザー「{del_target}」を削除しました。")
            st.rerun()
    else:
        st.info("admin 以外に削除できるユーザーがいません。")


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(
        page_title="家計管理アプリ",
        page_icon="🏠",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    if "user" not in st.session_state:
        st.session_state.user = None

    # 未ログインならログイン画面を表示
    if not st.session_state.user:
        page_login()
        return

    user = st.session_state.user

    # ── サイドバー ──────────────────────────────────────────────────────────
    with st.sidebar:
        st.title("🏠 家計管理")
        st.caption(f"ログイン中: **{user['username']}**")
        if user["is_admin"]:
            st.caption("🔑 管理者権限あり")
        st.divider()

        menu: dict[str, str] = {
            "💰 支出申告":   "expense",
            "📋 履歴・編集": "history",
            "📊 集計":       "aggregation",
            "📈 分析":       "analysis",
        }
        if user["is_admin"]:
            menu["👥 ユーザー管理"] = "users"

        choice = st.radio("メニュー", list(menu.keys()), label_visibility="collapsed")

        st.divider()
        if st.button("🚪 ログアウト", use_container_width=True):
            st.session_state.user = None
            st.rerun()

    # ── ページ振り分け ──────────────────────────────────────────────────────
    page_key = menu[choice]
    if page_key == "expense":
        page_expense_entry()
    elif page_key == "history":
        page_history()
    elif page_key == "aggregation":
        page_aggregation()
    elif page_key == "analysis":
        page_analysis()
    elif page_key == "users":
        page_user_management()


if __name__ == "__main__":
    main()
