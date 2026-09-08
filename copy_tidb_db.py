"""
将源 TiDB 库中的全部基表（结构 + 数据）复制到目标 TiDB。

Requirements:
  pip install pymysql

连接方式参考 import_sql_tidb.py：TiDB Cloud 公网网关需 TLS；
可用 --ssl-ca 指向 CA（如 isrgrootx1_ca.pem）做证书校验。

示例：
  py -3 copy_tidb_db.py
  py -3 copy_tidb_db.py --ssl-ca isrgrootx1_ca.pem --batch-size 500
"""

from __future__ import annotations

import argparse
import re
import ssl
import sys
from pathlib import Path

import pymysql
from pymysql.err import OperationalError

# ---------- 以下连接辅助逻辑自 import_sql_tidb.py 复制并沿用 ----------


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
            "  2. 在 Connect 里点 Generate Password 重新生成，立刻用「复制」粘贴到 --dst-password（不要手打、不要用旧截图）。\n"
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
    """建立 TiDB TLS 连接；database 可为 None（仅建连，再 CREATE DATABASE）。"""
    user = user.strip()
    password = password.strip()
    if database is not None:
        database = database.strip() or None

    if msg := _latin1_ok(f"{label} 用户名", user):
        raise SystemExit(msg)
    if msg := _latin1_ok(f"{label} 密码", password):
        raise SystemExit(msg)

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
            print(f"[{label}] {hint}", file=sys.stderr)
            raise SystemExit(1) from e
        raise


# ---------- 复制逻辑 ----------


def list_base_tables(cur) -> list[str]:
    cur.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
    # 列名形如 Tables_in_xxx / Table_type
    return [row[0] for row in cur.fetchall()]


def list_views(cur) -> list[str]:
    cur.execute("SHOW FULL TABLES WHERE Table_type = 'VIEW'")
    return [row[0] for row in cur.fetchall()]


def ensure_database(conn, database: str) -> None:
    cur = conn.cursor()
    try:
        cur.execute(
            f"CREATE DATABASE IF NOT EXISTS `{database}` "
            "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"
        )
        conn.commit()
        cur.execute(f"USE `{database}`")
    finally:
        cur.close()


def copy_table_structure(src_cur, dst_cur, table: str) -> None:
    src_cur.execute(f"SHOW CREATE TABLE `{table}`")
    row = src_cur.fetchone()
    if not row:
        raise RuntimeError(f"无法获取表结构: {table}")
    create_sql = row[1]
    dst_cur.execute(f"DROP TABLE IF EXISTS `{table}`")
    dst_cur.execute(create_sql)


def copy_table_data(src_cur, dst_cur, table: str, batch_size: int) -> int:
    src_cur.execute(f"SELECT * FROM `{table}`")
    cols = [d[0] for d in src_cur.description]
    if not cols:
        return 0

    col_list = ", ".join(f"`{c}`" for c in cols)
    placeholders = ", ".join(["%s"] * len(cols))
    insert_sql = f"INSERT INTO `{table}` ({col_list}) VALUES ({placeholders})"

    total = 0
    while True:
        rows = src_cur.fetchmany(batch_size)
        if not rows:
            break
        dst_cur.executemany(insert_sql, rows)
        total += len(rows)
    return total


def copy_view(src_cur, dst_cur, view: str) -> None:
    src_cur.execute(f"SHOW CREATE VIEW `{view}`")
    row = src_cur.fetchone()
    if not row:
        raise RuntimeError(f"无法获取视图定义: {view}")
    # SHOW CREATE VIEW 返回: View, Create View, character_set_client, collation_connection
    create_sql = row[1]
    dst_cur.execute(f"DROP VIEW IF EXISTS `{view}`")
    dst_cur.execute(create_sql)


