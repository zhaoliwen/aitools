"""
Import a .sql dump into TiDB Cloud over TLS.

Requirements:
  pip install pymysql sqlparse

TiDB Cloud (public gateway) requires TLS. Older MySQL 5.x Windows clients sometimes
cannot complete the TLS handshake against modern gateways; Python + pymysql works.

Important:
  - Use the prefixed root user shown in TiDB Cloud "Connect" (e.g. 4RNtEwraRC28sKX.root).
    Plain "root" is rejected (missing username prefix).
  - Whitelist your current public IP in TiDB Cloud before connecting.

Optional:
  Download "CA cert" from the TiDB Cloud Connect dialog and pass --ssl-ca for full verification.
  Without --ssl-ca, this script encrypts TLS but does not validate the server certificate chain.
"""

from __future__ import annotations

import argparse
import re
import ssl
import sys
from pathlib import Path

import pymysql
import sqlparse
from pymysql.err import OperationalError


def _latin1_ok(label_cn: str, value: str) -> str | None:
    """pymysql 在建立连接时会把用户名/口令按 latin-1 编码；含中文或占位符会触发 UnicodeEncodeError。"""
    try:
        value.encode("latin1")
    except UnicodeEncodeError:
        return (
            f"{label_cn} 含有非 Latin-1 字符，无法用于当前 pymysql 连接。"
            " 请使用 TiDB「Connect / Parameters」里通过复制按钮得到的真实用户名与密码，"
            "不要把说明文档里的占位符（例如「从控制台复制的密码」或带尖括号的一整段）原样当作参数。"
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
            "连接被拒绝（MySQL 1045）：用户名/密码不对，或 **执行脚本的机器的公网 IP 未加入白名单**。\n"
            f"  • 白名单：报错里 @ 之后是本脚本的出口公网 IP（当前为 {ip}）。请在 TiDB Cloud 该集群的「Trusted sources / IP 访问」里 **新增** 这一条；\n"
            "    控制台绿条里「当前 IP 已允许」通常是你 **浏览器上网** 的出口地址，**不等于** 运行 Python 的那台电脑的地址；必须在白名单里单独加上 **跑 py 命令的机器** 的公网 IP。\n"
            "  • 密码：在 Connect → Parameters 用「复制」粘贴 Password，避免手打；注意易混字符（如 I 与 l、0 与 O）。\n"
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

r'''
py -3 g:\codes\import_sql_tidb.py --host gateway01.ap-southeast-1.prod.alicloud.tidbcloud.com --port 4000 --user 2AtNsm9Nf83Xr7d.root --password XqItDX318Yae1BPg --database bootdo --sql-file G:\文件\bootdo-tidebase.sql
'''

def main() -> int:
    ap = argparse.ArgumentParser(description="Import SQL dump into TiDB Cloud (TLS).")
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=4000)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--database", default="bootdo")
    ap.add_argument("--sql-file", required=True, type=Path)
    ap.add_argument("--ssl-ca", dest="ssl_ca", default=None)
    ap.add_argument("--commit-every", type=int, default=200)

    args = ap.parse_args()
    args.user = args.user.strip()
    args.password = args.password.strip()
    args.database = (args.database or "").strip()

    if msg := _latin1_ok("用户名", args.user):
        print(msg, file=sys.stderr)
        return 2
    if msg := _latin1_ok("密码", args.password):
        print(msg, file=sys.stderr)
        return 2

    sql_path = Path(args.sql_file)
    if not sql_path.is_file():
        print(f"missing sql file: {sql_path}", file=sys.stderr)
        return 2

    raw = sql_path.read_text(encoding="utf-8", errors="replace")
    stmts = [s.strip().rstrip(";").strip() for s in sqlparse.split(raw)]
    stmts = [s for s in stmts if s]

    ssl_ctx = tls_context(args.ssl_ca)

    try:
        conn = pymysql.connect(
            host=args.host,
            port=args.port,
            user=args.user,
            password=args.password,
            database=args.database,
            charset="utf8mb4",
            autocommit=False,
            ssl=ssl_ctx,
        )
    except OperationalError as e:
        if hint := _connect_access_denied_hint(e):
            print(hint, file=sys.stderr)
            return 1
        raise

    cur = conn.cursor()
    executed = 0

    try:
        for s in stmts:
            cur.execute(s)
            executed += 1
            if args.commit_every and executed % args.commit_every == 0:
                conn.commit()
                print(f"progress: executed {executed} statements...")
        conn.commit()
        print(f"finished OK: executed {executed} statements.")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
