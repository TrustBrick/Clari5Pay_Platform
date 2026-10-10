"""
Simple AWS RDS connection test — username + password only.

This is a MANUAL diagnostic script, not an automated unit test. It connects to a real RDS
endpoint, so it must never run during test collection/CI. Everything lives under the
``__main__`` guard below (including the ``psycopg2`` import) so importing this file has no side
effects and requires no PostgreSQL driver — the automated suite (backend/tests/) does not use
psycopg2; the app's runtime driver is asyncpg.

Run:  python test_aws_connection.py
      (install the driver first if needed:  pip install psycopg2-binary)
(Do NOT paste anything from the AWS "Connect with IAM authentication" page here.)
"""

HOST     = "your-rds-endpoint.<region>.rds.amazonaws.com"
PORT     = 5432
DBNAME   = "postgres"
USER     = "postgres"
PASSWORD = "PUT_YOUR_RDS_MASTER_PASSWORD_HERE"   # fill in locally; never commit a real password


def main() -> None:
    import psycopg2  # imported here so test collection never requires the driver

    try:
        conn = psycopg2.connect(
            host=HOST, port=PORT, dbname=DBNAME, user=USER,
            password=PASSWORD, sslmode="require", connect_timeout=8,
        )
        cur = conn.cursor()
        cur.execute("SELECT version();")
        print("OK - connected to AWS RDS!")
        print("   ", cur.fetchone()[0].split(",")[0])
        cur.close()
        conn.close()
    except Exception as e:
        print("FAILED:", e)


if __name__ == "__main__":
    main()
