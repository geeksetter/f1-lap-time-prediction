# Фактически проверенное воспроизведение

Проверка 14.10.2026 выполнена в отдельном локальном Git-клоне `ae8819f` с финальными изменениями установки/CLI, новой виртуальной средой и новым пустым кластером PostgreSQL. Исходный проект и его БД не использовались как вход. Все семь исходных таблиц сначала имели 0 строк. Кластер слушал только `127.0.0.1:55433`. Пароль создан `configure_env.py`, не публикуется.

## Проверенные команды

Команды приведены относительно корня нового клона; интерпретатор `.venv` использован явно. Создание venv выполнялось Python 3.12.6.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python scripts/configure_env.py --port 55433
.venv/bin/python scripts/local_postgres.py start
.venv/bin/f1-pipeline init-db
.venv/bin/f1-pipeline counts
.venv/bin/f1-pipeline collect --years 2023 2024 2025 --country Spain --circuit Catalunya --verify-repeat
.venv/bin/f1-pipeline quality --output .local/reports
.venv/bin/f1-pipeline prepare --years 2023 2024 2025 --circuit Catalunya --output .local/reports --verify-repeat
.venv/bin/f1-pipeline eda --output .local/reports
.venv/bin/f1-pipeline train --output .local/experiment
.venv/bin/f1-pipeline evaluate --artifacts .local/experiment --output .local/reports/modeling
.venv/bin/f1-pipeline predict --artifacts .local/experiment --session 9971 --driver 1 --lap 40
.venv/bin/python scripts/check_notebook.py notebooks/01_data_quality.ipynb
.venv/bin/python scripts/check_notebook.py notebooks/02_preparation_eda.ipynb
.venv/bin/pytest -q -m 'not integration'
```

Зафиксированы Python 3.12.6, PostgreSQL 16.14, NumPy 2.5.4, pandas 2.3.3, Matplotlib 3.11.2, requests 2.34.2, psycopg 3.3.6, pytest 9.1.1. Полный список — `requirements.lock.txt`; фактически установленные основные версии и доказательства — [final_verification.json](../reports/final_verification.json).

Повтор загрузки: одинаковые количества и SHA-256 ответов; 3826 исходных кругов. Повтор подготовки: одинаковые входной/выходной хеши, 3362 принятых, 464 исключённых. SQL-проверки уникальности, возраста шин, прошлой погоды, истории и сохранности исходных таблиц прошли. Новый каталог модели действительно обучен, а не скопирован из `artifacts/stage3`. Метрики всех пяти методов совпали без разницы в используемой численной точности. Исторический прогноз примера — 78.90700790885019 с.

Два ноутбука выполнены полностью. Offline: **40 passed**. PostgreSQL integration: **11 passed**. `ruff check src scripts tests` и `docker compose config --quiet` прошли. Запуск Docker Compose локально не выполнен: daemon не запущен. Реальная проверка развёртывания использовала поддерживаемый нативный путь. CI дополнительно запускает PostgreSQL 16 как контейнерный service.

## Отдельная тестовая БД

Интеграционные тесты очищают тестовые таблицы. Никогда не направляйте `TEST_DATABASE_URL` на рабочую БД. Защита проверяет окончание имени БД `_test`.

Следующий блок создаёт отдельную БД в уже запущенном собственном локальном PostgreSQL и передаёт URL тестам без вывода пароля:

```bash
python - <<'PY'
import os
import subprocess
import sys
from dotenv import dotenv_values
import psycopg
from psycopg import sql

config = psycopg.conninfo.conninfo_to_dict(dotenv_values('.env')['DATABASE_URL'])
config['dbname'] = 'postgres'
with psycopg.connect(**config, autocommit=True) as conn:
    exists = conn.execute(
        'SELECT 1 FROM pg_database WHERE datname=%s', ('f1_ingestion_test',)
    ).fetchone()
    if not exists:
        conn.execute(sql.SQL('CREATE DATABASE {}').format(
            sql.Identifier('f1_ingestion_test')
        ))
config['dbname'] = 'f1_ingestion_test'
env = os.environ.copy()
env['TEST_DATABASE_URL'] = psycopg.conninfo.make_conninfo(**config)
subprocess.run([sys.executable, '-m', 'pytest', '-q', '-m', 'integration'],
               env=env, check=True)
PY
```

Именно такой способ проверен в чистом кластере. В CI тестовая БД создаётся сервисом Actions и не содержит реальные данные. Сетевой доступ к OpenF1 не нужен ни offline, ни интеграционным тестам: для загрузки применяются тестовые ответы.

## Поведение повторных запусков

Журналы API и подготовки пополняются; число предметных строк при неизменном источнике сохраняется. Текущая производная выборка заменяется атомарно; предыдущий аудит запуска сохраняется. Если источник изменился, повторный хеш может отличаться: это требует изучения, а не принудительного объявления идентичности.

Эксперимент после оценки блокирует изменение кода/выборки/весов под уже изученный тест. Для воспроизведения с нуля используется новый `.local/experiment`; опубликованный `artifacts/stage3` сохраняется. Создание нового каталога не разрешает подбор под прежнюю итоговую гонку: для развития нужна новая заранее отложенная гонка.

Нативный кластер можно остановить `python scripts/local_postgres.py stop`; эта команда адресует только `.local/pgdata` текущего клона. Docker-путь останавливается `docker compose down`, без удаления тома с данными.