def main() -> int:
    here = Path(__file__).resolve().parent
    default_ca = here / "isrgrootx1_ca.pem"

    ap = argparse.ArgumentParser(description="Copy all tables/data between TiDB Cloud clusters.")
    # 源库（bootdo）
    ap.add_argument("--src-host", default="gateway01.us-west-2.prod.aws.tidbcloud.com")
    ap.add_argument("--src-port", type=int, default=4000)
    ap.add_argument("--src-user", default="2AtNsm9Nf83Xr7d.root")
    ap.add_argument("--src-password", default="1oan6hGQ7SOy5diW")
    ap.add_argument("--src-database", default="bootdo")
    # 目标库（badyfat 集群；业务库默认 bodyfatdb）
    ap.add_argument("--dst-host", default="gateway01.sa-east-1.prod.aws.tidbcloud.com")
    ap.add_argument("--dst-port", type=int, default=4000)
    ap.add_argument("--dst-user", default="ug21YiZX1lSH2pL.root")
    # 不设默认密码：截图/手打易错；必须从 Connect「复制」后通过参数传入
    ap.add_argument("--dst-password", required=True, help="从目标集群 Connect 复制的密码（必填）")
    ap.add_argument(
        "--dst-database",
        default="vitaband_test",
        help="目标业务库名；不存在则自动创建。勿使用 sys。",
    )
    ap.add_argument(
        "--ssl-ca",
        dest="ssl_ca",
        default=str(default_ca) if default_ca.is_file() else None,
    )
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument(
        "--include-views",
        action="store_true",
        help="基表复制完成后，再复制视图定义",
    )

    args = ap.parse_args()
    if args.dst_database.strip().lower() == "sys":
        print("拒绝写入系统库 sys，请指定业务库（默认 bodyfatdb）。", file=sys.stderr)
        return 2

    print(f"连接源库 {args.src_host}/{args.src_database} ...")
    src = connect_tidb(
        host=args.src_host,
        port=args.src_port,
        user=args.src_user,
        password=args.src_password,
        database=args.src_database,
        ssl_ca=args.ssl_ca,
        label="源库",
    )
    print(f"连接目标集群 {args.dst_host}（先不指定库，稍后创建/使用 {args.dst_database}）...")
    dst = connect_tidb(
        host=args.dst_host,
        port=args.dst_port,
        user=args.dst_user,
        password=args.dst_password,
        database=None,
        ssl_ca=args.ssl_ca,
        label="目标库",
    )

    src_cur = src.cursor()
    dst_cur = dst.cursor()

    try:
        ensure_database(dst, args.dst_database)
        tables = list_base_tables(src_cur)
        print(f"源库基表数量: {len(tables)}")
        if not tables:
            print("源库没有基表，结束。")
            return 0

        dst_cur.execute("SET FOREIGN_KEY_CHECKS=0")
        dst.commit()

        for i, table in enumerate(tables, 1):
            print(f"[{i}/{len(tables)}] 复制表结构: {table}")
            copy_table_structure(src_cur, dst_cur, table)
            dst.commit()

            print(f"[{i}/{len(tables)}] 复制表数据: {table} ...", end="", flush=True)
            n = copy_table_data(src_cur, dst_cur, table, args.batch_size)
            dst.commit()
            print(f" {n} 行")

        if args.include_views:
            views = list_views(src_cur)
            print(f"源库视图数量: {len(views)}")
            for i, view in enumerate(views, 1):
                print(f"[视图 {i}/{len(views)}] {view}")
                copy_view(src_cur, dst_cur, view)
                dst.commit()

        dst_cur.execute("SET FOREIGN_KEY_CHECKS=1")
        dst.commit()
        print("全部完成。")
        return 0
    except Exception:
        dst.rollback()
        raise
    finally:
        src_cur.close()
        dst_cur.close()
        src.close()
        dst.close()


if __name__ == "__main__":
    raise SystemExit(main())

# 执行命令：
# py -3 copy_tidb_db.py --dst-user "粘贴的用户名" --dst-password "粘贴的密码"


################################################################################

# ERROR 2026 (HY000): SSL connection error: unknown error number

# 这是 本机 MySQL 客户端 TLS 握手失败（Windows 上旧版 mysql 客户端连 TiDB Cloud 很常见），不是 SQL 写错。你们仓库里的 import_sql_tidb.py 注释也写了这一点。

# 用 Python 删库（和复制脚本同一套连接方式）：

# py -3 -c "import ssl,pymysql; from pathlib import Path; ca=str(Path('isrgrootx1_ca.pem').resolve()); ctx=ssl.create_default_context(cafile=ca); c=pymysql.connect(host='gateway01.sa-east-1.prod.aws.tidbcloud.com',port=4000,user='你的前缀.root',password='你的密码',charset='utf8mb4',ssl=ctx); cur=c.cursor(); cur.execute('DROP DATABASE IF EXISTS `test`'); c.commit(); print('done'); c.close()"
# 在 aitools 目录下执行，把用户名/密码换成目标集群 Connect 里复制的。

# 其它可选：

# 用 DBeaver / DataGrip，开启 SSL，CA 指向 isrgrootx1_ca.pem
# 升级到较新的 MySQL Shell / MariaDB 客户端后再用 --ssl-mode=VERIFY_IDENTITY --ssl-ca=...
# 不要用不带 TLS 的连接；TiDB Cloud 公网要求 TLS。