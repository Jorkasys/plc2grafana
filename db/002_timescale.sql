-- ===========================================================================
--  002_timescale — выполняется ТОЛЬКО если расширение timescaledb доступно.
--  На обычном PostgreSQL приложение этот файл пропускает: всё продолжает
--  работать, просто без гипертаблицы, сжатия и непрерывных агрегатов.
-- ===========================================================================

-- Превращаем обычную таблицу в гипертаблицу (данные, если есть, переносятся).
SELECT create_hypertable(
    'tag_value', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    migrate_data        => TRUE,
    if_not_exists       => TRUE
);

-- Минутные агрегаты -------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS tag_value_1m
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 minute', ts) AS bucket,
       tag_id,
       avg(value)                           AS avg_value,
       min(value)                           AS min_value,
       max(value)                           AS max_value,
       first(value, ts)                     AS first_value,
       last(value, ts)                      AS last_value,
       count(*)                             AS sample_count,
       count(*) FILTER (WHERE quality <> 0) AS bad_count
FROM tag_value
GROUP BY bucket, tag_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy('tag_value_1m',
    start_offset      => INTERVAL '3 hours',
    end_offset        => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists     => TRUE);

-- Часовые агрегаты (поверх минутных — это дёшево) -------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS tag_value_1h
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 hour', bucket) AS bucket,
       tag_id,
       avg(avg_value)             AS avg_value,
       min(min_value)             AS min_value,
       max(max_value)             AS max_value,
       first(first_value, bucket) AS first_value,
       last(last_value, bucket)   AS last_value,
       sum(sample_count)          AS sample_count,
       sum(bad_count)             AS bad_count
FROM tag_value_1m
GROUP BY 1, 2
WITH NO DATA;

SELECT add_continuous_aggregate_policy('tag_value_1h',
    start_offset      => INTERVAL '3 days',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '10 minutes',
    if_not_exists     => TRUE);

-- Realtime: агрегат «достраивается» свежими сырыми данными на лету
ALTER MATERIALIZED VIEW tag_value_1m SET (timescaledb.materialized_only = false);
ALTER MATERIALIZED VIEW tag_value_1h SET (timescaledb.materialized_only = false);

-- Сжатие сырых данных старше 7 дней (обычно 10–20x)
ALTER TABLE tag_value SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'tag_id',
    timescaledb.compress_orderby   = 'ts DESC'
);

SELECT add_compression_policy('tag_value', INTERVAL '7 days', if_not_exists => TRUE);
