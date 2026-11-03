"""Load the SYNTHETIC SAMPLE files into the configured database (DB_PATH).

Usage:
    python scripts/load_samples.py                # initial demo files (no conflicting update)
    python scripts/load_samples.py --with-update  # also the 4th Tidewell file (creates the conflict)

Runs the same pipeline as an upload: parse, chunk, embed, extract metrics with the
real extraction model (LLM_EXTRACT_MODEL). Re-running replaces files with the same name.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.extract import extract_metrics  # noqa: E402
from app.ingest import ingest_document  # noqa: E402
from app.llm import LLMError, get_llm  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--with-update", action="store_true", help="also load the conflicting 4th file")
    args = parser.parse_args()

    settings = get_settings()
    conn = connect(settings.db_path)
    init_db(conn)
    llm = get_llm("extract")
    print(f"Database: {settings.db_path}")

    for item in json.loads((SAMPLES / "manifest.json").read_text()):
        if not item["initial"] and not args.with_update:
            continue
        row = conn.execute("SELECT id FROM companies WHERE name = ?", (item["company"],)).fetchone()
        company_id = row["id"] if row else conn.execute(
            "INSERT INTO companies(name) VALUES (?)", (item["company"],)).lastrowid
        conn.commit()
        path = SAMPLES / item["path"]
        doc = ingest_document(conn, company_id, path.name, item["doc_type"], item["doc_date"], path.read_bytes())
        try:
            result = extract_metrics(conn, doc["document_id"], llm)
            extraction = f"metrics kept {result['kept']}, dropped {len(result['dropped'])}"
        except LLMError as e:
            extraction = f"extraction failed: {e}"
        print(f"{item['company']:<20} {path.name:<32} {doc['chunks']:>3} chunks, {extraction}")
    conn.close()


if __name__ == "__main__":
    main()
