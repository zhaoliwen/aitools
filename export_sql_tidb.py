"""
从 TiDB Cloud 导出库结构 + 数据为 .sql（TLS，与 import_sql_tidb.py 同一套连接方式）。

Requirements:
  pip install pymysql

启动后先弹出可编辑确认窗，点击「执行」后才开始导出。
默认输出：G:\\文件\\bootdo-tidebase-YYYYMMDDHHmm.sql
"""

from __future__ import annotations

import ssl
import sys
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pymysql
from pymysql.err import OperationalError

# ---------- 连接辅助（与 import_sql_tidb.py 一致） ----------


def _latin1_ok(label_cn: str, value: str) -> str | None:
    try:
        value.encode("latin1")
    except UnicodeEncodeError:
        return (
            f"{label_cn} 含有非 Latin-1 字符，无法用于当前 pymysql 连接。"
            " 请使用 TiDB「Connect / Parameters」里通过复制按钮得到的真实用户名与密码。"
        )
    return None


def _connect_access_denied_hint(exc: OperationalError) -> str | None:
    import re

    if not exc.args:
        return None
    code = exc.args[0]
    raw = exc.args[1] if len(exc.args) > 1 else str(exc)
    if code == 1045:
        m = re.search(r"@'([^']+)'", raw)
        ip = m.group(1) if m else "（见报错中的客户端地址）"
        return (
            "连接被拒绝（MySQL 1045）：用户名/密码不对，或执行脚本机器的公网 IP 未加入白名单。\n"
            f"  • 白名单出口 IP（当前为 {ip}）需在 TiDB Cloud Trusted sources 中单独添加。\n"
            "  • 密码请从 Connect → Parameters 用「复制」粘贴。\n"
            f"原始错误：{raw}"
        )
    if code == 1105:
        return (
            "TiDB 返回 1105：账号或密码与当前集群不匹配（或缺少/错误的前缀用户名）。\n"
            "请打开该集群的 Connect 对话框，复制其中的完整用户名（如 prefix.root）与密码后重试。\n"
            f"原始错误：{raw}"
        )
    return None


def tls_context(ca_pem: str | None):
    if ca_pem:
        ctx = ssl.create_default_context(cafile=ca_pem)
        return ctx
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def connect_tidb(*, host: str, port: int, user: str, password: str, database: str, ssl_ca: str | None):
    user = user.strip()
    password = password.strip()
    database = (database or "").strip()

    if msg := _latin1_ok("用户名", user):
        raise RuntimeError(msg)
    if msg := _latin1_ok("密码", password):
        raise RuntimeError(msg)
    if not database:
        raise RuntimeError("数据库名称不能为空。")

    ssl_ctx = tls_context(ssl_ca)
    try:
        return pymysql.connect(
            host=host.strip(),
            port=port,
            user=user,
            password=password,
            database=database,
            charset="utf8mb4",
            autocommit=True,
            ssl=ssl_ctx,
        )
    except OperationalError as e:
        if hint := _connect_access_denied_hint(e):
            raise RuntimeError(hint) from e
        raise


# ---------- SQL 导出 ----------


def _ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return "NULL"
        return repr(value)
    if isinstance(value, datetime):
        return "'" + value.strftime("%Y-%m-%d %H:%M:%S") + "'"
    if isinstance(value, date):
        return "'" + value.strftime("%Y-%m-%d") + "'"
    if isinstance(value, timedelta):
        total = int(value.total_seconds())
        sign = "-" if total < 0 else ""
        total = abs(total)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"'{sign}{h:02d}:{m:02d}:{s:02d}'"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex() if value else "''"
    text = str(value)
    escaped = (
        text.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\x00", "\\0")
    )
    return f"'{escaped}'"


def list_base_tables(cur) -> list[str]:
    cur.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
    return [row[0] for row in cur.fetchall()]


def list_views(cur) -> list[str]:
    cur.execute("SHOW FULL TABLES WHERE Table_type = 'VIEW'")
    return [row[0] for row in cur.fetchall()]


def _insertable_columns(cur, table: str) -> list[str]:
    cur.execute(f"SHOW COLUMNS FROM {_ident(table)}")
    cols = []
    for row in cur.fetchall():
        extra = (row[5] or "").upper()
        if "GENERATED" in extra or "VIRTUAL" in extra:
            continue
        cols.append(row[0])
    return cols


def dump_table(conn, cur, table: str, out, batch_size: int, log) -> int:
    ident = _ident(table)
    cur.execute(f"SHOW CREATE TABLE {ident}")
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"无法获取表结构: {table}")
    create_sql = row[1].rstrip().rstrip(";")
    out.write(f"DROP TABLE IF EXISTS {ident};\n")
    out.write(f"{create_sql};\n\n")

    cols = _insertable_columns(cur, table)
    if not cols:
        log(f"  表 {table}: 无可插入列，仅导出结构")
        return 0

    col_sql = ", ".join(_ident(c) for c in cols)
    cur.execute(f"SELECT {col_sql} FROM {ident}")
    total = 0
    while True:
        rows = cur.fetchmany(batch_size)
        if not rows:
            break
        out.write(f"INSERT INTO {ident} ({col_sql}) VALUES\n")
        lines = []
        for r in rows:
            lines.append("(" + ", ".join(_sql_literal(v) for v in r) + ")")
        out.write(",\n".join(lines))
        out.write(";\n")
        total += len(rows)
    out.write("\n")
    log(f"  表 {table}: {total} 行")
    return total


