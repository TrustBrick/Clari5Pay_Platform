"""
Simple AWS RDS connection test — username + password only.
Run:  python test_aws_connection.py
(Do NOT paste anything from the AWS "Connect with IAM authentication" page here.)
"""
import psycopg2

HOST     = "clari5pay.c76auiocst4e.eu-north-1.rds.amazonaws.com"
PORT     = 5432
DBNAME   = "postgres"
USER     = "postgres"
PASSWORD = "PUT_YOUR_RDS_MASTER_PASSWORD_HERE"   # fill in locally; never commit a real password

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
