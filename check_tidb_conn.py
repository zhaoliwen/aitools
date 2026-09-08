# -*- coding: utf-8 -*-
"""检查 TiDB Cloud 连通性：账号、库名、SSL。密码用参数传入，不要写进仓库。"""
import argparse
import ssl
import sys

import pymysql


def try_connect(title, host, port, user, password, database, ssl_mode):
    print("---- %s ----" % title)
    print("host=%s port=%s user=%s db=%s ssl=%s" % (host, port, user, database or "(none)", ssl_mode))
    kwargs = {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "connect_timeout": 15,
        "charset": "utf8mb4",
    }
    if database:
        kwargs["database"] = database
    if ssl_mode == "required":
        kwargs["ssl"] = {"ssl": {}}
    elif ssl_mode == "verify":
        ctx = ssl.create_default_context()
        kwargs["ssl"] = ctx
    try:
        conn = pymysql.connect(**kwargs)
        with conn.cursor() as cur:
            cur.execute("SELECT CURRENT_USER(), DATABASE(), VERSION()")
            print("OK", cur.fetchone())
            cur.execute("SHOW DATABASES")
            dbs = [row[0] for row in cur.fetchall()]
            print("databases:", ", ".join(dbs))
        conn.close()
        return True
    except Exception as e:
        print("FAIL", type(e).__name__, e)
        return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="gateway01.us-west-2.prod.aws.tidbcloud.com")
    p.add_argument("--port", type=int, default=4000)
    p.add_argument("--user", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--database", default="")
    args = p.parse_args()

    ok = False
    for ssl_mode in ("required", "verify", "off"):
        ok = try_connect(
            "ssl=%s" % ssl_mode,
            args.host,
            args.port,
            args.user,
            args.password,
            args.database or None,
            ssl_mode,
        ) or ok
        print()
    if args.database:
        print("再试：同一账号不指定库名")
        try_connect(
            "no-database ssl=required",
            args.host,
            args.port,
            args.user,
            args.password,
            None,
            "required",
        )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
