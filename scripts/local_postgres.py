"""Start an isolated existing-installation PostgreSQL; never stop another cluster."""
import argparse
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile

from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['start','stop','status']);args=p.parse_args()
    if not shutil.which('pg_config'): p.error('Install PostgreSQL and add pg_config to PATH, or use Docker Compose')
    bindir=Path(subprocess.check_output(['pg_config','--bindir'],text=True).strip())
    data=ROOT/'.local/pgdata';local=ROOT/'.local';local.mkdir(exist_ok=True)
    ctl=[str(bindir/'pg_ctl'),'-D',str(data)]
    if args.action!='start':
        raise SystemExit(subprocess.run(ctl+[args.action]).returncode)
    envfile=ROOT/'.env'
    if not envfile.exists():
        password=secrets.token_urlsafe(24)
        envfile.write_text(f'POSTGRES_USER=f1\nPOSTGRES_DB=f1_project\nPOSTGRES_PASSWORD={password}\nPOSTGRES_PORT=55432\nDATABASE_URL=postgresql://f1:{password}@127.0.0.1:55432/f1_project\n')
        envfile.chmod(0o600)
    cfg=dotenv_values(envfile);user=cfg.get('POSTGRES_USER','f1');port=cfg.get('POSTGRES_PORT','55432');database=cfg.get('POSTGRES_DB','f1_project')
    password=cfg.get('POSTGRES_PASSWORD')
    if not password: p.error('POSTGRES_PASSWORD is missing in .env')
    if not (data/'PG_VERSION').exists():
        with tempfile.NamedTemporaryFile(mode='w') as f:
            f.write(password);f.flush()
            subprocess.run([str(bindir/'initdb'),'-D',str(data),'-U',user,'--auth-host=scram-sha-256',
                            '--auth-local=trust','--pwfile='+f.name,'--encoding=UTF8','--locale=C'],check=True,stdout=subprocess.DEVNULL)
    if subprocess.run(ctl+['status'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
        socket=Path('/tmp')/('f1-pg-'+hashlib.sha256(str(ROOT).encode()).hexdigest()[:8]);socket.mkdir(exist_ok=True,mode=0o700)
        subprocess.run(ctl+['-l',str(local/'postgres.log'),'-o',f'-h 127.0.0.1 -p {port} -k {socket}','start'],check=True)
    env=dict(os.environ,PGPASSWORD=password)
    common=['-h','127.0.0.1','-p',port,'-U',user]
    # SQL values use psycopg placeholders, not shell or SQL string interpolation.
    import psycopg
    from psycopg import sql
    with psycopg.connect(host='127.0.0.1',port=port,user=user,password=password,dbname='postgres',autocommit=True) as c:
        if not c.execute('SELECT 1 FROM pg_database WHERE datname=%s',(database,)).fetchone():
            c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
    print(f'PostgreSQL ready on 127.0.0.1:{port}; database {database}; password stays in .env')


if __name__=='__main__': main()
