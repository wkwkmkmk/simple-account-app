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
from datetime import date, datetime

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
    elif page_key == "users":
        page_user_management()


if __name__ == "__main__":
    main()
