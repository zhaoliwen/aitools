"""
将 TiDB 库中的全部基表（结构 + 数据）导出为 .sql。

连接方式、默认目标库与 copy_tidb_db.py 一致：TiDB Cloud 公网网关需 TLS；
可用 SSL CA（如 isrgrootx1_ca.pem）做证书校验。

启动后先弹出可编辑确认窗，点击「执行」后才开始导出。
默认输出：G:\\文件\\vitaband_test-tidebase-YYYYMMDDHHmm.sql

无界面（计划任务）：
  py -3 export_sql_tidb.py --config "G:\\文件\\export_tidb_config.json"
  每次会按当前时间刷新输出文件名；若必须沿用配置里的路径，加 --keep-sql-path。
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pymysql
from pymysql.err import OperationalError

# ---------- 以下连接辅助逻辑自 copy_tidb_db.py 复制并沿用 ----------


def _latin1_ok(label_cn: str, value: str) -> str | None:
    """pymysql 建连时会把用户名/口令按 latin-1 编码；含非 Latin-1 会触发 UnicodeEncodeError。"""
    try:
        value.encode("latin1")
    except UnicodeEncodeError:
        return (
            f"{label_cn} 含有非 Latin-1 字符，无法用于当前 pymysql 连接。"
            " 请使用 TiDB「Connect / Parameters」里通过复制按钮得到的真实用户名与密码。"
        )
    return None


def _connect_access_denied_hint(exc: OperationalError) -> str | None:
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
            "TiDB 返回 1105：当前用户名/密码未被该 Host 接受（与 TLS、是否指定库无关）。\n"
            "  1. Host、Username、Password 必须来自同一集群的 Connect 对话框（共享网关靠前缀区分实例）。\n"
            "  2. 在 Connect 里点 Generate Password 重新生成，立刻用「复制」粘贴（不要手打、不要用旧截图）。\n"
            "  3. Username 必须是「前缀.root」完整串，不要只写 root。\n"
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


def connect_tidb(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str | None,
    ssl_ca: str | None,
    label: str,
):
    """建立 TiDB TLS 连接（与 copy_tidb_db.py 相同）。"""
    user = user.strip()
    password = password.strip()
    if database is not None:
        database = database.strip() or None

    if msg := _latin1_ok(f"{label} 用户名", user):
        raise RuntimeError(msg)
    if msg := _latin1_ok(f"{label} 密码", password):
        raise RuntimeError(msg)

    ssl_ctx = tls_context(ssl_ca)
    try:
        return pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            charset="utf8mb4",
            autocommit=False,
            ssl=ssl_ctx,
        )
    except OperationalError as e:
        if hint := _connect_access_denied_hint(e):
            raise RuntimeError(f"[{label}] {hint}") from e
        raise


def list_base_tables(cur) -> list[str]:
    cur.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
    # 列名形如 Tables_in_xxx / Table_type
    return [row[0] for row in cur.fetchall()]


def list_views(cur) -> list[str]:
    cur.execute("SHOW FULL TABLES WHERE Table_type = 'VIEW'")
    return [row[0] for row in cur.fetchall()]


# ---------- 导出逻辑（表结构/数据遍历方式与 copy_tidb_db.py 一致） ----------


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
        if value.microsecond:
            return "'" + value.strftime("%Y-%m-%d %H:%M:%S.%f") + "'"
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
        raw = bytes(value)
        return "0x" + raw.hex() if raw else "''"
    text = str(value)
    escaped = (
        text.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\x00", "\\0")
    )
    return f"'{escaped}'"


def dump_table_structure(cur, table: str, out) -> None:
    cur.execute(f"SHOW CREATE TABLE `{table}`")
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"无法获取表结构: {table}")
    create_sql = row[1].rstrip().rstrip(";")
    out.write(f"DROP TABLE IF EXISTS `{table}`;\n")
    out.write(f"{create_sql};\n\n")


def dump_table_data(cur, table: str, out, batch_size: int) -> int:
    cur.execute(f"SELECT * FROM `{table}`")
    cols = [d[0] for d in cur.description]
    if not cols:
        return 0

    col_list = ", ".join(f"`{c}`" for c in cols)
    total = 0
    while True:
        rows = cur.fetchmany(batch_size)
        if not rows:
            break
        out.write(f"INSERT INTO `{table}` ({col_list}) VALUES\n")
        lines = ["(" + ", ".join(_sql_literal(v) for v in r) + ")" for r in rows]
        out.write(",\n".join(lines))
        out.write(";\n")
        total += len(rows)
    if total:
        out.write("\n")
    return total


def dump_view(cur, view: str, out) -> None:
    cur.execute(f"SHOW CREATE VIEW `{view}`")
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"无法获取视图定义: {view}")
    # SHOW CREATE VIEW 返回: View, Create View, character_set_client, collation_connection
    create_sql = row[1].rstrip().rstrip(";")
    out.write(f"DROP VIEW IF EXISTS `{view}`;\n")
    out.write(f"{create_sql};\n\n")


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
    include_views: bool,
    log,
) -> None:
    database = database.strip()
    if database.lower() == "sys":
        raise RuntimeError("拒绝导出系统库 sys，请指定业务库（默认 vitaband_test）。")

    sql_file = Path(sql_file)
    sql_file.parent.mkdir(parents=True, exist_ok=True)

    log(f"连接目标库 {host}/{database} ...")
    conn = connect_tidb(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        ssl_ca=ssl_ca,
        label="目标库",
    )
    cur = conn.cursor()
    try:
        tables = list_base_tables(cur)
        log(f"目标库基表数量: {len(tables)}")
        if not tables:
            log("目标库没有基表，结束。")
            return

        log(f"写入 {sql_file}")
        with sql_file.open("w", encoding="utf-8", newline="\n") as out:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            out.write(f"-- TiDB dump\n-- Database: {database}\n-- Date: {now}\n\n")
            out.write("SET NAMES utf8mb4;\n")
            out.write("SET FOREIGN_KEY_CHECKS=0;\n\n")

            total_rows = 0
            for i, table in enumerate(tables, 1):
                log(f"[{i}/{len(tables)}] 导出表结构: {table}")
                dump_table_structure(cur, table, out)

                log(f"[{i}/{len(tables)}] 导出表数据: {table} ...")
                n = dump_table_data(cur, table, out, batch_size)
                total_rows += n
                log(f"  {n} 行")

            if include_views:
                views = list_views(cur)
                log(f"目标库视图数量: {len(views)}")
                for i, view in enumerate(views, 1):
                    log(f"[视图 {i}/{len(views)}] {view}")
                    dump_view(cur, view, out)

            out.write("SET FOREIGN_KEY_CHECKS=1;\n")

        log(f"全部完成。{len(tables)} 张表，{total_rows} 行，文件 {sql_file}")
    finally:
        cur.close()
        conn.close()


# ---------- GUI ----------

# 与 copy_tidb_db.py 目标库（vitaband_test）一致；密码不预填，从 Connect 复制后粘贴
DEFAULT_HOST = "gateway01.sa-east-1.prod.aws.tidbcloud.com"
DEFAULT_PORT = "4000"
DEFAULT_USER = "ug21YiZX1lSH2pL.root"
DEFAULT_PASSWORD = ""
DEFAULT_DATABASE = "vitaband_test"
DEFAULT_BATCH_SIZE = "1000"
DEFAULT_OUT_DIR = Path(r"G:\文件")


def default_sql_path(database: str, out_dir: Path | None = None) -> Path:
    stamp = datetime.now().strftime("%Y%m%d%H%M")
    name = f"{database.strip() or 'vitaband_test'}-tidebase-{stamp}.sql"
    return (out_dir or DEFAULT_OUT_DIR) / name


_SQL_STAMP_NAME = re.compile(r"^(.*-tidebase-)\d{12}(\.sql)$", re.IGNORECASE)


def refresh_sql_file_stamp(sql_file: str, database: str) -> str:
    """只替换文件名里的 YYYYMMDDHHmm，目录和前缀保持配置里的。"""
    stamp = datetime.now().strftime("%Y%m%d%H%M")
    if sql_file:
        path = Path(sql_file)
        parent = path.parent
        m = _SQL_STAMP_NAME.match(path.name)
        if m:
            return str(parent / f"{m.group(1)}{stamp}{m.group(2)}")
        return str(default_sql_path(database, parent))
    return str(default_sql_path(database))


def load_config_file(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("配置文件内容必须是 JSON 对象。")
    return data


def parse_export_config(data: dict, *, refresh_sql_stamp: bool) -> dict:
    host = str(data.get("host") or "").strip()
    user = str(data.get("user") or "").strip()
    password = str(data.get("password") or "")
    database = str(data.get("database") or "").strip()
    sql_file = str(data.get("sql_file") or "").strip()
    ssl_ca = str(data.get("ssl_ca") or "").strip() or None
    try:
        port = int(str(data.get("port") or "").strip())
        batch_size = int(str(data.get("batch_size") or DEFAULT_BATCH_SIZE).strip())
    except ValueError as e:
        raise ValueError("端口和每批行数必须是数字。") from e
    if batch_size <= 0:
        raise ValueError("每批行数必须大于 0。")
    if not host or not user or not password.strip() or not database:
        raise ValueError("主机、用户名、密码、数据库均不能为空。")
    if ssl_ca and not Path(ssl_ca).is_file():
        raise ValueError(f"SSL CA 文件不存在：{ssl_ca}")

    if refresh_sql_stamp or not sql_file:
        sql_file = refresh_sql_file_stamp(sql_file, database)
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "database": database,
        "sql_file": Path(sql_file),
        "ssl_ca": ssl_ca,
        "batch_size": batch_size,
        "include_views": bool(data.get("include_views")),
    }


def run_from_config(config_path: Path, *, refresh_sql_stamp: bool) -> int:
    cfg_path = config_path.expanduser().resolve()
    if not cfg_path.is_file():
        print(f"找不到配置文件: {cfg_path}", file=sys.stderr)
        return 2
    try:
        data = load_config_file(cfg_path)
        args = parse_export_config(data, refresh_sql_stamp=refresh_sql_stamp)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"使用配置: {cfg_path}")
    print(f"输出文件: {args['sql_file']}")
    export_database(
        host=args["host"],
        port=args["port"],
        user=args["user"],
        password=args["password"],
        database=args["database"],
        sql_file=args["sql_file"],
        ssl_ca=args["ssl_ca"],
        batch_size=args["batch_size"],
        include_views=args["include_views"],
        log=print,
    )
    return 0


def run_gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    here = Path(__file__).resolve().parent
    default_ca = here / "isrgrootx1_ca.pem"

    root = tk.Tk()
    root.title("导出 TiDB 数据库")
    root.minsize(640, 560)
    root.geometry("740x620")

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
    batch_var = tk.StringVar(value=DEFAULT_BATCH_SIZE)
    include_views_var = tk.BooleanVar(value=False)

    def add_row(row: int, label: str, var: tk.StringVar) -> ttk.Entry:
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="e", **pad)
        entry = ttk.Entry(frm, textvariable=var)
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

    add_row(4, "数据库名称", database_var)

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
    add_row(7, "每批行数", batch_var)

    ttk.Checkbutton(frm, text="基表完成后导出视图定义（对应 copy_tidb_db.py --include-views）", variable=include_views_var).grid(
        row=8, column=1, sticky="w", **pad
    )

    hint = ttk.Label(
        frm,
        text="默认填充 copy_tidb_db.py 目标库（vitaband_test）。密码请从 Connect 复制后粘贴；默认隐藏，点右侧眼睛可切换可见。",
        foreground="#555",
    )
    hint.grid(row=9, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 4))

    def collect_config() -> dict:
        return {
            "version": 1,
            "host": host_var.get(),
            "port": port_var.get(),
            "user": user_var.get(),
            "password": password_var.get(),
            "database": database_var.get(),
            "sql_file": sql_var.get(),
            "ssl_ca": ssl_ca_var.get(),
            "batch_size": batch_var.get(),
            "include_views": bool(include_views_var.get()),
        }

    def apply_config(data: dict) -> None:
        mapping = {
            "host": host_var,
            "port": port_var,
            "user": user_var,
            "password": password_var,
            "database": database_var,
            "sql_file": sql_var,
            "ssl_ca": ssl_ca_var,
            "batch_size": batch_var,
        }
        for key, var in mapping.items():
            if key in data and data[key] is not None:
                var.set(str(data[key]))
        if "include_views" in data:
            include_views_var.set(bool(data["include_views"]))
        sql_var.set(refresh_sql_file_stamp(sql_var.get(), database_var.get()))

    def config_initial_dir() -> str:
        if DEFAULT_OUT_DIR.is_dir():
            return str(DEFAULT_OUT_DIR)
        return str(here)

    def save_config() -> None:
        path = filedialog.asksaveasfilename(
            title="导出当前配置",
            defaultextension=".json",
            filetypes=[("JSON 配置", "*.json"), ("全部", "*.*")],
            initialfile="export_tidb_config.json",
            initialdir=config_initial_dir(),
        )
        if not path:
            return
        out = Path(path)
        if out.exists():
            if not messagebox.askyesno("覆盖确认", f"文件已存在，是否覆盖？\n{out}"):
                return
        try:
            out.write_text(json.dumps(collect_config(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as e:
            messagebox.showerror("导出配置失败", str(e))
            return
        messagebox.showinfo("完成", f"配置已保存到：\n{out}")

    def load_config() -> None:
        path = filedialog.askopenfilename(
            title="导入配置",
            filetypes=[("JSON 配置", "*.json"), ("全部", "*.*")],
            initialdir=config_initial_dir(),
        )
        if not path:
            return
        src = Path(path)
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            messagebox.showerror("导入配置失败", str(e))
            return
        if not isinstance(data, dict):
            messagebox.showerror("导入配置失败", "配置文件内容必须是 JSON 对象。")
            return
        apply_config(data)
        messagebox.showinfo("完成", f"已从以下文件载入配置：\n{src}")

    btn_row = ttk.Frame(frm)
    btn_row.grid(row=10, column=0, columnspan=2, sticky="w", padx=10, pady=8)
    run_btn = ttk.Button(btn_row, text="执行")
    run_btn.pack(side=tk.LEFT)
    save_cfg_btn = ttk.Button(btn_row, text="导出配置…", command=save_config)
    save_cfg_btn.pack(side=tk.LEFT, padx=(8, 0))
    load_cfg_btn = ttk.Button(btn_row, text="导入配置…", command=load_config)
    load_cfg_btn.pack(side=tk.LEFT, padx=(8, 0))
    ttk.Button(btn_row, text="关闭", command=root.destroy).pack(side=tk.LEFT, padx=(8, 0))

    log_box = scrolledtext.ScrolledText(frm, height=14, wrap=tk.WORD, state=tk.DISABLED)
    log_box.grid(row=11, column=0, columnspan=2, sticky="nsew", padx=10, pady=(4, 0))
    frm.rowconfigure(11, weight=1)

    def log(msg: str) -> None:
        def _append() -> None:
            log_box.configure(state=tk.NORMAL)
            log_box.insert(tk.END, msg + "\n")
            log_box.see(tk.END)
            log_box.configure(state=tk.DISABLED)

        root.after(0, _append)
        print(msg)

    def set_running(running: bool) -> None:
        state = tk.DISABLED if running else tk.NORMAL
        run_btn.configure(state=state)
        save_cfg_btn.configure(state=state)
        load_cfg_btn.configure(state=state)

    def do_export() -> None:
        try:
            args = parse_export_config(
                {
                    "host": host_var.get(),
                    "port": port_var.get(),
                    "user": user_var.get(),
                    "password": password_var.get(),
                    "database": database_var.get(),
                    "sql_file": sql_var.get(),
                    "ssl_ca": ssl_ca_var.get(),
                    "batch_size": batch_var.get(),
                    "include_views": include_views_var.get(),
                },
                refresh_sql_stamp=False,
            )
        except ValueError as e:
            messagebox.showerror("参数错误", str(e))
            return

        out_path = args["sql_file"]
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
                export_database(**args, log=log)
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
    ap = argparse.ArgumentParser(description="Export a TiDB database to a .sql dump.")
    ap.add_argument(
        "--config",
        type=Path,
        help="JSON 配置文件（窗口里「导出配置」保存的那种）。指定后不弹窗，直接导出。",
    )
    ap.add_argument(
        "--keep-sql-path",
        action="store_true",
        help="使用配置里的输出路径；默认会按当前时间生成新的 *-tidebase-YYYYMMDDHHmm.sql",
    )
    args = ap.parse_args()
    if args.config:
        return run_from_config(args.config, refresh_sql_stamp=not args.keep_sql_path)
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
