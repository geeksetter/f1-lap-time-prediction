# Проверка исходных данных OpenF1

Сформировано: 2026-10-13T21:14:05.927511+00:00

Исходные наблюдения сохранены в PostgreSQL без заполнения пропусков и фильтрации кругов.

| Год | Сессия | Пилоты | Круги | Отрезки | Погода | События | Нет времени круга | Нет начала круга | Дождевые замеры |
|---|---|---|---|---|---|---|---|---|---|
| 2023 | 9102 | 20 | 1312 | 63 | 154 | 49 | 2 | 2 | 0 |
| 2024 | 9539 | 20 | 1310 | 62 | 154 | 107 | 1 | 1 | 1 |
| 2025 | 9971 | 19 | 1204 | 73 | 155 | 102 | 2 | 0 | 0 |

## Связи

```json
[
  {
    "year": 2023,
    "laps": 1312,
    "unmatched_stint": 0,
    "multiple_stints": 0,
    "missing_prior_weather": 0,
    "max_weather_age_seconds": "59.999000"
  },
  {
    "year": 2024,
    "laps": 1310,
    "unmatched_stint": 0,
    "multiple_stints": 0,
    "missing_prior_weather": 0,
    "max_weather_age_seconds": "59.882000"
  },
  {
    "year": 2025,
    "laps": 1204,
    "unmatched_stint": 2,
    "multiple_stints": 0,
    "missing_prior_weather": 0,
    "max_weather_age_seconds": "59.948000"
  }
]
```

## Замечания

- {"severity": "medium", "year": 2023, "issue": "Null lap timing; preserve now, exclude or resolve in stage 2", "null_duration": 2, "null_start": 2}
- {"severity": "medium", "year": 2024, "issue": "Null lap timing; preserve now, exclude or resolve in stage 2", "null_duration": 1, "null_start": 1}
- {"severity": "medium", "year": 2024, "issue": "Dry scope needs temporal rainfall filtering", "rain_samples": 1, "rain_unknown": 0}
- {"severity": "medium", "year": 2025, "issue": "Null lap timing; preserve now, exclude or resolve in stage 2", "null_duration": 2, "null_start": 0}
- {"severity": "high", "issue": "Join coverage requires stage-2 treatment", "year": 2025, "laps": 1204, "unmatched_stint": 2, "multiple_stints": 0, "missing_prior_weather": 0, "max_weather_age_seconds": "59.948000"}

## Границы интерпретации

- Пропуски не означают нулевое время круга; источник сохранён без подстановок.
- Номер пилота является ключом внутри сессии, а не постоянным идентификатором человека.
- Отсутствие дождевых замеров не доказывает сухость каждого участка трассы.
- Круги с флагами, ограничениями и въездами в боксы будут отфильтрованы на следующем этапе.
- Лаповые строки зависимы; три гонки дают только три независимых гоночных условия.
- Составы SOFT/MEDIUM/HARD относительны выбранному гоночному уикенду.
- OpenF1 date_start круга приблизителен; точность временных объединений ограничена источником.
- Полные профили, частоты, происхождение и SHA-256 находятся в quality.json.
