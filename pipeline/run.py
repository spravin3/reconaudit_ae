"""
Pipeline entrypoint. Run with:  uv run python -m pipeline.run
"""

import os
from pathlib import Path

import dlt
from dotenv import load_dotenv

from pipeline.sources import reconaudit_source

load_dotenv()


def run_pipeline(
    data_path: str = "data",
    db_path: str | None = None,
) -> dlt.Pipeline:
    db_path = db_path or os.getenv("DUCKDB_PATH", "reconaudit.duckdb")

    pipeline = dlt.pipeline(
        pipeline_name="reconaudit",
        destination=dlt.destinations.duckdb(credentials=db_path),
        dataset_name="raw",
        dev_mode=False,
    )

    info = pipeline.run(reconaudit_source(data_path=data_path))
    print(info)
    return pipeline


if __name__ == "__main__":
    run_pipeline()
