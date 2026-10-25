from pathlib import Path

import lancedb
import pandas as pd
from dotenv import load_dotenv
from lancedb.embeddings import get_registry

load_dotenv(override=True)
DATA = Path(__file__).resolve().parent.parent / "data"
# OpenRouter rejects the null `dimensions` LanceDB sends when dim is unset
embedder = get_registry().get("openai").create(name="text-embedding-3-small", dim=1536)


def search(text: str, k: int = 10) -> pd.DataFrame:
    tbl = lancedb.connect(DATA / "lancedb").open_table("request_types")
    return tbl.search(text, query_type="hybrid").limit(k).to_pandas()