def dump_view(cur, view: str, out, log) -> None:
    ident = _ident(view)
    cur.execute(f"SHOW CREATE VIEW {ident}")
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"无法获取视图定义: {view}")
    create_sql = row[1].rstrip().rstrip(";")
    out.write(f"DROP VIEW IF EXISTS {ident};\n")
    out.write(f"{create_sql};\n\n")
    log(f"  视图 {view}")


def export_database(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    sql_file: Path,
    ssl_ca: str | None,
    batch_size: int,
    log,
) -> None:
    sql_file = Path(sql_file)
    sql_file.parent.mkdir(parents=True, exist_ok=True)

    log(f"连接 {host}:{port} / {database} ...")
    conn = connect_tidb(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        ssl_ca=ssl_ca,
    )
    cur = conn.cursor()
    try:
        tables = list_base_tables(cur)
        views = list_views(cur)
        log(f"基表 {len(tables)} 张，视图 {len(views)} 个")
        log(f"写入 {sql_file}")

        with sql_file.open("w", encoding="utf-8", newline="\n") as out:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            out.write(f"-- TiDB dump\n-- Database: {database}\n-- Date: {now}\n\n")
            out.write("SET NAMES utf8mb4;\n")
            out.write("SET FOREIGN_KEY_CHECKS=0;\n\n")

            total_rows = 0
            for i, table in enumerate(tables, 1):
                log(f"[{i}/{len(tables)}] 导出表 {table}")
                total_rows += dump_table(conn, cur, table, out, batch_size, log)

            for i, view in enumerate(views, 1):
                log(f"[视图 {i}/{len(views)}] {view}")
                dump_view(cur, view, out, log)

            out.write("SET FOREIGN_KEY_CHECKS=1;\n")

        log(f"完成：{len(tables)} 张表，{total_rows} 行，文件 {sql_file}")
    finally:
        cur.close()
        conn.close()


# ---------- GUI ----------

# 与 import_sql_tidb.py 注释中的目标库一致，可在窗口里改
DEFAULT_HOST = "gateway01.ap-southeast-1.prod.alicloud.tidbcloud.com"
DEFAULT_PORT = "4000"
DEFAULT_USER = "2AtNsm9Nf83Xr7d.root"
DEFAULT_PASSWORD = "XqItDX318Yae1BPg"
DEFAULT_DATABASE = "bootdo"
DEFAULT_OUT_DIR = Path(r"G:\文件")


