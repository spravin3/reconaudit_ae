with date_spine as (
    select unnest(
        generate_series(
            (select min(tx_timestamp)::date from {{ ref('stg_server_logs') }}),
            (select max(tx_timestamp)::date from {{ ref('stg_server_logs') }}),
            interval '1 day'
        )
    )::date as date_day
)

select
    date_day,
    extract(year  from date_day)::int                  as year,
    extract(month from date_day)::int                  as month,
    extract(day   from date_day)::int                  as day_of_month,
    extract(dow   from date_day)::int                  as day_of_week_num,
    strftime(date_day, '%A')                           as day_of_week_name,
    strftime(date_day, '%B')                           as month_name,
    extract(dow from date_day) in (0, 6)               as is_weekend
from date_spine