def default_sql_path(database: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d%H%M")
    name = f"{database.strip() or 'bootdo'}-tidebase-{stamp}.sql"
    return DEFAULT_OUT_DIR / name


def run_gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    here = Path(__file__).resolve().parent
    default_ca = here / "isrgrootx1_ca.pem"

    root = tk.Tk()
    root.title("导出 TiDB 数据库")
    root.minsize(640, 520)
    root.geometry("720x580")

    try:
        root.tk.call("tk", "scaling", 1.2)
    except tk.TclError:
        pass

    pad = {"padx": 10, "pady": 4}

    frm = ttk.Frame(root, padding=12)
    frm.pack(fill=tk.BOTH, expand=True)
    frm.columnconfigure(1, weight=1)

    host_var = tk.StringVar(value=DEFAULT_HOST)
    port_var = tk.StringVar(value=DEFAULT_PORT)
    user_var = tk.StringVar(value=DEFAULT_USER)
    password_var = tk.StringVar(value=DEFAULT_PASSWORD)
    database_var = tk.StringVar(value=DEFAULT_DATABASE)
    sql_var = tk.StringVar(value=str(default_sql_path(DEFAULT_DATABASE)))
    ssl_ca_var = tk.StringVar(value=str(default_ca) if default_ca.is_file() else "")

    def add_row(row: int, label: str, var: tk.StringVar, *, show: str | None = None) -> ttk.Entry:
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="e", **pad)
        entry = ttk.Entry(frm, textvariable=var, show=show or "")
        entry.grid(row=row, column=1, sticky="ew", **pad)
        return entry

    add_row(0, "主机", host_var)
    add_row(1, "端口", port_var)
    add_row(2, "用户名", user_var)

    ttk.Label(frm, text="密码").grid(row=3, column=0, sticky="e", **pad)
    pwd_row = ttk.Frame(frm)
    pwd_row.grid(row=3, column=1, sticky="ew", **pad)
    pwd_row.columnconfigure(0, weight=1)
    pwd_entry = ttk.Entry(pwd_row, textvariable=password_var, show="*")
    pwd_entry.grid(row=0, column=0, sticky="ew")
    pwd_visible = {"on": False}

    def toggle_password() -> None:
        pwd_visible["on"] = not pwd_visible["on"]
        if pwd_visible["on"]:
            pwd_entry.configure(show="")
            eye_btn.configure(text="🙈")
        else:
            pwd_entry.configure(show="*")
            eye_btn.configure(text="👁")

    eye_btn = ttk.Button(pwd_row, text="👁", width=3, command=toggle_password)
    eye_btn.grid(row=0, column=1, padx=(6, 0))

    add_row(4, "数据库", database_var)

    ttk.Label(frm, text="输出文件").grid(row=5, column=0, sticky="e", **pad)
    out_row = ttk.Frame(frm)
    out_row.grid(row=5, column=1, sticky="ew", **pad)
    out_row.columnconfigure(0, weight=1)
    ttk.Entry(out_row, textvariable=sql_var).grid(row=0, column=0, sticky="ew")

    def browse_sql() -> None:
        path = filedialog.asksaveasfilename(
            title="选择导出 SQL 文件",
            defaultextension=".sql",
            filetypes=[("SQL", "*.sql"), ("全部", "*.*")],
            initialfile=Path(sql_var.get()).name if sql_var.get() else "",
            initialdir=str(DEFAULT_OUT_DIR) if DEFAULT_OUT_DIR.is_dir() else str(here),
        )
        if path:
            sql_var.set(path)

    def refresh_sql_name() -> None:
        sql_var.set(str(default_sql_path(database_var.get())))

    ttk.Button(out_row, text="浏览…", command=browse_sql).grid(row=0, column=1, padx=(6, 0))
    ttk.Button(out_row, text="按库名刷新文件名", command=refresh_sql_name).grid(row=0, column=2, padx=(6, 0))

    add_row(6, "SSL CA（可选）", ssl_ca_var)

    hint = ttk.Label(
        frm,
        text="确认以上信息后点击「执行」。密码默认隐藏，点右侧眼睛可切换可见。",
        foreground="#555",
    )
    hint.grid(row=7, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 4))

    btn_row = ttk.Frame(frm)
    btn_row.grid(row=8, column=0, columnspan=2, sticky="w", padx=10, pady=8)
    run_btn = ttk.Button(btn_row, text="执行")
    run_btn.pack(side=tk.LEFT)
    close_btn = ttk.Button(btn_row, text="关闭", command=root.destroy)
    close_btn.pack(side=tk.LEFT, padx=(8, 0))

    log_box = scrolledtext.ScrolledText(frm, height=14, wrap=tk.WORD, state=tk.DISABLED)
    log_box.grid(row=9, column=0, columnspan=2, sticky="nsew", padx=10, pady=(4, 0))
    frm.rowconfigure(9, weight=1)

    def log(msg: str) -> None:
        def _append() -> None:
            log_box.configure(state=tk.NORMAL)
            log_box.insert(tk.END, msg + "\n")
            log_box.see(tk.END)
            log_box.configure(state=tk.DISABLED)

        root.after(0, _append)
        print(msg)

    def set_running(running: bool) -> None:
        run_btn.configure(state=tk.DISABLED if running else tk.NORMAL)

    def do_export() -> None:
        host = host_var.get().strip()
        user = user_var.get().strip()
        password = password_var.get()
        database = database_var.get().strip()
        sql_file = sql_var.get().strip()
        ssl_ca = ssl_ca_var.get().strip() or None
        try:
            port = int(port_var.get().strip())
        except ValueError:
            messagebox.showerror("参数错误", "端口必须是数字。")
            return
        if not host or not user or not database or not sql_file:
            messagebox.showerror("参数错误", "主机、用户名、数据库、输出文件均不能为空。")
            return
        if ssl_ca and not Path(ssl_ca).is_file():
            messagebox.showerror("参数错误", f"SSL CA 文件不存在：{ssl_ca}")
            return

        out_path = Path(sql_file)
        if out_path.exists():
            if not messagebox.askyesno("覆盖确认", f"文件已存在，是否覆盖？\n{out_path}"):
                return

        set_running(True)
        log_box.configure(state=tk.NORMAL)
        log_box.delete("1.0", tk.END)
        log_box.configure(state=tk.DISABLED)

        def worker() -> None:
            err: BaseException | None = None
            try:
                export_database(
                    host=host,
                    port=port,
                    user=user,
                    password=password,
                    database=database,
                    sql_file=out_path,
                    ssl_ca=ssl_ca,
                    batch_size=200,
                    log=log,
                )
            except BaseException as e:
                err = e
                log(str(e))

            def done() -> None:
                set_running(False)
                if err is None:
                    messagebox.showinfo("完成", f"已导出到：\n{out_path}")
                else:
                    messagebox.showerror("导出失败", str(err))

            root.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    run_btn.configure(command=do_export)
    root.mainloop()
    return 0


def main() -> int:
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
